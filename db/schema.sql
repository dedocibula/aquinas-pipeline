-- ============================================================================
-- Aquinas → Slovak Translation Pipeline — consolidated database schema
-- ============================================================================
--
-- SINGLE SOURCE OF TRUTH for the current database shape. This file equals the
-- live schema after migrations 001–007 are applied (verified against a fresh
-- pg_dump --schema-only). Fresh setup runs THIS file; the numbered scripts in
-- migrations/archive/ are historical only — do not replay them on a new DB.
--
-- Run ONLY after human review. See CLAUDE.md "Stop for DDL".
--
-- Execution (psql is not on PATH locally; run inside the db container):
--   docker exec -i aquinas-pipeline-db-1 psql -U aquinas -d aquinas < db/schema.sql
--
-- Annotations below state each table/column's PRODUCER and CONSUMER. They
-- complement .claude/database.md (the design rationale); keep the two in sync.
-- ============================================================================

SET client_encoding = 'UTF8';

-- ── Extensions ──────────────────────────────────────────────────────────────
-- ltree  : hierarchical locator_path (safe, indexed ancestry queries).
-- vector : reserved for future semantic search; no column uses it yet.
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS vector;


-- ============================================================================
-- TABLES
-- ============================================================================

-- ── work ──────────────────────────────────────────────────────────────────
-- One row per text translated. A second Aquinas work is a new row with its own
-- structure_type — nothing downstream hardcodes "Summa" or "Latin is source".
--   Produced by: manual seed (below).
--   Consumed by: parser dispatch (structure_type); query scoping (work_id).
CREATE TABLE work (
    work_id        serial PRIMARY KEY,
    author         text NOT NULL,                  -- e.g. 'Thomas Aquinas'
    title          text NOT NULL,                  -- e.g. 'Summa Theologiae'
    structure_type text NOT NULL,                  -- 'summa_articulus'; drives parser dispatch
    source_lang    text NOT NULL                   -- 'la'; makes the source/reference split derivable
);


-- ── source ──────────────────────────────────────────────────────────────────
-- The precedence dimension. Authority order is DATA, not code.
-- authority_rank is UNIQUE on purpose: two sources at one rank would make the
-- resolver's evidence vote non-deterministic and silently break "Krystal wins".
--   Produced by: seed (below).
--   Consumed by: resolver (which evidence wins); translator prompt (which
--                reference to trust); v_segment/v_sense (source code filters).
CREATE TABLE source (
    source_id      serial PRIMARY KEY,
    code           text NOT NULL UNIQUE,           -- 'krystal','bahounek','model','human',…
    lang           text NOT NULL,                  -- language this source provides: 'la','cs','en','sk'
    kind           text NOT NULL,                  -- 'glossary'|'reference'|'source_text'|'machine'|'review'
    authority_rank integer NOT NULL UNIQUE,        -- lower = higher authority; UNIQUE enforced at DB level
    note           text
);


-- ── segment ──────────────────────────────────────────────────────────────────
-- The spine: one row per atomic translatable unit.
--   Produced by: Latin parser (creates the segment graph).
--   Consumed by: resolver; translator; reviewer structural check; re-run engine.
CREATE TABLE segment (
    segment_id         serial PRIMARY KEY,
    work_id            integer NOT NULL REFERENCES work(work_id),
    locator_path       ltree NOT NULL,             -- hierarchical coord, e.g. 'I.q3.a1.arg2'; query with <@ / ~, never LIKE
    element_type       text NOT NULL,              -- 'arg'|'sed_contra'|'respondeo'|'reply'
    reply_to           integer REFERENCES segment(segment_id),  -- objection this reply answers; kept for the reviewer's structural-fidelity check
    translation_status text NOT NULL DEFAULT 'pending'
        CHECK (translation_status IN ('pending', 'translated', 'needs_human')),  -- set by the translation loop
    reviewer_notes     jsonb                       -- advisory JSON from the R1 reviewer; NULL until translated
);


