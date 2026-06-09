"""Segment-level term resolution: phrase matching, sense voting, term_usage writes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from common.lemmatize import lemmatize_czech, lemmatize_latin
from ingest.gap_terms import _GAP_MIN_LEN, _strip_lemma_suffix

# Authority rank threshold for a "strong" signal (Krystal=10, Bahounek=20)
_STRONG_RANK_THRESHOLD = 20

# Element types to run the resolver on.
# Titles have no Latin text and resolve to zero terms, but are included so the
# resolver produces a clean (empty) result for them.
_BODY_TYPES = {"arg", "sed_contra", "respondeo", "reply", "article_title", "question_title"}


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
