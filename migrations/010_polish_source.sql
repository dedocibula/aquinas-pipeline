-- Migration 010: add 'polish' source row
-- Authority rank 85 — below human (1), above model (90).
-- Display precedence enforced in application: human → polish → model.
INSERT INTO source (code, lang, kind, authority_rank, note)
VALUES ('polish', 'sk', 'machine', 85, 'Claude Sonnet polish pass');
