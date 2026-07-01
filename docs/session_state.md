# Session State

## Current Milestone
M5 — **Step 1 IN PROGRESS** — Prefect orchestration built. Full corpus run not yet executed.

## M4 Deliverables (status)
- `migrations/004_translation_status.sql` — **applied**; `translation_status` + `reviewer_notes` columns live
- `src/translate/translator.py` — DeepSeek V3 caller; `build_user_turn` single-turn (system+user only)
- `src/translate/reviewer.py` — DeepSeek R1 caller; `max_tokens=8000`; `_parse_verdict` updated (XML + bottom-up)
- `src/translate/prechecks.py` — `check_structure` + `check_terminology_lemma` (MorphoDiTa Slovak); both wired
- `src/translate/loop.py` — `translate_segment()`; preamble guard; reviewer_notes on exhausted path
- `src/translate/prompt_logger.py` — `notes` field added to `log_iteration`; wired in `loop.py`
- `src/translate/pilot.py` — PILOT_FULL mode; ThreadPoolExecutor; thread-safe PromptLogger
- `src/common/lemmatize.py` — `lemmatize_slovak` added; Slovak MorphoDiTa model downloaded
- `src/server/app.py` — Flask preview server; `/api/edit` + `/api/approve` endpoints
- `src/server/db.py` — `save_segment_text`, `approve_segment`, `get_segment_constraints`
- `src/server/templates/article.html` — inline editable SK field; reviewer_notes detail panel
- `src/review/sheets.py` — `delete_stale_rows` (single batchUpdate, reverse-sorted)
- `src/review/export_sheet.py` — stale-row cleanup wired into `export_tab`
- `prompts/translator_system.txt` — FORMATTING; LEGIBILITY; GRAMMAR (passive infinitive examples)
- `prompts/reviewer_system.txt` — semantics + legibility only; `<verdict>` XML tags

## Latest Pilot Results (debug_1780989182.jsonl — post-glossary-trim retranslation)
250 segments (249 reset after trim + 1 new) | **236 translated (94.4%) | 14 needs_human (5.6%)**
Avg iterations: 1.33 | Cost: $0.50 | Cache hit: 50.4%

| Failure type | Count | Root cause |
|---|---|---|
| Preamble injection | 9 | Model sees own preamble in `prior_draft`; pattern-matches and regenerates it. Feedback ignored because it's flattened into a single user message (no real conversation turn). |
| Terminology miss | 5 | Single-turn architecture: feedback is text in a document, not a real conversational reply. Model has no incentive to correct. |

### All 14 needs_human
| Seg | Path | Failure |
|---|---|---|
| 198 | I.q1.a10.respondeo | `scientia→poznanie` missing all 3 iters |
| 199 | I.q1.a10.reply1 | `hoc aliquid→toto niečo` — permanent/accept |
| 231 | I.q1.a6.arg2 | Preamble all 3 iters |
| 242 | I.q1.a7.respondeo | `scientia→poznanie` iter 1; REVISION_NEEDED iters 2–3 |
| 2356 | I.q3.a3.arg3 | Preamble all 3 iters |
| 2358 | I.q3.a3.arg5 | Preamble all 3 iters |
| 2369 | I.q3.a4.arg4 | Preamble all 3 iters |
| 2375 | I.q3.a2.reply3 | REVISION_NEEDED (precheck passes; semantic issue) |
| 2413 | I.q3.a8.arg2 | Preamble all 3 iters |
| 2414 | I.q3.a8.arg3 | Preamble all 3 iters |
| 2836 | I.q4.a2.respondeo | `ratio→rozum` missing |
| 3403 | I.q5.a2.arg4 | Preamble all 3 iters |
| 3436 | I.q5.a5.sed_contra | `natura→prirodzenosť`, `ratio→rozum`, sed_contra formula |
| 3852 | I.q6.a3.arg1 | `habitus→habitus`; REVISION_NEEDED |

## Root Cause Analysis: Single-Turn Architecture

`call_translator_v3` sends exactly two messages per call: `system` + `user`.
On retry, `prior_draft` + `prior_feedback` are concatenated into the **same user message**
as the translation request. The model reads feedback as document text, not as a conversational
reply — it has no internal state that says "I said something wrong and must fix it."

