-- M1 schema corrections — apply before Step 2 (Krystal preseed).
--
-- Fixes identified in code review:
--   1. sense_rendering: add UNIQUE(sense_id, lang, source_id) — preseed idempotency + view safety
--   2. segment_locator_idx: replace btree with gist — ltree <@ and ~ queries require gist
--   3. sense_rendering: add CHECK(lang IN ('cs','en','sk')) — enforce no-la-row invariant at DB
--   4. glossary_sense: add UNIQUE(term_id, context_label) — prevent duplicate single-sense rows
--   5. term_usage: add indexes on sense_id and segment_id — re-run query + provenance report
--
-- Run:
--   docker cp migrations/002_schema_fixes.sql aquinas-pipeline-db-1:/tmp/002_schema_fixes.sql
--   docker exec aquinas-pipeline-db-1 psql -U aquinas -d aquinas -f /tmp/002_schema_fixes.sql

-- 1. sense_rendering UNIQUE constraint
--    (no existing rows yet so no conflict risk)
ALTER TABLE sense_rendering
    ADD CONSTRAINT sense_rendering_sense_lang_source_unique
    UNIQUE (sense_id, lang, source_id);

-- 2. Replace btree segment locator index with gist
--    btree does not accelerate <@ (ancestor) or ~ (lquery) operators
DROP INDEX IF EXISTS segment_locator_idx;
CREATE INDEX segment_locator_idx ON segment USING gist (locator_path);

-- 3. lang CHECK on sense_rendering — enforce the "no la row" invariant
ALTER TABLE sense_rendering
    ADD CONSTRAINT sense_rendering_lang_check
    CHECK (lang IN ('cs', 'en', 'sk'));

-- 4. Prevent duplicate (term_id, context_label) rows in glossary_sense
--    Handles both the NULL case (single-sense) and labelled multi-sense cases.
--    PostgreSQL treats NULL as distinct in UNIQUE, so two NULL rows for the
--    same term_id would pass a plain UNIQUE. Use a partial index for the NULL
--    case and a standard UNIQUE for the non-NULL case.
CREATE UNIQUE INDEX glossary_sense_single_unique
    ON glossary_sense (term_id)
    WHERE context_label IS NULL;

CREATE UNIQUE INDEX glossary_sense_multi_unique
    ON glossary_sense (term_id, context_label)
    WHERE context_label IS NOT NULL;

-- 5. Indexes for re-run query and provenance report
CREATE INDEX term_usage_sense_id_idx    ON term_usage (sense_id);
CREATE INDEX term_usage_segment_id_idx  ON term_usage (segment_id);
