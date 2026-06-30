"""
Term resolver.

Processes every body segment and writes term_usage rows with full provenance.
DeepSeek V3 proposes Slovak terms for ALL gap lemmas (not in Krystal) in a
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
  (term/name/formula/prose) stored on glossary_term.category and overridable later.
  No POS filter, no static word lists — only a mechanical length gate + suffix strip.

Determinism: all intermediate collections are sorted; no randomness.

DeepSeek env vars:
  DEEPSEEK_API_KEY  — required when gap terms exist
  DEEPSEEK_API_URL  — default: https://api.deepseek.com/v1/chat/completions
  DEEPSEEK_MODEL    — default: deepseek-v4-flash

Run:
  uv run python -m ingest.resolver
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from common.deepseek import _api_stats, _api_stats_lock, get_api_stats
from common.lemmatize import lemmatize_czech, lemmatize_latin, pos_tag_latin
from ingest.gap_terms import (
    _GAP_BATCH_SIZE,
    _GAP_FREQ_CEILING_PCT,
    _GAP_FREQ_FLOOR,
    _GAP_MAX_WORKERS,
    _GAP_MIN_LEN,
    _canonical_lemma,
    _load_existing_gap_terms,
    _load_ignored_lemmas,
    _propose_gap_terms,
    _scan_gap_lemmas,
)
from storage.db import get_conn, source_id, work_id
from storage.models import Segment, Sense, Term
from storage.repositories import GlossaryRepository, SegmentRepository, TermUsageRepository

ROOT = Path(__file__).resolve().parents[2]

# Authority rank threshold for a "strong" signal (Krystal=10, Bahounek=20)
_STRONG_RANK_THRESHOLD = 20


def _source_rank(conn, code: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT authority_rank FROM source WHERE code = %s", (code,))
        return cur.fetchone()[0]


# ── Phrase matching ───────────────────────────────────────────────────────────


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _match_pattern(term: Term) -> re.Pattern:
    """Return a compiled regex for matching this term in Latin text.

    Uses la_surface over latin_lemma (human-edited surface text beats the slug key).
    Formula terms are anchored at start-of-text so "Praeterea" in the middle of a
    sentence does not fire as a formula opener.
    """
    surface = term.la_surface or term.latin_lemma
    escaped = re.escape(surface)
    if term.category == "formula":
        return re.compile(r"^" + escaped, re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def phrase_match(latin_text: str, multiword_terms: list[Term]) -> list[tuple[Term, str]]:
    """Find all multiword glossary terms in latin_text.

    Returns list of (term, matched_span) in document order.
    Matched spans are removed from further processing (returned as masked text).
    """
    normalized = _normalize_ws(latin_text)
    matches: list[tuple[int, int, Term, str]] = []

    for term in multiword_terms:
        pattern = _match_pattern(term)
        if term.category == "formula":
            # Anchored pattern: at most one match at position 0.
            m = pattern.match(normalized)
            if m:
                matches.append((m.start(), m.end(), term, m.group()))
        else:
            for m in pattern.finditer(normalized):
                matches.append((m.start(), m.end(), term, m.group()))

    # Sort by start position; for same start prefer longer match (greedy / leftmost-longest).
    # This ensures "actus essendi" is consumed before "actus" can claim the same position.
    matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    result: list[tuple[Term, str]] = []
    last_end = 0
    for start, end, term, span in matches:
        if start >= last_end:
            result.append((term, span))
            last_end = end

    return result


def mask_spans(latin_text: str, multiword_terms: list[Term]) -> str:
    """Return latin_text with matched multiword spans replaced by spaces."""
    normalized = _normalize_ws(latin_text)
    for term in multiword_terms:
        pattern = _match_pattern(term)
        normalized = pattern.sub(" ", normalized)
    return normalized


# ── Sense resolution ──────────────────────────────────────────────────────────


@dataclass
class Resolution:
    term: Term
    sense: Sense
    method: str
    confidence: str   # 'auto' | 'needs_review'
    signals: dict = field(default_factory=dict)


def _resolve_single(term: Term) -> Resolution:
    """Single-sense term: silent auto-resolution."""
    return Resolution(
        term=term,
        sense=term.senses[0],
        method="krystal_single",
        confidence="auto",
    )


def _resolve_multi(term: Term, czech_text: str | None, english_text: str | None,
                   cs_rank: int, en_rank: int) -> Resolution:
    """Multi-sense term: evidence vote."""
    senses = term.senses
    signals: dict[int, list[tuple[int, str]]] = {}  # sense_id → [(rank, signal_desc)]

    # Czech signal: MorphoDiTa lemmatizes segment cs text, match against sense cs_lemma
    if czech_text:
        cs_lemmas = set()
        for token in re.findall(r"\w+", czech_text):
            cs_lemmas.update(lemmatize_czech(token))

        for sense in senses:
            cs_key = sense.cs_lemma or sense.cs_content or ""
            if cs_key and cs_key in cs_lemmas:
                sid = sense.sense_id
                signals.setdefault(sid, []).append(
                    (cs_rank, f"cs={cs_key}→sense_{sid}")
                )

    # English signal: substring match of en_cue in english_text
    if english_text:
        for sense in senses:
            en_cue = sense.en_cue or ""
            if en_cue and en_cue.lower() in english_text.lower():
                sid = sense.sense_id
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

        winning_sense = next(s for s in senses if s.sense_id == winning_sid)
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
    primary = next((s for s in senses if s.context_label is None), senses[0])
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


# ── Perfect-passive habere suppression ─────────────────────────────────────────
#
# CLTK lemmatizes the perfect-passive participle 'habitum' (in 'habitum est' /
# 'habita sunt' — "as was held/stated") to the noun *habitus*, so a segment that
# never mentions the concept would otherwise get a bogus habitus term_usage row.
# pos_tag_latin CANNOT disambiguate the participle itself: it tags 'habitum' '?'
# both as the PPP and as the accusative noun. The reliable signal is the *esse*
# copula that follows, which the tagger marks as a verb ('est'/'sunt' → 'V'); so
# the construction is detected by the participle + a verb-tagged copula, and the
# noun is suppressed only when that construction is the segment's sole habitus
# evidence ('habita' alone lemmatizes to 'habeo', so only 'habitum' matters).

_HABERE_PPP_RE = re.compile(r"\b(?:habitum|habita)\s+(?:est|sunt)\b", re.IGNORECASE)
_HABERE_PPP_FORMS = {"habitum", "habita"}
_ESSE_FORMS = {"est", "sunt"}


def _suppressed_habitus_tokens(latin: str) -> set[str]:
    """Surface tokens that lemmatize to *habitus* only via perfect-passive habere.

    Returns the set of participle surfaces ('habitum'/'habita') to suppress when
    the 'habitum est'/'habita sunt' construction accounts for *every* occurrence
    of that surface in `latin`; empty when habitus is genuinely present elsewhere
    (or the construction is absent).

    Suppression is decided per surface by occurrence count: a surface is bogus
    only when its total occurrences ≤ its construction occurrences. A surface
    that also appears as a genuine accusative noun (total > construction — e.g.
    'per habitum virtutis ... ut supra habitum est') keeps its habitus
    constraint; resolution is surface-granular, so this is the finest decision
    the consuming resolver loop can act on.
    """
    if not _HABERE_PPP_RE.search(latin):
        return set()
    tagged = pos_tag_latin(latin)
    # Per-surface count of the 'habitum/habita' + verb-tagged copula construction.
    construction_counts = Counter(
        surface.lower()
        for (surface, _), (nxt, nxt_pos) in zip(tagged, tagged[1:])
        if surface.lower() in _HABERE_PPP_FORMS
        and nxt.lower() in _ESSE_FORMS
        and nxt_pos == "V"
    )
    if not construction_counts:
        return set()
    # Genuine habitus evidence from any *other* token keeps the noun in play.
    for token in set(re.findall(r"[a-zA-Z]+", latin)):
        if token.lower() in construction_counts:
            continue
        if "habitus" in {_canonical_lemma(cand) for cand in lemmatize_latin(token)}:
            return set()
    # Suppress a surface only when all its occurrences are the construction;
    # if it also occurs as a genuine accusative (total > construction), keep it.
    total_counts = Counter(t.lower() for t in re.findall(r"[a-zA-Z]+", latin))
    return {
        surface
        for surface, c_count in construction_counts.items()
        if total_counts[surface] <= c_count
    }


def resolve_segment(
    segment: Segment,
    multiword_terms: list[Term],
    lemma_to_term: dict[str, Term],
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
    latin = segment.latin or ""
    czech = segment.czech
    english = segment.english
    gap_terms_db = gap_terms_db or {}

    resolutions: list[Resolution] = []
    seen_term_ids: set[int] = set()  # deduplicate: one resolution per term per segment

    # 1. Phrase-match multiword terms first
    mw_matches = phrase_match(latin, multiword_terms)
    for term, _span in mw_matches:
        if term.term_id in seen_term_ids:
            continue
        seen_term_ids.add(term.term_id)
        if len(term.senses) == 1:
            resolutions.append(_resolve_single(term))
        else:
            resolutions.append(_resolve_multi(term, czech, english, cs_rank, en_rank))

    # 2. Lemmatize remaining tokens
    masked = mask_spans(latin, [t for t, _ in mw_matches])
    tokens = sorted(set(re.findall(r"[a-zA-Z]+", masked)))  # sorted for determinism

    # Tokens that lemmatize to *habitus* only via perfect-passive habere — never
    # resolve them to the noun (see _suppressed_habitus_tokens).
    suppressed_habitus = _suppressed_habitus_tokens(latin)

    gap_candidates: set[str] = set()  # stripped lemmas not in Krystal, for gap handling

    for token in tokens:
        cands = lemmatize_latin(token)
        if not cands:
            continue

        # Krystal: try every candidate lemma; first hit wins. Lookup is
        # case-insensitive (lemma_to_term is lowercase-keyed) so a sentence-initial
        # "Caritas" resolves to the Krystal "caritas" rather than falling to gap.
        krystal_hit = False
        for lemma in cands:
            term = lemma_to_term.get(lemma.lower())
            if term is None:
                continue
            # Suppress the noun habitus when this token is only a perfect-passive
            # habere participle: consume the token (no resolution, no gap keying).
            if term.latin_lemma == "habitus" and token.lower() in suppressed_habitus:
                krystal_hit = True
                break
            krystal_hit = True
            if term.term_id not in seen_term_ids:
                seen_term_ids.add(term.term_id)
                if len(term.senses) == 1:
                    resolutions.append(_resolve_single(term))
                else:
                    resolutions.append(_resolve_multi(term, czech, english, cs_rank, en_rank))
            break
        if krystal_hit:
            continue

        # Gap: the canonical first candidate (matches _scan_gap_lemmas keying:
        # suffix-stripped + lowercased).
        stripped = _canonical_lemma(cands[0])
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

        sense = Sense(
            sense_id=db["sense_id"],
            context_label=None,
            version=db["version"],
            cs_lemma=None,
            cs_content=None,
            en_cue=None,
            sk_content=db["slovak"],
            la_surface=None,
        )
        gap_term = Term(
            term_id=db["term_id"],
            latin_lemma=stripped,
            is_multiword=False,
            category=db.get("category"),
            la_surface=None,
            senses=(sense,),
        )
        resolutions.append(Resolution(
            term=gap_term,
            sense=sense,
            method=method,
            confidence="needs_review",
        ))

    return resolutions


# ── Entry point ───────────────────────────────────────────────────────────────


def run(
    freq_floor: int = _GAP_FREQ_FLOOR,
    batch_size: int = _GAP_BATCH_SIZE,
    max_workers: int = _GAP_MAX_WORKERS,
    min_len: int = _GAP_MIN_LEN,
    freq_ceiling_pct: float = _GAP_FREQ_CEILING_PCT,
) -> None:
    """Two-phase resolver.

    Phase 1: scan all segments for gap lemmas, then classify/translate via DeepSeek
             and pre-write glossary_term(category) + sense + sk rendering to DB.
    Phase 2: main resolution loop — Krystal terms + gap terms (proposals in DB).
             A gap lemma only becomes a term_usage row if it received a Phase-1
             proposal (no-stub invariant).

    Knobs:
      freq_floor      — min segment frequency for a gap lemma (default 10)
      batch_size      — lemmas per DeepSeek batch call (default 50)
      max_workers     — concurrent batch requests (default 10)
      min_len         — gap lemma must be longer than this (default 3)
      freq_ceiling_pct — drop lemmas appearing in >X% of segments (default 0.40)
    """
    # Reset accumulated stats so a single-process multi-step run reports only this run.
    with _api_stats_lock:
        _api_stats["calls"] = 0
        _api_stats["input_tokens"] = 0
        _api_stats["output_tokens"] = 0

    print(f"Loading glossary and segments (freq_floor={freq_floor}, min_len={min_len}, "
          f"freq_ceiling_pct={freq_ceiling_pct})...")

    with get_conn() as conn:
        wid = work_id(conn, "summa_articulus")
        multiword_terms, singleword_terms = GlossaryRepository(conn).load_glossary()
        segments = SegmentRepository(conn).load_body_segments(wid)
        ignored_lemmas = _load_ignored_lemmas(conn)

    # Lowercase-keyed so Krystal lookup is case-insensitive (all Krystal lemmas
    # are lowercase; a capitalized token must still resolve, not leak to gap).
    lemma_to_term = {t.latin_lemma.lower(): t for t in singleword_terms}
    krystal_lemmas = set(lemma_to_term.keys()) | {t.latin_lemma.lower() for t in multiword_terms}
    print(f"  Glossary: {len(multiword_terms)} multiword + {len(singleword_terms)} singleword Krystal terms")
    print(f"  Ignored (stopword): {len(ignored_lemmas)}")
    print(f"  Segments: {len(segments)} body segments to resolve")

    # ── Phase 1: scan, batch-propose (classify + translate) ─────────────────────
    print("\n[Phase 1] Scanning gap lemmas...")
    gap_data = _scan_gap_lemmas(
        segments, krystal_lemmas, freq_floor, min_len, freq_ceiling_pct, ignored_lemmas,
    )
    print(f"  {len(gap_data)} gap lemmas qualify (freq≥{freq_floor}, len>{min_len}, "
          f"ceil≤{freq_ceiling_pct*100:.0f}%)")

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
        multiword_terms, singleword_terms = GlossaryRepository(conn).load_glossary()
        lemma_to_term = {t.latin_lemma.lower(): t for t in singleword_terms}
        segments = SegmentRepository(conn).load_body_segments(wid)
        usage_repo = TermUsageRepository(conn)

        for i, seg in enumerate(segments, 1):
            resolutions = resolve_segment(
                seg, multiword_terms, lemma_to_term,
                cs_rank, en_rank, gap_terms_db, min_len,
            )
            n = usage_repo.write_term_usage(seg.segment_id, resolutions)
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


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        sys.exit(1)
