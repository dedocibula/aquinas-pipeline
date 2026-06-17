-- Migration 008: editor allowlist for the preview server OAuth gate.
--
-- Editors are stored here so the list can be updated via psql without a
-- code deploy. is_editor is resolved once per login and cached in the Flask
-- session; changes take effect on next login.
--
-- STOP: human review required before running (CLAUDE.md DDL rule).

CREATE TABLE IF NOT EXISTS editor (
    email text PRIMARY KEY
);
