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

# ── Fake gspread objects ──────────────────────────────────────────────────────


class FakeWorksheet:
    def __init__(self, title: str = "Review", rows: list[list] | None = None):
        self.title = title
        self.id = 42
        self._rows: list[list] = rows if rows is not None else []
        self.batch_updates_issued: list[list] = []
        self.appended: list[list] = []
        self.cell_updates: list[tuple] = []

    def get_all_values(self) -> list[list]:
        return list(self._rows)

    def batch_update(self, updates, **kw):
        self.batch_updates_issued.append(updates)

    def append_rows(self, rows, **kw):
        self.appended.extend(rows)

    def update(self, range_name, values, **kw):
        self.cell_updates.append((range_name, values))


class FakeSpreadsheet:
    def __init__(self, worksheets: dict[str, FakeWorksheet] | None = None):
        self._worksheets: dict[str, FakeWorksheet] = worksheets or {}
        self.batch_update_calls: list[dict] = []

    def worksheets(self):
        return list(self._worksheets.values())

    def worksheet(self, title: str) -> FakeWorksheet:
        return self._worksheets[title]

    def add_worksheet(self, title: str, rows: int, cols: int) -> FakeWorksheet:
        ws = FakeWorksheet(title=title)
        self._worksheets[title] = ws
        return ws

    def batch_update(self, body: dict):
        self.batch_update_calls.append(body)


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
    row1 = ["FALSE", "ratio", "term", "", "rozum", "", "", "krystal_single",
            "4400", "I.q1.a1", "101", "1", "1"]
    row2 = ["FALSE", "anima", "term", "", "duša", "", "", "bahounek_derived",
            "200", "I.q2.a1", "202", "1", "1"]
    result = read_existing_rows_from_data([header, row1, row2])
    assert result == {101: 2, 202: 3}


def test_read_existing_rows_from_data_skips_blank_sense_id():
    row = ["FALSE", "ratio", "term", "", "rozum", "", "", "krystal_single",
           "4400", "I.q1.a1", "", "1", "1"]
    assert read_existing_rows_from_data([HEADER, row]) == {}


def test_read_existing_rows_delegates():
    header = HEADER
    row = ["FALSE", "ratio", "term", "", "rozum", "", "", "krystal_single",
           "4400", "I.q1.a1", "77", "1", "1"]
    ws = FakeWorksheet(rows=[header, row])
    assert read_existing_rows(ws) == {77: 2}


# ── batch_write_rows ──────────────────────────────────────────────────────────


def _make_row(sense_id: int, proposed_slovak: str = "test") -> list:
    return [
        False, "ratio", "term", "", proposed_slovak, "cs_text", "en_text",
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
    # Two ranges per row: B:D and F:M
    assert "B2:D2" in updated_ranges
    assert "F2:M2" in updated_ranges
    # Individual cell ranges must NOT appear
    assert "A2" not in updated_ranges
    assert "E2" not in updated_ranges


def test_batch_write_rows_preserves_col_a_and_e():
    """Columns A and E must never appear as update range targets."""
    ws = FakeWorksheet(rows=[HEADER])
    row = _make_row(sense_id=55)
    batch_write_rows(ws, [row], existing_map={55: 3})
    all_ranges = {u["range"] for u in ws.batch_updates_issued[0]}
    assert not any("A" in r and ":A" not in r for r in all_ranges if r.startswith("A"))
    assert "B3:D3" in all_ranges
    assert "F3:M3" in all_ranges


def test_batch_write_rows_range_values_correct():
    """B:D range carries cols 1,2,3; F:M carries cols 5-12."""
    ws = FakeWorksheet(rows=[HEADER])
    row = ["chk", "lemma", "cat", "ctx", "slovak", "cs", "en", "method",
           42, "loc", 101, 7, 3]
    batch_write_rows(ws, [row], existing_map={101: 2})
    updates_by_range = {u["range"]: u["values"][0] for u in ws.batch_updates_issued[0]}
    assert updates_by_range["B2:D2"] == ["lemma", "cat", "ctx"]
    assert updates_by_range["F2:M2"] == ["cs", "en", "method", 42, "loc", 101, 7, 3]


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