-- ── segment_text ──────────────────────────────────────────────────────────────
-- Per-language, per-source text for a segment. Languages are ROWS, not columns.
-- The UNIQUE key lets (sk,model) draft and (sk,human) final coexist for diffing.
--   Produced by: Latin parser (la,corpus_thomisticum); Bahounek (cs,bahounek);
--                English ingest (en,dominican|freddoso); translator (sk,model);
--                reviewer (sk,human).
--   Consumed by: resolver (la finds terms; cs/en are evidence); translator
--                (references + writes draft); re-run engine.
CREATE TABLE segment_text (
    segment_id integer NOT NULL REFERENCES segment(segment_id),
    lang       text NOT NULL,                      -- 'la','cs','en','sk'
    content    text NOT NULL,
    source_id  integer NOT NULL REFERENCES source(source_id),
    UNIQUE (segment_id, lang, source_id)
);


-- ── glossary_term ──────────────────────────────────────────────────────────────
-- One row per distinct Latin term (lemma); the join target after lemmatization.
-- Multiword terms must be phrase-matched BEFORE single-token lemmatization.
--   Produced by: Krystal preseed; gap terms appended during resolution.
--   Consumed by: resolver.
CREATE TABLE glossary_term (
    term_id      serial PRIMARY KEY,
    latin_lemma  text NOT NULL UNIQUE,             -- dictionary form; gap lemmas are canonicalized to lowercase
    is_multiword boolean NOT NULL DEFAULT false,   -- true for 'actus essendi', 'per se', …
    notes        text,
    category     text CHECK (category IN ('term', 'name', 'formula', 'prose')),
        -- NULL for ~150 Krystal-seeded terms (authoritative regardless of category);
        -- a model-assigned value is set for gap terms only, during the DeepSeek proposal pass.
    la_surface   text                              -- canonical Latin surface form; NULL → fall back to latin_lemma in pattern matching
);


-- ── glossary_sense ──────────────────────────────────────────────────────────────
-- One row per (term, meaning). version is the INVALIDATION ENGINE: it pairs with
-- term_usage.sense_version_used to answer "which translations are stale?" cheaply.
-- Bump version ONLY when the sk sense_rendering.content actually changes — a bare
-- status transition (proposed→approved) with no Slovak change must NOT bump.
--   Produced by: Krystal preseed (single-sense → one NULL-label row; multi-sense →
--                several labelled rows); gap terms → status='proposed'.
--   Consumed by: resolver (pick the sense); translator (via term_usage); reviewer.
CREATE TABLE glossary_sense (
    sense_id      serial PRIMARY KEY,
    term_id       integer NOT NULL REFERENCES glossary_term(term_id),
    context_label text,                            -- e.g. 'as passion'; NULL = default/only sense
    status        text NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'flagged', 'approved')),
    version       integer NOT NULL DEFAULT 1       -- increments only when the sk rendering content changes
);

-- Enforce "at most one default sense and unique labels per term" via partial uniques:
CREATE UNIQUE INDEX glossary_sense_single_unique ON glossary_sense (term_id)
    WHERE context_label IS NULL;
CREATE UNIQUE INDEX glossary_sense_multi_unique ON glossary_sense (term_id, context_label)
    WHERE context_label IS NOT NULL;


-- ── sense_rendering ──────────────────────────────────────────────────────────────
-- Per-language realization of a sense; one row per (sense, lang, source).
-- No 'la' row by original design — the Latin lemma lives in glossary_term — but the
-- lang CHECK admits 'la' (migration 006) for forward flexibility.
--   Produced by: Krystal preseed (cs anchor, en cue, sk approved); reviewer
--                overwrites the sk row with source=human.
--   Consumed by: resolver (cs lemma is the reverse-map key; cs+en are evidence);
--                translator (the sk content is the hard constraint in the prompt).
CREATE TABLE sense_rendering (
    sense_id  integer NOT NULL REFERENCES glossary_sense(sense_id),
    lang      text NOT NULL CHECK (lang IN ('cs', 'en', 'sk', 'la')),
    lemma     text,                                -- lemmatized form; populated for cs (the reverse-map key)
    content   text NOT NULL,                       -- term / cue / approved-translation text
    source_id integer NOT NULL REFERENCES source(source_id),
    CONSTRAINT sense_rendering_sense_lang_source_unique UNIQUE (sense_id, lang, source_id)
);

