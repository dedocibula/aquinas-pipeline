# Glossary Completeness + Hard-Term Flow Redesign

## Context

The q1–q20 subset run surfaced 79 needs_human segments. Root-cause analysis showed ~74% are
*not* model failures but pipeline gaps: (a) the glossary is missing senses for multisense terms
(`ratio` only has `rozum` approved; temporal `principium`; etc.), so the precheck forces the wrong
term; (b) the terminology precheck false-fails on correct inflections because MorphoDiTa's
*analysis* direction is open-vocabulary and the MorfFlex SK dict lacks archaic/loan lemmas
(`čnosť`, `habitus`). Two design questions to settle before fixing: how to complete the glossary
(recall vs cost), and whether the current constraint/precheck architecture is optimal.

## Verified facts (this session)

- MorphoDiTa **generation** works: `morpho.generate('rozum', '', GUESSER, …)` → all 8 forms.
  OOV lemmas return 0 forms: `čnosť` (dict has modern `cnosť`), `habitus`.
- Evidence coverage for mining: 22,621 body segments — **91% have Czech, 99% English, 100% either**.
- Glossary: 4,387 terms, 2,527 senses in use (term_usage is corpus-wide from M2).
- Multi-turn retry is already implemented (`loop.py` appends assistant/user turns).
- `lemmatize.py` already has `pos_tag_latin` (unused by resolver) — usable for `habitum est` filtering.

---

## Part 1 — Completing the glossary (multisense recall)

### Key insight
The corpus already contains two professional, segment-aligned translations (Bahounek cs 91%,
Dominican en 99%). The proposed "translate full Summa with cs/en rails, then mine the draft"
pays ~$40–90 to generate a *third*, machine-quality translation — when the mining loop
(term → rendering → lemmatize → group → resolve back) can run directly on the existing human
translations for near zero cost. A machine draft is also *worse* mining input: an unconstrained
model tends to flatten polysemy (one Slovak word everywhere), reducing sense recall — and the
draft is throwaway, since the final translation must be re-run constraint-driven anyway.

### Options compared

| | Approach | Cost | Sense recall | Infra effort |
|---|---|---|---|---|
| **A** | Full SK draft + mine alignments (user proposal) | ~$40–90 draft + $10–20 LLM alignment (or new fast_align dep) + review storage for draft | Medium — machine draft flattens senses; circular | High — new draft storage, new review surface |
| **B** | **Mine existing cs/en corpus** (recommended) | **~$1–3** (DeepSeek labeling calls only; mining is local) | **High** — all occurrences, human-quality evidence, two independent axes (cs+en) | Low — reuses term_usage, Sheets review, import_approvals, rerun_stale |
| **C** | LLM sense-discovery by sampled contexts | ~$3–8 (1 call/term, 10–20 contexts) | Medium — sampling misses rare senses; model-judged, less auditable | Lowest — extends existing gap_terms batch pattern |
| **D** | pgvector embedding clustering of contexts | embeddings + infra | Medium | Medium-high; less interpretable than cs/en evidence |
| **E** | Reactive (harvest from needs_human feedback per run) | $0 upfront; ~3 wasted iterations × every affected segment per missed sense, every run | Lowest, slowest | None |

### Recommended: B with C-style labeling (hybrid)

Pipeline (new module `src/ingest/sense_mining.py`, pattern-follows `gap_terms.py`):

1. **Collect**: for each glossary term, via existing `term_usage` join — all segments containing it,
   with cs + en text. (No new resolution pass needed.)
2. **Mine renderings**: lemmatize cs text (existing `lemmatize_czech`); rank Czech lemmas by
   association with the Latin lemma (log-odds vs corpus baseline). Same for English tokens.
   One dominant rendering → confirm single sense, no API call. **Multiple strong distinct
   renderings → polysemy candidate.**
3. **Label senses**: one DeepSeek batch call per candidate term: sample contexts per rendering
   cluster → returns per sense: `context_label` (English, 3–6 words), `en_cue`, `cs_lemma`,
   proposed `sk`. Writes `glossary_sense(status='proposed')` + 3 `sense_rendering` rows —
   exactly what `_resolve_multi`'s evidence vote consumes.
4. **Review**: existing `export_sheet.py` flow (context_label is already editable col D).
   Reviewer approves senses; `import_approvals.py` bumps versions.
