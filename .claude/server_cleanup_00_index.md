# Server Cleanup — Index & Runbook

**Read this file, then read and execute exactly one milestone file at a time.**
This is a **behavior-preserving** cleanup of the Flask preview server and its data layer.
No feature, route, JSON-shape, or UX change is allowed anywhere in this effort.

## Why (context)

The Flask preview server (`src/server/`) grew feature-first (parallel-text viewer → comments →
glossary proposals → digests). Three conventions the rest of the repo enforces have drifted at the
server boundary:

1. **"All SQL lives in repositories."** `src/server/db.py` (~1452 lines) is effectively a *second*
   persistence layer: ~70–75% raw inline SQL, uses **none** of the typed domain models
   (`Segment`/`Sense`/`Term`/`Constraint`), returns bare dicts, and re-implements constraint/sense/
   segment joins that already exist in `src/storage/repositories.py`. Every other module
   (`pipeline/interactive.py`, `translate/run.py`) goes strictly through repositories.
2. **"Typed models over dicts."** `server/db.py` mixes three return styles in one file: typed models
   (`CommentThread`, `Comment`), bare dicts (`get_article_segments → list[dict]`), and positional
   tuples (`approve_proposal → tuple[str, dict | None]`). Route handlers in `app.py` consume the mix.
3. **Single source of truth.** Proposal-kind schema strings and locator-conversion logic are
   duplicated across Python, JavaScript, and Jinja.

Nothing is broken; the server is well-tested. This effort makes the server read like the rest of the
codebase and cheaper to extend safely.

## Milestones (do in order — later depends on earlier)

| Order | File | Risk | Depends on | Status |
|---|---|---|---|---|
| 1 | `server_cleanup_01_quickwins.md` | low (mechanical) | — | done (`a1133a9`) |
| 2 | `server_cleanup_02_typed_returns.md` | medium | 01 merged | done (`f3f341a`) |
| 3 | `server_cleanup_03_repository_fold.md` | higher (broad) | 01 + 02 merged | not started |

Each milestone file is fully self-contained (its own Scope / Do-this / Files / Reuse / Constraints /
Verify / Commit). Context is expected to be **cleared between milestones**, so each file repeats the
shared rules below — that redundancy is intentional.

## How to run this loop (per milestone)
1. `git status` clean; you are on `main` (or branch off it first).
2. Read the one milestone file end to end.
3. Implement **only** what that file lists. Do not scope-creep into a later milestone.
4. `uv run ruff check src/server src/storage` → clean.
5. `uv run pytest -q tests/server` then `uv run pytest -q` → green.
6. **Show the diff and get approval before committing** (repo rule).
7. Commit with the Conventional Commit message the milestone file specifies.
8. Clear context. Start the next milestone.

## Shared conventions (apply to every milestone)
- **Behavior-preserving only.** Same routes, same JSON response shapes, same rendered HTML for a
  given segment before/after. If a change would alter output, it is out of scope — stop and flag it.
- **No DDL.** This is code-only. If a change *seems* to need a schema/index/migration, **stop and
  request human review** (per `CLAUDE.md`). Do not write or run migrations.
- **Fail loudly.** No new silent `try/except`. Parsers/queries crash with the exact locator on
  unexpected structure. The one existing broad catch in `get_structural_formulas` is intentional
  (documented non-critical fallback) — leave it and its comment untouched.
- **Boring code, no new dependencies.** Plain, debuggable Python. New JS files are ES5 IIFE with
  `'use strict'`, `var`, function expressions — no build step, no framework.
- **No milestone/M0–M5 labels in code or comments.** Comments state the non-obvious *why*/consumer.
- **Show diffs before committing.** Never commit without approval.
- **Commit message trailer** (repo-wide): end commit messages with
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## Reusable utilities (lean on these — do not reinvent)
- **Repositories** (`src/storage/repositories.py`): `GlossaryRepository.locked_terms` (returns
  `list[Constraint]`, already applies `tu.status <> 'rejected'`, `gs.status = 'approved'`,
  `sr.lang = 'sk'`, authority-rank ordering), `.get_current_sense`, `.sense_ids_for_term`;
  `SegmentRepository.get_segment` / `.load_body_segments` (return typed `Segment`).
- **Models** (`src/storage/models.py`): frozen dataclasses `Segment`, `Constraint`, `Sense`
  (+ `.from_row` / `.as_dict`); the `Comment` / `CommentThread` / `ActivityEntry` pattern is the
  template for any new `Proposal` / `ActionResult` model.
- **Server helpers** (`src/server/`): `url_to_ltree`, `_ltree_to_url_locator`, `_locator_to_title`,
  `segment_exists`, `ProposalRaceError`, and the `ltree_to_url` value passed to views.
- **Transaction boundary** is centralized in `src/storage/db.py:get_conn()` (commits on clean exit,
  rolls back on exception). Neither layer calls `.commit()` — keep it that way.

## Global verification (also repeated per milestone)
- `uv run ruff check src/server src/storage` — clean (rules E/F/I/N, line-length 100, py312).
- `uv run pytest -q tests/server` — the safety net (covers comments, proposals, approve/review,
  activity/digest, locator routing).
- `uv run pytest -q` — full suite (milestone 03 touches shared `storage/`).
- Manual smoke: DB access is psycopg2 with the hardcoded connection string; `psql` is **not** on
  PATH — use `uv run python3` for ad-hoc checks. Load an article view + a question view (Latin|Slovak
  render, breadcrumb links, detail toggle, ref-lang switcher); open a comment thread
  (add/resolve/reopen/delete updates badge + sidebar); as editor submit a change/remove/add-term
  proposal; as admin open the proposal queue and approve/reject/reopen.
