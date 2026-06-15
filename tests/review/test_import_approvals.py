"""
Tests for src/review/import_approvals.py — pure logic, no real gspread, no DB.
"""

from __future__ import annotations

import re

from review.import_approvals import (
    COLS,
    get_current_sense,
    get_la_surface,
    load_approved_rows,
    process_approval,
    write_human_surface,
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
    latin_text: str = "ratio",
    context_label: str = "",
    proposed_slovak: str = "rozum",
    sense_id: int = 101,
    db_version: int = 1,
) -> list:
    row = [""] * 15
    row[COLS["approved"]] = approved
    row[COLS["latin_lemma"]] = latin
    row[COLS["latin_text"]] = latin_text
    row[COLS["context_label"]] = context_label
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


def test_load_approved_rows_returns_context_label():
    ws = FakeWorksheet(rows=[HEADER, _make_sheet_row(context_label="sanctifying grace")])
    rows = load_approved_rows(ws)
    assert rows[0]["context_label"] == "sanctifying grace"


def test_load_approved_rows_empty_context_label():
    ws = FakeWorksheet(rows=[HEADER, _make_sheet_row(context_label="")])
    rows = load_approved_rows(ws)
    assert rows[0]["context_label"] == ""


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


# ── process_approval ─────────────────────────────────────────────────────────


def _row(sense_id=101, proposed_slovak="rozum", db_version=1, latin_lemma="ratio", context_label=""):
    return {
        "sense_id": sense_id,
        "proposed_slovak": proposed_slovak,
        "context_label": context_label,
        "db_version": db_version,
        "latin_lemma": latin_lemma,
    }


def test_process_approval_ok_proposed_sense_bumps():
    """Approving a proposed sense returns OK and always bumps the version."""
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),   # get_current_sense
        (2,),                   # bump_sense_version RETURNING version
    ])
    status, bumped = process_approval(conn, _row(proposed_slovak="nový_rozum"), human_src_id=6)
    assert status == "OK"
    assert bumped is True


def test_process_approval_version_mismatch_proceeds():
    """A version mismatch on a proposed sense is no longer a conflict — approval proceeds."""
    conn = FakeConn(fetchone_results=[
        (101, 3, "proposed"),   # DB version 3, sheet has 1 — mismatch, but still proposed
        (4,),                   # bump_sense_version RETURNING version
    ])
    status, bumped = process_approval(conn, _row(db_version=1), human_src_id=6)
    assert status == "OK"
    assert bumped is True


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


# ── process_approval — context_label write-back ───────────────────────────────


def test_process_approval_writes_context_label():
    """A non-empty context_label in the row is written to glossary_sense."""
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),
        ("rozum",),
    ])
    process_approval(conn, _row(context_label="sanctifying grace"), human_src_id=6)
    label_updates = [
        e for e in conn.executed
        if "UPDATE glossary_sense" in e[0] and "context_label" in e[0]
    ]
    assert len(label_updates) == 1
    sql, params = label_updates[0]
    assert params == ("sanctifying grace", 101)


def test_process_approval_empty_context_label_writes_null():
    """Empty string context_label writes NULL, not empty string."""
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),
        ("rozum",),
    ])
    process_approval(conn, _row(context_label=""), human_src_id=6)
    label_updates = [
        e for e in conn.executed
        if "UPDATE glossary_sense" in e[0] and "context_label" in e[0]
    ]
    assert len(label_updates) == 1
    _, params = label_updates[0]
    assert params[0] is None  # NULL, not ""


# ── get_la_surface ────────────────────────────────────────────────────────────


def test_get_la_surface_returns_content():
    conn = FakeConn(fetchone_results=[("Sed contra",)])
    result = get_la_surface(conn, 101)
    assert result == "Sed contra"


def test_get_la_surface_returns_none_when_missing():
    conn = FakeConn(fetchone_results=[None])
    result = get_la_surface(conn, 101)
    assert result is None


def test_get_la_surface_queries_glossary_term():
    conn = FakeConn(fetchone_results=[("Sed contra",)])
    get_la_surface(conn, 55)
    sql, params = conn.executed[0]
    assert "glossary_term" in sql
    assert "la_surface" in sql
    assert params == (55,)


# ── write_human_surface ───────────────────────────────────────────────────────


