-- 013_glossary_proposals.sql
-- (a) glossary_proposal: editor-proposed glossary/term_usage changes, admin-reviewed in Flask.
--     Approval applies changes via src/review/glossary_apply.py; this table is the inbox + audit.
-- (b) term_usage.status gains 'rejected': permanent tombstone for false-positive detections;
--     constraint readers skip it and the resolver must not re-insert a guessed duplicate (D10).
-- (c) glossary_sense.status gains 'retired': constraint removed corpus-wide; never re-approved
--     by automation.

CREATE TABLE glossary_proposal (
    proposal_id       serial PRIMARY KEY,
    kind              text NOT NULL CHECK (kind IN
                        ('rendering','sense_here','remove_here','retire_sense','add_term')),
    sense_id          int  REFERENCES glossary_sense(sense_id),
                      -- the sense the editor acted on; NULL only for add_term
    proposed_sense_id int  REFERENCES glossary_sense(sense_id),
                      -- sense_here: the sense the editor picked from the dropdown; NULL = free
                      -- text suggestion in proposed_sk (record-only, gold label)
    latin_lemma       text NOT NULL,     -- denormalized display copy; identity for add_term
    current_sk        text,              -- winning sk rendering snapshot at propose time
    proposed_sk       text,              -- NULL for remove_here / retire_sense
    note              text,              -- editor rationale — future gold data, keep verbatim
    origin_segment_id int  REFERENCES segment(segment_id),
                      -- REQUIRED for sense_here / remove_here (the segment to fix)
    proposed_by       text NOT NULL,     -- editor email (session)
    created_at        timestamptz NOT NULL DEFAULT now(),
    status            text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','approved','rejected','superseded')),
    decided_by        text,
    decided_at        timestamptz,
    decision_note     text,
    CHECK (kind = 'add_term' OR sense_id IS NOT NULL),
    CHECK (kind NOT IN ('sense_here','remove_here') OR origin_segment_id IS NOT NULL),
    CHECK (kind NOT IN ('rendering','add_term') OR proposed_sk IS NOT NULL)
);

CREATE INDEX ix_glossary_proposal_status ON glossary_proposal (status);
CREATE INDEX ix_glossary_proposal_sense  ON glossary_proposal (sense_id);

-- (b) term_usage.status += 'rejected'
ALTER TABLE term_usage DROP CONSTRAINT term_usage_status_check;
ALTER TABLE term_usage ADD CONSTRAINT term_usage_status_check
    CHECK (status IN ('guessed','confirmed','rejected'));

-- (c) glossary_sense.status += 'retired'
ALTER TABLE glossary_sense DROP CONSTRAINT glossary_sense_status_check;
ALTER TABLE glossary_sense ADD CONSTRAINT glossary_sense_status_check
    CHECK (status IN ('proposed','flagged','approved','retired'));
