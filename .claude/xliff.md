Plan: XLIFF 2.0 Export

 Context

 M5 Step 4. The corpus translation is in progress; the theological editor needs a standard
 portable format to review, correct, and approve the Slovak output. XLIFF 2.0 is the target
 — readable by OmegaT, Lokalise, and plain text editors. Export only (no write-back yet).
 All element types included. One file per tractate (pars).

 Files to create

 ┌────────────────────────────┬─────────────────────────────────┐
 │            Path            │             Purpose             │
 ├────────────────────────────┼─────────────────────────────────┤
 │ src/export/__init__.py     │ empty package marker            │
 ├────────────────────────────┼─────────────────────────────────┤
 │ src/export/xliff.py        │ export module + CLI entry point │
 ├────────────────────────────┼─────────────────────────────────┤
 │ tests/export/__init__.py   │ empty                           │
 ├────────────────────────────┼─────────────────────────────────┤
 │ tests/export/test_xliff.py │ unit tests                      │
 └────────────────────────────┴─────────────────────────────────┘

 exports/ directory at repo root — created by the module on first run (mkdir(parents=True,
 exist_ok=True)).

 Key design decisions

 Unit ID: locator_path dots are illegal in XML NCNames. Replace with underscores:
 I.q1.a1.arg1 → I_q1_a1_arg1. Store original locator in <note category="locator">.

 Target text: COALESCE(slovak_final, slovak_draft) — human edit wins over model draft.
 v_segment already provides both columns, no extra join needed.

 Source text: COALESCE(latin, english) — article_title / question_title segments have
 no Latin; fall back to English. Add <note category="source_lang">en</note> when English
 is used, so the editor sees the flag.

 Scope: only translation_status IN ('translated', 'needs_human') — pending segments
 have no target text and would confuse the editor. Pending will fill in as the corpus run
 progresses; re-export is idempotent.

 Chunking: one file per pars (I.xlf, I_II.xlf, II_II.xlf, III.xlf). No
 per-question chunking — XLIFF tools handle large files; editor workflow not yet defined.

 Implementation

 SQL (uses existing v_segment view)

 SELECT
     segment_id,
     locator_path::text AS locator_path,
     element_type,
     translation_status,
     reviewer_notes,
     COALESCE(latin, english)         AS source_text,
     CASE WHEN latin IS NULL
          THEN 'en' ELSE 'la' END     AS source_lang,
     COALESCE(slovak_final, slovak_draft) AS target_text
 FROM v_segment
 WHERE work_id = %s
   AND subpath(locator_path, 0, 1)::text = %s   -- pars filter
   AND translation_status IN ('translated', 'needs_human')
 ORDER BY locator_path

 XLIFF 2.0 structure per file

 <?xml version="1.0" encoding="UTF-8"?>
 <xliff xmlns="urn:oasis:names:tc:xliff:document:2.0"
        version="2.0" srcLang="la" trgLang="sk">
   <file id="I">
     <unit id="I_q1_a1_arg1">
       <notes>
         <note id="n1" category="locator">I.q1.a1.arg1</note>
         <note id="n2" category="element_type">arg</note>
         <note id="n3" category="translation_status">translated</note>
         <!-- only when not null: -->
         <note id="n4" category="reviewer_notes">{"iteration":2,...}</note>
         <!-- only when source is English: -->
         <note id="n5" category="source_lang">en</note>
       </notes>
       <segment>
         <source>Latin text…</source>
         <target>Slovak text…</target>
       </segment>
     </unit>
   </file>
 </xliff>

 Module structure (src/export/xliff.py)

 def _unit_id(locator: str) -> str:
     return locator.replace(".", "_")

 def _build_unit(parent, row: dict, note_src_lang: bool) -> None:
     # lxml etree element construction; appends <unit> to parent <file>

 def export_pars(conn, work_id: int, pars: str, output_dir: Path) -> Path:
     # fetch rows, build lxml tree, write to output_dir / f"{pars}.xlf"
     # returns path written

 def run(work_id: int = 1, pars_filter: list[str] | None = None,
         output_dir: Path = Path("exports")) -> None:
     # discovers pars from DB if pars_filter is None
     # calls export_pars() per pars, prints summary

 # __main__ block: argparse --pars (nargs="+") --work-id --output-dir

 Use from lxml import etree (existing project style — see src/acquire/latin.py:31).
 Use lxml.etree.ElementTree.write(path, xml_declaration=True, encoding="UTF-8", pretty_print=True).

 CLI

 uv run python -m export.xliff                    # all pars → exports/*.xlf
 uv run python -m export.xliff --pars I I_II      # subset
 uv run python -m export.xliff --output-dir /tmp/review

 Tests (tests/export/test_xliff.py)

 Use pytest, tmp_path, no DB — pass synthetic row dicts to _build_unit and
 export_pars (monkey-patch _fetch_rows). Follow pattern in
 tests/review/test_review_steps.py.

 ┌────────────────────────────────────────┬───────────────────────────────────────────────────────────┐
 │                  Test                  │                      What it checks                       │
 ├────────────────────────────────────────┼───────────────────────────────────────────────────────────┤
 │ test_unit_id                           │ dots → underscores                                        │
 ├────────────────────────────────────────┼───────────────────────────────────────────────────────────┤
 │ test_unit_id_no_dots                   │ passthrough for simple strings                            │
 ├────────────────────────────────────────┼───────────────────────────────────────────────────────────┤
 │ test_build_unit_human_preferred        │ slovak_final wins over slovak_draft                       │
 ├────────────────────────────────────────┼───────────────────────────────────────────────────────────┤
 │ test_build_unit_model_fallback         │ slovak_final NULL → uses slovak_draft                     │
 ├────────────────────────────────────────┼───────────────────────────────────────────────────────────┤
 │ test_build_unit_english_source         │ latin NULL → english used + source_lang note present      │
     │    │                                                                                              │
     │ ├────────────────────────────────────────┼─────────────────────────────────────────────────────── │
     │ ────┤                                                                                             │
     │ │ test_build_unit_model_fallback         │ slovak_final NULL → uses slovak_draft                  │
     │    │                                                                                              │
     │ ├────────────────────────────────────────┼─────────────────────────────────────────────────────── │
     │ ────┤                                                                                             │
     │ │ test_build_unit_english_source         │ latin NULL → english used + source_lang note present   │
     │    │                                                                                              │
     │ ├────────────────────────────────────────┼─────────────────────────────────────────────────────── │
     │ ────┤                                                                                             │
     │ │ test_build_unit_reviewer_notes_null    │ no reviewer_notes note element emitted                 │
     │    │                                                                                              │
     │ ├────────────────────────────────────────┼─────────────────────────────────────────────────────── │
     │ ────┤                                                                                             │
     │ │ test_build_unit_reviewer_notes_present │ note with category="reviewer_notes" emitted            │
     │    │                                                                                              │
     │ ├────────────────────────────────────────┼─────────────────────────────────────────────────────── │
     │ ────┤                                                                                             │
     │ │ test_export_pars_xml_valid             │ output parses as valid XML; root tag, version,         │
     │ srcLang,   │                                                                                      │
     │ │                                        │ trgLang                                                │
     │    │                                                                                              │
     │ ├────────────────────────────────────────┼─────────────────────────────────────────────────────── │
     │ ────┤                                                                                             │
     │ │ test_export_pars_unit_count            │ N input rows → N <unit> elements                       │
     │    │                                                                                              │
     │ ├────────────────────────────────────────┼─────────────────────────────────────────────────────── │
     │ ────┤                                                                                             │
     │ │ test_export_pars_filename              │ output file named {pars}.xlf under output_dir          │
  