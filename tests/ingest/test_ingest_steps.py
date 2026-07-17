"""Unit tests for the ingest-stage step wrappers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ingest.apply_new_terms import TermApplyResult
from ingest.steps import (
    ApplyNewTermsStep,
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
    with patch("ingest.coverage_report.run") as fn:
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
    assert ApplyNewTermsStep.stage == "resolve"


# ── ApplyNewTermsStep ────────────────────────────────────────────────────────


def test_apply_new_terms_nothing_to_apply(tmp_path):
    with (
        patch("storage.db.get_conn") as get_conn,
        patch("ingest.apply_new_terms.find_target_proposals", return_value=[]),
    ):
        get_conn.return_value.__enter__.return_value = object()
        result = ApplyNewTermsStep().run(_ctx(tmp_path))
    assert result.ok
    assert result.summary == "nothing to apply"


def test_apply_new_terms_resolves_but_no_segment_gained(tmp_path):
    targets = [{"proposal_id": 1, "latin_lemma": "ens", "term_id": 10, "sense_ids": [100]}]
    with (
        patch("storage.db.get_conn") as get_conn,
        patch("ingest.apply_new_terms.find_target_proposals", return_value=targets),
        patch(
            "ingest.apply_new_terms.resolve_and_diff",
            return_value=[
                TermApplyResult(
                    proposal_id=1, latin_lemma="ens", term_id=10, sense_ids=[100],
                    gained_segment_ids=[],
                )
            ],
        ),
        patch("ingest.apply_new_terms.sample_locators", return_value=[]),
    ):
        get_conn.return_value.__enter__.return_value = object()
        result = ApplyNewTermsStep().run(_ctx(tmp_path))
    assert result.ok
    assert "no already-translated segment gained a lock" in result.summary


def test_apply_new_terms_passes_ctx_work_id(tmp_path):
    targets = [{"proposal_id": 1, "latin_lemma": "ens", "term_id": 10, "sense_ids": [100]}]
    ctx = PipelineContext(reports_dir=tmp_path, work_id=2)
    with (
        patch("storage.db.get_conn") as get_conn,
        patch("ingest.apply_new_terms.find_target_proposals", return_value=targets),
        patch(
            "ingest.apply_new_terms.resolve_and_diff",
            return_value=[
                TermApplyResult(
                    proposal_id=1, latin_lemma="ens", term_id=10, sense_ids=[100],
                    gained_segment_ids=[],
                )
            ],
        ) as resolve_fn,
        patch("ingest.apply_new_terms.sample_locators", return_value=[]),
    ):
        get_conn.return_value.__enter__.return_value = object()
        ApplyNewTermsStep().run(ctx)
    resolve_fn.assert_called_once_with(targets, work_id=2)


def test_apply_new_terms_confirms_and_resets_gained_segments(tmp_path):
    targets = [{"proposal_id": 1, "latin_lemma": "ens", "term_id": 10, "sense_ids": [100]}]
    with (
        patch("storage.db.get_conn") as get_conn,
        patch("ingest.apply_new_terms.find_target_proposals", return_value=targets),
        patch(
            "ingest.apply_new_terms.resolve_and_diff",
            return_value=[
                TermApplyResult(
                    proposal_id=1, latin_lemma="ens", term_id=10, sense_ids=[100],
                    gained_segment_ids=[5],
                )
            ],
        ),
        patch("ingest.apply_new_terms.sample_locators", return_value=["I.q1.a1"]),
        patch("ingest.apply_new_terms.preview_gained_cost", return_value=(1, 0.05)),
        patch("ingest.apply_new_terms.apply_gained_segments") as apply_fn,
    ):
        get_conn.return_value.__enter__.return_value = object()
        result = ApplyNewTermsStep(read=lambda _: "y").run(_ctx(tmp_path))
    apply_fn.assert_called_once_with([5])
    assert result.ok
    assert "restaged 1 segments" in result.summary


def test_apply_new_terms_declined_cancels_without_reset(tmp_path):
    targets = [{"proposal_id": 1, "latin_lemma": "ens", "term_id": 10, "sense_ids": [100]}]
    with (
        patch("storage.db.get_conn") as get_conn,
        patch("ingest.apply_new_terms.find_target_proposals", return_value=targets),
        patch(
            "ingest.apply_new_terms.resolve_and_diff",
            return_value=[
                TermApplyResult(
                    proposal_id=1, latin_lemma="ens", term_id=10, sense_ids=[100],
                    gained_segment_ids=[5],
                )
            ],
        ),
        patch("ingest.apply_new_terms.sample_locators", return_value=["I.q1.a1"]),
        patch("ingest.apply_new_terms.preview_gained_cost", return_value=(1, 0.05)),
        patch("ingest.apply_new_terms.apply_gained_segments") as apply_fn,
    ):
        get_conn.return_value.__enter__.return_value = object()
        result = ApplyNewTermsStep(read=lambda _: "n").run(_ctx(tmp_path))
    apply_fn.assert_not_called()
    assert result.ok
    assert "cancelled" in result.summary
