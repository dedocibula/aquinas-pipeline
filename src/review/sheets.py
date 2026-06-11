"""Injectable Google Sheets helpers.

All worksheet/spreadsheet operations accept gspread objects as arguments —
never call gspread.service_account() here unless you need the shared auth
helpers (get_spreadsheet_id / authenticate). This keeps worksheet helpers
testable without real credentials.
"""

from __future__ import annotations

import os

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SECRETS_PATH = ".secrets/gsheets_service_account.json"

SENSE_ID_COL = 12   # column M (0-based); shifted by latin_text insertion at D
HEADER = [
    "approved",              # A — checkbox; reviewer ticks to approve
    "category",              # B
    "latin_lemma",           # C
    "latin_text",            # D — editable by reviewer; canonical Latin surface form
    "context_label",         # E — editable by reviewer; English, 3-6 words
    "proposed_slovak",       # F — editable by reviewer
    "latin_occurrence",      # G — full Latin segment text
    "czech_occurrence",      # H — full Czech segment text
    "english_occurrence",    # I — full English segment text
    "resolution_method",     # J
    "frequency",             # K
    "sample_locator",        # L
    "sense_id",              # M — hidden
    "group_id",              # N — hidden
    "db_version",            # O — hidden
]

# Sheets API hard limit on ValueRange objects per batchUpdate request.
_BATCH_LIMIT = 400
# Max rows per append_rows call — keeps payload well under the 10 MB API limit
# given that rows now carry full segment text (avg ~1.4 KB each).
_APPEND_CHUNK = 500


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


def get_or_create_worksheet(spreadsheet, title: str, rows: int = 5000, cols: int = 15):
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
            if raw:
                try:
                    result[int(float(str(raw).strip()))] = row_idx
                except (ValueError, OverflowError):
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


def delete_stale_rows(
    spreadsheet,
    worksheet,
    existing_map: dict[int, int],
    db_sense_ids: set[int],
) -> int:
    """Delete sheet rows whose sense_id is no longer present in the DB.

    Uses a single batchUpdate with deleteDimension requests (reverse-sorted so
    that deleting a row doesn't shift the indices of later rows).
    Returns the number of rows deleted.
    """
    stale_row_nums = sorted(
        (row_num for sense_id, row_num in existing_map.items() if sense_id not in db_sense_ids),
        reverse=True,
    )
    if not stale_row_nums:
        return 0

    sheet_id = worksheet.id
    requests = [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": row_num - 1,   # 0-based inclusive
                    "endIndex":   row_num,        # 0-based exclusive
                }
            }
        }
        for row_num in stale_row_nums
    ]
    # All requests must be sent in a single batchUpdate — the reverse-sort only
    # keeps indices stable within one atomic call. Splitting across multiple calls
    # would make each subsequent call's indices wrong after prior deletions shifted rows.
    spreadsheet.batch_update({"requests": requests})

    return len(stale_row_nums)


def batch_write_rows(
    worksheet,
    db_rows: list[list],
    existing_map: dict[int, int],
) -> None:
    """Idempotent write: update existing rows (preserve cols A, D, E, F) and append new rows.

    Preserved on update (reviewer-editable): A (checkbox), D (latin_text),
    E (context_label), F (proposed_slovak).
    Updated from DB: B-C (category, latin_lemma) and G-O (occurrences through db_version).
    Updates are issued as two ValueRange objects per row (B:C and G:O), chunked at
    _BATCH_LIMIT ranges per batchUpdate call to stay within the Sheets API limit.
    """
    updates: list[dict] = []
    inserts: list[list] = []

    for row in db_rows:
        sense_id = row[SENSE_ID_COL]
        if sense_id in existing_map:
            row_num = existing_map[sense_id]
            # Two contiguous ranges per row, skipping preserved cols A(0), D(3), E(4), F(5).
            b_to_c = [row[1], row[2]]                                           # B, C
            g_to_o = [row[6], row[7], row[8], row[9], row[10], row[11],        # G, H, I, J, K, L
                      row[12], row[13], row[14]]                                # M, N, O
            updates.append({"range": f"B{row_num}:C{row_num}", "values": [b_to_c]})
            updates.append({"range": f"G{row_num}:O{row_num}", "values": [g_to_o]})
        else:
            inserts.append(row)

    # Chunk to stay under the Sheets API per-request ValueRange limit.
    for i in range(0, len(updates), _BATCH_LIMIT):
        worksheet.batch_update(
            updates[i : i + _BATCH_LIMIT],
            value_input_option="USER_ENTERED",
        )

    for i in range(0, len(inserts), _APPEND_CHUNK):
        worksheet.append_rows(inserts[i : i + _APPEND_CHUNK], value_input_option="USER_ENTERED")


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
