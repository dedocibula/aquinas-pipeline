# Implementation Plan — Editor Glossary Proposals (Flask UI), Admin Review & Cost-Gated Application

> **Audience:** a fresh model (Sonnet/Opus) implementing ONE stage per session, with context
> cleared between stages. Each stage is self-contained: read **§0 (Shared Context)** plus **only
> your stage**, then implement. Do not read other stages except where a stage says so. Code facts
> below were verified against the repo on **2026-07-12** — line numbers drift, so always `grep`
> for the symbol instead of trusting a number.
>
> **Execution protocol per stage:**
> 1. Read `docs/claude-corrections.md`, `.claude/database.md`, `.claude/decisions.md`,
>    `docs/session_state.md` (project rule), then §0 and your stage here.
> 2. Check the stage's **Preconditions** (greps). If a previous stage's artifacts are missing,
>    STOP and tell the user.
> 3. Implement with tests. Run the stage's verification commands.
> 4. Show the diff, get approval, commit (Conventional Commits — suggested message given).
> 5. Tick the stage checkbox in **§8 Progress** of THIS file and update `docs/session_state.md`
>    (milestone, decisions, files changed, exact next step = the next stage of this plan).

---

## §0. Shared Context (read before every stage)

### What we are building

Editors reviewing translations in the Flask server can currently only edit segment text. We are
adding a **glossary proposal workflow** covering the full term lifecycle:

1. **Editor** (authenticated) acts on the locked-terms list of a segment: propose a different
   Slovak rendering (here-only or everywhere), remove a wrongly-detected term (here-only or
   everywhere), or suggest a missing term. Proposals land in a new `glossary_proposal` table.
   **$0, no glossary mutation, minimal clicks.**
2. **Admin** (an editor whose `editor.admin` column is true — D6) opens a queue page showing every
   pending proposal with its **blast radius** (segments that would be restaged) and **estimated
   cost**, and approves or rejects. Approval applies the glossary/term_usage change — segments
   become *stale* or *pending*, but **nothing is spent**.
3. **Owner** (the user, CLI only — never the web) later runs one batched, cost-previewed,
   confirmed `rerun_stale`. Batching = each segment is paid once no matter how many of its terms
   changed. New-term corpus application is a separate CLI step (re-resolve + diff + reset), also
   cost-previewed.

The Google-Sheets review cycle (`src/review/export_sheet.py` / `import_approvals.py`) is being
**retired** as a surface; its approval logic is the reference implementation we extract (Stage 2).

### The five proposal kinds (D9)

| kind | meaning | applied on admin approve | cost scope |
|---|---|---|---|
| `rendering` | sense's Slovak is wrong **everywhere** | human sk rendering + guarded version bump | sense-wide, stale → rerun |
| `sense_here` | wrong **sense** chosen in this segment | this segment's `term_usage` row → chosen sense, `status='confirmed'`; target sense approved if still `proposed`; segment reset | 1 segment (~cents) |
| `remove_here` | term falsely detected in this segment (surface collision, overfit) | `term_usage.status='rejected'` tombstone; segment reset | 1 segment (~cents) |
| `retire_sense` | glossary entry is overfit / wrong **everywhere** — stop constraining | `glossary_sense.status='retired'` + version bump | sense-wide, stale → rerun (regenerates WITHOUT the constraint) |
| `add_term` | term missing from glossary (resolver never locks it) | create term + approved sense + human sk rendering; **corpus application deferred to the Stage 6 CLI step** | previewed at CLI time |

`sense_here` where no existing sense fits: the editor's free-text suggestion is **recorded only**
(gold label for the deferred `ratio`/`sensus` sense-disambiguation workstream —
`.claude/m5_reviewer_corrections_plan.md` §6.3). Admin can reject or approve-as-acknowledged; no
DB mutation beyond the proposal row.

### Locked decisions — do NOT relitigate

| # | Decision |
|---|---|
| D1 | Pending proposals live in the new `glossary_proposal` table. NOT in `sense_rendering`, NOT via `glossary_sense.status='flagged'`. |
| D2 | **Never set `glossary_sense.status='flagged'` from this workflow.** `locked_terms` and `get_segment_constraints` only emit constraints for `approved` senses — flagging silently drops the constraint corpus-wide while pending. Propose/reject never touches sense status; approve touches it only as specified per kind (`retired` for retire_sense, `approved` for a sense_here target still `proposed`). |
| D3 | Version bumps happen **only when they change translation-relevant state** (`bump_sense_version` is not idempotent; a spurious bump restages hundreds of segments): rendering approve bumps only if the rendering actually differs; retire_sense always bumps (constraint removal must restage); sense_here / remove_here never bump (they reset their one segment directly). |
| D4 | Approve ≠ spend. No web endpoint ever triggers translation. All paid or corpus-scale operations are CLI-only (Stages 5–6). |
| D5 | Duplicate handling in the repo, not the schema: before insert, look for an existing pending row with the same (kind, sense_id or latin_lemma, origin_segment_id, proposed_by) and update it in place. Approving a sense-wide proposal auto-supersedes other pending sense-wide proposals on the same sense. Boring code wins over clever partial-unique upserts. |
| D6 | **(revised 2026-07-13)** Admin = editor row with `editor.admin = true`. A migration adds `admin boolean NOT NULL DEFAULT false` to the `editor` table (008); no env var. Toggled via psql, same operational model as the `editor` allowlist itself (D6 originally used `ADMIN_EMAILS` — superseded; do not reintroduce the env var). |
| D7 | `add_term` approval creates the glossary entry but **never** touches segments — newly inserted `term_usage` rows carry the current version (not stale), so corpus application is the explicit Stage 6 CLI step (re-resolve → diff → cost preview → reset). |
| D8 | Editors can only target senses that appear as locked terms in the panel (approved senses with sk rendering), plus free-form add_term. |
| D9 | The five kinds table above is the complete v1 scope. Sense-*default* inversion (e.g. making `rozum` the default `ratio` sense corpus-wide) stays deferred to the classifier workstream — it is NOT expressible here except as many sense_here proposals. |
| D10 | `term_usage.status='rejected'` rows are permanent tombstones: constraint readers skip them AND the resolver must never re-insert a `guessed` row for a (segment, sense) that has one (Stage 2 makes both true). `confirmed` rows already survive re-resolution (segment-replace deletes only `guessed`). |
| D11 | **(added 2026-07-13)** All UI-facing text/control elements (button titles, labels, placeholders, status messages, hints) added by this workflow are in **English**, matching the rest of the site's control chrome. Slovak is reserved for translation *content* (renderings, segment text), never for UI copy. Applies to all remaining stages (4's admin queue included). |

