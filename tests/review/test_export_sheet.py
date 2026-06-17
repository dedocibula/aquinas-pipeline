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
from tests._fakes import FakeSpreadsheet, FakeWorksheet

# ── Fake objects come from tests/_fakes.py (shared definition) ─────────────────


def _db_row(**overrides) -> dict:
    base = {
        "term_id": 1,
        "sense_id": 101,
        "latin_lemma": "ratio",
        "category": "term",
        "latin_text": "ratio",
        "context_label": None,
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
    assert len(values[0]) == 15


def test_rows_to_sheet_values_approved_is_false():
    values = rows_to_sheet_values([_db_row()])
    assert values[0][0] is False


def test_rows_to_sheet_values_column_order():
    row = _db_row(context_label="as rational faculty", latin_text="ratio")
    values = rows_to_sheet_values([row])[0]
    assert values[1] == "term"                  # B category
    assert values[2] == "ratio"                 # C latin_lemma
    assert values[3] == "ratio"                 # D latin_text
    assert values[4] == "as rational faculty"   # E context_label
    assert values[5] == "rozum"                 # F proposed_slovak
    assert values[6] == "Ratio est..."          # G latin_occurrence
    assert values[7] == "Rozum je..."           # H czech_occurrence
    assert values[8] == "Reason is..."          # I english_occurrence
    assert values[9] == "krystal_single"        # J resolution_method
    assert values[10] == 4400                   # K frequency
    assert values[11] == "I.q1.a1.arg1"        # L sample_locator
    assert values[12] == 101                    # M sense_id
    assert values[13] == 1                      # N group_id
    assert values[14] == 1                      # O db_version


def test_rows_to_sheet_values_null_context_label_is_empty_string():
    row = _db_row(context_label=None)
    values = rows_to_sheet_values([row])[0]
    assert values[4] == ""  # E context_label


def test_rows_to_sheet_values_none_to_empty_string():
    row = _db_row(category=None, latin_text=None, context_label=None, proposed_slovak=None,
                  latin_occurrence=None, czech_occurrence=None, english_occurrence=None,
                  resolution_method=None, sample_locator=None,
                  frequency=None, group_id=None, version=None)
    values = rows_to_sheet_values([row])[0]
    # cols: 1=category, 3=latin_text, 4=context_label, 5=proposed_slovak, 6=latin_occ,
    #       7=czech_occ, 8=en_occ, 9=method, 10=freq, 11=locator, 13=group_id, 14=db_version
    for col in [1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14]:
        assert values[col] == "", f"col {col} should be empty string, got {values[col]!r}"


def test_rows_to_sheet_values_sense_id_preserved():
    row = _db_row(sense_id=999)
    values = rows_to_sheet_values([row])[0]
    assert values[12] == 999  # col M (sense_id shifted by latin_text insertion at D)


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
    # Sheet already has the header + one data row with sense_id=101 (col M = index 12)
    existing_data_row = [
        "FALSE", "term", "ratio", "ratio", "", "rozum", "", "", "", "krystal_single",
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
