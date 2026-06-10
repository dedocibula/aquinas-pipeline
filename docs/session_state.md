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

## Known Gaps / Next Actions
1. **Execute the approved plan** (above) — start with migration 005 DDL draft for human review.
2. **Permanent accepts** — mark seg 199 (`toto niečo`) as accepted; evaluate `habitus`.
3. **Seg 3429 semantic error** — final cause vs efficient cause; needs manual inspection.
4. **M5 Step 1 acceptance** — after subset run: verify statuses, test crash recovery + rerun_stale.
5. **M5 Steps 2–4** — polish (Anthropic Batch API), consistency report, XLIFF export — AFTER Step 1 review.
