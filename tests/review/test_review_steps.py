"""Unit tests for the review-stage step wrappers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pipeline import PipelineContext
from review.steps import ExportReviewStep, ImportApprovalsStep


def _ctx(tmp_path: Path) -> PipelineContext:
    return PipelineContext(reports_dir=tmp_path)


def test_export_review_step_delegates(tmp_path):
    with patch("review.export_sheet.run") as run:
        result = ExportReviewStep().run(_ctx(tmp_path))
    run.assert_called_once_with()
    assert result.ok and result.name == "export-review"


def test_import_approvals_step_delegates(tmp_path):
    with patch("review.import_approvals.run") as run:
        result = ImportApprovalsStep().run(_ctx(tmp_path))
    run.assert_called_once_with()
    assert result.ok and result.name == "import-approvals"


def test_review_steps_declare_stage():
    assert ExportReviewStep.stage == "review"
    assert ImportApprovalsStep.stage == "review"
