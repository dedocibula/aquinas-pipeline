"""Tests for src/common/corpus_db.py."""

from __future__ import annotations

from unittest.mock import MagicMock

from common.corpus_db import (
    flag_needs_human,
    get_all_article_locators,
    get_human_edited_segments,
    get_pending_segment_ids_for_article,
    get_stale_segments,
    has_pending_segments,
    reset_translation_status,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_conn(rows: list) -> tuple[MagicMock, MagicMock]:
    """Return (conn, cursor) mock where fetchall returns rows and fetchone returns rows[0]."""
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None

    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


# ── get_all_article_locators ─────────────────────────────────────────────────


def test_get_all_article_locators_returns_prefixes():
    conn, cur = _make_conn([("I.q1.a1",), ("I.q1.a2",), ("I.q2.a1",)])
    result = get_all_article_locators(conn, work_id=1)
    assert result == ["I.q1.a1", "I.q1.a2", "I.q2.a1"]
    cur.execute.assert_called_once()
    sql, params = cur.execute.call_args.args
    assert "subpath" in sql
    assert "nlevel" in sql
    assert params == (1,)


def test_get_all_article_locators_empty():
    conn, _ = _make_conn([])
    assert get_all_article_locators(conn) == []


# ── get_pending_segment_ids_for_article ──────────────────────────────────────


def test_get_pending_segment_ids_for_article_returns_ids():
    conn, cur = _make_conn([(10,), (11,), (12,)])
    result = get_pending_segment_ids_for_article(conn, "I.q1.a1", work_id=1)
    assert result == [10, 11, 12]
    sql, params = cur.execute.call_args.args
    assert "<@" in sql
    assert "pending" in sql
    assert "work_id" in sql
    assert params == ("I.q1.a1", 1)


def test_get_pending_segment_ids_for_article_empty():
    conn, _ = _make_conn([])
    assert get_pending_segment_ids_for_article(conn, "I.q1.a1") == []


# ── has_pending_segments ─────────────────────────────────────────────────────


def test_has_pending_segments_true_when_row_returned():
    conn, cur = _make_conn([(1,)])
    assert has_pending_segments(conn, "I.q1.a1", work_id=1) is True
    sql, params = cur.execute.call_args.args
    assert "work_id" in sql
    assert params == ("I.q1.a1", 1)


def test_has_pending_segments_false_when_no_row():
    conn, cur = _make_conn([])
    cur.fetchone.return_value = None
    assert has_pending_segments(conn, "I.q1.a1") is False


# ── get_stale_segments ───────────────────────────────────────────────────────


def test_get_stale_segments_returns_segment_ids():
    conn, cur = _make_conn([(42,), (99,)])
    result = get_stale_segments(conn, work_id=1)
    assert result == [42, 99]
    sql, params = cur.execute.call_args.args
    assert "sense_version_used" in sql
    assert "gs.version" in sql
    assert "work_id" in sql
    assert params == (1,)


def test_get_stale_segments_empty():
    conn, _ = _make_conn([])
    assert get_stale_segments(conn) == []


# ── reset_translation_status ─────────────────────────────────────────────────


def test_reset_translation_status_executes_update():
    conn, cur = _make_conn([])
    reset_translation_status(conn, [1, 2, 3])
    cur.execute.assert_called_once()
    sql, params = cur.execute.call_args.args
    assert "translation_status" in sql
    assert "'pending'" in sql
    assert "reviewer_notes" in sql
    assert params == ([1, 2, 3],)


def test_reset_translation_status_no_op_on_empty_list():
    conn, cur = _make_conn([])
    reset_translation_status(conn, [])
    cur.execute.assert_not_called()


# ── get_human_edited_segments ────────────────────────────────────────────────


def test_get_human_edited_segments_returns_subset():
    conn, cur = _make_conn([(2,), (5,)])
    result = get_human_edited_segments(conn, [1, 2, 5, 9])
    assert result == [2, 5]
    sql, params = cur.execute.call_args.args
    assert "'human'" in sql
    assert "lang = 'sk'" in sql
    assert params == ([1, 2, 5, 9],)


def test_get_human_edited_segments_no_op_on_empty_list():
    conn, cur = _make_conn([])
    assert get_human_edited_segments(conn, []) == []
    cur.execute.assert_not_called()


# ── flag_needs_human ─────────────────────────────────────────────────────────


def test_flag_needs_human_executes_update_with_note():
    conn, cur = _make_conn([])
    flag_needs_human(conn, [3, 4], "term updated after human edit — verify")
    cur.execute.assert_called_once()
    sql, params = cur.execute.call_args.args
    assert "'needs_human'" in sql
    assert "reviewer_notes" in sql
    note_json, ids = params
    assert ids == [3, 4]
    assert "term updated" in str(note_json.adapted)


def test_flag_needs_human_no_op_on_empty_list():
    conn, cur = _make_conn([])
    flag_needs_human(conn, [], "note")
    cur.execute.assert_not_called()