CREATE INDEX sense_rendering_lang_lemma_idx ON sense_rendering (lang, lemma);


-- ── term_usage ──────────────────────────────────────────────────────────────────
-- The invalidation backbone: one row per occurrence of a term in a segment.
-- term_id is intentionally omitted — derive it via sense_id → glossary_sense.term_id.
-- confidence (set once by the resolver) and status (human-process state) are
-- DISTINCT; never write a CHECK treating them as mutually exclusive.
--   Produced by: resolver, one row per term per segment.
--   Consumed by: coverage report; re-run engine; translator.
CREATE TABLE term_usage (
    usage_id           serial PRIMARY KEY,
    segment_id         integer NOT NULL REFERENCES segment(segment_id),
    sense_id           integer NOT NULL REFERENCES glossary_sense(sense_id),
    sense_version_used integer NOT NULL,           -- sense.version live when this segment was translated; vs sense.version → stale set
    resolution_method  text NOT NULL
        CHECK (resolution_method IN (
            'krystal_single', 'krystal_multi_voted', 'krystal_multi_flagged',
            'bahounek_derived', 'english_derived', 'model_proposed', 'formula_backfill')),
    confidence         text NOT NULL
        CHECK (confidence IN ('auto', 'needs_review')),
    signals            jsonb,                       -- evidence used, e.g. {"cs":"dychtění→202","en":"desire→202"}
    status             text NOT NULL DEFAULT 'guessed'
        CHECK (status IN ('guessed', 'confirmed'))
);

CREATE INDEX term_usage_segment_id_idx ON term_usage (segment_id);
CREATE INDEX term_usage_sense_id_idx ON term_usage (sense_id);


-- ── translation_run ──────────────────────────────────────────────────────────────
-- One row per translation run (run analytics). Captures the exact conditions of a
-- run so cost/quality can be attributed to a prompt/model/glossary snapshot.
--   Produced by: the translation runner (open at start, close at finish).
--   Consumed by: run analytics / reporting.
CREATE TABLE translation_run (
    run_id            serial PRIMARY KEY,
    flow_name         text NOT NULL,               -- e.g. 'pilot_sample', 'corpus'
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    git_sha           text,
    prompt_hash       text,
    glossary_snapshot jsonb,
    translator_model  text,
    reviewer_model    text,
    temperature       numeric(3, 2),
    filters           jsonb,
    max_workers       integer,
    total_segments    integer,
    total_translated  integer,
    total_needs_human integer,
    total_cost_usd    numeric(10, 4),
    jsonl_path        text
);


-- ── run_segment ──────────────────────────────────────────────────────────────────
-- Per-segment outcome within a run. Composite PK (run_id, segment_id).
--   Produced by: the translation runner, one row per segment attempted.
--   Consumed by: run analytics (failure classes, iteration/cost rollups).
CREATE TABLE run_segment (
    run_id          integer NOT NULL REFERENCES translation_run(run_id) ON DELETE CASCADE,
    segment_id      integer NOT NULL REFERENCES segment(segment_id),
    final_status    text NOT NULL
        CHECK (final_status IN ('translated', 'needs_human')),
    iterations_used integer NOT NULL,
    chosen_iteration integer,
    cost_usd        numeric(10, 6),
    failure_classes jsonb,
    last_feedback   text,
    PRIMARY KEY (run_id, segment_id)
);