### Database access

Real data is on **Railway production** (the local docker DB at
`postgresql://aquinas:aquinas@localhost:5432/aquinas` is empty). psql is not on PATH; use
psycopg2. ⚠️ The Railway TCP proxy is normally OFF and the credential rotates — **ask the user to
enable the proxy and paste the DSN** when a stage needs prod. Never commit a DSN. Cast `ltree`
columns `::text` before string ops.

App code connects via `storage.db.get_conn()` (context manager, commits on clean exit, reads
`DATABASE_URL` from `.env`) and `storage.db.source_id(conn, code)`.

### Architecture facts (verified 2026-07-12)

- **Term locking:** the resolver writes `term_usage` rows (one per segment×term occurrence)
  pinning a `glossary_sense` and recording `sense_version_used`. At translate time
  `GlossaryRepository.locked_terms(segment_id)` (src/storage/repositories.py, ~105) returns hard
  constraints: term_usage row + `approved` sense + non-null sk `sense_rendering`, best rendering
  by `source.authority_rank ASC`. Ranks: `human=1` (wins), corpus_thomisticum=5, krystal=10,
  bahounek=20, dominican=30, freddoso=35, polish=85, model=90. Writing the `human` sk rendering
  is how an approved rendering proposal becomes the constraint.
- **`term_usage` columns:** `usage_id, segment_id, sense_id, sense_version_used,
  resolution_method, confidence CHECK ('auto','needs_review'), signals jsonb,
  status CHECK ('guessed','confirmed')` — Stage 1 extends the status CHECK with `'rejected'`.
- **`glossary_sense.status`:** `CHECK IN ('proposed','flagged','approved')` — Stage 1 extends
  with `'retired'`. Only `approved` senses constrain; the resolver builds its lemma map from
  approved senses only (repositories.py ~79), so `retired` also drops out of future resolution.
- **Staleness engine:** `GlossaryRepository.bump_sense_version(sense_id)` (~191) increments
  `glossary_sense.version`. `SegmentRepository.get_stale_segments(work_id)` (~756) returns
  segments where `term_usage.sense_version_used < glossary_sense.version`. The Prefect flow
  `rerun_stale` (src/translate/run.py ~387) resets stale segments to `pending` — segments with a
  `(sk, human)` row are flagged `needs_human` instead (`_guard_and_reset` ~369 +
  `get_human_edited_segments` ~778) — then calls `translate_corpus`. After success the loop
  advances `sense_version_used`.
- **Resolver writes are segment-replace:** `TermUsageRepository.write_term_usage`
  (repositories.py ~929) DELETEs all `guessed` rows for the segment and re-INSERTs the full
  resolution set. `resolver.run` (src/ingest/resolver.py ~421) is full-corpus only. Consequence:
  `confirmed`/`rejected` rows survive; `guessed` duplicates of a rejected pair must be prevented
  at insert (Stage 2).
- **Glossary write helpers** (`GlossaryRepository`; none self-commit): `find_term_by_lemma`,
  `find_sense_by_label(term_id, label)` (label=None ⇒ primary sense), `get_current_sense` →
  `{sense_id,version,status}`, `get_sk_rendering_content`,
  `write_human_rendering(sense_id, sk_text, src_id)` (upsert of sk rendering for any source; no
  bump), `bump_sense_version`, `update_sense_status`, `insert_glossary_term(lemma, category,
  la_surface)`, `insert_glossary_sense(term_id, context_label, status=...)`.
- **Reference approval logic:** `src/review/import_approvals.py::process_approval` — for an
  already-`approved` sense it writes the human rendering and bumps **only when it differs** (the
  guard to preserve).
- **Flask server:** `src/server/app.py` — `requires_editor` checks `session["is_editor"]`;
  `session["email"]` set at OAuth callback; routes open `with get_conn() as conn:` per request;
  JSON idiom `{"ok": False, "error": ...}` with 400/404/409. All server SQL lives as plain
  functions in `src/server/db.py`. Templates `src/server/templates/` (`base.html`,
  `article.html`, `question.html`, `_segment_panel.html` — macros `segment_panel`/`status_cell`).
  JS `src/server/static/review.js` (vanilla IIFE, `fetch` JSON). CSS `style.css`.
- **`get_segment_constraints`** (src/server/db.py ~272): DISTINCT ON (segment, sense) ordered by
  authority_rank; emits `{latin_lemma, slovak, context_label}` per segment — Stage 3 adds
  `sense_id`; Stage 2 adds the `rejected` filter.
- **Cost basis:** `RunRepository.last_run()` (repositories.py ~973) → last `translation_run` row
  (`total_cost_usd`, `total_segments`); fallback: token constants at the top of
  `src/translate/coverage_report.py` (~34–39).
- **Segment reset:** grep `reset_translation_status` in src/storage/repositories.py — if only a
  corpus/bulk variant exists, add a targeted `reset_segments(segment_ids)` that sets
  `translation_status='pending'` EXCEPT for human-edited segments (those →
  `needs_human`, mirroring `_guard_and_reset` semantics). Never modify `(sk, human)` rows.
