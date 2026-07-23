"""Unit tests for server.db.collect_digests / mark_thread_notified (SQL shape via fake_conn)."""

from __future__ import annotations

from datetime import datetime

from server.db import collect_digests, mark_thread_notified


def test_collect_digests_groups_rows_per_recipient(fake_conn):
    rows = [
        {
            "user_email": "alice@example.com",
            "segment_id": 42,
            "locator": "I.q3.a1.arg2",
            "author": "bob@example.com",
            "created_at": datetime(2026, 7, 20, 10, 0),
            "body": "reply one",
        },
        {
            "user_email": "alice@example.com",
            "segment_id": 43,
            "locator": "I.q3.a1.arg3",
            "author": "carol@example.com",
            "created_at": datetime(2026, 7, 20, 11, 0),
            "body": "reply two",
        },
        {
            "user_email": "bob@example.com",
            "segment_id": 42,
            "locator": "I.q3.a1.arg2",
            "author": "alice@example.com",
            "created_at": datetime(2026, 7, 20, 12, 0),
            "body": "reply three",
        },
    ]
    conn = fake_conn(fetchall_rows=rows)
    digests = collect_digests(conn)

    by_email = {d.user_email: d for d in digests}
    assert set(by_email) == {"alice@example.com", "bob@example.com"}
    assert [i.body for i in by_email["alice@example.com"].items] == ["reply one", "reply two"]
    assert [i.body for i in by_email["bob@example.com"].items] == ["reply three"]


def test_collect_digests_empty_yields_no_digests(fake_conn):
    conn = fake_conn(fetchall_rows=[])
    assert collect_digests(conn) == []


def test_collect_digests_query_excludes_self_and_respects_watermarks(fake_conn):
    """The SQL itself must exclude the author and use both watermarks — asserted on the SQL text."""
    conn = fake_conn(fetchall_rows=[])
    collect_digests(conn)
    sql, _ = conn.executed[-1]
    assert "c.author <> p.user_email" in sql
    assert "GREATEST(st.last_read_at, st.last_notified_at)" in sql
    assert "'-infinity'" in sql


def test_mark_thread_notified_upserts(fake_conn):
    conn = fake_conn()
    mark_thread_notified(conn, 42, "alice@example.com")
    sql, params = conn.executed[-1]
    assert "ON CONFLICT (segment_id, user_email) DO UPDATE SET last_notified_at = now()" in sql
    assert params == (42, "alice@example.com")
