"""Pilot translation runner — translates Q1–Q6 of Prima Pars.

Usage:
    uv run python -m translate.pilot

Abort conditions (exits 1):
    - needs_human / total > 0.20   → rubric too strict
    - sum(iterations) / total > 2.5 → translator prompt needs tuning

Writes reports/m4_pilot.txt on completion.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from common.db import get_conn
from common.pricing import UsageInfo
from translate.loop import translate_segment

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

_PILOT_QUESTIONS = ["I.q1", "I.q2", "I.q3", "I.q4", "I.q5", "I.q6"]
_REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"

_ABORT_NEEDS_HUMAN_RATE = 0.20
_ABORT_AVG_ITERATIONS = 2.5


@dataclass
class SegmentStats:
    segment_id: int
    usages: list[UsageInfo] = field(default_factory=list)
    latin_chars: int = 0
    czech_chars: int = 0
    english_chars: int = 0


# ── DB helpers ─────────────────────────────────────────────────────────────────


def fetch_pilot_segments(conn) -> list[dict]:
    """Return all pending segments in Q1–Q6, ordered by locator_path."""
    sql = f"""
        SELECT segment_id, locator_path::text, translation_status
        FROM segment
        WHERE ({" OR ".join("locator_path <@ %s::ltree" for _ in _PILOT_QUESTIONS)})
          AND translation_status = 'pending'
        ORDER BY locator_path
    """
    with conn.cursor() as cur:
        cur.execute(sql, _PILOT_QUESTIONS)
        return [{"segment_id": r[0], "locator_path": r[1], "status": r[2]} for r in cur.fetchall()]


def fetch_all_pilot_segments(conn) -> list[dict]:
    """Return ALL segments in Q1–Q6 (regardless of status) for progress report."""
    sql = f"""
        SELECT segment_id, translation_status
        FROM segment
        WHERE {" OR ".join("locator_path <@ %s::ltree" for _ in _PILOT_QUESTIONS)}
        ORDER BY locator_path
    """
    with conn.cursor() as cur:
        cur.execute(sql, _PILOT_QUESTIONS)
        return [{"segment_id": r[0], "status": r[1]} for r in cur.fetchall()]


def fetch_segment_text_lengths(conn, segment_id: int) -> dict[str, int]:
    """Return character lengths of la/cs/en texts for a segment.

    Returns a dict with keys 'la', 'cs', 'en'; missing languages default to 0.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT lang, LENGTH(content)
            FROM segment_text
            WHERE segment_id = %s AND lang IN ('la', 'cs', 'en')
            """,
            (segment_id,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_corpus_char_counts(conn) -> dict[str, int]:
    """Return total character counts per lang across the full corpus.

    Returns a dict with keys 'la', 'cs', 'en'; used for M5 cost extrapolation.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT lang, SUM(LENGTH(content))::bigint AS total_chars
            FROM segment_text
            WHERE lang IN ('la', 'cs', 'en')
            GROUP BY lang
            """
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_reviewer_notes(conn, segment_id: int) -> dict | None:
    """Return reviewer_notes JSON for a segment, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT reviewer_notes FROM segment WHERE segment_id = %s",
            (segment_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _iteration_count(notes: dict | None, status: str) -> int:
    """Infer iteration count from reviewer_notes and final status."""
    if status == "needs_human":
        return 3  # always exhausted MAX_ITERATIONS
    if notes and isinstance(notes, dict) and "iteration" in notes:
        return notes["iteration"]
    return 1  # APPROVED with no notes → approved on first R1 call


# ── Pilot runner ──────────────────────────────────────────────────────────────


def run_pilot() -> None:
    start = time.time()

    with get_conn() as conn:
        pending = fetch_pilot_segments(conn)
        total_all = len(fetch_all_pilot_segments(conn))

    log.info(
        "Pilot set: %d total segments in Q1–Q6, %d pending translation",
        total_all,
        len(pending),
    )

    if not pending:
        log.info("All segments already translated — nothing to do.")
        _write_report(
            total_segments=total_all,
            translated=total_all,
            needs_human=0,
            iterations_list=[],
            stats_list=[],
            elapsed=time.time() - start,
        )
        return

    translated_count = 0
    needs_human_count = 0
    iterations_list: list[int] = []
    stats_list: list[SegmentStats] = []

    with get_conn() as conn:
        for i, seg in enumerate(pending, 1):
            sid = seg["segment_id"]
            log.info(
                "[%d/%d] segment_id=%d  %s",
                i,
                len(pending),
                sid,
                seg["locator_path"],
            )

            status, usages = translate_segment(sid, conn)
            notes = fetch_reviewer_notes(conn, sid)
            iters = _iteration_count(notes, status)
            iterations_list.append(iters)

            lengths = fetch_segment_text_lengths(conn, sid)
            seg_stats = SegmentStats(
                segment_id=sid,
                usages=usages,
                latin_chars=lengths.get("la", 0),
                czech_chars=lengths.get("cs", 0),
                english_chars=lengths.get("en", 0),
            )
            stats_list.append(seg_stats)

            seg_cost = sum(u.cost_usd for u in usages)
            if status == "translated":
                translated_count += 1
            else:
                needs_human_count += 1

            log.info(
                "  → %s (iter=%d, cost=$%.4f, running needs_human=%.1f%%)",
                status,
                iters,
                seg_cost,
                100.0 * needs_human_count / i,
            )

    elapsed = time.time() - start
    total_run = len(pending)
    _write_report(
        total_segments=total_all,
        translated=translated_count,
        needs_human=needs_human_count,
        iterations_list=iterations_list,
        stats_list=stats_list,
        elapsed=elapsed,
    )

    avg_iters = sum(iterations_list) / total_run if iterations_list else 0.0
    needs_human_rate = needs_human_count / total_run if total_run else 0.0

    abort = False
    if needs_human_rate > _ABORT_NEEDS_HUMAN_RATE:
        log.error(
            "ABORT: needs_human rate %.1f%% > 20%% — "
            "rubric too strict; adjust reviewer.py before proceeding",
            100 * needs_human_rate,
        )
        abort = True
    if avg_iters > _ABORT_AVG_ITERATIONS:
        log.error(
            "ABORT: avg_iterations %.2f > 2.5 — "
            "translator prompt needs tuning",
            avg_iters,
        )
        abort = True

    if abort:
        sys.exit(1)

    log.info("Pilot complete. Report written to reports/m4_pilot.txt")


def _write_report(
    *,
    total_segments: int,
    translated: int,
    needs_human: int,
    iterations_list: list[int],
    stats_list: list[SegmentStats],
    elapsed: float,
) -> None:
    total_run = translated + needs_human

    def pct(n: int) -> str:
        return f"{100.0 * n / total_run:.1f}%" if total_run else "N/A"

    avg_iters = sum(iterations_list) / total_run if total_run else 0.0
    mins, secs = divmod(int(elapsed), 60)

    # ── Aggregate usages by model ───────────────────────────────────────────
    all_usages = [u for s in stats_list for u in s.usages]
    # Use the same env-resolved model IDs that translator.py and reviewer.py use,
    # so filtering works correctly even if overridden via DEEPSEEK_MODEL env vars.
    translator_model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    reviewer_model   = os.environ.get("DEEPSEEK_R1_MODEL", "deepseek-reasoner")

    t_usages = [u for u in all_usages if u.model == translator_model]
    r_usages = [u for u in all_usages if u.model == reviewer_model]

    t_cost = sum(u.cost_usd for u in t_usages)
    r_cost = sum(u.cost_usd for u in r_usages)
    total_cost = t_cost + r_cost

    total_hit   = sum(u.cache_hit_tokens  for u in all_usages)
    total_miss  = sum(u.cache_miss_tokens for u in all_usages)
    total_input = total_hit + total_miss
    hit_rate    = total_hit / total_input if total_input else 0.0

    # ── Calibration ratios (for full-corpus projection) ────────────────────
    # Input chars = la + cs + en text fed into the translator prompt.
    pilot_input_chars = sum(
        s.latin_chars + s.czech_chars + s.english_chars for s in stats_list
    )
    pilot_la_chars = sum(s.latin_chars for s in stats_list)

    # Translator: cost per input char (la+cs+en)
    t_cost_per_input_char = t_cost / pilot_input_chars if pilot_input_chars else 0.0

    # Reviewer cost scales with la chars (reviewer sees Latin + draft ≈ 2×la chars)
    r_cost_per_la_char = r_cost / pilot_la_chars if pilot_la_chars else 0.0

    # ── Full-corpus char counts ─────────────────────────────────────────────
    try:
        with get_conn() as conn:
            corpus_chars = fetch_corpus_char_counts(conn)
    except Exception as exc:
        log.warning("Could not fetch corpus char counts for extrapolation: %s", exc)
        corpus_chars = {}

    corpus_la = corpus_chars.get("la", 0)
    corpus_cs = corpus_chars.get("cs", 0)
    corpus_en = corpus_chars.get("en", 0)
    corpus_input_chars = corpus_la + corpus_cs + corpus_en

    est_t_cost = corpus_input_chars * t_cost_per_input_char
    est_r_cost = corpus_la          * r_cost_per_la_char
    est_total  = est_t_cost + est_r_cost

    avg_cost_per_seg = total_cost / total_run if total_run else 0.0

    # ── Report lines ────────────────────────────────────────────────────────
    lines = [
        "PILOT RUN SUMMARY",
        f"  Pilot questions:   {', '.join(_PILOT_QUESTIONS)}",
        f"  Total segments:    {total_segments}",
        f"  Translated:        {translated}  ({pct(translated)})",
        f"  Needs human:       {needs_human}  ({pct(needs_human)})",
        f"  Avg iterations:    {avg_iters:.2f}",
        f"  Time elapsed:      {mins}m {secs}s",
        "",
        "ABORT THRESHOLDS",
        f"  needs_human > 20%: {'TRIGGERED' if total_run and needs_human / total_run > _ABORT_NEEDS_HUMAN_RATE else 'ok'}",
        f"  avg_iters > 2.5:   {'TRIGGERED' if avg_iters > _ABORT_AVG_ITERATIONS else 'ok'}",
        "",
        "COST (actual, pilot run only)",
        f"  Translator ({translator_model}):  ${t_cost:.4f}",
        f"  Reviewer   ({reviewer_model}): ${r_cost:.4f}",
        f"  Total:                          ${total_cost:.4f}",
        f"  Avg cost/segment:               ${avg_cost_per_seg:.5f}",
        f"  Cache hit rate:                 {hit_rate * 100:.1f}%"
        f"  ({total_hit:,} hit / {total_input:,} total input tokens)",

        "",
        "M5 EXTRAPOLATION (calibrated from pilot)",
        f"  Pilot segments:                 {total_run}",
        f"  Pilot input chars (la+cs+en):   {pilot_input_chars:,}",
        f"  Corpus chars — la:              {corpus_la:,}",
        f"                  cs:              {corpus_cs:,}",
        f"                  en:              {corpus_en:,}",
        f"                  total:           {corpus_input_chars:,}",
        f"  Calibrated translator $/char:   ${t_cost_per_input_char:.8f}",
        f"  Calibrated reviewer $/la-char:  ${r_cost_per_la_char:.8f}",
        f"  Est. translator cost (corpus):  ~${est_t_cost:.2f}",
        f"  Est. reviewer cost (corpus):    ~${est_r_cost:.2f}",
        f"  Est. full corpus total:         ~${est_total:.2f}",
        f"  Note: assumes cache hit rate stays at {hit_rate * 100:.1f}%;"
        " lower rate raises cost proportionally.",
    ]

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _REPORTS_DIR / "m4_pilot.txt"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("Report written: %s", report_path)


if __name__ == "__main__":
    run_pilot()
