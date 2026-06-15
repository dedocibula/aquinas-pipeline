"""Shared test fakes for DB and Google Sheets.

Until now each test module rolled its own FakeConn/FakeCursor (and the review
tests each re-implemented gspread fakes). These shared versions model just
enough of the psycopg2 cursor/connection contract and the gspread
worksheet/spreadsheet contract for unit tests — no real I/O.

New tests (repositories, pipeline steps) should use the factory fixtures below.
Existing modules keep their local fakes; migrate opportunistically.
"""
from __future__ import annotations

import re

import pytest


def normalize_sql(sql: str) -> str:
    """Collapse whitespace so SQL assertions match regardless of formatting."""
    return re.sub(r"\s+", " ", sql).strip()


class FakeCursor:
    """Records executed (sql, params) and replays canned results.

    fetchone_results is consumed positionally — one row per fetchone() call,
    None once exhausted. fetchall_rows is returned whole by every fetchall().
    """

    def __init__(self, fetchone_results=None, fetchall_rows=None):
        self._fetchone = list(fetchone_results or [])
        self._idx = 0
        self._fetchall = list(fetchall_rows) if fetchall_rows is not None else []
        self.executed: list[tuple] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((normalize_sql(sql), params or ()))

    def executemany(self, sql, seq):
        for params in seq:
            self.execute(sql, params)

    def fetchone(self):
        if self._idx < len(self._fetchone):
            row = self._fetchone[self._idx]
            self._idx += 1
            return row
        return None

    def fetchall(self):
        return list(self._fetchall)


class FakeConn:
    """Minimal psycopg2 connection: hands out one shared FakeCursor.

    The cursor is shared across cursor() calls so executed/fetchone sequencing
    accumulates across `with conn.cursor() as cur:` blocks, matching how the
    real code threads multiple cursors over one connection.
    """

    def __init__(self, fetchone_results=None, fetchall_rows=None):
        self._cursor = FakeCursor(fetchone_results, fetchall_rows)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, cursor_factory=None):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def executed(self):
        return self._cursor.executed


class FakeWorksheet:
    def __init__(self, title="Review", rows=None):
        self.title = title
        self.id = 42
        self._rows = rows if rows is not None else []
        self.batch_updates_issued: list = []
        self.appended: list = []
        self.cell_updates: list = []

    def get_all_values(self):
        return list(self._rows)

    def batch_update(self, updates, **kw):
        self.batch_updates_issued.append(updates)

    def append_rows(self, rows, **kw):
        self.appended.extend(rows)

    def update(self, range_name, values, **kw):
        self.cell_updates.append((range_name, values))


class FakeSpreadsheet:
    def __init__(self, worksheets=None):
        self._worksheets = worksheets or {}
        self.batch_update_calls: list = []

    def worksheets(self):
        return list(self._worksheets.values())

    def worksheet(self, title):
        return self._worksheets[title]

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet(title=title)
        self._worksheets[title] = ws
        return ws

    def batch_update(self, body):
        self.batch_update_calls.append(body)


@pytest.fixture
def fake_conn():
    """Factory: fake_conn(fetchone_results=..., fetchall_rows=...) -> FakeConn."""
    return lambda **kw: FakeConn(**kw)


@pytest.fixture
def fake_worksheet():
    """Factory: fake_worksheet(title=..., rows=...) -> FakeWorksheet."""
    return lambda **kw: FakeWorksheet(**kw)


@pytest.fixture
def fake_spreadsheet():
    """Factory: fake_spreadsheet(worksheets=...) -> FakeSpreadsheet."""
    return lambda **kw: FakeSpreadsheet(**kw)
