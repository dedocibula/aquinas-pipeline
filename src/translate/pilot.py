"""Pilot translation runner — translates Q1–Q6 of Prima Pars.

Usage:
    uv run python -m translate.pilot
    PILOT_FULL=1   uv run python -m translate.pilot   # full Q1-Q6
    PILOT_TITLES=1 uv run python -m translate.pilot   # title segments only
    PILOT_SAMPLE=1 uv run python -m translate.pilot   # 100-segment representative sample

Abort conditions (exits 1):
    - needs_human / total > 0.20   → rubric too strict
    - sum(iterations) / total > 2.5 → translator prompt needs tuning

Writes reports/m4_pilot.txt on completion.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from common.db import get_conn
from common.pricing import UsageInfo
from translate.loop import translate_segment
from translate.prompt_logger import PromptLogger
from translate.run import ArticleResult, _close_run, _open_run

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

_PILOT_QUESTIONS = ["I.q1", "I.q2", "I.q3", "I.q4", "I.q5", "I.q6"]
_DEBUG_QUESTION = "I.q1"
_DEBUG_LIMIT = 10
_REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"
_SAMPLE_FILE = Path(__file__).resolve().parent.parent.parent / "docs" / "pilot_sample_100.json"

_ABORT_NEEDS_HUMAN_RATE = 0.20
_ABORT_AVG_ITERATIONS = 2.5
_DEFAULT_WORKERS = 1


@dataclass
class SegmentStats:
    segment_id: int
    usages: list[UsageInfo] = field(default_factory=list)
    latin_chars: int = 0
    czech_chars: int = 0
    english_chars: int = 0


# ── DB helpers ─────────────────────────────────────────────────────────────────


def fetch_pilot_segments(conn) -> list[dict]:
    """Return pending segments in Q1–Q6 that have Latin text, ordered by locator_path.

    article_title segments have English only (no Latin) and are included via the
    English fallback condition.
    """
    sql = f"""
        SELECT s.segment_id, s.locator_path::text, s.translation_status
        FROM segment s
        WHERE ({" OR ".join("s.locator_path <@ %s::ltree" for _ in _PILOT_QUESTIONS)})
          AND s.translation_status = 'pending'
          AND EXISTS (
              SELECT 1 FROM segment_text st
              WHERE st.segment_id = s.segment_id AND st.lang IN ('la', 'en')
          )
        ORDER BY s.locator_path
    """
    with conn.cursor() as cur:
        cur.execute(sql, _PILOT_QUESTIONS)
        return [{"segment_id": r[0], "locator_path": r[1], "status": r[2]} for r in cur.fetchall()]


def fetch_debug_segments(conn) -> list[dict]:
    """Return first _DEBUG_LIMIT pending segments in _DEBUG_QUESTION that have Latin text."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.segment_id, s.locator_path::text, s.translation_status
            FROM segment s
            WHERE s.locator_path <@ %s::ltree
              AND s.translation_status = 'pending'
              AND EXISTS (
                  SELECT 1 FROM segment_text st
                  WHERE st.segment_id = s.segment_id AND st.lang = 'la'
              )
            ORDER BY s.locator_path
            LIMIT %s
            """,
            (_DEBUG_QUESTION, _DEBUG_LIMIT),
        )
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


def fetch_title_segments(conn) -> list[dict]:
    """Return all pending question_title and article_title segments across the full corpus.

    These have English text only (no Latin) so R1 review is skipped — one
    translator call each, no reviewer cost.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.segment_id, s.locator_path::text, s.translation_status
            FROM segment s
            WHERE s.element_type IN ('question_title', 'article_title')
              AND s.translation_status = 'pending'
              AND EXISTS (
                  SELECT 1 FROM segment_text st
                  WHERE st.segment_id = s.segment_id AND st.lang = 'en'
              )
            ORDER BY s.locator_path
            """
        )
        return [{"segment_id": r[0], "locator_path": r[1], "status": r[2]} for r in cur.fetchall()]


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


def fetch_sample_segments(conn) -> list[dict]:
    """Return pending segments listed in docs/pilot_sample_100.json, ordered by locator_path."""
    sample = json.loads(_SAMPLE_FILE.read_text())
    ids = [s["segment_id"] for s in sample["segments"]]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.segment_id, s.locator_path::text, s.translation_status
            FROM segment s
            WHERE s.segment_id = ANY(%s)
              AND s.translation_status = 'pending'
              AND EXISTS (
                  SELECT 1 FROM segment_text st
                  WHERE st.segment_id = s.segment_id AND st.lang IN ('la', 'en')
              )
            ORDER BY s.locator_path
            """,
            (ids,),
        )
        return [{"segment_id": r[0], "locator_path": r[1], "status": r[2]} for r in cur.fetchall()]


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


# ── Pilot runner ──────────────────────────────────────────────────────────────