- **Migrations:** plain SQL in `migrations/` (latest live: `012_pars_order.sql`; applied ones move
  to `migrations/archive/`). No runner — applied manually. **House rule: STOP for human DDL
  review before executing any migration.**
- **Tests:** pytest under `tests/<package>/`, run `uv run pytest tests/<pkg>`. Server tests
  (`tests/server/test_server.py`) are DB-free: monkeypatch `server.db` helpers +
  `server.app.get_conn`, Flask test client. Shared fakes: `tests/_fakes.py`.
- **House rules:** boring debuggable Python; no new dependencies; show diffs before committing;
  never modify/delete `(sk, human)` rows; fail loudly.

### Related plan — coordination

`.claude/m5_reviewer_corrections_plan.md` ("Type-A corrections") Phase 2 specifies the same CLI
cost gate as our **Stage 5**. Neither is built as of 2026-07-12. Whichever executes first
implements it; the other detects it (grep `preview_stale_cost`) and only adds what's missing.

---

## Stage 1 — Migration 013 + `ProposalRepository`

**Goal:** storage layer only: the `glossary_proposal` table, the two status-CHECK extensions,
repository class, schema docs. No UI, no services.

**Preconditions:** none (first stage). Baseline: `uv run pytest tests/storage` green.

### 1.1 Migration — `migrations/013_glossary_proposals.sql`

```sql
-- 013_glossary_proposals.sql
-- (a) glossary_proposal: editor-proposed glossary/term_usage changes, admin-reviewed in Flask.
--     Approval applies changes via src/review/glossary_apply.py; this table is the inbox + audit.
-- (b) term_usage.status gains 'rejected': permanent tombstone for false-positive detections;
--     constraint readers skip it and the resolver must not re-insert a guessed duplicate (D10).
-- (c) glossary_sense.status gains 'retired': constraint removed corpus-wide; never re-approved
--     by automation.

CREATE TABLE glossary_proposal (
    proposal_id       serial PRIMARY KEY,
    kind              text NOT NULL CHECK (kind IN
                        ('rendering','sense_here','remove_here','retire_sense','add_term')),
    sense_id          int  REFERENCES glossary_sense(sense_id),
                      -- the sense the editor acted on; NULL only for add_term
    proposed_sense_id int  REFERENCES glossary_sense(sense_id),
                      -- sense_here: the sense the editor picked from the dropdown; NULL = free
                      -- text suggestion in proposed_sk (record-only, gold label)
    latin_lemma       text NOT NULL,     -- denormalized display copy; identity for add_term
    current_sk        text,              -- winning sk rendering snapshot at propose time
    proposed_sk       text,              -- NULL for remove_here / retire_sense
    note              text,              -- editor rationale — future gold data, keep verbatim
    origin_segment_id int  REFERENCES segment(segment_id),
                      -- REQUIRED for sense_here / remove_here (the segment to fix)
    proposed_by       text NOT NULL,     -- editor email (session)
    created_at        timestamptz NOT NULL DEFAULT now(),
    status            text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','approved','rejected','superseded')),
    decided_by        text,
    decided_at        timestamptz,
    decision_note     text,
    CHECK (kind = 'add_term' OR sense_id IS NOT NULL),
    CHECK (kind NOT IN ('sense_here','remove_here') OR origin_segment_id IS NOT NULL),
    CHECK (kind NOT IN ('rendering','add_term') OR proposed_sk IS NOT NULL)
);

CREATE INDEX ix_glossary_proposal_status ON glossary_proposal (status);
CREATE INDEX ix_glossary_proposal_sense  ON glossary_proposal (sense_id);

-- (b) term_usage.status += 'rejected'
-- Find the constraint name first (it may be auto-generated):
--   SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
--   WHERE conrelid = 'term_usage'::regclass AND pg_get_constraintdef(oid) ILIKE '%status%';
ALTER TABLE term_usage DROP CONSTRAINT <found_name>;
ALTER TABLE term_usage ADD CONSTRAINT term_usage_status_check
    CHECK (status IN ('guessed','confirmed','rejected'));

-- (c) glossary_sense.status += 'retired'  (same name-lookup procedure)
ALTER TABLE glossary_sense DROP CONSTRAINT <found_name>;
ALTER TABLE glossary_sense ADD CONSTRAINT glossary_sense_status_check
    CHECK (status IN ('proposed','flagged','approved','retired'));
```

**STOP after writing this file: show it to the user for DDL review (house rule).** After
approval, resolve the `<found_name>` placeholders against each DB and apply via a throwaway
psycopg2 script to **both** local docker and Railway prod (ask the user for the DSN). Do not
commit any DSN.

### 1.2 `ProposalRepository` — append to `src/storage/repositories.py`

Follow the file's existing repository style (class with `__init__(self, conn)`, explicit SQL,
RealDictCursor, no self-commit). Methods:

- `create_or_update_pending(*, kind, sense_id, proposed_sense_id, latin_lemma, current_sk,
  proposed_sk, note, origin_segment_id, proposed_by) -> int` — per D5: SELECT an existing pending
  row matching (kind, proposed_by, and: sense_id when kind≠add_term / latin_lemma when add_term,
  and origin_segment_id for the per-segment kinds); UPDATE it in place (content + `created_at =
  now()`) or INSERT. Returns proposal_id.
- `get(proposal_id) -> dict | None`.
- `list_pending() -> list[dict]` — oldest first.
- `pending_by_sense(sense_ids: list[int]) -> dict[int, int]` — count of pending proposals per
  sense (Stage 3 uses it for the "proposal pending" badge — D11: English UI copy).
- `decide(proposal_id, status, decided_by, decision_note=None) -> bool` — UPDATE ... WHERE
  `status='pending'`; returns rowcount == 1 (False ⇒ caller responds 409).
- `supersede_sense_wide_siblings(sense_id, keep_proposal_id, decided_by) -> int` — mark other
  **pending** proposals on the sense with kind IN ('rendering','retire_sense') as `superseded`.
  (Per-segment kinds on the same sense stay pending — they are independent fixes.)

