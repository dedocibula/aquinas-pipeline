# Server Cleanup — Milestone 01: Quick Wins

**Precondition:** none. Start from a clean `main` (or branch off it).
**Read `server_cleanup_00_index.md` first** for shared context and rules.
**Behavior-preserving only** — no feature/route/JSON/UX change.

## Scope
Low-risk, mechanical de-duplication. No return-type changes, no schema changes. Each item below is
independent; you may commit them together as one milestone or split if a diff gets large.

## Do this

### 1.1 Collapse triplicated proposal-kind constants (3 Python copies → 1)
- Today the proposal-kind groupings exist in **three** places:
  - `src/server/db.py`: `_SENSE_WIDE_KINDS` / `_PER_SEGMENT_KINDS`
  - `src/server/app.py`: `_SENSE_WIDE_PROPOSAL_KINDS` / `_PER_SEGMENT_PROPOSAL_KINDS` (exact dup)
  - `src/server/static/proposals.js`: `KIND_CHANGE_EVERYWHERE` / `KIND_WRONG_SENSE_HERE` /
    `KIND_REMOVE_HERE` / `KIND_RETIRE_EVERYWHERE`
- **Python:** promote the `db.py` sets to public names (`SENSE_WIDE_KINDS`, `PER_SEGMENT_KINDS`),
  make them the single source, and **delete** `app.py`'s private copies — `app.py` imports them
  from `db.py`. Confirm every use site now references the `db.py` names.
- **JS:** the repo is deliberately build-free, so JS **cannot** import Python — this mirror is an
  inherent boundary and must stay. Do **not** try to eliminate it. Instead tighten its comment to
  name the exact CHECK-constraint migration (013) and state it is the one permitted mirror of the
  Python `SENSE_WIDE_KINDS`/`PER_SEGMENT_KINDS`. Net Python-side copies: 3 → 1.

### 1.2 One status→HTTP dispatch helper in app.py
- `app.py` route handlers repeat `if status == "not_found": return …, 404`,
  `if status == "not_pending": return …, 409`, etc. ~10×.