-- ============================================================================
-- INDEXES (partial / supporting; co-located with their query consumer)
-- ============================================================================

-- Fast pilot/batch "what's left" scans.
CREATE INDEX idx_segment_translation_status ON segment (translation_status)
    WHERE translation_status = 'pending';

-- ltree ancestry queries (locator_path <@ 'I.q3', ~ 'I.q3.a1.*').
CREATE INDEX segment_locator_idx ON segment USING gist (locator_path);

-- Run-analytics access paths.
CREATE INDEX idx_run_segment_segment ON run_segment (segment_id);
CREATE INDEX idx_run_segment_failures ON run_segment (run_id)
    WHERE final_status = 'needs_human';


-- ============================================================================
-- VIEWS — read-convenience pivots back to column-per-language. The normalized
-- tables remain the source of truth; promote to materialized only if measured slow.
-- ============================================================================

-- Pivot segment_text to one row per segment with a column per language.
-- slovak_draft/slovak_final split the (sk,model) and (sk,human) rows.
CREATE VIEW v_segment AS
    SELECT
        s.segment_id,
        s.work_id,
        s.locator_path,
        s.element_type,
        s.reply_to,
        max(t.content) FILTER (WHERE t.lang = 'la')                      AS latin,
        max(t.content) FILTER (WHERE t.lang = 'cs')                      AS czech,
        max(t.content) FILTER (WHERE t.lang = 'en')                      AS english,
        max(t.content) FILTER (WHERE t.lang = 'sk' AND src.code = 'model') AS slovak_draft,
        max(t.content) FILTER (WHERE t.lang = 'sk' AND src.code = 'human') AS slovak_final
    FROM segment s
    JOIN segment_text t USING (segment_id)
    JOIN source src ON t.source_id = src.source_id
    GROUP BY s.segment_id, s.work_id, s.locator_path, s.element_type, s.reply_to;

-- Pivot sense_rendering to one row per sense with a column per language.
CREATE VIEW v_sense AS
    SELECT
        gs.sense_id,
        gs.term_id,
        gt.latin_lemma,
        gs.context_label,
        gs.status,
        gs.version,
        max(r.lemma)   FILTER (WHERE r.lang = 'cs') AS czech_lemma,
        max(r.content) FILTER (WHERE r.lang = 'cs') AS czech_term,
        max(r.content) FILTER (WHERE r.lang = 'en') AS english_cue,
        max(r.content) FILTER (WHERE r.lang = 'sk') AS slovak_term,
        max(src.code)  FILTER (WHERE r.lang = 'sk') AS slovak_source
    FROM glossary_sense gs
    JOIN glossary_term gt USING (term_id)
    JOIN sense_rendering r USING (sense_id)
    JOIN source src ON r.source_id = src.source_id
    GROUP BY gs.sense_id, gs.term_id, gt.latin_lemma, gs.context_label, gs.status, gs.version;


-- ============================================================================
-- SEED DATA — required for a functional fresh install (part of the schema, not
-- corpus content). The source precedence order is load, not preference.
-- ============================================================================

INSERT INTO source (code, lang, kind, authority_rank, note) VALUES
    ('human',              'sk', 'review',      1,  'Theologian reviewer'),
    ('corpus_thomisticum', 'la', 'source_text', 5,  'Corpus Thomisticum XML'),
    ('krystal',            'cs', 'glossary',    10,  'Krystal OP glossary + style rules'),
    ('bahounek',           'cs', 'reference',   20,  'Bahounek modern Czech revision'),
    ('dominican',          'en', 'reference',   30,  'Dominican Province translation'),
    ('freddoso',           'en', 'reference',   35,  'Freddoso translation (partial)'),
    ('model',              'sk', 'machine',     90,  'Model-generated draft');

INSERT INTO work (author, title, structure_type, source_lang) VALUES
    ('Thomas Aquinas', 'Summa Theologiae', 'summa_articulus', 'la');
