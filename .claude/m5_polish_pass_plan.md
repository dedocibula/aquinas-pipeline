/# M5 Step 2 — Polish Pass: Phased Build Plan

## Purpose

Implement the M5 "polish pass": improve the **prose** of the machine Slovak translation under hard
constraints (no terminology change, no sentence-boundary change, preserve scholastic particles and
word repetition, do not raise literary quality). Polish uses **Claude Sonnet 4.6** — synchronous for
pilot/refinement/interactive, Batch API for the full corpus.

## How an implementing model uses this document

1. Read **this file** and `docs/session_state.md`.
2. Pick the **lowest-numbered phase that is not Done and whose dependencies are all Done** (see
   *Phase status*). Build only that phase.
3. Build it **fully**: code + tests + the phase's *Acceptance* checks must pass.
4. Update `docs/session_state.md` (phase completed, files changed, exact next step) **and** tick the
   *Phase status* checklist in this file.
5. **Then clear context.** Each phase is self-contained; do not carry one phase's context into the next.

**Rules that override convenience:** Phases marked **(DDL — human-gated)** must *pause and request human
review* before any migration is applied (CLAUDE.md). Fail loudly in parsers/clients; commit with
Conventional Commits and show the diff before committing.

---

## Phase status

- [x] **Phase 0** — `polish` source row (DDL — human-gated)
- [x] **Phase 1** — LLM client encapsulation + pricing + shared constraint helper
- [x] **Phase 2** — Polish core (`src/polish/`)
- [x] **Phase 3** — Pilot = full-pipeline subset run (writes to DB)
- [x] **Phase 4** — Semi-supervised refinement (`src/optimize/`)
- [x] **Phase 5** — Interactive editor step ("Accept + Polish")
- [ ] **Phase 6** — Production Batch run

Dependency graph: `0 → 2`, `1 → 2`, `2 → {3, 5, 6}`, `3 → 4`. Phases 4/5/6 may be built in any order
once their deps are Done.

---

## Shared reference (read only the slice your phase needs)

### Core decisions
- Polish operates on `(sk, model)` of `translation_status='translated'` segments; **skips segments that
  already have a `(sk,human)` row** (human is authoritative).
- Write-back: a **new `polish` source** → `(sk, polish)` coexists with `(sk, model)`. Display precedence:
  **human → polish → model**.
- `needs_human` segments are not batch-polished; they are rescued interactively (Phase 5).
- Refinement is **semi-supervised**: the user picks the better of two polished versions; that drives the
  next prompt edit. All refinement code lives under `src/optimize/`.

### Model + API (consult the `claude-api` skill for exact SDK shapes before coding)
- Model id: **`claude-sonnet-4-6`**. Thinking omitted (polish is a constrained rewrite, not reasoning).
- Sync: `client.messages.create(model="claude-sonnet-4-6", max_tokens=…, system=[{"type":"text","text":SYS,"cache_control":{"type":"ephemeral"}}], messages=[{"role":"user","content":…}])`.
  Text = first `resp.content` block with `.type=="text"`. Usage fields: `resp.usage.cache_read_input_tokens`,
  `cache_creation_input_tokens`, `input_tokens`, `output_tokens`.
- Batch: `client.messages.batches.create(requests=[Request(custom_id, params=MessageCreateParamsNonStreaming(...))])`
  → poll `retrieve(id).processing_status == "ended"` → `results(id)` **keyed by `custom_id` (unordered)**.
- Pricing (per 1M): input $3 / output $15; cache-read ≈ $0.30; cache-write ≈ $3.75. **Batch = 50% off all.**
- Key from `ANTHROPIC_API_KEY`; every env-reading module calls `load_dotenv()` at import.

### Reusable functions (path — what to reuse)
- `storage.db`: `get_conn()` (auto commit/rollback ctx mgr), `source_id(conn, code)`.
- `storage.repositories.SegmentRepository`: `get_segment(id)`, `write_segment_text(id,'sk',src_id,content)`,
  `update_translation_status(id, status)`. `GlossaryRepository(conn).locked_terms(id) -> [Constraint]`,
  `Constraint.to_prompt_dict()`.