def run_pilot() -> None:
    start = time.time()
    debug_log_path = _REPORTS_DIR / f"debug_{int(start)}.jsonl"
    full_mode = os.environ.get("PILOT_FULL", "").strip() == "1"
    titles_mode = os.environ.get("PILOT_TITLES", "").strip() == "1"
    sample_mode = os.environ.get("PILOT_SAMPLE", "").strip() == "1"

    if titles_mode:
        flow_name = "pilot_titles"
    elif full_mode:
        flow_name = "pilot_full"
    elif sample_mode:
        flow_name = "pilot_sample"
    else:
        flow_name = "pilot_debug"

    with get_conn() as conn:
        if titles_mode:
            pending = fetch_title_segments(conn)
        elif full_mode:
            pending = fetch_pilot_segments(conn)
        elif sample_mode:
            pending = fetch_sample_segments(conn)
        else:
            pending = fetch_debug_segments(conn)

    if titles_mode:
        log.info(
            "Titles mode: %d pending title segments (full corpus). Prompt log → %s",
            len(pending),
            debug_log_path,
        )
    elif full_mode:
        log.info(
            "Full pilot: %d pending segments across %s. Prompt log → %s",
            len(pending),
            _PILOT_QUESTIONS,
            debug_log_path,
        )
    elif sample_mode:
        log.info(
            "Sample mode: %d pending segments from %s. Prompt log → %s",
            len(pending),
            _SAMPLE_FILE.name,
            debug_log_path,
        )
    else:
        log.info(
            "Debug pilot: %d segments from %s (limit %d). Prompt log → %s",
            len(pending),
            _DEBUG_QUESTION,
            _DEBUG_LIMIT,
            debug_log_path,
        )

    if not pending:
        log.info("No pending segments found — nothing to do.")
        _write_report(
            total_segments=0,
            translated=0,
            needs_human=0,
            iterations_list=[],
            stats_list=[],
            elapsed=time.time() - start,
        )
        return

    try:
        n_workers = int(os.environ.get("PILOT_WORKERS", str(_DEFAULT_WORKERS)))
    except ValueError:
        log.warning("PILOT_WORKERS is not a valid integer; defaulting to %d", _DEFAULT_WORKERS)
        n_workers = _DEFAULT_WORKERS
    log.info("Workers: %d", n_workers)

    run_id = _open_run(flow_name, None, None, n_workers)

    translated_count = 0
    needs_human_count = 0
    iterations_list: list[int] = []
    stats_list: list[SegmentStats] = []
    article_result = ArticleResult(locator=flow_name)

    def _translate_worker(
        seg: dict, pl: PromptLogger
    ) -> tuple[dict, str, list, object, dict]:
        """Translate one segment in its own DB connection."""
        with get_conn() as wconn:
            status, usages, outcome = translate_segment(seg["segment_id"], wconn, prompt_log=pl)
            lengths = fetch_segment_text_lengths(wconn, seg["segment_id"])
        return seg, status, usages, outcome, lengths

    completed = 0
    with PromptLogger(debug_log_path) as prompt_log, ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(_translate_worker, seg, prompt_log): seg for seg in pending}
        for fut in as_completed(futures):
            try:
                seg, status, usages, outcome, lengths = fut.result()
            except Exception as exc:
                orig_seg = futures[fut]
                log.error(
                    "segment_id=%d (%s) failed: %s — skipping",
                    orig_seg["segment_id"],
                    orig_seg["locator_path"],
                    exc,
                )
                completed += 1
                continue
            sid = seg["segment_id"]
            completed += 1

            iters = outcome.iterations_used
            iterations_list.append(iters)

            seg_stats = SegmentStats(
                segment_id=sid,
                usages=usages,
                latin_chars=lengths.get("la", 0),
                czech_chars=lengths.get("cs", 0),
                english_chars=lengths.get("en", 0),
            )
            stats_list.append(seg_stats)

            article_result.usages.extend(usages)
            if status == "translated":
                translated_count += 1
                article_result.translated += 1
            else:
                needs_human_count += 1
                article_result.needs_human += 1
            article_result.segment_records.append(
                {
                    "segment_id": sid,
                    "final_status": status,
                    "iterations_used": outcome.iterations_used,
                    "chosen_iteration": outcome.chosen_iteration,
                    "cost_usd": sum(u.cost_usd for u in usages),
                    "failure_classes": outcome.failure_classes or None,
                    "last_feedback": outcome.last_feedback,
                }
            )

            seg_cost = sum(u.cost_usd for u in usages)
            log.info(
                "[%d/%d] segment_id=%d  %s → %s (iter=%d, cost=$%.4f, running needs_human=%.1f%%)",
                completed,
                len(pending),
                sid,
                seg["locator_path"],
                status,
                iters,
                seg_cost,
                100.0 * needs_human_count / completed,
            )

    elapsed = time.time() - start
    _close_run(run_id, [article_result])

    total_run = len(pending)
    if titles_mode:
        report_name = "m4_titles.txt"
    elif sample_mode:
        report_name = "m4_sample.txt"
    else:
        report_name = "m4_pilot.txt"
    _write_report(
        total_segments=total_run,
        translated=translated_count,
        needs_human=needs_human_count,
        iterations_list=iterations_list,
        stats_list=stats_list,
        elapsed=elapsed,
        report_name=report_name,
        titles_mode=titles_mode,
        sample_mode=sample_mode,
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
    report_name: str = "m4_pilot.txt",
    titles_mode: bool = False,
    sample_mode: bool = False,
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
    if titles_mode:
        mode_label = "TITLES RUN SUMMARY"
        scope_line = "  Scope:             all question_title + article_title (full corpus)"
    elif sample_mode:
        mode_label = "SAMPLE RUN SUMMARY"
        scope_line = f"  Scope:             {_SAMPLE_FILE.name} (100-segment representative sample)"
    else:
        mode_label = "PILOT RUN SUMMARY"
        scope_line = f"  Pilot questions:   {', '.join(_PILOT_QUESTIONS)}"
    lines = [
        mode_label,
        scope_line,
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
    report_path = _REPORTS_DIR / report_name
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("Report written: %s", report_path)


if __name__ == "__main__":
    run_pilot()
