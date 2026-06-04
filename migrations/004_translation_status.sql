-- M4: add translation tracking columns to segment
--
-- translation_status: 'pending' (default) → 'translated' | 'needs_human'
-- reviewer_notes: advisory JSON from R1 reviewer, keyed by axis
--   {"iteration": 2, "register": "phrase X is colloquial", "semantics_minor": null}
--
-- The partial index on pending segments makes pilot batch queries fast.

ALTER TABLE segment
    ADD COLUMN translation_status text NOT NULL DEFAULT 'pending'
        CHECK (translation_status IN ('pending', 'translated', 'needs_human')),
    ADD COLUMN reviewer_notes jsonb;

CREATE INDEX idx_segment_translation_status
    ON segment (translation_status)
    WHERE translation_status = 'pending';
