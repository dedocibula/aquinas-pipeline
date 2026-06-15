"""Import theologian approvals from Google Sheets back to the DB.

CLI:
    uv run python -m review.import_approvals

Idempotent — safe to re-run. Senses already marked 'approved' in the DB are
skipped. Approvals always bump sense version so rerun_stale picks up stale
segments. Version mismatches on proposed senses are accepted (the DB was
bumped after export; human authority overrides). Only blank db_version is
treated as a conflict.

Prerequisites:
    - GSHEETS_SPREADSHEET_ID set in environment
    - .secrets/gsheets_service_account.json with Sheets API access
"""

from __future__ import annotations

from common.glossary_repo import bump_sense_version, update_sense_status, write_human_rendering
from review.sheets import authenticate, get_spreadsheet_id
from storage.db import get_conn, source_id

_REVIEW_TAB = "Review"

# Column indices (0-based) matching the sheet layout in export_sheet.py
COLS = {
    "approved": 0,          # A
    "latin_lemma": 2,       # C
    "latin_text": 3,        # D — canonical Latin surface form (editable)
    "context_label": 4,     # E — was D
    "proposed_slovak": 5,   # F — was E
    "sense_id": 12,         # M — was L
    "db_version": 14,       # O — was N
}

_TRUTHY = {"TRUE", "True", "true", "1", "YES", "yes"}


def load_approved_rows(worksheet) -> list[dict]:
    """Return rows where column A is truthy. Skips header and blank sense_id cells."""
    all_rows = worksheet.get_all_values()
    result = []
    for raw in all_rows[1:]:  # skip header
        approved_cell = raw[COLS["approved"]] if len(raw) > COLS["approved"] else ""
        if str(approved_cell).strip() not in _TRUTHY:
            continue
        sense_raw = raw[COLS["sense_id"]] if len(raw) > COLS["sense_id"] else ""
        if not str(sense_raw).strip():
            continue
        try:
            sense_id_val = int(sense_raw)
        except (ValueError, TypeError):
            continue
        version_raw = raw[COLS["db_version"]] if len(raw) > COLS["db_version"] else ""
        try:
            db_version = int(version_raw)
        except (ValueError, TypeError):
            db_version = None
        result.append({
            "sense_id": sense_id_val,
            "latin_text": raw[COLS["latin_text"]] if len(raw) > COLS["latin_text"] else "",
            "proposed_slovak": raw[COLS["proposed_slovak"]] if len(raw) > COLS["proposed_slovak"] else "",
            "context_label": raw[COLS["context_label"]] if len(raw) > COLS["context_label"] else "",
            "db_version": db_version,
            "latin_lemma": raw[COLS["latin_lemma"]] if len(raw) > COLS["latin_lemma"] else "",
        })
    return result


def get_current_sense(conn, sense_id_val: int) -> dict | None:
    """Fetch current version and status for a sense. Returns None if not found."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sense_id, version, status FROM glossary_sense WHERE sense_id = %s",
            (sense_id_val,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"sense_id": row[0], "version": row[1], "status": row[2]}


def get_la_surface(conn, sense_id_val: int) -> str | None:
    """Fetch la_surface for the term owning this sense. Returns None if absent."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT gt.la_surface
            FROM glossary_sense gs
            JOIN glossary_term gt ON gt.term_id = gs.term_id
            WHERE gs.sense_id = %s
            """,
            (sense_id_val,),
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


def write_human_surface(conn, sense_id_val: int, surface: str, src_id: int) -> None:
    """Write la_surface onto the glossary_term that owns this sense."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE glossary_term SET la_surface = %s
            WHERE term_id = (SELECT term_id FROM glossary_sense WHERE sense_id = %s)
            """,
            (surface, sense_id_val),
        )


def process_approval(conn, row: dict, human_src_id: int) -> tuple[str, bool]:
    """Apply one approved row to the DB.

    Returns (status, version_bumped) where status is one of:
      'OK'                — processed successfully
      'ALREADY_CONFIRMED' — sense already approved (skipped)
      'CONFLICT'          — db_version is blank (skipped)
      'NOT_FOUND'         — sense_id does not exist in the DB
    version_bumped is always True for 'OK'.
    """
    sense_id_val = row["sense_id"]
    new_slovak = row["proposed_slovak"]
    sheet_version = row["db_version"]

    current = get_current_sense(conn, sense_id_val)
    if current is None:
        return "NOT_FOUND", False

    # Blank db_version means unknown provenance — treat as a conflict to be safe.
    if sheet_version is None:
        return "CONFLICT", False

    if current["status"] == "approved":
        # Already approved — idempotent skip regardless of version.
        return "ALREADY_CONFIRMED", False

    # For proposed senses, a version mismatch means the DB was bumped after export
    # (e.g. sense-mining re-resolution). The human has seen this sense and explicitly
    # approved it, so proceed unconditionally.

    write_human_rendering(conn, sense_id_val, new_slovak, human_src_id)

    # Write context_label — empty string becomes NULL; does NOT bump version.
    raw_label = (row.get("context_label") or "").strip()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE glossary_sense SET context_label = %s WHERE sense_id = %s",
            (raw_label if raw_label else None, sense_id_val),
        )

    # Always bump on approval: marks all term_usage rows using any prior version
    # as stale so rerun_stale picks them up.
    bump_sense_version(conn, sense_id_val)

    # LA surface — write if reviewer supplied one; approval bump already covers rerun.
    new_surface = (row.get("latin_text") or "").strip() or None
    if new_surface is not None:
        current_surface = get_la_surface(conn, sense_id_val)
        if new_surface != current_surface:
            write_human_surface(conn, sense_id_val, new_surface, human_src_id)

    update_sense_status(conn, sense_id_val, "approved")
    return "OK", True


def run() -> None:
    spreadsheet_id = get_spreadsheet_id()
    client = authenticate()
    spreadsheet = client.open_by_key(spreadsheet_id)
    ws = spreadsheet.worksheet(_REVIEW_TAB)

    approved_rows = load_approved_rows(ws)
    if not approved_rows:
        print("No approved rows found. Nothing to import.")
        return

    ok = skipped = conflict = not_found = 0
    conflicts: list[dict] = []

    with get_conn() as conn:
        human_src_id = source_id(conn, "human")
        for row in approved_rows:
            status, _bumped = process_approval(conn, row, human_src_id)
            if status == "OK":
                ok += 1
            elif status == "ALREADY_CONFIRMED":
                skipped += 1
            elif status == "CONFLICT":
                conflict += 1
                conflicts.append(row)
            elif status == "NOT_FOUND":
                not_found += 1

    print("Import complete.")
    print(f"  Approved (rerun triggered): {ok} terms")
    print(f"  Skipped (already approved): {skipped}")
    print(f"  Not found:                  {not_found}")
    print(f"  Conflicts (blank version):  {conflict} (see below)")
    if conflicts:
        print("\nConflicts (db_version blank — skipped):")
        for c in conflicts:
            print(f"  sense_id={c['sense_id']}  latin={c['latin_lemma']!r}  "
                  f"sheet_version={c['db_version']}")


if __name__ == "__main__":
    run()
