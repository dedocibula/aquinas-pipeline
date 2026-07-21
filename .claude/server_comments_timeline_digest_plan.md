# Build Plan — Comment Threads, Admin Timeline & Daily Digest

> **Persisted staged build plan** for cold-context, stage-by-stage implementation. Lives in
> `.claude/` alongside the other `*_plan.md` docs. A model implements one stage at a time:
> read §0 + the Appendix + the assigned stage, implement, commit, then start the next fresh.

## How to use this document (for the implementing model)

This plan is built for **cold-context execution**: each stage is self-contained. Workflow:

1. **Always read §0 Orientation first** — it holds the project facts, conventions, file
   anchors, and data model every stage assumes.
2. **Read the Appendix** (concrete DDL / dataclasses / SQL / skeletons) — stages reference it
   by letter instead of repeating code.
3. **Read only your assigned Stage.** It lists: goal, what it depends on, the files to read,
   exactly what to implement, acceptance checks, and the commit message.
4. Implement → run the stage's acceptance checks → **show the diff and get approval, then
   commit** (Conventional Commits) → stop. Context is cleared; the next stage starts fresh.
5. **DDL gate:** Stage 1 writes a migration. Per project rule, **pause for human review before
   running any migration**; do not apply DDL without explicit approval.

Dependency order: **1 → 2 → 3 → 4 → 5**. (3 needs 2; 4 needs 1; 5 needs 1+2.)

---

## §0 Orientation (read first, every stage)

### What this is
The Aquinas pipeline translates Aquinas's *Summa* into Slovak. This plan extends its **Flask
preview/review server** (`src/server/`) with three surfaces:

- **Comment threads** — Google-Docs-style, editor-only, per-segment threaded comments in a
  right-hand sidebar, opened by a 💬 button placed under the existing "Review" button. Flat
  thread per segment; resolve/reopen. **Coexists with** (does not replace) the current single
  public "translator note".
- **Admin timeline** (`/timeline`, admin-only) — a chronological activity feed of human
  reviews, comments, and machine-run markers; each editorial entry deep-links to its segment.
  **No new per-row timestamp** — uses existing timestamps only.
- **Daily email digest** — one consolidated end-of-day email per user listing unread replies
  in threads they're part of. SMTP via stdlib; recipients = thread participants + the segment's
  reviewer (minus the reply's own author); sent by a daily Prefect flow.

### Locked decisions (do not re-litigate)
- New query results are **frozen dataclasses in `src/storage/models.py`** — never dicts. (This
  is the one models home; the existing dict-returning helpers in `db.py` are left as-is —
  converting them is out of scope.)
- Comments are **editor-internal** (not shown to anonymous readers). The public note UI stays.
- Timeline uses only existing timestamps (`segment_review.human_reviewed_at`,
  `translation_run.finished_at`, `segment_comment.created_at`); machine translations appear only
  as coarse run markers. It shows each segment's **latest** review (`segment_review` is an
  upsert — no full edit history); accepted trade-off.
- Email: **stdlib `smtplib`** (no new dependency). Recipients = **participants + reviewer**,
  minus the reply's author. **Daily Prefect flow**, ~18:00 Europe/Bratislava, one email/user.

### Stack & how to run / test
- Python 3.12 + `uv`. Flask (`server.app:app`), Jinja2, vanilla JS. Auth = Google OAuth
  (authlib). Postgres 16 (ltree + pgvector). Prefect ≥3 already a dependency (M5 flows in
  `src/translate/run.py`). **No frontend framework, no build step.**
- Run server (dev): `uv run flask --app server.app run --debug` (port 5000).
- Tests: `uv run pytest tests/server` (and `tests/notify` once it exists). Server tests use
  in-memory fakes (`FAKE_SEGMENTS`) — no live DB required.
