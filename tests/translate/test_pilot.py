"""Tests for src/translate/pilot.py — sample selector and report logic."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from translate.pilot import (
    _write_report,
    fetch_sample_segments,
    run_pilot,
)

# Silence the corpus-char DB query that _write_report issues. It's wrapped in
# try/except so the tests pass either way, but mocking avoids spurious
# connection-refused warnings in CI.
_PATCH_GET_CONN = "translate.pilot.get_conn"

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


def _patch_sample(monkeypatch, ids):
    """Make fetch_sample_segments read a fixed list of segment_ids."""
    sample = {"segments": [{"segment_id": i} for i in ids]}
    monkeypatch.setattr(
        "translate.pilot._SAMPLE_FILE",
        MagicMock(read_text=lambda: json.dumps(sample), name="sample"),
    )


# ── fetch_sample_segments ─────────────────────────────────────────────────────


def test_fetch_sample_segments_filters_pending(monkeypatch):
    _patch_sample(monkeypatch, [1, 2])
    rows = [(1, "I.q1.a1", "pending"), (2, "I.q2.a1", "pending")]
    conn, cur = _fake_conn(rows)
    result = fetch_sample_segments(conn)
    assert len(result) == 2
    assert result[0]["segment_id"] == 1
    assert result[0]["locator_path"] == "I.q1.a1"


def test_fetch_sample_segments_filters_by_status_and_text(monkeypatch):
    _patch_sample(monkeypatch, [1, 2])
    conn, cur = _fake_conn([])
    fetch_sample_segments(conn)
    sql = cur.execute.call_args[0][0]
    assert "pending" in sql
    assert "ANY(%s)" in sql
    assert "la" in sql and "en" in sql


def test_fetch_sample_segments_passes_sample_ids(monkeypatch):
    _patch_sample(monkeypatch, [7, 8, 9])
    conn, cur = _fake_conn([])
    fetch_sample_segments(conn)
    _, params = cur.execute.call_args[0]
    assert params == ([7, 8, 9],)


def test_fetch_sample_segments_returns_empty_when_none(monkeypatch):
    _patch_sample(monkeypatch, [1])
    conn, cur = _fake_conn([])
    result = fetch_sample_segments(conn)
    assert result == []


# ── run_pilot routing ─────────────────────────────────────────────────────────


def test_run_pilot_uses_fetch_sample_segments():
    """The pilot's only selector is the sample file; empty sample is a no-op."""
    with patch("translate.pilot.fetch_sample_segments", return_value=[]) as mock_sample, \
         patch(_PATCH_GET_CONN), \
         patch("translate.pilot._write_report") as mock_report:
        run_pilot()

    mock_sample.assert_called_once()
    mock_report.assert_called_once()


# ── _write_report ─────────────────────────────────────────────────────────────


def test_write_report_creates_file(tmp_path):
    with patch("translate.pilot._REPORTS_DIR", tmp_path), patch(_PATCH_GET_CONN):
        _write_report(
            total_segments=50,
            translated=40,
            needs_human=5,
            iterations_list=[1, 1, 2, 1, 2, 2, 3, 1, 1, 2] + [1] * 35,
            stats_list=[],
            elapsed=125.0,
        )
    assert (tmp_path / "m4_sample.txt").exists()


def test_write_report_contains_key_fields(tmp_path):
    with patch("translate.pilot._REPORTS_DIR", tmp_path), patch(_PATCH_GET_CONN):
        _write_report(
            total_segments=50,
            translated=40,
            needs_human=5,
            iterations_list=[1] * 45,
            stats_list=[],
            elapsed=90.0,
        )
    content = (tmp_path / "m4_sample.txt").read_text()
    assert "Translated:" in content
    assert "Needs human:" in content
    assert "Avg iterations:" in content
    assert "Time elapsed:" in content
    assert "Sample file:" in content


def test_write_report_abort_threshold_needs_human_ok(tmp_path):
    with patch("translate.pilot._REPORTS_DIR", tmp_path), patch(_PATCH_GET_CONN):
        _write_report(
            total_segments=100,
            translated=90,
            needs_human=10,  # 10% — below 20% threshold
            iterations_list=[1] * 100,
            stats_list=[],
            elapsed=60.0,
        )
    content = (tmp_path / "m4_sample.txt").read_text()
    assert "ok" in content


def test_write_report_abort_threshold_needs_human_triggered(tmp_path):
    with patch("translate.pilot._REPORTS_DIR", tmp_path), patch(_PATCH_GET_CONN):
        _write_report(
            total_segments=100,
            translated=70,
            needs_human=30,  # 30% — above 20% threshold
            iterations_list=[3] * 100,
            stats_list=[],
            elapsed=60.0,
        )
    content = (tmp_path / "m4_sample.txt").read_text()
    assert "TRIGGERED" in content


def test_write_report_avg_iterations(tmp_path):
    with patch("translate.pilot._REPORTS_DIR", tmp_path), patch(_PATCH_GET_CONN):
        _write_report(
            total_segments=10,
            translated=8,
            needs_human=2,
            iterations_list=[1, 1, 2, 3, 1, 1, 1, 2, 3, 3],
            stats_list=[],
            elapsed=30.0,
        )
    content = (tmp_path / "m4_sample.txt").read_text()
    # avg = 18/10 = 1.80
    assert "1.80" in content


def test_write_report_empty_run(tmp_path):
    """Should not crash when no segments were run."""
    with patch("translate.pilot._REPORTS_DIR", tmp_path), patch(_PATCH_GET_CONN):
        _write_report(
            total_segments=50,
            translated=0,
            needs_human=0,
            iterations_list=[],
            stats_list=[],
            elapsed=5.0,
        )
    assert (tmp_path / "m4_sample.txt").exists()
