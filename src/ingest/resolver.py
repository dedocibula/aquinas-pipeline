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
import sys
from pathlib import Path

from common.deepseek import _api_stats, _api_stats_lock, get_api_stats
from common.glossary_repo import _load_glossary, _load_segments
from ingest.gap_terms import (
    _GAP_BATCH_SIZE,
    _GAP_FREQ_CEILING_PCT,
    _GAP_FREQ_FLOOR,
    _GAP_MAX_WORKERS,
    _GAP_MIN_LEN,
    _load_existing_gap_terms,
    _load_ignored_lemmas,
    _propose_gap_terms,
    _scan_gap_lemmas,
)
from ingest.resolution import _source_rank, _write_term_usage, resolve_segment
from storage.db import get_conn, source_id, work_id

ROOT = Path(__file__).resolve().parents[2]


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
        multiword_terms, singleword_terms = _load_glossary(conn)
        segments = _load_segments(conn, wid)
        ignored_lemmas = _load_ignored_lemmas(conn)

    lemma_to_term = {t["latin_lemma"]: t for t in singleword_terms}
    krystal_lemmas = set(lemma_to_term.keys()) | {t["latin_lemma"] for t in multiword_terms}
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


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        sys.exit(1)
