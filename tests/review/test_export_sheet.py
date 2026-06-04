"""
Tests for src/review/export_sheet.py — pure logic, no real gspread, no DB.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from review.export_sheet import (
    _WHERE_AUTO,
    _WHERE_MAIN,
    export_tab,
    fetch_auto_resolved_rows,
    fetch_main_rows,
    rows_to_sheet_values,
)
from review.sheets import HEADER

# ── Fake objects ──────────────────────────────────────────────────────────────


class FakeWorksheet:
    def __init__(self, title: str = "Review", rows: list[list] | None = None):
        self.title = title
        self.id = 42
        self._rows: list[list] = rows if rows is not None else []
        self.batch_updates_issued: list = []
        self.appended: list[list] = []
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
    def __init__(self, worksheets: dict | None = None):
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


def _db_row(**overrides) -> dict:
    base = {
        "term_id": 1,
        "sense_id": 101,
        "latin_lemma": "ratio",
        "category": "term",
        "proposed_slovak": "rozum",
        "latin_occurrence": "Ratio est...",
        "czech_occurrence": "Rozum je...",
        "english_occurrence": "Reason is...",
        "resolution_method": "krystal_single",
        "frequency": 4400,
        "sample_locator": "I.q1.a1.arg1",
        "status": "approved",
        "version": 1,
        "group_id": 1,
    }
    base.update(overrides)
    return base


# ── rows_to_sheet_values ──────────────────────────────────────────────────────


def test_rows_to_sheet_values_length():
    values = rows_to_sheet_values([_db_row()])
    assert len(values) == 1
    assert len(values[0]) == 13


def test_rows_to_sheet_values_approved_is_false():
    values = rows_to_sheet_values([_db_row()])
    assert values[0][0] is False


def test_rows_to_sheet_values_column_order():
    row = _db_row()
    values = rows_to_sheet_values([row])[0]
    assert values[1] == "term"              # B category
    assert values[2] == "ratio"             # C latin_lemma
    assert values[3] == "rozum"             # D proposed_slovak
    assert values[4] == "Ratio est..."      # E latin_occurrence
    assert values[5] == "Rozum je..."       # F czech_occurrence
    assert values[6] == "Reason is..."      # G english_occurrence
    assert values[7] == "krystal_single"    # H resolution_method
    assert values[8] == 4400                # I frequency
    assert values[9] == "I.q1.a1.arg1"     # J sample_locator
    assert values[10] == 101                # K sense_id
    assert values[11] == 1                  # L group_id
    assert values[12] == 1                  # M db_version


def test_rows_to_sheet_values_none_to_empty_string():
    row = _db_row(category=None, proposed_slovak=None,
                  latin_occurrence=None, czech_occurrence=None, english_occurrence=None,
                  resolution_method=None, sample_locator=None,
                  frequency=None, group_id=None, version=None)
    values = rows_to_sheet_values([row])[0]
    for col in [1, 3, 4, 5, 6, 7, 8, 9, 11, 12]:
        assert values[col] == "", f"col {col} should be empty string, got {values[col]!r}"


def test_rows_to_sheet_values_sense_id_preserved():
    row = _db_row(sense_id=999)
    values = rows_to_sheet_values([row])[0]
    assert values[10] == 999


# ── fetch_main_rows / fetch_auto_resolved_rows SQL filtering ─────────────────


def _fake_conn_with_rows(rows):
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_fetch_main_rows_uses_main_where_clause():
    conn, cur = _fake_conn_with_rows([])
    fetch_main_rows(conn)
    sql = cur.execute.call_args[0][0]
    assert _WHERE_MAIN in sql
    assert _WHERE_AUTO not in sql


def test_fetch_auto_resolved_rows_uses_auto_where_clause():
    conn, cur = _fake_conn_with_rows([])
    fetch_auto_resolved_rows(conn)
    sql = cur.execute.call_args[0][0]
    assert _WHERE_AUTO in sql
    assert _WHERE_MAIN not in sql


# ── export_tab ────────────────────────────────────────────────────────────────


def test_export_tab_fresh_sheet_appends_all_rows():
    sp = FakeSpreadsheet()
    ws = FakeWorksheet(rows=[])
    sp._worksheets["Review"] = ws
    rows = [_db_row(sense_id=i) for i in [1, 2, 3]]
    count = export_tab(sp, "Review", rows, apply_checkbox=False)
    assert count == 3
    assert len(ws.appended) == 3


def test_export_tab_writes_header_first():
    sp = FakeSpreadsheet()
    ws = FakeWorksheet(rows=[])
    sp._worksheets["Review"] = ws
    export_tab(sp, "Review", [_db_row()], apply_checkbox=False)
    assert ws.cell_updates[0][1] == [HEADER]


def test_export_tab_creates_worksheet_if_missing():
    sp = FakeSpreadsheet()
    export_tab(sp, "Review", [_db_row()], apply_checkbox=False)
    assert "Review" in {ws.title for ws in sp.worksheets()}


def test_export_tab_existing_rows_not_duplicated():
    # Sheet already has the header + one data row with sense_id=101
    existing_data_row = [
        "FALSE", "ratio", "term", "", "rozum", "", "", "krystal_single",
        4400, "I.q1.a1", 101, 1, 1,
    ]
    sp = FakeSpreadsheet()
    ws = FakeWorksheet(rows=[HEADER, existing_data_row])
    sp._worksheets["Review"] = ws
    rows = [_db_row(sense_id=101)]
    export_tab(sp, "Review", rows, apply_checkbox=False)
    assert len(ws.appended) == 0        # not duplicated
    assert len(ws.batch_updates_issued) == 1  # updated in-place


def test_export_tab_applies_checkbox_when_requested():
    sp = FakeSpreadsheet()
    ws = FakeWorksheet(rows=[])
    sp._worksheets["Review"] = ws
    export_tab(sp, "Review", [_db_row()], apply_checkbox=True)
    assert len(sp.batch_update_calls) == 1


def test_export_tab_no_checkbox_for_auto_resolved():
    sp = FakeSpreadsheet()
    ws = FakeWorksheet(rows=[])
    sp._worksheets["Auto-resolved"] = ws
    export_tab(sp, "Auto-resolved", [_db_row()], apply_checkbox=False)
    assert len(sp.batch_update_calls) == 0


def test_export_tab_returns_row_count():
    sp = FakeSpreadsheet()
    rows = [_db_row(sense_id=i) for i in range(5)]
    count = export_tab(sp, "Review", rows, apply_checkbox=False)
    assert count == 5


def test_export_tab_one_get_all_values_call():
    """export_tab must call get_all_values() exactly once per tab."""
    call_count = 0

    class CountingWorksheet(FakeWorksheet):
        def get_all_values(self):
            nonlocal call_count
            call_count += 1
            return super().get_all_values()

    sp = FakeSpreadsheet()
    ws = CountingWorksheet(rows=[])
    sp._worksheets["Review"] = ws
    export_tab(sp, "Review", [_db_row()], apply_checkbox=False)
    assert call_count == 1
