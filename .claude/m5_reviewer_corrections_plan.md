# Handoff Brief — Type-A Glossary Corrections, Cost-Gated Retranslation & Polisher Safety Fix

> **Audience:** a fresh model/engineer with no prior session context. This document is
> self-contained: it carries all the research so you do **not** need to re-explore. Read it
> top to bottom, then execute the phases in order. Every non-obvious claim below was verified
> against the live production DB or the code on **2026-07-10**.

---

## 0. Orientation — read these first

Project: `/Users/agalad/Workspace/python/aquinas-pipeline` — a pipeline translating Aquinas's
*Summa Theologiae* from Latin to Slovak. Before touching anything, read (project rule):
`docs/claude-corrections.md`, `.claude/database.md`, `.claude/decisions.md`, and
`docs/session_state.md`. Key operating rules from `CLAUDE.md`:

- **Commit often**, Conventional Commits; **show diffs before committing** (get approval).
- **Pause for human review before any prod DB mutation** (this whole plan mutates prod).
- **Fail loudly** in parsers; **boring, debuggable Python**; no new dependencies without approval.
- Three principles: (1) quality via *constraints* not model freedom; (2) **Krystal glossary is
  the authority**; (3) **re-runs are segment-scoped** via `version`/`sense_version_used`, never
  corpus-wide.

### Database access

The real data lives on **Railway production** (the local `docker` DB is empty). Connect with
`psycopg2` (psql is not on PATH):

```python
import psycopg2, psycopg2.extras
DSN = "postgresql://postgres:<REDACTED>@tokaido.proxy.rlwy.net:23100/railway"
conn = psycopg2.connect(DSN)
```

> ⚠️ **This is a live production credential exposed via Railway's TCP proxy and may have rotated.**
> If it fails, ask the user to re-enable the proxy and provide the current URL. Never commit it.
> The proxy is normally OFF; the user turns it on for these sessions.
> Cast `ltree` columns with `::text` before string ops. Local dev DSN (empty):
> `postgresql://aquinas:aquinas@localhost:5432/aquinas`.

---

## 1. Why this work exists (context)

A human theological reviewer (`galadova@gmail.com`) reviewed one full article, **`I.q1.a1`**,
plus the **`I.q1` preamble**, through the Flask review server. Her edits (stored as
`segment_text(sk, human)`) and her `segment_review.human_note` fields are our **first labeled
gold data**. Comparing her text + notes against the machine output (`sk, model` and `sk, polish`)
reveals **systematic, corpus-wide defects** — not one-offs.

The defects split into two kinds:

- **Type A — terminology/formula errors** (mechanical, high-leverage). Fixable at the glossary
  level; one edit corrects hundreds–thousands of segments via the existing
  version→stale→retranslate machinery. The reviewer often writes the correction *verbatim* in
  her notes — the notes are literally glossary-edit instructions.
- **Type B — fluency rework** (idiosyncratic prose). Not reducible to a term table; the target
  for future few-shot prompt tuning. **Out of scope here.**

This plan executes only the **cheap, unambiguous Type-A wins**, plus the **cost-control infra**
the user requires for any paid retranslation, plus a **polisher safety fix**.

### The reviewed gold segments (verbatim research data)

