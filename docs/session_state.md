# Session State

## Current Milestone
M4 — **IN PROGRESS** — Gate 1 cleared. Glossary trim (1,858 senses) + retranslation (250 segs) complete.
Preview server fully operational with editable translations and approve endpoint.

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

## Known Gaps / Next Actions
1. **Multi-turn + prompt fixes** — ✅ DONE. Removed `_PREAMBLE_RE` loop hack; translator prompt explicitly forbids preambles and Latin output; reviewer CRITICAL block catches Latin output and routes to needs_human; Czech/English passed to reviewer as cross-check.
2. **Permanent accepts** — mark seg 199 (`toto niečo`) as accepted; evaluate `habitus`.
3. **`principium` 2nd sense** (seg 233) — "in principio X" = "at the beginning" → `začiatok`
4. **Persistent terminology failures** (segs 242, 1912, 2408, 3436, 3852) — `rozum/čnosť/habitus/prirodzenosť` model avoids these; needs glossary or prompt-level fix
5. **Seg 3429 semantic error** — final cause vs efficient cause; needs manual inspection
6. **Gate 1 human review**: inspect translated output at `http://localhost:5000`
