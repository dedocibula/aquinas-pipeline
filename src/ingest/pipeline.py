"""
Ingestion pipeline — single entry point.

Each step is a `PipelineStep` wrapping the existing module's run() function and
returning a `StepResult`; the `Runner` drives them with uniform banners, timing,
and fail-loud / stop-on-failure semantics. Individual modules remain directly
runnable via `python -m ingest.<module>`.

`verify-sources` runs as prerequisite step 0 of a full run: if the source tree,
DB, or env is broken it fails, and stop-on-failure means no ingest step runs.

Steps and their dependencies:
  verify   — source acceptance checks (prerequisite for everything below)
  latin    — parse all articles into segment + segment_text(la)
  bahounek — match Czech text to existing segments (requires: latin)
  english  — match English text to existing segments (requires: latin)
  resolve  — run term resolver, write term_usage (requires: latin + bahounek + english)
  report   — produce coverage report + dedup roll-up (requires: resolve)

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
import os
import sys
import time
from pathlib import Path

from acquire.steps import VerifySourcesStep
from pipeline import BaseStep, PipelineContext, PipelineStep, Runner, StepResult

ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = ROOT / "reports"

# Env knobs the resolve step reads (passed through PipelineContext).
_KNOB_KEYS = (
    "GAP_FREQ_FLOOR",
    "GAP_BATCH_SIZE",
    "GAP_MAX_WORKERS",
    "GAP_FREQ_CEILING_PCT",
)


class LatinStep(BaseStep):
    name = "latin"

    def run(self, ctx: PipelineContext) -> StepResult:
        from ingest.parser_latin import run_full

        anomaly_log = ctx.reports_dir / "m2_parser_anomalies.txt"
        anomaly_log.parent.mkdir(parents=True, exist_ok=True)
        print(f"[latin] Ingesting full Latin corpus → anomalies logged to {anomaly_log}")
        result = run_full(anomaly_log)
        # Persist stats so the coverage report can compute correct article totals.
        stats_path = ctx.reports_dir / "m2_latin_stats.json"
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
        return StepResult(
            name=self.name,
            ok=True,
            summary=(
                f"{result['ingested']}/{result['total']} articles, "
                f"{result['anomalies']} anomalies"
            ),
            details=result,
        )


class BahounekStep(BaseStep):
    name = "bahounek"

    def run(self, ctx: PipelineContext) -> StepResult:
        from ingest.parser_bahounek import run

        gap_log = ctx.reports_dir / "m2_bahounek_gaps.txt"
        gap_log.parent.mkdir(parents=True, exist_ok=True)
        print(f"[bahounek] Ingesting Czech text → gaps logged to {gap_log}")
        run(gap_log_path=gap_log)
        print("[bahounek] Done.")
        return StepResult(name=self.name, ok=True, summary="Czech overlay ingested")


class EnglishStep(BaseStep):
    name = "english"

    def run(self, ctx: PipelineContext) -> StepResult:
        from ingest.ingest_english import run

        print("[english] Ingesting English text...")
        run()
        print("[english] Done.")
        return StepResult(name=self.name, ok=True, summary="English overlay ingested")


class ResolveStep(BaseStep):
    name = "resolve"

    def run(self, ctx: PipelineContext) -> StepResult:
        from ingest.resolver import run

        freq_floor = ctx.knob_int("GAP_FREQ_FLOOR", 10)
        batch_size = ctx.knob_int("GAP_BATCH_SIZE", 50)
        max_workers = ctx.knob_int("GAP_MAX_WORKERS", 10)
        freq_ceiling_pct = ctx.knob_float("GAP_FREQ_CEILING_PCT", 0.40)

        print(
            f"[resolve] Running term resolver "
            f"(freq_floor={freq_floor}, batch_size={batch_size}, max_workers={max_workers}, "
            f"freq_ceiling_pct={freq_ceiling_pct})..."
        )
        run(freq_floor=freq_floor, batch_size=batch_size, max_workers=max_workers,
            freq_ceiling_pct=freq_ceiling_pct)
        print("[resolve] Done.")
        return StepResult(
            name=self.name,
            ok=True,
            summary=f"resolver complete (batch_size={batch_size})",
        )


class ReportStep(BaseStep):
    name = "report"

    def run(self, ctx: PipelineContext) -> StepResult:
        from ingest.report_m2 import run

        print("[report] Generating coverage report and dedup roll-up...")
        run()
        return StepResult(
            name=self.name, ok=True, summary="coverage report + dedup roll-up written"
        )


def _build_steps() -> dict[str, PipelineStep]:
    """The selectable steps, keyed by CLI token (also their run order)."""
    return {
        "verify": VerifySourcesStep(),
        "latin": LatinStep(),
        "bahounek": BahounekStep(),
        "english": EnglishStep(),
        "resolve": ResolveStep(),
        "report": ReportStep(),
    }


# CLI tokens (order is the --all run order: verify first as prerequisite step 0).
_STEPS = ("verify", "latin", "bahounek", "english", "resolve", "report")


def _context() -> PipelineContext:
    knobs = {k: os.environ[k] for k in _KNOB_KEYS if k in os.environ}
    return PipelineContext(reports_dir=REPORTS_DIR, knobs=knobs)


def _step_pilot(top_n: int, batch_sizes: list[int]) -> None:
    from ingest.gap_terms import _load_ignored_lemmas, _scan_gap_lemmas, pilot_batch_sizes
    from storage.db import get_conn, work_id
    from storage.repositories import GlossaryRepository, SegmentRepository

    freq_floor = int(os.environ.get("GAP_FREQ_FLOOR", "10"))
    freq_ceiling_pct = float(os.environ.get("GAP_FREQ_CEILING_PCT", "0.40"))

    print("[pilot] Loading segments and glossary...")
    with get_conn() as conn:
        wid = work_id(conn, "summa_articulus")
        multiword_terms, singleword_terms = GlossaryRepository(conn).load_glossary()
        segments = SegmentRepository(conn).load_body_segments(wid)
        ignored_lemmas = _load_ignored_lemmas(conn)

    krystal_lemmas = (
        {t.latin_lemma for t in singleword_terms}
        | {t.latin_lemma for t in multiword_terms}
    )
    print(f"[pilot] Scanning gap lemmas (freq_floor={freq_floor}, "
          f"freq_ceiling_pct={freq_ceiling_pct}, ignored={len(ignored_lemmas)})...")
    gap_data = _scan_gap_lemmas(
        segments, krystal_lemmas, freq_floor=freq_floor,
        freq_ceiling_pct=freq_ceiling_pct, ignored_lemmas=ignored_lemmas,
    )
    print(f"[pilot] {len(gap_data)} qualifying gap lemmas found")

    pilot_batch_sizes(gap_data, top_n=top_n, batch_sizes=batch_sizes)
    print(
        "\n[pilot] Choose a batch size, then run the full resolve step:\n"
        "  GAP_BATCH_SIZE=<N> uv run python -m ingest.pipeline --step resolve"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingestion pipeline",
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
        help="Run all steps in order (verify → latin → bahounek → english → resolve → report)",
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

    registry = _build_steps()
    tokens = list(_STEPS) if args.all else [args.step]
    steps = [registry[token] for token in tokens]

    results = Runner(_context()).run(steps)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
