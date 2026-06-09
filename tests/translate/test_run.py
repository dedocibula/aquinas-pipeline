"""Tests for src/translate/run.py — Prefect flows and report helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from common.pricing import UsageInfo
from translate.run import (
    ArticleResult,
    _avg_iterations,
    _cache_hit_rate,
    _fetch_needs_human_rows,
    _total_cost,
    _write_needs_human_report,
    _write_production_report,
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
