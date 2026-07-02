"""Polish-stage pipeline steps.

Two step implementations:
  PolishCorpusStep     — Anthropic Batch API (async, fire-and-forget, ~7× more expensive)
  PolishCorpusSyncStep — DeepSeek sync (default, crash-safe, commits per segment)
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


class PolishCorpusSyncStep(BaseStep):
    """Synchronous polish via DeepSeek (default) or Anthropic.

    Backend is controlled by the POLISH_BACKEND env var ('deepseek' or 'anthropic').
    Commits each segment immediately — a crash loses at most one in-flight segment.
    """

    name = "polish-corpus-sync"
    stage = "polish"

    def run(self, ctx: PipelineContext) -> StepResult:
        from polish.polisher import run_polish

        backend = ctx.knob("POLISH_BACKEND") or "deepseek"
        workers = ctx.knob_int("POLISH_WORKERS", 10)
        stats = run_polish(backend=backend, max_workers=workers)
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