**Preamble symptom:** `prior_draft` starts with "Ok, rozumiem..." → model pattern-matches its own
output format and regenerates identical preamble. Confirmed: 3/3 iterations identical byte-for-byte.

**Terminology symptom:** Model writes a synonym; feedback says "use X, not Y"; next call again
produces a synonym because the feedback is just another paragraph in a long user message.

**Fix options (ranked by impact):**
1. **Multi-turn messages** — send proper `[system, user, assistant, user]` chain so the model
   sees its own draft as an assistant turn and feedback as a user correction. Highest impact,
   changes `call_translator_v3` signature to accept `messages: list[dict]` instead of scalars.
   Breaks prompt-cache on retry turns (but retries are rare; 94.4% succeed on iter 1).
2. **Strip preamble from `prior_draft`** before sending back — removes the pattern the model
   copies from. Keeps single-turn architecture. Low risk of format drift since the preamble
   prefix is structurally separate from the translation body.
3. **Temperature 0 after terminology failure** — reduces sampling variance; may help if the
   model "knows" the right term but samples a synonym at T=0.3.
4. **Permanent accepts in glossary** — `toto niečo`, possibly `habitus` — mark as
   `accept_needs_human` so we don't waste 3 iterations on structurally impossible constraints.

## Glossary State
- 1,858 noise senses deleted (prose-category + first-person verb suffixes)
- 249 dependent segments reset to pending and retranslated
- Google Sheet: 2,423 Review rows + 118 Auto-resolved (post-trim)

## Previous Session: Glossary Data Fixes
- **CLTK mismatch merges** (3 terms): `intellectus→intellego`, `providentia→provideo`, `similiter→similis`
- **NULL category fix**: 134 Krystal-seeded terms assigned `category='term'`
- **`ratio` 2nd sense**: `context_label='as aspect/notion'`, `sk='hľadisko'`
- **intellego/similis senses downgraded**: cannot converge on CLTK verb/adjective keys

## Latest Pilot Results (post prompt-fix run — 294 total, 254 with Latin)
254 segments | **245 translated (96.5%) | 9 needs_human (3.5%)**
Avg iterations: 1.11 | Cost: $0.42 | Cache hit: 48.9%

| Seg | Path | Failure |
|---|---|---|
| 199 | I.q1.a10.reply1 | `toto niečo` — permanent/accept |
| 233 | I.q1.a6.sed_contra | `principium` 2nd sense (začiatok) |
| 242 | I.q1.a7.respondeo | `čnosť` precheck all 3 iters |
| 1912 | I.q2.a3.respondeo | `rozum` precheck all 3 iters |
| 2408 | I.q3.a7.respondeo | `rozum` precheck all 3 iters |
| 3419 | I.q5.a3.reply3 | Latin output — reviewer correctly flagged |
| 3429 | I.q5.a4.reply3 | Semantic error: final cause → efficient cause |
| 3436 | I.q5.a5.sed_contra | `prirodzenosť`/`rozum` + formula |
| 3852 | I.q6.a3.arg1 | `habitus` precheck all 3 iters |

## This Session (2026-06-09)

### Code review findings addressed
- **`/api/edit` silent 200 bug fixed** (`server/app.py`): now returns 404 when `save_segment_text` returns `False` (missing or pending segment).
- **Empty REVISION_NEEDED feedback bug fixed** (`loop.py`): `None`/empty feedback no longer appended as zero-length user message to DeepSeek conversation; loop breaks with a warning instead.

### M4 completion verified
- All core deliverables done. `concupiscentia` context_labels migrated to English in DB. `providentia` has no approved senses yet (no migration needed). LEGIBILITY rule confirmed complete (Czech text already in user turn).
- **M4 status: DONE. Ready for M5.**

## M5 Step 1 Deliverables (status)
- `src/common/corpus_db.py` — **DONE**. 5 corpus-wide DB helpers: `get_all_article_locators`, `get_pending_segment_ids_for_article`, `has_pending_segments`, `get_stale_segments`, `reset_translation_status`
- `src/translate/run.py` — **DONE**. Prefect 3.7 flows: `translate_corpus` (ThreadPoolTaskRunner, MAX_WORKERS env), `rerun_stale`; report writers for `m5_production.txt` + `m5_needs_human.txt`
- `pyproject.toml` — **DONE**. `prefect>=3.0` added.
- `tests/common/test_corpus_db.py` — 10 tests, all passing
- `tests/translate/test_run.py` — 12 tests, all passing