5. **Re-resolve**: re-run resolver Phase 2 for segments containing upgraded terms (sense vote
   now has multiple senses to choose between); `rerun_stale` re-translates affected segments.

Recall safety net: senses invisible to both cs and en evidence (both languages collapse the
distinction) fall through to option E (reviewer feedback) — rare, acceptable.

---

## Part 2 — Hard-term constraint flow

### Current frictions
1. **Precheck direction is backwards**: it lemmatizes every *draft* token (open vocabulary —
   exactly where MorphoDiTa fails: `čnostiam` ↛ `čnosť`) instead of generating the closed set
   of forms for the ~3 required terms. False fails → wasted retries → needs_human.
2. **Prompt wording fights the checker**: "HARD TERM CONSTRAINTS (verbatim, no exceptions)"
   tells the model not to inflect; the precheck accepts any inflection. Mixed signal.
3. **False constraints upstream**: CLTK lemmatizes `habitum` (perfect passive of *habere*,
   "as was stated") → `habitus` noun; resolver writes a bogus term_usage; precheck then demands
   "habitus" in Slovak; reviewer rejects the result. 3+ segments per run.
4. **Full re-translation retry for one missing term** is wasteful and usually reproduces the
   same synonym (observed: 3/3 identical failures), burning 2 extra full iterations.

