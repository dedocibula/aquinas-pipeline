"""Injectable Google Sheets helpers.

All worksheet/spreadsheet operations accept gspread objects as arguments —
never call gspread.service_account() here unless you need the shared auth
helpers (get_spreadsheet_id / authenticate). This keeps worksheet helpers
testable without real credentials.
"""

from __future__ import annotations

import os

import gspread
from google.oauth2.service_account import Credentials

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SECRETS_PATH = ".secrets/gsheets_service_account.json"

SENSE_ID_COL = 10   # column K (0-based)
HEADER = [
    "approved",           # A — checkbox; reviewer ticks to approve
    "latin_lemma",        # B
    "category",           # C
    "context_label",      # D
    "proposed_slovak",    # E — editable by reviewer
    "czech_anchor",       # F
    "english_cue",        # G
    "resolution_method",  # H
    "frequency",          # I
    "sample_locator",     # J
    "sense_id",           # K — hidden
    "group_id",           # L — hidden
    "db_version",         # M — hidden
]

# Columns preserved on re-export for existing rows (0-based indices).
# col A (0) = reviewer checkbox; col E (4) = reviewer-edited proposed_slovak.
_PRESERVE_COLS = frozenset({0, 4})

# Sheets API hard limit on ValueRange objects per batchUpdate request.
_BATCH_LIMIT = 400


# ── Shared auth helpers ───────────────────────────────────────────────────────


def get_spreadsheet_id() -> str:
    """Read GSHEETS_SPREADSHEET_ID from the environment."""
    sid = os.environ.get("GSHEETS_SPREADSHEET_ID", "").strip()
    if not sid:
        raise RuntimeError(
            "GSHEETS_SPREADSHEET_ID is not set. "
            "Add it to your .env file and re-run."
        )
    return sid


def authenticate() -> gspread.Client:
    """Authenticate via service account JSON at _SECRETS_PATH."""
    creds = Credentials.from_service_account_file(_SECRETS_PATH, scopes=_SCOPES)
    return gspread.authorize(creds)


# ── Worksheet helpers ─────────────────────────────────────────────────────────


def get_or_create_worksheet(spreadsheet, title: str, rows: int = 5000, cols: int = 13):
    """Return an existing worksheet by title, or create a new one."""
    for ws in spreadsheet.worksheets():
        if ws.title == title:
            return ws
    return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def read_existing_rows_from_data(all_rows: list[list]) -> dict[int, int]:
    """Build {sense_id: 1-based row number} from pre-fetched sheet data.

    Row 1 (index 0) is assumed to be the header and is skipped.
    Blank or non-integer values in column K are skipped.
    """
    result: dict[int, int] = {}
    for row_idx, row in enumerate(all_rows[1:], start=2):
        if len(row) > SENSE_ID_COL:
            raw = row[SENSE_ID_COL]
            if raw and str(raw).strip().lstrip("-").isdigit():
                try:
                    result[int(raw)] = row_idx
                except ValueError:
                    pass
    return result


def read_existing_rows(worksheet) -> dict[int, int]:
    """Scan column K for sense_ids; return {sense_id: 1-based row number}.

    One get_all_values() call. Prefer read_existing_rows_from_data when you
    already have the sheet data to avoid a redundant API round-trip.
    """
    return read_existing_rows_from_data(worksheet.get_all_values())


def write_header(worksheet, existing_values: list[list] | None = None) -> bool:
    """Write the header row if missing or incorrect.

    Accepts pre-fetched sheet data to avoid an extra get_all_values() call.
    Returns True if the header was written, False if it was already present.
    """
    data = existing_values if existing_values is not None else worksheet.get_all_values()
    if not data or data[0] != HEADER:
        worksheet.update("A1", [HEADER], value_input_option="USER_ENTERED")
        return True
    return False


def batch_write_rows(
    worksheet,
    db_rows: list[list],
    existing_map: dict[int, int],
) -> None:
    """Idempotent write: update existing rows (preserve cols A+E) and append new rows.

    For existing rows, columns B-D and F-M are updated (skipping preserved A and E).
    Updates are issued as two ValueRange objects per row (B:D and F:M), chunked at
    _BATCH_LIMIT ranges per batchUpdate call to stay within the Sheets API limit.

    New rows are appended with False in col A and the DB value in col E.
    """
    updates: list[dict] = []
    inserts: list[list] = []

    for row in db_rows:
        sense_id = row[SENSE_ID_COL]
        if sense_id in existing_map:
            row_num = existing_map[sense_id]
            # Two contiguous ranges per row, skipping preserved cols A(0) and E(4).
            b_to_d = [row[1], row[2], row[3]]                          # B, C, D
            f_to_m = [row[5], row[6], row[7], row[8], row[9],          # F, G, H, I, J
                      row[10], row[11], row[12]]                        # K, L, M
            updates.append({"range": f"B{row_num}:D{row_num}", "values": [b_to_d]})
            updates.append({"range": f"F{row_num}:M{row_num}", "values": [f_to_m]})
        else:
            inserts.append(row)

    # Chunk to stay under the Sheets API per-request ValueRange limit.
    for i in range(0, len(updates), _BATCH_LIMIT):
        worksheet.batch_update(
            updates[i : i + _BATCH_LIMIT],
            value_input_option="USER_ENTERED",
        )

    if inserts:
        worksheet.append_rows(inserts, value_input_option="USER_ENTERED")


def apply_checkbox_validation(spreadsheet, worksheet, num_data_rows: int) -> None:
    """Apply DATA_VALIDATION (checkbox) to column A for all data rows in one API call."""
    if num_data_rows <= 0:
        return
    spreadsheet.batch_update({
        "requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": worksheet.id,
                    "startRowIndex": 1,
                    "endRowIndex": 1 + num_data_rows,
                    "startColumnIndex": 0,
                    "endColumnIndex": 1,
                },
                "cell": {
                    "dataValidation": {
                        "condition": {"type": "BOOLEAN"},
                        "strict": True,
                    }
                },
                "fields": "dataValidation",
            }
        }]
    })