- DB from Python (no psql on PATH): `psycopg2.connect('postgresql://aquinas:aquinas@localhost:5432/aquinas')`.
- Migrations run via `uv run python scripts/migrate.py` (applies `migrations/*.sql` in numeric
  order; tracks `schema_migrations`). **STOP for human review before running.**

### Conventions (match these exactly)
- **All server SQL lives in `src/server/db.py`** (module of functions, not classes). Routes in
  `src/server/app.py` call those functions; **no raw SQL in `app.py`**.
- **`get_conn()` owns the commit boundary** (`from storage.db import get_conn`). db.py functions
  must **not** call `conn.commit()` themselves.
- **Optimistic locking** precedent = `review_segment` (`db.py:861`): `UPDATE ... WHERE
  human_version = %s RETURNING ...`; a missing row ⇒ conflict ⇒ HTTP 409.
- **Auth decorators** (`app.py:90-104`): `@requires_editor` (403 unless `session["is_editor"]`),
  `@requires_admin` (403 unless `is_editor` **and** `is_admin`). `session["email"]` is the
  logged-in email. `is_editor`/`is_admin` also exposed to every template via the context
  processor `_inject_user` (`app.py:217`).
- **JSON routes** return `{ok: true, ...}` / `{ok: false, error: ...}` with 200/404/403/409.
- **Frontend JS** = vanilla IIFE modules (`(function(){ 'use strict'; ... }())`), `fetch` +
  `JSON.stringify`, `data-*` attributes, `_currentUserEmail` global injected via `tojson`.
  Precedent: `src/server/static/review.js`.
- **Migrations**: `migrations/NNN_snake_case.sql`, next is **015**. Each carries a
  `-- STOP: human review required before running` header. After writing, **mirror** the DDL into
  `db/schema.sql` (the fresh-install source of truth) and document it in `.claude/database.md`
  (state each column's consumer).
- Commit often, Conventional Commits (`feat:`/`fix:`/`refactor:`). **Show the diff before
  committing.**

### File map with anchors (so you needn't re-explore)
- `src/server/app.py` — `_WORK_ID=1` (73); decorators (90-104); `url_to_ltree` (116),
  `_ltree_to_url_locator`→`ltree_to_url` (152); OAuth callback sets session (190); context
  processor (217); `index` (231); `_question_view` (274); `_article_view` (313);
  `review_segment_route` `/api/segment/<id>/review` (398); `glossary_proposals_page`
  `@requires_admin` (583) — **the admin-page precedent**; approve/unapprove (670/689).
- `src/server/db.py` — `_segment_select_sql` (38); `get_question_articles` (149);
  `get_article_segments` (178); `get_translation_progress` (246); `get_pending_proposals_view`
  (503); **`review_segment` (861)** — the upsert/optimistic-lock precedent; `is_editor` (1003);
  `is_admin` (1010).
- `src/storage/models.py` — frozen dataclasses (`@dataclass(frozen=True)`); **add new models
  here**. `src/storage/db.py` — `get_conn` (35), `source_id` (48).
- `src/server/templates/` — `base.html` (layout; `{% block content %}` / `{% block scripts %}`);
  `index.html` (progress-legend badges 8-29; admin-only glossary badge 21-23);
  `article.html` (imports `_segment_panel.html as panel` 1-2; `<tr data-segment-id=...>` rows;
  `panel.segment_panel(...)` ~96; `panel.status_cell(...)` ~98; editor scripts 147-151);
  `question.html` (same panel usage for preamble rows); **`_segment_panel.html`** —
  `segment_panel(seg, seg_constraints, is_editor, pending_counts={})` macro (slovak-display +
  `human-note` 22-31; review panel + note area 34-82) and **`status_cell(seg, is_editor)`**
  macro (199-220, holds `.btn-review`); `glossary_proposals.html` (admin queue precedent).
- `src/server/static/` — `review.js` (`_doAction` 90-104; open/close 122-140; note handlers
  227-269), `proposals.js`, `style.css`.

### Data model facts (from `db/schema.sql`)
- `segment` (87-96): `segment_id serial PK`, `work_id`, `locator_path ltree` (e.g. `I.q3.a1.arg2`;
  article = first 3 labels), `element_type`, `reply_to`, `translation_status`
  (`pending|translated|needs_human`), `reviewer_notes jsonb`.
- `segment_text` (107-113): `(segment_id, lang, content, source_id)`, `UNIQUE(segment_id, lang,
  source_id)`. **No timestamp column.** Slovak source codes: `model` / `polish` / `human`
  (via `source.code`; `source_id(conn, code)` resolves them). `v_segment` (274-293) pivots to
  `slovak_draft/polish/final`.
- `translation_run` (209-227): `run_id`, `started_at timestamptz DEFAULT now()`, `finished_at`,
  plus model/cost/counts (`flow_name`/status counts as available — inspect columns when writing
  the timeline query). `run_segment` (234-245): per-segment run outcome, no own timestamp.
- `editor` (322-324): `email text PK`; `admin boolean` added by migration 014. `is_admin` = an
  editor row with `admin=true`.
- **`segment_review` (334-340)**: `segment_id PK`, `human_reviewed_by text`, `human_reviewed_at
  timestamptz DEFAULT now()`, `human_note text NULL`, `human_version int` (optimistic lock).
  One row per segment; created on Save/Accept/Note; deleted on Reset.

---

## Stage 1 — Migration 015, schema mirror, and models

**Goal:** Create the two new tables and the six new dataclasses; nothing wired yet.
**Depends on:** nothing. **Prereq:** read §0.

**Read:** `migrations/` (naming + STOP header; the latest is `014_editor_admin.sql`),
`db/schema.sql` (the `segment_review` block, to mirror its doc-comment style), `.claude/database.md`
(how tables are documented), `src/storage/models.py` (existing `@dataclass(frozen=True)` style),
`scripts/migrate.py` (the runner).

**Implement:**
1. `migrations/015_segment_comments.sql` — **Appendix A** verbatim (both `segment_comment` and
   `comment_thread_state`, with the `-- STOP` header).
2. Mirror both `CREATE TABLE`/index statements into `db/schema.sql` with a doc-comment block
   matching `segment_review`'s style.
3. Document both tables in `.claude/database.md`, stating each column's consumer (per Appendix A
   comments).
