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

from review.sheets import authenticate, get_spreadsheet_id
from storage.db import get_conn, source_id
from storage.repositories import GlossaryRepository

_REVIEW_TAB = "Review"

# Column indices (0-based) matching the sheet layout in export_sheet.py
COLS = {
    "approved": 0,          # A
    "category": 1,          # B
    "latin_lemma": 2,       # C
    "latin_text": 3,        # D — canonical Latin surface form (editable)
    "context_label": 4,     # E
    "proposed_slovak": 5,   # F
    "sense_id": 12,         # M
    "db_version": 14,       # O
}

_TRUTHY = {"TRUE", "True", "true", "1", "YES", "yes"}


def _cell(raw: list, col: str) -> str:
    idx = COLS[col]
    return raw[idx].strip() if len(raw) > idx else ""


def load_approved_rows(worksheet) -> list[dict]:
    """Return approved rows from the sheet.

    Rows with a sense_id go to the existing-sense approval path.
    Rows with a blank sense_id but a filled latin_lemma go to the new-term creation path.
    Rows with neither are skipped.
    """
    all_rows = worksheet.get_all_values()
    result = []
    for raw in all_rows[1:]:  # skip header
        if _cell(raw, "approved") not in _TRUTHY:
            continue

        sense_str = _cell(raw, "sense_id")
        latin_lemma = _cell(raw, "latin_lemma")

        if not sense_str and not latin_lemma:
            continue

        sense_id_val = None
        if sense_str:
            try:
                sense_id_val = int(sense_str)
            except (ValueError, TypeError):
                continue  # non-integer sense_id — malformed row

        db_version = None
        version_str = _cell(raw, "db_version")
        if version_str:
            try:
                db_version = int(version_str)
            except (ValueError, TypeError):
                pass

        result.append({
            "sense_id": sense_id_val,        # None = new-term path
            "latin_lemma": latin_lemma,
            "latin_text": _cell(raw, "latin_text"),
            "context_label": _cell(raw, "context_label"),
            "proposed_slovak": _cell(raw, "proposed_slovak"),
            "category": _cell(raw, "category"),
            "db_version": db_version,
        })
    return result


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

    glossary = GlossaryRepository(conn)

    current = glossary.get_current_sense(sense_id_val)
    if current is None:
        return "NOT_FOUND", False

    # Blank db_version means unknown provenance — treat as a conflict to be safe.
    if sheet_version is None:
        return "CONFLICT", False

    raw_label = (row.get("context_label") or "").strip()
    new_label = raw_label if raw_label else None

    if current["status"] == "approved":
        # Sense already approved — apply content corrections if anything changed.
        current_sk = glossary.get_sk_rendering_content(sense_id_val)
        version_bumped = False
        if new_slovak and new_slovak != current_sk:
            glossary.write_human_rendering(sense_id_val, new_slovak, human_src_id)
            glossary.bump_sense_version(sense_id_val)
            version_bumped = True
        glossary.write_context_label(sense_id_val, new_label)
        return ("OK" if version_bumped else "ALREADY_CONFIRMED"), version_bumped

    # For proposed senses, a version mismatch means the DB was bumped after export
    # (e.g. sense-mining re-resolution). The human has seen this sense and explicitly
    # approved it, so proceed unconditionally.

    glossary.write_human_rendering(sense_id_val, new_slovak, human_src_id)

    # Write context_label — empty string becomes NULL; does NOT bump version.
    glossary.write_context_label(sense_id_val, new_label)

    # Always bump on approval: marks all term_usage rows using any prior version
    # as stale so rerun_stale picks them up.
    glossary.bump_sense_version(sense_id_val)

    # LA surface — write if reviewer supplied one; approval bump already covers rerun.
    new_surface = (row.get("latin_text") or "").strip() or None
    if new_surface is not None:
        current_surface = glossary.get_la_surface(sense_id_val)
        if new_surface != current_surface:
            glossary.write_human_surface(sense_id_val, new_surface)

    glossary.update_sense_status(sense_id_val, "approved")
    return "OK", True


