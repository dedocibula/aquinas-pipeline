"""
Tests for the M3 write stubs in src/ingest/glossary_repo.py.
DB-free — uses FakeConn/FakeCursor that records executed SQL.
"""

from __future__ import annotations

import re

from ingest.glossary_repo import bump_sense_version, update_sense_status, write_human_rendering

# ── Fake connection ───────────────────────────────────────────────────────────


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


class FakeCursor:
    def __init__(self, *, fetchone_result=None):
        self.executed: list[tuple[str, tuple]] = []
        self._fetchone = fetchone_result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((_norm(sql), params or ()))

    def fetchone(self):
        return self._fetchone


class FakeConn:
    def __init__(self, *, fetchone_result=None):
        self._cursor = FakeCursor(fetchone_result=fetchone_result)

    def cursor(self):
        return self._cursor

    @property
    def executed(self):
        return self._cursor.executed


# ── update_sense_status ───────────────────────────────────────────────────────


def test_update_sense_status_issues_correct_sql():
    conn = FakeConn()
    update_sense_status(conn, sense_id=42, status="approved")
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "UPDATE glossary_sense" in sql
    assert "SET status" in sql
    assert "WHERE sense_id" in sql
    assert params == ("approved", 42)


def test_update_sense_status_rejected():
    conn = FakeConn()
    update_sense_status(conn, sense_id=7, status="rejected")
    _, params = conn.executed[0]
    assert params == ("rejected", 7)


# ── bump_sense_version ────────────────────────────────────────────────────────


def test_bump_sense_version_issues_correct_sql():
    conn = FakeConn(fetchone_result=(3,))
    bump_sense_version(conn, sense_id=99)
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "UPDATE glossary_sense" in sql
    assert "version = version + 1" in sql
    assert "RETURNING version" in sql
    assert params == (99,)


def test_bump_sense_version_returns_new_version():
    conn = FakeConn(fetchone_result=(5,))
    result = bump_sense_version(conn, sense_id=1)
    assert result == 5


# ── write_human_rendering ─────────────────────────────────────────────────────


def test_write_human_rendering_issues_correct_sql():
    conn = FakeConn()
    write_human_rendering(conn, sense_id=10, sk_text="milosť", src_id=6)
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "INSERT INTO sense_rendering" in sql
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql
    assert "content = EXCLUDED.content" in sql
    assert params == (10, "milosť", 6)


def test_write_human_rendering_uses_sk_lang():
    conn = FakeConn()
    write_human_rendering(conn, sense_id=10, sk_text="milosť", src_id=6)
    sql, _ = conn.executed[0]
    assert "'sk'" in sql
