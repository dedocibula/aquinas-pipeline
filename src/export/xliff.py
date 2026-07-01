"""XLIFF 2.0 export for the Aquinas pipeline.

Exports translated/needs_human segments to XLIFF 2.0 format.
One file per pars (I, I_II, II_II, III).

CLI:
    uv run python -m export.xliff                         # all pars → exports/*.xlf
    uv run python -m export.xliff --pars I I_II           # subset
    uv run python -m export.xliff --output-dir /tmp/out   # custom dir
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg2
from lxml import etree

XLIFF_NS = "urn:oasis:names:tc:xliff:document:2.0"

# Shorthand for building namespace-qualified tag names.
def _q(tag: str) -> str:
    return f"{{{XLIFF_NS}}}{tag}"


# ---------------------------------------------------------------------------
# Pure helpers — no I/O, easy to unit-test
# ---------------------------------------------------------------------------


def _unit_id(locator: str) -> str:
    """Replace dots with underscores for XML NCName compliance."""
    return locator.replace(".", "_")


def _note(parent: etree._Element, nid: str, category: str, text: str) -> None:
    el = etree.SubElement(parent, _q("note"), id=nid, category=category)
    el.text = text


def _build_unit(parent: etree._Element, row: dict) -> None:
    """Append one <unit> element to parent <file>."""
    unit = etree.SubElement(parent, _q("unit"), id=_unit_id(row["locator_path"]))
    notes_el = etree.SubElement(unit, _q("notes"))
    _note(notes_el, "n1", "locator",            row["locator_path"])
    _note(notes_el, "n2", "element_type",        row["element_type"])
    _note(notes_el, "n3", "translation_status",  row["translation_status"])
    if row.get("reviewer_notes"):
        rn = row["reviewer_notes"]
        _note(notes_el, "n4", "reviewer_notes",
              json.dumps(rn) if isinstance(rn, dict) else str(rn))
    if row.get("source_lang") == "en":
        _note(notes_el, "n5", "source_lang", "en")
    seg_el = etree.SubElement(unit, _q("segment"))
    etree.SubElement(seg_el, _q("source")).text = row.get("source_text") or ""
    etree.SubElement(seg_el, _q("target")).text = row.get("target_text") or ""


def _build_tree(rows: list[dict], pars: str) -> etree._ElementTree:
    """Build an lxml ElementTree for one pars from pre-fetched rows."""
    root = etree.Element(
        _q("xliff"),
        attrib={"version": "2.0", "srcLang": "la", "trgLang": "sk"},
        nsmap={None: XLIFF_NS},
    )
    file_el = etree.SubElement(root, _q("file"), id=pars)
    for row in rows:
        _build_unit(file_el, row)
    return etree.ElementTree(root)


# ---------------------------------------------------------------------------
# DB fetch
# ---------------------------------------------------------------------------


def _fetch_rows(conn: psycopg2.extensions.connection, work_id: int, pars: str) -> list[dict]:
    """Fetch translated/needs_human segments for one pars from v_segment."""
    sql = """
        SELECT
            segment_id,
            locator_path::text,
            element_type,
            translation_status,
            reviewer_notes,
            COALESCE(latin, english)                              AS source_text,
            CASE WHEN latin IS NULL THEN 'en' ELSE 'la' END       AS source_lang,
            COALESCE(slovak_final, slovak_polish, slovak_draft)   AS target_text
        FROM v_segment
        WHERE work_id = %s
          AND subpath(locator_path, 0, 1)::text = %s
          AND translation_status IN ('translated', 'needs_human')
        ORDER BY locator_path
    """
    with conn.cursor() as cur:
        cur.execute(sql, (work_id, pars))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def export_pars_bytes(
    conn: psycopg2.extensions.connection, work_id: int, pars: str
) -> bytes:
    """Return XLIFF 2.0 bytes for one pars — used by the server route."""
    rows = _fetch_rows(conn, work_id, pars)
    tree = _build_tree(rows, pars)
    return etree.tostring(
        tree.getroot(), xml_declaration=True, encoding="UTF-8", pretty_print=True
    )


def export_pars(
    conn: psycopg2.extensions.connection,
    work_id: int,
    pars: str,
    output_dir: Path,
) -> Path:
    """Write XLIFF 2.0 file for one pars — used by the CLI."""
    rows = _fetch_rows(conn, work_id, pars)
    tree = _build_tree(rows, pars)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{pars}.xlf"
    tree.write(str(path), xml_declaration=True, encoding="UTF-8", pretty_print=True)
    return path


def run(
    work_id: int = 1,
    pars_filter: list[str] | None = None,
    output_dir: Path = Path("exports"),
) -> None:
    from server.db import get_distinct_pars
    from storage.db import get_conn

    with get_conn() as conn:
        all_pars = get_distinct_pars(conn, work_id)
    targets = [p for p in all_pars if not pars_filter or p in pars_filter]
    if not targets:
        print("No matching pars found.")
        return
    for pars in targets:
        with get_conn() as conn:
            path = export_pars(conn, work_id, pars, output_dir)
        print(f"  wrote {path}  ({_count_units(path)} units)")


def _count_units(path: Path) -> int:
    tree = etree.parse(str(path))
    return len(tree.findall(_q("unit"), namespaces={}))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Export Aquinas corpus to XLIFF 2.0")
    p.add_argument("--pars",       nargs="+", help="Pars to export (default: all)")
    p.add_argument("--work-id",    type=int,  default=1)
    p.add_argument("--output-dir", type=Path, default=Path("exports"))
    args = p.parse_args()
    run(args.work_id, args.pars, args.output_dir)
