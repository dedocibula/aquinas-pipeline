-- gap-term category migration
-- Run ONLY after human review. See CLAUDE.md "Stop for DDL".
--
-- Execution:
--   docker cp migrations/003_term_category.sql aquinas-pipeline-db-1:/tmp/003_term_category.sql
--   docker exec aquinas-pipeline-db-1 psql -U aquinas -d aquinas -f /tmp/003_term_category.sql

-- ── glossary_term.category ─────────────────────────────────────────────────────
-- Model-assigned classification for gap terms (lemmas not in Krystal), set during
-- the DeepSeek proposal pass. Drives review ordering and is fully overridable
-- by a reviewer — terminology decisions live here in the DB, never in code.
--
--   term     — theological/philosophical content word (anima, peccatum, potentia)
--   name     — proper noun (Christus, Augustinus, philosophus=Aristotle)
--   formula  — recurring structural/formulaic connective (Praeterea, Respondeo, Videtur)
--   prose    — ordinary word: verb, quantifier, function word (dico, omnis, tertius)
--
-- Nullable: Krystal-seeded terms keep category NULL — they are
-- authoritative regardless of category. Only gap terms carry a model category.

ALTER TABLE glossary_term
  ADD COLUMN category text
  CHECK (category IN ('term', 'name', 'formula', 'prose'));
