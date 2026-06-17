"""Unit tests for the ingest-stage step wrappers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ingest.steps import (
    BahounekStep,
    EnglishStep,
    LatinStep,
    MineSensesStep,
    ReportStep,
    ResolveStep,
)
from pipeline import PipelineContext


def _ctx(tmp_path: Path) -> PipelineContext:
    return PipelineContext(reports_dir=tmp_path)


def test_latin_step_persists_stats_and_delegates(tmp_path):
    stats = {"ingested": 2, "total": 2, "anomalies": 0}
    with patch("ingest.parser_latin.run_full", return_value=stats) as fn:
        result = LatinStep().run(_ctx(tmp_path))
    fn.assert_called_once()
    assert (tmp_path / "m2_latin_stats.json").exists()
    assert result.ok and result.name == "latin"


def test_bahounek_step_delegates(tmp_path):
    with patch("ingest.parser_bahounek.run") as fn:
        result = BahounekStep().run(_ctx(tmp_path))
    fn.assert_called_once()
    assert result.ok and result.name == "bahounek"


def test_english_step_delegates(tmp_path):
    with patch("ingest.ingest_english.run") as fn:
        result = EnglishStep().run(_ctx(tmp_path))
    fn.assert_called_once_with()
    assert result.ok and result.name == "english"


def test_resolve_step_passes_knobs(tmp_path):
    ctx = PipelineContext(reports_dir=tmp_path, knobs={"GAP_BATCH_SIZE": "25"})
    with patch("ingest.resolver.run") as fn:
        result = ResolveStep().run(ctx)
    _, kwargs = fn.call_args
    assert kwargs["batch_size"] == 25
    assert result.ok and result.name == "resolve"


def test_report_step_delegates(tmp_path):
    with patch("ingest.report_m2.run") as fn:
        result = ReportStep().run(_ctx(tmp_path))
    fn.assert_called_once_with()
    assert result.ok and result.name == "report"


def test_mine_senses_step_delegates(tmp_path):
    with patch("ingest.sense_mining.run") as fn:
        result = MineSensesStep().run(_ctx(tmp_path))
    fn.assert_called_once_with(terms_filter=None, do_label=True, do_write=True)
    assert result.ok and result.name == "mine-senses"


def test_ingest_steps_declare_stage():
    assert LatinStep.stage == "ingest"
    assert BahounekStep.stage == "ingest"
    assert EnglishStep.stage == "ingest"
    assert ReportStep.stage == "ingest"
    assert ResolveStep.stage == "resolve"
    assert MineSensesStep.stage == "resolve"