4. Add the six dataclasses to `src/storage/models.py` — **Appendix B** verbatim. Import
   `datetime` as needed; keep the module free of any server/session import.

**DDL GATE:** Do **not** run the migration. Present the SQL diff and request human review. Only
after approval: `uv run python scripts/migrate.py` against the local docker DB, then verify both
tables exist (psycopg2 connection string in §0).

**Acceptance:**
- `git diff` shows the migration, the `db/schema.sql` mirror, the `.claude/database.md` entry,
  and the models.
- `uv run python -c "import storage.models"` imports cleanly (dataclasses valid).
- (After approved apply) `\d segment_comment` / `\d comment_thread_state` equivalents via
  psycopg2 confirm the shapes; `db/schema.sql` reproduces them on a fresh install.

**Commit:** `feat(db): add segment_comment + comment_thread_state (migration 015) and models`

---

## Stage 2 — Comment backend (db.py functions + routes + tests)

**Goal:** Full server-side CRUD/resolve/read for threads, plus batch counts with unread. No UI.
**Depends on:** Stage 1. **Prereq:** read §0.

**Read:** `src/server/db.py` — **`review_segment` (861)** for the upsert/optimistic-lock and
`source_id` usage; `get_translation_progress` for the count-query shape. `src/server/app.py` —
`review_segment_route` (398) for request/response shape; `_article_view` (313) / `_question_view`
(274) for where per-segment view data is assembled and passed to templates; the `requires_editor`
decorator (90). Existing `tests/server/` for the fixture/fake pattern.

