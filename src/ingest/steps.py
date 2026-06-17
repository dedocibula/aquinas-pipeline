"""Ingest-stage pipeline steps.

Thin `PipelineStep` wrappers around the corpus-build modules. Each delegates to
the module that owns the work; the step exists so the interactive driver (and the
runner's timing/reporting) can invoke a stage uniformly. Individual modules remain
directly runnable via ``python -m ingest.<module>``.

Stages and their dependencies:
  latin    — parse all articles into segment + segment_text(la)
  bahounek — match Czech text to existing segments (requires: latin)
  english  — match English text to existing segments (requires: latin)
  resolve  — run term resolver, write term_usage (requires: latin + bahounek + english)
  report   — produce coverage report + dedup roll-up (requires: resolve)

`mine-senses` is exposed for the interactive driver but is not a corpus-build
stage: it spends API budget and feeds the review surface, not the corpus.
"""

from __future__ import annotations

import json

from pipeline import BaseStep, PipelineContext, StepResult


class LatinStep(BaseStep):
    name = "latin"
    stage = "ingest"

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
    stage = "ingest"

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
    stage = "ingest"

    def run(self, ctx: PipelineContext) -> StepResult:
        from ingest.ingest_english import run

        print("[english] Ingesting English text...")
        run()
        print("[english] Done.")
        return StepResult(name=self.name, ok=True, summary="English overlay ingested")


class ResolveStep(BaseStep):
    name = "resolve"
    stage = "resolve"

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
    stage = "ingest"

    def run(self, ctx: PipelineContext) -> StepResult:
        from ingest.coverage_report import run

        print("[report] Generating coverage report and dedup roll-up...")
        run()
        return StepResult(
            name=self.name, ok=True, summary="coverage report + dedup roll-up written"
        )


class MineSensesStep(BaseStep):
    """Mine polysemy candidates, label them via DeepSeek, write proposed senses.

    Not part of the linear corpus-build flow (it spends API budget and feeds the
    review surface, not the corpus build); it's exposed for the interactive
    driver. Mines every minable term, labels candidates, and writes the result
    as 'proposed' senses for review.
    """

    name = "mine-senses"
    stage = "resolve"

    def run(self, ctx: PipelineContext) -> StepResult:
        from ingest.sense_mining import run

        run(terms_filter=None, do_label=True, do_write=True)
        return StepResult(
            name=self.name, ok=True, summary="senses mined, labeled, written as proposed"
        )