### 1.3 Schema docs

Append to `.claude/database.md` in its exact house style (column table + "Populated by:" /
"Read by:" per column): a `## glossary_proposal` section, and update the `term_usage.status` and
`glossary_sense.status` rows to document `rejected` / `retired` (meaning, who writes them, who
filters them — cite D10).

### 1.4 Tests — `tests/storage/` (mirror the existing test approach there — inspect first)

Cover: create twice (same editor/kind/target) → one row updated; different editors → two rows;
per-segment kinds keyed by origin_segment_id (same editor, two segments → two rows); `decide` on
non-pending → False; `supersede_sense_wide_siblings` leaves per-segment kinds pending; CHECK
constraints reject malformed rows (e.g. sense_here without origin_segment_id).

**Verification:** `uv run pytest tests/storage` green; both DBs report the new table and both
widened CHECKs.

**Commit:** `feat(storage): glossary_proposal table + rejected/retired statuses + ProposalRepository`

---

## Stage 2 — Apply services + constraint-reader/resolver safety

**Goal:** one tested module with the five apply functions, plus the three safety changes that
make `rejected`/`retired` real: constraint readers skip rejected usages, the resolver never
resurrects them, and the Sheets importer reuses the shared rendering logic.

**Preconditions:** Stage 1 done — `grep -n "class ProposalRepository" src/storage/repositories.py`
hits; local DB has the widened CHECKs. Baseline: `uv run pytest tests/review tests/translate
tests/ingest` green.

### 2.1 Safety first — readers and resolver honor the new statuses

These touch the most safety-critical queries in the project; write regression tests before
changing them.

1. **`GlossaryRepository.locked_terms`** (src/storage/repositories.py ~105): add
   `AND tu.status <> 'rejected'` (or `tu.status IN ('guessed','confirmed')`) to the term_usage
   join. (Sense status already filters to `approved`, which excludes `retired` automatically —
   verify by reading the SQL, don't assume.)
2. **`get_segment_constraints`** (src/server/db.py ~272): same filter.
3. **Resolver insert** — `TermUsageRepository.write_term_usage` (repositories.py ~929): the
   segment-replace DELETE targets only `guessed` rows (verify), so `rejected` and `confirmed`
   survive; but the re-INSERT would create a `guessed` duplicate next to a `rejected` tombstone.
   Change the insert to skip any (segment_id, sense_id) pair that already has a `rejected` OR
   `confirmed` row for that segment. Keep it in SQL (`NOT EXISTS`) — boring and race-free.
4. Check for any other reader: `grep -rn "FROM term_usage\|JOIN term_usage" src/` and audit each
   hit for whether it must exclude `rejected` (e.g. stale-segment queries: a rejected usage row
   must NOT make a segment stale — read `get_stale_segments` and add the filter if it joins
   term_usage without a status filter).

### 2.2 New module `src/review/glossary_apply.py`

All functions take `conn`, use the §0 repositories, never commit (get_conn owns that), never
touch `(sk, human)` rows, and return a small result dict. Raise ValueError on invalid targets
(fail loudly).

```python
def apply_rendering_change(conn, sense_id, new_sk) -> dict
    # {"changed", "bumped", "old_sk", "new_sk", "new_version"}
```
Extracted from `import_approvals.process_approval`'s already-approved branch: read
`get_sk_rendering_content`; empty/whitespace `new_sk` → ValueError; equal → return
changed=False (no write, NO bump — D3); else `write_human_rendering(..., source_id(conn,
"human"))` + `bump_sense_version`. Never touches sense status (D2).

```python
def apply_sense_here(conn, segment_id, from_sense_id, to_sense_id) -> dict
    # {"confirmed": True, "target_sense_approved": bool, "segment_reset": "pending"|"needs_human"}
```
Validate both senses belong to the same `glossary_term` (ValueError otherwise). UPDATE the
segment's term_usage row(s) for `from_sense_id` → `sense_id = to_sense_id`,
`status = 'confirmed'`, `sense_version_used =` the target sense's current version (so the row is
NOT stale — the direct reset below handles regeneration; D3). If the target sense is `proposed`,
`update_sense_status(to_sense_id, 'approved')` (it must constrain from now on; record in result).
Reset the segment via the targeted reset helper (pending, or needs_human if human-edited).

```python
def apply_remove_here(conn, segment_id, sense_id) -> dict
```
UPDATE that segment's term_usage row(s) for the sense → `status = 'rejected'` (tombstone, D10).
Reset the segment. No version bump (D3).

```python
def apply_retire_sense(conn, sense_id) -> dict   # {"retired": True, "new_version": int}
```
`update_sense_status(sense_id, 'retired')` + `bump_sense_version` (always — constraint removal
must restage every segment that was translated under it; D3). Do NOT delete term_usage rows —
staleness needs them, and `retired` already excludes the sense from constraints and future
resolution.

