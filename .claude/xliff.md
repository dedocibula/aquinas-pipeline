# Plan: XLIFF 2.0 Export — CLI + per-pars index buttons

## Context
M5 Step 4. The theological editor needs a standard portable format (XLIFF 2.0) to review
the Slovak translations in OmegaT / Lokalise / a plain text editor. Export only (no write-back).
One file per pars (I, I_II, II_II, III). The index page gets one download button per pars
section, visible only to editors. The CLI path is the canonical entry point; the server
button is a convenience wrapper.

`v_segment` currently exposes only `slovak_draft` (model) and `slovak_final` (human).
Migration 011 adds `slovak_polish` so the export COALESCE can follow the full
precedence order: human → polish → model.

---

## Files to create / modify

| Path | Action |
|---|---|
| `migrations/011_v_segment_polish.sql` | new — recreates `v_segment` with `slovak_polish` |
| `src/export/__init__.py` | new, empty |
| `src/export/xliff.py` | new — export module + CLI `__main__` |
| `tests/export/__init__.py` | new, empty |
| `tests/export/test_xliff.py` | new — 10 unit tests, no DB |
| `src/server/app.py` | add `GET /export/<pars>` route |
| `src/server/db.py` | add `get_distinct_pars(conn, work_id)` |
| `src/server/templates/index.html` | add per-pars download button |
| `src/server/static/style.css` | add `.btn-export` style |

---

## Step 1 — Migration 011 (DDL — pause for human review before applying)

`migrations/011_v_segment_polish.sql`:

```sql
-- Migration 011: add slovak_polish column to v_segment
-- Adds the polish source (code='polish', authority_rank=85) to the read view.
-- Also adds translation_status and reviewer_notes (on segment table, not aggregated).
DROP VIEW IF EXISTS v_segment;
CREATE VIEW v_segment AS
  SELECT
    s.segment_id,
    s.work_id,
    s.locator_path,
    s.element_type,
    s.reply_to,
    s.translation_status,
    s.reviewer_notes,
    max(t.content) FILTER (WHERE t.lang='la')                         AS latin,
    max(t.content) FILTER (WHERE t.lang='cs')                         AS czech,
    max(t.content) FILTER (WHERE t.lang='en')                         AS english,
    max(t.content) FILTER (WHERE t.lang='sk' AND src.code='model')    AS slovak_draft,
    max(t.content) FILTER (WHERE t.lang='sk' AND src.code='polish')   AS slovak_polish,
    max(t.content) FILTER (WHERE t.lang='sk' AND src.code='human')    AS slovak_final
  FROM segment s
  JOIN segment_text t   USING (segment_id)
  JOIN source     src   ON t.source_id = src.source_id
  GROUP BY s.segment_id, s.work_id, s.locator_path,
           s.element_type, s.reply_to,
           s.translation_status, s.reviewer_notes;
```

Note: also update `database.md` after applying to reflect the two new columns.

---

## Step 2 — `src/export/xliff.py`

### Internal helpers (pure / no I/O — easy to unit-test)

```python
from lxml import etree  # existing project style (src/acquire/latin.py:31)
import json

XLIFF_NS = "urn:oasis:names:tc:xliff:document:2.0"

def _unit_id(locator: str) -> str:
    return locator.replace(".", "_")

def _note(parent, nid, category, text):
    el = etree.SubElement(parent, "note", id=nid, category=category)
    el.text = text

def _build_unit(parent: etree._Element, row: dict) -> None:
    unit = etree.SubElement(parent, "unit", id=_unit_id(row["locator_path"]))
    notes_el = etree.SubElement(unit, "notes")
    _note(notes_el, "n1", "locator",           row["locator_path"])
    _note(notes_el, "n2", "element_type",       row["element_type"])
    _note(notes_el, "n3", "translation_status", row["translation_status"])
    if row.get("reviewer_notes"):
        rn = row["reviewer_notes"]
        _note(notes_el, "n4", "reviewer_notes",
              json.dumps(rn) if isinstance(rn, dict) else str(rn))
    if row["source_lang"] == "en":
        _note(notes_el, "n5", "source_lang", "en")
    seg_el = etree.SubElement(unit, "segment")
    etree.SubElement(seg_el, "source").text = row["source_text"] or ""
    etree.SubElement(seg_el, "target").text = row["target_text"] or ""
```

### DB fetch

```python
def _fetch_rows(conn, work_id: int, pars: str) -> list[dict]:
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
```

### Tree builder (shared by CLI and server)

```python
def _build_tree(rows: list[dict], pars: str) -> etree._ElementTree:
    root = etree.Element("xliff",
                         xmlns=XLIFF_NS, version="2.0",
                         srcLang="la", trgLang="sk")
    file_el = etree.SubElement(root, "file", id=pars)
    for row in rows:
        _build_unit(file_el, row)
    return etree.ElementTree(root)

def export_pars_bytes(conn, work_id: int, pars: str) -> bytes:
    """Return XLIFF 2.0 bytes for one pars — used by the server route."""
    rows = _fetch_rows(conn, work_id, pars)
    tree = _build_tree(rows, pars)
    return etree.tostring(tree.getroot(),
                          xml_declaration=True, encoding="UTF-8", pretty_print=True)

def export_pars(conn, work_id: int, pars: str, output_dir: Path) -> Path:
    """Write XLIFF 2.0 file for one pars — used by the CLI."""
    rows = _fetch_rows(conn, work_id, pars)
    tree = _build_tree(rows, pars)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{pars}.xlf"
    tree.write(str(path), xml_declaration=True, encoding="UTF-8", pretty_print=True)
    return path
```

