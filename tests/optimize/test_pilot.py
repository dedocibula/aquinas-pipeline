"""Tests for src/optimize/pilot.py — sample selector and report logic."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from common.pricing import UsageInfo
from optimize.pilot import (
    SegmentStats,
    _resolve_sample_file,
    _write_polish_report,
    _write_report,
    fetch_sample_segments,
    run_pilot,
)
from optimize.reset_golden import main as reset_main
from tests._fakes import FakeConn

# Silence the corpus-char DB query that _write_report issues. It's wrapped in
# try/except so the tests pass either way, but mocking avoids spurious
# connection-refused warnings in CI.
_PATCH_GET_CONN = "optimize.pilot.get_conn"

_SAMPLE_FILE = _resolve_sample_file()

# ── Fake DB helpers ───────────────────────────────────────────────────────────


def _fake_conn(rows=None):
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = rows if rows is not None else []
    cur.fetchone.return_value = rows[0] if rows else None
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def _fake_sample_file(ids):
    """Return a mock Path whose read_text() yields a sample JSON for the given ids."""
    sample = {"segments": [{"segment_id": i} for i in ids]}
    mock_file = MagicMock()
    mock_file.read_text.return_value = json.dumps(sample)
    return mock_file


# ── fetch_sample_segments ─────────────────────────────────────────────────────


def test_fetch_sample_segments_filters_pending():
    rows = [(1, "I.q1.a1", "pending"), (2, "I.q2.a1", "pending")]
    conn, cur = _fake_conn(rows)
    result = fetch_sample_segments(conn, _fake_sample_file([1, 2]))
    assert len(result) == 2
    assert result[0]["segment_id"] == 1
    assert result[0]["locator_path"] == "I.q1.a1"


def test_fetch_sample_segments_filters_by_status_and_text():
    conn, cur = _fake_conn([])
    fetch_sample_segments(conn, _fake_sample_file([1, 2]))
    sql = cur.execute.call_args[0][0]
    assert "pending" in sql
    assert "ANY(%s)" in sql
    assert "la" in sql and "en" in sql


def test_fetch_sample_segments_passes_sample_ids():
    conn, cur = _fake_conn([])
    fetch_sample_segments(conn, _fake_sample_file([7, 8, 9]))
    _, params = cur.execute.call_args[0]
    assert params == ([7, 8, 9],)


def test_fetch_sample_segments_returns_empty_when_none():
    conn, cur = _fake_conn([])
    result = fetch_sample_segments(conn, _fake_sample_file([1]))
    assert result == []


# ── run_pilot routing ─────────────────────────────────────────────────────────


def test_run_pilot_uses_fetch_sample_segments():
    """The pilot's only selector is the sample file; empty sample is a no-op."""
    with patch("optimize.pilot.fetch_sample_segments", return_value=[]) as mock_sample, \
         patch(_PATCH_GET_CONN), \
         patch("optimize.pilot._write_report") as mock_report:
        run_pilot()

    mock_sample.assert_called_once()
    mock_report.assert_called_once()


# ── _write_report ─────────────────────────────────────────────────────────────


def test_write_report_creates_file(tmp_path):
    with patch("optimize.pilot._REPORTS_DIR", tmp_path), patch(_PATCH_GET_CONN):
        _write_report(
            total_segments=50,
            translated=40,
            needs_human=5,
            iterations_list=[1, 1, 2, 1, 2, 2, 3, 1, 1, 2] + [1] * 35,
            stats_list=[],
            elapsed=125.0,
            sample_file=_SAMPLE_FILE,
        )
    assert (tmp_path / "m4_sample.txt").exists()


def test_write_report_contains_key_fields(tmp_path):
    with patch("optimize.pilot._REPORTS_DIR", tmp_path), patch(_PATCH_GET_CONN):
        _write_report(
            total_segments=50,
            translated=40,
            needs_human=5,
            iterations_list=[1] * 45,
            stats_list=[],
            elapsed=90.0,
            sample_file=_SAMPLE_FILE,
        )
    content = (tmp_path / "m4_sample.txt").read_text()
    assert "Translated:" in content
    assert "Needs human:" in content
    assert "Avg iterations:" in content
    assert "Time elapsed:" in content
    assert "Sample file:" in content


