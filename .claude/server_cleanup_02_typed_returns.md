# Server Cleanup — Milestone 02: Typed Returns & Display-Logic Relocation

**Precondition:** milestone 01 merged (`server_cleanup_01_quickwins.md`). In particular the `_json`
status→HTTP helper from 1.2 must exist in `app.py`.
**Read `server_cleanup_00_index.md` first** for shared context and rules.
**Behavior-preserving only** — same routes, same JSON shapes, same rendered HTML.

## Scope
Make `src/server/db.py` internally consistent about what it returns. Today it mixes typed models
(`CommentThread`, `Comment`), bare dicts (`get_article_segments → list[dict]`), and positional
tuples (`approve_proposal → tuple[str, dict | None]`). This milestone converts domain reads to the
existing typed models, normalizes action tuples to one small result type, and moves JSONB display
parsing out of Jinja into Python. No repository migration yet — that is milestone 03.

## Do this

### 2.1 Domain read functions return typed models
- Convert the dict-returning domain reads in `db.py` to the frozen dataclasses in
  `src/storage/models.py`:
  - `get_article_segments`, `get_question_title_segment`, `get_question_preamble_segment` → `Segment`
    (reuse `Segment.from_row`). Lists become `list[Segment]`.
  - `get_segment_constraints` → `dict[int, list[Constraint]]` (reuse `Constraint.from_row`).
  - `get_term_senses` → `Sense` (or `list[Sense]` where it lists senses).
- Update every `app.py` handler and Jinja template that consumes these to **attribute access**
  (`seg.slovak_human`, `seg.la_surface`, `constraint.latin_lemma`, …) instead of subscripting
  (`seg["slovak_human"]`). Grep the templates (`_segment_panel.html`, `article.html`,
  `question.html`) and `app.py` for `["..."]` / `.get("..."` on these objects and convert.
- **If a template needs a field the model doesn't expose, add the field to the model** (extend
  `from_row`), do **not** fall back to returning a dict. The whole point is to stop leaking raw rows.
- Keep rendered output byte-identical. If `Segment.from_row` doesn't yet cover a column the server
  SELECT returns (e.g. a display-only computed column), extend the model deliberately and note the
  consumer in a comment.

### 2.2 Normalize action-function tuples to one `ActionResult`
- These return ad-hoc tuples today: `approve_proposal → tuple[str, dict | None]`,
  `reject_proposal → str`, `reopen_proposal → tuple[str, int | None]`,
  `propose_sense_change → tuple[str, int | None]`, `propose_add_term → tuple[str, int | None]`,
  `review_segment → tuple[str, int | None]`.
- Introduce one small frozen result type — `ActionResult(status: str, payload: dict | None = None)`
  — in `src/storage/models.py` (next to the other frozen models) **or** a new
  `src/server/results.py` if you prefer to keep it server-local (either is fine; pick one and be
  consistent). Give it an `as_dict()`/`payload or {}` convenience if helpful.
- Each action function returns `ActionResult(...)`. The `status` string values must match the keys
  in `app.py`'s `_STATUS_HTTP` map from milestone 1.2 exactly (add any missing keys to the map).
- In `app.py`, the routes call the action then `return _json(result.status, **(result.payload or {}))`.
  Delete the per-route tuple unpacking. Response JSON and HTTP codes must be identical to before.

### 2.3 Move JSONB display parsing out of Jinja into Python
- `_segment_panel.html` currently destructures `seg.reviewer_notes` JSONB in the template
  (the `iteration` / `raw` / `last_feedback` keys) — display logic that belongs in Python.
- Parse it server-side: expose typed fields on the `Segment` model (or a small nested
  `ReviewerNotes` value object) populated in `Segment.from_row`, so the template only *renders*
  already-parsed values.
- This mirrors how `Comment` / `CommentThread` already do their derivation server-side. Rendered
  output must be unchanged; if a note is missing/empty, render exactly as today.

## Files
- `src/server/db.py` (2.1, 2.2)
- `src/storage/models.py` (2.1 field additions, 2.2 `ActionResult`, 2.3 `ReviewerNotes`/fields)
  — or `src/server/results.py` for `ActionResult` if kept server-local
- `src/server/app.py` (2.1 attribute access, 2.2 `_json(result...)`)
- `src/server/templates/_segment_panel.html`, `article.html`, `question.html` (2.1, 2.3)

## Reuse (don't reinvent)
- `Segment` / `Constraint` / `Sense` + `.from_row` in `src/storage/models.py`.
- The `Comment` / `CommentThread` derivation pattern as the template for `ActionResult` /
  `ReviewerNotes`.
- `app.py`'s `_json` helper and `_STATUS_HTTP` map from milestone 01.

## Constraints
- Behavior-preserving; **no DDL**; fail loudly (a missing expected column should raise, not silently
  default); boring code; no M-labels. Show diff before commit. Full rules in
  `server_cleanup_00_index.md`.
- Do **not** migrate SQL into repositories in this milestone — that is 03. Here you only change what
  `db.py` functions *return*, not where the SQL lives.

## Verify
1. `uv run ruff check src/server src/storage` → clean.
2. `uv run pytest -q tests/server` → green; then `uv run pytest -q` → green. (These tests assert JSON
   shapes and rendered content — they are the guard that `ActionResult`/typed-model changes didn't
   alter output.)
3. Manual smoke (psycopg2; `psql` not on PATH → `uv run python3`): article + question views render
   identically; reviewer-notes panel shows the same iteration/feedback text; approve/reject/reopen and
   propose/review return the same JSON + HTTP codes as before.

## Commit
Show the diff, get approval, then:
`refactor(server): return typed models from db domain reads; ActionResult for proposal/review actions; parse reviewer_notes in Python`
(end with the `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer). Clear context and
proceed to `server_cleanup_03_repository_fold.md`.
