"""Tests for src/translate/pilot.py — DB helpers and report logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from translate.pilot import (
    _PILOT_QUESTIONS,
    _iteration_count,
    _write_report,
    fetch_all_pilot_segments,
    fetch_pilot_segments,
    fetch_reviewer_notes,
)

# Silence the corpus-char DB query that _write_report now issues.
# It's wrapped in try/except so the tests pass either way, but mocking
# avoids spurious connection-refused warnings in CI.
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


# ── fetch_pilot_segments ──────────────────────────────────────────────────────


def test_fetch_pilot_segments_filters_pending():
    rows = [(1, "I.q1.a1", "pending"), (2, "I.q2.a1", "pending")]
    conn, cur = _fake_conn(rows)
    result = fetch_pilot_segments(conn)
    assert len(result) == 2
    assert result[0]["segment_id"] == 1
    assert result[0]["locator_path"] == "I.q1.a1"


def test_fetch_pilot_segments_sql_uses_ltree_operator():
    conn, cur = _fake_conn([])
    fetch_pilot_segments(conn)
    sql = cur.execute.call_args[0][0]
    assert "<@" in sql
    assert "pending" in sql


def test_fetch_pilot_segments_passes_all_pilot_questions():
    conn, cur = _fake_conn([])
    fetch_pilot_segments(conn)
    _, params = cur.execute.call_args[0]
    for q in _PILOT_QUESTIONS:
        assert q in params


def test_fetch_pilot_segments_returns_empty_when_none():
    conn, cur = _fake_conn([])
    result = fetch_pilot_segments(conn)
    assert result == []


# ── fetch_all_pilot_segments ──────────────────────────────────────────────────


def test_fetch_all_pilot_segments_returns_all_statuses():
    rows = [(1, "translated"), (2, "pending"), (3, "needs_human")]
    conn, cur = _fake_conn(rows)
    result = fetch_all_pilot_segments(conn)
    assert len(result) == 3
    statuses = {r["status"] for r in result}
    assert "translated" in statuses
    assert "pending" in statuses


def test_fetch_all_pilot_segments_no_pending_filter():
    conn, cur = _fake_conn([])
    fetch_all_pilot_segments(conn)
    sql = cur.execute.call_args[0][0]
    assert "pending" not in sql


# ── PILOT_FULL mode switch ────────────────────────────────────────────────────


def test_run_pilot_full_mode_calls_fetch_pilot_segments(monkeypatch):
    """PILOT_FULL=1 must route to fetch_pilot_segments, not fetch_debug_segments."""
    monkeypatch.setenv("PILOT_FULL", "1")
    from translate.pilot import run_pilot

    with patch("translate.pilot.fetch_pilot_segments", return_value=[]) as mock_full, \
         patch("translate.pilot.fetch_debug_segments") as mock_debug, \
         patch(_PATCH_GET_CONN):
        run_pilot()

    mock_full.assert_called_once()
    mock_debug.assert_not_called()


def test_run_pilot_debug_mode_calls_fetch_debug_segments(monkeypatch):
    """Without PILOT_FULL, must route to fetch_debug_segments."""
    monkeypatch.delenv("PILOT_FULL", raising=False)
    from translate.pilot import run_pilot

    with patch("translate.pilot.fetch_debug_segments", return_value=[]) as mock_debug, \
         patch("translate.pilot.fetch_pilot_segments") as mock_full, \
         patch(_PATCH_GET_CONN):
        run_pilot()

    mock_debug.assert_called_once()
    mock_full.assert_not_called()


# ── fetch_reviewer_notes ──────────────────────────────────────────────────────


def test_fetch_reviewer_notes_returns_dict():
    notes_data = {"iteration": 2, "raw": "looks good"}
    conn, cur = _fake_conn([(notes_data,)])
    result = fetch_reviewer_notes(conn, 1)
    assert result == notes_data


def test_fetch_reviewer_notes_returns_none_when_missing():
    conn, cur = _fake_conn([])
    result = fetch_reviewer_notes(conn, 999)
    assert result is None


def test_fetch_reviewer_notes_passes_segment_id():
    conn, cur = _fake_conn([(None,)])
    fetch_reviewer_notes(conn, 42)
    _, params = cur.execute.call_args[0]
    assert params == (42,)


# ── _iteration_count ──────────────────────────────────────────────────────────


def test_iteration_count_needs_human_always_3():
    assert _iteration_count(None, "needs_human") == 3
    assert _iteration_count({"iteration": 1}, "needs_human") == 3


def test_iteration_count_uses_notes_iteration():
    notes = {"iteration": 2, "raw": "some note"}
    assert _iteration_count(notes, "translated") == 2


def test_iteration_count_defaults_to_1_without_notes():
    assert _iteration_count(None, "translated") == 1
    assert _iteration_count({}, "translated") == 1


def test_iteration_count_notes_with_no_iteration_key():
    assert _iteration_count({"raw": "note only"}, "translated") == 1


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
    assert (tmp_path / "m4_pilot.txt").exists()


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
    content = (tmp_path / "m4_pilot.txt").read_text()
    assert "Translated:" in content
    assert "Needs human:" in content
    assert "Avg iterations:" in content
    assert "Time elapsed:" in content


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
    content = (tmp_path / "m4_pilot.txt").read_text()
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
    content = (tmp_path / "m4_pilot.txt").read_text()
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
    content = (tmp_path / "m4_pilot.txt").read_text()
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
    assert (tmp_path / "m4_pilot.txt").exists()


# ── _PILOT_QUESTIONS sanity ───────────────────────────────────────────────────


def test_pilot_questions_covers_q1_to_q6():
    assert len(_PILOT_QUESTIONS) == 6
    for i, q in enumerate(_PILOT_QUESTIONS, 1):
        assert q == f"I.q{i}"
