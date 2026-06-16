"""Executes an ordered sequence of steps against one context.

Lifts the timing/logging/fail-loud loop that ``ingest/pipeline.py`` grew by
hand so every stage gets identical behaviour:

  - a banner per step,
  - an optional ``verify(ctx)`` precondition checked before ``run`` — a step
    that declares one (e.g. "sources are present and the DB is reachable")
    that returns False is turned into a failed `StepResult` and never runs,
  - wall-clock timing,
  - an uncaught exception is reported and turned into a failed `StepResult`
    (fail loud, but don't let one step's traceback swallow the run summary),
  - stop on the first failure by default.

The runner returns the list of `StepResult`s it produced; CLIs map that to an
exit code.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import Sequence

from pipeline.context import PipelineContext
from pipeline.reporting import StepReport
from pipeline.step import PipelineStep, StepResult


class Runner:
    def __init__(self, ctx: PipelineContext, *, out=None, err=None) -> None:
        self.ctx = ctx
        self._out = out if out is not None else sys.stdout
        self._err = err if err is not None else sys.stderr

    def run(
        self,
        steps: Sequence[PipelineStep],
        *,
        stop_on_failure: bool = True,
    ) -> list[StepResult]:
        results: list[StepResult] = []
        for step in steps:
            result = self._run_one(step)
            results.append(result)
            if not result.ok and stop_on_failure:
                break
        return results

    def _run_one(self, step: PipelineStep) -> StepResult:
        name = step.name
        print(f"\n{'=' * 60}", file=self._out)
        print(f"STEP: {name.upper()}", file=self._out)
        print(f"{'=' * 60}", file=self._out)

        started_at = datetime.now()
        t0 = time.monotonic()
        result = self._execute(step)
        elapsed = time.monotonic() - t0

        status = "ok" if result.ok else "FAILED"
        suffix = f" — {result.summary}" if result.summary else ""
        print(f"[{name}] {status} in {elapsed:.1f}s{suffix}", file=self._out)

        # Persist a concise per-step report when the step declares its stage
        # folder. Bare Protocol/test steps without a `stage` are unaffected.
        stage = getattr(step, "stage", None)
        if stage:
            StepReport(
                stage=stage, result=result, started_at=started_at, elapsed_s=elapsed
            ).write(self.ctx.stage_reports_dir(stage))
        return result

    def _execute(self, step: PipelineStep) -> StepResult:
        """Run a step's precondition + body, turning any failure into a StepResult."""
        name = step.name

        # Precondition gate. A step may declare verify(ctx) -> bool (BaseStep
        # defaults it to True). A False precondition is a failure, so under the
        # default stop-on-failure the rest of the run refuses to proceed.
        verify = getattr(step, "verify", None)
        if verify is not None:
            try:
                ok = verify(self.ctx)
            except Exception as exc:  # fail loud
                print(f"\nFAIL in step '{name}' precondition: {exc}", file=self._err)
                return StepResult(name=name, ok=False, summary=f"precondition error: {exc}")
            if not ok:
                print(f"\nFAIL in step '{name}': precondition not met", file=self._err)
                return StepResult(name=name, ok=False, summary="precondition not met")

        try:
            return step.run(self.ctx)
        except Exception as exc:  # fail loud, but keep the run summary intact
            print(f"\nFAIL in step '{name}': {exc}", file=self._err)
            return StepResult(name=name, ok=False, summary=str(exc))
