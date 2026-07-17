"""Translate-stage pipeline steps.

Thin `PipelineStep` wrappers around the full-corpus translation flows in
``translate.run``. Each delegates to the flow function that owns the work; the
step exists so the interactive driver (and the runner's timing/reporting) can
invoke a flow uniformly. The work id comes from the context (default 1).

``TranslateCorpusStep`` runs the whole corpus; restricting to particular pars or
a question cap stays on the ``translate.run`` CLI, which is the right surface for
that kind of one-off filtering.

``RerunStaleStep`` and ``ResetCorpusStep`` are the only two steps that trigger
paid retranslation, so both are cost-gated: ``run()`` previews the cost, refuses
outright if ``AQUINAS_MAX_RUN_USD`` is set and exceeded, and otherwise asks for
an explicit y/N confirmation before invoking the flow. A non-yes answer is a
*successful* step (nothing was wrong, the owner just declined to spend) — never
a failure.
"""

from __future__ import annotations

import os

from pipeline import BaseStep, PipelineContext, StepResult


def _work_id(ctx: PipelineContext) -> int:
    return ctx.work_id if ctx.work_id is not None else 1


def _max_run_usd() -> float | None:
    raw = os.getenv("AQUINAS_MAX_RUN_USD", "").strip()
    return float(raw) if raw else None


def _confirm_and_spend(
    name: str,
    n_segments: int,
    est_cost: float,
    do_run,
    *,
    read,
) -> StepResult:
    """Shared cost-gate body for RerunStaleStep and ResetCorpusStep.

    Read-only preview already happened by the time this is called; this only
    decides whether to actually invoke the (paid) flow.
    """
    if n_segments == 0:
        return StepResult(name=name, ok=True, summary="nothing to restage")

    cap = _max_run_usd()
    if cap is not None and est_cost > cap:
        return StepResult(
            name=name,
            ok=False,
            summary=f"refused: est ${est_cost:.2f} exceeds AQUINAS_MAX_RUN_USD=${cap:.2f}",
        )

    answer = read(f"Restage {n_segments} segments, est ~${est_cost:.2f} — proceed? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        return StepResult(name=name, ok=True, summary="cancelled — no retranslation")

    do_run()
    return StepResult(
        name=name, ok=True, summary=f"restaged {n_segments} segments (~${est_cost:.2f})"
    )


class TranslateCorpusStep(BaseStep):
    name = "translate-corpus"
    stage = "translate"

    def run(self, ctx: PipelineContext) -> StepResult:
        from translate.run import translate_corpus

        translate_corpus(work_id=_work_id(ctx))
        return StepResult(name=self.name, ok=True, summary="corpus translation flow complete")


class RerunStaleStep(BaseStep):
    name = "rerun-stale"
    stage = "translate"

    def __init__(self, *, limit: int | None = None, read=input):
        self._limit = limit
        self._read = read

    def run(self, ctx: PipelineContext) -> StepResult:
        from translate.run import preview_stale_cost, rerun_stale

        work_id = _work_id(ctx)
        n, cost = preview_stale_cost(work_id, limit=self._limit)
        return _confirm_and_spend(
            self.name,
            n,
            cost,
            lambda: rerun_stale(work_id=work_id, limit=self._limit),
            read=self._read,
        )


class ResetCorpusStep(BaseStep):
    name = "reset-corpus"
    stage = "translate"

    def __init__(self, *, read=input):
        self._read = read

    def run(self, ctx: PipelineContext) -> StepResult:
        from translate.run import preview_reset_corpus_cost, reset_corpus

        work_id = _work_id(ctx)
        n, cost = preview_reset_corpus_cost(work_id)
        return _confirm_and_spend(
            self.name, n, cost, lambda: reset_corpus(work_id=work_id), read=self._read
        )
