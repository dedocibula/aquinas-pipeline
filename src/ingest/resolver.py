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
  GAP_POS_FILTER  — CLTK POS codes to keep, comma-separated (default "N,A")
                    N=noun, A=adjective; set to "" to disable POS filter

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
from ingest.lemmatize import lemmatize_czech, lemmatize_latin, pos_tag_latin

ROOT = Path(__file__).resolve().parents[2]

# Authority rank threshold for a "strong" signal (Krystal=10, Bahounek=20)
_STRONG_RANK_THRESHOLD = 20

# Element types to run the resolver on (skip title/preamble segments)
_BODY_TYPES = {"arg", "sed_contra", "respondeo", "reply"}

# ── Gap term proposal knobs ───────────────────────────────────────────────────
# Override via run() parameters or GAP_* env vars read in pipeline.py.

_GAP_FREQ_FLOOR: int = 10
# CLTK first-char POS codes: N=noun, A=adjective, V=verb, R=preposition, etc.
# None = no POS filter (propose for all gap lemmas above freq floor)
_GAP_POS_FILTER: frozenset[str] = frozenset({"N", "A"})
_GAP_BATCH_SIZE: int = 25   # lemmas per DeepSeek batch call
_GAP_MAX_WORKERS: int = 10  # concurrent batch requests

# ── DeepSeek API ──────────────────────────────────────────────────────────────

_api_stats: dict[str, int] = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
_api_stats_lock = threading.Lock()

_DEEPSEEK_URL = os.environ.get(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions"
)
_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def _call_deepseek(
    latin_lemma: str,
    context_snippet: str,
    czech_ref: str,
    english_ref: str,
) -> str:
    """Propose a Slovak term for a gap lemma via DeepSeek V3.

    Returns a proposed single Slovak word/term, or a fallback stub on error.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. "
            "Export it before running the resolver with model_proposed terms."
        )

    prompt = (
        f"You are a Slovak theological terminologist. "
        f"Propose a single Slovak term (one word or short phrase) for the Latin lemma '{latin_lemma}'.\n"
        f"Context (Latin): {context_snippet[:300]}\n"
    )
    if czech_ref:
        prompt += f"Czech reference: {czech_ref[:200]}\n"
    if english_ref:
        prompt += f"English reference: {english_ref[:200]}\n"
    prompt += (
        "Respond with ONLY the Slovak term — no explanation, no punctuation, no quotes. "
        "If unsure, give your best single-word guess."
    )

    with _api_stats_lock:
        _api_stats["calls"] += 1  # count every attempt, including failures
    try:
        resp = requests.post(
            _DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": _DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 20,
                "temperature": 0.0,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        usage = data.get("usage", {})
        with _api_stats_lock:
            _api_stats["input_tokens"] += usage.get("prompt_tokens", 0)
            _api_stats["output_tokens"] += usage.get("completion_tokens", 0)

        term = data["choices"][0]["message"]["content"].strip()
        return term or f"[model_proposed: {latin_lemma}]"

    except Exception as exc:
        print(f"  [WARN] DeepSeek API error for {latin_lemma!r}: {exc}", flush=True)
        return f"[model_proposed: {latin_lemma}]"


def _call_deepseek_batch(batch: list[dict]) -> dict[str, str]:
    """Propose Slovak terms for a batch of Latin gap lemmas in one API call.

    Each item: {"lemma": str, "best_latin": str, "best_czech": str, "best_english": str}
    Returns {lemma: sk_proposal}. Missing/malformed entries fall back to stubs in the caller.
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
        "You are a Slovak theological terminologist translating Thomas Aquinas's Summa Theologiae.\n"
        "For each Latin term below, propose the single best Slovak translation.\n"
        "Czech (Bahounek) and English (Dominican) excerpts are provided as context.\n"
        'Respond ONLY with a JSON object: {"latin_lemma": "slovak_term", ...}\n'
        "No explanations, no markdown fences, no extra text.\n\n"
        "Terms:\n" + "\n".join(lines)
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
                "max_tokens": len(batch) * 15,
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
        return {str(k): str(v).strip() for k, v in result.items() if v}

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