## This Session (2026-06-10)

### q1–q20 subset run (4 pars, MAX_WORKERS=10)
- Added `--pars` / `--max-questions` filter to `translate_corpus` (committed f0d3bec).
- Fixed `ORDER BY` alias bug in `get_all_article_locators` (committed ad28f99).
- Run launched on I, I_II, II_II, III q1–q20 (3,803 pending segments). **Check final
  status in `reports/m5_production.txt` / `m5_needs_human.txt`.**

### needs_human root-cause analysis (79 segs mid-run)
~74% are pipeline gaps, not model failures:
- `ratio→rozum` forced where ratio means concept/aspect/reason (~24 segs) — missing senses.
- `čnosť`/`habitus` precheck false-fails — MorfFlex SK dict lacks these lemmas (has `cnosť`);
  precheck lemmatizes draft tokens (open vocab) instead of generating forms (closed vocab).
- `habitum est` (perfect passive of habere) mislemmatized to noun `habitus` → bogus constraint (3 segs).
- Preamble injection (4), Latin output (4), genuine semantic errors (~10), permanent-accept (1).

### APPROVED PLAN: `.claude/m5_glossary_and_term_flow_plan.md`
Four parts, all decisions user-confirmed:
1. **Glossary sense mining (Option B)**: mine existing cs/en aligned corpus (91%/99% coverage)
   for multi-rendering Latin lemmas; DeepSeek labels candidates only (~$1–3). NOT full-draft mining.
2. **Term-flow fixes (all four)**: generation-based precheck (morpho.generate verified working,
   OOV stem fallback), terminology micro-edit retry, prompt wording ("verbatim"→"inflect as needed"),
   habitum-est filter.
3. **Run analytics**: migration 005 (`translation_run` + `run_segment`, failure_classes jsonb);
   failure classification in loop; `run_compare.py` tool. DDL needs human review before apply.
4. **Term-overwrite policy**: automate rerun_stale; failure tail → preview server; NEW guard —
   stale segments with human SK row are flagged needs_human, never auto-reset.

Implementation order: migration 005 → Part 2 fixes → Part 3 wiring → Part 4 guard → Part 1 mining → review cycle.

## M5 Plan Implementation Progress (session 2026-06-10, continued)

All five implementation steps complete and committed:

| Step | Commit | Status |
|---|---|---|
| Migration 005 (`translation_run` + `run_segment`) | applied | DONE |
| Part 2: generation-based precheck + OOV fallback | committed | DONE |
| Part 2: habitum-est filter (TEMPORARY) | committed | DONE |
| Part 2: micro-edit retry + prompt wording | committed | DONE |
| Part 3: run analytics wiring (`run.py`, `run_compare.py`) | committed | DONE |
| Part 4: human-edit guard in `rerun_stale` | committed | DONE |
| Part 1: `sense_mining.py` + 21 tests | 5a71db5 | DONE |

### Dry-run on known polysemes (`--terms ratio species principium`)
- **principium** → CANDIDATE: `původ` 54% lift=16, `počátek` 23% lift=15, `začátek` 18% lift=14 ✓
- **species** → CANDIDATE: `druh` 73% lift=13, `podoba` 20% lift=8 ✓
- **ratio** → single/none — Bahounek consistently uses one Czech rendering; polysemy invisible to cs mining (English evidence needed for `ratio`)

### Pending cleanup
- `_drop_habere_ppp_constraints` in `loop.py` is TEMPORARY — delete after POS-aware resolver fix + term_usage purge (scheduled as Part 1 re-resolution).

## Server: Concurrent Review Surface (2026-06-22)

All three phases of `.claude/server_concurrent_review_plan.md` are done and verified.

| Phase | Commits | Status |
|---|---|---|
| Phase 1 — Migration 009 (`segment_review` DDL) | archived | DONE |
| Phase 2 — Backend (db.py + app.py + tests) | aa89d29 | DONE |
| Phase 3 — Templates + JS (article.html, question.html, review.js) | cfe1d27 | DONE |

