"""
Term resolver — M1/M2.

Processes every body segment and writes term_usage rows with full provenance.
M2 addition: real DeepSeek V3 API calls for model_proposed gap terms.

Order of operations (strict per spec):
  1. Phrase-match multiword glossary_term entries first.
  2. Lemmatize remaining Latin tokens with CLTK.
  3. Resolve each matched term's sense:
     - Single-sense  → krystal_single,        confidence=auto
     - Multi-sense   → evidence vote from cs/en signals:
         consistent + ≥1 rank≤20 signal → krystal_multi_voted,    confidence=auto
         otherwise                       → krystal_multi_flagged,  confidence=needs_review
     - Not in Krystal:
         Bahounek cs available  → bahounek_derived,  confidence=needs_review
         English en available   → english_derived,   confidence=needs_review
         nothing                → model_proposed,    confidence=needs_review (DeepSeek V3)
  4. Write term_usage rows.

Determinism: all intermediate collections are sorted; no randomness.

DeepSeek env vars:
  DEEPSEEK_API_KEY  — required when model_proposed terms exist
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

# ── DeepSeek API ──────────────────────────────────────────────────────────────

_api_stats: dict[str, int] = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

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
        _api_stats["input_tokens"] += usage.get("prompt_tokens", 0)
        _api_stats["output_tokens"] += usage.get("completion_tokens", 0)

        term = data["choices"][0]["message"]["content"].strip()
        return term or f"[model_proposed: {latin_lemma}]"

    except Exception as exc:
        print(f"  [WARN] DeepSeek API error for {latin_lemma!r}: {exc}", flush=True)
        return f"[model_proposed: {latin_lemma}]"


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

    return {
        "sense_id": sense_id,
        "context_label": None,
        "version": version,
        "cs_lemma": None,
        "cs_content": None,
        "en_cue": None,
        "sk_content": sk_proposal,
    }


# ── Main resolution loop ──────────────────────────────────────────────────────


def resolve_segment(
    segment: dict,
    multiword_terms: list[dict],
    lemma_to_term: dict[str, dict],
    conn,
    cs_rank: int,
    en_rank: int,
    src_model: int,
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

        if method == "model_proposed":
            sk_proposal = _call_deepseek(
                lemma,
                segment["latin"][:300] if segment.get("latin") else "",
                segment.get("czech") or "",
                segment.get("english") or "",
            )
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


def run() -> None:
    print("Loading glossary and segments...")
    with get_conn() as conn:
        wid = work_id(conn, "summa_articulus")
        src_model = source_id(conn, "model")
        cs_rank = _source_rank(conn, "bahounek")
        en_rank = _source_rank(conn, "dominican")

        multiword_terms, singleword_terms = _load_glossary(conn)
        lemma_to_term = {t["latin_lemma"]: t for t in singleword_terms}

        segments = _load_segments(conn, wid)
        print(f"  Glossary: {len(multiword_terms)} multiword + {len(singleword_terms)} singleword terms")
        print(f"  Segments: {len(segments)} body segments to resolve")

        total_usages = 0
        for i, seg in enumerate(segments, 1):
            resolutions = resolve_segment(
                seg, multiword_terms, lemma_to_term,
                conn, cs_rank, en_rank, src_model,
            )
            n = _write_term_usage(conn, seg["segment_id"], resolutions)
            total_usages += n
            if i % 500 == 0 or i == len(segments):
                print(f"  {i}/{len(segments)} segments resolved", flush=True)

        print(f"\nDone. {total_usages} term_usage rows written across {len(segments)} segments.")

    stats = get_api_stats()
    if stats["calls"] > 0:
        cost_usd = (stats["input_tokens"] * 0.00014 + stats["output_tokens"] * 0.00028) / 1000
        print(
            f"DeepSeek API: {stats['calls']} calls, "
            f"{stats['input_tokens']} input + {stats['output_tokens']} output tokens, "
            f"~${cost_usd:.4f}"
        )
        stats_path = ROOT / "reports" / "m2_api_stats.json"
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(json.dumps({**stats, "cost_usd": round(cost_usd, 6)}, indent=2))


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        sys.exit(1)