### `run()` + `__main__`

```python
def run(work_id: int = 1, pars_filter: list[str] | None = None,
        output_dir: Path = Path("exports")) -> None:
    from server.db import get_distinct_pars
    from storage.db import get_conn
    with get_conn() as conn:
        all_pars = get_distinct_pars(conn, work_id)
    targets = [p for p in all_pars if not pars_filter or p in pars_filter]
    for pars in targets:
        with get_conn() as conn:
            path = export_pars(conn, work_id, pars, output_dir)
        print(f"  wrote {path}")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--pars",       nargs="+")
    p.add_argument("--work-id",    type=int, default=1)
    p.add_argument("--output-dir", type=Path, default=Path("exports"))
    args = p.parse_args()
    run(args.work_id, args.pars, args.output_dir)
```

---

## Step 3 — `src/server/db.py`

Add one helper (used by both `run()` in xliff.py and the server route):

```python
def get_distinct_pars(conn, work_id: int) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT subpath(locator_path, 0, 1)::text"
            " FROM segment WHERE work_id = %s ORDER BY 1",
            (work_id,),
        )
        return [r[0] for r in cur.fetchall()]
```

---

## Step 4 — `src/server/app.py`

Add `send_file` to the flask import line (or use `current_app.response_class`).
Add `from export.xliff import export_pars_bytes` and `get_distinct_pars` to db imports.

New route (editor-only; button hidden for non-editors in template):

```python
@app.route("/export/<pars>")
@requires_editor
def export_xliff(pars: str):
    with get_conn() as conn:
        valid = get_distinct_pars(conn, 1)
    if pars not in valid:
        abort(404)
    with get_conn() as conn:
        data = export_pars_bytes(conn, 1, pars)
    return current_app.response_class(
        data,
        mimetype="application/xliff+xml",
        headers={"Content-Disposition": f'attachment; filename="{pars}.xlf"'},
    )
```

---

## Step 5 — `src/server/templates/index.html`

Add download link inside each pars `<h2>`, shown only to editors:

```html
<h2>
  Pars {{ pars }}
  {% if is_editor %}
    <a href="/export/{{ pars }}" class="btn-export">↓ XLIFF</a>
  {% endif %}
</h2>
```

---

## Step 6 — `src/server/static/style.css`

Add after the `.btn-approve` block (~line 330):

```css
.btn-export {
  display: inline-block;
  margin-left: 0.75rem;
  padding: 0.15rem 0.5rem;
  font-size: 0.75rem;
  font-weight: 600;
  border-radius: 3px;
  border: 1px solid #b0c4de;
  color: #1a3a5c;
  background: #dce8f5;
  text-decoration: none;
  vertical-align: middle;
}
.btn-export:hover {
  background: #c2d8ee;
}
```

---

## Step 7 — `tests/export/test_xliff.py` (10 tests, no DB)

Monkey-patch `_fetch_rows`. Use `lxml.etree` to parse output and assert structure.

| Test | Checks |
|---|---|
| `test_unit_id` | dots → underscores |
| `test_unit_id_no_dots` | passthrough for simple strings |
| `test_build_unit_human_preferred` | `target_text=slovak_final` in `<target>` |
| `test_build_unit_model_fallback` | `target_text=None` → `<target>` is empty string |
| `test_build_unit_english_source` | `source_lang='en'` → note with `category="source_lang"` present |
| `test_build_unit_reviewer_notes_null` | no `reviewer_notes` note element emitted |
| `test_build_unit_reviewer_notes_present` | note with `category="reviewer_notes"` in output |
| `test_export_pars_xml_valid` | root tag `xliff`, `version="2.0"`, `srcLang="la"`, `trgLang="sk"` |
| `test_export_pars_unit_count` | N rows → N `<unit>` elements |
| `test_export_pars_filename` | output file named `{pars}.xlf` under `tmp_path` |

---

## Verification

```bash
# 1. Write migration file; pause for review before applying:
#    uv run python -m storage.apply_migration migrations/011_v_segment_polish.sql
#    (or psql < migrations/011_v_segment_polish.sql)

# 2. Run new tests
uv run pytest tests/export/ -q

# 3. Full suite
uv run pytest -q

# 4. CLI smoke test (after migration applied)
uv run python -m export.xliff --pars I --output-dir /tmp/xliff_test
# → /tmp/xliff_test/I.xlf; open and confirm <xliff> root, <unit> elements, Slovak text

# 5. Server: start server, log in as editor, visit index page
#    → each pars section has "↓ XLIFF" link
#    → click → browser downloads {pars}.xlf
```
