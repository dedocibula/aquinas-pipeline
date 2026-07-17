"""Unit tests for the translate-stage step wrappers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pipeline import PipelineContext
from translate.steps import RerunStaleStep, ResetCorpusStep, TranslateCorpusStep


def _ctx(tmp_path: Path, work_id=None) -> PipelineContext:
    return PipelineContext(reports_dir=tmp_path, work_id=work_id)


def test_translate_corpus_step_defaults_work_id(tmp_path):
    with patch("translate.run.translate_corpus") as fn:
        result = TranslateCorpusStep().run(_ctx(tmp_path))
    fn.assert_called_once_with(work_id=1)
    assert result.ok and result.name == "translate-corpus"


def test_translate_corpus_step_uses_ctx_work_id(tmp_path):
    with patch("translate.run.translate_corpus") as fn:
        TranslateCorpusStep().run(_ctx(tmp_path, work_id=3))
    fn.assert_called_once_with(work_id=3)


def test_translate_steps_declare_stage():
    for step in (TranslateCorpusStep, RerunStaleStep, ResetCorpusStep):
        assert step.stage == "translate"


# ── RerunStaleStep.run() — cost preview + confirm gate ──────────────────────


def test_rerun_stale_step_nothing_stale_skips_confirm(tmp_path):
    with (
        patch("translate.run.preview_stale_cost", return_value=(0, 0.0)),
        patch("translate.run.rerun_stale") as fn,
    ):
        result = RerunStaleStep(read=lambda _: "y").run(_ctx(tmp_path))
    fn.assert_not_called()
    assert result.ok and "nothing to restage" in result.summary


def test_rerun_stale_step_confirmed_invokes_flow(tmp_path):
    with (
        patch("translate.run.preview_stale_cost", return_value=(5, 1.23)),
        patch("translate.run.rerun_stale") as fn,
    ):
        result = RerunStaleStep(read=lambda _: "y").run(_ctx(tmp_path))
    fn.assert_called_once_with(work_id=1, limit=None)
    assert result.ok and "restaged 5 segments" in result.summary


def test_rerun_stale_step_declined_cancels(tmp_path):
    with (
        patch("translate.run.preview_stale_cost", return_value=(5, 1.23)),
        patch("translate.run.rerun_stale") as fn,
    ):
        result = RerunStaleStep(read=lambda _: "n").run(_ctx(tmp_path))
    fn.assert_not_called()
    assert result.ok and "cancelled" in result.summary


def test_rerun_stale_step_max_run_usd_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("AQUINAS_MAX_RUN_USD", "1.00")
    with (
        patch("translate.run.preview_stale_cost", return_value=(5, 1.23)),
        patch("translate.run.rerun_stale") as fn,
    ):
        result = RerunStaleStep(read=lambda _: "y").run(_ctx(tmp_path))
    fn.assert_not_called()
    assert result.ok is False
    assert "AQUINAS_MAX_RUN_USD" in result.summary


def test_rerun_stale_step_max_run_usd_under_cap_proceeds(tmp_path, monkeypatch):
    monkeypatch.setenv("AQUINAS_MAX_RUN_USD", "5.00")
    with (
        patch("translate.run.preview_stale_cost", return_value=(5, 1.23)),
        patch("translate.run.rerun_stale") as fn,
    ):
        result = RerunStaleStep(read=lambda _: "y").run(_ctx(tmp_path))
    fn.assert_called_once_with(work_id=1, limit=None)
    assert result.ok


def test_rerun_stale_step_forwards_limit(tmp_path):
    with (
        patch("translate.run.preview_stale_cost", return_value=(5, 1.23)) as preview,
        patch("translate.run.rerun_stale") as fn,
    ):
        result = RerunStaleStep(limit=5, read=lambda _: "y").run(_ctx(tmp_path))
    preview.assert_called_once_with(1, limit=5)
    fn.assert_called_once_with(work_id=1, limit=5)
    assert result.ok


# ── ResetCorpusStep.run() — same gate shape ─────────────────────────────────


def test_reset_corpus_step_confirmed_invokes_flow(tmp_path):
    with (
        patch("translate.run.preview_reset_corpus_cost", return_value=(10, 4.5)),
        patch("translate.run.reset_corpus") as fn,
    ):
        result = ResetCorpusStep(read=lambda _: "yes").run(_ctx(tmp_path, work_id=2))
    fn.assert_called_once_with(work_id=2)
    assert result.ok and "restaged 10 segments" in result.summary


def test_reset_corpus_step_declined_cancels(tmp_path):
    with (
        patch("translate.run.preview_reset_corpus_cost", return_value=(10, 4.5)),
        patch("translate.run.reset_corpus") as fn,
    ):
        result = ResetCorpusStep(read=lambda _: "").run(_ctx(tmp_path))
    fn.assert_not_called()
    assert result.ok and "cancelled" in result.summary
