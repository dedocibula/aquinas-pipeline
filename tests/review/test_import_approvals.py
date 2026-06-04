"""
Tests for src/review/import_approvals.py — pure logic, no real gspread, no DB.
"""

from __future__ import annotations

import re

from review.import_approvals import (
    COLS,
    get_current_sense,
    get_model_rendering,
    load_approved_rows,
    process_approval,
)
from review.sheets import HEADER

# ── Fake objects ──────────────────────────────────────────────────────────────


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


class FakeCursor:
    def __init__(self, results: list):
        self._results = list(results)
        self._idx = 0
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((_norm(sql), params or ()))

    def fetchone(self):
        if self._idx < len(self._results):
            val = self._results[self._idx]
            self._idx += 1
            return val
        return None


class FakeConn:
    def __init__(self, fetchone_results: list | None = None):
        self._cursor = FakeCursor(fetchone_results or [])

    def cursor(self):
        return self._cursor

    @property
    def executed(self):
        return self._cursor.executed


class FakeWorksheet:
    def __init__(self, rows: list[list]):
        self._rows = rows

    def get_all_values(self):
        return list(self._rows)


def _make_sheet_row(
    approved: str = "TRUE",
    latin: str = "ratio",
    proposed_slovak: str = "rozum",
    sense_id: int = 101,
    db_version: int = 1,
) -> list:
    row = [""] * 13
    row[COLS["approved"]] = approved
    row[COLS["latin_lemma"]] = latin
    row[COLS["proposed_slovak"]] = proposed_slovak
    row[COLS["sense_id"]] = str(sense_id)
    row[COLS["db_version"]] = str(db_version)
    return row


# ── load_approved_rows ────────────────────────────────────────────────────────


def test_load_approved_rows_skips_header():
    ws = FakeWorksheet(rows=[HEADER, _make_sheet_row(approved="TRUE")])
    rows = load_approved_rows(ws)
    assert len(rows) == 1


def test_load_approved_rows_filters_unticked():
    ws = FakeWorksheet(rows=[
        HEADER,
        _make_sheet_row(approved="TRUE", sense_id=1),
        _make_sheet_row(approved="FALSE", sense_id=2),
        _make_sheet_row(approved="", sense_id=3),
    ])
    rows = load_approved_rows(ws)
    assert len(rows) == 1
    assert rows[0]["sense_id"] == 1


def test_load_approved_rows_truthy_variants():
    for val in ("TRUE", "True", "true", "1", "YES", "yes"):
        ws = FakeWorksheet(rows=[HEADER, _make_sheet_row(approved=val, sense_id=99)])
        rows = load_approved_rows(ws)
        assert len(rows) == 1, f"Expected 1 row for approved={val!r}"


def test_load_approved_rows_no_dead_is_not_true_branch():
    """Approved filtering uses only _TRUTHY set — no identity check on True."""
    ws = FakeWorksheet(rows=[HEADER, _make_sheet_row(approved="FALSE", sense_id=5)])
    rows = load_approved_rows(ws)
    assert rows == []


def test_load_approved_rows_skips_blank_sense_id():
    row = _make_sheet_row(approved="TRUE")
    row[COLS["sense_id"]] = ""
    ws = FakeWorksheet(rows=[HEADER, row])
    assert load_approved_rows(ws) == []


def test_load_approved_rows_parses_ints():
    ws = FakeWorksheet(rows=[HEADER, _make_sheet_row(sense_id=42, db_version=3)])
    rows = load_approved_rows(ws)
    assert rows[0]["sense_id"] == 42
    assert rows[0]["db_version"] == 3


def test_load_approved_rows_blank_db_version_parses_as_none():
    row = _make_sheet_row(approved="TRUE")
    row[COLS["db_version"]] = ""
    ws = FakeWorksheet(rows=[HEADER, row])
    rows = load_approved_rows(ws)
    assert rows[0]["db_version"] is None


def test_load_approved_rows_returns_proposed_slovak():
    ws = FakeWorksheet(rows=[HEADER, _make_sheet_row(proposed_slovak="milosť")])
    rows = load_approved_rows(ws)
    assert rows[0]["proposed_slovak"] == "milosť"


def test_load_approved_rows_empty_sheet():
    ws = FakeWorksheet(rows=[HEADER])
    assert load_approved_rows(ws) == []


# ── get_current_sense ─────────────────────────────────────────────────────────


def test_get_current_sense_returns_dict():
    conn = FakeConn(fetchone_results=[(101, 2, "proposed")])
    result = get_current_sense(conn, 101)
    assert result == {"sense_id": 101, "version": 2, "status": "proposed"}


def test_get_current_sense_returns_none_when_missing():
    conn = FakeConn(fetchone_results=[None])
    result = get_current_sense(conn, 999)
    assert result is None


def test_get_current_sense_queries_correct_table():
    conn = FakeConn(fetchone_results=[(1, 1, "proposed")])
    get_current_sense(conn, 5)
    sql, params = conn.executed[0]
    assert "glossary_sense" in sql
    assert params == (5,)


# ── get_model_rendering ───────────────────────────────────────────────────────


def test_get_model_rendering_returns_content():
    conn = FakeConn(fetchone_results=[("rozum",)])
    result = get_model_rendering(conn, 101)
    assert result == "rozum"


def test_get_model_rendering_returns_none_when_missing():
    conn = FakeConn(fetchone_results=[None])
    result = get_model_rendering(conn, 101)
    assert result is None


