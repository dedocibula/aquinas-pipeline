"""M5 Prefect flows for full-corpus translation.

Usage
-----
# Full corpus (new segments):
    MAX_WORKERS=10 uv run python -m translate.run

# Subset run — first 20 questions of selected pars:
    MAX_WORKERS=10 uv run python -m translate.run --pars I I_II II_II III --max-questions 20

# After bumping glossary sense versions (re-translate stale segments):
    MAX_WORKERS=10 uv run python -m translate.run --flow rerun_stale

# Run a specific flow with args (Python API):
    from translate.run import translate_corpus, rerun_stale
    translate_corpus(work_id=1)
    translate_corpus(work_id=1, pars=["I", "I_II", "II_II", "III"], max_question=20)
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import psycopg2.extras
from prefect import flow, task
from prefect.task_runners import ThreadPoolTaskRunner

from common.corpus_db import (
    flag_needs_human,
    get_all_article_locators,
    get_human_edited_segments,
    get_pending_segment_ids_for_article,
    get_stale_segments,
    has_pending_segments,
    reset_translation_status,
)
from common.db import get_conn
from common.pricing import UsageInfo
from translate.loop import translate_segment

log = logging.getLogger(__name__)

_REPORTS_DIR = Path("reports")

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))


def _filter_locators(
    locators: list[str],
    pars_filter: list[str] | None,
    max_question: int | None,
) -> list[str]:
    """Filter article locators to a pars/question subset.

    locators are of the form 'I.q1.a1' or 'I.q1.question_title'.
    pars_filter: keep only these pars (e.g. ['I', 'I_II', 'II_II', 'III']).
    max_question: keep only questions whose number <= max_question.
    """
    if pars_filter is None and max_question is None:
        return locators
    result = []
    for loc in locators:
        parts = loc.split(".")
        pars = parts[0]
        if pars_filter and pars not in pars_filter:
            continue
        if max_question is not None:
            q_part = parts[1] if len(parts) > 1 else ""
            if q_part.startswith("q") and q_part[1:].isdigit():
                if int(q_part[1:]) > max_question:
                    continue
        result.append(loc)
    return result


# ── Dataclass ─────────────────────────────────────────────────────────────────


@dataclass
class ArticleResult:
    locator: str
    translated: int = 0
    needs_human: int = 0
    usages: list[UsageInfo] = field(default_factory=list)
    # One dict per segment, shaped for run_segment bulk insert (migration 005).
    segment_records: list[dict] = field(default_factory=list)
    error: str | None = None


# ── Run analytics (translation_run / run_segment, migration 005) ─────────────

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def _prompt_hash() -> str:
    """sha256 over both system prompts — any wording change yields a new hash."""
    h = hashlib.sha256()
    for name in ("translator_system.txt", "reviewer_system.txt"):
        h.update((_PROMPTS_DIR / name).read_bytes())
    return h.hexdigest()


def _glossary_snapshot(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FILTER (WHERE status = 'approved'), max(version) "
            "FROM glossary_sense"
        )
        approved, max_version = cur.fetchone()
    return {"approved_senses": approved, "max_version": max_version}


def _open_run(
    flow_name: str,
    pars: list[str] | None,
    max_question: int | None,
    max_workers: int,
) -> int:
    """Insert a translation_run row at flow start; return its run_id.

    finished_at stays NULL until _close_run — a crashed run is recognizable.
    """
    from translate.reviewer import _DEEPSEEK_R1_MODEL
    from translate.translator import _DEEPSEEK_MODEL, TRANSLATOR_TEMPERATURE

    filters = None
    if pars or max_question:
        filters = {"pars": pars, "max_question": max_question}

    with get_conn() as conn:
        snapshot = _glossary_snapshot(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO translation_run
                    (flow_name, git_sha, prompt_hash, glossary_snapshot,
                     translator_model, reviewer_model, temperature,
                     filters, max_workers)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING run_id
                """,
                (
                    flow_name,
                    _git_sha(),
                    _prompt_hash(),
                    psycopg2.extras.Json(snapshot),
                    _DEEPSEEK_MODEL,
                    _DEEPSEEK_R1_MODEL,
                    TRANSLATOR_TEMPERATURE,
                    psycopg2.extras.Json(filters) if filters else None,
                    max_workers,
                ),
            )
            run_id = cur.fetchone()[0]
    log.info("Opened translation_run %d (%s)", run_id, flow_name)
    return run_id


