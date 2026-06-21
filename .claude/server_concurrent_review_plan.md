# Server: Concurrent Human-Review Surface — Implementation Plan (phased handoff)

**Status:** design-approved, not built. **Scope:** `src/server/` only (+ one migration).
**How to use this doc:** implement one phase per context window. Each phase has
`READ → IMPLEMENT → VERIFY → /clear`. Read only the files its READ list names plus the
shared spec below. Do NOT load other milestone files. Phases are ordered: each assumes the
previous phase is merged (and, for Phase 2+, that migration 009 is applied).

---

## Goal & context

The preview server lets one editor **Edit** (overwrite Slovak) and **Approve**
(`needs_human → translated`). Two problems now that multiple editors exist:

1. **No concurrency safety.** `save_segment_text` (`src/server/db.py:309`) does a blind
   `ON CONFLICT … DO UPDATE` — last write wins silently; two editors on one article erase
   each other.
2. **Machine and human state are conflated.** A human Save flips `translation_status` to
   `translated` (`src/server/db.py:341`), destroying the machine's pending/translated/needs_human
   signal.

This adds a **separate human-review layer** that never touches the machine pipeline. The machine
keeps owning `translation_status` and keeps translating only its own `pending` segments (the
machine queue is **not** modified). A new sparse `segment_review` table plus the existing
`segment_text(sk, human)` row hold all human work. Readers see `human → machine → awaiting`
plus an optional public translator note; editors get one concurrency-safe **Review** panel.

### Requirements (from the product owner)
- Replace the per-row **Approve** and end-of-row **Edit** with a single **Review** button on
  every segment (translated or not).
- Review opens a **Machine | Human** toggle. Machine text is immutable. Human is an editable
  textarea: prefilled with existing human text, else *imports the machine draft* as a starting
  point, else empty.
- Per-segment status (editor-only): machine status (Translated / Needs Review / Pending) **plus**
  a `Reviewed by <email>` line.
- Review actions at the bottom: **Save, Cancel, Reset** (remove human translation; fall back to
  machine), **Add Note** (translator metatext, shown next to the segment).
- **Reader (anonymous):** human → machine → `— awaiting translation —`; plus the note if present.
- **Editor:** machine status + `Reviewed by` (self or someone else) + the note + the existing
  detail panel.
- A segment counts as translated if **either** machine or human translated it.

---

## Verified architecture facts (do not re-derive)

- **All glossary/segment/run SQL lives in `src/storage/repositories.py`** (`SegmentRepository`,
  `GlossaryRepository`, `TermUsageRepository`, `RunRepository`). **The server is the deliberate
  exception**: `src/server/db.py` owns server-specific SQL and was *not* folded into the
  repositories (see the absorption list in `repositories.py:1-10`). **New server SQL goes in
  `src/server/db.py`.**
- Connection + source helpers to reuse: `storage.db.get_conn` (context manager, commits on exit),
  `storage.db.source_id(conn, code)`. Already imported in `src/server/app.py:21-35`.
- **Machine queue — DO NOT TOUCH:** `SegmentRepository.get_pending_segment_ids_for_article`
  (`repositories.py:584`), `has_pending_segments` (`:632`), driven by
  `translate.run.translate_corpus` (`src/translate/run.py:233`). It selects
  `translation_status='pending'` and ignores the human layer.
- Machine status writes live only in `src/translate/loop.py` (via
  `SegmentRepository.update_translation_status`). The only **server** status writes are the two
  in `src/server/db.py` removed in Phase 2.
- Existing read queries that need the new fields: `get_article_segments` (`db.py:62`),
  `get_question_title_segment` (`db.py:178`), `get_question_preamble_segment` (`db.py:347`).
  All three share the same big SELECT with LATERAL joins for cs/en/sk and a single coalesced
  `slovak` (human-over-model). Consider extracting the shared SELECT into one helper to avoid a
  three-way edit; current code duplicates it.
- Templates: `src/server/templates/article.html` (the live editing UI + JS — current Save/Cancel/
  Approve/Edit handlers are the pattern to evolve), `src/server/templates/question.html` (renders
  the title/preamble segments the same way). `base.html` exposes `is_editor` and
  `current_user_email` via the `@app.context_processor` at `app.py:173`.