### Alternatives considered
- **Marker emission** (model wraps terms in ⟦⟧, checker strips): brittle, pollutes output — rejected.
- **Constrained decoding / logit bias**: not available on DeepSeek API — rejected.
- **Predicting the exact required Slovak inflection** from Latin case: unreliable (case systems
  don't map 1:1) — rejected; let the model inflect, as today.

### Recommended bundle (presuming complete glossary from Part 1)

Keep: surface-form Latin → Slovak lemma mapping in the prompt (good anchoring), model inflects.

1. **Generation-based precheck** (`prechecks.py`): for each required Slovak word, precompute its
   full inflected-form set via `morpho.generate` (verified working); match draft tokens against
   the set. OOV fallback (`čnosť`, `habitus`): normalized stem-prefix match (`čnos`, `habitus`).
   Cache per-term form sets (`functools.lru_cache`). Deterministic, closed-vocabulary, and faster
   (no per-token analysis of the whole draft).
2. **Targeted term-fix micro-edit** (`loop.py`): when the *only* failure is terminology, send a
   small follow-up turn — "replace word X with the correctly inflected form of Y; change nothing
   else" — instead of a full re-translation turn. Tiny output, much higher convergence.
   Structural/semantic failures keep the full retry path.
3. **Prompt wording** (`translator.py` + `prompts/translator_system.txt`): replace "verbatim, no
   exceptions" with "use this Slovak lemma, inflected as the grammar of your sentence requires".
4. **`habitum est` false-constraint fix**: in `_build_surface_constraints` (`loop.py`), skip a
   surface match when the token is `habitum/habita` immediately followed by `est/sunt` in the
   Latin (unambiguously *habere*). Optionally harden at resolver level later using existing
   `pos_tag_latin`.

### Expected impact on the 79 needs_human
- Part 2 fix 1: eliminates `čnosť`-class false fails (~10 segs/run) and `habitus`-OOV fails.
- Part 2 fix 4: eliminates `habitum est` class (~3 segs/run + reviewer rejections).
- Part 1: eliminates wrong-sense forcing (`ratio` ~24 segs, `principium`, `species`, …).
- Remaining: genuine semantic errors + Latin-output (~14) — correctly needs_human.

## Decisions (user-confirmed)
- Glossary: **Option B** — mine existing cs/en corpus, DeepSeek labels multi-rendering candidates only.
- Term flow: **all four fixes** (generation precheck, micro-edit retry, prompt wording, habitum-est filter).
- Staleness: **standard rerun_stale flow** — affected segments re-translate after sense approvals.
- Run analytics: **yes** — minimal versioned-run schema (see Part 3).
- Term overwrite policy: **automate re-runs; failure tail goes to preview server; guard human-edited segments** (see Part 4).

---

## Part 3 — Versioned run analytics

Purpose: make structural/incremental changes measurable (regression vs improvement) instead of
manual JSONL forensics. PromptLogger JSONL stays as the deep-dive artifact; DB stores the
queryable dimensions.

**Migration 005** (⚠ stop for human DDL review before applying):
- `translation_run`: run_id PK, started_at, finished_at, git_sha, prompt_hash (sha256 of
  translator_system.txt + reviewer_system.txt), glossary_snapshot (approved sense count +
  max version), translator_model, reviewer_model, temperature, filters (pars, max_question),
  max_workers, totals (segments, translated, needs_human), total_cost_usd, jsonl_path.
- `run_segment`: run_id FK, segment_id FK, final_status, iterations_used, chosen_iteration,
  cost_usd, failure_classes jsonb (per-iteration: class + detail, e.g.
  `[{"iter":1,"class":"precheck_terminology","term":"rozum"}]`), last_feedback.

**Loop changes** (`loop.py`): classify failures at failure time (the loop knows which check
failed — no post-hoc log parsing): `precheck_terminology(term)`, `precheck_structure`,
`reviewer_revision`, `preamble`, `latin_output`, `translator_error`. Return per-segment record;
`run.py` opens a run row at flow start, bulk-inserts run_segment rows, closes with totals.

**Analysis tool** (`src/translate/run_compare.py`): compare two runs — status flips per segment,
failure-class deltas, cost/segment, avg iterations, cache hit. Output to
`reports/run_compare_<a>_<b>.txt`.

Not doing (overkill): prompts/drafts in DB, dashboards, token-level logs.

---

## Part 4 — Term-overwrite re-evaluation policy

Layered, mostly automated:
1. **Clean majority**: sense version bump → `rerun_stale` (existing) → re-translation passes →
   done, zero human involvement. Cost: cents per term change (segment-scoped, Principle 3).
2. **Re-failure tail**: still fails after 3 iterations → `needs_human` + `last_feedback` →
   existing preview server queue; human edits final text directly (`/api/edit` + approve).
3. **Human-edit guard (new)**: in `rerun_stale`, stale segments that already have a
   `segment_text(sk, human)` row are NOT reset to pending. Instead set
   `translation_status='needs_human'` + reviewer_note `"term updated after human edit — verify"`.
   Never auto-overwrite or churn reviewed work.
4. **Structurally impossible constraints**: analytics (Part 3) surface terms whose re-failure
   tail recurs across runs → candidate for permanent-accept (like `hoc aliquid → toto niečo`)
   instead of burning 3 iterations every cycle.

## Implementation order
1. **Migration 005** (run analytics tables) — draft DDL, stop for human review, apply.
2. **Part 2 fixes** (small, independent, immediately reduce needs_human):
   prechecks → habitum-est filter → micro-edit retry → prompt wording.
3. **Part 3 wiring**: failure classification in loop, run/run_segment writes in run.py,
   run_compare tool. (Before the next big run so changes are measurable.)
4. **Part 4 guard** in rerun_stale.
5. **Part 1 mining module** + dry-run on known polysemes (`ratio`, `species`, `principium`).
6. Export candidates to Sheets → human review cycle → `import_approvals` → `rerun_stale`.

## Files to create/modify

| File | Change |
|---|---|
| `migrations/005_run_analytics.sql` | new — translation_run + run_segment (human review before apply) |
| `src/ingest/sense_mining.py` | new — Part 1 steps 1–3 (collect, mine, label) |
| `src/translate/prechecks.py` | generation-based form matching + OOV stem fallback |
| `src/translate/loop.py` | micro-edit retry turn; habitum-est filter; failure classification |
| `src/translate/run.py` | run row lifecycle; run_segment bulk insert; human-edit guard in rerun_stale |
| `src/translate/run_compare.py` | new — cross-run regression/improvement report |
| `src/translate/translator.py` | constraint block wording |
| `prompts/translator_system.txt` | matching wording change |
| `tests/…` | tests alongside each module |

## Verification
- Unit: form-set generation for `rozum/viera/milosť` + OOV fallback for `čnosť/habitus`;
  `habitum est` filter; micro-edit path.
- Mining dry-run on known polysemes (`ratio`, `species`, `principium`) — must rediscover the
  senses we found manually (pojem/dôvod/hľadisko; začiatok).
- Re-run the 56 precheck-failure needs_human segments after fixes; target: only genuine
  semantic failures remain.
