"""Export the M2 glossary dedup roll-up to Google Sheets.

CLI:
    uv run python -m review.export_sheet

Idempotent — safe to re-run. On re-run, reference columns (B-C, F-N) are
refreshed from DB; columns A (checkbox), D (context_label), and E (proposed_slovak)
are preserved so reviewer edits survive.

Prerequisites:
    - GSHEETS_SPREADSHEET_ID set in environment
    - .secrets/gsheets_service_account.json with Sheets API access
"""

from __future__ import annotations

import psycopg2.extras

from common.db import get_conn
from review.sheets import (
    apply_checkbox_validation,
    authenticate,
    batch_write_rows,
    delete_stale_rows,
    get_or_create_worksheet,
    get_spreadsheet_id,
    read_existing_rows_from_data,
    write_header,
)

# Export SQL — fetches everything needed for the sheet in one query.
# Wrapped as a CTE so the outer WHERE can reference the computed tu_agg columns.
_EXPORT_SQL = """
WITH base AS (
    SELECT
        gt.term_id,
        gs.sense_id,
        gt.latin_lemma,
        gt.category,
        gs.context_label,
        sr_sk.content       AS proposed_slovak,
        st_la.content       AS latin_occurrence,
        st_cs.content       AS czech_occurrence,
        st_en.content       AS english_occurrence,
        tu_agg.method       AS resolution_method,
        tu_agg.freq         AS frequency,
        tu_agg.sample       AS sample_locator,
        gs.status,
        gs.version,
        dense_rank() OVER (
            PARTITION BY gt.category
            ORDER BY sr_sk.content
        )                   AS group_id
    FROM glossary_term gt
    JOIN glossary_sense gs ON gs.term_id = gt.term_id
    LEFT JOIN sense_rendering sr_sk
           ON sr_sk.sense_id = gs.sense_id AND sr_sk.lang = 'sk'
    LEFT JOIN (
        SELECT tu.sense_id,
               mode() WITHIN GROUP (ORDER BY tu.resolution_method)              AS method,
               count(*)                                                          AS freq,
               min(s.locator_path::text)                                        AS sample,
               -- segment_id of the earliest-path occurrence, for context join below
               (array_agg(s.segment_id ORDER BY s.locator_path::text))[1]      AS sample_segment_id
        FROM term_usage tu
        JOIN segment s USING (segment_id)
        GROUP BY tu.sense_id
    ) tu_agg ON tu_agg.sense_id = gs.sense_id
    LEFT JOIN segment_text st_la
           ON st_la.segment_id = tu_agg.sample_segment_id AND st_la.lang = 'la'
    LEFT JOIN segment_text st_cs
           ON st_cs.segment_id = tu_agg.sample_segment_id AND st_cs.lang = 'cs'
    LEFT JOIN segment_text st_en
           ON st_en.segment_id = tu_agg.sample_segment_id AND st_en.lang = 'en'
    ORDER BY
        CASE gt.category
            WHEN 'term'    THEN 1
            WHEN 'name'    THEN 2
            WHEN 'formula' THEN 3
            WHEN 'prose'   THEN 4
            ELSE 5
        END,
        CASE gs.status WHEN 'flagged' THEN 1 ELSE 2 END,
        tu_agg.freq DESC NULLS LAST,
        sr_sk.content,
        gt.latin_lemma
)
SELECT * FROM base
WHERE {where_clause}
"""

_WHERE_MAIN = (
    "status = 'flagged' "
    "OR (category IN ('term', 'formula') AND ("
    "    resolution_method IS NULL OR resolution_method != 'krystal_single' "
    "    OR status != 'approved'"
    "))"
)
_WHERE_AUTO = "resolution_method = 'krystal_single' AND status = 'approved'"


def _fetch_rows(conn, where_clause: str) -> list[dict]:
    sql = _EXPORT_SQL.format(where_clause=where_clause)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return cur.fetchall()


def fetch_main_rows(conn) -> list[dict]:
    """Fetch non-krystal_single/approved rows for the Review tab."""
    return _fetch_rows(conn, _WHERE_MAIN)


def fetch_auto_resolved_rows(conn) -> list[dict]:
    """Fetch krystal_single/approved rows for the Auto-resolved tab."""
    return _fetch_rows(conn, _WHERE_AUTO)


def rows_to_sheet_values(rows: list[dict]) -> list[list]:
    """Convert DB rows to 14-element lists matching the sheet column layout."""
    result = []
    for r in rows:
        result.append([
            False,                                                      # A — checkbox
            r["category"] or "",                                        # B
            r["latin_lemma"] or "",                                     # C
            r["context_label"] or "",                                   # D
            r["proposed_slovak"] or "",                                 # E
            r["latin_occurrence"] or "",                                # F
            r["czech_occurrence"] or "",                                # G
            r["english_occurrence"] or "",                              # H
            r["resolution_method"] or "",                               # I
            r["frequency"] if r["frequency"] is not None else "",       # J
            r["sample_locator"] or "",                                  # K
            r["sense_id"],                                              # L — hidden
            r["group_id"] if r["group_id"] is not None else "",        # M — hidden
            r["version"] if r["version"] is not None else "",          # N — hidden
        ])
    return result


def export_tab(
    spreadsheet,
    title: str,
    db_rows: list[dict],
    *,
    apply_checkbox: bool = True,
) -> int:
    """Write db_rows to the named worksheet. Returns number of data rows written.

    Uses a single get_all_values() call to both check the header and build the
    existing-row map, avoiding a redundant API round-trip.
    """
    ws = get_or_create_worksheet(spreadsheet, title)

    # One API call covers both header check and existing-row map.
    all_values = ws.get_all_values()
    header_written = write_header(ws, existing_values=all_values)
    if header_written:
        # Header changed — stale rows have the old column layout; remove them
        # before inserting fresh so we don't leave misaligned data behind.
        if len(all_values) > 1:
            ws.delete_rows(2, len(all_values))
        existing_map = {}
    else:
        existing_map = read_existing_rows_from_data(all_values)

    sheet_values = rows_to_sheet_values(db_rows)
    batch_write_rows(ws, sheet_values, existing_map)

    # Remove rows whose sense_id is no longer in the DB (e.g. after glossary trim).
    db_sense_ids = {r["sense_id"] for r in db_rows}
    n_deleted = delete_stale_rows(spreadsheet, ws, existing_map, db_sense_ids)
    if n_deleted:
        print(f"  Deleted {n_deleted} stale rows from '{title}'.")

    total_rows = len(sheet_values)
    if apply_checkbox and total_rows > 0:
        apply_checkbox_validation(spreadsheet, ws, total_rows)

    return total_rows


def run() -> None:
    spreadsheet_id = get_spreadsheet_id()
    client = authenticate()
    spreadsheet = client.open_by_key(spreadsheet_id)

    with get_conn() as conn:
        main_rows = fetch_main_rows(conn)
        auto_rows = fetch_auto_resolved_rows(conn)

    n_main = export_tab(spreadsheet, "Review", main_rows, apply_checkbox=True)
    n_auto = export_tab(spreadsheet, "Auto-resolved", auto_rows, apply_checkbox=False)

    print(f"Export complete: {n_main} main rows, {n_auto} auto-resolved rows.")


if __name__ == "__main__":
    run()