**Verified behaviors:**
- `human → machine → awaiting` fallback renders correctly (anonymous + editor)
- `human_note` renders publicly beneath segment when set
- Review panel, status column, and "Reviewed by" line are editor-only (`{% if is_editor %}`)
- `review_segment(save)` upserts `segment_review` + `segment_text(sk,human)`; leaves `translation_status` untouched
- `review_segment(accept)` creates review row only (no human text)
- `review_segment(note)` stores/clears note without touching text
- `review_segment(reset)` deletes both `segment_review` and `segment_text(sk,human)`; machine text survives
- Optimistic-lock conflict → `("conflict", None)` → HTTP 409
- Save on `pending` segments works (guard removed)
- Old `/api/edit` and `/api/approve` endpoints are gone; `git grep` confirms no references in src/ or tests/
- 58 server tests green

**Design note:** The "Save" and "Accept" buttons from the spec were merged into one **Accept** button. When the textarea has content it sends `action:'save'`; when empty it sends `action:'accept'`. Intentional UX simplification — confirmed no DB-level issues.

**M5 follow-ups still pending** (from plan § Out of scope):
- `rerun_stale` should clear `segment_review` when flagging human-edited segment `needs_human`
- Decide whether `segment_review`-only rows (accept without text) should be treated as "human-touched" in the stale guard

## M5 Polish Pass — Phase Status (2026-06-28)

- [x] **Phase 0** — `polish` source row: migration `010_polish_source.sql` applied; `source_id=8`, `authority_rank=85`.
- [x] **Phase 1** — LLM client encapsulation + pricing + shared constraint helper (commit eb05b28)
  - `src/common/anthropic_client.py` — `AnthropicClient` + `AnthropicAPIError`; mirrors `DeepSeekClient`; system prompt cached via `cache_control: ephemeral`; lazy `ANTHROPIC_API_KEY`.
  - `src/common/pricing.py` — `claude-sonnet-4-6` + `claude-sonnet-4-6-batch` rates; `extract_anthropic_usage()` adapter.
  - `src/common/prompt_blocks.py` — `build_hard_constraints_block()` extracted from `translator.py` (no behaviour change).
  - `pyproject.toml` — `anthropic>=0.40` (installed 0.112.0).
  - 44 tests green in `tests/common/`; 147 translate tests unaffected.
- [x] **Phase 2** — Polish core (`src/polish/`) — 35 tests green
  - `prompts/polish_system.txt` — generalised to "Scholastic theological text" (not Summa-specific); particle union (totiž/teda/avšak/lebo/preto/však/odtiaľ/ale).
  - `src/polish/guards.py` — `sentence_count_delta`, `locked_term_retention`, `particle_retention`, `length_ratio`, `run_guards`; advisory; ok=True requires delta=0 + all terms + all particles + ratio∈[0.5,2.0].
  - `src/polish/polisher.py` — `polish_segment(id,conn,*,_client) -> (status,[UsageInfo],PolishOutcome)`; skips on (sk,human); uses lemma-form constraints only (no CLTK surface expansion — polisher works on Slovak text); guards advisory; always writes (sk,polish) on success.
- [x] **Phase 3** — Pilot = full-pipeline subset run (commit c3a755a)
  - `src/translate/prompt_logger.py` — `log_polish()` method added (type="polish" JSONL record).
  - `src/optimize/pilot.py` — `SegmentStats` extended (element_type, polish_status, polish_usages, guard_flags); `_translate_worker` calls `polish_segment` in same connection after successful translation; `_write_polish_report` writes `reports/m5_polish_sample.txt` with per-element-type guard pass-rates + Anthropic cost; early-return path also writes polish report.
  - `src/optimize/reset_golden.py` — DELETE now joins `source` and restricts to `code IN ('model','polish')`; never deletes `(sk,human)` rows.
  - 66 tests green.
