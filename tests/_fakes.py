"""Shared test fakes for DB and Google Sheets — the single definition.

These model just enough of the psycopg2 cursor/connection contract and the
gspread worksheet/spreadsheet contract for unit tests (no real I/O). They live
in a plain importable module (not ``conftest.py``) so tests can either:

  - take the factory fixtures in ``conftest.py`` (``fake_conn``/``fake_worksheet``/
    ``fake_spreadsheet``) — the default for new tests; or
  - import the classes directly (``from tests._fakes import FakeConn``) when a
    test needs the class itself (e.g. to subclass it, or to seed it inline).

Either way there is one definition of each fake, used everywhere.
"""
from __future__ import annotations

import re


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