- Tests: `tests/server/test_server.py` (only server test file; covers `save_segment_text`,
  `approve_segment`, `/api/edit`, `/api/approve`). These break in Phase 2 and must be rewritten there.
- Migrations live in `migrations/`; applied ones are moved to `migrations/archive/` (latest is
  `008_editor_table.sql`). Follow its `-- STOP: human review required` header convention.
  `psql` is not on PATH (see `docs/claude-corrections.md`); DB is `postgresql://aquinas:aquinas@localhost:5432/aquinas`.

---

## Data model — `segment_review` (sparse; row exists only once a human touches a segment)

```sql
CREATE TABLE segment_review (
    segment_id        int PRIMARY KEY REFERENCES segment(segment_id),
    human_reviewed_by text        NOT NULL,           -- editor email (session["email"])
    human_reviewed_at timestamptz NOT NULL DEFAULT now(),
    human_note        text        NULL,               -- translator metatext, PUBLIC
    human_version     int         NOT NULL DEFAULT 0   -- optimistic-lock token
);
```

- `human_reviewed_by` = email, **plain text not FK** to `editor` (removing an editor must not
  erase audit history). Email is sufficient — no `editor.name`.
- `human_note` is **distinct** from `segment.reviewer_notes` (the machine R1 reviewer's JSONB,
  still rendered editor-only in the detail panel). The `human_` prefix keeps the human layer
  visually separate from the machine pipeline.
- `human_version` is one optimistic-lock token guarding text + note + review state together
  (mirrors the `glossary_sense.version` idiom in `.claude/decisions.md:81`).
- Human Slovak **text** stays in `segment_text(sk, human)` — unchanged.

## Status semantics (two independent axes)

| Axis | Storage | Written by |
|---|---|---|
| Machine status | `segment.translation_status` (pending/translated/needs_human) | M4 loop only |
| Human review | `segment_review` + `segment_text(sk,human)` | server only |

- Server **never** writes `translation_status` again.
- **Effective "translated" is computed at display time**: `has (sk,human) row OR translation_status='translated'`.
- Machine queue untouched ⇒ a `pending` segment a human translated may still be machine-translated
  later (adds a `(sk,model)` row); the human row always wins for display. This is the intended
  "human layer fully separate" behavior.

## Decisions baked in (do not re-litigate)
- **Reset** deletes the `segment_review` row entirely → back to unreviewed machine state.
- **Accept** records a review (sets `reviewed_by`) **without** creating a `(sk,human)` row.
- **Reviewable element types:** same as today plus now `pending` segments. Structural formula
  headings (`sed_contra`/`respondeo` labels) stay glossary-driven, not per-segment editable.
- `human_reviewed_by` and the note are editor-visible; the **note is also public**, the
  `Reviewed by` line is **editor-only**.

## Out of scope here — M5 follow-ups (DO NOT modify `src/translate/` in these phases)
1. `rerun_stale` (`src/translate/run.py:294`) guards human work via `get_human_edited_segments`
   (`repositories.py:734`, keys on `(sk,human)` rows). When it flags a human-edited stale segment
   `needs_human`, it should also clear that segment's `segment_review` row so it re-enters review.
2. **Accept** leaves only a `segment_review` row (no `(sk,human)` text), so accepted-but-not-edited
   segments are **not** guarded and may be re-translated on a term change, leaving a stale review
   row. Decide in the M5 pass whether the guard should also treat a `segment_review` row as
   "human-touched".

---

# Phase 1 — Migration 009 (DDL gate)

**Prerequisite:** none.

### READ
- `migrations/archive/008_editor_table.sql` (header + style convention)
- `.claude/database.md` (the `segment` / `segment_text` sections)

### IMPLEMENT
- Create `migrations/009_segment_review.sql` with the `CREATE TABLE segment_review` DDL above,
  preceded by a comment block explaining the table and a `-- STOP: human review required before
  running (CLAUDE.md DDL rule).` line.

### VERIFY / APPLY (human-gated)
- **Do not auto-apply.** Per `CLAUDE.md`, pause for human review of the DDL.
- Human applies (psql not on PATH): e.g.
  `docker compose exec -T db psql -U aquinas -d aquinas -f - < migrations/009_segment_review.sql`
  or via psycopg2 against `postgresql://aquinas:aquinas@localhost:5432/aquinas`.