- `translate.loop.translate_segment(id, conn, prompt_log=None) -> (status, [UsageInfo], SegmentOutcome)` —
  the contract `polish_segment` mirrors. Writes `(sk,model)`; sets status.
- `translate.loop._build_surface_constraints(latin, constraints)`.
- `translate.translator.build_initial_user_turn` — contains the `<hard_constraints>` block builder to extract.
- `translate.prechecks.check_terminology_lemma(text, constraints) -> r(.ok, .failed_terms, .failures)`.
- `common.lemmatize.lemmatize_slovak`.
- `common.deepseek_client.DeepSeekClient.chat(messages,*,temperature,max_tokens,...) -> ChatResult(content,usage,raw)` — the client shape to mirror.
- `common.pricing.UsageInfo(model, cache_hit_tokens, cache_miss_tokens, completion_tokens, cost_usd)`,
  `extract_usage(model, json)`, `zero_usage(model)`.
- `optimize.pilot`: `_translate_worker`, `run_pilot`, `_write_report`, `fetch_sample_segments`,
  `fetch_segment_text_lengths`. `translate.run.ArticleResult/_open_run/_close_run`.
  `translate.prompt_logger.PromptLogger`. `optimize.reset_golden` (resets golden → pending, deletes `(sk,*)`).
- `optimize.run_compare` (run-vs-run diff). `src/optimize/optimize_loop.sh`, `src/optimize/prompt_changelog.md`.
- Server: `server/db.py:_segment_select_sql` (LATERAL model/human joins), `review_segment`,
  `get_article_segments`; `server/app.py:review_segment_route`; `templates/article.html`; `static/.../review.js`.
- Tests: `tests/_fakes.py` (`FakeConn`,`FakeCursor`), `tests/conftest.py` (`fake_conn`), `_seg_row`/`_term_row`.

---

## Phase 0 — `polish` source row  *(DDL — human-gated)*

**Goal:** make `(sk, polish)` writable. **Depends on:** none.
**Read:** `.claude/database.md` (`source` seed block), `migrations/` for the numbering convention.
**Build:** `migrations/0NN_polish_source.sql` inserting one row:
`('polish','sk','machine', 85, 'Claude Sonnet polish pass')` (rank between human=1 and model=90; rank is UNIQUE).
**Acceptance:** migration written; **pause and request human review; apply only after approval**; verify
`SELECT source_id FROM source WHERE code='polish'` returns one row.
**On completion:** update `docs/session_state.md` + tick Phase 0. Clear context.

## Phase 1 — LLM client encapsulation + pricing + shared constraint helper

**Goal:** an Anthropic client interchangeable with `DeepSeekClient`, plus shared prompt/usage plumbing.
**Depends on:** none.
**Read:** `src/common/deepseek_client.py`, `src/common/pricing.py`, `src/translate/translator.py`
(the `build_initial_user_turn` `<hard_constraints>` block, ~lines 76–134).
**Build:**
- `src/common/anthropic_client.py` — `AnthropicClient` whose `.chat(messages,*,max_tokens,...) -> ChatResult`
  mirrors `DeepSeekClient`. Wrap the synchronous `anthropic` SDK; cache the system block; key lazily from
  `ANTHROPIC_API_KEY`; fail loudly (raise on missing key / API error / empty content).
- `src/common/pricing.py` — add `claude-sonnet-4-6` rates (input/output/cache-read; note Batch=50%); add an
  Anthropic usage adapter mapping `cache_read`→hit, `input`+`cache_creation`→miss, `output`→completion into `UsageInfo`.
- Extract the `<hard_constraints>` block into a shared helper (e.g. `common/prompt_blocks.py:build_hard_constraints_block(constraints)`)
  and have `translate.translator` import it (no behavior change).