```python
def apply_add_term(conn, latin_lemma, proposed_sk, note) -> dict
    # {"term_id", "sense_id", "existed": bool}
```
`find_term_by_lemma` — if the term exists, ValueError (`"term_exists"`; the admin should reject
the proposal and act on the term's senses instead). Else `insert_glossary_term(latin_lemma,
'term', None)` + `insert_glossary_sense(term_id, None, status='approved')` +
`write_human_rendering(sense_id, proposed_sk, human_src)`. **No segment work** (D7 — Stage 6
applies it to the corpus).

### 2.3 Refactor `src/review/import_approvals.py` (behavior-preserving)

Replace `process_approval`'s already-approved inline read-compare-write-bump block with
`apply_rendering_change`. Keep `write_context_label`, return semantics, the proposed-sense
branch, and `process_new_term` untouched. Existing tests in
`tests/review/test_import_approvals.py` must pass unchanged (or with mechanical updates only).

### 2.4 Tests

- `tests/review/test_glossary_apply.py` (mirror the fake style of the existing review tests):
  every function's happy path + guards — rendering unchanged ⇒ no bump; sense_here across
  different terms ⇒ ValueError; sense_here sets sense_version_used to current (row not stale);
  remove_here writes the tombstone and resets; retire always bumps; add_term on existing lemma ⇒
  ValueError; human-edited segment resets to needs_human, its `(sk,human)` untouched.
- Regression tests for 2.1: a `rejected` usage row is excluded from `locked_terms` and
  `get_segment_constraints`; `write_term_usage` skips re-inserting over a tombstone; a rejected
  row never makes a segment stale.

**Verification:** `uv run pytest tests/review tests/storage tests/ingest tests/translate
tests/server` green.

**Commit:** `feat(review): glossary_apply services + rejected/retired safety in readers and resolver`

---

## Stage 3 — Editor propose UI + endpoints

**Goal:** the editor-facing surface: per-term actions (✎ change / ✗ remove, each with an
"only here / everywhere" choice), a sense dropdown for "wrong sense here", a missing-term form,
and a pending badge. Writes only to `glossary_proposal`.

**Preconditions:** Stage 1 done (`ProposalRepository` exists). Stage 2 NOT required (proposals
are inert until Stage 4 applies them), but grep for it and note the status to the user.

### 3.1 Server data — `src/server/db.py`

- `get_segment_constraints` (~272): add `gs.sense_id` to the SELECT and the emitted dicts
  (`"sense_id": ...`). Existing consumers ignore extra keys — backward compatible. Update the
  docstring. (If Stage 2 already added the `rejected` filter here, leave it; otherwise DO NOT add
  it in this stage — it belongs with Stage 2's regression tests.)
- New `get_term_senses(conn, sense_id) -> dict` — given a sense, return its term
  (`term_id`, `latin_lemma`) and all of the term's senses:
  `[{sense_id, context_label, status, slovak}]` with each sense's winning sk rendering
  (authority_rank-ordered, same join shape as `get_segment_constraints`). Powers the
  "wrong sense here" dropdown.
- Wire `ProposalRepository.pending_by_sense` into the article/question views: for the page's
  constraint sense_ids, pass `pending_counts` into the templates so the panel can show a
  "proposal pending" badge (English — D11) next to terms that already have a pending proposal.

### 3.2 Endpoints — `src/server/app.py` (all `@requires_editor`; follow the existing JSON/status
idioms; `proposed_by = session["email"]`)

```
GET  /api/sense/<int:sense_id>/alternatives
     → {ok, latin_lemma, senses:[{sense_id, context_label, status, slovak}]}   (3.1 helper)

POST /api/sense/<int:sense_id>/propose
     body: {"kind": "rendering"|"sense_here"|"remove_here"|"retire_sense",
            "proposed_sk": str?, "proposed_sense_id": int?,
            "note": str?, "origin_segment_id": int?}
POST /api/term-proposal
     body: {"latin_lemma": str, "proposed_sk": str, "note": str?, "origin_segment_id": int?}
```

`propose` validation per kind (404 unknown sense; 400 with a specific error string otherwise):
- all kinds: sense exists (`get_current_sense`); snapshot `current_sk` via
  `get_sk_rendering_content`; look up `latin_lemma` (via `get_term_senses`).
- `rendering`: `proposed_sk` non-empty and ≠ current winning rendering (`"no_change"`).
- `sense_here`: `origin_segment_id` required; either `proposed_sense_id` (must be a sense of the
  same term — validate via `get_term_senses`, else 400 `"wrong_term"`; must differ from
  `sense_id`, else `"no_change"`) or free-text `proposed_sk` (recorded-only path) — at least one.
- `remove_here`: `origin_segment_id` required; ignore proposed_sk.
- `retire_sense`: no extra fields; note strongly encouraged (UI hints "why?" — English, D11).

`term-proposal` (add_term): `latin_lemma` + `proposed_sk` non-empty; if
`GlossaryRepository.find_term_by_lemma` hits → 400 `"term_exists"` (message includes the lemma so
the editor knows to act on the existing term instead).

All writes via `ProposalRepository.create_or_update_pending` (D5 upsert semantics — re-proposing
edits your own pending proposal). Response `{ok: true, proposal_id}`.

### 3.3 Template — `src/server/templates/_segment_panel.html`

In the `segment_panel` macro's "Locked terms" `<ul class="term-list">` loop, per term (editor
only): keep the current display, add compact action buttons and the pending badge:

```html
<button class="btn-term-act" data-act="change" data-sense-id="{{ c.sense_id }}"
        data-segment-id="{{ seg.segment_id }}" data-current-sk="{{ c.slovak }}"
        data-lemma="{{ c.latin_lemma }}" title="propose change">✎</button>
