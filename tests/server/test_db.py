"""Unit tests for src/server/db.py query helpers (SQL shape via fake_conn)."""

from __future__ import annotations

from server.db import get_segment_constraints


def test_get_segment_constraints_excludes_rejected_usage(fake_conn):
    """D10: a rejected term_usage row must not resurface as a locked term."""
    conn = fake_conn(fetchall_rows=[])
    get_segment_constraints(conn, [1])
    sql, _ = conn.executed[-1]
    assert "tu.status <> 'rejected'" in sql


def test_get_segment_constraints_empty_short_circuits(fake_conn):
    conn = fake_conn()
    assert get_segment_constraints(conn, []) == {}
    assert conn.executed == []
