# Server Cleanup — Milestone 03: Fold the Parallel Layer Toward Repositories

**Precondition:** milestones 01 and 02 merged. Domain reads in `db.py` already return typed models
(02); `ltree_to_url` is passed to `article.html` (01, item 1.5).
**Read `server_cleanup_00_index.md` first** for shared context and rules.
**Behavior-preserving only** — same routes, same JSON shapes, same rendered HTML.

## Scope
Stop `src/server/db.py` from re-implementing domain SQL that already lives in
`src/storage/repositories.py`. After this milestone `server/db.py` should shrink toward **server-only**
concerns: comments, activity feed, digests, proposal-display enrichment, editor/admin checks. Domain
reads (segments, constraints, senses) go through repositories. This aligns the server with the
repo-wide "all SQL lives in repositories" rule that every non-server module already follows.

This is the broadest milestone but it is well test-covered. Move in small, individually-verified
steps; commit in sub-parts if the diff grows.

## Do this

### 3.1 Reuse existing repository methods instead of inline duplicate SQL
Replace hand-written joins in `db.py` with the repository methods that already encode the same rules:

- **Constraints:** `get_segment_constraints` → use `GlossaryRepository.locked_terms(segment_id)`.
  It already returns `list[Constraint]` and already applies `tu.status <> 'rejected'`,
  `gs.status = 'approved'`, `sr.lang = 'sk'`, and authority-rank ordering. If the server needs the
  batch (`dict[int, list[Constraint]]` over many segment_ids) shape, **add a batch method to
  `GlossaryRepository`** (e.g. `locked_terms_for(segment_ids)`) rather than keeping the inline SQL —
  put the SQL in the repository, call it from the server.
- **Senses:** `segment_has_locked_sense`, `_sense_blast_radius`, and `get_term_senses` should call
  `GlossaryRepository.get_current_sense` / `.sense_ids_for_term` / `.locked_terms` instead of
  re-inlining the same joins. Add narrowly-scoped repository methods where a needed shape is missing.
- **Segments:** `_segment_select_sql` + `get_article_segments` should reuse
  `SegmentRepository.get_segment` / `.load_body_segments` (both already return typed `Segment` with
  the display-precedence lateral joins). If the article/question read needs a slightly different WHERE
  or ordering, add a method to `SegmentRepository` — do not keep a parallel SELECT in the server.
- If milestone 01's interim helper `_locked_term_join_sql()` (item 1.6) was created, **delete it** —
  it is superseded here.
- Genuinely presentation-only enrichment (e.g. `_origin_locator`, formatting) may stay server-local,
  but must **not** duplicate a join a repository already owns.

**Verify after each replacement** that the produced rows/models are identical (same ordering, same
filtering). The tests in `tests/server` + `tests/` cover these paths — run them between steps.

### 3.2 `ProposalRepository` returns a typed `Proposal` model
- `ProposalRepository` (`get`, `list_pending`, `list_decided`, `pending_by_sense`, `clone_as_pending`,
  `decide`, …) currently returns `dict` / `list[dict]`, unlike its sibling repositories.
- Add a `Proposal` frozen dataclass to `src/storage/models.py` (follow the `Comment` model shape:
  `from_row` + `as_dict`). Return `Proposal` from the read methods.
- Update `db.py`'s `_enrich_proposal_display` and `get_pending_proposals_view` /
  `get_decided_proposals_view`, and `glossary_proposals.html`, to consume the typed model
  (attribute access). Rendered proposal-queue HTML must be identical.

### 3.3 Address the proposal-view N+1 (opportunistic — keep the diff contained)
- `get_pending_proposals_view` fans out ~7 queries per proposal (`_enrich_proposal_display` +
  `get_term_senses` + `_origin_locator` + `get_current_sense` + `get_sk_rendering_content` +
  `_sense_blast_radius`). Once senses/constraints come from shared repository methods (3.1), batch the
  per-proposal lookups (one query keyed by sense_id / segment_id, then map in Python) instead of
  per-row loops.
- **Strictly opt-in / behavior-identical.** Output must not change — only the number of round-trips.
  If batching risks changing ordering or content, **skip it** and leave a `# TODO` noting the N+1;
  correctness and identical output outrank the optimization.

### 3.4 Retire the last hand-rolled locator logic in templates
- `article.html`'s breadcrumb hand-rolls the `_ltree_to_url_locator` q→Q / a→A conversion as a Jinja
  append loop (it existed because the route didn't pass `ltree_to_url` — fixed in 01/1.5).
  `glossary_proposals.html` writes `p.origin_locator.split('.')[:3] | join('.')` twice.
- Replace both with the shared helper: use `ltree_to_url` / `_ltree_to_url_locator`, or add a tiny
  `article_path` Jinja filter/context helper in `app.py`, so locator formatting lives in exactly one
  place. Rendered links/anchors must be byte-identical.

## Files
- `src/storage/repositories.py` (new/extended methods for batch constraints, sense lookups, segment
  reads; `ProposalRepository` typed returns)
- `src/storage/models.py` (new `Proposal` model)
- `src/server/db.py` (delete duplicated SQL; call repositories; consume `Proposal`) — this file
  should get noticeably shorter
- `src/server/app.py` (any `article_path` helper/filter for 3.4)
- `src/server/templates/article.html`, `glossary_proposals.html` (3.4)

## Reuse (don't reinvent)
- `GlossaryRepository.locked_terms` / `.get_current_sense` / `.sense_ids_for_term`;
  `SegmentRepository.get_segment` / `.load_body_segments`.
- `Constraint` / `Segment` / `Sense` models; the `Comment` model as the pattern for `Proposal`.
- `_ltree_to_url_locator` / `ltree_to_url` for locator formatting.
- `get_conn()` transaction boundary — do not add `.commit()` anywhere.

## Constraints
- Behavior-preserving; **no DDL** (this is a query-relocation, not a schema change — if a repository
  method seems to need an index to match performance, **stop and request review**, don't add one);
  fail loudly; boring code; no M-labels. Show diff before commit. Full rules in
  `server_cleanup_00_index.md`.
- Move SQL **into** repositories; never leave two copies of the same join. When done, a grep of
  `db.py` for the `term_usage → glossary_sense → glossary_term` join and the segment lateral-join
  SELECT should find them gone (delegated to repositories).

## Verify
1. `uv run ruff check src/server src/storage` → clean.
2. `uv run pytest -q tests/server` → green; then `uv run pytest -q` → green (this milestone touches
   shared `src/storage/` — the **full** suite matters here, not just server tests).
3. Manual smoke (psycopg2; `psql` not on PATH → `uv run python3`): article + question views render the
   same segments, constraints/locked-terms, and breadcrumbs as before; the proposal queue shows the
   same rows, blast-radius, and cost; approve/reject/reopen still work.
4. Spot-check no behavior drift: pick a fixed segment and a fixed proposal, dump the rendered page
   before/after (`git stash` toggle) and diff — expect no differences.

## Commit
Show the diff, get approval, then:
`refactor(server): route domain reads through repositories; typed Proposal model; drop duplicated SQL`
(end with the `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer).

## Done
After 03 merges, the server cleanup is complete: one persistence layer for domain data, typed returns
throughout, and no triplicated constants or locator logic. `server/db.py` now holds server-only
concerns. No further milestone.
