"""
Term resolver — M1/M2.

Processes every body segment and writes term_usage rows with full provenance.
M2: DeepSeek V3 proposes Slovak terms for ALL gap lemmas (not in Krystal) in a
pre-scan batch pass before the main resolution loop.

Order of operations (strict per spec):
  1. Phrase-match multiword glossary_term entries first.
  2. Lemmatize remaining Latin tokens with CLTK.
  3. Resolve each matched term's sense:
     - Single-sense  → krystal_single,        confidence=auto
     - Multi-sense   → evidence vote from cs/en signals:
         consistent + ≥1 rank≤20 signal → krystal_multi_voted,    confidence=auto
         otherwise                       → krystal_multi_flagged,  confidence=needs_review
     - Not in Krystal (gap term):
         Bahounek cs available  → bahounek_derived,  confidence=needs_review
         English en available   → english_derived,   confidence=needs_review
         nothing                → model_proposed,    confidence=needs_review
         All three methods receive a DeepSeek-proposed Slovak term.
         The method label indicates what context was available for the proposal.
  4. Write term_usage rows.

Gap term proposal configuration (knobs):
  GAP_FREQ_FLOOR  — min segments a lemma must appear in (default 10)
  GAP_BATCH_SIZE  — lemmas per DeepSeek batch call (default 25)
  GAP_MAX_WORKERS — concurrent batch requests (default 10)
  Precision is dynamic: the model assigns each gap lemma a category
  (term/name/formula/prose) stored on glossary_term.category and overridable in M3.
  No POS filter, no static word lists — only a mechanical length gate + suffix strip.

Determinism: all intermediate collections are sorted; no randomness.

DeepSeek env vars:
  DEEPSEEK_API_KEY  — required when gap terms exist
  DEEPSEEK_API_URL  — default: https://api.deepseek.com/v1/chat/completions
  DEEPSEEK_MODEL    — default: deepseek-chat

Run:
  uv run python -m ingest.resolver
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import psycopg2.extras
import requests

from ingest.db import get_conn, source_id, work_id
from ingest.lemmatize import lemmatize_czech, lemmatize_latin

ROOT = Path(__file__).resolve().parents[2]

# Authority rank threshold for a "strong" signal (Krystal=10, Bahounek=20)
_STRONG_RANK_THRESHOLD = 20

# Element types to run the resolver on (skip title/preamble segments)
_BODY_TYPES = {"arg", "sed_contra", "respondeo", "reply"}

# ── Gap term proposal knobs ───────────────────────────────────────────────────
# Override via run() parameters or GAP_* env vars read in pipeline.py.

_GAP_FREQ_FLOOR: int = 10
# Mechanical pre-filter only: a lemma must be longer than this to be sent for
# proposal (filters most Latin function words). No POS filter, no word lists —
# precision is handled dynamically by the model's category (see _GAP_CATEGORIES).
_GAP_MIN_LEN: int = 5
_GAP_BATCH_SIZE: int = 50   # lemmas per DeepSeek batch call
_GAP_MAX_WORKERS: int = 10  # concurrent batch requests

# Model-assigned gap-term categories (stored in glossary_term.category, overridable
# in M3). 'term'/'name'/'formula' are kept-and-locked; 'prose' is ordinary vocab.
_GAP_CATEGORIES: frozenset[str] = frozenset({"term", "name", "formula", "prose"})


def _strip_lemma_suffix(lemma: str) -> str:
    """Strip CLTK's trailing numeric disambiguation suffix (dico2 → dico).

    Mechanical and general — not a word list. The model's `canonical` field does the
    real headword normalization; this only removes the bare numeric tag CLTK appends.
    """
    return re.sub(r"\d+$", "", lemma)

# ── DeepSeek API ──────────────────────────────────────────────────────────────

_api_stats: dict[str, int] = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
_api_stats_lock = threading.Lock()

_DEEPSEEK_URL = os.environ.get(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions"
)
_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")




def _parse_batch_entry(input_lemma: str, value) -> dict | None:
    """Normalize one model entry into {category, slovak}.

    Returns None for malformed entries (caller fills a fallback). Accepts a plain
    string (legacy/loose model output) by treating it as the slovak term with no category.
    """
    if isinstance(value, str):
        slovak = value.strip()
        if not slovak:
            return None
        return {"category": None, "slovak": slovak}

    if not isinstance(value, dict):
        return None

    slovak = str(value.get("slovak", "")).strip()
    if not slovak:
        return None
    category = value.get("category")
    if category is not None:
        category = str(category).strip().lower()
        if category not in _GAP_CATEGORIES:
            category = None
    return {"category": category, "slovak": slovak}


def _call_deepseek_batch(batch: list[dict]) -> dict[str, dict]:
    """Classify and translate a batch of Latin gap lemmas in one call.

    Each item: {"lemma": str, "best_latin": str, "best_czech": str, "best_english": str}
    Returns {input_lemma: {"category": str|None, "slovak": str}}.
    Missing/malformed entries are omitted; the caller fills per-lemma fallbacks.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. "
            "Export it before running the resolver."
        )

    lines = []
    for item in batch:
        parts = [f"- {item['lemma']}"]
        if item.get("best_latin"):
            parts.append(f"Latin: {item['best_latin'][:150]}")
        if item.get("best_czech"):
            parts.append(f"Czech: {item['best_czech'][:80]}")
        if item.get("best_english"):
            parts.append(f"English: {item['best_english'][:80]}")
        lines.append(" | ".join(parts))

    prompt = (
        "You are a Slovak theological terminologist working on Thomas Aquinas's Summa Theologiae.\n"
        "For each Latin lemma below (with Czech/English context excerpts), return two fields:\n"
        '  "category" — one of: "term" (theological/philosophical content word),\n'
        '               "name" (proper noun, e.g. Christus, Augustinus, philosophus=Aristotle),\n'
        '               "formula" (recurring structural/formulaic connective, e.g. Praeterea,\n'
        '               Respondeo, Videtur), "prose" (ordinary verb/quantifier/function word).\n'
        '  "slovak"   — the single best Slovak rendering of this lemma.\n'
        'Respond ONLY with a JSON object keyed by the input lemma:\n'
        '  {"<input_lemma>": {"category": "...", "slovak": "..."}, ...}\n'
        "No explanations, no markdown fences, no extra text.\n\n"
        "Lemmas:\n" + "\n".join(lines)
    )

    with _api_stats_lock:
        _api_stats["calls"] += 1
    try:
        resp = requests.post(
            _DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": _DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": len(batch) * 60,
                "temperature": 0.0,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        usage = data.get("usage", {})
        with _api_stats_lock:
            _api_stats["input_tokens"] += usage.get("prompt_tokens", 0)
            _api_stats["output_tokens"] += usage.get("completion_tokens", 0)

        content = data["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if the model wraps the JSON
        content = re.sub(r"```(?:json)?\s*", "", content).replace("```", "").strip()
        result = json.loads(content)

        valid_lemmas = {item["lemma"] for item in batch}
        parsed: dict[str, dict] = {}
        for k, v in result.items():
            if str(k) not in valid_lemmas:
                continue  # model hallucinated a key not in the input batch
            entry = _parse_batch_entry(str(k), v)
            if entry is not None:
                parsed[str(k)] = entry
        return parsed

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 402, 403):
            raise RuntimeError(
                f"DeepSeek API fatal error (HTTP {status}) — "
                "check DEEPSEEK_API_KEY and account credits. Aborting."
            ) from exc
        print(f"  [WARN] DeepSeek batch HTTP error {status} ({len(batch)} lemmas): {exc}", flush=True)
        return {}
    except Exception as exc:
        print(f"  [WARN] DeepSeek batch error ({len(batch)} lemmas): {exc}", flush=True)
        return {}


def get_api_stats() -> dict[str, int]:
    """Return accumulated DeepSeek API usage stats."""
    return dict(_api_stats)


# ── DB helpers ────────────────────────────────────────────────────────────────


def _load_glossary(conn) -> tuple[list[dict], list[dict]]:
    """Return (multiword_terms, singleword_terms) sorted for deterministic processing.

    Each term dict: {term_id, latin_lemma, is_multiword, senses: [...]}
    Each sense dict: {sense_id, context_label, cs_lemma, en_cue, sk_content, version}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Only load 'approved' senses into the Krystal lookup.
        # 'proposed' senses belong to gap terms and must continue to be resolved
        # via gap methods (bahounek_derived etc.) on every run, not promoted to
        # krystal_single just because they were created in a previous run.
        cur.execute("""
            SELECT gt.term_id, gt.latin_lemma, gt.is_multiword,
                   gs.sense_id, gs.context_label, gs.version,
                   max(sr_cs.lemma)   FILTER (WHERE sr_cs.lang = 'cs') AS cs_lemma,
                   max(sr_cs.content) FILTER (WHERE sr_cs.lang = 'cs') AS cs_content,
                   max(sr_en.content) FILTER (WHERE sr_en.lang = 'en') AS en_cue,
                   max(sr_sk.content) FILTER (WHERE sr_sk.lang = 'sk') AS sk_content
            FROM glossary_term gt
            JOIN glossary_sense gs USING (term_id)
            LEFT JOIN sense_rendering sr_cs ON sr_cs.sense_id = gs.sense_id AND sr_cs.lang = 'cs'
            LEFT JOIN sense_rendering sr_en ON sr_en.sense_id = gs.sense_id AND sr_en.lang = 'en'
            LEFT JOIN sense_rendering sr_sk ON sr_sk.sense_id = gs.sense_id AND sr_sk.lang = 'sk'
            WHERE gs.status = 'approved'
            GROUP BY gt.term_id, gt.latin_lemma, gt.is_multiword,
                     gs.sense_id, gs.context_label, gs.version
            ORDER BY gt.latin_lemma, gs.sense_id
        """)
        rows = cur.fetchall()

    # Group senses by term
    terms: dict[int, dict] = {}
    for row in rows:
        tid = row["term_id"]
        if tid not in terms:
            terms[tid] = {
                "term_id": tid,
                "latin_lemma": row["latin_lemma"],
                "is_multiword": row["is_multiword"],
                "senses": [],
            }
        terms[tid]["senses"].append({
            "sense_id": row["sense_id"],
            "context_label": row["context_label"],
            "version": row["version"],
            "cs_lemma": row["cs_lemma"],
            "cs_content": row["cs_content"],
            "en_cue": row["en_cue"],
            "sk_content": row["sk_content"],
        })

    all_terms = sorted(terms.values(), key=lambda t: t["latin_lemma"])
    multiword = [t for t in all_terms if t["is_multiword"]]
    singleword = [t for t in all_terms if not t["is_multiword"]]
    return multiword, singleword


def _load_segments(conn, wid: int) -> list[dict]:
    """Return body segments with la/cs/en text for the given work, sorted by locator."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT s.segment_id, s.locator_path::text AS locator_path, s.element_type,
                   max(t.content) FILTER (WHERE t.lang = 'la') AS latin,
                   max(t.content) FILTER (WHERE t.lang = 'cs') AS czech,
                   max(t.content) FILTER (WHERE t.lang = 'en') AS english
            FROM segment s
            LEFT JOIN segment_text t USING (segment_id)
            WHERE s.work_id = %s
              AND s.element_type = ANY(%s)
            GROUP BY s.segment_id, s.locator_path, s.element_type
            ORDER BY s.locator_path
        """, (wid, list(_BODY_TYPES)))
        return cur.fetchall()


def _source_rank(conn, code: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT authority_rank FROM source WHERE code = %s", (code,))
        return cur.fetchone()[0]


# ── Phrase matching ───────────────────────────────────────────────────────────


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def phrase_match(latin_text: str, multiword_terms: list[dict]) -> list[tuple[dict, str]]:
    """Find all multiword glossary terms in latin_text.

    Returns list of (term_dict, matched_span) in document order.
    Matched spans are removed from further processing (returned as masked text).
    """
    normalized = _normalize_ws(latin_text)
    matches: list[tuple[int, int, dict, str]] = []

    for term in multiword_terms:
        lemma = term["latin_lemma"]
        # Simple substring search (case-insensitive)
        pattern = re.compile(re.escape(lemma), re.IGNORECASE)
        for m in pattern.finditer(normalized):
            matches.append((m.start(), m.end(), term, m.group()))

    # Sort by start position; for same start prefer longer match (greedy / leftmost-longest).
    # This ensures "actus essendi" is consumed before "actus" can claim the same position.
    matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    result: list[tuple[dict, str]] = []
    last_end = 0
    for start, end, term, span in matches:
        if start >= last_end:
            result.append((term, span))
            last_end = end

    return result


def mask_spans(latin_text: str, multiword_terms: list[dict]) -> str:
    """Return latin_text with matched multiword spans replaced by spaces."""
    normalized = _normalize_ws(latin_text)
    for term in multiword_terms:
        pattern = re.compile(re.escape(term["latin_lemma"]), re.IGNORECASE)
        normalized = pattern.sub(" ", normalized)
    return normalized


# ── Sense resolution ──────────────────────────────────────────────────────────


@dataclass
class Resolution:
    term: dict
    sense: dict
    method: str
    confidence: str   # 'auto' | 'needs_review'
    signals: dict = field(default_factory=dict)


def _resolve_single(term: dict) -> Resolution:
    """Single-sense term: silent auto-resolution."""
    return Resolution(
        term=term,
        sense=term["senses"][0],
        method="krystal_single",
        confidence="auto",
    )


def _resolve_multi(term: dict, czech_text: str | None, english_text: str | None,
                   cs_rank: int, en_rank: int) -> Resolution:
    """Multi-sense term: evidence vote."""
    senses = term["senses"]
    signals: dict[int, list[tuple[int, str]]] = {}  # sense_id → [(rank, signal_desc)]

    # Czech signal: MorphoDiTa lemmatizes segment cs text, match against sense cs_lemma
    if czech_text:
        cs_lemmas = set()
        for token in re.findall(r"\w+", czech_text):
            cs_lemmas.update(lemmatize_czech(token))

        for sense in senses:
            cs_key = sense.get("cs_lemma") or sense.get("cs_content") or ""
            if cs_key and cs_key in cs_lemmas:
                sid = sense["sense_id"]
                signals.setdefault(sid, []).append(
                    (cs_rank, f"cs={cs_key}→sense_{sid}")
                )

    # English signal: substring match of en_cue in english_text
    if english_text:
        for sense in senses:
            en_cue = sense.get("en_cue") or ""
            if en_cue and en_cue.lower() in english_text.lower():
                sid = sense["sense_id"]
                signals.setdefault(sid, []).append(
                    (en_rank, f"en={en_cue}→sense_{sid}")
                )

    # Determine winning sense
    # Consistent = only one sense has signals; ≥1 strong = rank ≤ _STRONG_RANK_THRESHOLD
    voted_senses = sorted(signals.keys())

    if len(voted_senses) == 1:
        winning_sid = voted_senses[0]
        winning_signals = signals[winning_sid]
        has_strong = any(rank <= _STRONG_RANK_THRESHOLD for rank, _ in winning_signals)

        winning_sense = next(s for s in senses if s["sense_id"] == winning_sid)
        flat_signals = {desc: rank for rank, desc in winning_signals}

        if has_strong:
            return Resolution(
                term=term,
                sense=winning_sense,
                method="krystal_multi_voted",
                confidence="auto",
                signals=flat_signals,
            )

    # Fallback: flag
    # Use primary sense (context_label=None) or first sense
    primary = next((s for s in senses if s["context_label"] is None), senses[0])
    all_signals = {}
    for sid, sig_list in signals.items():
        for rank, desc in sig_list:
            all_signals[desc] = rank
    return Resolution(
        term=term,
        sense=primary,
        method="krystal_multi_flagged",
        confidence="needs_review",
        signals=all_signals,
    )


def _ensure_glossary_term(conn, lemma: str, category: str | None = None) -> int:
    """Insert glossary_term (with model category) if not present; return term_id.

    On conflict the category is refreshed only for gap terms (no approved senses).
    Krystal-seeded terms have approved senses and must never have their category
    overwritten by a model re-run.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO glossary_term (latin_lemma, is_multiword, category)
            VALUES (%s, false, %s)
            ON CONFLICT (latin_lemma) DO UPDATE
                SET category = EXCLUDED.category
                WHERE NOT EXISTS (
                    SELECT 1 FROM glossary_sense
                    WHERE term_id = glossary_term.term_id AND status = 'approved'
                )
            RETURNING term_id
            """,
            (lemma, category),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        # Conflict on a Krystal term (approved senses exist) — fetch without updating
        cur.execute(
            "SELECT term_id FROM glossary_term WHERE latin_lemma = %s",
            (lemma,),
        )
        return cur.fetchone()[0]


# ── Gap term pre-scan and batch proposal ─────────────────────────────────────


def _scan_gap_lemmas(
    segments: list[dict],
    krystal_lemmas: set[str],
    freq_floor: int,
    min_len: int = _GAP_MIN_LEN,
) -> dict[str, dict]:
    """One read-only pass over all segments to collect gap lemma data.

    Returns {lemma: {freq, best_latin, best_czech, best_english}} for lemmas
    (CLTK lemma with numeric suffix stripped) that appear in ≥ freq_floor segments.

    Mechanical filters only — no POS tagging, no word lists. Precision is handled
    downstream by the model's category (term/name/formula/prose). The length gate
    (`len > min_len`) drops most Latin function words; the model classifies the rest.
    """
    lemma_data: dict[str, dict] = {}

    for seg in segments:
        latin = seg["latin"] or ""
        if not latin:
            continue
        czech = seg["czech"] or ""
        english = seg["english"] or ""

        seen_in_seg: set[str] = set()
        for token in re.findall(r"[a-zA-Z]+", latin):
            cands = lemmatize_latin(token)
            if not cands:
                continue
            # Phase 2 tries all candidates for Krystal lookup; skip here if any would hit.
            if any(c in krystal_lemmas for c in cands):
                continue
            lemma = _strip_lemma_suffix(cands[0])
            if len(lemma) <= min_len or lemma in krystal_lemmas or lemma in seen_in_seg:
                continue
            seen_in_seg.add(lemma)

            d = lemma_data.get(lemma)
            if d is None:
                d = lemma_data[lemma] = {
                    "freq": 0,
                    "best_latin": latin[:300],
                    "best_czech": czech[:300],
                    "best_english": english[:300],
                }
            d["freq"] += 1
            # Upgrade context if current best lacks Czech or English
            if czech and not d["best_czech"]:
                d["best_czech"] = czech[:300]
            if english and not d["best_english"]:
                d["best_english"] = english[:300]

    return {lemma: d for lemma, d in lemma_data.items() if d["freq"] >= freq_floor}


def _propose_gap_terms(
    gap_data: dict[str, dict],
    batch_size: int = _GAP_BATCH_SIZE,
    max_workers: int = _GAP_MAX_WORKERS,
    conn=None,
    src_model: int | None = None,
) -> dict:
    """Batch-call DeepSeek to classify and translate gap lemmas.

    Each CLTK lemma is its own key — no canonicalization, no fragment merging.
    When conn + src_model are supplied each successful batch is written to DB
    immediately after it completes — no re-translation cost on retry runs.

    Returns:
      {
        "terms":        {lemma: {slovak, category, freq, best_latin, best_czech, best_english}},
        "dropped":      [lemma, ...],   # batch failed / model omitted
        "gap_terms_db": {lemma: {term_id, sense_id, version, category, slovak}}
                        (populated only when conn is provided),
      }
    """
    items = [
        {
            "lemma": lemma,
            "best_latin": d["best_latin"],
            "best_czech": d["best_czech"],
            "best_english": d["best_english"],
        }
        for lemma, d in sorted(gap_data.items())
    ]
    batches = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]

    print(
        f"  Calling DeepSeek for {len(items)} gap lemmas "
        f"in {len(batches)} batches ({max_workers} concurrent)...",
        flush=True,
    )

    raw: dict[str, dict] = {}  # lemma → {category, slovak}
    gap_terms_db: dict[str, dict] = {}
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_batch = {pool.submit(_call_deepseek_batch, b): b for b in batches}
        for future in as_completed(future_to_batch):
            try:
                batch_raw = future.result()
            except Exception as exc:
                print(f"  [WARN] batch failed, lemmas dropped this run: {exc}", flush=True)
                completed += 1
                if completed % 20 == 0 or completed == len(batches):
                    print(f"  {completed}/{len(batches)} batches complete", flush=True)
                continue

            raw.update(batch_raw)

            # Write this batch's proposals to DB immediately when conn is supplied.
            if conn is not None and src_model is not None and batch_raw:
                batch_terms = {
                    lemma: {
                        "slovak": entry["slovak"],
                        "category": entry["category"],
                        "freq": gap_data[lemma]["freq"],
                        "best_latin": gap_data[lemma]["best_latin"],
                        "best_czech": gap_data[lemma]["best_czech"],
                        "best_english": gap_data[lemma]["best_english"],
                    }
                    for lemma, entry in batch_raw.items()
                }
                new_entries = _write_gap_proposals(conn, {"terms": batch_terms}, src_model)
                gap_terms_db.update(new_entries)

            completed += 1
            if completed % 20 == 0 or completed == len(batches):
                print(f"  {completed}/{len(batches)} batches complete", flush=True)

    dropped = [lemma for lemma in gap_data if lemma not in raw]
    if dropped:
        print(
            f"  [WARN] {len(dropped)} gap lemmas had no proposal (batch failure / model "
            f"omission) and are skipped this run; re-run to pick them up.",
            flush=True,
        )

    terms = {
        lemma: {
            "slovak": entry["slovak"],
            "category": entry["category"],
            "freq": gap_data[lemma]["freq"],
            "best_latin": gap_data[lemma]["best_latin"],
            "best_czech": gap_data[lemma]["best_czech"],
            "best_english": gap_data[lemma]["best_english"],
        }
        for lemma, entry in raw.items()
    }

    return {"terms": terms, "dropped": dropped, "gap_terms_db": gap_terms_db}


def _write_gap_proposals(conn, proposals: dict, src_model: int) -> dict[str, dict]:
    """Pre-write glossary_term(category) + glossary_sense + sense_rendering per gap term.

    Uses DO UPDATE for sense_rendering so re-runs refresh proposals. Returns
    gap_terms_db: {lemma: {term_id, sense_id, version, category, slovak}}
    for the main loop to attach term_usage rows to.
    """
    gap_terms_db: dict[str, dict] = {}
    for lemma, term in sorted(proposals["terms"].items()):
        sk_proposal = term["slovak"]
        category = term["category"]
        term_id = _ensure_glossary_term(conn, lemma, category)
        with conn.cursor() as cur:
            # If this CLTK lemma is already a Krystal term (has an approved sense),
            # skip creating a gap proposal — it resolves via the Krystal path in Phase 2.
            cur.execute(
                "SELECT sense_id, status FROM glossary_sense "
                "WHERE term_id = %s AND context_label IS NULL",
                (term_id,),
            )
            existing = cur.fetchone()
            if existing is not None and existing[1] == "approved":
                continue
            # glossary_sense has no unique constraint covering (term_id, NULL context_label)
            # because NULLs are not equal under standard SQL uniqueness. Use explicit
            # SELECT-then-INSERT to avoid duplicate proposed senses on re-runs.
            row = existing  # may be an existing proposed sense
            if row is None:
                cur.execute(
                    "INSERT INTO glossary_sense (term_id, context_label, status) "
                    "VALUES (%s, NULL, 'proposed') RETURNING sense_id",
                    (term_id,),
                )
                row = cur.fetchone()
            sense_id = row[0]
            # Overwrite on re-run to refresh the proposal
            cur.execute(
                """
                INSERT INTO sense_rendering (sense_id, lang, content, source_id)
                VALUES (%s, 'sk', %s, %s)
                ON CONFLICT (sense_id, lang, source_id) DO UPDATE SET content = EXCLUDED.content
                """,
                (sense_id, sk_proposal, src_model),
            )
            cur.execute("SELECT version FROM glossary_sense WHERE sense_id = %s", (sense_id,))
            version = cur.fetchone()[0]

        gap_terms_db[lemma] = {
            "term_id": term_id,
            "sense_id": sense_id,
            "version": version,
            "category": category,
            "slovak": sk_proposal,
        }
    conn.commit()
    return gap_terms_db


def _load_existing_gap_terms(conn) -> dict[str, dict]:
    """Return {latin_lemma: {term_id, sense_id, version, category, slovak}}
    for all gap terms already written to DB (proposed status, sk rendering present).
    Used on re-runs to skip re-calling DeepSeek for already-translated lemmas.
    Each key is the CLTK lemma (after suffix strip) — not a DeepSeek-corrected canonical.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT gt.latin_lemma, gt.term_id, gs.sense_id, gs.version,
                   gt.category, sr.content
            FROM glossary_term gt
            JOIN glossary_sense gs
              ON gs.term_id = gt.term_id AND gs.context_label IS NULL
            JOIN sense_rendering sr
              ON sr.sense_id = gs.sense_id AND sr.lang = 'sk'
            WHERE gs.status = 'proposed'
            """
        )
        return {
            row[0]: {
                "term_id": row[1],
                "sense_id": row[2],
                "version": row[3],
                "category": row[4],
                "slovak": row[5],
            }
            for row in cur.fetchall()
        }


# ── Main resolution loop ──────────────────────────────────────────────────────


def resolve_segment(
    segment: dict,
    multiword_terms: list[dict],
    lemma_to_term: dict[str, dict],
    cs_rank: int,
    en_rank: int,
    gap_terms_db: dict[str, dict] | None = None,
    min_len: int = _GAP_MIN_LEN,
) -> list[Resolution]:
    """Resolve all terms in one segment. Returns list of Resolutions.

    No-stub invariant: a gap lemma becomes a term_usage Resolution ONLY if it
    is present in gap_terms_db — i.e. it received a model proposal in Phase 1.
    Lemmas with no proposal are skipped entirely; they never produce a bracketed
    stub or an orphan term_usage row. Gap senses are pre-written by Phase 1,
    so this function performs no DB writes.
    """
    latin = segment["latin"] or ""
    czech = segment["czech"]
    english = segment["english"]
    gap_terms_db = gap_terms_db or {}

    resolutions: list[Resolution] = []
    seen_term_ids: set[int] = set()  # deduplicate: one resolution per term per segment

    # 1. Phrase-match multiword terms first
    mw_matches = phrase_match(latin, multiword_terms)
    for term, _span in mw_matches:
        if term["term_id"] in seen_term_ids:
            continue
        seen_term_ids.add(term["term_id"])
        senses = term["senses"]
        if len(senses) == 1:
            resolutions.append(_resolve_single(term))
        else:
            resolutions.append(_resolve_multi(term, czech, english, cs_rank, en_rank))

    # 2. Lemmatize remaining tokens
    masked = mask_spans(latin, [t for t, _ in mw_matches])
    tokens = sorted(set(re.findall(r"[a-zA-Z]+", masked)))  # sorted for determinism

    gap_candidates: set[str] = set()  # stripped lemmas not in Krystal, for gap handling

    for token in tokens:
        cands = lemmatize_latin(token)
        if not cands:
            continue

        # Krystal: try every candidate lemma; first hit wins.
        krystal_hit = False
        for lemma in cands:
            term = lemma_to_term.get(lemma)
            if term is None:
                continue
            krystal_hit = True
            if term["term_id"] not in seen_term_ids:
                seen_term_ids.add(term["term_id"])
                senses = term["senses"]
                if len(senses) == 1:
                    resolutions.append(_resolve_single(term))
                else:
                    resolutions.append(_resolve_multi(term, czech, english, cs_rank, en_rank))
            break
        if krystal_hit:
            continue

        # Gap: the stripped first candidate (matches _scan_gap_lemmas keying).
        stripped = _strip_lemma_suffix(cands[0])
        if len(stripped) > min_len:
            gap_candidates.add(stripped)

    # 3. Gap terms: only those that received a Phase-1 proposal (in gap_terms_db).
    # Method label reflects available context; the proposal itself is the model's.
    if czech:
        method = "bahounek_derived"
    elif english:
        method = "english_derived"
    else:
        method = "model_proposed"

    for stripped in sorted(gap_candidates):
        db = gap_terms_db.get(stripped)
        if db is None:
            continue

        sense = {
            "sense_id": db["sense_id"],
            "context_label": None,
            "version": db["version"],
            "cs_lemma": None,
            "cs_content": None,
            "en_cue": None,
            "sk_content": db["slovak"],
        }
        gap_term = {
            "term_id": db["term_id"],
            "latin_lemma": stripped,
            "is_multiword": False,
            "senses": [sense],
        }
        resolutions.append(Resolution(
            term=gap_term,
            sense=sense,
            method=method,
            confidence="needs_review",
        ))

    return resolutions


def _write_term_usage(conn, segment_id: int, resolutions: list[Resolution]) -> int:
    """Write term_usage rows. Idempotent per (segment_id, sense_id). Returns count."""
    if not resolutions:
        return 0
    with conn.cursor() as cur:
        # Only wipe guessed rows — confirmed rows survive re-runs (Principle 3).
        cur.execute("DELETE FROM term_usage WHERE segment_id = %s AND status = 'guessed'", (segment_id,))
        for res in resolutions:
            cur.execute(
                """
                INSERT INTO term_usage
                  (segment_id, sense_id, sense_version_used,
                   resolution_method, confidence, signals, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'guessed')
                """,
                (
                    segment_id,
                    res.sense["sense_id"],
                    res.sense["version"],
                    res.method,
                    res.confidence,
                    json.dumps(res.signals) if res.signals else None,
                ),
            )
    return len(resolutions)


# ── Entry point ───────────────────────────────────────────────────────────────


def run(
    freq_floor: int = _GAP_FREQ_FLOOR,
    batch_size: int = _GAP_BATCH_SIZE,
    max_workers: int = _GAP_MAX_WORKERS,
    min_len: int = _GAP_MIN_LEN,
) -> None:
    """Two-phase resolver.

    Phase 1: scan all segments for gap lemmas (mechanical filter only), then
             classify/canonicalize/translate them via DeepSeek and pre-write the
             canonical glossary_term(category) + sense + sk rendering to the DB.
    Phase 2: main resolution loop — Krystal terms + gap terms (proposals in DB).
             A gap lemma only becomes a term_usage row if its canonical headword
             received a Phase-1 proposal (no-stub invariant).

    Knobs:
      freq_floor  — min segment frequency for a gap lemma (default 10)
      batch_size  — lemmas per DeepSeek batch call (default 25)
      max_workers — concurrent batch requests (default 10)
      min_len     — gap lemma must be longer than this (default 5)
    """
    # Reset accumulated stats so a single-process multi-step run reports only this run.
    with _api_stats_lock:
        _api_stats["calls"] = 0
        _api_stats["input_tokens"] = 0
        _api_stats["output_tokens"] = 0

    print(f"Loading glossary and segments (freq_floor={freq_floor}, min_len={min_len})...")

    with get_conn() as conn:
        wid = work_id(conn, "summa_articulus")
        multiword_terms, singleword_terms = _load_glossary(conn)
        segments = _load_segments(conn, wid)

    lemma_to_term = {t["latin_lemma"]: t for t in singleword_terms}
    krystal_lemmas = set(lemma_to_term.keys()) | {t["latin_lemma"] for t in multiword_terms}
    print(f"  Glossary: {len(multiword_terms)} multiword + {len(singleword_terms)} singleword Krystal terms")
    print(f"  Segments: {len(segments)} body segments to resolve")

    # ── Phase 1: scan, batch-propose (classify + translate) ─────────────────────
    print("\n[Phase 1] Scanning gap lemmas...")
    gap_data = _scan_gap_lemmas(segments, krystal_lemmas, freq_floor, min_len)
    print(f"  {len(gap_data)} gap lemmas qualify (freq≥{freq_floor}, len>{min_len})")

    gap_terms_db: dict[str, dict] = {}

    if gap_data:
        # Load what was already proposed in a previous (possibly partial) run.
        # Each CLTK lemma is its own key — no fragment mapping needed.
        with get_conn() as conn:
            existing = _load_existing_gap_terms(conn)
            src_model_id = source_id(conn, "model")

        if existing:
            print(f"  {len(existing)} gap terms already in DB — skipping DeepSeek for those")
            gap_terms_db.update(existing)
            for lemma in list(gap_data):
                if lemma in existing:
                    del gap_data[lemma]

        if gap_data:
            with get_conn() as conn:
                proposals = _propose_gap_terms(
                    gap_data,
                    batch_size=batch_size,
                    max_workers=max_workers,
                    conn=conn,
                    src_model=src_model_id,
                )
            gap_terms_db.update(proposals["gap_terms_db"])
            print(
                f"  {len(gap_terms_db)} gap terms total in DB "
                f"(+{len(proposals['gap_terms_db'])} new)"
            )
        else:
            print("  All gap lemmas already proposed — no DeepSeek calls needed")

    # ── Phase 2: main resolution loop ────────────────────────────────────────
    print(f"\n[Phase 2] Resolving {len(segments)} segments...")
    total_usages = 0

    with get_conn() as conn:
        wid = work_id(conn, "summa_articulus")
        cs_rank = _source_rank(conn, "bahounek")
        en_rank = _source_rank(conn, "dominican")
        multiword_terms, singleword_terms = _load_glossary(conn)
        lemma_to_term = {t["latin_lemma"]: t for t in singleword_terms}
        segments = _load_segments(conn, wid)

        for i, seg in enumerate(segments, 1):
            resolutions = resolve_segment(
                seg, multiword_terms, lemma_to_term,
                cs_rank, en_rank, gap_terms_db, min_len,
            )
            n = _write_term_usage(conn, seg["segment_id"], resolutions)
            total_usages += n
            if i % 500 == 0 or i == len(segments):
                conn.commit()  # checkpoint: crash can only lose the current 500-seg batch
                print(f"  {i}/{len(segments)} segments resolved", flush=True)

    print(f"\nDone. {total_usages} term_usage rows written across {len(segments)} segments.")

    # Always write stats file so coverage report always has accurate numbers,
    # even when gap_data was empty (all lemmas below freq floor) → calls==0.
    stats = get_api_stats()
    cost_usd = (stats["input_tokens"] * 0.00014 + stats["output_tokens"] * 0.00028) / 1000
    if stats["calls"] > 0:
        print(
            f"DeepSeek API: {stats['calls']} calls, "
            f"{stats['input_tokens']} input + {stats['output_tokens']} output tokens, "
            f"~${cost_usd:.4f}"
        )
    stats_path = ROOT / "reports" / "m2_api_stats.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(
        json.dumps(
            {**stats, "cost_usd": round(cost_usd, 6), "lemmas_proposed": len(gap_terms_db)},
            indent=2,
        )
    )


def pilot_batch_sizes(
    gap_data: dict[str, dict],
    top_n: int = 50,
    batch_sizes: list[int] | None = None,
    sample_n: int = 5,
) -> list[dict]:
    """Compare batch sizes on top_n gap lemmas to find the cost/quality optimum.

    For each batch size: runs _propose_gap_terms on the top-N lemmas, captures the
    token-count delta vs. the running global stats, and records cost and sample terms.

    Returns a list of result dicts (one per batch size).  Prints a comparison table
    and side-by-side sample terms to stdout.  Does NOT write to the database.
    """
    if batch_sizes is None:
        batch_sizes = [10, 25, 50, 100]

    top_lemmas = dict(
        sorted(gap_data.items(), key=lambda x: -x[1]["freq"])[:top_n]
    )
    total_qualifying = len(gap_data)
    n_pilot = len(top_lemmas)

    # Rough cost estimate: ~60 tokens per lemma per batch call (input+output combined).
    est_calls = sum(-((-n_pilot) // bs) for bs in batch_sizes)
    est_tokens = n_pilot * len(batch_sizes) * 60
    est_cost = (est_tokens * 0.00014) / 1000
    print(
        f"\n[PILOT] {n_pilot} lemmas (of {total_qualifying} qualifying) "
        f"at batch sizes {batch_sizes}",
        flush=True,
    )
    print(
        f"  Estimated: ~{est_calls} API calls, ~{est_tokens} tokens, ~${est_cost:.4f}",
        flush=True,
    )

    from collections import Counter

    def _sk(proposals: dict, lemma: str) -> str:
        term = proposals["terms"].get(lemma)
        return term["slovak"] if term else "???"

    def _cat(proposals: dict, lemma: str) -> str | None:
        term = proposals["terms"].get(lemma)
        return term["category"] if term else None

    top_order = sorted(top_lemmas, key=lambda lm: -top_lemmas[lm]["freq"])

    results: list[dict] = []
    per_size_proposals: dict[int, dict] = {}

    for bs in batch_sizes:
        with _api_stats_lock:
            stats_before = dict(_api_stats)

        proposals = _propose_gap_terms(top_lemmas, batch_size=bs, max_workers=4)

        stats_after = get_api_stats()
        d_calls = stats_after["calls"] - stats_before["calls"]
        d_in = stats_after["input_tokens"] - stats_before["input_tokens"]
        d_out = stats_after["output_tokens"] - stats_before["output_tokens"]
        cost = (d_in * 0.00014 + d_out * 0.00028) / 1000

        cat_counts = Counter(
            (t["category"] or "uncategorized") for t in proposals["terms"].values()
        )

        per_size_proposals[bs] = proposals
        results.append({
            "batch_size": bs,
            "calls": d_calls,
            "input_tokens": d_in,
            "output_tokens": d_out,
            "cost_usd": round(cost, 6),
            "cost_per_lemma": round(cost / n_pilot, 8) if n_pilot else 0.0,
            "category_counts": dict(cat_counts),
            "samples": [
                {
                    "lemma": lm,
                    "category": _cat(proposals, lm),
                    "slovak": _sk(proposals, lm),
                    "freq": top_lemmas[lm]["freq"],
                }
                for lm in top_order[:sample_n]
            ],
        })

    # ── comparison table ─────────────────────────────────────────────────────
    est_label = f"Est.({total_qualifying} lemmas)"
    print(f"\n{'Batch':>8} {'Calls':>6} {'InTok':>7} {'OutTok':>7} {'$/lemma':>11}  {est_label}")
    print("-" * (8 + 7 + 8 + 8 + 12 + 4 + len(est_label)))
    for r in results:
        est = r["cost_per_lemma"] * total_qualifying
        print(
            f"{r['batch_size']:>8} {r['calls']:>6} {r['input_tokens']:>7} "
            f"{r['output_tokens']:>7} ${r['cost_per_lemma']:.8f}  ~${est:.4f}"
        )

    # ── category distribution (from the largest batch size, for stability) ────
    last = results[-1]
    if last["category_counts"]:
        dist = ", ".join(f"{c}={n}" for c, n in sorted(last["category_counts"].items()))
        print(f"\nCategory distribution (bs={last['batch_size']}): {dist}")

    # ── side-by-side sample terms ─────────────────────────────────────────────
    if top_order:
        col_w = 18
        header = f"{'Lemma':18} {'Freq':>6}"
        for r in results:
            header += f"  {'bs='+str(r['batch_size']):<{col_w}}"
        print(f"\nSample terms (top {min(sample_n, len(top_order))} by frequency):")
        print(header)
        print("-" * len(header))
        for lemma in top_order[:sample_n]:
            freq = top_lemmas[lemma]["freq"]
            row = f"{lemma:18} {freq:>6}"
            for r in results:
                p = per_size_proposals[r["batch_size"]]
                cat = _cat(p, lemma) or "?"
                cell = f"{_sk(p, lemma)} ({cat})"
                row += f"  {cell:<{col_w}}"
            print(row)

    return results


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        sys.exit(1)