- [x] **Phase 4** — Semi-supervised refinement (commit 58e4ca7)
  - `src/optimize/run_compare.py` — `--polish` mode: `parse_polish_jsonl`, `_guard_line`, `_render_polish_pair`, `build_polish_report` (interactive 1/2/s, writes `reports/polish_decisions_<ts>.txt`); fail-loud on malformed JSONL.
  - `src/optimize/polish_optimize_loop.sh` — per-epoch loop; POSIX-compatible (no mapfile); overfitting guard in claude -p prompt.
  - `src/optimize/polish_prompt_changelog.md` — empty table.
  - `src/polish/polisher.py` — `PolishOutcome.polished_text` set after `conn.commit()` to prevent ghost JSONL records.
  - `src/translate/prompt_logger.py` — `log_polish` carries `polished_text`.
  - 24 new tests green.
- [x] **Phase 5** — Interactive editor step (commit a03e9a8)
  - `src/server/db.py` — `_segment_select_sql` adds `sk_polish` LATERAL join; `slovak_polish` column; `human → polish → model` fallback.
  - `src/server/app.py` — `POST /api/segment/<id>/polish` (editor-only): runs `polish_segment`, flips `needs_human → translated`, returns `{ok, polished_text, guard_flags, flipped}`.
  - `src/server/templates/article.html` — polish-draft-section; "Accept + Polish" (needs_human) + "Re-polish" (translated) buttons.
  - `src/server/static/review.js` — `_doPolish()`, `_updatePolishDisplay()`, button handlers.
  - 7 new server tests; 65 total green.