def _close_run(run_id: int, results: list[ArticleResult]) -> None:
    """Bulk-insert run_segment rows and finalize the translation_run totals."""
    records = [rec for r in results for rec in r.segment_records]
    translated = sum(r.translated for r in results)
    needs_human = sum(r.needs_human for r in results)
    cost = _total_cost([u for r in results for u in r.usages])

    with get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO run_segment
                    (run_id, segment_id, final_status, iterations_used,
                     chosen_iteration, cost_usd, failure_classes, last_feedback)
                VALUES %s
                """,
                [
                    (
                        run_id,
                        rec["segment_id"],
                        rec["final_status"],
                        rec["iterations_used"],
                        rec["chosen_iteration"],
                        rec["cost_usd"],
                        psycopg2.extras.Json(rec["failure_classes"])
                        if rec["failure_classes"]
                        else None,
                        rec["last_feedback"],
                    )
                    for rec in records
                ],
            )
            cur.execute(
                """
                UPDATE translation_run
                SET finished_at = now(),
                    total_segments = %s,
                    total_translated = %s,
                    total_needs_human = %s,
                    total_cost_usd = %s
                WHERE run_id = %s
                """,
                (len(records), translated, needs_human, cost, run_id),
            )
    log.info("Closed translation_run %d (%d segments)", run_id, len(records))


# ── Tasks ─────────────────────────────────────────────────────────────────────


@task(retries=3, retry_delay_seconds=30, name="translate-article")
def translate_article_task(locator_prefix: str, work_id: int = 1) -> ArticleResult:
    """Translate all pending segments under locator_prefix.

    One Prefect task = one article. Retried up to 3 times on API failure.
    Each retry re-fetches pending segments so idempotently skips any that
    succeeded in a prior attempt.
    """
    result = ArticleResult(locator=locator_prefix)
    with get_conn() as conn:
        segment_ids = get_pending_segment_ids_for_article(conn, locator_prefix, work_id)

    for seg_id in segment_ids:
        with get_conn() as conn:
            status, usages, outcome = translate_segment(seg_id, conn)
        result.usages.extend(usages)
        if status == "translated":
            result.translated += 1
        else:
            result.needs_human += 1
        result.segment_records.append(
            {
                "segment_id": seg_id,
                "final_status": status,
                "iterations_used": outcome.iterations_used,
                "chosen_iteration": outcome.chosen_iteration,
                "cost_usd": sum(u.cost_usd for u in usages),
                "failure_classes": outcome.failure_classes or None,
                "last_feedback": outcome.last_feedback,
            }
        )

    return result


# ── Flows ─────────────────────────────────────────────────────────────────────


@flow(
    name="translate-corpus",
    task_runner=ThreadPoolTaskRunner(max_workers=MAX_WORKERS),
)
def translate_corpus(
    work_id: int = 1,
    pars: list[str] | None = None,
    max_question: int | None = None,
    flow_name: str = "translate_corpus",
) -> None:
    """Translate all pending segments in the corpus (or a filtered subset).

    pars: restrict to these pars (e.g. ['I', 'I_II', 'II_II', 'III']).
    max_question: restrict to question numbers <= this value.
    flow_name: recorded in translation_run ('rerun_stale' when called from there).
    Safe to re-run: already-translated segments are skipped (status != 'pending').
    Every invocation opens a translation_run row; a NULL finished_at marks a crash.
    """
    t_start = time.monotonic()

    with get_conn() as conn:
        article_locators = get_all_article_locators(conn, work_id)
        article_locators = _filter_locators(article_locators, pars, max_question)
        pending = [a for a in article_locators if has_pending_segments(conn, a, work_id)]

    log.info(
        "Articles to translate: %d of %d (filtered) — pars=%s max_q=%s",
        len(pending),
        len(article_locators),
        pars,
        max_question,
    )

    run_id = _open_run(flow_name, pars, max_question, MAX_WORKERS)

    futures = [translate_article_task.submit(loc, work_id) for loc in pending]
    results: list[ArticleResult] = [f.result() for f in futures]

    elapsed = time.monotonic() - t_start
    _close_run(run_id, results)
    _write_production_report(results, elapsed)
    _write_needs_human_report(results, work_id)


@flow(name="rerun-stale")
def rerun_stale(work_id: int = 1) -> None:
    """Reset stale segments to pending, then re-translate.

    A segment is stale when any glossary sense it used has been updated
    (sense_version_used < current glossary_sense.version). This flow is run
    after import_approvals.py bumps sense versions following a review cycle.

    Human-edit guard: stale segments whose Slovak text was already edited by a
    human are NOT reset — re-translation would overwrite reviewed work. They
    are flagged needs_human with a note so a reviewer verifies the edit still
    holds under the updated term.
    """
    with get_conn() as conn:
        stale = get_stale_segments(conn, work_id)
        if not stale:
            log.info("No stale segments — nothing to do.")
            return

        human_edited = set(get_human_edited_segments(conn, stale))
        if human_edited:
            log.info(
                "Guarding %d human-edited stale segments (flagged needs_human, not reset)",
                len(human_edited),
            )
            flag_needs_human(
                conn,
                sorted(human_edited),
                "term updated after human edit — verify the edit still holds",
            )

        to_reset = [s for s in stale if s not in human_edited]
        if not to_reset:
            log.info("All stale segments are human-edited — nothing to re-translate.")
            return
        log.info("Resetting %d stale segments to pending", len(to_reset))
        reset_translation_status(conn, to_reset)

    translate_corpus(work_id, flow_name="rerun_stale")


# ── Report writers ────────────────────────────────────────────────────────────


def _total_cost(usages: list[UsageInfo]) -> float:
    return sum(u.cost_usd for u in usages)


def _cache_hit_rate(usages: list[UsageInfo]) -> float:
    # cache_hit_tokens = prompt tokens served from cache (zero cost)
    # cache_miss_tokens = prompt tokens that were not cached
    total_prompt = sum(u.cache_hit_tokens + u.cache_miss_tokens for u in usages)
    cached = sum(u.cache_hit_tokens for u in usages)
    return cached / total_prompt if total_prompt else 0.0


def _avg_iterations(results: list[ArticleResult]) -> float:
    total_segs = sum(r.translated + r.needs_human for r in results)
    # Approximate iterations by counting translator (deepseek-chat) API calls.
    # Each loop iteration calls the translator once, so calls / segments ≈ avg iterations.
    from translate.translator import _DEEPSEEK_MODEL  # avoid circular at module level

    translator_calls = sum(
        1 for r in results for u in r.usages if _DEEPSEEK_MODEL in (u.model or "")
    )
    return translator_calls / total_segs if total_segs else 0.0


def _write_production_report(results: list[ArticleResult], elapsed: float) -> None:
    all_usages = [u for r in results for u in r.usages]
    total = sum(r.translated + r.needs_human for r in results)
    translated = sum(r.translated for r in results)
    needs_human = sum(r.needs_human for r in results)
    cost = _total_cost(all_usages)
    hit_rate = _cache_hit_rate(all_usages)
    avg_iters = _avg_iterations(results)
    hours, rem = divmod(int(elapsed), 3600)
    mins, _ = divmod(rem, 60)

    lines = [
        "FULL CORPUS RUN SUMMARY",
        f"  Total segments:    {total}",
        f"  Translated:        {translated}  ({translated / total * 100:.1f}%)"
        if total
        else "  Translated:        0",
        f"  Needs human:       {needs_human}  ({needs_human / total * 100:.1f}%)"
        if total
        else "  Needs human:       0",
        f"  Avg iterations:    {avg_iters:.2f}",
        f"  Cache hit rate:    {hit_rate * 100:.1f}%",
        f"  API cost:          ~${cost:.2f}",
        f"  Wall time:         {hours}h {mins}m",
    ]

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _REPORTS_DIR / "m5_production.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("Production report: %s", path)
    print("\n".join(lines))


def _write_needs_human_report(results: list[ArticleResult], work_id: int = 1) -> None:
    """Write m5_needs_human.txt listing flagged segments for the theological editor."""
    if not any(r.needs_human > 0 for r in results):
        log.info("No needs_human segments — skipping triage report.")
        return

    with get_conn() as conn:
        rows = _fetch_needs_human_rows(conn, work_id)

    lines = [
        "NEEDS HUMAN TRIAGE",
        f"  Total flagged: {sum(r.needs_human for r in results)}",
        "",
        f"{'locator_path':<40} {'iters':>5}  last_reviewer_feedback",
        "-" * 100,
    ]
    for row in rows:
        locator = row["locator_path"]
        iters = row.get("iteration", "?")
        feedback = (row.get("last_feedback") or "").replace("\n", " ")[:80]
        lines.append(f"{locator:<40} {str(iters):>5}  {feedback}")

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _REPORTS_DIR / "m5_needs_human.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("Needs-human report: %s", path)


def _fetch_needs_human_rows(conn, work_id: int = 1) -> list[dict]:
    """Fetch all needs_human segments for work_id with their locator and reviewer notes."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                s.locator_path::text AS locator_path,
                s.reviewer_notes
            FROM segment s
            WHERE s.translation_status = 'needs_human'
              AND s.work_id = %s
            ORDER BY s.locator_path
            """,
            (work_id,),
        )
        rows = []
        for locator_path, reviewer_notes in cur.fetchall():
            notes = reviewer_notes or {}
            rows.append(
                {
                    "locator_path": locator_path,
                    "iteration": notes.get("iteration"),
                    "last_feedback": notes.get("last_feedback"),
                }
            )
        return rows


# ── Entry point ───────────────────────────────────────────────────────────────


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="M5 translation flows")
    parser.add_argument(
        "--flow",
        choices=["translate_corpus", "rerun_stale"],
        default="translate_corpus",
        help="Which flow to run (default: translate_corpus)",
    )
    parser.add_argument("--work-id", type=int, default=1)
    parser.add_argument(
        "--pars",
        nargs="+",
        metavar="PARS",
        help="Restrict to these pars (e.g. --pars I I_II II_II III)",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        metavar="N",
        help="Restrict to first N questions per pars",
    )
    args = parser.parse_args()

    if args.flow == "rerun_stale":
        rerun_stale(work_id=args.work_id)
    else:
        translate_corpus(
            work_id=args.work_id,
            pars=args.pars,
            max_question=args.max_questions,
        )
