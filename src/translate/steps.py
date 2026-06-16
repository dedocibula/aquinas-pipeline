"""Translate-stage pipeline steps.

Thin `PipelineStep` wrappers around the full-corpus translation flows in
``translate.run``. Each delegates to the flow function that owns the work; the
step exists so the interactive driver (and the runner's timing/reporting) can
invoke a flow uniformly. The work id comes from the context (default 1).

``TranslateCorpusStep`` runs the whole corpus; restricting to particular pars or
a question cap stays on the ``translate.run`` CLI, which is the right surface for
that kind of one-off filtering.
"""

from __future__ import annotations

from pipeline import BaseStep, PipelineContext, StepResult


def _work_id(ctx: PipelineContext) -> int:
    return ctx.work_id if ctx.work_id is not None else 1


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

    def run(self, ctx: PipelineContext) -> StepResult:
        from translate.run import rerun_stale

        rerun_stale(work_id=_work_id(ctx))
        return StepResult(name=self.name, ok=True, summary="stale segments re-translated")


class RetranslateBodyStep(BaseStep):
    name = "retranslate-body"
    stage = "translate"

    def run(self, ctx: PipelineContext) -> StepResult:
        from translate.run import retranslate_body

        retranslate_body(work_id=_work_id(ctx))
        return StepResult(name=self.name, ok=True, summary="body segments re-translated")