def test_write_report_abort_threshold_needs_human_ok(tmp_path):
    with patch("optimize.pilot._REPORTS_DIR", tmp_path), patch(_PATCH_GET_CONN):
        _write_report(
            total_segments=100,
            translated=90,
            needs_human=10,  # 10% — below 20% threshold
            iterations_list=[1] * 100,
            stats_list=[],
            elapsed=60.0,
            sample_file=_SAMPLE_FILE,
        )
    content = (tmp_path / "m4_sample.txt").read_text()
    assert "ok" in content


def test_write_report_abort_threshold_needs_human_triggered(tmp_path):
    with patch("optimize.pilot._REPORTS_DIR", tmp_path), patch(_PATCH_GET_CONN):
        _write_report(
            total_segments=100,
            translated=70,
            needs_human=30,  # 30% — above 20% threshold
            iterations_list=[3] * 100,
            stats_list=[],
            elapsed=60.0,
            sample_file=_SAMPLE_FILE,
        )
    content = (tmp_path / "m4_sample.txt").read_text()
    assert "TRIGGERED" in content


def test_write_report_avg_iterations(tmp_path):
    with patch("optimize.pilot._REPORTS_DIR", tmp_path), patch(_PATCH_GET_CONN):
        _write_report(
            total_segments=10,
            translated=8,
            needs_human=2,
            iterations_list=[1, 1, 2, 3, 1, 1, 1, 2, 3, 3],
            stats_list=[],
            elapsed=30.0,
            sample_file=_SAMPLE_FILE,
        )
    content = (tmp_path / "m4_sample.txt").read_text()
    # avg = 18/10 = 1.80
    assert "1.80" in content


def test_write_report_empty_run(tmp_path):
    """Should not crash when no segments were run."""
    with patch("optimize.pilot._REPORTS_DIR", tmp_path), patch(_PATCH_GET_CONN):
        _write_report(
            total_segments=50,
            translated=0,
            needs_human=0,
            iterations_list=[],
            stats_list=[],
            elapsed=5.0,
            sample_file=_SAMPLE_FILE,
        )
    assert (tmp_path / "m4_sample.txt").exists()


# ── _write_polish_report ──────────────────────────────────────────────────────


def _make_stats(sid: int, element_type: str, polish_status: str, guard_ok: bool) -> SegmentStats:
    flags = {
        "ok": guard_ok,
        "sentence_delta": 0,
        "term_retention_ok": guard_ok,
        "missing_terms": [] if guard_ok else ["ratio"],
        "particle_retention_ok": True,
        "missing_particles": [],
        "length_ratio": 1.0,
    }
    usage = UsageInfo(
        model="claude-sonnet-4-6",
        cache_hit_tokens=0,
        cache_miss_tokens=100,
        completion_tokens=80,
        cost_usd=0.002,
    )
    return SegmentStats(
        segment_id=sid,
        element_type=element_type,
        polish_status=polish_status,
        polish_usages=[usage],
        guard_flags=flags,
    )


def test_write_polish_report_creates_file(tmp_path):
    stats = [
        _make_stats(1, "arg1", "polished", True),
        _make_stats(2, "respondeo", "polished", True),
    ]
    with patch("optimize.pilot._REPORTS_DIR", tmp_path):
        _write_polish_report(stats_list=stats, sample_file=_SAMPLE_FILE)
    assert (tmp_path / "m5_polish_sample.txt").exists()


def test_write_polish_report_empty_stats(tmp_path):
    """No crashes when no segments were polished."""
    with patch("optimize.pilot._REPORTS_DIR", tmp_path):
        _write_polish_report(stats_list=[], sample_file=_SAMPLE_FILE)
    content = (tmp_path / "m5_polish_sample.txt").read_text()
    assert "M5 POLISH PASS SAMPLE REPORT" in content