- `pyproject.toml` — add `anthropic>=1.x`; `uv sync`.
**Acceptance:** `uv run pytest tests/common/` green; new tests cover the usage→cost math and the constraint
helper (translator output unchanged). Client unit-tested with a faked SDK (no live call).
**On completion:** update `docs/session_state.md` + tick Phase 1. Clear context.

## Phase 2 — Polish core (`src/polish/`)

**Goal:** `polish_segment` (mirrors `translate_segment`) + guards + the polish prompt.
**Depends on:** Phase 0 (writes `(sk,polish)`), Phase 1 (client/pricing/helper).
**Read:** `src/translate/loop.py` (write/commit pattern, `_build_surface_constraints`), Phase 1 outputs,
`prompts/translator_system.txt` (the "DO NOT" block), `.claude/database.md` (polish constraints/particles).
**Build:**
- `prompts/polish_system.txt` — author from the existing constraints; reconcile the two particle lists
  (`translator_system.txt`: totiž/teda/avšak/lebo/preto vs `database.md`: totiž/teda/však/odtiaľ/ale — union them).
- `src/polish/guards.py` — `sentence_count_delta`, `locked_term_retention` (reuse `check_terminology_lemma`),
  `particle_retention` (reuse `lemmatize_slovak`), `length_ratio`; return a flags dict.
- `src/polish/polisher.py` — `polish_segment(segment_id, conn) -> (status, [UsageInfo], outcome)`:
  read `(sk,model)`; skip (no-op) if a `(sk,human)` row exists; build constraints via `locked_terms` +
  the shared helper; call `AnthropicClient`; run guards (recorded on the outcome); `write_segment_text((sk,polish))`;
  commit; return a tuple shape compatible with the pilot aggregation.
- `tests/polish/` — guards + `polish_segment` with a fake `AnthropicClient` and `FakeConn`.
**Acceptance:** `uv run pytest tests/polish/` green; a manual single-segment run writes a `(sk,polish)` row and
leaves `(sk,model)` intact.
**On completion:** update `docs/session_state.md` + tick Phase 2. Clear context.

## Phase 3 — Pilot = full-pipeline subset run (writes to DB)

**Goal:** make `optimize.pilot` an instrumented subset run of the *full* pipeline: per sample segment,
translate (write `(sk,model)`) **then** polish (write `(sk,polish)`). No flag.
**Depends on:** Phase 2.
**Read:** `src/optimize/pilot.py` (whole), `src/translate/prompt_logger.py`, `src/translate/run.py`
(`ArticleResult`, `_open_run`, `_close_run`).
**Build:**
- In the pilot worker, after `translate_segment` returns `translated` (and no `(sk,human)` row exists), call
  `polish_segment(seg_id, conn)` in the same worker connection. Two persisted writes per segment.
- Instrument both stages: extend `PromptLogger`/run JSONL so each record carries **translation + polish + guard
  flags**; extend `_write_report` (or write `reports/m5_polish_sample.txt`) with per-element-type guard
  pass-rates and polish cost (Anthropic `UsageInfo`).
- Confirm `optimize.reset_golden` deletes only `(sk,model)`/`(sk,polish)` for the golden set, never `(sk,human)`.
**Acceptance:** `ANTHROPIC_API_KEY=… DEEPSEEK_API_KEY=… PILOT_WORKERS=8 uv run python -m optimize.pilot`
on a small sample → every `translated` segment has both `(sk,model)` and `(sk,polish)` rows; JSONL shows both;
`reports/m5_polish_sample.txt` reports guard pass-rates + cost. `tests/optimize/` green.
**On completion:** update `docs/session_state.md` + tick Phase 3. Clear context.

## Phase 4 — Semi-supervised refinement (`src/optimize/`)

