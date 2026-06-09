"""M5 Prefect flows for full-corpus translation.

Usage
-----
# Full corpus (new segments):
    MAX_WORKERS=10 uv run python -m translate.run

# After bumping glossary sense versions (re-translate stale segments):
    MAX_WORKERS=10 uv run python -m translate.run --flow rerun_stale

# Run a specific flow with args (Python API):
    from translate.run import translate_corpus, rerun_stale
    translate_corpus(work_id=1)
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from prefect import flow, task
from prefect.task_runners import ThreadPoolTaskRunner

from common.corpus_db import (
    get_all_article_locators,
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


# ── Dataclass ─────────────────────────────────────────────────────────────────


@dataclass
class ArticleResult:
    locator: str
    translated: int = 0
    needs_human: int = 0
    usages: list[UsageInfo] = field(default_factory=list)
    error: str | None = None


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
            status, usages = translate_segment(seg_id, conn)
        result.usages.extend(usages)
        if status == "translated":
            result.translated += 1
        else:
            result.needs_human += 1

    return result


# ── Flows ─────────────────────────────────────────────────────────────────────


@flow(
    name="translate-corpus",
    task_runner=ThreadPoolTaskRunner(max_workers=MAX_WORKERS),
)
def translate_corpus(work_id: int = 1) -> None:
    """Translate all pending segments in the corpus.

    Submits one Prefect task per article, run concurrently via ThreadPool.
    Safe to re-run: already-translated segments are skipped (status != 'pending').
    """
    t_start = time.monotonic()

    with get_conn() as conn:
        article_locators = get_all_article_locators(conn, work_id)
        pending = [a for a in article_locators if has_pending_segments(conn, a, work_id)]

    log.info("Articles to translate: %d of %d total", len(pending), len(article_locators))

    futures = [translate_article_task.submit(loc, work_id) for loc in pending]
    results: list[ArticleResult] = [f.result() for f in futures]

    elapsed = time.monotonic() - t_start
    _write_production_report(results, elapsed)
    _write_needs_human_report(results, work_id)


@flow(name="rerun-stale")
def rerun_stale(work_id: int = 1) -> None:
    """Reset stale segments to pending, then re-translate.

    A segment is stale when any glossary sense it used has been updated
    (sense_version_used < current glossary_sense.version). This flow is run
    after import_approvals.py bumps sense versions following a review cycle.
    """
    with get_conn() as conn:
        stale = get_stale_segments(conn, work_id)
        if not stale:
            log.info("No stale segments — nothing to do.")
            return
        log.info("Resetting %d stale segments to pending", len(stale))
        reset_translation_status(conn, stale)

    translate_corpus(work_id)


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
        f"  Translated:        {translated}  ({translated / total * 100:.1f}%)" if total else "  Translated:        0",
        f"  Needs human:       {needs_human}  ({needs_human / total * 100:.1f}%)" if total else "  Needs human:       0",
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
    args = parser.parse_args()

    if args.flow == "rerun_stale":
        rerun_stale(work_id=args.work_id)
    else:
        translate_corpus(work_id=args.work_id)