- Confirm: `SELECT * FROM segment_review LIMIT 0;` succeeds (table + columns exist).
- After apply, move the file to `migrations/archive/` (matching how 001–008 are stored).

**Done when:** `segment_review` exists in the DB. → `/clear`

---

# Phase 2 — Backend (data layer + routes + tests)

**Prerequisite:** Phase 1 applied. (db.py and app.py are coupled at the import boundary —
removing the old writers breaks `app.py`'s imports — so they ship together.)

### READ
- `src/server/db.py` (whole file)
- `src/server/app.py` (whole file)
- `src/storage/db.py` (only `get_conn`, `source_id`)
- `tests/server/test_server.py` (whole file — for the existing test harness/fakes)

### IMPLEMENT — `src/server/db.py`
1. **Reads** (`get_article_segments`, `get_question_title_segment`, `get_question_preamble_segment`):
   - Add `LEFT JOIN segment_review sr ON sr.segment_id = s.segment_id`.
   - Replace the single coalesced `slovak` with **both** `slovak_model` and `slovak_human`
     (two LATERALs, or split the existing one: model = `src.code='model'`, human = `src.code='human'`).
   - Also select `sr.human_note`, `sr.human_reviewed_by`, `COALESCE(sr.human_version, 0) AS human_version`,
     and keep `s.translation_status`.
   - Recommend extracting the shared SELECT into one private helper used by all three (they are
     identical today except the WHERE clause).
2. **New** `review_segment(conn, segment_id, action, *, expected_version, text=None, note=None) -> str`
   returning `"ok" | "conflict" | "notfound"`:
   - First: `SELECT 1 FROM segment WHERE segment_id=%s` → if missing, return `"notfound"`.
   - Optimistic-locked upsert template (version guard; absent row == version 0):
     ```sql
     INSERT INTO segment_review (segment_id, human_reviewed_by, human_reviewed_at, human_note, human_version)
     VALUES (%(sid)s, %(email)s, now(), %(note_or_null)s, 1)
     ON CONFLICT (segment_id) DO UPDATE
        SET human_reviewed_by = EXCLUDED.human_reviewed_by,
            human_reviewed_at = EXCLUDED.human_reviewed_at,
            -- include the next line ONLY for action='note':
            human_note        = EXCLUDED.human_note,
            human_version     = segment_review.human_version + 1
        WHERE segment_review.human_version = %(expected)s
     RETURNING human_version;
     ```
     If a prior row exists and the version differs, the `WHERE` fails and nothing is returned →
     `"conflict"`. If no prior row, the INSERT path runs (new version 1).
   - Per-action behavior:
     - `save`: run the guarded upsert (do **not** set `human_note`); if it returned a row, also
       upsert `segment_text(sk, human) = text` using `source_id(conn,"human")` and the existing
       `INSERT … ON CONFLICT (segment_id,lang,source_id) DO UPDATE SET content` pattern already
       in this file. **Allowed for `pending` segments** — drop the `!= 'pending'` guard at `db.py:321`.
     - `accept`: run the guarded upsert only (no `human_note`, no text). Blesses the machine draft.
     - `note`: run the guarded upsert **including** `human_note = note`.
     - `reset`: `DELETE FROM segment_review WHERE segment_id=%s AND human_version=%s`
       (rowcount 0 with an existing row → `"conflict"`); then
       `DELETE FROM segment_text WHERE segment_id=%s AND lang='sk' AND source_id=<human>`.
   - Do **not** write `translation_status` in any branch. Do **not** commit (get_conn commits).
3. **Remove** `save_segment_text` (`db.py:309`) and `approve_segment` (`db.py:288`).
4. **Dashboard counts:** in `get_translation_progress` (`db.py:156`) add a `reviewed` count
   (`COUNT … WHERE EXISTS segment_review row`). In `get_question_articles` (`db.py:35`) and
   `get_questions_by_status` (`db.py:408`) add a reviewed rollup (LEFT JOIN segment_review).
   Keep pending/translated/needs_human as truthful machine counts.

### IMPLEMENT — `src/server/app.py`
- Remove the `approve` (`app.py:342`) and `edit_segment` (`app.py:355`) routes and their imports.
- Add one editor-only route, `@requires_editor`:
  `POST /api/segment/<int:segment_id>/review`, JSON body `{action, text?, note?, expected_version}`.
  - `action` ∈ {save, accept, reset, note}; validate.
  - Pull reviewer email from `session["email"]`.
  - Call `review_segment(...)` inside `with get_conn() as conn`.
  - Map result: `"ok"` → `200 {ok:true, human_version:<new>}`; `"notfound"` → `404`;
    `"conflict"` → `409 {ok:false, error:"conflict"}`. Empty `text` for `save` → `400`.

### IMPLEMENT — `tests/server/test_server.py`
- Delete tests for `save_segment_text` / `approve_segment` / `/api/edit` / `/api/approve`.
- Add tests for `review_segment` and the new route: save on a `pending` segment writes
  `(sk,human)` + `segment_review` and leaves `translation_status` unchanged; accept creates a
  review row with no human text; reset deletes both; note round-trips; **stale `expected_version`
  → 409**; unknown segment → 404; non-editor → 403.

### VERIFY
- `uv run pytest tests/server -q` (green).

**Done when:** new endpoint works, old endpoints gone, tests pass. → `/clear`

---

# Phase 3 — Templates / UI

**Prerequisite:** Phase 2 merged (reads return `slovak_model`, `slovak_human`, `human_note`,
`human_reviewed_by`, `human_version`, `translation_status`; route is `/api/segment/<id>/review`).

### READ
- `src/server/templates/article.html` (whole file — markup + JS)
- `src/server/templates/question.html` (the title/preamble segment blocks)
- `src/server/templates/base.html` (only the `is_editor` / `current_user_email` context)
- `src/server/db.py` (only the read functions' SELECT, to confirm the exact field names returned)

### IMPLEMENT (both templates, same segment-rendering pattern)
- **Reader (anonymous):** Slovak cell shows `slovak_human` if present, else `slovak_model`, else
  `— awaiting translation —`. Render `human_note` beneath the segment when present. No badges,
  no reviewed-by for anonymous users.
- **Editor:** replace the row **Approve** button and the end-of-row **Edit** button with a single
  **Review** button. Review opens a panel containing:
  - a **Machine | Human** toggle: Machine = read-only immutable `slovak_model`; Human = a textarea
    auto-sized to content, prefilled with `slovak_human` if present, else `slovak_model` (imported
    as a starting point), else empty.
  - action buttons: **Save, Cancel, Reset, Accept** (Accept small, consistent with Review styling),
    **Add Note** (toggles a note textarea bound to `human_note`).
  - status cell: machine badge (truthful pending/translated/needs_human) **plus** a
    `Reviewed by {{ human_reviewed_by }}` line when set (editor-only).
  - keep the existing detail panel (locked terms + machine `reviewer_notes`) editor-only, unchanged.
- **JS:** evolve the existing fetch handlers in `article.html` to POST
  `/api/segment/<id>/review` with `{action, text?, note?, expected_version}`, where
  `expected_version` is the rendered `human_version`. On `200`, update the displayed text/note and
  store the returned `human_version` (so the same editor can keep editing without reload).
  On `409`, alert "changed by another editor — reload" and do not overwrite. On `400`/`404`,
  surface the error.

### VERIFY
- Run the server: `uv run python -m server.app` (port 5000). (Requires `.env` with
  `FLASK_SECRET_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.)
- **Reader:** logged out — confirm `human → machine → awaiting` ordering, note renders, and no
  badges/reviewed-by leak to anonymous users.
- **Editor:** log in as an `editor` row; exercise Save / Accept / Reset / Add Note on a translated,
  a needs_human, and a pending segment; confirm the machine badge does not change on Save and that
  `Reviewed by <email>` appears.
- **Concurrency:** open one article in two editor sessions; Save in A, then Save in B with B's stale
  version → B gets the 409 reload prompt and A's text survives.

**Done when:** all flows verified in the running app. → `/clear`

---

## Final cross-check (after all phases)
- `git grep -n "save_segment_text\|approve_segment\|/api/edit\|/api/approve"` returns nothing in
  `src/` or `tests/`.
- `src/storage/repositories.py` and `src/translate/` are untouched; `translate_corpus` still drives
  off the unchanged pending query.