- [x] **Phase 6** — Production Batch run (commits 87cf06b, 331ea6f)
  - `src/polish/batch.py` — `fetch_batch_candidates` (skip already-done), `_build_request` (cache_control ephemeral), `_poll_batch` (60s loop + request_counts log), `_process_results` (custom_id keyed, per-result commit, try/except continues on write failure, three-bucket pricing), `run_batch`, `_write_report` → `reports/m5_polish_production.txt`. `_REPORTS_DIR` anchored to `__file__`.
  - `src/polish/steps.py` — `PolishCorpusStep` thin wrapper.
  - `src/pipeline/interactive.py` — "Polish corpus (Batch API)" menu item (position 11, between rerun-stale and reset-corpus).
  - `src/server/static/style.css` — machine tab fonts (#222), btn-accept-polish (green), btn-repolish (grey), polish-text-ro (green-tinted), machine-actions flex, machine-label, polish-guard-info.
  - `src/server/templates/article.html` — `data-needs-human="1"` on review panel.
  - `src/server/static/review.js` — `needs_human` segments open to machine tab (Accept + Polish immediately visible).
  - 17 tests in `tests/polish/test_batch.py`, 52 total polish tests green.

**All M5 polish phases complete.** Next: trigger full corpus run via `uv run python -m pipeline` → "Polish corpus (Batch API)" once ANTHROPIC_API_KEY is populated in `.env`.

## This Session (2026-07-01) — Approve/Un-Approve Flow + Post-Review Fixes

### Approve/Un-Approve flow (replaces live-API "Accept + Polish")

Removed `POST /api/segment/<id>/polish` (live Anthropic API call). Replaced with:
- **Approve** button (machine pane, `needs_human` only): flips `needs_human → translated`, queuing the segment for the next `python -m pipeline → polish-corpus` Batch API run.
- **Un-Approve** toggles back to `needs_human` (blocked if `(sk,polish)` row already exists).
- Zero raw SQL in `app.py` — all logic in `db.py`.

| File | Change |
|---|---|
| `src/server/db.py` | Added `is_editor()`, `approve_segment()`, `unapprove_segment()` |
| `src/server/app.py` | Removed polish endpoint; added approve/unapprove endpoints |
| `src/server/templates/article.html` | Single Approve button replaces Accept+Polish/Re-polish |
| `src/server/static/review.js` | Approve/Un-Approve toggle handler; polish handlers removed |
| `tests/server/test_server.py` | Removed 8 polish tests; added 8 approve/unapprove tests |

### Post-review fixes (code-reviewer agent findings)

| Fix | Detail |
|---|---|
| Atomic UPDATE (TOCTOU) | `approve_segment` and `unapprove_segment` now use `UPDATE ... WHERE <guard> RETURNING segment_id` — single atomic operation; no SELECT-then-UPDATE race |
| Removed internal commit | Both functions removed `conn.commit()`; `get_conn()` owns the commit boundary (consistent with `review_segment`) |
| `unapprove` polish check atomic | `NOT EXISTS` subquery embedded in the UPDATE itself — check and flip are one operation |
| `src_type` column bug | Fixed `unapprove_segment` to join `source` on `code='polish'` instead of non-existent `src_type` column |
| Fixture gap | Added missing `"slovak_polish": None` to FAKE_SEGMENTS rows 2–4 |

66 server tests green.

## This Session (2026-07-01) — Batch Code Review Fixes (commit ee2c3c7)

Five issues from the `batch-reviewer` agent addressed:

| Fix | What changed |
|---|---|
| Single HTTP materialise | `_process_results` now accepts `results_list: list` (pre-fetched); callers materialise once with `list(client.messages.batches.results(...))`. Eliminates the two-pass double-HTTP pattern. |
| In-flight segment guard | `fetch_batch_candidates` + `submit_batch` accept `segment_ids: list[int] | None`. Pipelined path in `translate_corpus` tracks newly-translated IDs and passes them per submit, preventing re-queuing of segments already in a prior batch. |
| Crash-and-resume safety | `collect_batch` first pass now skips segments with an existing `(sk,polish)` row (same `NOT EXISTS` filter as `fetch_batch_candidates`). Counts as `polished`, avoids double-write on resume. |
| Exception safety | Both `_submit_polish_batch()` calls in pipelined mode wrapped in `try/except`; API errors log and continue — never abort the translation run. |
| Module-level env var | `_POLISH_BATCH_SIZE` at module import removed; `polish_batch_size = int(os.getenv(...))` now read inside `translate_corpus` at call time. |

All 1015 tests green.

## Known Gaps / Next Actions

*(Verified 2026-06-22 against live DB and source.)*

1. **Full corpus run** — 24,686 pending (93%); only q1–q20 done. This is the only M5 Step 1 blocker.
2. **`rerun_stale` + `segment_review` integration** — two open decisions:
   - `rerun_stale` should clear `segment_review` when flagging a human-edited segment `needs_human`.
   - Decide whether `segment_review`-only rows (accept without text) count as "human-touched" in the stale guard.
3. **Seg 199 (`toto niečo`) permanent accept** — single segment, still `pending`; not a blocker.
4. **M5 Steps 2–4** — polish (Anthropic Batch API), consistency report, XLIFF export — AFTER corpus run and review cycle.

### Resolved (no longer open)
- POS-aware resolver: `_HABERE_PPP_RE`/`_HABERE_PPP_FORMS` live in `resolver.py:246–275`; `loop.py` is clean.
- `ratio` polysemy: 4 senses with Slovak (`rozum`, `hľadisko`, `dôvod`, `ráz`) — added manually.
- Sense mining export: no `proposed_sense` table; ratio gap closed without the Sheets mining cycle.
- Bogus `habitus` term_usage rows: all 723 rows are `krystal_single`; no PPP-sourced rows remain.

## Formula Terms — DB State (applied 2026-06-11)

One-off `seed_formula_terms.py` was run and deleted. The following is now live in the DB and must be reproduced by the glossary-rebuild pipeline:

**Migration 006** (`migrations/006_sense_rendering_la_lang.sql`):
- `sense_rendering.lang` check extended to include `'la'` (was cs/en/sk only).
- `term_usage.resolution_method` check extended to include `'formula_backfill'`.

**glossary_term changes:**
- `sed_contra`: `is_multiword=True`, `la_surface='Sed contra'` written to `sense_rendering(lang='la', source='model')`.
- `respondeo`: `is_multiword=True`, `la_surface='Respondeo dicendum quod'` written to `sense_rendering(lang='la', source='model')`.

**term_usage backfill:**
- 2,655 `sed_contra` segment rows inserted (`resolution_method='formula_backfill'`, `confidence='auto'`).
- 2,599 `respondeo` segment rows inserted.

**Glossary-rebuild must:**
1. Set `is_multiword=True` for structural formula terms (sed_contra, respondeo) — slug keys cannot be phrase-matched otherwise.
2. Write `la` sense_rendering with the anchoring surface prefix (`_match_pattern` uses it over `latin_lemma`).
3. Insert `term_usage` rows for all segments of the matching `element_type` using `resolution_method='formula_backfill'`.
4. `praeterea` stays singleword — CLTK finds it via token lemmatization; `^`-anchoring would break mid-sentence occurrences.
