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
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from common.db import get_conn
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
            elapsed=time.time() - start,
        )
        return

    translated_count = 0
    needs_human_count = 0
    iterations_list: list[int] = []

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

            status = translate_segment(sid, conn)
            notes = fetch_reviewer_notes(conn, sid)
            iters = _iteration_count(notes, status)
            iterations_list.append(iters)

            if status == "translated":
                translated_count += 1
            else:
                needs_human_count += 1

            log.info(
                "  → %s (iter=%d, running needs_human=%.1f%%)",
                status,
                iters,
                100.0 * needs_human_count / i,
            )

    elapsed = time.time() - start
    total_run = len(pending)
    _write_report(
        total_segments=total_all,
        translated=translated_count,
        needs_human=needs_human_count,
        iterations_list=iterations_list,
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
    elapsed: float,
) -> None:
    total_run = translated + needs_human

    def pct(n: int) -> str:
        return f"{100.0 * n / total_run:.1f}%" if total_run else "N/A"

    avg_iters = sum(iterations_list) / total_run if total_run else 0.0
    mins, secs = divmod(int(elapsed), 60)

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
    ]

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _REPORTS_DIR / "m4_pilot.txt"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("Report written: %s", report_path)


if __name__ == "__main__":
    run_pilot()
