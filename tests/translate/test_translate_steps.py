"""Unit tests for the translate-stage step wrappers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pipeline import PipelineContext
from translate.steps import RerunStaleStep, RetranslateBodyStep, TranslateCorpusStep


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


def test_rerun_stale_step_delegates(tmp_path):
    with patch("translate.run.rerun_stale") as fn:
        result = RerunStaleStep().run(_ctx(tmp_path))
    fn.assert_called_once_with(work_id=1)
    assert result.ok and result.name == "rerun-stale"


def test_retranslate_body_step_delegates(tmp_path):
    with patch("translate.run.retranslate_body") as fn:
        result = RetranslateBodyStep().run(_ctx(tmp_path))
    fn.assert_called_once_with(work_id=1)
    assert result.ok and result.name == "retranslate-body"


def test_translate_steps_declare_stage():
    for step in (TranslateCorpusStep, RerunStaleStep, RetranslateBodyStep):
        assert step.stage == "translate"
