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
    from ingest.resolver import run
    print("[resolve] Running term resolver (DeepSeek V3 for model_proposed terms)...")
    run()
    print("[resolve] Done.")


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
    args = parser.parse_args(argv)

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
