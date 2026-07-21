-- Migration 014: admin flag on the editor allowlist (M5 editor glossary proposals, D6).
--
-- Admin = an editor row with admin = true. Gates the proposal-approval queue
-- (/glossary/proposals) — the surface that applies glossary/term_usage changes.
-- No env var (D6 revised 2026-07-13): toggle via psql, same operational model
-- as the editor allowlist itself (migration 008). Resolved once per login and
-- cached in the Flask session (session["is_admin"]) — changes take effect on
-- next login, mirroring is_editor.
--
-- STOP: human review required before running (CLAUDE.md DDL rule).

ALTER TABLE editor ADD COLUMN admin boolean NOT NULL DEFAULT false;
