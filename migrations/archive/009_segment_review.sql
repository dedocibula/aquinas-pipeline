-- Migration 009: human-review layer for the preview server.
--
-- Adds a sparse segment_review table that tracks human editor activity
-- independently of the machine translation pipeline. A row exists only once
-- a human touches a segment (Save, Accept, or Add Note). Reset deletes the
-- row, returning the segment to unreviewed machine state.
--
-- human_reviewed_by stores the editor's email as plain text (no FK to editor)
-- so that removing an editor account does not erase audit history.
--
-- human_note is public translator metatext, distinct from the machine R1
-- reviewer's JSONB in segment.reviewer_notes.
--
-- human_version is an optimistic-lock token that guards text + note + review
-- state together, mirroring the glossary_sense.version idiom. A stale
-- expected_version from a concurrent editor returns a 409 from the server.
--
-- Human Slovak text continues to live in segment_text(sk, human) — unchanged.
-- The machine pipeline (translation_status, pending queue) is NOT modified by
-- this table. See .claude/server_concurrent_review_plan.md for full design.
--
-- STOP: human review required before running (CLAUDE.md DDL rule).

CREATE TABLE segment_review (
    segment_id        int         PRIMARY KEY REFERENCES segment(segment_id),
    human_reviewed_by text        NOT NULL,
    human_reviewed_at timestamptz NOT NULL DEFAULT now(),
    human_note        text        NULL,
    human_version     int         NOT NULL DEFAULT 0
);
