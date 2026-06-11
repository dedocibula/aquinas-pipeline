-- Migration 007: add la_surface column to glossary_term
--
-- Canonical Latin surface form lives on the term, not in sense_rendering.
-- NULL means "fall back to latin_lemma" in _match_pattern.
-- Backfill from existing sense_rendering(lang='la') rows seeded by
-- seed_formula_terms (sed_contra, respondeo); all others remain NULL.

BEGIN;

ALTER TABLE glossary_term ADD COLUMN la_surface text;

-- Backfill from sense_rendering(lang='la'), authority-ranked per term.
UPDATE glossary_term gt
SET la_surface = sub.content
FROM (
    SELECT DISTINCT ON (gs.term_id)
        gs.term_id,
        sr.content
    FROM sense_rendering sr
    JOIN glossary_sense gs ON gs.sense_id = sr.sense_id
    JOIN source src ON src.source_id = sr.source_id
    WHERE sr.lang = 'la'
    ORDER BY gs.term_id, src.authority_rank
) sub
WHERE sub.term_id = gt.term_id;

COMMIT;
