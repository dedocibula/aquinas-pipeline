"""Review-stage pipeline steps.

Thin `PipelineStep` wrappers around the glossary review surface — export the
proposed/gap senses to Google Sheets, and import the human-approved rows back
(triggering segment-scoped re-runs). They hold no logic of their own; each
delegates to the module that owns the operation so the interactive driver and
any future automation share one implementation.
"""

from __future__ import annotations

from pipeline import BaseStep, PipelineContext, StepResult


class ExportReviewStep(BaseStep):
    name = "export-review"
    stage = "review"

    def run(self, ctx: PipelineContext) -> StepResult:
        from review.export_sheet import run

        run()
        return StepResult(
            name=self.name, ok=True, summary="glossary exported to review sheet"
        )


class ImportApprovalsStep(BaseStep):
    name = "import-approvals"
    stage = "review"

    def run(self, ctx: PipelineContext) -> StepResult:
        from review.import_approvals import run

        run()
        return StepResult(
            name=self.name, ok=True, summary="approved rows imported; stale set flagged"
        )