- Add a module-level mapping and helper near the top of `app.py`:
  - `_STATUS_HTTP = {"ok": 200, "not_found": 404, "not_pending": 409, "forbidden": 403,
    "conflict": 409, ...}` — include every status string the action functions currently emit
    (grep `return "` / the tuple returns in `db.py` to enumerate them exhaustively; if a status is
    missing from the map that is a bug — fail loudly, don't default silently).
  - `_json(status, **payload)` → `return jsonify({"ok": status == "ok", **payload}), _STATUS_HTTP[status]`.
- Replace the per-route if/elif ladders with a single `return _json(status, ...)`.
- Keep response bodies byte-identical to today (same keys, same `ok` boolean). This is refactor only;
  milestone 02 will feed `_json` from a typed `ActionResult`.

### 1.3 Shared JS fetch wrapper (new `util.js`)
- `review.js` `_doAction` and `proposals.js` `_postJson` are the same
  `fetch(POST) → resp.json() → {status, data}` wrapper. `comments.js` open-codes
  `fetch().then(r => r.json())` inline in ~5 handlers.
- Create `src/server/static/util.js` (IIFE, `'use strict'`) exposing a namespaced global:
  ```js
  window.AQ = window.AQ || {};
  AQ.postJson = function (url, body) { /* POST JSON → {status, data} */ };
  AQ.getJson  = function (url)       { /* GET  JSON → {status, data} */ };
  ```
- Load `util.js` **before** the other scripts (see 1.4 for placement).
- Point `review.js`, `proposals.js`, and `comments.js` at `AQ.postJson`/`AQ.getJson`; delete the
  local `_doAction` / `_postJson` definitions and the inline fetch bodies. Keep each handler's
  success/error behavior identical.

### 1.4 Fix template `<script>` placement + kill duplicated viewer JS
- `question.html` correctly puts page scripts in `{% block scripts %}`; `article.html` puts its
  **ref-lang switcher + detail-toggle** JS **inline inside `{% block content %}`**, and that same
  switcher/toggle JS is **duplicated verbatim** in `question.html`. `base.html` already declares an
  (otherwise unused) `{% block scripts %}`.
- Extract the shared switcher + detail-toggle logic into `src/server/static/viewer.js`
  (IIFE, `'use strict'`).
- Both `article.html` and `question.html` load `util.js` + `viewer.js` (+ their existing
  `review.js`/`proposals.js`/`comments.js`) via `{% block scripts %}`. Remove the inline
  `<script>` from `article.html`'s content block and the duplicated block in `question.html`.
- Load order in the shared scripts block: `util.js` first, then `viewer.js`, then feature scripts.

### 1.5 Route parity + stale-reference fixes
- `_article_view` in `app.py` does **not** pass `ltree_to_url` into `article.html`, though sibling
  routes pass it to their templates. Add it so `article.html` can use the shared helper (milestone 03
  removes the hand-rolled breadcrumb loop that exists because of this gap).
- Fix the `src/server/db.py` module docstring: it references the stale path `src/common/db.py`; the
  actual shared DB module is `src/storage/db.py`.

### 1.6 Collapse the copy-pasted "locked term" SQL join (interim helper)
- The `term_usage → glossary_sense → glossary_term → sense_rendering → source` join with
  `DISTINCT ON (...) ORDER BY authority_rank` is copy-pasted ~4× in `db.py`
  (`get_segment_constraints`, `get_term_senses`, `segment_has_locked_sense`, `_sense_blast_radius`).
- **Interim only:** hoist the shared SELECT/JOIN fragment into a single private helper (e.g.
  `_locked_term_join_sql()` returning the reusable SQL string, parameterized by WHERE clause).
- **NOTE:** milestone 03 supersedes this by reusing `GlossaryRepository.locked_terms` directly. If
  you are confident 03 will follow immediately, you may **skip 1.6** and do it there. Do the interim
  hoist only if 01 will ship well ahead of 03.

### 1.7 Segment-row Jinja macro
- The segment `<tr>` markup is hand-repeated 3× (the `article.html` loop, `question.html`'s title
  row, `question.html`'s preamble row). In `_segment_panel.html` the Slovak precedence
  `seg.slovak_human or seg.slovak_polish or seg.slovak_model` is written twice.
- Add a `segment_row(...)` macro alongside the existing macros in `_segment_panel.html`; the three
  call sites render via the macro. Centralize the Slovak-precedence expression in one place
  (macro or a single `{% set %}`). Rendered HTML must be identical to today.

## Files
- `src/server/app.py` (1.1, 1.2, 1.5)
- `src/server/db.py` (1.1, 1.5, 1.6)
- `src/server/static/proposals.js`, `review.js`, `comments.js` (1.1, 1.3)
- **new** `src/server/static/util.js` (1.3), `src/server/static/viewer.js` (1.4)
- `src/server/templates/base.html`, `article.html`, `question.html`, `_segment_panel.html`
  (1.4, 1.7)

## Reuse (don't reinvent)
- Existing server helpers `_ltree_to_url_locator`, `ltree_to_url` (1.5). Existing macros in
  `_segment_panel.html` (1.7). Do not add new dependencies.

## Constraints
- Behavior-preserving; no DDL; fail loudly; boring ES5 JS; no M-labels in code. Show diff before
  commit. See `server_cleanup_00_index.md` for the full list.

## Verify
1. `uv run ruff check src/server` → clean.
2. `uv run pytest -q tests/server` → green; then `uv run pytest -q` → green.
3. Manual smoke (psycopg2, `psql` not on PATH → `uv run python3`): load an article + a question view,
   confirm ref-lang switcher, detail toggle, breadcrumb links, comment badges, and proposal buttons
   all still work. Confirm JSON responses for approve/reject/comment are unchanged (same keys/codes).

## Commit
Show the diff, get approval, then:
`refactor(server): dedup proposal-kind constants, status→HTTP helper, shared JS util + viewer, segment-row macro`
(end with the `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer). Clear context and
proceed to `server_cleanup_02_typed_returns.md`.