| seg_id | locator | element | What the reviewer changed | `human_note` |
|---|---|---|---|---|
| 26152 | I.q1.a1.arg1 | arg | `ratio`: machine `hľadisko` → wants **`rozum`**; also fluency | "ratio - rozum, supra rationem - nad rozum, presahujúce rozum / doctrina - náuka (fixný termín)" |
| 26153 | I.q1.a1.arg2 | arg | `ens/ente` → **`súcno`** (machine didn't detect it); `scientia divina` → **`veda božská`**; large fluency rework. **Polish CORRUPTED a locked term**: model `náuka` → polish `poznanie` | "ente - súcno, bytie / doctrina - náuka / scientia divina - veda božská" |
| 26154 | I.q1.a1.sed_contra | sed_contra | `sed_contra` formula `Avšak proti je to` → **`Avšak proti tomu stojí to`**; `ratio` → `rozum`; Bible → SSV | "Avšak proti je to - volím: Avšak proti tomu stojí to; ratio - volím: rozum" |
| 26155 | I.q1.a1.respondeo | respondeo | `respondeo` formula `Odpovedám: treba povedať, že` → **`Odpovedám, že`**; `ratio` → `rozum` (many); `intentio` → **`úmysel`**; `preto, že` → `preto, lebo` | "Respondeo - navrhujem prekladať len: Odpovedám, že; preto, že - preto, lebo; ratio - rozum; intentio - úmysly" |
| 26156 | I.q1.a1.reply1 | reply | `ratio` (per rationem) → `rozum` | (none) |
| 51427 | I.q1.preamble | preamble | `subiectum` → **`predmet`** (machine `subjekt`, undetected); `sensus` (plures sensus) → **`význam`** (kept `zmysel` for "supra sensum") | "Zvolila som slovo význam namiesto zmysel." |

(The two other `segment_review` rows — `I.q10` title/preamble by `matus.hagara`, no notes — are
test clicks; ignore them.)

### The Bible-citation conflict (DEFERRED — needs an expert)

The reviewer replaces machine renderings of Thomas's scripture quotes with the **canonical Slovak
Catholic Bible (SSV)** (e.g. "Nevypytuj sa na veci vyššie od seba", "Celé Písmo je Bohom vnuknuté
a užitočné na poúčanie…"). This **contradicts the documented house rule** (`.claude/database.md`:
*"translate Thomas's own Bible quotations from Thomas's Latin, not from the modern Bible"*). Do not
act on it — the user is consulting an expert.

---

## 2. Scope decisions (user-confirmed — do not relitigate)

| Item | Decision | Rationale |
|---|---|---|
| `intentio → úmysel` | **APPLY** (retranslate ~410) | Single sense; mid-prose word change needs regeneration |
| `respondeo` formula | **APPLY — $0 direct body patch** | 2,652/2,652 bodies start with the exact old string; deterministic |
| `sed_contra` formula | **DEFER** | Only 70% share the prefix; target phrasing varies with the Latin shape |
| `subiectum` / `ens` coverage | **DEFER** | Resolver lemmatization misses + `subiectum` surface-collides with participle + `ens` polysemous → belongs with the `ratio` sense-disambiguation workstream |
| `ratio` / `sensus` polysemy | **DEFER** | Sense-disambiguation problem, not a cheap flip |
| Bible citations | **DEFER** | Needs theological expert |
| Polisher | **Safety fix only** | Only 6 gold segments — prompt rethink / convergence tuning is premature |

---

## 3. Research findings (so you don't re-explore)

### 3.1 How term locking drives translation

- The resolver writes **`term_usage`** rows: one per (segment, detected term), pinning a
  `glossary_sense` and recording `sense_version_used` = the sense's version at resolve time.
- At translate time, `GlossaryRepository.locked_terms(segment_id)`
  (`src/storage/repositories.py:105-130`) returns a term as a **hard constraint only if** it has
  a `term_usage` row **AND** an `approved` sense **AND** a non-null `sk` `sense_rendering`. These
  become the `<hard_constraints>` block (`src/common/prompt_blocks.py:9-36`, injected in
  `src/translate/translator.py`) and are enforced post-generation by the terminology precheck
  (`src/translate/prechecks.py`). **No lock ⇒ the model translates that word freely** (this is
  why `subiectum` came out as "subjekt").
- **Which rendering wins when a sense has several:** `get_segment_constraints`
  (`src/server/db.py:272-317`) and `locked_terms` order by `source.authority_rank ASC`. Ranks:
  `human=1` (wins), `corpus_thomisticum=5`, `krystal=10`, `bahounek=20`, `dominican=30`,
  `freddoso=35`, `polish=85`, `model=90`. **So editing the `human` sk rendering is what changes
  the constraint the translator sees.**

### 3.2 The version → stale → retranslate mechanism (the cost-control engine)

- `glossary_sense.version` is bumped on any approved change (`GlossaryRepository.bump_sense_version`,
  `repositories.py:191`). `import_approvals.py` bumps on every approval.
- `SegmentRepository.get_stale_segments(work_id)` (`repositories.py:756-776`) returns segments
  where `term_usage.sense_version_used < glossary_sense.version` — exactly the segments that used
  the old rendering.
- The Prefect flow **`rerun_stale`** (`src/translate/run.py:387-422`) resets those to `pending`
  (guarding human-edited ones — see 3.5), then calls `translate_corpus`.
- After a successful (re)translation the loop advances `sense_version_used` to current
  (`update_sense_version_used`, `repositories.py:532`; called from `loop.py`).
- **Critical subtlety:** a *newly inserted* `term_usage` lock is written at the current version,
  so `sense_version_used == version` → it is **NOT** stale and won't be picked up. Coverage-gap
  fixes (subiectum/ens, deferred) therefore need an explicit `reset_translation_status` or a
  post-insert version bump. (Not needed for this plan's applied items.)

### 3.3 Structural formulas (`sed_contra`, `respondeo`)

- They are `glossary_term` rows with `category='formula'`, `is_multiword=True`. Backfilled
  `term_usage` rows (`resolution_method='formula_backfill'`) link them to every matching segment
  (see `docs/session_state.md` "Formula Terms — DB State").
- The Slovak formula text is **NOT deterministically injected** — it is a hard constraint the
  translator must reproduce; the precheck (`prechecks.py:70-83`) requires the phrase verbatim in
  the draft. So the exact phrasing physically lives in **two** places: (a) the formula's
  `sense_rendering(lang='sk')`, and (b) verbatim at the **start of every already-translated
  `segment_text(sk, model/polish)` body**.
- `get_structural_formulas` (`src/server/db.py:457-493`) reads the sk rendering **for the web
  display label only**. **There is no `style_profile.yaml`** in the repo (the `.claude/decisions.md`
  reference is aspirational; confirmed absent).
- **Consequence:** changing a formula in the *existing corpus* requires either a version-bump →
  `rerun_stale` (LLM cost) **or** a direct `segment_text` prefix patch (free) where the prefix is
  deterministic. `respondeo` qualifies for the free patch; `sed_contra` does not (see 3.6).

### 3.4 Resolver detection & why the coverage gaps happen (DEFERRED items)

- Single-word terms: CLTK lemmatize each token → look up in `lemma_to_term` (built from
  **approved senses only**, `repositories.py:79`). `resolve_segment` in `src/ingest/resolver.py:341-368`.
- Multiword/formula terms: regex on `la_surface` via `_match_pattern` (`resolver.py:89-133`);
  formulas anchored at `^`.
- Empirically confirmed CLTK behavior: `ente → ['ente']` (ablative not normalized to `ens`),
  `subiectum → ['subicio']` (mapped to the **verb**), `ratio → ['ratio']` (works — why ratio locks
  fine). So `ens`/`subiectum` are **lemmatization misses**, and `subiectum` the noun is
  surface-identical to the participle of `subicio` ("subjected") — a surface regex can't
  distinguish them.
- `TermUsageRepository.write_term_usage` (`repositories.py:929-958`) is **segment-replace**
  (DELETE all `guessed` rows for the segment, re-INSERT the full set) — you can't append one term
  without the segment's complete resolution list. `resolver.run` (`resolver.py:421`) is
  **full-corpus only**; there is no single-term re-resolution path today.

### 3.5 The human-edit guard (protects reviewed work automatically)

`_guard_and_reset` (`src/translate/run.py:369-384`) + `get_human_edited_segments`
(`repositories.py:778-799`): any segment with a `(sk, human)` row is **flagged `needs_human`
instead of reset/retranslated**, so re-runs never overwrite reviewed text. This is why the
2 reviewed `intentio` segments are safe when we bump `intentio`.

### 3.6 Polisher — where it blocks and where it doesn't

- **`polish_segment`** (`src/polish/polisher.py:132-197`) computes `run_guards` but **writes
  `(sk,polish)` UNCONDITIONALLY** (`:182-186`) — this is the hole. Docstring admits it
  ("writes … regardless of guard flags"). This is the pilot/inline path and is what wrote the
  corrupted arg2 polish (`náuka`→`poznanie`).
- **Production paths already block**: `run_polish` (`polisher.py:279`) and batch
  `_process_results` (`src/polish/batch.py:206-221`) skip the write when `not flags["ok"]`.
- Guards (`src/polish/guards.py`, `run_guards:68-90`): `sentence_count_delta` (must be 0),
  `locked_term_retention` (delegates to `check_terminology_lemma`), `particle_retention`,
  `length_ratio ∈ [0.5, 2.0]`. `ok` requires all.
- Convergence-to-human is **not** wired; `get_sk_text(id, "human")` exists as the hook. (Deferred.)

### 3.7 Pipeline / step / menu structure (where infra attaches)

- Interactive menu: `src/pipeline/interactive.py` — `build_menu:163-255` (item #10 =
  `rerun-stale` → `RerunStaleStep`; #13 = `reset-corpus` → `ResetCorpusStep`), `run_loop:273-319`
  runs the chosen step **immediately, with no confirmation, no cost preview, and no auth**.
- Step pattern: `src/pipeline/step.py` — `BaseStep` (`:43-56`) with `run(ctx)` and an optional
  `verify(ctx) -> bool` precondition; the runner (`src/pipeline/runner.py:75-97`) turns a False
  `verify` into a blocked/failed step **before** any work runs — the natural owner-gate hook.
- Translate steps: `src/translate/steps.py` — thin wrappers `TranslateCorpusStep`,
  `RerunStaleStep`, `ResetCorpusStep`.
- Cost basis available: `RunRepository.last_run()` (`repositories.py:973-986`,
  `total_cost_usd`/`total_segments`); `coverage_report.py:34-39` token constants as fallback.
- There is **no "owner" concept anywhere** (no env var, DB column, or flag). The `editor` table +
  `is_editor()` gate exists **only in the Flask server**, which has **zero retranslation routes**.

### 3.8 Glossary write helpers (all in `src/storage/repositories.py`; none self-commit — the
`get_conn()` context manager in `src/storage/db.py:35-46` commits on clean exit)

| Method | Line | Behavior |
|---|---|---|
| `find_term_by_lemma(lemma) -> term_id\|None` | 246 | case-insensitive |
| `find_sense_by_label(term_id, label) -> dict\|None` | 286 | `label=None` matches the **primary** sense (context_label IS NULL) |
| `get_current_sense(sense_id) -> {sense_id,version,status}` | 146 | |
| `get_sk_rendering_content(sense_id) -> str\|None` | 326 | current sk rendering |
| `write_human_rendering(sense_id, sk_text, src_id)` | 205 | upsert `sense_rendering(lang='sk', source_id=src_id)`; callers pass `source_id(conn,"human")`; **does NOT bump version** |
| `bump_sense_version(sense_id) -> new_version` | 191 | `version = version+1` (NOT idempotent) |
| `update_sense_status(sense_id, status)` | 179 | |
| `write_context_label` / `write_human_surface` | 222 / 233 | no version bump |
| `insert_glossary_term` / `insert_glossary_sense` | 256 / 271 | for new terms (not needed here) |

`source_id(conn, code)` lives in `src/storage/db.py`.

### 3.9 Verified DB facts (as of 2026-07-10)

**Glossary senses & renderings**

| term | sense_id | sk rendering | status | version | notes |
|---|---|---|---|---|---|
| `intentio` | **12361** | `zámer` (under both `human` src1 and `model` src7) | approved | 2 | single sense → change to `úmysel` |
| `respondeo` | **16247** | `Odpovedám: treba povedať, že` (human + model) | approved | 2 | `is_multiword=True`, la=`Respondeo dicendum quod` → change sk to `Odpovedám, že` |
| `sed_contra` | 10214 | `Avšak proti` (human) | approved | 1 | DEFER |
| `ratio` | 111=`rozum`(proposed,0 uses), 14468=`hľadisko`(approved, **2718**), 14653=`dôvod`(1150), 14654=`podstata`(596) | | | | DEFER — default-sense inversion |
| `ens` | 41=`súcno`(approved,225), 14542=`bytie`(5), 14709=`nebytie`(7), +2 | | | | DEFER — polysemous |
| `subiectum` | 14004=`predmet`(approved,98) | | approved | 2 | DEFER — lemmatization/surface |
| `sensus` | 120=`zmysel`(469) + 5 more; **no `význam`** | | | | DEFER — context-split |

**Blast radius (segments with the sense locked, by translation_status)**

| sense | translated (of which reviewed) | needs_human | total |
|---|---|---|---|
| `intentio` 12361 | 410 (2 reviewed) | 33 | 443 |
| `respondeo` 16247 | 2,357 (1 reviewed) | 295 | 2,652 |
| `sed_contra` 10214 | 2,545 (1 reviewed) | 75 | 2,620 |

**Prefix determinism (of `sk, model` bodies for that sense)**

- `respondeo`: **2,652 / 2,652** start with the exact `"Odpovedám: treba povedať, že"` → 100% ⇒
  safe direct patch.
- `sed_contra`: 1,834 / 2,620 start with `"Avšak proti je to"` (70%); 86 with `"Avšak proti tomu"`;
  all 2,620 start with `"Avšak proti"`; the remainder vary ⇒ **no clean patch** (deferred).

**Corpus totals:** 25,234 `translated`, 1,143 `needs_human`, 26,377 total segments (`work_id=1`).

---

## 4. Phased implementation

Do the phases in this order. **Each phase: build behind tests, show the diff, get approval, and
for any prod mutation run a dry/read-only check first.** Commit per phase (Conventional Commits).

### Phase 1 — Polisher safety fix (do first; it protects Phase 5's retranslation output)

**Goal:** stop `polish_segment` from persisting a polish that corrupts a locked term.

**Changes**
- `src/polish/polisher.py` (`polish_segment`, ~line 182–197): make guards **blocking by default**.
  Add a parameter `_enforce_guards: bool = True`. When enforcing and `not flags["ok"]` (at minimum
  `not flags["term_retention_ok"]`): return status `"guard_failed"` with the `PolishOutcome`
  (flags populated) and **do not write** `(sk,polish)` and do not commit. Keep the current
  write-anyway behavior available only via `_enforce_guards=False` for the pilot measurement
  harness if it genuinely needs it.
- Check callers: `src/optimize/pilot.py` `_translate_worker` calls `polish_segment(...)`. Decide
  whether the pilot should now see `"guard_failed"` (preferred — it already records
  `guard_flags`) or pass `_enforce_guards=False` to preserve its old measurement semantics. Update
  pilot report expectations if the status set changes.

**Verify**
- New regression test in `tests/polish/`: feed a `polished` string where a locked term's required
  lemma was replaced (the `doctrina`: `náuka` → `poznanie` case). Assert
  `run_guards(...)["term_retention_ok"] is False` **and** `polish_segment` returns `"guard_failed"`
  with no `(sk,polish)` write. **If `check_terminology_lemma` does NOT flag this substitution, that
  detection gap is the real bug — fix the guard, not just the write path.**
- `uv run pytest tests/polish` green.

### Phase 2 — Cost-gated, owner-only retranslate trigger (infra the user requires)

**Goal:** no retranslation can run without an owner token and an explicit cost-previewed
confirmation. This is the reusable gate for every future glossary edit.

**Changes**
- `src/translate/run.py`: add `preview_stale_cost(work_id: int = 1) -> tuple[int, float]`.
  Compute `stale = get_stale_segments(work_id)`, subtract `get_human_edited_segments(stale)` (the
  guarded set that won't be restaged) → `n_restage`; multiply by $/segment from
  `RunRepository.last_run()` (`total_cost_usd / total_segments`), falling back to the
  `coverage_report.py:34-39` token constant when there is no prior run. Read-only.
- `src/translate/steps.py`: on `RerunStaleStep` (and `ResetCorpusStep`):
  - `verify(ctx) -> bool`: **owner gate** — read env `AQUINAS_OWNER_TOKEN` (name TBD with user);
    if unset/blank, return False (runner blocks the step with a clear message; zero spend).
  - In `run()`: call `preview_stale_cost`, print `"Restage N segments, est ~$X.XX — proceed? [y/N]"`,
    read confirmation (keep the reader injectable, default `input`, for tests). On anything but
    `y`/`yes`, return `StepResult(ok=True, summary="cancelled — no retranslation")`. On confirm,
    invoke the existing flow.
- Do **not** change menu ordinals in `interactive.py`; the labels (#10, #13) stay.

**Verify (all without spending)**
- Unit-test `preview_stale_cost` arithmetic against a seeded `translation_run` row.
- `verify()` returns False with no token; True with token.
- With token + `N` at the prompt → `run()` returns "cancelled", flow never called (assert via a
  patched flow).
- `uv run pytest tests/translate` green.

### Phase 3 — Durable glossary corrections (`intentio` + `respondeo` rendering)

**Goal:** apply the two rendering changes reproducibly and idempotently.

**Changes** — new checked-in module `src/review/reviewer_corrections.py` (+ `python -m
review.reviewer_corrections` CLI). Inside `with get_conn() as conn:`; `g = GlossaryRepository(conn)`:

- **`intentio`** (`term=find_term_by_lemma("intentio")`, sense via `find_sense_by_label(term, None)`
  → expect **12361**): read current sk rendering; **only if != "úmysel"**:
  `write_human_rendering(12361, "úmysel", source_id(conn,"human"))` then `bump_sense_version(12361)`.
  (Guard the bump — it is not idempotent; a double bump causes spurious staleness.)
- **`respondeo`** (sense **16247**): `write_human_rendering(16247, "Odpovedám, že",
  source_id(conn,"human"))` **and do NOT bump the version** (we patch existing bodies in Phase 4;
  a bump would needlessly restage ~2,357 segments). The rendering update keeps the web label and
  all *future* translations consistent.
- Record provenance (reviewer note references, seg ids) in the module docstring. Add a
  `docs/session_state.md` note mirroring the "Formula Terms — DB State" section so the
  glossary-rebuild reproduces these.

**Verify**
- Run twice; second run is a no-op (assert version bumped by exactly 1 for intentio, unchanged for
  respondeo). Confirm renderings in DB.
- **Show the intended DB writes to the user before running against prod.**

### Phase 4 — One-time `respondeo` body patch ($0, no LLM)

**Goal:** rewrite the deterministic formula prefix in existing translated bodies.

**Changes** — checked-in idempotent script `src/review/patch_respondeo_prefix.py`. For every
`segment_text` row with `lang='sk'` under sources `model` **and** `polish` whose `segment_id` is
locked to `respondeo` sense 16247, if `content` starts with `"Odpovedám: treba povedať, že"`,
replace that leading substring with `"Odpovedám, že"`. **Never touch `human` rows.** Idempotent
(prefix check). Report rows changed.

**Verify**
- Read-only pre-count: how many `model`+`polish` bodies start with the old prefix (expect 2,652
  model + however many polish exist). Show the user.
- After run: all `respondeo` `(sk,model/polish)` bodies start with `"Odpovedám, že"`; the 1 reviewed
  respondeo's `(sk,human)` untouched; re-run is a no-op.

### Phase 5 — `intentio` retranslation via the Phase 2 gate

**Goal:** regenerate the ~410 `intentio` bodies with the new term, cheaply and observably.

**Steps**
- After Phase 3's `intentio` bump, `get_stale_segments(1)` should include the ~443 intentio
  segments (410 translated + 33 needs_human); the 2 human-reviewed are guarded → flagged
  `needs_human`, not overwritten.
- Run the gated `rerun-stale` (menu #10 or `AQUINAS_OWNER_TOKEN=… uv run python -m translate.run
  --flow rerun_stale`), confirming the cost preview (est. < ~$2). `MAX_WORKERS` controls
  parallelism; polish runs pipelined (Phase 1 makes it guard-safe).

**Verify**
- ~408 segments regenerate containing `úmysel` (grep the new `(sk,model)`); the 2 reviewed are
  `needs_human` with human text intact; realized cost ≈ preview.
- Spot-check on the preview server (`I.q1.a1.respondeo` etc.).

---

## 5. Global acceptance / safety checklist

- [ ] No `(sk, human)` row is ever modified or deleted by any phase.
- [ ] Every prod-mutating script is idempotent and was shown to the user (diff/dry-run) first.
- [ ] `bump_sense_version` is called at most once per correction (guarded).
- [ ] Retranslation only runs behind the owner token + confirmed cost preview.
- [ ] `uv run pytest` green (esp. `tests/polish`, `tests/translate`, `tests/review`).
- [ ] `docs/session_state.md` updated: current milestone, decisions, files changed, exact next step.

## 6. Deferred backlog (context preserved for later)

1. **`sed_contra` formula** — decide a target rendering that fits all shapes (`Sed contra est quod…`
   vs bare `Sed contra, X dicit`), then rendering update + gated retranslate (~2,545 segs).
2. **`subiectum` / `ens` coverage** — needs a resolver lemmatization override (custom lemma map or
   surface patterns) + full re-resolve (free compute) + explicit `reset_translation_status` on
   changed segments; `ens` additionally needs sense disambiguation. Fold into the `ratio` workstream.
3. **`ratio` / `sensus` polysemy** — the big one: `ratio→hľadisko` is locked on **2,718** segments
   but the reviewer wants `rozum` (sense 111, currently `proposed`, 0 uses) as the *default*; needs a
   context-scoped sense-disambiguation flow (cheap LLM classifier seeded by the gold labels), not a
   blind flip. Highest corpus-wide leverage.
4. **Bible citations** — expert decision on SSV vs. translate-from-Latin house rule.
5. **Polisher prompt rethink** — rewrite `prompts/polish_system.txt` to style-only + wire an
   edit-distance-to-`(sk,human)` convergence metric into `src/optimize/polish_optimize_loop.sh` /
   `run_compare.py`; wait until more gold accumulates.
6. **Web glossary-editing UI** — **IMPLEMENTED** (2026-07-17) by
   `.claude/m5_editor_glossary_proposals_plan.md`, Stages 1–7: editors propose glossary changes
   in the Flask app (`glossary_proposal` table, five kinds — rendering/sense_here/remove_here/
   retire_sense/add_term), an admin queue reviews with blast-radius/cost preview and applies on
   approve ($0, no spend), and only the owner triggers the paid retranslate via the CLI gate
   (`RerunStaleStep`/`ApplyNewTermsStep`, this Phase 2's gate — Stage 5 detected it already
   existed and added nothing new). The engine (`bump_sense_version` → `get_stale_segments` →
   `rerun_stale`) is unchanged, just fed by the new proposal/approval surface instead of the
   retired Sheets review cycle.
