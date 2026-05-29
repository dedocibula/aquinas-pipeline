-- M1 initial schema migration
-- Run ONLY after human review. See CLAUDE.md "Stop for DDL".
--
-- Execution:
--   psql -h localhost -U aquinas -d aquinas -f migrations/001_initial.sql

-- ── Extensions ───────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS vector;

-- ── work ─────────────────────────────────────────────────────────────────────
-- One row per text translated.
-- source_lang='la' means we never hardcode "Latin is the source" elsewhere.

CREATE TABLE work (
    work_id       serial PRIMARY KEY,
    author        text   NOT NULL,
    title         text   NOT NULL,
    structure_type text  NOT NULL,   -- 'summa_articulus'; drives parser dispatch
    source_lang   text   NOT NULL    -- 'la'
);

-- ── source ────────────────────────────────────────────────────────────────────
-- The authority-precedence dimension. Lower authority_rank = higher authority.
-- UNIQUE on authority_rank: two sources sharing a rank makes the resolver
-- evidence-vote non-deterministic, violating the "Krystal wins" principle.

CREATE TABLE source (
    source_id      serial PRIMARY KEY,
    code           text   NOT NULL UNIQUE,
    lang           text   NOT NULL,
    kind           text   NOT NULL,  -- 'glossary'|'reference'|'source_text'|'machine'|'review'
    authority_rank int    NOT NULL UNIQUE,
    note           text
);

-- ── segment ───────────────────────────────────────────────────────────────────
-- The spine. One row per atomic translatable unit.
-- locator_path is ltree: use <@ for ancestry, ~ for pattern; never LIKE.

CREATE TABLE segment (
    segment_id    serial PRIMARY KEY,
    work_id       int    NOT NULL REFERENCES work(work_id),
    locator_path  ltree  NOT NULL,
    element_type  text   NOT NULL,   -- value owned by parser; e.g. 'arg','sed_contra','respondeo','reply' for Summa
    reply_to      int    REFERENCES segment(segment_id)   -- NULL except for reply elements
);

CREATE INDEX segment_locator_idx ON segment USING btree (locator_path);

-- ── segment_text ─────────────────────────────────────────────────────────────
-- Per-language, per-source text. Languages are rows, not columns.
-- UNIQUE allows (sk,model) and (sk,human) to coexist for diff/audit purposes.

CREATE TABLE segment_text (
    segment_id  int  NOT NULL REFERENCES segment(segment_id),
    lang        text NOT NULL,
    content     text NOT NULL,
    source_id   int  NOT NULL REFERENCES source(source_id),
    UNIQUE (segment_id, lang, source_id)
);

-- ── glossary_term ─────────────────────────────────────────────────────────────
-- One row per distinct Latin lemma (dictionary form).
-- is_multiword=true entries must be phrase-matched BEFORE single-token lemmatization.

CREATE TABLE glossary_term (
    term_id       serial PRIMARY KEY,
    latin_lemma   text   NOT NULL UNIQUE,
    is_multiword  bool   NOT NULL DEFAULT false,
    notes         text
);

-- ── glossary_sense ────────────────────────────────────────────────────────────
-- One row per (term, meaning). A single-sense term has one row (context_label NULL).
-- version bumps ONLY when sense_rendering(sk).content changes — not on status
-- transitions. This prevents spurious re-runs when a reviewer confirms without
-- changing the term.

CREATE TABLE glossary_sense (
    sense_id       serial PRIMARY KEY,
    term_id        int    NOT NULL REFERENCES glossary_term(term_id),
    context_label  text,              -- NULL = default/only sense
    status         text   NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'flagged', 'approved')),
    version        int    NOT NULL DEFAULT 1
);

-- ── sense_rendering ───────────────────────────────────────────────────────────
-- Per-language realization of a sense. lang IN ('cs','en','sk') only.
-- No 'la' row: Latin lemma lives in glossary_term.latin_lemma.
-- The cs row's lemma is the reverse-map key used by the resolver.
-- The sk row's content is the hard constraint injected into the translation prompt.

CREATE TABLE sense_rendering (
    sense_id   int  NOT NULL REFERENCES glossary_sense(sense_id),
    lang       text NOT NULL,
    lemma      text,                  -- populated for lang='cs' (reverse-map key)
    content    text NOT NULL,
    source_id  int  NOT NULL REFERENCES source(source_id)
);

CREATE INDEX sense_rendering_lang_lemma_idx ON sense_rendering (lang, lemma);