**Implement (all SQL in `db.py`; no commits inside functions):**
- Functions per **Appendix C** (signatures + SQL): `list_comments`, `add_comment`,
  `resolve_thread`, `reopen_thread`, `delete_comment`, `get_comment_counts(..., viewer_email)`,
  `mark_thread_read`. Each returns the Stage-1 dataclasses (or `str`/`int` status as noted).
- Routes in `app.py`, all `@requires_editor`, per **Appendix D**:
  `GET /api/segment/<int:segment_id>/comments` (also calls `mark_thread_read` for
  `session["email"]`), `POST .../comments`, `POST .../comments/resolve`, `.../comments/reopen`,
  `DELETE /api/comment/<int:comment_id>`. Serialize dataclasses with `dataclasses.asdict`;
  render `created_at`/`resolved_at` as `.isoformat()` (jsonify can't serialize a datetime).
- Wire counts into the views: in `_article_view` and `_question_view`, call
  `get_comment_counts(conn, [segment_ids], session.get("email"))` and pass the resulting
  `dict[int, CommentCount]` to the template as `comment_counts` (Stage 3 consumes it).

**Acceptance:**
- New `tests/server/` tests (extend `FAKE_SEGMENTS` pattern): add→list roundtrip; resolve sets
  all rows resolved and a new comment reopens; `delete_comment` forbids non-authors (403-mapped);
  `get_comment_counts` returns correct `total`/`open_count`/`unread` against a seeded
  `last_read_at`; `mark_thread_read` upserts; **auth**: anonymous/non-editor → 403 on every route.
- `uv run pytest tests/server` green.

**Commit:** `feat(server): comment thread backend (CRUD, resolve, read-state, counts)`

---

## Stage 3 — Comment frontend (button, sidebar, JS, CSS)

**Goal:** The 💬 button under Review, the right-hand thread drawer, and its JS/CSS.
**Depends on:** Stage 2. **Prereq:** read §0.

**Read:** `_segment_panel.html` — the **`status_cell` macro (199-220)** where `.btn-review` lives,
and the `segment_panel` signature (to thread `comment_counts` through); `article.html` /
`question.html` — `<tr data-segment-id=...>` rows, the `panel.status_cell(...)` call site, and
the editor-scripts block (147-151) where `review.js` loads with `_currentUserEmail`. `review.js`
in full (mirror its structure); `style.css` (badge/panel styles to match).

**Implement:**
- **Comment button** in `status_cell`, directly under `.btn-review`:
  `<button class="btn-comment" data-segment-id="{{ seg.segment_id }}"
   data-open-count="{{ c.open_count }}" data-unread="{{ c.unread }}">💬 {{ c.open_count or '' }}</button>`
  where `c = comment_counts.get(seg.segment_id)` (guard `None` → zeros). Style to stand out when
  `open_count>0`; show an unread dot when `unread>0`. It's already inside `{% if is_editor %}`.
  Thread `comment_counts` into `segment_panel`/`status_cell` calls in `article.html` +
  `question.html`. **Leave the existing "Add Note" button (`_segment_panel.html:48-62`) untouched.**
- **Row anchor:** add `id="seg-{{ seg.segment_id }}"` to each segment `<tr>` in `article.html`
  and `question.html` (enables native `#seg-<id>` deep-links from timeline + digest).
- **`_comment_sidebar.html`** (new partial) — a hidden right-hand fixed drawer `#comment-sidebar`
  (title, comment list, reply `<textarea>` + "Add comment", Resolve/Reopen, Close). Include it
  once from `article.html` and `question.html`.
- **`src/server/static/comments.js`** (new, vanilla IIFE like `review.js`) — per **Appendix E**:
  open drawer on `.btn-comment` click → `GET .../comments` → render cards (author · relative time
  · body · Delete on own, compared to `_currentUserEmail`); Add comment (POST); Resolve/Reopen;
  Delete; keep the button's `data-open-count`/`data-unread` + label in sync; highlight the active
  row. Load it in the editor-scripts block next to `review.js`.
- **`style.css`** — `.btn-comment` + open-count badge + unread dot; `#comment-sidebar` drawer;
  comment cards; resolved (muted/collapsed) styling; active-row highlight.

**Acceptance (manual, via running server):**
- `uv run flask --app server.app run --debug`; log in as an editor; open an article.
- 💬 under Review opens the drawer; add two comments (they render with author/time); Resolve
  collapses/marks the thread and the badge updates; a new comment reopens; Delete works only on
  your own; the unread dot clears after opening; the **public note is unaffected**; reloading the
  page preserves state. (Non-editors never see the button — `status_cell` is editor-gated.)

**Commit:** `feat(server): Google-Docs-style comment sidebar with resolve/reopen`

---

## Stage 4 — Admin timeline

**Goal:** `/timeline` admin activity feed + index link.
**Depends on:** Stage 1 (needs `segment_comment` for the comment source). **Prereq:** read §0.

**Read:** `app.py` — **`glossary_proposals_page` (583)** as the `@requires_admin` page precedent
and `_ltree_to_url_locator`/`ltree_to_url` (152); `db.py` — `get_pending_proposals_view` (503) for
a multi-source read shape; `db/schema.sql` — `segment_review` (334), `segment_comment` (Stage 1),
`translation_run` (209) columns (inspect the actual run columns before writing the query);
`index.html` progress-legend (8-29) + the admin glossary badge (21-23); `glossary_proposals.html`
+ `base.html` for the page/template shape.

**Implement:**
- `get_activity_feed(conn, *, before=None, limit=50) -> list[ActivityEntry]` in `db.py` — per
  **Appendix F**: `UNION ALL` of review rows (`segment_review`⋈`segment`), comment rows
  (`segment_comment`⋈`segment`), and run markers (`translation_run`), projected to
  `(ts, kind, author, segment_id, locator, summary, …run fields)`, `ORDER BY ts DESC LIMIT`
  (+`WHERE ts < before` when paginating), mapped into `ActivityEntry`.
- `GET /timeline` (`@requires_admin`) in `app.py` → `render_template("timeline.html", entries=…,
  ltree_to_url=_ltree_to_url_locator, next_before=…)`. Pagination via `?before=<iso ts>` query
  param (server-rendered; no JS module needed).
- `timeline.html` (new, extends `base.html`) — entries grouped under **day headers**. Editorial
  entry: time · author · action · locator link `"/~" + ltree_to_url(article_locator) + "#seg-" +
  segment_id` · snippet. Run entry: a distinct full-width marker. Footer "Load older activity"
  link (`?before=`) when the page is full.
- `index.html` — add an `{% if is_admin %}` link to `/timeline` in the progress-legend, beside
  the glossary badge. `style.css` — timeline list, day headers, run markers.

**Acceptance:**
- Tests: `get_activity_feed` shaping (three kinds merge + order desc; `before` paginates); route
  auth (non-admin → 403). `uv run pytest tests/server` green.
- Manual: as admin, `/timeline` shows reviews + comments + run markers grouped by day; clicking an
  editorial entry lands on the segment (row highlighted via `#seg-<id>`); "Load older" paginates;
  non-admin gets 403 / no link.

**Commit:** `feat(server): admin activity timeline`

---

## Stage 5 — Daily email digest

**Goal:** `src/notify/` (SMTP sender + Prefect digest flow) driven by the read/notify watermarks.
**Depends on:** Stage 1 + Stage 2 (tables + `mark_thread_read` wiring). **Prereq:** read §0.

**Read:** `src/translate/run.py` (existing Prefect `@flow`/`@task` style + how flows get a DB
connection); `db.py` (`get_conn`, `source_id`, `_ltree_to_url_locator` equivalent for building
links — note the locator→URL helper lives in `app.py`; either move a small pure copy into `db.py`
or build the path in `notify` from the locator string); §0 data-model facts for
`segment_review.human_reviewed_by` (the reviewer recipient).

**Implement:**
- `src/server/db.py`: `collect_digests(conn) -> list[UserDigest]` (**Appendix G** SQL — recipients
  = participants ∪ reviewer, minus each comment's author; email-worthy = `created_at >
  COALESCE(GREATEST(last_read_at, last_notified_at), '-infinity')`; group per recipient) and
  `mark_thread_notified(conn, segment_id, user_email)` (upsert `last_notified_at = now()`).
- `src/notify/__init__.py`, `src/notify/email_sender.py` — `EmailSender` over stdlib `smtplib`
  (`SMTP`/`SMTP_SSL`) + `email.message.EmailMessage`; config from env `SMTP_HOST/PORT/USER/PASS`,
  `MAIL_FROM`, `PUBLIC_BASE_URL`; `send(to, subject, text_body)`. Add a `DryRunEmailSender` (logs,
  no network) for tests/local. **Fail-closed**: raise a clear error if required SMTP env is missing.
- `src/notify/digest.py` — Prefect flow `send_comment_digest` (**Appendix H**): `collect_digests`
  → per recipient render one **text** email (subject *"Aquinas: N new replies to threads you're
  in"*; body groups items by locator with author · time · snippet + deep link
  `PUBLIC_BASE_URL + "/~" + <article locator url> + "#seg-" + id`) → `send` → **only on success**
  `mark_thread_notified` for that recipient's segments (per-recipient commit). Idempotent: a clean
  re-run yields `[]`. A CLI entry (`python -m notify.digest`) is fine for manual runs.
- Scheduling is an **operational step** (a Prefect deployment, daily ~18:00 Europe/Bratislava) —
  document it; no code gate.

**Acceptance:**
- `tests/notify/`: `collect_digests` shaping (excludes self; includes the reviewer; respects both
  watermarks; NULL watermark ⇒ all unread); idempotency (after `mark_thread_notified`, re-run is
  empty; marking read before the run suppresses the item); `EmailSender` against a monkeypatched
  `smtplib` (asserts recipients/subject/body, deep links) + fail-closed on missing env.
- `uv run pytest tests/notify tests/server` green.
- Manual: with `DryRunEmailSender`, two editors comment on one segment; run `send_comment_digest`;
  each is "emailed" the other's reply (not their own), the reviewer is included, links resolve,
  second run is a no-op.

**Commit:** `feat(notify): daily email digest of unread comment replies`

---

## Appendix (concrete code — copy, don't re-derive)

### A. `migrations/015_segment_comments.sql`
```sql
-- STOP: human review required before running.
-- Adds editor-internal threaded comments per segment + per-(user,segment) read/notify state.

-- Flat thread per segment; resolution is thread-level.
-- author/created_at -> sidebar + timeline + digest; resolved* -> thread state / open-count badge.
CREATE TABLE segment_comment (
    comment_id   serial      PRIMARY KEY,
    segment_id   integer     NOT NULL REFERENCES segment(segment_id),
    author       text        NOT NULL,               -- editor email (plain text, like human_reviewed_by)
    body         text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    resolved     boolean     NOT NULL DEFAULT false,
    resolved_by  text        NULL,
    resolved_at  timestamptz NULL
);
CREATE INDEX segment_comment_segment_idx ON segment_comment (segment_id, created_at);

-- Per-(user,segment) watermarks.
-- last_read_at  -> in-app unread dot + digest filter (bumped when the user opens the thread).
-- last_notified_at -> digest de-dupe (bumped when a digest covering these comments is sent).
CREATE TABLE comment_thread_state (
    segment_id       integer     NOT NULL REFERENCES segment(segment_id),
    user_email       text        NOT NULL,
    last_read_at     timestamptz NULL,
    last_notified_at timestamptz NULL,
    PRIMARY KEY (segment_id, user_email)
);
```

### B. Dataclasses (append to `src/storage/models.py`)
```python
@dataclass(frozen=True)
class Comment:
    comment_id: int
    segment_id: int
    author: str
    body: str
    created_at: datetime
    resolved: bool
    resolved_by: str | None
    resolved_at: datetime | None

@dataclass(frozen=True)
class CommentThread:
    comments: list[Comment]
    resolved: bool        # thread is resolved when it has comments and none are open
    open_count: int

@dataclass(frozen=True)
class CommentCount:
    total: int
    open_count: int
    unread: int           # comments by others newer than the viewer's last_read_at

@dataclass(frozen=True)
class ActivityEntry:
    ts: datetime
    kind: str             # "review" | "comment" | "run"
    author: str | None
    segment_id: int | None
    locator: str | None
    summary: str
    translated: int | None = None      # run-only
    needs_human: int | None = None     # run-only
    cost: float | None = None          # run-only

@dataclass(frozen=True)
class DigestItem:
    segment_id: int
    locator: str
    author: str
    created_at: datetime
    body: str

@dataclass(frozen=True)
class UserDigest:
    user_email: str
    items: list[DigestItem]
```

### C. `db.py` comment functions (shapes)
```python
def list_comments(conn, segment_id) -> CommentThread:
    # SELECT ... FROM segment_comment WHERE segment_id=%s ORDER BY created_at ASC
    # build [Comment(...)]; open_count = sum(not c.resolved); resolved = bool(comments) and open_count==0

def add_comment(conn, segment_id, author, body) -> Comment:
    # INSERT ... RETURNING *  (new comment defaults resolved=false -> reopens the thread)

def resolve_thread(conn, segment_id, resolver_email) -> int:
    # UPDATE segment_comment SET resolved=true, resolved_by=%s, resolved_at=now()
    #   WHERE segment_id=%s AND resolved=false  -> return rowcount

def reopen_thread(conn, segment_id) -> int:
    # UPDATE segment_comment SET resolved=false, resolved_by=NULL, resolved_at=NULL
    #   WHERE segment_id=%s AND resolved=true   -> return rowcount

def delete_comment(conn, comment_id, requester_email) -> str:
    # SELECT author FROM segment_comment WHERE comment_id=%s
    #   None -> "notfound"; author!=requester -> "forbidden"; else DELETE -> "ok"

def mark_thread_read(conn, segment_id, user_email) -> None:
    # INSERT INTO comment_thread_state (segment_id,user_email,last_read_at) VALUES (%s,%s,now())
    #   ON CONFLICT (segment_id,user_email) DO UPDATE SET last_read_at = now()

def get_comment_counts(conn, segment_ids, viewer_email) -> dict[int, CommentCount]:
    # For segment_ids: total=count(*), open_count=count(resolved=false),
    #   unread=count(author<>viewer AND created_at > that viewer's last_read_at)
    # Left-join comment_thread_state on (segment_id, viewer_email) for last_read_at.
    # Return {segment_id: CommentCount(...)}; segments with no comments may be omitted.
```

### D. Route shapes (`app.py`, all `@requires_editor`)
```python
@app.route("/api/segment/<int:segment_id>/comments", methods=["GET"])
def list_comments_route(segment_id):
    with get_conn() as conn:
        thread = list_comments(conn, segment_id)
        mark_thread_read(conn, segment_id, session["email"])
    return jsonify({"ok": True, **_thread_json(thread)})   # created_at/resolved_at -> .isoformat()

# POST .../comments {body}        -> add_comment  -> {ok, comment}
# POST .../comments/resolve       -> resolve_thread(session["email"])
# POST .../comments/reopen        -> reopen_thread
# DELETE /api/comment/<id>        -> delete_comment(id, session["email"]);
#                                    "notfound"->404, "forbidden"->403, "ok"->200
# Use a small helper to asdict() a Comment/CommentThread and stringify datetimes.
```

### E. `comments.js` structure (mirror `review.js`)
```
(function () { 'use strict';
  function openSidebar(segId) { fetch GET .../comments -> render list + set active row }
  function renderComment(c) { author, time, body; Delete button iff c.author === _currentUserEmail }
  addComment: POST .../comments {body}     -> append, bump data-open-count, clear unread dot
  resolve/reopen: POST .../comments/(resolve|reopen) -> restyle thread + button
  del: DELETE /api/comment/<id>            -> remove card, adjust count
  wire .btn-comment click -> openSidebar(dataset.segmentId); Close button hides #comment-sidebar
}());
```

### F. `get_activity_feed` query skeleton
```sql
SELECT ts, kind, author, segment_id, locator, summary, translated, needs_human, cost FROM (
  SELECT sr.human_reviewed_at AS ts, 'review' AS kind, sr.human_reviewed_by AS author,
         sr.segment_id, s.locator_path::text AS locator,
         (CASE WHEN sr.human_note IS NOT NULL THEN 'noted' ELSE 'reviewed' END) AS summary,
         NULL::int AS translated, NULL::int AS needs_human, NULL::float AS cost
  FROM segment_review sr JOIN segment s USING (segment_id)
  UNION ALL
  SELECT c.created_at, 'comment', c.author, c.segment_id, s.locator_path::text,
         left(c.body, 140), NULL, NULL, NULL
  FROM segment_comment c JOIN segment s USING (segment_id)
  UNION ALL
  SELECT COALESCE(r.finished_at, r.started_at), 'run', NULL, NULL, NULL,
         'machine run', <translated_col>, <needs_human_col>, <cost_col>
  FROM translation_run r
) feed
WHERE (%(before)s IS NULL OR ts < %(before)s)
ORDER BY ts DESC
LIMIT %(limit)s;
-- Replace <..._col> with the real translation_run columns (inspect the table first).
```

### G. `collect_digests` query skeleton
```sql
WITH participants AS (
  SELECT DISTINCT segment_id, author AS user_email FROM segment_comment
  UNION
  SELECT sr.segment_id, sr.human_reviewed_by
  FROM segment_review sr
  WHERE sr.human_reviewed_by IS NOT NULL
    AND sr.segment_id IN (SELECT DISTINCT segment_id FROM segment_comment)
)
SELECT p.user_email, c.segment_id, s.locator_path::text AS locator,
       c.author, c.created_at, c.body
FROM participants p
JOIN segment_comment c ON c.segment_id = p.segment_id
JOIN segment s        ON s.segment_id = c.segment_id
LEFT JOIN comment_thread_state st
       ON st.segment_id = p.segment_id AND st.user_email = p.user_email
WHERE c.author <> p.user_email
  AND c.created_at > COALESCE(GREATEST(st.last_read_at, st.last_notified_at),
                              '-infinity'::timestamptz)
ORDER BY p.user_email, c.created_at;
-- Group rows by user_email in Python -> UserDigest(user_email, [DigestItem(...)]).
```

### H. `send_comment_digest` flow shape
```python
@flow
def send_comment_digest():
    sender = EmailSender.from_env()            # or DryRunEmailSender in tests/local
    with get_conn() as conn:
        digests = collect_digests(conn)
    for d in digests:                          # one email per user
        subject, body = render_digest(d)       # groups items by locator; builds #seg-<id> links
        sender.send(d.user_email, subject, body)
        with get_conn() as conn:               # only after a successful send
            for seg_id in {i.segment_id for i in d.items}:
                mark_thread_notified(conn, seg_id, d.user_email)
```

### Env (Stage 5, document in README/.env.example — do not commit secrets)
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `MAIL_FROM`, `PUBLIC_BASE_URL`.