def _ensure_glossary_term(conn, lemma: str) -> int:
    """Insert glossary_term if not present; return term_id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO glossary_term (latin_lemma, is_multiword)
            VALUES (%s, false)
            ON CONFLICT (latin_lemma) DO UPDATE SET latin_lemma = EXCLUDED.latin_lemma
            RETURNING term_id
            """,
            (lemma,),
        )
        return cur.fetchone()[0]


def _gap_sense(conn, term_id: int, method: str, sk_proposal: str, src_model: int) -> dict:
    """Create a proposed glossary_sense + sense_rendering for a gap term.

    sense_rendering is attributed to src_model (source.code='model'), not Krystal.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO glossary_sense (term_id, context_label, status)
            VALUES (%s, NULL, 'proposed')
            ON CONFLICT DO NOTHING
            RETURNING sense_id
            """,
            (term_id,),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT sense_id FROM glossary_sense WHERE term_id = %s AND context_label IS NULL",
                (term_id,),
            )
            row = cur.fetchone()
        sense_id = row[0]

        # DO NOTHING: preserves proposals already written by the pre-scan batch pass.
        # Stubs generated in the main loop must not overwrite a good DeepSeek proposal.
        cur.execute(
            """
            INSERT INTO sense_rendering (sense_id, lang, content, source_id)
            VALUES (%s, 'sk', %s, %s)
            ON CONFLICT (sense_id, lang, source_id) DO NOTHING
            """,
            (sense_id, sk_proposal, src_model),
        )

        cur.execute("SELECT version FROM glossary_sense WHERE sense_id = %s", (sense_id,))
        version = cur.fetchone()[0]

    return {
        "sense_id": sense_id,
        "context_label": None,
        "version": version,
        "cs_lemma": None,
        "cs_content": None,
        "en_cue": None,
        "sk_content": sk_proposal,
    }


# ── Gap term pre-scan and batch proposal ─────────────────────────────────────


def _scan_gap_lemmas(
    segments: list[dict],
    krystal_lemmas: set[str],
    freq_floor: int,
    pos_filter: frozenset[str] | None,
) -> dict[str, dict]:
    """One read-only pass over all segments to collect gap lemma data.

    Returns {lemma: {freq, best_latin, best_czech, best_english}} for lemmas
    that pass freq_floor and pos_filter.

    POS filter logic:
    - If pos_filter is set: keep lemmas whose dominant non-'?' POS is in pos_filter.
    - Lemmas with only '?' tags (tagger didn't recognise them — often medieval
      theological vocabulary) are kept: benefit of the doubt.
    - Lemmas positively identified as excluded POS (e.g. V=verb) are dropped.
    """
    from collections import Counter

    lemma_data: dict[str, dict] = {}

    for seg in segments:
        latin = seg["latin"] or ""
        if not latin:
            continue
        czech = seg["czech"] or ""
        english = seg["english"] or ""

        tagged = pos_tag_latin(latin)  # [(surface, pos_char), ...]
        seen_in_seg: set[str] = set()

        for surface, pos_char in tagged:
            if len(surface) <= 5:
                continue
            for lemma in lemmatize_latin(surface):
                if lemma in krystal_lemmas or lemma in seen_in_seg:
                    break
                seen_in_seg.add(lemma)

                if lemma not in lemma_data:
                    lemma_data[lemma] = {
                        "freq": 0,
                        "pos_votes": Counter(),
                        "best_latin": latin[:300],
                        "best_czech": czech[:300],
                        "best_english": english[:300],
                    }
                lemma_data[lemma]["freq"] += 1
                if pos_char != "?":
                    lemma_data[lemma]["pos_votes"][pos_char] += 1
                # Upgrade context if current best lacks Czech or English
                d = lemma_data[lemma]
                if czech and not d["best_czech"]:
                    d["best_czech"] = czech[:300]
                if english and not d["best_english"]:
                    d["best_english"] = english[:300]
                break  # one lemma candidate per surface token

    filtered: dict[str, dict] = {}
    for lemma, d in lemma_data.items():
        if d["freq"] < freq_floor:
            continue
        if pos_filter is not None:
            votes = d["pos_votes"]
            known_votes = {p: n for p, n in votes.items() if p != "?"}
            if known_votes:
                dominant = max(known_votes, key=known_votes.__getitem__)
                if dominant not in pos_filter:
                    continue
            # All tags were '?' (unknown to tagger) → keep
        filtered[lemma] = {
            "freq": d["freq"],
            "best_latin": d["best_latin"],
            "best_czech": d["best_czech"],
            "best_english": d["best_english"],
        }

    return filtered


