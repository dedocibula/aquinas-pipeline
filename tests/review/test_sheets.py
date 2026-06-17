"""
Tests for src/review/sheets.py — pure logic, no real gspread, no network.
"""

from __future__ import annotations

from review.sheets import (
    HEADER,
    apply_checkbox_validation,
    batch_write_rows,
    get_or_create_worksheet,
    read_existing_rows,
    read_existing_rows_from_data,
    write_header,
)
from tests._fakes import FakeSpreadsheet, FakeWorksheet

# ── Fake gspread objects come from tests/_fakes.py (shared definition) ─────────


# ── get_or_create_worksheet ───────────────────────────────────────────────────


def test_get_or_create_returns_existing():
    existing = FakeWorksheet(title="Review")
    sp = FakeSpreadsheet({"Review": existing})
    ws = get_or_create_worksheet(sp, "Review")
    assert ws is existing


def test_get_or_create_adds_missing():
    sp = FakeSpreadsheet({})
    ws = get_or_create_worksheet(sp, "Auto-resolved")
    assert ws.title == "Auto-resolved"
    assert "Auto-resolved" in {w.title for w in sp.worksheets()}


# ── read_existing_rows_from_data / read_existing_rows ─────────────────────────


def test_read_existing_rows_from_data_empty():
    assert read_existing_rows_from_data([]) == {}


def test_read_existing_rows_from_data_header_only():
    assert read_existing_rows_from_data([HEADER]) == {}


def test_read_existing_rows_from_data_returns_map():
    header = HEADER
    # 15 cols: A B C D(la_text) E(ctx) F(sk) G H I J K L M(sense_id) N O
    row1 = ["FALSE", "term", "ratio", "ratio", "", "rozum", "", "", "",
            "krystal_single", "4400", "I.q1.a1", "101", "1", "1"]
    row2 = ["FALSE", "term", "anima", "anima", "", "duša", "", "", "",
            "bahounek_derived", "200", "I.q2.a1", "202", "1", "1"]
    result = read_existing_rows_from_data([header, row1, row2])
    assert result == {101: 2, 202: 3}


def test_read_existing_rows_from_data_skips_blank_sense_id():
    row = ["FALSE", "term", "ratio", "ratio", "", "rozum", "", "", "",
           "krystal_single", "4400", "I.q1.a1", "", "1", "1"]
    assert read_existing_rows_from_data([HEADER, row]) == {}


def test_read_existing_rows_delegates():
    header = HEADER
    row = ["FALSE", "term", "ratio", "ratio", "", "rozum", "", "", "",
           "krystal_single", "4400", "I.q1.a1", "77", "1", "1"]
    ws = FakeWorksheet(rows=[header, row])
    assert read_existing_rows(ws) == {77: 2}


# ── batch_write_rows ──────────────────────────────────────────────────────────


def _make_row(sense_id: int, proposed_slovak: str = "test") -> list:
    # 15 cols: A(chk) B(cat) C(lemma) D(la_text) E(ctx) F(sk) G(la_occ) H(cs) I(en) J(method) K(freq) L(loc) M(sense_id) N(group) O(ver)
    return [
        False, "term", "ratio", "ratio", "", proposed_slovak, "la_occ", "cs_text", "en_text",
        "krystal_single", 100, "I.q1.a1", sense_id, 1, 1,
    ]


def test_batch_write_rows_new_row_appended():
    ws = FakeWorksheet(rows=[HEADER])
    row = _make_row(sense_id=999)
    batch_write_rows(ws, [row], existing_map={})
    assert row in ws.appended


def test_batch_write_rows_existing_row_not_appended():
    ws = FakeWorksheet(rows=[HEADER])
    row = _make_row(sense_id=101)
    batch_write_rows(ws, [row], existing_map={101: 2})
    assert row not in ws.appended


def test_batch_write_rows_existing_row_issues_range_updates():
    ws = FakeWorksheet(rows=[HEADER])
    row = _make_row(sense_id=101)
    batch_write_rows(ws, [row], existing_map={101: 2})
    assert len(ws.batch_updates_issued) == 1
    updated_ranges = [u["range"] for u in ws.batch_updates_issued[0]]
    # Two ranges per row: B:C (category+lemma) and G:O (occurrences through db_version)
    # Preserved: A (checkbox), D (latin_text), E (context_label), F (proposed_slovak)
    assert "B2:C2" in updated_ranges
    assert "G2:O2" in updated_ranges
    # Preserved columns must NOT appear as update targets
    assert "A2" not in updated_ranges
    assert "D2" not in updated_ranges
    assert "E2" not in updated_ranges
    assert "F2" not in updated_ranges


def test_batch_write_rows_preserves_col_a_d_e():
    """Columns A (checkbox), D (latin_text), E (context_label), F (proposed_slovak) must not be overwritten."""
    ws = FakeWorksheet(rows=[HEADER])
    row = _make_row(sense_id=55)
    batch_write_rows(ws, [row], existing_map={55: 3})
    all_ranges = {u["range"] for u in ws.batch_updates_issued[0]}
    assert "B3:C3" in all_ranges
    assert "G3:O3" in all_ranges
    assert not any(r.startswith("A") for r in all_ranges)
    assert not any(r.startswith("D") for r in all_ranges)
    assert not any(r.startswith("E") for r in all_ranges)
    assert not any(r.startswith("F") for r in all_ranges)


