# Plan: Preamble Ingestion

## Coverage (verified)

| Source | Questions with preamble | Total questions | Coverage |
|---|---|---|---|
| Latin (Corpus Thomisticum) | 510 | 512 | 99.6% |
| Czech (Bahounek) | 503 | 510 | 98.6% |
| English (Dominican) | — | — | n/a — see below |

**Latin gaps:** I.q71, I.q72 only.
**Bahounek gaps:** ~7 questions (exact set derivable by comparing against Latin set).
**Aquinas.cc summaries:** editorial additions, not Aquinas's text. CT preamble IS Aquinas's
text and is already in our source files. Not worth ingesting.

## No schema migration required

`element_type` has no CHECK constraint in the DB. `translation_status` defaults to `'pending'`.

---

## Step 1 — Latin parser: extract and insert preambles

File: `src/ingest/parser_latin.py`

- Add `_collect_preambles(html_path) -> list[ParsedElement]`: scans a CT HTML file and
  returns `ParsedElement` objects where `element_type == 'preamble'` (locators like
  `I.q3.preamble`). `_parse_title_full` already matches `q. N pr.` — just collect instead
  of dropping.
- Add `_insert_preamble(conn, preamble_elem, work_id, src_id)`: inserts one `segment` row
  at `I.q3.preamble` with `element_type='preamble'` and one `segment_text(la)` row.
  Idempotent: delete-then-insert by exact `locator_path`.
- In `run_full`: after the article pass, add a second pass over all HTML files calling
  `_collect_preambles` then `_insert_preamble`. Log preamble count to anomaly log.
  Commit per preamble.
- Update the `run()` test-mode entrypoint to also insert preambles for the 8 test articles.

---

## Step 2 — Czech parser: include preambles in ingest

File: `src/ingest/parser_bahounek.py`

- `_extract_elements_from_file` and `_parse_coord` already emit
  `BahouněkElement(locator='I.q3.preamble', ...)` correctly — no change there.
- `parse_bahounek_for_articles`: currently builds `parent_q_locs` for question_title lookup.
  Extend it to also yield preamble elements for those same parent questions:
  `all_elements.get(f"{q_loc}.preamble")`.
- `insert_bahounek_texts` already looks up by `locator_path` and will find the new segment
  rows — no changes there.
- Add a `run_full` mode (or extend the existing one) that ingests Czech preamble text for
  all questions in each pars file. Expected: ~503 rows.

---

## Step 3 — English: do NOT ingest

The Dominican `<ol>` before each question's first article is an article-index list (question
form), not a prose preamble. It is not a translation of the Latin preamble. English reference
text will be NULL for preamble segments — Latin + Czech is sufficient for translation context.

If this decision is revisited, the `<ol>` items could be concatenated and stored, but they
would need a flag distinguishing them from translated prose (they would be misleading as
translation reference).

---

## Step 4 — Verify: confirm Latin and Czech align

After Steps 1–2, run:

```sql
SELECT
  s.locator_path::text,
  max(t.content) FILTER (WHERE t.lang='la') IS NOT NULL AS has_latin,
  max(t.content) FILTER (WHERE t.lang='cs') IS NOT NULL AS has_czech
FROM segment s
JOIN segment_text t USING (segment_id)
WHERE s.element_type = 'preamble'
GROUP BY s.locator_path
HAVING max(t.content) FILTER (WHERE t.lang='la') IS NULL
    OR max(t.content) FILTER (WHERE t.lang='cs') IS NULL
ORDER BY s.locator_path;
```

Expected: zero rows, or only the known gaps (I.q71/I.q72 Latin missing; ~7 Bahounek gaps).
Any unexpected NULL is a parser bug.

---

## Step 5 — Translation: no changes needed

Preamble segments will be `translation_status = 'pending'` by default and have Latin text.
`run.py` and `pilot.py` already exclude only `question_title` and `article_title` —
preambles are picked up by `translate_corpus` automatically. Precheck and reviewer work on
Latin text presence, which preambles have.