<button class="btn-term-act" data-act="remove" data-sense-id="{{ c.sense_id }}"
        data-segment-id="{{ seg.segment_id }}" data-lemma="{{ c.latin_lemma }}"
        title="propose removal">✗</button>
{% if pending_counts.get(c.sense_id) %}<span class="term-pending" title="proposal pending">⏳</span>{% endif %}
```

One shared hidden form per panel (`id="tpform-{{ seg.segment_id }}"`), repositioned under the
clicked term by JS, containing: scope radio ("here only" / "everywhere"), a rendering input
(prefilled with `data-current-sk` for ✎), a sense `<select>` (shown only for ✎ + "here only";
populated from `/alternatives`; includes an "other…" option that reveals the free-text input), a
note textarea, submit/cancel, status span. For ✗ + "everywhere" show a red hint ("removes the
term from the entire corpus — requires a reason") and require the note client-side.

Below the list: "+ missing term" toggle → lemma + rendering + note → POST `/api/term-proposal`.
All copy English throughout (D11).

Keep all markup/id conventions consistent with the existing panel (`...-{{ seg.segment_id }}`).
Pass `pending_counts` through `article.html` / `question.html` render calls (default `{}`).

### 3.4 JS + CSS

New `src/server/static/proposals.js` (kept separate from `review.js` rather than extending it —
distinct concern, same vanilla-IIFE / delegated-`querySelectorAll` / `fetch`-JSON idiom). Kind
derivation: ✎+everywhere → `rendering`; ✎+here → `sense_here`; ✗+here → `remove_here`;
✗+everywhere → `retire_sense`. On success show "Proposed — pending approval." (English, D11) and
flip the ⏳ badge on; render server error strings verbatim in the status span. `style.css`
additions consistent with existing panel styles.

### 3.5 Tests — extend `tests/server/test_server.py` (same monkeypatch style)

Anonymous → 403 on all three endpoints. Per kind: valid → 200 + repo called with session email
and correct kind; `rendering` no_change → 400; `sense_here` missing origin → 400, wrong-term
sense → 400, free-text-only → 200; `remove_here` ignores proposed_sk; `term-proposal` on
existing lemma → 400 `term_exists`; `/alternatives` returns the term's senses;
`get_segment_constraints` fixtures gain `sense_id`.

**Verification:** `uv run pytest tests/server` green. Manual: run the server against the local
DB (seed a term/sense/segment/term_usage row if empty), exercise each gesture, confirm the
`glossary_proposal` rows (kind, snapshot, origin).

**Commit:** `feat(server): editor term-proposal UI (change/remove/add, here-vs-everywhere)`

---

## Stage 4 — Admin queue + approve/reject application

**Goal:** admin sees pending proposals grouped by impact, with blast radius + cost estimate,
and approves (applies via Stage 2 services, $0) or rejects.

**Preconditions:** Stages 1–3 done — grep `ProposalRepository`, `glossary_apply`,
`btn-term-act` all hit.

### 4.1 Admin gate — DB column + `src/server/app.py` (D6, revised 2026-07-13)

- **Migration** (append to `migrations/` as the next-numbered file, or fold into 013 if still
  unapplied to prod — check `.claude/database.md` / ask the user): `ALTER TABLE editor ADD COLUMN
  admin boolean NOT NULL DEFAULT false;`. **STOP for DDL review (house rule)** before applying to
  either DB.
- `src/server/db.py`: extend the existing `is_editor(conn, email)` query (or add a sibling
  `is_admin(conn, email) -> bool`: `SELECT admin FROM editor WHERE email = %s`, `fetchone()` is
  `None` or `(False,)` ⇒ `False`) — do not read an env var.
- Resolve admin status once at login (mirroring how `is_editor` is cached), store
  `session["is_admin"]`, same lifecycle as `session["is_editor"]` (changes take effect on next
  login).
- `requires_admin` decorator modeled on `requires_editor`: `session["is_editor"]` AND
  `session.get("is_admin")`, else 403 JSON. No admin rows ⇒ nobody is admin (fail closed, same
  spirit as the old env-var default — just DB-backed now).
- Add `is_admin` to the `_inject_user` context processor (source it from the session, already
  computed at login); nav link to `/glossary/proposals` in `base.html` for admins.

### 4.2 Queue data — `src/server/db.py`

`get_pending_proposals_view(conn) -> list[dict]`: pending proposals LEFT JOINed with
`glossary_term.latin_lemma` / `glossary_sense.context_label` (+ the proposed_sense's label and
rendering for sense_here), plus per row:

- **live current sk** for the sense (winning rendering, authority_rank ASC — same join shape as
  `get_segment_constraints`) → queue shows a ⚠ drift warning when it differs from the stored
  `current_sk` snapshot (the glossary moved since the editor proposed).
- for sense-wide kinds (`rendering`, `retire_sense`) — **blast radius**:
  ```sql
  SELECT s.translation_status, count(DISTINCT tu.segment_id) AS n
  FROM term_usage tu JOIN segment s ON s.segment_id = tu.segment_id
  WHERE tu.sense_id = %(sense_id)s AND tu.status <> 'rejected'
  GROUP BY s.translation_status
  ```
  plus reviewed count (`JOIN segment_review`, guard-protected ⇒ free), plus **marginal restage
  count**: the same segments minus those already stale
  (`NOT EXISTS (SELECT 1 FROM term_usage tu2 JOIN glossary_sense gs2 ON gs2.sense_id =
  tu2.sense_id WHERE tu2.segment_id = tu.segment_id AND tu2.sense_version_used < gs2.version)`)
  — what approving *adds* to the next paid run.
- for per-segment kinds — the origin segment's locator (`segment.locator_path::text`) as a link.

`get_cost_per_segment(conn) -> float`: `RunRepository.last_run()`'s
`total_cost_usd / total_segments` when present and > 0, else derive the fallback from
`src/translate/coverage_report.py`'s token constants (import them, don't copy numbers).

### 4.3 Routes + template + JS

- `GET /glossary/proposals` `@requires_admin` → new `glossary_proposals.html` extending
  `base.html`, three sections:
  1. **Sense-wide** (`rendering`, `retire_sense`): term, context_label, current → proposed (or
     "RETIRE — constraint removed corpus-wide"), note, proposer, created_at, drift ⚠, blast
     radius split (translated / needs_human / reviewed), `marginal × $/seg = est. $`.
  2. **Per-segment** (`sense_here`, `remove_here`): term, origin locator link, chosen target
     sense (or "free-text: X — record-only"), note, proposer; cost line "1 segment (~cents)".
  3. **New terms** (`add_term`): lemma, proposed sk, note; explanation line: "creates the
     glossary entry; corpus application runs via the CLI re-resolve step" (D7).
  Approve / Reject buttons + optional decision-note input per row.
- `POST /api/proposal/<int:proposal_id>/approve` `@requires_admin` — ALL inside one
  `with get_conn() as conn:` block so a failure rolls back atomically:
  1. `ProposalRepository.get` → 404 if missing.
  2. Dispatch by kind to the Stage 2 service:
     `rendering` → `apply_rendering_change(conn, sense_id, proposed_sk)`;
     `sense_here` with `proposed_sense_id` → `apply_sense_here(...)`, without → no service call
     (record-only);
     `remove_here` → `apply_remove_here(...)`;
     `retire_sense` → `apply_retire_sense(...)`;
     `add_term` → `apply_add_term(...)` (ValueError `term_exists` → 409 with that error, row
     stays pending for the admin to reject with a note).
  3. `decide(id, "approved", admin_email, note)` — False (raced) → raise so the transaction
     rolls back the service writes, respond 409.
  4. Sense-wide kinds: `supersede_sense_wide_siblings(...)`.
  5. Respond with the service result (`{ok, applied, bumped?, new_version?, ...}`); the page
     renders per kind: "N segments now stale — owner runs the paid rerun via CLI" /
     "segment reset — retranslates in the next run" / "term created — corpus application via
     CLI re-resolve".
- `POST /api/proposal/<int:proposal_id>/reject` `@requires_admin`: `decide(..., "rejected", ...)`;
  409 if not pending.
- JS: new `static/proposals.js` in the review.js idiom (POST, update row state inline).

### 4.4 Tests — extend `tests/server/`

`requires_admin` matrix (anonymous / editor / admin; `editor.admin = false` or missing row ⇒ 403 for everyone).
Approve dispatch per kind (correct service called with correct args; record-only sense_here calls
none; add_term term_exists → 409, proposal still pending). Race: `decide` False → 409 and the
service write is not committed (assert via the single-transaction structure with a mock conn).
Reject non-pending → 409. Queue view: monkeypatched data renders all three sections; drift flag
computed.

**Verification:** `uv run pytest tests/server` green. Manual on local DB: full loop for at least
`rendering` and `remove_here` — propose (editor session) → queue shows radius/cost → approve
(admin session) → confirm `sense_rendering(human)` + bump, or tombstone + segment reset, and the
proposal row's decided fields.

**Commit:** `feat(server): admin proposal queue with blast-radius/cost preview + apply-on-approve`

---

## Stage 5 — CLI cost gate for `rerun_stale` (+ installments)

**Goal:** the only spend path: owner-gated, cost-previewed, confirmable, optionally limited
(`--limit N`) restaging of stale segments.

**Preconditions:** `grep -rn "preview_stale_cost" src/` — **if it hits, the twin Phase 2 of
`.claude/m5_reviewer_corrections_plan.md` was built first: read what exists, ADD only what's
missing (`limit`, `AQUINAS_MAX_RUN_USD`), and skip the rest.** Baseline:
`uv run pytest tests/translate tests/pipeline` green.

### 5.1 `src/translate/run.py`

- `preview_stale_cost(work_id: int = 1, limit: int | None = None) -> tuple[int, float]`:
  `stale = get_stale_segments(work_id)`; subtract human-edited segments
  (`get_human_edited_segments` — they get flagged, not paid); sort by segment_id and apply
  `limit`; multiply by $/segment from `RunRepository.last_run()`
  (`total_cost_usd / total_segments`), falling back to `coverage_report.py` constants. Read-only.
- Extend the `rerun_stale` flow with `limit: int | None`: restage only the first N stale
  segment_ids (sorted — deterministic installments). The rest stay stale and are picked up by the
  next run: the stale set shrinks incrementally, which is the pay-in-installments mechanism.

### 5.2 `src/translate/steps.py` — `RerunStaleStep` (and `ResetCorpusStep`)

- `verify(ctx) -> bool`: env `AQUINAS_OWNER_TOKEN` set and non-blank (the runner in
  `src/pipeline/runner.py` turns False into a blocked step before any work — zero spend).
- `run()`: call `preview_stale_cost`; if env `AQUINAS_MAX_RUN_USD` is set and the estimate
  exceeds it → refuse with a clear message (no override flag; the owner edits the env
  consciously). Print `"Restage N segments, est ~$X.XX — proceed? [y/N]"`, read confirmation via
  an injectable reader (default `input`). Non-yes → `StepResult(ok=True, summary="cancelled — no
  retranslation")`. Yes → invoke the flow (passing `limit` if provided). Do not renumber the
  interactive menu (`src/pipeline/interactive.py` items #10/#13 keep labels/positions).

### 5.3 Tests — `tests/translate/` (all without spending)

`preview_stale_cost` arithmetic against a faked last-run; limit slicing determinism; `verify()`
False without token / True with; prompt `N` → cancelled, flow never called (patched);
`AQUINAS_MAX_RUN_USD` exceeded → refused, flow never called.

**Verification:** `uv run pytest tests/translate tests/pipeline` green.

**Commit:** `feat(translate): owner-gated cost preview + --limit installments for rerun_stale`

*(Then tell the user this fulfills Phase 2 of `.claude/m5_reviewer_corrections_plan.md` and note
it in that file.)*

---

## Stage 6 — New-term corpus application (CLI re-resolve + diff + reset)

**Goal:** make approved `add_term` proposals actually constrain the corpus: detect the new terms
via a full re-resolve (free compute), then cost-preview and reset exactly the segments that
gained a lock.

**Preconditions:** Stages 1, 2, 4, 5 done (approved add_term rows can exist; the gate exists).
Read `.claude/m5_reviewer_corrections_plan.md` §3.4 for the resolver background: `resolver.run`
(src/ingest/resolver.py ~421) is full-corpus only; `write_term_usage` is segment-replace; a
newly-inserted lock has `sense_version_used == version` ⇒ NOT stale ⇒ needs an explicit reset.
Known limitation to state honestly in output: CLTK lemmatization misses some forms (`ente` ↛
`ens`, `subiectum` → verb `subicio`), so a new term only applies where the resolver can actually
detect it; lemmatization overrides are the deferred workstream
(m5_reviewer_corrections_plan §6.2), not this stage.

### 6.1 New step — `src/ingest/steps.py` (or wherever resolver steps live — grep
`class.*Step` in src/ingest/ and follow the house pattern) — `ApplyNewTermsStep`

1. Find target terms: `glossary_proposal` rows with `kind='add_term' AND status='approved'`
   whose `term_id` (via lemma lookup) has zero `term_usage` rows — i.e. created but never
   applied. None → summary "nothing to apply", done.
2. Snapshot: `SELECT segment_id, sense_id FROM term_usage WHERE status <> 'rejected'` into a set
   (memory is fine at ~10⁵ rows).
3. Run the resolver (full corpus — free, but slow; print progress). `confirmed`/`rejected` rows
   survive by design (D10/Stage 2.1).
4. Diff: segments that now have a term_usage row for the new terms' senses and were previously
   translated. Report per term: segment count, sample locators.
5. Gate (reuse Stage 5's pattern verbatim): `AQUINAS_OWNER_TOKEN` in `verify()`; cost preview
   `n_segments × $/seg` (+ `AQUINAS_MAX_RUN_USD` cap) and `[y/N]` confirm — then targeted reset
   of exactly those segments (pending / needs_human for human-edited). The next
   `translate_corpus`/`rerun_stale` run translates them WITH the new constraint.
6. No confirm → segments stay translated-without-the-term (safe default; report says so).

Add a menu entry in `src/pipeline/interactive.py` at the END of the menu (do not renumber
existing items).

### 6.2 Tests — `tests/ingest/` (resolver faked; no CLTK in unit tests — mirror how existing
resolver tests fake it)

Diff logic: pre/post snapshot → exactly the gained segments; already-applied term skipped;
declined confirm resets nothing; human-edited → needs_human.

**Verification:** `uv run pytest tests/ingest tests/pipeline` green.

**Commit:** `feat(ingest): ApplyNewTermsStep — resolve, diff, cost-gated reset for new glossary terms`

---

## Stage 7 — End-to-end verification + docs

**Goal:** prove the loop on the real stack; persist state.

**Preconditions:** Stages 1–6 ticked in §8.

1. `uv run pytest` — entire suite green.
2. **Local e2e** (seed minimal rows via psycopg2 if the local DB is empty): for each kind —
   propose (editor session) → queue shows it correctly classified with radius/cost → approve
   (admin session) → verify the DB effect per the D9 table → `preview_stale_cost` /
   segment status reflects it → gated CLI step previews and cancels cleanly on `N`.
3. **Prod smoke ($0 steps only, user present):** deploy; user proposes on a real term; user
   approves; check stale/pending counts. **Do NOT run any paid retranslation — the owner runs it
   themselves when they choose.**
4. Ask the user to flip `editor.admin = true` (Railway, via psql) for the intended admin
   account(s) and confirm `AQUINAS_OWNER_TOKEN` / `AQUINAS_MAX_RUN_USD` handling locally — the
   code fails closed without them.
5. Docs: update `docs/session_state.md` (milestone, D1–D10 summary, files changed, next step =
   "owner batches approvals, then runs gated rerun_stale / apply-new-terms when budget allows");
   note in `.claude/m5_reviewer_corrections_plan.md` §6.6 that this plan implemented it; confirm
   `.claude/database.md` covers `glossary_proposal` + the widened CHECKs (Stage 1.3).

**Commit:** `docs: session state + cross-references for editor glossary proposal workflow`

---

## §8. Progress

- [x] Stage 1 — migration 013 + ProposalRepository
- [x] Stage 2 — apply services + rejected/retired safety
- [x] Stage 3 — editor propose UI
- [x] Stage 4 — admin queue + apply-on-approve
- [ ] Stage 5 — CLI cost gate + installments
- [ ] Stage 6 — new-term corpus application step
- [ ] Stage 7 — e2e + docs

**Stage 1 note (2026-07-12):** migration 013 applied to local docker DB only; Railway prod
still pending (deferred by user this session — enable proxy + provide DSN before Stage 2
needs prod, or apply standalone whenever convenient).

**Stage 2 note (2026-07-12):** `src/review/glossary_apply.py` implements all five apply
functions. Safety filters added: `locked_terms`, `get_segment_constraints`, and
`get_stale_segments` all exclude `tu.status = 'rejected'`; `write_term_usage`'s re-INSERT
is now guarded with `NOT EXISTS (... status IN ('confirmed','rejected'))`. Added
`GlossaryRepository.sense_term_id`, `TermUsageRepository.update_sense_for_segment` /
`mark_rejected`, and `SegmentRepository.reset_segments` (the D3/D10-compliant targeted
reset with the human-edited → needs_human guard, mirroring `translate.run._guard_and_reset`)
to support the apply functions. `import_approvals.process_approval`'s already-approved
branch now delegates to `apply_rendering_change` (behavior-preserving — existing tests pass
unchanged). Full suite green (1067 tests). Other `term_usage` readers audited
(`sense_mining.py`, `build_sample.py`, `coverage_report.py`, `export_sheet.py`) are
reporting/mining tools, not spend- or constraint-critical, and were left as-is — not
extended with the `rejected` filter.

**Stage 2 code-review pass (2026-07-12):** independent review caught that
`apply_sense_here`/`apply_remove_here` ignored the UPDATE rowcount, so a stale
proposal (target row already gone — race with resolver or another approval)
would report false success and reset a segment for nothing. Fixed: both now
raise `ValueError` when the UPDATE affects zero rows; regression tests added.
Also fixed: `apply_sense_here` now rejects a `retired` target sense (previously
would silently re-point a segment to a non-constraining sense with no error and
no version bump). `export_sheet.py`'s unfiltered `term_usage` read was reassessed
and left as-is — that surface is being retired, not worth touching for a non-spend
reporting path. Full suite green (1070 tests).