def _propose_gap_terms(
    gap_data: dict[str, dict],
    batch_size: int = _GAP_BATCH_SIZE,
    max_workers: int = _GAP_MAX_WORKERS,
) -> dict[str, str]:
    """Batch-call DeepSeek in parallel for all filtered gap lemmas.

    Returns {lemma: sk_proposal}. Lemmas whose batch call fails fall back to stubs.
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

    proposals: dict[str, str] = {}
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_batch = {pool.submit(_call_deepseek_batch, b): b for b in batches}
        for future in as_completed(future_to_batch):
            try:
                proposals.update(future.result())
            except Exception as exc:
                print(f"  [WARN] batch failed, stubs will be used: {exc}", flush=True)
            completed += 1
            if completed % 20 == 0 or completed == len(batches):
                print(f"  {completed}/{len(batches)} batches complete", flush=True)

    # Fill any missing entries with stubs
    for item in items:
        lemma = item["lemma"]
        if lemma not in proposals:
            proposals[lemma] = f"[model_proposed: {lemma}]"

    return proposals


def _write_gap_proposals(conn, proposals: dict[str, str], src_model: int) -> int:
    """Pre-write glossary_term + glossary_sense + sense_rendering for each proposal.

    Uses DO UPDATE for sense_rendering so re-runs refresh proposals.
    Returns count of senses written.
    """
    written = 0
    for lemma, sk_proposal in sorted(proposals.items()):
        term_id = _ensure_glossary_term(conn, lemma)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO glossary_sense (term_id, context_label, status)
                VALUES (%s, NULL, 'proposed')
                ON CONFLICT DO NOTHING
                RETURNING sense_id
                """,
                (term_id,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT sense_id FROM glossary_sense "
                    "WHERE term_id = %s AND context_label IS NULL",
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
        written += 1
    conn.commit()
    return written


# ── Main resolution loop ──────────────────────────────────────────────────────


def resolve_segment(
    segment: dict,
    multiword_terms: list[dict],
    lemma_to_term: dict[str, dict],
    conn,
    cs_rank: int,
    en_rank: int,
    src_model: int,
    gap_proposals: dict[str, str] | None = None,
) -> list[Resolution]:
    """Resolve all terms in one segment. Returns list of Resolutions."""
    latin = segment["latin"] or ""
    czech = segment["czech"]
    english = segment["english"]

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

    seen_lemmas: set[str] = set()
    gap_candidates: set[str] = set()  # lemmas not in Krystal, collected during token pass

    for token in tokens:
        for lemma in lemmatize_latin(token):
            if lemma in seen_lemmas:
                continue
            seen_lemmas.add(lemma)

            term = lemma_to_term.get(lemma)
            if term is None:
                # Not in Krystal — candidate for gap handling.
                # Threshold > 5 chars filters most Latin function words
                # (enim, ergo, quod, sicut, esse, idem) while keeping
                # theological/philosophical terms (corpus, anima, potentia, actus...).
                if len(lemma) > 5:
                    gap_candidates.add(lemma)
                continue
            if term["term_id"] in seen_term_ids:
                continue
            seen_term_ids.add(term["term_id"])

            # 3. Resolve sense
            senses = term["senses"]
            if len(senses) == 1:
                resolutions.append(_resolve_single(term))
            else:
                resolutions.append(_resolve_multi(term, czech, english, cs_rank, en_rank))

    # 4. Gap terms: Latin lemmas found in the segment that have no Krystal entry.
    # Method: bahounek_derived if Czech text present, english_derived if English
    # present, model_proposed otherwise.  For M1 the sk proposal is a stub;
    # M2 will replace model_proposed with a real DeepSeek call.
    gap_lemmas = sorted(gap_candidates)

    for lemma in gap_lemmas:
        if czech:
            method = "bahounek_derived"
        elif english:
            method = "english_derived"
        else:
            method = "model_proposed"

        # Use pre-computed proposal from batch pre-scan; fall back to stub for
        # lemmas below the freq floor or filtered out by POS.
        if gap_proposals is not None and lemma in gap_proposals:
            sk_proposal = gap_proposals[lemma]
        else:
            sk_proposal = f"[{method}: {lemma}]"

        term_id = _ensure_glossary_term(conn, lemma)
        sense = _gap_sense(conn, term_id, method, sk_proposal, src_model)

        gap_term = {"term_id": term_id, "latin_lemma": lemma, "is_multiword": False, "senses": [sense]}
        # Do NOT add to lemma_to_term: that dict is for Krystal terms only.
        # Subsequent segments will re-call _ensure_glossary_term/_gap_sense (idempotent).
        # Adding here would cause the next segment to resolve via krystal_single instead
        # of the correct gap method.

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
        # Wipe existing for this segment (idempotency)
        cur.execute("DELETE FROM term_usage WHERE segment_id = %s", (segment_id,))
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
    pos_filter: frozenset[str] | None = _GAP_POS_FILTER,
    batch_size: int = _GAP_BATCH_SIZE,
    max_workers: int = _GAP_MAX_WORKERS,
) -> None:
    """Two-phase resolver.

    Phase 1: scan all segments for gap lemmas, filter by freq_floor / pos_filter,
             batch-propose Slovak terms via DeepSeek, pre-write to DB.
    Phase 2: main resolution loop — Krystal terms + gap terms (proposals already in DB).

    Knobs:
      freq_floor  — min segment frequency for a gap lemma (default 10)
      pos_filter  — CLTK POS codes to keep; None = no filter (default {'N','A'})
      batch_size  — lemmas per DeepSeek batch call (default 25)
      max_workers — concurrent batch requests (default 10)
    """
    pos_label = sorted(pos_filter) if pos_filter else "all"
    print(f"Loading glossary and segments (freq_floor={freq_floor}, pos_filter={pos_label})...")

    with get_conn() as conn:
        wid = work_id(conn, "summa_articulus")
        src_model = source_id(conn, "model")
        cs_rank = _source_rank(conn, "bahounek")
        en_rank = _source_rank(conn, "dominican")
        multiword_terms, singleword_terms = _load_glossary(conn)
        segments = _load_segments(conn, wid)

    lemma_to_term = {t["latin_lemma"]: t for t in singleword_terms}
    krystal_lemmas = set(lemma_to_term.keys()) | {t["latin_lemma"] for t in multiword_terms}
    print(f"  Glossary: {len(multiword_terms)} multiword + {len(singleword_terms)} singleword Krystal terms")
    print(f"  Segments: {len(segments)} body segments to resolve")

    # ── Phase 1: scan, filter, batch-propose ─────────────────────────────────
    print("\n[Phase 1] Scanning gap lemmas...")
    gap_data = _scan_gap_lemmas(segments, krystal_lemmas, freq_floor, pos_filter)
    print(f"  {len(gap_data)} gap lemmas qualify (freq≥{freq_floor}, pos={pos_label})")

    gap_proposals: dict[str, str] = {}
    if gap_data:
        gap_proposals = _propose_gap_terms(gap_data, batch_size=batch_size, max_workers=max_workers)
        with get_conn() as conn:
            src_model_conn = source_id(conn, "model")
            n_written = _write_gap_proposals(conn, gap_proposals, src_model_conn)
        print(f"  {n_written} gap senses pre-written to DB")

    # ── Phase 2: main resolution loop ────────────────────────────────────────
    print(f"\n[Phase 2] Resolving {len(segments)} segments...")
    total_usages = 0

    with get_conn() as conn:
        src_model = source_id(conn, "model")
        cs_rank = _source_rank(conn, "bahounek")
        en_rank = _source_rank(conn, "dominican")
        multiword_terms, singleword_terms = _load_glossary(conn)
        lemma_to_term = {t["latin_lemma"]: t for t in singleword_terms}
        segments = _load_segments(conn, wid)

        for i, seg in enumerate(segments, 1):
            resolutions = resolve_segment(
                seg, multiword_terms, lemma_to_term,
                conn, cs_rank, en_rank, src_model, gap_proposals,
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
            {**stats, "cost_usd": round(cost_usd, 6), "lemmas_proposed": len(gap_proposals)},
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        sys.exit(1)
