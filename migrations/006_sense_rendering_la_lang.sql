-- Migration 006: extend sense_rendering lang check to include 'la'
--
-- Previously only cs/en/sk were permitted. Adding 'la' allows storing the
-- canonical Latin surface form for a sense (used by phrase_match for
-- formula terms like "Sed contra", "Respondeo dicendum quod").
-- The authority-ranked LATERAL join in _load_glossary reads this as la_surface.

BEGIN;

ALTER TABLE sense_rendering DROP CONSTRAINT sense_rendering_lang_check;
ALTER TABLE sense_rendering ADD CONSTRAINT sense_rendering_lang_check
    CHECK (lang IN ('cs', 'en', 'sk', 'la'));

-- Also extend term_usage.resolution_method to allow formula_backfill writes
-- from seed_formula_terms (and future glossary-rebuild pipeline).
ALTER TABLE term_usage DROP CONSTRAINT term_usage_resolution_method_check;
ALTER TABLE term_usage ADD CONSTRAINT term_usage_resolution_method_check
    CHECK (resolution_method IN (
        'krystal_single','krystal_multi_voted','krystal_multi_flagged',
        'bahounek_derived','english_derived','model_proposed','formula_backfill'
    ));

COMMIT;
