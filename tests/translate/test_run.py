"""Tests for src/translate/run.py — Prefect flows and report helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from common.pricing import UsageInfo
from translate.run import (
    ArticleResult,
    _avg_iterations,
    _cache_hit_rate,
    _fetch_needs_human_rows,
    _filter_locators,
    _total_cost,
    _write_needs_human_report,
    _write_production_report,
    rerun_stale,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _usage(
    cost: float, cache_miss: int = 100, cache_hit: int = 0, model: str = "deepseek-chat"
) -> UsageInfo:
    return UsageInfo(
        model=model,
        cache_hit_tokens=cache_hit,
        cache_miss_tokens=cache_miss,
        completion_tokens=10,
        cost_usd=cost,
    )


def _result(locator: str, translated: int = 1, needs_human: int = 0, usages=None) -> ArticleResult:
    return ArticleResult(
        locator=locator,
        translated=translated,
        needs_human=needs_human,
        usages=usages or [],
    )


# ── _filter_locators ──────────────────────────────────────────────────────────


def test_filter_locators_no_filter_returns_all():
    locs = ["I.q1.a1", "I.q21.a1", "I_II.q5.a2"]
    assert _filter_locators(locs, None, None) == locs


def test_filter_locators_pars_only():
    locs = ["I.q1.a1", "I_II.q1.a1", "II_II.q1.a1", "III.q1.a1"]
    result = _filter_locators(locs, ["I", "III"], None)
    assert result == ["I.q1.a1", "III.q1.a1"]


def test_filter_locators_max_question_only():
    locs = ["I.q20.a1", "I.q21.a1", "I.q100.a1"]
    assert _filter_locators(locs, None, 20) == ["I.q20.a1"]


def test_filter_locators_pars_and_max_question():
    locs = ["I.q1.a1", "I.q20.a1", "I.q21.a1", "I_II.q1.a1", "II_II.q1.a1", "III.q1.a1"]
    result = _filter_locators(locs, ["I", "I_II", "II_II", "III"], 20)
    assert "I.q21.a1" not in result
    assert "I.q1.a1" in result
    assert "I_II.q1.a1" in result


# ── ArticleResult ─────────────────────────────────────────────────────────────


def test_article_result_defaults():
    r = ArticleResult(locator="I.q1.a1")
    assert r.translated == 0
    assert r.needs_human == 0
    assert r.usages == []
    assert r.error is None


# ── _total_cost ───────────────────────────────────────────────────────────────


def test_total_cost_sums_usages():
    usages = [_usage(0.01), _usage(0.02), _usage(0.005)]
    assert abs(_total_cost(usages) - 0.035) < 1e-9


def test_total_cost_empty():
    assert _total_cost([]) == 0.0


# ── _cache_hit_rate ───────────────────────────────────────────────────────────


def test_cache_hit_rate_50_percent():
    usages = [_usage(0.01, cache_miss=50, cache_hit=50)]
    assert abs(_cache_hit_rate(usages) - 0.5) < 1e-9


def test_cache_hit_rate_zero_input():
    u = UsageInfo(
        model="m", cache_hit_tokens=0, cache_miss_tokens=0, completion_tokens=0, cost_usd=0.0
    )
    assert _cache_hit_rate([u]) == 0.0


# ── _avg_iterations ───────────────────────────────────────────────────────────


def test_avg_iterations_counts_translator_calls():
    # 2 segments, each with 1 translator call → avg = 1.0
    r = _result(
        "I.q1.a1",
        translated=2,
        usages=[_usage(0.01, model="deepseek-chat"), _usage(0.01, model="deepseek-chat")],
    )
    assert abs(_avg_iterations([r]) - 1.0) < 1e-9


def test_avg_iterations_zero_segments():
    r = _result("I.q1.a1", translated=0, needs_human=0)
    assert _avg_iterations([r]) == 0.0


# ── _fetch_needs_human_rows ───────────────────────────────────────────────────


def test_fetch_needs_human_rows_parses_notes():
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = [
        ("I.q1.a1.respondeo", {"iteration": 3, "last_feedback": "Missing term X"}),
        ("I.q2.a1.arg1", None),
    ]
    conn = MagicMock()
    conn.cursor.return_value = cur

    rows = _fetch_needs_human_rows(conn, work_id=1)
    assert len(rows) == 2
    assert rows[0]["locator_path"] == "I.q1.a1.respondeo"
    assert rows[0]["iteration"] == 3
    assert rows[0]["last_feedback"] == "Missing term X"
    assert rows[1]["iteration"] is None
    assert rows[1]["last_feedback"] is None
    # Verify work_id is passed to the query
    sql, params = cur.execute.call_args.args
    assert "work_id" in sql
    assert params == (1,)


# ── _write_production_report ──────────────────────────────────────────────────


def test_write_production_report_creates_file(tmp_path, monkeypatch):
    monkeypatch.setattr("translate.run._REPORTS_DIR", tmp_path)
    results = [
        _result("I.q1.a1", translated=5, usages=[_usage(0.02, cache_miss=100, cache_hit=100)]),
        _result("I.q1.a2", translated=3, needs_human=1),
    ]
    _write_production_report(results, elapsed=125.0)
    report = (tmp_path / "m5_production.txt").read_text()
    assert "FULL CORPUS RUN SUMMARY" in report
    assert "Total segments:    9" in report
    assert "Translated:        8" in report
    assert "Needs human:       1" in report
    assert "0h 2m" in report


def test_write_production_report_zero_segments(tmp_path, monkeypatch):
    monkeypatch.setattr("translate.run._REPORTS_DIR", tmp_path)
    _write_production_report([], elapsed=0.0)
    report = (tmp_path / "m5_production.txt").read_text()
    assert "FULL CORPUS RUN SUMMARY" in report


# ── _write_needs_human_report ─────────────────────────────────────────────────


def test_write_needs_human_report_skips_when_none(tmp_path, monkeypatch):
    monkeypatch.setattr("translate.run._REPORTS_DIR", tmp_path)
    _write_needs_human_report([_result("I.q1.a1", translated=1, needs_human=0)])
    assert not (tmp_path / "m5_needs_human.txt").exists()


def test_write_needs_human_report_creates_file(tmp_path, monkeypatch):
    monkeypatch.setattr("translate.run._REPORTS_DIR", tmp_path)

    mock_row = {
        "locator_path": "I.q1.a1.respondeo",
        "iteration": 3,
        "last_feedback": "Terminology miss",
    }
    with (
        patch("translate.run.get_conn"),
        patch("translate.run._fetch_needs_human_rows", return_value=[mock_row]),
    ):
        results = [_result("I.q1.a1", translated=0, needs_human=1)]
        _write_needs_human_report(results)

    report = (tmp_path / "m5_needs_human.txt").read_text()
    assert "NEEDS HUMAN TRIAGE" in report
    assert "I.q1.a1.respondeo" in report
    assert "Terminology miss" in report


# ── rerun_stale human-edit guard ──────────────────────────────────────────────
# rerun_stale.fn bypasses the Prefect engine so the flow body runs as plain Python.
# Stale/human-edit/flag/reset queries are SegmentRepository methods; patching them
# on the class means the mocks are called without `self`, so flag_needs_human args
# are (segment_ids, note) and reset_translation_status args are (segment_ids,).
_PATCH_STALE = "translate.run.SegmentRepository.get_stale_segments"
_PATCH_HUMAN_EDITED = "translate.run.SegmentRepository.get_human_edited_segments"
_PATCH_FLAG = "translate.run.SegmentRepository.flag_needs_human"
_PATCH_RESET = "translate.run.SegmentRepository.reset_translation_status"


def test_rerun_stale_guards_human_edited_segments():
    """Human-edited stale segments are flagged, the rest reset and re-translated."""
    with (
        patch("translate.run.get_conn"),
        patch(_PATCH_STALE, return_value=[1, 2, 3]),
        patch(_PATCH_HUMAN_EDITED, return_value=[2]),
        patch(_PATCH_FLAG) as mock_flag,
        patch(_PATCH_RESET) as mock_reset,
        patch("translate.run.translate_corpus") as mock_translate,
    ):
        rerun_stale.fn(work_id=1)

    flagged_ids = mock_flag.call_args.args[0]
    assert flagged_ids == [2]
    assert "human edit" in mock_flag.call_args.args[1]
    reset_ids = mock_reset.call_args.args[0]
    assert reset_ids == [1, 3]
    mock_translate.assert_called_once_with(1, flow_name="rerun_stale")


def test_rerun_stale_all_human_edited_skips_translation():
    """When every stale segment is human-edited, nothing is reset or re-translated."""
    with (
        patch("translate.run.get_conn"),
        patch(_PATCH_STALE, return_value=[2]),
        patch(_PATCH_HUMAN_EDITED, return_value=[2]),
        patch(_PATCH_FLAG) as mock_flag,
        patch(_PATCH_RESET) as mock_reset,
        patch("translate.run.translate_corpus") as mock_translate,
    ):
        rerun_stale.fn(work_id=1)

    mock_flag.assert_called_once()
    mock_reset.assert_not_called()
    mock_translate.assert_not_called()


def test_rerun_stale_no_stale_segments_noop():
    """No stale segments: no flagging, no reset, no translation."""
    with (
        patch("translate.run.get_conn"),
        patch(_PATCH_STALE, return_value=[]),
        patch(_PATCH_FLAG) as mock_flag,
        patch(_PATCH_RESET) as mock_reset,
        patch("translate.run.translate_corpus") as mock_translate,
    ):
        rerun_stale.fn(work_id=1)

    mock_flag.assert_not_called()
    mock_reset.assert_not_called()
    mock_translate.assert_not_called()


# ── run analytics: _open_run / _close_run / segment records ───────────────────


def test_prompt_hash_deterministic_and_sensitive(tmp_path, monkeypatch):
    (tmp_path / "translator_system.txt").write_text("A")
    (tmp_path / "reviewer_system.txt").write_text("B")
    monkeypatch.setattr("translate.run._PROMPTS_DIR", tmp_path)
    from translate.run import _prompt_hash

    h1 = _prompt_hash()
    assert h1 == _prompt_hash()
    (tmp_path / "translator_system.txt").write_text("A changed")
    assert _prompt_hash() != h1


def test_open_run_inserts_row_and_returns_id():
    """_open_run delegates to RunRepository and returns its run_id.

    The INSERT SQL itself is covered by tests/storage/test_run_repo.py.
    """
    from translate.run import _open_run

    with (
        patch("translate.run.get_conn"),
        patch("translate.run._git_sha", return_value="abc1234"),
        patch("translate.run._prompt_hash", return_value="deadbeef"),
        patch("translate.run.RunRepository") as mock_repo_cls,
    ):
        repo = mock_repo_cls.return_value
        repo.glossary_snapshot.return_value = {"approved_senses": 10}
        repo.open_run.return_value = 7
        run_id = _open_run("translate_corpus", ["I"], 20, max_workers=10)

    assert run_id == 7
    kwargs = repo.open_run.call_args.kwargs
    assert kwargs["flow_name"] == "translate_corpus"
    assert kwargs["git_sha"] == "abc1234"
    assert kwargs["prompt_hash"] == "deadbeef"
    assert kwargs["snapshot"] == {"approved_senses": 10}
    assert kwargs["filters"] == {"pars": ["I"], "max_question": 20}


def test_close_run_bulk_inserts_segments_and_totals():
    from translate.run import _close_run

    record = {
        "segment_id": 5,
        "final_status": "translated",
        "iterations_used": 1,
        "chosen_iteration": 1,
        "cost_usd": 0.001,
        "failure_classes": None,
        "last_feedback": None,
    }
    results = [
        _result("I.q1.a1", translated=1, usages=[_usage(0.001)]),
    ]
    results[0].segment_records.append(record)

    # _close_run delegates the run_segment/translation_run SQL to RunRepository,
    # so the execute_values call now lives in storage.repositories.
    with (
        patch("translate.run.get_conn") as mock_gc,
        patch("storage.repositories.psycopg2") as mock_psycopg2,
    ):
        conn = mock_gc.return_value.__enter__.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        _close_run(7, results)

    rows = mock_psycopg2.extras.execute_values.call_args.args[2]
    assert len(rows) == 1
    assert rows[0][0] == 7  # run_id
    assert rows[0][1] == 5  # segment_id
    sql, params = cur.execute.call_args.args
    assert "UPDATE translation_run" in sql
    assert params[0] == 1  # total_segments
    assert params[4] == 7  # run_id


def test_translate_article_task_builds_segment_records():
    from translate.loop import SegmentOutcome
    from translate.run import translate_article_task

    outcome = SegmentOutcome(
        segment_id=11,
        iterations_used=2,
        chosen_iteration=2,
        failure_classes=[{"iter": 1, "class": "precheck_terminology", "term": "rozum"}],
        last_feedback=None,
    )
    with (
        patch("translate.run.get_conn"),
        patch(
            "translate.run.SegmentRepository.get_pending_segment_ids_for_article",
            return_value=[11],
        ),
        patch(
            "translate.run.translate_segment",
            return_value=("translated", [_usage(0.002)], outcome),
        ),
    ):
        result = translate_article_task.fn("I.q1.a1", work_id=1)

    assert result.translated == 1
    rec = result.segment_records[0]
    assert rec["segment_id"] == 11
    assert rec["final_status"] == "translated"
    assert rec["iterations_used"] == 2
    assert abs(rec["cost_usd"] - 0.002) < 1e-9
    assert rec["failure_classes"][0]["term"] == "rozum"
