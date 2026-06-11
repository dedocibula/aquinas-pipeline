"""Import theologian approvals from Google Sheets back to the DB.

CLI:
    uv run python -m review.import_approvals

Idempotent — safe to re-run. Rows with status='approved' and a matching
db_version are counted as already confirmed and skipped. Rows where
db_version is blank or mismatches the DB version are reported as conflicts.

Prerequisites:
    - GSHEETS_SPREADSHEET_ID set in environment
    - .secrets/gsheets_service_account.json with Sheets API access
"""

from __future__ import annotations

from common.db import get_conn, source_id
from common.glossary_repo import bump_sense_version, update_sense_status, write_human_rendering
from review.sheets import authenticate, get_spreadsheet_id

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


def get_term_flags(conn, sense_id_val: int) -> dict | None:
    """Fetch is_multiword and category for the term owning this sense."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT gt.is_multiword, gt.category
            FROM glossary_sense gs
            JOIN glossary_term gt ON gt.term_id = gs.term_id
            WHERE gs.sense_id = %s
            """,
            (sense_id_val,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"is_multiword": row[0], "category": row[1]}


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


def get_model_rendering(conn, sense_id_val: int) -> str | None:
    """Fetch the reference Slovak rendering for version-bump comparison.

    Prefers the model proposal; falls back to any existing SK rendering
    (e.g. Krystal-seeded terms that have no model row). Returns None only
    if no SK rendering exists at all.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sr.content
            FROM sense_rendering sr
            JOIN source src ON sr.source_id = src.source_id
            WHERE sr.sense_id = %s AND sr.lang = 'sk'
            ORDER BY CASE src.code WHEN 'model' THEN 1 ELSE 2 END
            LIMIT 1
            """,
            (sense_id_val,),
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


def process_approval(conn, row: dict, human_src_id: int) -> tuple[str, bool]:
    """Apply one approved row to the DB.

    Returns (status, version_bumped) where status is one of:
      'OK'                — processed successfully
      'ALREADY_CONFIRMED' — row was already approved with the same version (skipped)
      'CONFLICT'          — db_version is blank, or DB version has changed since export
      'NOT_FOUND'         — sense_id does not exist in the DB
    version_bumped is True only when Slovak content changed and version was incremented.
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

    if current["version"] != sheet_version:
        # If already approved at this version mismatch, it's a re-run after a
        # successful first import that bumped the version.
        if current["status"] == "approved":
            return "ALREADY_CONFIRMED", False
        return "CONFLICT", False

    # Versions match — safe to write.
    write_human_rendering(conn, sense_id_val, new_slovak, human_src_id)

    # Write context_label unconditionally — empty string from sheet becomes NULL.
    # Does NOT bump version: context_label is metadata, not sense_rendering content.
    raw_label = (row.get("context_label") or "").strip()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE glossary_sense SET context_label = %s WHERE sense_id = %s",
            (raw_label if raw_label else None, sense_id_val),
        )

    reference_text = get_model_rendering(conn, sense_id_val)
    version_bumped = False
    if new_slovak != reference_text:
        bump_sense_version(conn, sense_id_val)
        version_bumped = True

    # Process latin_text (LA surface) — write if reviewer changed it, bump only for
    # multiword/formula terms (surface change → re-resolution + rerun_stale needed).
    new_surface = (row.get("latin_text") or "").strip() or None
    if new_surface is not None:
        current_surface = get_la_surface(conn, sense_id_val)
        if new_surface != current_surface:
            write_human_surface(conn, sense_id_val, new_surface, human_src_id)
            if not version_bumped:
                flags = get_term_flags(conn, sense_id_val)
                if flags and (flags["is_multiword"] or flags["category"] == "formula"):
                    bump_sense_version(conn, sense_id_val)
                    version_bumped = True

    update_sense_status(conn, sense_id_val, "approved")
    return "OK", version_bumped


def run() -> None:
    spreadsheet_id = get_spreadsheet_id()
    client = authenticate()
    spreadsheet = client.open_by_key(spreadsheet_id)
    ws = spreadsheet.worksheet(_REVIEW_TAB)

    approved_rows = load_approved_rows(ws)
    if not approved_rows:
        print("No approved rows found. Nothing to import.")
        return

    ok = skipped = conflict = not_found = version_bumped = version_held = 0
    conflicts: list[dict] = []

    with get_conn() as conn:
        human_src_id = source_id(conn, "human")
        for row in approved_rows:
            status, bumped = process_approval(conn, row, human_src_id)
            if status == "OK":
                ok += 1
                if bumped:
                    version_bumped += 1
                else:
                    version_held += 1
            elif status == "ALREADY_CONFIRMED":
                skipped += 1
            elif status == "CONFLICT":
                conflict += 1
                conflicts.append(row)
            elif status == "NOT_FOUND":
                not_found += 1

    print("Import complete.")
    print(f"  Approved:                          {ok} terms")
    print(f"  Skipped:                           {skipped} (already confirmed)")
    print(f"  Not found:                         {not_found}")
    print(f"  Conflicts:                         {conflict} (see below)")
    print(f"  Version bumped (re-run triggered): {version_bumped} terms")
    print(f"  Version held (no content change):  {version_held} terms")
    if conflicts:
        print("\nConflicts (db_version blank or DB changed — skipped):")
        for c in conflicts:
            print(f"  sense_id={c['sense_id']}  latin={c['latin_lemma']!r}  "
                  f"sheet_version={c['db_version']}")


if __name__ == "__main__":
    run()