def test_write_human_surface_updates_glossary_term():
    conn = FakeConn()
    write_human_surface(conn, 101, "Respondeo dicendum quod", 7)
    sql, params = conn.executed[0]
    assert "UPDATE glossary_term" in sql
    assert "la_surface" in sql
    assert "Respondeo dicendum quod" in params
    assert 101 in params


def test_write_human_surface_targets_correct_term():
    conn = FakeConn()
    write_human_surface(conn, 55, "Sed contra", 7)
    sql, params = conn.executed[0]
    assert "glossary_sense" in sql  # resolves sense_id → term_id
    assert 55 in params


# ── process_approval — latin_text import semantics ───────────────────────────


def _row_with_surface(**overrides):
    base = {
        "sense_id": 101,
        "proposed_slovak": "rozum",
        "context_label": "",
        "db_version": 1,
        "latin_lemma": "ratio",
        "latin_text": "ratio",
    }
    base.update(overrides)
    return base


def test_process_approval_latin_text_unchanged_no_surface_write():
    """When latin_text matches current LA surface, no write is performed."""
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),   # get_current_sense
        ("rozum",),             # get_model_rendering (same SK content)
        ("ratio",),             # get_la_surface → same as latin_text
    ])
    status, bumped = process_approval(conn, _row_with_surface(), human_src_id=6)
    assert status == "OK"
    updates_la = [e for e in conn.executed if "UPDATE glossary_term" in e[0] and "la_surface" in e[0]]
    assert len(updates_la) == 0


def test_process_approval_latin_text_changed_writes_la_surface():
    """When latin_text differs from DB LA surface, glossary_term.la_surface is updated."""
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),                   # get_current_sense
        ("rozum",),                             # get_model_rendering (same → no SK bump)
        ("old_ratio",),                         # get_la_surface → different
        (False, "term"),                        # get_term_flags → singleword term
    ])
    status, bumped = process_approval(conn, _row_with_surface(latin_text="ratio"), human_src_id=6)
    assert status == "OK"
    updates_la = [e for e in conn.executed if "UPDATE glossary_term" in e[0] and "la_surface" in e[0]]
    assert len(updates_la) == 1


def test_process_approval_latin_text_changed_multiword_bumps_version():
    """LA surface change for a multiword term triggers a version bump."""
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),
        ("actus essendi",), # same SK
        ("old surface",),   # different LA surface
        (True, "term"),     # is_multiword=True
        (2,),               # bump RETURNING
    ])
    status, bumped = process_approval(conn, _row_with_surface(latin_text="actus essendi"), human_src_id=6)
    assert status == "OK"
    assert bumped is True


def test_process_approval_latin_text_changed_formula_bumps_version():
    """LA surface change for a formula term triggers a version bump."""
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),
        ("Odpovedám",),         # same SK
        ("old formula",),       # different LA surface
        (True, "formula"),      # is_multiword=True + formula category
        (2,),                   # bump RETURNING
    ])
    status, bumped = process_approval(conn, _row_with_surface(latin_text="Respondeo dicendum quod"), human_src_id=6)
    assert status == "OK"
    assert bumped is True


def test_process_approval_sk_and_la_change_only_bumps_once():
    """If both SK and LA changed, version is bumped once (SK bump already covers it)."""
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),
        ("old_rozum",),         # different SK → SK bump
        (3,),                   # bump_sense_version RETURNING
        ("old surface",),       # different LA surface
        (True, "formula"),      # formula → LA would bump too, but already bumped
    ])
    row = _row_with_surface(proposed_slovak="new_rozum", latin_text="new surface")
    status, bumped = process_approval(conn, row, human_src_id=6)
    assert status == "OK"
    assert bumped is True
    bump_calls = [e for e in conn.executed if "version = version + 1" in e[0]]
    assert len(bump_calls) == 1


def test_process_approval_empty_latin_text_skips_la_processing():
    """Empty or blank latin_text is treated as None — LA processing is skipped."""
    conn = FakeConn(fetchone_results=[
        (101, 1, "proposed"),
        ("rozum",),
    ])
    row = _row_with_surface(latin_text="")
    status, bumped = process_approval(conn, row, human_src_id=6)
    assert status == "OK"
    assert bumped is True
    # No LA surface queries or writes should have been made
    la_queries = [e for e in conn.executed if "la_surface" in e[0]]
    assert len(la_queries) == 0
    # Approval always bumps once, regardless of LA processing being skipped.
    bump_calls = [e for e in conn.executed if "version = version + 1" in e[0]]
    assert len(bump_calls) == 1
