"""Compare two translation runs for regressions and improvements.

Reads translation_run + run_segment (migration 005) and reports:
  - per-run summary (totals, cost, avg iterations, code/prompt/glossary state)
  - segment status flips (improved: needs_human → translated; regressed: reverse)
  - failure-class deltas (e.g. did precheck_terminology failures go down?)

Usage:
    uv run python -m translate.run_compare <run_a> <run_b>

run_a is the baseline (older), run_b the candidate (newer). Only segments
present in BOTH runs are compared for flips — a subset run against a full run
compares the intersection. Output goes to reports/run_compare_<a>_<b>.txt and
stdout. Deep dives (full prompts/drafts) live in the PromptLogger JSONL
referenced by translation_run.jsonl_path.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from common.db import get_conn

_REPORTS_DIR = Path("reports")


def fetch_run_summary(conn, run_id: int) -> dict:
    """Return the translation_run row plus derived avg iterations, or raise."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.flow_name, r.started_at, r.finished_at, r.git_sha,
                   r.prompt_hash, r.glossary_snapshot, r.translator_model,
                   r.reviewer_model, r.filters, r.total_segments,
                   r.total_translated, r.total_needs_human, r.total_cost_usd,
                   avg(s.iterations_used) AS avg_iterations
            FROM translation_run r
            LEFT JOIN run_segment s USING (run_id)
            WHERE r.run_id = %s
            GROUP BY r.run_id
            """,
            (run_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"run_id={run_id} not found in translation_run")
    keys = (
        "flow_name", "started_at", "finished_at", "git_sha", "prompt_hash",
        "glossary_snapshot", "translator_model", "reviewer_model", "filters",
        "total_segments", "total_translated", "total_needs_human",
        "total_cost_usd", "avg_iterations",
    )
    return dict(zip(keys, row))


def fetch_status_flips(conn, run_a: int, run_b: int) -> list[tuple]:
    """Return (segment_id, locator_path, status_a, status_b) where status changed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.segment_id, seg.locator_path::text,
                   a.final_status, b.final_status
            FROM run_segment a
            JOIN run_segment b USING (segment_id)
            JOIN segment seg USING (segment_id)
            WHERE a.run_id = %s
              AND b.run_id = %s
              AND a.final_status <> b.final_status
            ORDER BY seg.locator_path
            """,
            (run_a, run_b),
        )
        return cur.fetchall()


def fetch_failure_class_counts(conn, run_id: int) -> Counter:
    """Count failure_classes entries by class (terminology keyed by term too)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fc->>'class' AS cls, fc->>'term' AS term, count(*)
            FROM run_segment, jsonb_array_elements(failure_classes) AS fc
            WHERE run_id = %s
            GROUP BY 1, 2
            """,
            (run_id,),
        )
        counts: Counter = Counter()
        for cls, term, n in cur.fetchall():
            key = f"{cls}({term})" if term else cls
            counts[key] = n
        return counts


def _format_summary(label: str, run_id: int, s: dict) -> list[str]:
    snapshot = s["glossary_snapshot"] or {}
    cost = s["total_cost_usd"]
    avg_iters = s["avg_iterations"]
    return [
        f"RUN {label} (run_id={run_id}, {s['flow_name']})",
        f"  started:      {s['started_at']}",
        f"  finished:     {s['finished_at'] or 'NOT FINISHED (crashed?)'}",
        f"  git sha:      {s['git_sha']}",
        f"  prompt hash:  {(s['prompt_hash'] or '')[:12]}",
        f"  glossary:     {snapshot.get('approved_senses')} approved senses"
        f" (max version {snapshot.get('max_version')})",
        f"  filters:      {s['filters']}",
        f"  segments:     {s['total_segments']}"
        f" ({s['total_translated']} translated, {s['total_needs_human']} needs_human)",
        f"  avg iters:    {float(avg_iters):.2f}" if avg_iters is not None else "  avg iters:    n/a",
        f"  cost:         ${float(cost):.2f}" if cost is not None else "  cost:         n/a",
    ]


def build_report(conn, run_a: int, run_b: int) -> str:
    summary_a = fetch_run_summary(conn, run_a)
    summary_b = fetch_run_summary(conn, run_b)
    flips = fetch_status_flips(conn, run_a, run_b)
    fails_a = fetch_failure_class_counts(conn, run_a)
    fails_b = fetch_failure_class_counts(conn, run_b)

    improved = [(sid, loc) for sid, loc, sa, sb in flips if sb == "translated"]
    regressed = [(sid, loc) for sid, loc, sa, sb in flips if sb == "needs_human"]

    lines: list[str] = [f"RUN COMPARISON: {run_a} (baseline) → {run_b} (candidate)", ""]
    lines += _format_summary("A", run_a, summary_a)
    lines.append("")
    lines += _format_summary("B", run_b, summary_b)

    if summary_a["prompt_hash"] != summary_b["prompt_hash"]:
        lines.append("\n  NOTE: prompt hash differs — prompt change between runs.")
    if summary_a["git_sha"] != summary_b["git_sha"]:
        lines.append("  NOTE: git sha differs — code change between runs.")

    lines += ["", f"STATUS FLIPS (segments in both runs): {len(flips)}"]
    lines.append(f"  improved (needs_human → translated): {len(improved)}")
    for _, loc in improved:
        lines.append(f"    + {loc}")
    lines.append(f"  regressed (translated → needs_human): {len(regressed)}")
    for _, loc in regressed:
        lines.append(f"    - {loc}")

    lines += ["", "FAILURE CLASS DELTAS (count in A → count in B):"]
    for key in sorted(set(fails_a) | set(fails_b)):
        a, b = fails_a.get(key, 0), fails_b.get(key, 0)
        marker = "▼" if b < a else ("▲" if b > a else " ")
        lines.append(f"  {marker} {key:<45} {a:>4} → {b:<4}")
    if not fails_a and not fails_b:
        lines.append("  (no failures recorded in either run)")

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two translation runs")
    parser.add_argument("run_a", type=int, help="baseline run_id (older)")
    parser.add_argument("run_b", type=int, help="candidate run_id (newer)")
    args = parser.parse_args()

    with get_conn() as conn:
        report = build_report(conn, args.run_a, args.run_b)

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _REPORTS_DIR / f"run_compare_{args.run_a}_{args.run_b}.txt"
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"Report written to {path}")


if __name__ == "__main__":
    main()