def test_batch_write_rows_range_values_correct():
    """B:C carries cols 1-2 (category, lemma); G:O carries cols 6-14 (occurrences onward)."""
    ws = FakeWorksheet(rows=[HEADER])
    # 15 cols: 0=chk 1=cat 2=lemma 3=la_text 4=ctx 5=sk 6=la_occ 7=cs_occ 8=en_occ 9=method 10=freq 11=loc 12=sense_id 13=group 14=ver
    row = ["chk", "cat", "lemma", "la_text", "ctx", "slovak", "la_occ", "cs_occ", "en_occ",
           "method", 42, "loc", 101, 7, 3]
    batch_write_rows(ws, [row], existing_map={101: 2})
    updates_by_range = {u["range"]: u["values"][0] for u in ws.batch_updates_issued[0]}
    assert updates_by_range["B2:C2"] == ["cat", "lemma"]
    assert updates_by_range["G2:O2"] == ["la_occ", "cs_occ", "en_occ", "method", 42, "loc", 101, 7, 3]


def test_batch_write_rows_mixed_new_and_existing():
    ws = FakeWorksheet(rows=[HEADER])
    existing_row = _make_row(sense_id=101)
    new_row = _make_row(sense_id=202)
    batch_write_rows(ws, [existing_row, new_row], existing_map={101: 2})
    assert new_row in ws.appended
    assert len(ws.batch_updates_issued) == 1
    assert existing_row not in ws.appended


def test_batch_write_rows_no_updates_when_all_new():
    ws = FakeWorksheet(rows=[HEADER])
    rows = [_make_row(i) for i in [1, 2, 3]]
    batch_write_rows(ws, rows, existing_map={})
    assert len(ws.batch_updates_issued) == 0
    assert len(ws.appended) == 3


def test_batch_write_rows_chunked_at_limit():
    """Verify updates are chunked when exceeding _BATCH_LIMIT ranges."""
    from review.sheets import _BATCH_LIMIT
    ws = FakeWorksheet(rows=[HEADER])
    # Need enough rows that 2 ranges × N rows > _BATCH_LIMIT
    n_rows = (_BATCH_LIMIT // 2) + 5
    rows = [_make_row(i) for i in range(n_rows)]
    existing_map = {i: i + 2 for i in range(n_rows)}
    batch_write_rows(ws, rows, existing_map=existing_map)
    # Each row produces 2 ranges; expect more than one batch_update call
    assert len(ws.batch_updates_issued) > 1
    # Total ranges across all chunks == 2 × n_rows
    total_ranges = sum(len(chunk) for chunk in ws.batch_updates_issued)
    assert total_ranges == 2 * n_rows


# ── write_header ──────────────────────────────────────────────────────────────


def test_write_header_on_empty_sheet():
    ws = FakeWorksheet(rows=[])
    written = write_header(ws)
    assert written is True
    assert len(ws.cell_updates) == 1
    assert ws.cell_updates[0][1] == [HEADER]


def test_write_header_skips_if_already_present():
    ws = FakeWorksheet(rows=[HEADER])
    written = write_header(ws)
    assert written is False
    assert len(ws.cell_updates) == 0


def test_write_header_uses_pre_fetched_data():
    """When existing_values provided, no get_all_values() call is made."""
    ws = FakeWorksheet(rows=[HEADER])
    write_header(ws, existing_values=[HEADER])
    assert len(ws.cell_updates) == 0


def test_write_header_rewrites_wrong_header():
    ws = FakeWorksheet(rows=[["old_col_a", "old_col_b"]])
    written = write_header(ws, existing_values=[["old_col_a", "old_col_b"]])
    assert written is True


# ── apply_checkbox_validation ─────────────────────────────────────────────────


def test_apply_checkbox_validation_sends_batch_update():
    ws = FakeWorksheet()
    sp = FakeSpreadsheet({"Review": ws})
    apply_checkbox_validation(sp, ws, num_data_rows=10)
    assert len(sp.batch_update_calls) == 1
    body = sp.batch_update_calls[0]
    req = body["requests"][0]["repeatCell"]
    assert req["range"]["startRowIndex"] == 1
    assert req["range"]["endRowIndex"] == 11
    assert req["range"]["startColumnIndex"] == 0
    assert req["range"]["endColumnIndex"] == 1
    assert req["cell"]["dataValidation"]["condition"]["type"] == "BOOLEAN"


def test_apply_checkbox_validation_no_op_for_zero_rows():
    ws = FakeWorksheet()
    sp = FakeSpreadsheet({"Review": ws})
    apply_checkbox_validation(sp, ws, num_data_rows=0)
    assert len(sp.batch_update_calls) == 0