def test_write_polish_report_guard_pass_rate(tmp_path):
    stats = [
        _make_stats(1, "arg1", "polished", True),
        _make_stats(2, "arg1", "polished", False),
        _make_stats(3, "respondeo", "polished", True),
    ]
    with patch("optimize.pilot._REPORTS_DIR", tmp_path):
        _write_polish_report(stats_list=stats, sample_file=_SAMPLE_FILE)
    content = (tmp_path / "m5_polish_sample.txt").read_text()
    # 2 of 3 passed
    assert "2/3" in content
    assert "66.7%" in content


def test_write_polish_report_skipped_and_errors(tmp_path):
    stats = [
        _make_stats(1, "arg1", "polished", True),
        SegmentStats(segment_id=2, element_type="arg2", polish_status="skipped"),
        SegmentStats(segment_id=3, element_type="body", polish_status="error"),
    ]
    with patch("optimize.pilot._REPORTS_DIR", tmp_path):
        _write_polish_report(stats_list=stats, sample_file=_SAMPLE_FILE)
    content = (tmp_path / "m5_polish_sample.txt").read_text()
    assert "Skipped (human):   1" in content
    assert "Errors:            1" in content


def test_write_polish_report_guard_failures_section(tmp_path):
    stats = [
        _make_stats(10, "arg1", "polished", False),  # guard fail
    ]
    with patch("optimize.pilot._REPORTS_DIR", tmp_path):
        _write_polish_report(stats_list=stats, sample_file=_SAMPLE_FILE)
    content = (tmp_path / "m5_polish_sample.txt").read_text()
    assert "GUARD FAILURES" in content
    assert "segment_id=10" in content


def test_write_polish_report_by_element_type(tmp_path):
    stats = [
        _make_stats(1, "arg1", "polished", True),
        _make_stats(2, "arg1", "polished", True),
        _make_stats(3, "respondeo", "polished", False),
    ]
    with patch("optimize.pilot._REPORTS_DIR", tmp_path):
        _write_polish_report(stats_list=stats, sample_file=_SAMPLE_FILE)
    content = (tmp_path / "m5_polish_sample.txt").read_text()
    assert "arg1" in content
    assert "respondeo" in content


def test_write_polish_report_cost(tmp_path):
    stats = [_make_stats(1, "arg1", "polished", True)]
    with patch("optimize.pilot._REPORTS_DIR", tmp_path):
        _write_polish_report(stats_list=stats, sample_file=_SAMPLE_FILE)
    content = (tmp_path / "m5_polish_sample.txt").read_text()
    assert "claude-sonnet-4-6" in content
    assert "0.0020" in content


# ── reset_golden DELETE targets only model/polish ─────────────────────────────


def test_reset_golden_delete_targets_model_and_polish_codes(tmp_path):
    """reset_golden must not delete (sk, human) rows."""
    # Write a minimal sample file
    sample_path = tmp_path / "sample.json"
    sample_path.write_text(json.dumps({"segments": [{"segment_id": 1}, {"segment_id": 2}]}))

    fake = FakeConn(fetchone_results=[], fetchall_rows=[])
    fake._cursor.rowcount = 2

    with patch("optimize.reset_golden._SAMPLE", sample_path), \
         patch("optimize.reset_golden.get_conn", return_value=fake):
        reset_main()

    sql_calls = [sql for sql, _ in fake.executed]
    delete_sqls = [s for s in sql_calls if "DELETE" in s.upper()]
    assert len(delete_sqls) == 1
    delete_sql = delete_sqls[0]
    # Must join source and restrict to model/polish — never deletes all sk rows
    assert "source" in delete_sql.lower()
    assert "model" in delete_sql
    assert "polish" in delete_sql
    assert "human" not in delete_sql