def process_new_term(conn, row: dict, human_src_id: int) -> str:
    """Create or update a glossary term/sense for a row whose sense_id is blank.

    Routes:
      'CREATED'     — new glossary_term + glossary_sense (+ SK rendering if supplied)
      'SENSE_ADDED' — term exists, new sense added for this context_label
      'UPDATED'     — term + sense both exist; content/label updated as needed
      'NO_LEMMA'    — latin_lemma blank; row skipped
    """
    latin_lemma = (row.get("latin_lemma") or "").strip()
    if not latin_lemma:
        return "NO_LEMMA"

    context_label = (row.get("context_label") or "").strip() or None
    proposed_slovak = (row.get("proposed_slovak") or "").strip() or None
    category = (row.get("category") or "").strip() or None
    la_surface = (row.get("latin_text") or "").strip() or None

    glossary = GlossaryRepository(conn)
    term_id = glossary.find_term_by_lemma(latin_lemma)

    if term_id is None:
        term_id = glossary.insert_glossary_term(latin_lemma, category, la_surface)
        sense_id = glossary.insert_glossary_sense(term_id, context_label, status="approved")
        if proposed_slovak:
            glossary.write_human_rendering(sense_id, proposed_slovak, human_src_id)
        return "CREATED"

    existing = glossary.find_sense_by_label(term_id, context_label)

    if existing is None:
        sense_id = glossary.insert_glossary_sense(term_id, context_label, status="approved")
        if proposed_slovak:
            glossary.write_human_rendering(sense_id, proposed_slovak, human_src_id)
        return "SENSE_ADDED"

    # Sense exists — update what changed.
    sense_id = existing["sense_id"]

    if existing["status"] != "approved":
        glossary.update_sense_status(sense_id, "approved")

    if proposed_slovak is not None:
        current_sk = glossary.get_sk_rendering_content(sense_id)
        if proposed_slovak != current_sk:
            glossary.write_human_rendering(sense_id, proposed_slovak, human_src_id)
            glossary.bump_sense_version(sense_id)  # version owned by SK content changes

    if la_surface:
        current_surface = glossary.get_la_surface(sense_id)
        if la_surface != current_surface:
            glossary.write_human_surface(sense_id, la_surface)

    return "UPDATED"


def _sort_label_updates(conn, rows: list[dict]) -> list[dict]:
    """Sort rows so a sense vacating a label comes before a sense claiming that same label.

    Without this, a label-swap within the same term (e.g. A→X and B→A where B currently
    holds A) triggers a unique constraint violation if B is processed before A.
    """
    sense_ids = [r["sense_id"] for r in rows if r["sense_id"] is not None]
    if not sense_ids:
        return rows

    current_label = GlossaryRepository(conn).get_context_labels(sense_ids)

    # label → sense_id that currently holds it (only for senses in this batch)
    holder_of: dict[str, int] = {
        lbl: sid for sid, lbl in current_label.items() if lbl is not None
    }

    result = list(rows)
    changed = True
    while changed:
        changed = False
        for i, row in enumerate(result):
            if row["sense_id"] is None:
                continue
            new_label = (row.get("context_label") or "").strip() or None
            if new_label is None:
                continue
            holder_id = holder_of.get(new_label)
            if holder_id is None or holder_id == row["sense_id"]:
                continue
            # Find the holder later in the list and move it before this row
            for j in range(i + 1, len(result)):
                if result[j].get("sense_id") == holder_id:
                    result.insert(i, result.pop(j))
                    changed = True
                    break
    return result


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
    created = sense_added = updated = 0
    conflicts: list[dict] = []

    with get_conn() as conn:
        human_src_id = source_id(conn, "human")
        approved_rows = _sort_label_updates(conn, approved_rows)
        for row in approved_rows:
            if row["sense_id"] is None:
                status = process_new_term(conn, row, human_src_id)
                if status == "CREATED":
                    created += 1
                elif status == "SENSE_ADDED":
                    sense_added += 1
                elif status == "UPDATED":
                    updated += 1
                # NO_LEMMA: silently skip
                continue

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
    print(f"  Approved (rerun triggered):  {ok} terms")
    print(f"  Skipped (already approved):  {skipped}")
    print(f"  Created (new term + sense):  {created}")
    print(f"  Sense added (new sense):     {sense_added}")
    print(f"  Updated (existing sense):    {updated}")
    print(f"  Not found:                   {not_found}")
    print(f"  Conflicts (blank version):   {conflict} (see below)")
    if conflicts:
        print("\nConflicts (db_version blank — skipped):")
        for c in conflicts:
            print(f"  sense_id={c['sense_id']}  latin={c['latin_lemma']!r}  "
                  f"sheet_version={c['db_version']}")


if __name__ == "__main__":
    run()
