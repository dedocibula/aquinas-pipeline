"""
M2 ingestion pipeline — single entry point.

Each step calls the existing module's run() function. Individual modules
remain directly runnable via `python -m ingest.<module>`.

Steps and their dependencies:
  latin   — parse all 2,663 articles into segment + segment_text(la)
  bahounek — match Czech text to existing segments (requires: latin)
  english  — match English text to existing segments (requires: latin)
  resolve  — run term resolver, write term_usage (requires: latin + bahounek + english)
  report   — produce m2_coverage.txt + m2_dedup_rollup.csv (requires: resolve)

Usage:
  uv run python -m ingest.pipeline --step latin
  uv run python -m ingest.pipeline --step bahounek
  uv run python -m ingest.pipeline --step english
  uv run python -m ingest.pipeline --step resolve
  uv run python -m ingest.pipeline --step report
  uv run python -m ingest.pipeline --all
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = ROOT / "reports"

_STEPS = ("latin", "bahounek", "english", "resolve", "report")


def _step_latin() -> None:
    from ingest.parser_latin import run_full
    anomaly_log = REPORTS_DIR / "m2_parser_anomalies.txt"
    print(f"[latin] Ingesting full Latin corpus → anomalies logged to {anomaly_log}")
    result = run_full(anomaly_log)
    # Persist stats so coverage report can compute correct article totals
    stats_path = REPORTS_DIR / "m2_latin_stats.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(json.dumps(result, indent=2))
    print(
        f"[latin] Done: {result['ingested']}/{result['total']} articles ingested, "
        f"{result['anomalies']} anomalies."
    )
    if result["anomalies"]:
        print(
            f"[latin] WARNING: Review {anomaly_log} before proceeding. "
            "Categorise anomalies by type and fix by category."
        )


def _step_bahounek() -> None:
    from ingest.parser_bahounek import run
    gap_log = REPORTS_DIR / "m2_bahounek_gaps.txt"
    gap_log.parent.mkdir(parents=True, exist_ok=True)
    print(f"[bahounek] Ingesting Czech text → gaps logged to {gap_log}")
    run(gap_log_path=gap_log)
    print("[bahounek] Done.")


def _step_english() -> None:
    from ingest.ingest_english import run
    print("[english] Ingesting English text...")
    run()
    print("[english] Done.")


def _step_resolve() -> None:
    import os

    from ingest.resolver import run

    freq_floor = int(os.environ.get("GAP_FREQ_FLOOR", "10"))
    batch_size = int(os.environ.get("GAP_BATCH_SIZE", "50"))
    max_workers = int(os.environ.get("GAP_MAX_WORKERS", "10"))

    print(
        f"[resolve] Running term resolver "
        f"(freq_floor={freq_floor}, batch_size={batch_size}, max_workers={max_workers})..."
    )
    run(freq_floor=freq_floor, batch_size=batch_size, max_workers=max_workers)
    print("[resolve] Done.")


def _step_pilot(top_n: int, batch_sizes: list[int]) -> None:
    import os

    from ingest.db import get_conn, work_id
    from ingest.resolver import (
        _load_glossary,
        _load_segments,
        _scan_gap_lemmas,
        pilot_batch_sizes,
    )

    freq_floor = int(os.environ.get("GAP_FREQ_FLOOR", "10"))

    print("[pilot] Loading segments and glossary...")
    with get_conn() as conn:
        wid = work_id(conn, "summa_articulus")
        multiword_terms, singleword_terms = _load_glossary(conn)
        segments = _load_segments(conn, wid)

    krystal_lemmas = (
        {t["latin_lemma"] for t in singleword_terms}
        | {t["latin_lemma"] for t in multiword_terms}
    )
    print(f"[pilot] Scanning gap lemmas (freq_floor={freq_floor})...")
    gap_data = _scan_gap_lemmas(segments, krystal_lemmas, freq_floor=freq_floor)
    print(f"[pilot] {len(gap_data)} qualifying gap lemmas found")

    pilot_batch_sizes(gap_data, top_n=top_n, batch_sizes=batch_sizes)
    print(
        "\n[pilot] Choose a batch size, then run the full resolve step:\n"
        "  GAP_BATCH_SIZE=<N> uv run python -m ingest.pipeline --step resolve"
    )


def _step_report() -> None:
    from ingest.report_m2 import run
    print("[report] Generating coverage report and dedup roll-up...")
    run()


_STEP_FNS: dict[str, callable] = {
    "latin": _step_latin,
    "bahounek": _step_bahounek,
    "english": _step_english,
    "resolve": _step_resolve,
    "report": _step_report,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="M2 ingestion pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--step",
        choices=_STEPS,
        help="Run a single pipeline step",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Run all steps in order (latin → bahounek + english → resolve → report)",
    )
    group.add_argument(
        "--pilot",
        type=int,
        metavar="N",
        help=(
            "Pilot mode: test multiple batch sizes on the top N gap lemmas "
            "(no DB writes). Use to find the cheapest batch size before a full run."
        ),
    )
    parser.add_argument(
        "--batch-sizes",
        default="10,25,50,100",
        metavar="S1,S2,...",
        help="Comma-separated batch sizes to compare in pilot mode (default: 10,25,50,100)",
    )
    args = parser.parse_args(argv)

    if args.pilot is not None:
        batch_sizes = [int(s.strip()) for s in args.batch_sizes.split(",") if s.strip()]
        t0 = time.monotonic()
        try:
            _step_pilot(args.pilot, batch_sizes)
        except Exception as exc:
            print(f"\nFAIL in pilot: {exc}", file=sys.stderr)
            return 1
        elapsed = time.monotonic() - t0
        print(f"[pilot] Completed in {elapsed:.1f}s")
        return 0

    steps_to_run = list(_STEPS) if args.all else [args.step]

    for step in steps_to_run:
        t0 = time.monotonic()
        print(f"\n{'=' * 60}")
        print(f"STEP: {step.upper()}")
        print(f"{'=' * 60}")
        try:
            _STEP_FNS[step]()
        except Exception as exc:
            print(f"\nFAIL in step '{step}': {exc}", file=sys.stderr)
            return 1
        elapsed = time.monotonic() - t0
        print(f"[{step}] Completed in {elapsed:.1f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
