"""Polish-stage pipeline steps.

Thin `PipelineStep` wrapper around the Anthropic Batch API polish pass in
``polish.batch``. Delegates all work to ``run_batch``; the step exists so the
interactive driver can invoke it uniformly with timing and reporting.
"""

from __future__ import annotations

from pipeline import BaseStep, PipelineContext, StepResult


class PolishCorpusStep(BaseStep):
    name = "polish-corpus"
    stage = "polish"

    def run(self, ctx: PipelineContext) -> StepResult:
        from polish.batch import run_batch

        stats = run_batch()
        summary = (
            f"polished={stats.polished} guard_failed={stats.guard_failed} "
            f"errored={stats.errored} cost=~${stats.cost_usd:.4f}"
        )
        ok = stats.errored == 0 or stats.polished > 0
        return StepResult(
            name=self.name,
            ok=ok,
            summary=summary,
            details={
                "polished": stats.polished,
                "guard_failed": stats.guard_failed,
                "errored": stats.errored,
                "no_source": stats.no_source,
                "cost_usd": stats.cost_usd,
            },
        )