-- ── term_usage ────────────────────────────────────────────────────────────────
-- The invalidation backbone. One row per term occurrence in a segment.
-- term_id is omitted: derivable via sense_id → glossary_sense.term_id.
--
-- confidence (set once at resolution time, never changes):
--   'auto'         — consistent evidence, ≥1 strong signal present
--   'needs_review' — absent/split evidence or only weak signals
--
-- status (mutable, tracks human-review process):
--   'guessed'   — initial; not confirmed by a human
--   'confirmed' — human approved
--
-- Do NOT add a CHECK that treats these as mutually exclusive.
-- confidence='auto' does NOT imply status='confirmed'.

CREATE TABLE term_usage (
    usage_id           serial PRIMARY KEY,
    segment_id         int    NOT NULL REFERENCES segment(segment_id),
    sense_id           int    NOT NULL REFERENCES glossary_sense(sense_id),
    sense_version_used int    NOT NULL,
    resolution_method  text   NOT NULL
        CHECK (resolution_method IN (
            'krystal_single',
            'krystal_multi_voted',
            'krystal_multi_flagged',
            'bahounek_derived',
            'english_derived',
            'model_proposed'
        )),
    confidence         text   NOT NULL
        CHECK (confidence IN ('auto', 'needs_review')),
    signals            jsonb,
    status             text   NOT NULL DEFAULT 'guessed'
        CHECK (status IN ('guessed', 'confirmed'))
);

-- ── Views ─────────────────────────────────────────────────────────────────────

-- Pivot segment_text back to column-per-language for application ergonomics.
-- Source of truth stays normalized; upgrade to materialized view only if measured slow.
CREATE VIEW v_segment AS
  SELECT
    s.segment_id,
    s.work_id,
    s.locator_path,
    s.element_type,
    s.reply_to,
    max(t.content) FILTER (WHERE t.lang = 'la')                       AS latin,
    max(t.content) FILTER (WHERE t.lang = 'cs')                       AS czech,
    max(t.content) FILTER (WHERE t.lang = 'en')                       AS english,
    max(t.content) FILTER (WHERE t.lang = 'sk' AND src.code = 'model') AS slovak_draft,
    max(t.content) FILTER (WHERE t.lang = 'sk' AND src.code = 'human') AS slovak_final
  FROM segment s
  JOIN segment_text t  USING (segment_id)
  JOIN source src      ON t.source_id = src.source_id
  GROUP BY s.segment_id, s.work_id, s.locator_path, s.element_type, s.reply_to;

-- Pivot sense_rendering back to column-per-language.
-- No latin_lemma_display: Latin lemma lives in glossary_term.latin_lemma.
CREATE VIEW v_sense AS
  SELECT
    gs.sense_id,
    gs.term_id,
    gt.latin_lemma,
    gs.context_label,
    gs.status,
    gs.version,
    max(r.lemma)   FILTER (WHERE r.lang = 'cs')  AS czech_lemma,
    max(r.content) FILTER (WHERE r.lang = 'cs')  AS czech_term,
    max(r.content) FILTER (WHERE r.lang = 'en')  AS english_cue,
    max(r.content) FILTER (WHERE r.lang = 'sk')  AS slovak_term,
    max(src.code)  FILTER (WHERE r.lang = 'sk')  AS slovak_source
  FROM glossary_sense gs
  JOIN glossary_term gt    USING (term_id)
  JOIN sense_rendering r   USING (sense_id)
  JOIN source src          ON r.source_id = src.source_id
  GROUP BY gs.sense_id, gs.term_id, gt.latin_lemma, gs.context_label, gs.status, gs.version;

-- ── Seed data ─────────────────────────────────────────────────────────────────

INSERT INTO source (code, lang, kind, authority_rank, note) VALUES
  ('human',              'sk', 'review',      1,  'Theologian reviewer'),
  ('corpus_thomisticum', 'la', 'source_text',  5,  'Corpus Thomisticum XML'),
  ('krystal',            'cs', 'glossary',    10,  'Krystal OP glossary + style rules'),
  ('bahounek',           'cs', 'reference',   20,  'Bahounek modern Czech revision'),
  ('dominican',          'en', 'reference',   30,  'Dominican Province translation'),
  ('freddoso',           'en', 'reference',   35,  'Freddoso translation (partial)'),
  ('model',              'sk', 'machine',     90,  'Model-generated draft');

INSERT INTO work (author, title, structure_type, source_lang) VALUES
  ('Thomas Aquinas', 'Summa Theologiae', 'summa_articulus', 'la');
