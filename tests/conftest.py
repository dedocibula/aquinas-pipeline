"""Shared test fixtures for DB and Google Sheets fakes.

The fake classes themselves live in ``tests/_fakes.py`` (a plain importable
module) so a test can also import the class directly when it needs to subclass
or seed it inline. These factory fixtures are the default entry point for new
tests — they hand back a fresh fake per call:

    def test_x(fake_conn):
        conn = fake_conn(fetchone_results=[(1,)])

``normalize_sql``/``FakeConn``/``FakeCursor``/``FakeWorksheet``/``FakeSpreadsheet``
are re-exported here for tests that still import them from ``conftest``.
"""
from __future__ import annotations

import pytest

from tests._fakes import (  # noqa: F401  (re-exported for importers)
    FakeConn,
    FakeCursor,
    FakeSpreadsheet,
    FakeWorksheet,
    normalize_sql,
)


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