**Goal:** human-in-the-loop polish-prompt tuning; all code under `src/optimize/`.
**Depends on:** Phase 3 (needs ≥2 runs to compare).
**Read:** `src/optimize/run_compare.py`, `src/optimize/optimize_loop.sh`, `src/optimize/prompt_changelog.md`.
**Build:**
- Extend `optimize/run_compare.py` to render, per segment, **side-by-side polished output for two runs**
  (prior vs current) with the translation for context and guard deltas; collect the user's **1/2 + optional
  note** (a decisions file the loop reads, or an interactive prompt).
- Add `src/optimize/polish_optimize_loop.sh` (mirror `optimize_loop.sh`): per epoch → `reset_golden` → run
  pilot → `run_compare` side-by-side → **wait** for the 1/2 decision → feed the preference summary + guard
  stats to `claude -p` editing `prompts/polish_system.txt` (keep the overfitting guard) → append a
  `prompt_changelog.md` row → commit.
**Acceptance:** two epochs run; `run_compare` shows prior-vs-current polish side by side; the loop blocks on
the 1/2 decision; `prompts/polish_system.txt` + `prompt_changelog.md` update and commit.
**On completion:** update `docs/session_state.md` + tick Phase 4. Clear context.

## Phase 5 — Interactive editor step ("Accept + Polish")

**Goal:** show polish in the preview server (precedence human → polish → model) and rescue `needs_human`
false positives with one click.
**Depends on:** Phase 0 + Phase 2.
**Read:** `src/server/db.py` (`_segment_select_sql`, `review_segment`), `src/server/app.py`
(`review_segment_route`), `src/server/templates/article.html`, `src/server/static/.../review.js`,
`docs/session_state.md` (Concurrent Review Surface section).
**Build:**
- `server/db.py:_segment_select_sql` — add a `sk_polish` LATERAL join (`code='polish'`), surface
  `slovak_polish`; display fallback **human → polish → model**.
- `server/app.py` — editor-only `/api/segment/<id>/polish`: run `polish_segment` on the `(sk,model)` draft,
  write `(sk,polish)`, return polished text + guard flags. **If the segment was `needs_human`, also flip
  `translation_status` → `translated`** in the same commit (the "Accept + Polish" rescue). Guards advisory only.
- `templates/article.html` + `review.js` — on the **machine tab**, show **"Accept + Polish"** only for
  `needs_human` segments (optional re-polish affordance for `translated`); render model vs polish. Human tab
  unchanged — a `(sk,human)` save still dominates.
**Acceptance:** `tests/server/` green; in the running server, a `needs_human` segment's "Accept + Polish"
writes `(sk,polish)` and flips status to `translated`; polish shows over model; a human save overrides both.
**On completion:** update `docs/session_state.md` + tick Phase 5. Clear context.

## Phase 6 — Production Batch run

**Goal:** polish the full corpus cost-effectively via the Anthropic Batch API.
**Depends on:** Phase 2. **Also requires:** M5 Step 1 full-corpus translation done, and the element-type
scope approved by the Phase 3/4 cycle.
**Read:** Phase 2 outputs, `claude-api` skill (Batches), `src/translate/run.py` (report-writer pattern).
**Build:**
- `src/polish/batch.py` — select `translated`, non-`(sk,human)`, approved-element-type segments → chunk →
  `messages.batches.create` (custom_id = segment_id, shared cached system prompt) → poll → `results()`
  **keyed by custom_id** → guards → `write_segment_text((sk,polish))` for guard-passing segments;
  guard-failures left unpolished and logged → `reports/m5_polish_production.txt`.
**Acceptance:** dry-run on a small set → `(sk,polish)` written, `(sk,model)` preserved, guard-failures skipped,
report emitted; results correctly matched by `custom_id`.
**On completion:** update `docs/session_state.md` + tick Phase 6. Clear context.

---

## Out of scope (defer)
- M5 Step 3 (consistency report) and Step 4 (XLIFF export) — after the polish cycle.
- Whether `segment_review`-accept-only rows count as "human-touched" for the polish skip — adopt the rule
  the `rerun_stale` guard lands on.