def test_get_model_rendering_queries_for_model_source_preferred():
    """SQL must prefer model source but fall back to any SK rendering."""
    conn = FakeConn(fetchone_results=[("rozum",)])
    get_model_rendering(conn, 55)
    sql, params = conn.executed[0]
    assert "sense_rendering" in sql
    assert "source" in sql
    # 'model' appears in ORDER BY CASE clause as the preferred source
    assert "'model'" in sql
    assert "'sk'" in sql
    assert params == (55,)


def test_get_model_rendering_fallback_via_order_by():
    """SQL uses ORDER BY to prefer model; absence of LIMIT 1 check."""
    conn = FakeConn(fetchone_results=[("krystal_value",)])
    result = get_model_rendering(conn, 10)
    # Should return whatever the DB returns (Krystal rendering for Krystal terms)
    assert result == "krystal_value"


# ── process_approval ─────────────────────────────────────────────────────────


def _row(sense_id=101, proposed_slovak="rozum", db_version=1, latin_lemma="ratio"):
    return {
        "sense_id": sense_id,
        "proposed_slovak": proposed_slovak,
        "db_version": db_version,
        "latin_lemma": latin_lemma,
    }


def test_process_approval_ok_content_unchanged():
    """Content matches reference → status updated, version NOT bumped."""
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),   # get_current_sense
        ("rozum",),             # get_model_rendering (same content)
    ])
    status, bumped = process_approval(conn, _row(), human_src_id=6)
    assert status == "OK"
    assert bumped is False


def test_process_approval_ok_content_changed():
    """Content differs from reference → status updated, version IS bumped."""
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),   # get_current_sense
        ("old_rozum",),         # get_model_rendering (different)
        (2,),                   # bump_sense_version RETURNING version
    ])
    status, bumped = process_approval(conn, _row(proposed_slovak="nový_rozum"), human_src_id=6)
    assert status == "OK"
    assert bumped is True


def test_process_approval_conflict_on_version_mismatch():
    """DB version != sheet version AND status not approved → CONFLICT, no writes."""
    conn = FakeConn(fetchone_results=[
        (101, 3, "proposed"),   # version 3, sheet has 1
    ])
    status, bumped = process_approval(conn, _row(db_version=1), human_src_id=6)
    assert status == "CONFLICT"
    assert bumped is False
    assert len(conn.executed) == 1  # only get_current_sense


def test_process_approval_blank_db_version_is_conflict():
    """Blank db_version (None) must be treated as CONFLICT, not bypass."""
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),
    ])
    status, bumped = process_approval(conn, _row(db_version=None), human_src_id=6)
    assert status == "CONFLICT"
    assert bumped is False
    assert len(conn.executed) == 1


def test_process_approval_already_confirmed_on_rerun():
    """Re-run: version bumped by prior import → approved + version mismatch → ALREADY_CONFIRMED."""
    conn = FakeConn(fetchone_results=[
        (101, 2, "approved"),   # DB version 2, sheet still has 1 from before first import
    ])
    status, bumped = process_approval(conn, _row(db_version=1), human_src_id=6)
    assert status == "ALREADY_CONFIRMED"
    assert bumped is False


def test_process_approval_not_found():
    conn = FakeConn(fetchone_results=[None])
    status, bumped = process_approval(conn, _row(sense_id=999), human_src_id=6)
    assert status == "NOT_FOUND"
    assert bumped is False
    assert len(conn.executed) == 1


def test_process_approval_writes_human_rendering():
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),   # get_current_sense
        ("rozum",),             # get_model_rendering (same → no bump)
    ])
    process_approval(conn, _row(proposed_slovak="rozum"), human_src_id=6)
    sqls = [e[0] for e in conn.executed]
    insert_sqls = [s for s in sqls if "INSERT INTO sense_rendering" in s]
    assert len(insert_sqls) == 1
    params = conn.executed[1][1]  # second execute = write_human_rendering
    assert "rozum" in params
    assert 6 in params


def test_process_approval_updates_status_to_approved():
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),
        ("rozum",),
    ])
    process_approval(conn, _row(), human_src_id=6)
    sqls = [e[0] for e in conn.executed]
    update_sqls = [s for s in sqls if "UPDATE glossary_sense" in s and "status" in s]
    assert len(update_sqls) == 1
    status_params = [e[1] for e in conn.executed if "status" in e[0] and "UPDATE" in e[0]]
    assert any("approved" in str(p) for p in status_params)


def test_process_approval_krystal_term_no_bump_when_content_matches():
    """Krystal term: get_model_rendering returns Krystal text. If unchanged → no bump."""
    conn2 = FakeConn(fetchone_results=[
        (101, 1, "proposed"),     # proposed, version 1 = sheet version 1
        ("bytie",),               # Krystal rendering returned by get_model_rendering
    ])
    status, bumped = process_approval(conn2, _row(proposed_slovak="bytie", db_version=1), human_src_id=6)
    assert status == "OK"
    assert bumped is False  # content unchanged vs Krystal rendering → no bump


def test_process_approval_krystal_term_bumps_when_reviewer_edits():
    """Krystal term: if reviewer changes proposed_slovak → version bumps."""
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),     # version matches
        ("bytie",),               # Krystal rendering
        (2,),                     # bump_sense_version RETURNING
    ])
    status, bumped = process_approval(conn, _row(proposed_slovak="súcno", db_version=1), human_src_id=6)
    assert status == "OK"
    assert bumped is True


def test_process_approval_idempotent_no_double_bump():
    """Two calls with same content → no double-bump."""
    def make_conn():
        return FakeConn(fetchone_results=[
            (101, 1, "proposed"),
            ("rozum",),
        ])
    s1, b1 = process_approval(make_conn(), _row(), human_src_id=6)
    s2, b2 = process_approval(make_conn(), _row(), human_src_id=6)
    assert s1 == s2 == "OK"
    assert b1 == b2 is False
