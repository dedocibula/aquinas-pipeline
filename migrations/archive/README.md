# Archived migrations — historical only

These are the original incremental migrations (`001`–`007`) that built the
database one change at a time. They are kept for provenance **only**.

**Do not replay them on a new database.** The single source of truth for the
current schema is [`db/schema.sql`](../../db/schema.sql), which equals the live
schema after all of these were applied (verified against a fresh
`pg_dump --schema-only`). A fresh install loads `db/schema.sql` and nothing here.

| file | change |
|---|---|
| `001_initial.sql` | initial schema + seed data (sources, work) |
| `002_schema_fixes.sql` | post-initial corrections |
| `003_term_category.sql` | `glossary_term.category` |
| `004_translation_status.sql` | `segment.translation_status` + `reviewer_notes` |
| `005_run_analytics.sql` | `translation_run` + `run_segment` |
| `006_sense_rendering_la_lang.sql` | allow `lang='la'` in `sense_rendering` |
| `007_glossary_term_la_surface.sql` | `glossary_term.la_surface` (+ backfill) |
