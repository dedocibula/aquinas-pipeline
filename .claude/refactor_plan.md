# Aquinas Pipeline — Refactor Plan & Progress Log

> Single source of truth for the typed/modular/step-based refactor. Read this first
> when resuming. Plan approved; Phase 0 complete. Work happens on branch
> `aquinas-refactor` in the worktree `.claude/worktrees/aquinas-refactor/`.

---

## 0. How to resume (environment)

```bash
# Worktree (branch aquinas-refactor, forked from main @ 93d7964)
cd /Users/agalad/Workspace/python/aquinas-pipeline/.claude/worktrees/aquinas-refactor

# One-time env setup (these are gitignored, not present in a fresh worktree):
cp ../../../.env .env                 # DATABASE_URL etc.
ln -s ../../../models models          # CLTK + MorphoDiTa models (large)
# .venv is created automatically by `uv run` from uv.lock

uv run pytest -q          # regression gate — MUST stay green (baseline: 745 passed)
uv run ruff check         # lint — pre-commit hook enforces this on staged .py
```

- **pytest config**: `pythonpath = ["src", "."]` (so `import translate.loop`, `import common.db` work).
- **DB access**: `psql` is NOT on PATH. Use `uv run python3` + `psycopg2`.
  `DATABASE_URL=postgresql://aquinas:aquinas@localhost:5432/aquinas`.
  `locator_path` is `ltree` — cast `locator_path::text` before string ops.
- Tests are isolated (FakeConn/monkeypatch) — they do **not** touch the live DB.

## 1. Guiding constraints (from CLAUDE.md — non-negotiable)

- **No new runtime deps.** `dataclasses`, `typing`, `pytest`, `psycopg2`, `requests` already present.
- **DDL-free** except: the consolidated schema snapshot (Phase 8, read-only `pg_dump`) and the
  one-off habere data purge (Phase 9) → **dry-run + STOP for human approval before running.**
- **Behavior-preserving.** Every commit keeps `uv run pytest` green (≥745) and `ruff` clean.
- **Commit in small chunks**, conventional-commit messages, one logical change per commit.
- **Fail loudly** — no silent `try/except` in parsers.
- **Cross-cutting**: any file you touch gets its milestone refs (M0–M5) stripped from comments
  (Phase 7 does a dedicated sweep of the rest).
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## 2. Why this refactor

The pipeline (Latin→Slovak translation of the *Summa Theologiae*) works but grew as standalone
scripts wired by convention. Structural problems:

- DB access scattered across `common/db.py`, `common/glossary_repo.py`, `common/corpus_db.py`,
  and inline helpers in `translate/loop.py`; same SQL shapes re-implemented per call site.
- Untyped dicts for shared concepts (segment, term/sense, constraint) with different keys per module.
- Duplicated `requests.post` API blocks (4 of them).
- Repeated parser loop in 3 parsers.
- No whole-pipeline step abstraction (only M2 has a bare-function runner); source verification
  sits outside any pipeline; no interactive "where am I" driver.
- Reporting is ad-hoc into a flat `reports/` dir.
- Prompt-optimization ("DSPy-like") code scattered across repo root + `scripts/` + `src/translate/`.
- Milestone labels leak into code comments; 7 incremental migrations, no single schema source of truth.

### Verification finding (important)
The original request named two "domain constraints to preserve": *modern-Slovak-suffix filtering*
and *first-person-verb-form removal from the terminology DB*. **Neither exists** (confirmed by two
Explore agents + grep). Disposition per the user:
- `_oov_stem` (`src/translate/prechecks.py:34`) is the real suffix/stem logic → **keep**; propose a
  cleaner alternative as an optional, non-blocking follow-up behind locked tests (Phase 9a).
- `_drop_habere_ppp_constraints` (`src/translate/loop.py:159`) is a self-described temporary
  read-time patch → **run once as a purge, harden the resolver, then delete** (Phase 9b).
- Do **not** invent a "suffix filter" or "first-person remover" component. Building the latter would
  corrupt the glossary (`intellego → intelekt` is a legitimate first-person headword).

---

## 3. Status summary

| Phase | Status | Notes |
|---|---|---|
| 0 — Test net (unify/prune/baseline) | ✅ DONE | baseline green at 745; dead tests pruned; shared conftest added |
| 1 — Typed models | ✅ DONE | `c126da6`; models.py + 7 tests; 752 passed; v_segment flag resolved |
| 2 — Repository layer | ✅ DONE | new `src/storage/` (db+models+repositories) holds all SQL; old fns are wrappers; +30 tests; 781 passed; import cycle removed (storage is a leaf) |
| 2b — Flip callers to models | ✅ DONE | resolver/resolution flipped (`4210376`); loop.translate_segment now consumes Segment/Constraint models + seg_repo writes; import_approvals bump/update/write_human_rendering via GlossaryRepository; corpus_db.py + glossary_repo.py deleted; tests repaired & test_import_approvals migrated to shared conftest fakes. Follow-up `b596cda`: folded `get_current_sense`/`get_la_surface`/`write_human_surface`/context_label UPDATE into GlossaryRepository (`get_current_sense`/`get_la_surface`/`write_context_label`/`write_human_surface`); helper tests moved to test_glossary.py. **No transition shims remain in import_approvals. 750 passed; ruff clean.** |
| 3 — DeepSeek client | ✅ DONE | `e2c7c8f`; `common/deepseek_client.py` (DeepSeekClient.chat + DeepSeekAPIError); 4 requests.post blocks collapsed; +9 client tests; 748 passed; ruff clean |
| 4 — Parser base class | ✅ DONE | Recommended scope built (user approved + "pull common DB access out of all three; remove dead code"). New `src/ingest/source_parser.py`: `OverlayElement(locator, text)` + `TextOverlayParser` ABC (class attr `lang`; abstract `parse`; concrete `store()` holding the shared lookup→upsert loop, missing-segment policy injected via an `on_missing` callback). bahounek (`BahounekParser`, cs) + english (`EnglishParser`, en) subclass it; `insert_bahounek_texts`/`insert_english_texts` are thin wrappers preserving their exact signatures + fail-loud/gap-log policy. **All parser SQL moved into `SegmentRepository`** (Phase-2 invariant): new `get_segment_id_by_locator(loc, work_id=None)`, `get_article_title_locators` (dedups the duplicated `_articles_from_db`), `wipe_article`, `create_segment`, `set_reply_to`, `body_text_coverage(lang)`. `parser_latin` left as the structural parser but its inline SQL now goes through the repo. Dead code removed: `_choose_edge_cases` (parser_latin, never called) + its comment ref; `_in_article` (ingest_english, defined-never-called). `BahouněkElement`/`EnglishElement` unified into `OverlayElement` (`.czech_text`/`.english_text` → `.text`); ~9 test sites updated; `TestInsertBahouněkTexts` migrated off MagicMock to the shared `fake_conn` (repo uses `with conn.cursor()`). +10 tests (`test_source_parser.py` + storage repo tests). **760 passed; ruff clean.** |
| 5 — Pipeline steps + runner + reporting + interactive | ✅ DONE | 5.0 + 5a + 5b + 5c + 5d all DONE (see below) |
| 6 — Isolate optimize/ toolchain | ✅ DONE | new `src/optimize/` package; toolchain relocated; 823 passed; ruff clean (see below). **Re-verified this session: 795 passed, ruff clean, deliverables present, old paths gone.** |
| 6.1 — report* review + gap-term dedup (user-requested side tasks) | ✅ DONE | see below |
| 7 — Strip milestone labels; rename milestone files | ☐ NEXT | |
| 8 — Consolidate DB schema | ☐ | |
| 9 — Domain housekeeping (oov_stem, habere) | ☐ | behavioral; habere purge gated by approval |
| 10/11 — Final gate + memory | ☐ | |

#### Phase 5.0 — Pilot consolidation (sample-only) — DONE
Decision (user-approved): the pilot and the production runner (`translate/run.py`) do **not** differ
in translation — both call the same `translate_segment`. The pilot's value is its *measurement
harness* (PromptLogger JSONL deep-dive, abort thresholds needs_human>20% / iters>2.5, cost
calibration + full-corpus extrapolation). The redundancy was *inside* the pilot: 3 of its 4 modes
(`debug` I.q1×10, `full` Q1–Q6, `titles`) were just hardcoded locator subsets that `run.py`
filtering already covers. So pilot collapses to **sample-file-only** — translate exactly the segments
named in `$PILOT_SAMPLE_FILE` (default `docs/pilot_sample_100.json`), the same file the prompt-opt
loop feeds.
- `src/translate/pilot.py`: removed `fetch_debug_segments`, `fetch_pilot_segments`,
  `fetch_all_pilot_segments`, `fetch_title_segments`, `_PILOT_QUESTIONS`, `_DEBUG_*`, and the
  `PILOT_FULL`/`PILOT_TITLES`/`PILOT_SAMPLE` env switches. `fetch_sample_segments` is the single
  selector; the sample **is** the pilot (no flag). Run is always recorded as flow `pilot_sample`.
  Measurement harness kept verbatim. `_write_report` signature simplified (dropped
  `report_name`/`titles_mode`/`sample_mode`); single report **`reports/m4_sample.txt`** (the name
  `optimize_loop.sh` already reads for its pass-rate row). Q1–Q6 calibration basis → the (more
  representative) sample.
- `optimize_loop.sh`: dropped the now-unneeded `PILOT_SAMPLE=1` (PILOT_SAMPLE_FILE still set).
- `docs/claude-corrections.md`: example command de-references the removed `PILOT_FULL` flag.
- `tests/translate/test_pilot.py`: rewrote the mode-routing/fetch tests around the single
  sample selector; report tests assert `m4_sample.txt` + the new "Sample file:" line.
- **758 passed** (760 − 2 intentionally-removed dead-mode tests; ≥745 baseline holds); ruff clean.
- Sets up Phase 6 cleanly: pilot is now a self-contained sample-driven harness that relocates to
  `src/optimize/` unchanged, fed by the unified sample-generation script (deferred to Phase 6).
- **Note for the rest of Phase 5**: wrap this consolidated pilot as the translate-stage measurement
  step; `_REPORT_NAME` should later route into `reports/translate/` per the reporting design.

#### Phase 5a — Pipeline core (PipelineStep + PipelineContext + Runner) — DONE
Additive only; no existing entry point rewired yet (5b flips the first real consumers).
- **New `src/pipeline/` package** (the step abstraction; a leaf over `storage.db`):
  - `step.py`: `StepResult(name, ok, summary, details)` dataclass; `PipelineStep` —
    a `@runtime_checkable` Protocol (`name`, `run(ctx) -> StepResult`), so existing
    callables adapt without inheritance; `BaseStep` convenience base (set `name`,
    implement `run`; default `verify(ctx) -> True` for the no-precondition case).
  - `context.py`: `PipelineContext(reports_dir, work_id=None, knobs={}, connect=get_conn)`.
    `connection()` pass-through (steps still open one conn per unit of work — preserves
    existing tx boundaries — but depend on the ctx, not `storage.db`, so they're fakeable);
    `stage_reports_dir(stage)` → creates `reports/<stage>/` (sets up 5c); typed knob
    accessors `knob`/`knob_int`/`knob_float` that **fail loud** on a malformed value
    rather than silently defaulting.
  - `runner.py`: `Runner(ctx, *, out, err)` lifts the timing/banner/fail-loud/
    stop-on-first-failure loop that `ingest/pipeline.py` grew by hand. An uncaught
    exception is reported to `err` and turned into a failed `StepResult` (fail loud, but
    the run summary survives). Returns `list[StepResult]`; CLIs map that to an exit code.
    `out`/`err` injectable for tests.
- `tests/pipeline/test_runner.py` (+11): ordering, stop-on-failure (default + override),
  exception→failed-result, Protocol conformance (bare class + BaseStep), knob casts +
  fail-loud, `stage_reports_dir` creation, injected connection factory.
- **769 passed** (758 + 11); ruff clean.
- **Next (5b)**: wrap `acquire/verify.py` checks as `VerifySourcesStep` (prerequisite step 0
  using the `verify()` hook); make `ingest/pipeline.py`'s `_STEP_FNS` the first real
  consumers of `Runner` (steps return `StepResult`); root `verify_sources.py`/`python -m`
  entry points become thin shims instantiating Steps.

#### Phase 5b — First real consumers of the runner — DONE
The ingest pipeline and source verification now run through `Runner`; the hand-rolled
timing/fail-loud loop in `ingest/pipeline.py` is gone.
- **Runner `verify()` precondition activated** (`runner.py`): before a step's `run`, the
  runner calls `step.verify(ctx)` when present (BaseStep defaults it True; bare Protocol steps
  without the method are unaffected via `getattr`). A False precondition → failed `StepResult`
  (`"precondition not met"`) and, under default stop-on-failure, halts the run; an exception in
  `verify` → `"precondition error: …"` (fail loud). This makes the dormant 5a hook real.
- **`VerifySourcesStep`** → new `src/acquire/steps.py`: a `BaseStep` (`name="verify-sources"`)
  whose `run` drives `acquire.verify.CHECKS` (injectable for tests), aggregating per-check
  pass/fail into `StepResult.details["checks"]` and `ok = all-passed`. An exception in any check
  is reported and counts as a failure (never crashes the runner). `acquire/verify.py` keeps the
  pure `check_*` functions + `CHECKS`; its old `main()` loop is deleted (superseded by the step).
  `steps.main()` runs the step via `Runner` and returns an exit code.
- **`ingest/pipeline.py` flipped to steps**: `_step_latin/_bahounek/_english/_resolve/_report`
  became `LatinStep/BahounekStep/EnglishStep/ResolveStep/ReportStep` (`BaseStep`, each returns a
  `StepResult` with a summary + details; print progress kept verbatim). `_build_steps()` returns
  the token→step registry; `_STEPS = ("verify","latin","bahounek","english","resolve","report")`
  is the `--all` order (**verify is prerequisite step 0** — a failed verify stops the run via
  stop-on-failure, so no ingest step touches the DB). `--step` accepts any single token (incl.
  `verify`). `main()` builds a `PipelineContext` (reports_dir + GAP_* env knobs) and drives the
  selected steps through `Runner`, exit 0 iff all `ok`. Resolve reads its knobs via
  `ctx.knob_int/float` (no direct `os.environ`). Steps write reports under `ctx.reports_dir`
  (flat `reports/` for now; 5c routes to per-stage folders). `_step_pilot` unchanged (separate
  mode, not a runner step).
- **Entry points**: `python -m acquire.steps` is the source-verify entry point (steps.py has a
  `__main__` → `main`); each `python -m …` entry point preserved. The redundant root
  `verify_sources.py` shim was **deleted** (follow-up) — `python -m acquire.steps` is the
  documented replacement; m0_setup.md / sources.md updated.
- Tests: `test_pipeline.py` rewritten to the step/registry contract (patch `_build_steps` with
  fakes; assert order, stop-on-failure, failed-verify-blocks-ingest, not-ok→exit 1); +3 Runner
  verify-hook tests; +6 `VerifySourcesStep`/`steps.main` tests. Smoke-tested `--help`, bad-step
  rejection, and `acquire.steps` import. **782 passed; ruff clean.**
- **Next (5c)**: `src/pipeline/reporting.py` `StepReport` writer; route each step's `StepResult`
  details into per-stage `reports/<stage>/` (use `ctx.stage_reports_dir`); PromptLogger JSONL →
  `reports/translate/debug/`.

#### Phase 5c — Per-stage reporting — DONE
Every step the `Runner` executes now leaves a concise, uniform run summary in its stage folder.
- **New `src/pipeline/reporting.py`**: `StepReport(stage, result, started_at, elapsed_s)` renders a
  `StepResult` to Markdown — header (`# <stage> · <step>`), status/when/elapsed/summary, a `## details`
  section (one level of nesting expanded: dicts → indented sub-items, lists → count + 5-item preview,
  bools → pass/FAIL), and a `## action required` section that names failing `checks` or, for any other
  failure, points at the summary. `write(stage_dir)` → `<stage_dir>/<step>.md`. Rendering is generic
  (no per-stage template) so a new step gets a usable report for free. Exported from `pipeline`.
- **Runner writes the report**: `_run_one` split into the banner/timing/report-writing shell and a new
  `_execute` (precondition + body → `StepResult`). Timing now spans the whole step (verify + run). When
  a step declares a `stage` class attr, the runner writes the `StepReport` into
  `ctx.stage_reports_dir(stage)`; bare Protocol/test steps without `stage` are unaffected (no file).
- **Steps declare their stage**: `VerifySourcesStep` → `acquire`; `LatinStep/BahounekStep/EnglishStep`
  + `ReportStep` → `ingest`; `ResolveStep` → `resolve`. (The step-owned domain reports — `m2_coverage.txt`,
  `m2_parser_anomalies.txt`, `m2_latin_stats.json`, gap logs — stay flat in `reports/` for now: they're
  cross-consumer coupled (`report_m2.py` reads the stats+anomaly files `LatinStep` writes), so relocating
  them is a separate, riskier change deferred out of this additive pass.)
- **PromptLogger JSONL relocated**: pilot's per-run debug log → `reports/translate/debug/debug_<ts>.jsonl`
  (PromptLogger already creates parents). Docstring updated.
- **Test isolation fix**: `steps.main()` writes to the real `reports/acquire/` now that the runner emits a
  report, so the two `TestStepsMain` cases monkeypatch `steps.ROOT` to `tmp_path`.
- **.gitignore**: the per-stage runner summaries (`reports/{acquire,ingest,resolve,review,translate}/`) are
  ephemeral run output → ignored.
- Tests: new `tests/pipeline/test_reporting.py` (+8 render/write), `tests/pipeline/test_runner.py`
  `TestRunnerReporting` (+3: report written into stage folder, no-stage→no-file, failed step still
  reported). **792 passed; ruff clean.**
- **Next (5d)**: interactive driver → `src/pipeline/interactive.py` (`python -m pipeline`) — flow position
  from DB status + last command (persist `.pipeline_state.json`) + numbered menu invoking Steps.

#### Phase 5d — Interactive driver — DONE
`python -m pipeline` shows where the corpus stands and a numbered menu whose every item invokes a
`PipelineStep` through the `Runner` — no operation logic in the driver; each entry delegates to the Step
that owns the work, so the driver and the per-stage CLIs share one implementation and each action gets the
runner's timing + per-stage `StepReport` for free.
- **New status SQL in the repositories** (Phase-2 invariant — all SQL in repos):
  `SegmentRepository.translation_status_counts(work_id)` → `{status: count}`;
  `GlossaryRepository.sense_status_counts()` → `{status: count}`;
  `RunRepository.last_run()` → most-recent `translation_run` dict (or None). +6 repo tests.
- **New step wrappers** (thin `BaseStep`s, each delegates to the module that owns the op; declare their
  stage so the runner reports them): `review/steps.py` → `ExportReviewStep`/`ImportApprovalsStep` (stage
  `review`); `translate/steps.py` → `TranslateCorpusStep`/`RerunStaleStep`/`RetranslateBodyStep` (stage
  `translate`, work_id from ctx, default 1); `ingest/pipeline.py` → `MineSensesStep` (stage `resolve`,
  mines+labels+writes proposed; **not** in the `--all` ingest flow — it spends API budget). +tests for each.
- **`src/pipeline/interactive.py`** (`python -m pipeline` via new `pipeline/__main__.py`):
  `StatusSnapshot` (segment counts / sense counts / last run) + `gather_status(ctx)` reading the three
  repos through `ctx.connection()`; `render_status` shows the flow position + the persisted last command;
  `.pipeline_state.json` (load/save, no DDL — gitignored) remembers the last command across invocations
  (resolved at call time so it's monkeypatchable); `build_menu()` is the ordered menu (verify → latin →
  bahounek → english → resolve → mine-senses → export → import → translate → rerun-stale → retranslate →
  report), each item a Step factory (local imports so a broken optional dep in one stage doesn't kill the
  whole menu); `run_loop(ctx, *, read, out, gather, make_runner)` is the fully-injectable menu loop
  (number → run Step via `Runner`, save state; `r` refresh, `q`/`0`/EOF quit; bad input + failed step
  surfaced, never fatal; a DB-unreachable status read is surfaced, not fatal). `--status` prints the
  position once and exits.
- Tests: `tests/pipeline/test_interactive.py` (+15: state roundtrip/corrupt, rendering incl. empty corpus
  + unfinished run, menu numbering, loop quit/EOF/select/invalid/refresh/failed-step/status-unavailable,
  gather wiring). Two new step test files renamed to unique basenames (`test_review_steps.py`,
  `test_translate_steps.py`) to dodge the pytest no-`__init__` basename collision. **823 passed; ruff
  clean.** Smoke-tested `python -m pipeline --help`, `--status` (live DB), and all menu imports.
- **Phase 5 complete.** Next: Phase 6 — isolate the prompt-optimization toolchain into `src/optimize/`.

#### Phase 6 — Isolate the prompt-optimization toolchain → `src/optimize/` — DONE
The whole prompt-opt toolchain now lives in one package, structurally separated from the production
`translate` package. All moves were `git mv` (history preserved); only path/import references changed.
- **New `src/optimize/` package** (`__init__.py` documents it as the sample-driven harness):
  - `pilot.py` ← `translate/pilot.py` **moved unchanged** (Phase 5.0 had already collapsed the pilot to
    the sample-only measurement harness, so there was no "corpus-pilot duty" left to split out — the
    pilot *is* the optimize harness). `_SAMPLE_FILE` now defaults to the in-package
    `samples/pilot_sample_100.json`; `PILOT_SAMPLE_FILE` env override still resolves relative to repo
    root. Entry point: `python -m optimize.pilot`. (Debug JSONL kept at `reports/translate/debug/` —
    behavior-preserving; relocating that report path is deferred, not required by the move.)
  - `reset_golden.py`, `run_compare.py` ← moved from `translate/` (same `_DEFAULT_SAMPLE` treatment in
    reset_golden). Entry points: `python -m optimize.{reset_golden,run_compare}`.
  - `build_sample.py` ← `scripts/build_sample_200.py` (renamed). Reads/writes `samples/pilot_sample_*.json`.
  - `samples/pilot_sample_100.json` + `pilot_sample_200.json` ← moved from `docs/` (stale
    `excludes_questions_from` metadata in the 200 file repointed to the new path).
- **Per user instruction** (overriding the plan's "keep a thin root `optimize_loop.sh` shim + port to
  `optimize/loop.py`"): `optimize_loop.sh` and `prompt_changelog.md` were **moved into `src/optimize/`**
  (no root shim, no Python port). The shell driver's three `python -m translate.*` calls → `optimize.*`,
  `PILOT_SAMPLE_FILE` → `src/optimize/samples/pilot_sample_200.json`, and the two `prompt_changelog.md`
  refs in the embedded `claude -p` prompt → `src/optimize/prompt_changelog.md`. It still must be run from
  repo root (documented in its header).
- **Tests** moved to `tests/optimize/` (`test_pilot.py`, `test_run_compare.py`); patch/import targets
  `translate.{pilot,run_compare}` → `optimize.*`. Basenames stay unique (no `__init__` collision).
- **`pyproject.toml`**: `optimize` added to isort `known-first-party` (ruff import ordering) and to the
  hatch wheel `packages`. (Runtime/test import already works via the editable `.pth`, which puts `src/`
  on the path.)
- **`docs/claude-corrections.md`**: the live `translate.pilot` operational commands repointed to
  `optimize.pilot` so future sessions don't hit a dead module path.
- **823 passed; ruff clean.** Smoke-tested all four `python -m optimize.*` imports + sample-path resolution.
- **Note for Phase 11 memory**: prompt-opt toolchain = `src/optimize/`; run the loop from repo root via
  `./src/optimize/optimize_loop.sh`; samples live in `src/optimize/samples/`.

#### Phase 6.1 — report* review + gap-term dedup (user-requested) — DONE
Two side tasks the user asked for after Phase 6.
- **`ingest/report*` review** → `report.py` (M1 provenance, `python -m ingest.report` →
  `reports/m1_provenance.txt`) **deleted** (`e29009b`): zero importers, no tests, in no pipeline
  step; per-term provenance lives in `term_usage`, its "pending M3 review" gap roll-up is superseded
  by the review surface (`export_sheet`) + M2 dedup CSV. `report_m2.py` is live (drives `ReportStep`,
  tested) → **kept** (Phase 7 still renames it to `coverage_report.py`).
- **Gap-term dedup** (`94a39fe`): CLTK preserves token case, so capitalized/sentence-initial tokens
  created capital-variant duplicate gap terms **and** could shadow a lowercase Krystal term. Measured
  in live DB: **201 case-variant dup groups, 531 uppercase gap rows** (all gap; 0 Krystal — Krystal is
  100% lowercase), incl. a proposed `Caritas` next to approved `caritas`. Fix = canonicalize gap lemmas
  to lowercase: new `gap_terms._canonical_lemma`; `_scan_gap_lemmas` + `_ensure_glossary_term` +
  resolver Krystal lookup/gap-keying all case-insensitive (`lemma_to_term` lowercase-keyed);
  `sense_mining.write_proposed_senses` dedup now casefold + within-batch. +7 tests. **802 passed; ruff clean.**
- **⚠ PENDING (gated on human approval) — existing-data cleanup.** The fix is forward-only; the 531
  uppercase rows / 201 dup groups already in the live glossary remain. Cleanup is a destructive data
  migration (delete gap `glossary_term` + cascade senses/renderings/term_usage for the capitalized
  variants, then re-run the resolver to regenerate canonical lowercase proposals — gap proposals are
  idempotently regenerated; OR in-place `lower()` rename where no lowercase twin exists). **Per
  CLAUDE.md: write as a `--dry-run` script, present the plan, STOP for approval before any DELETE.**
  Not yet started — decision surfaced to the user.

### Commits so far (on `aquinas-refactor`)
- `e2c7c8f` refactor(api): single DeepSeekClient for all chat calls (Phase 3)
  - `common/deepseek_client.py`: `DeepSeekClient(model, *, url, api_key, timeout)` with
    `chat(messages, *, temperature, max_tokens, response_format=None, timeout=None) -> ChatResult`
    (content/usage/raw). API key resolved lazily at call time (module-level clients build at import).
    `DeepSeekAPIError(RuntimeError)` carries `.status_code`; transport/empty-choices → plain RuntimeError.
  - Callers keep only their own policy: translator/reviewer fail loud; `_call_deepseek_batch`
    soft-fails to `{}` except fatal 401/402/403 (via `exc.status_code`) + keeps `_api_stats`;
    `call_deepseek_label` keeps retry-with-backoff (now catches `RuntimeError`/`JSONDecodeError`).
    Each keeps its own api-key guard where it must raise before the soft-fail path.
  - Tests repointed to patch `common.deepseek_client.requests.post`; fakes set `.status_code`
    (client no longer calls `raise_for_status`). New `tests/common/test_deepseek_client.py` (+9).
    **748 passed; ruff clean.**
- `4c7e934` test: repair pre-existing test drift to establish a green baseline
  - The suite on HEAD was **not** green: 1 failure + 2 uncollectable modules.
  - Removed dead tests for removed functions: `_iteration_count`, `fetch_reviewer_notes`
    (test_pilot.py); `get_model_rendering`, `get_term_flags` (test_import_approvals.py).
  - Rewrote 6 stale `process_approval` tests to the current contract (see §6); fixed the stale
    `find-and-replace` microedit assertion in test_loop.py.
  - Result: **694 passed / 1 failed / 2 errors → 745 passed.**
- `ef59519` test: add shared conftest fakes (FakeConn/FakeCursor, gspread)
  - `tests/conftest.py` with shared `FakeConn`/`FakeCursor` + `FakeWorksheet`/`FakeSpreadsheet`
    and `fake_conn`/`fake_worksheet`/`fake_spreadsheet` factory fixtures. Additive — existing
    modules keep their local fakes. New repo/step tests should use these.
- `c126da6` feat(models): typed dataclasses for shared pipeline shapes (Phase 1)
  - `src/common/models.py`: frozen `Sense`/`Term`/`Segment`/`Constraint` with `from_row`/`as_dict`
    bridges + `Constraint.to_prompt_dict`; re-exports `Resolution`, `CheckResult`, `ReviewResult`,
    `UsageInfo`, `SegmentOutcome`, `ArticleResult`. Additive — no consumer touched.
  - **v_segment flag RESOLVED**: `loop.get_segment_with_texts` SELECT carries `reply_to` +
    `translation_status` (the actual `v_segment` view instead exposes `slovak_draft`/`slovak_final`,
    not used by that loader). `_load_segments` carries only the six base columns. Both fields modeled
    as optional on `Segment` (default `None`) so one model covers both producers; `as_dict` emits them
    only when set, preserving each producer's exact dict shape.
  - `tests/common/test_models.py` (7 tests). Suite **752 passed** (745 + 7); ruff clean.

#### Phase 2 — Repository layer (this commit)
- **New `src/storage/` package** (the persistence boundary): `storage/db.py` (connection +
  source/work lookups, moved from `common/db.py`), `storage/models.py` (the four pure dataclasses,
  moved from `common/models.py`), `storage/repositories.py` (`GlossaryRepository`,
  `SegmentRepository`, `TermUsageRepository`, `RunRepository`). **SQL moved verbatim**; only the
  row→model mapping is new. Repos return models (`Term`/`Sense`/`Segment`/`Constraint`) or scalars.
  All ~37 `common.{db,repositories,models}` import sites rewritten to `storage.*`.
- Old module-level helpers are now **thin wrappers** delegating to the repos, preserving exact legacy
  shapes for un-migrated callers: `glossary_repo._load_glossary/_load_segments/update_sense_status/
  bump_sense_version/write_human_rendering`; all of `corpus_db.*`; `loop.get_segment_with_texts/
  get_locked_terms/write_segment_text/update_translation_status/write_reviewer_notes/
  update_sense_version_used`; `resolution._write_term_usage`; `run._glossary_snapshot/_open_run/
  _close_run` delegate to `RunRepository`.
- **Constraint model corrected**: `category` is now stored **raw** (NULL→None); the "term" default
  is applied only in `to_prompt_dict`. The previous from_row default was lossy and conflicted with
  `get_locked_terms`'s contract (`test_get_locked_terms_category_none_for_krystal_terms`). test_models
  updated accordingly.
- **Import cycle eliminated structurally** (no PEP-562 hack): the old `models.py` did double duty —
  defining the pure persistence dataclasses *and* re-exporting high-level result types
  (`Resolution`/`SegmentOutcome`/`ArticleResult`/…) from modules that depend on the repo layer. That
  forward edge closed the loop. The re-export surface was **dead** (no production consumer; only one
  test used it), so it was deleted. `storage/models.py` is now a verified **leaf** (imports nothing
  from the pipeline); `storage.repositories` depends only on it. Result/return types are imported from
  their owning modules, as every real consumer already did.
- Tests live in `tests/storage/` (`test_models.py` moved here; `test_glossary/segment/term_usage.py`
  + `test_run_repo.py` — the last renamed to dodge the pytest basename collision with
  `tests/translate/test_run.py`). The dead `test_reexports_are_the_canonical_types` was dropped.
  Suite **781 passed**; ruff clean.
- M5 labels stripped from `translate/run.py` (touched-file rule).
- **Transition shims still in `common/`**: `glossary_repo.py`, `corpus_db.py` (thin wrappers over
  `storage.repositories`), and the inline `loop`/`resolution`/`run` DB helpers. Phase 2b deletes them.

#### Phase 2b handoff (next) — flip callers to models, delete wrappers
- `resolver.py`/`pipeline.py` consume `GlossaryRepository.load_glossary()` → `list[Term]` directly;
  `resolution.py` voting uses `Term`/`Sense` attributes instead of dict subscripts. **Then** retype
  `Resolution.term`/`.sense` from `dict` to `Term`/`Sense` (the deliberate Phase-1 deferral) and
  `TermUsageRepository.write_term_usage` reads `res.sense.sense_id`/`.version`.
- `loop.translate_segment` consumes `Constraint`/`Segment` models + `Constraint.to_prompt_dict`
  instead of the dict wrappers; drop `get_segment_with_texts`/`get_locked_terms` shims.
- `run.py`/`corpus_db` callers use `SegmentRepository` directly; delete `corpus_db.py` + the
  `glossary_repo`/`loop` shims in a final cleanup commit.
- **Still in import_approvals (not yet moved)**: `write_human_surface`, `get_current_sense`,
  `get_la_surface`, and the inline `process_approval` UPDATEs → fold into `GlossaryRepository`
  (process_approval contract is locked by tests — see §6; keep it).

---

## 4. Target architecture (end state)

```
src/
  storage/                 # NEW — persistence boundary (a leaf; no pipeline imports)
    db.py                  # get_conn + source/work lookups (moved from common/db.py)
    models.py              # Segment, Sense, Term, Constraint (pure dataclasses; moved from common/models.py)
    repositories.py        # GlossaryRepository, SegmentRepository, TermUsageRepository, RunRepository
  common/
    deepseek_client.py     # NEW — DeepSeekClient (one place for all requests.post)
    lemmatize.py, pricing.py
    glossary_repo.py, corpus_db.py   # TRANSITION shims over storage.repositories — deleted in Phase 2b
  ingest/
    source_parser.py       # NEW — SourceParser ABC; parser_latin/bahounek/english subclass it
    resolution.py, resolver.py, gap_terms.py, sense_mining.py, krystal.py
    coverage_report.py     # RENAMED from report_m2.py
  translate/
    loop.py, translator.py, reviewer.py, run.py, pilot.py, prechecks.py
  pipeline/                # NEW
    step.py, runner.py, context.py
    reporting.py           # StepReport writer → reports/<stage>/
    interactive.py         # python -m pipeline — state machine + numbered menu
  optimize/                # NEW — moved DSPy-like toolchain
    loop.py (port of optimize_loop.sh), reset_golden.py, run_compare.py,
    build_sample.py, samples/pilot_sample_*.json, prompt_changelog.md
db/schema.sql              # NEW — single annotated current-state schema
migrations/archive/        # 001–007 moved here (historical only)
scripts/purge_habere_ppp_usage.py  # NEW one-off
```

---

## 5. Phase-by-phase plan

### Phase 1 — Typed data structures → `src/common/models.py`  ⏳ NEXT
Frozen dataclasses replacing ad-hoc dicts. **Recommended approach:** create `models.py` as a pure,
additive module + unit tests + `from_row`/`as_dict` helpers, and migrate consumers in Phase 2 where
the dict→model boundary naturally flips (repositories return models). This keeps Phase 1 low-risk.

Exact shapes (verified from source):

```python
@dataclass(frozen=True)
class Sense:
    sense_id: int
    context_label: str | None
    version: int
    cs_lemma: str | None
    cs_content: str | None
    en_cue: str | None
    sk_content: str | None
    la_surface: str | None

@dataclass(frozen=True)
class Term:
    term_id: int
    latin_lemma: str
    is_multiword: bool
    category: str | None
    la_surface: str | None
    senses: tuple[Sense, ...]      # was list; freeze for hashability

@dataclass(frozen=True)
class Segment:
    segment_id: int
    locator_path: str             # ltree cast to text
    element_type: str
    latin: str | None
    czech: str | None
    english: str | None
    # get_segment_with_texts (v_segment) MAY also carry reply_to/translation_status — VERIFY
    # the v_segment view columns before finalizing these optional fields.

@dataclass(frozen=True)
class Constraint:
    latin_lemma: str
    required_slovak: str
    context_label: str | None
    category: str                 # defaults to "term" when NULL
    sense_id: int | None = None
    version: int | None = None
    latin_surface: str | None = None
```

- Source of `Term`/`Sense`: `glossary_repo._load_glossary` (`src/common/glossary_repo.py:24-104`).
  Dict keys are exactly: term `{term_id, latin_lemma, is_multiword, category, la_surface, senses}`;
  sense `{sense_id, context_label, version, cs_lemma, cs_content, en_cue, sk_content, la_surface}`.
- Source of `Segment`: `glossary_repo._load_segments` (`:107-122`) returns
  `{segment_id, locator_path, element_type, latin, czech, english}`.
- Source of `Constraint`: `loop.get_locked_terms` SELECT returns
  `{latin_lemma, category, latin_surface (= gt.la_surface), required_slovak (= sr.content),
  sense_id, version, context_label}`; `loop.translate_segment` then builds the prompt constraint as
  `{latin_lemma: latin_surface or latin_lemma, required_slovak, context_label, category or "term"}`.
- **Re-home / re-export** existing dataclasses into `models.py` (re-export from current modules to
  avoid import churn): `Resolution` (`ingest/resolution.py`, type its `.term`/`.sense` as Term/Sense),
  `CheckResult` (`translate/prechecks.py`), `ReviewResult` (`translate/reviewer.py`),
  `UsageInfo` (`common/pricing.py`), `SegmentOutcome` (`translate/loop.py`),
  `ArticleResult` (`translate/run.py`). Parser element dataclasses unify in Phase 4.
- Add `from_row(row)` classmethods mirroring the SQL column order, and `as_dict()` only where a
  consumer still needs a dict during transition.
- Tests: `tests/common/test_models.py` — construction, from_row, as_dict round-trip.
- **Acceptance:** suite green; models importable; no consumer behavior change yet.

### Phase 2 — Repository layer → `src/common/repositories/`
Consolidate all SQL behind cohesive repos returning typed models. **Move SQL verbatim; only the
row→model mapping changes** (locked by existing tests: `test_glossary_repo.py`, `test_corpus_db.py`,
plus loop/resolver tests).
- `GlossaryRepository`: `load_glossary() -> (list[Term], list[Term])`;
  `locked_terms(segment_id) -> list[Constraint]` (from `loop.get_locked_terms`);
  `update_sense_status`, `bump_sense_version`, `write_human_rendering`, `write_human_surface`.
- `SegmentRepository`: `get_segment`, `load_body_segments`, `write_segment_text`,
  `update_translation_status`, `write_reviewer_notes`, `update_sense_version_used`, **plus the whole
  `common/corpus_db.py` set** (article locators, pending/stale/human-edited, reset, flag_needs_human).
- `TermUsageRepository`: `write_term_usage(segment_id, resolutions)`.
- `RunRepository`: open/close run + insert run_segment (lift from `translate/run.py`).
- Inline DB helpers currently in `loop.py` to move: `get_segment_with_texts`, `get_locked_terms`,
  `write_segment_text`, `update_translation_status`, `write_reviewer_notes`, `update_sense_version_used`.
- Old module-level functions (`glossary_repo._load_glossary`, `corpus_db.*`) become thin wrappers
  during transition; delete in a final cleanup commit.
- Tests: `tests/common/repositories/` using `conftest` fakes (canned rows → assert returned models).
- **Acceptance:** suite green; all SQL lives in repositories; callers use models.

### Phase 3 — DeepSeek client → `src/common/deepseek_client.py`
One `DeepSeekClient` collapsing the 4 duplicated `requests.post` blocks:
- `translator.call_translator_v3` (temp **0.3**, max_tokens 2048)
- `reviewer.call_reviewer_r1` (deepseek-reasoner, max_tokens ~8000)
- `deepseek._call_deepseek_batch` (gap classify, temp 0.0)
- `sense_mining.call_deepseek_label` (temp 0.0, json_object)
- `DeepSeekClient(model, url, api_key)` + `chat(messages, **opts) -> (text, UsageInfo)` using
  `pricing.extract_usage`; role methods `translate/review/classify_gap_terms/label_senses`.
- **Preserve exactly**: per-call temperature/max_tokens, and the **fail-loud
  `RuntimeError`-after-retries** semantics (loop's `try/except RuntimeError` depends on it).
- Tests: record representative responses to `tests/fixtures/api/` and replay.

### Phase 4 — Parser base class → `src/ingest/source_parser.py`
`SourceParser` ABC: abstract `parse(raw) -> list[ParsedElement]`; concrete `store()` via
`SegmentRepository`; `run()` with fail-loud + gap/anomaly logging.
- `parser_latin`, `parser_bahounek`, `ingest_english` become subclasses (override `parse`, declare
  `(lang, source)`). Unify the 3 element dataclasses (`ParsedElement`, `BahounekElement`,
  `EnglishElement`) into one `ParsedElement(locator, element_type, text)`.
- Locked by existing parser tests.

**Design tension found while scoping (decide before building):**
The three parsers do NOT share one `store()` contract:
- `parser_bahounek` (`insert_bahounek_texts`, lang `cs`) and `ingest_english` (`insert_english_texts`,
  lang `en`) genuinely share the loop: *look up existing segment by `locator_path::ltree` → upsert
  `segment_text(lang, content, source_id)`; missing segment → gap-log-and-skip, or RuntimeError when
  no gap log (fail-loud).* This is the real, valuable dedup.
- `parser_latin` is structurally different: it CREATES the segment graph (`_insert_article`: segment
  rows + title placeholders + reply_to links + idempotent term_usage/segment wipes). It never "looks
  up an existing segment by locator." `SegmentRepository` has no create-segment method. Forcing latin
  under a shared text-overlay `store()` = a risky rewrite of working idempotent code for zero dedup.
- Element dataclasses can't be unified naively: locked tests reference `.czech_text`, `.english_text`,
  and latin's 4-positional `ParsedElement(locator, etype, text, reply_number)`; latin's insert needs
  `reply_number` for reply_to linking.

**Recommended scope** (honest + low-risk, all behavior-preserving):
1. `src/ingest/source_parser.py`: a `TextOverlayParser` ABC for the two overlay parsers — class attr
   `lang`; abstract `parse(...) -> list[ParsedElement]`; concrete `store(conn, elements, src_id,
   gap_log)` holding the shared lookup-and-upsert loop with the fail-loud/gap-log policy. One unified
   `ParsedElement(locator, text)` for these two (rename `czech_text`/`english_text` → `text`; update
   ~6 test sites — mechanical, behavior-preserving).
2. Add `SegmentRepository.get_segment_id_by_locator(locator) -> int | None` (the one new SQL) so the
   shared `store()` goes through the repo layer (Phase-2 invariant: all SQL in repos).
3. `parser_bahounek.insert_bahounek_texts` / `ingest_english.insert_english_texts` become thin
   wrappers over the base `store()`, preserving their exact signatures/return for callers + tests.
4. **Leave `parser_latin` as-is** (the structural parser). Optionally note in its docstring that it is
   the segment-graph creator, distinct from the overlay parsers — but do NOT contort it into the ABC.

If the user instead wants all three under one ABC, that's a larger behavioral change requiring a
`SegmentRepository` create-segment surface and rewriting latin's idempotent insert — flag the added
risk first.

### Phase 5 — Pipeline: steps, runner, reporting, interactive → `src/pipeline/`
- **5a** `PipelineStep` protocol (`name`, `run(ctx) -> StepResult`, optional `verify()`),
  `PipelineContext` (work_id, repositories, knobs, reports dir), `Runner` (uniform
  logging/timing/fail-loud — lift `ingest/pipeline.py::_STEP_FNS` loop).
- **5b** Source verification as **prerequisite step 0**: wrap `acquire/verify.py`'s
  `check_latin/bahounek/krystal/dominican/freddoso/db/env` into `VerifySourcesStep`; downstream
  steps refuse to run if it fails. Acquire downloaders become steps. Root `verify_sources.py` → thin
  shim. Each `python -m …` entry point becomes a thin shim instantiating its Step (CLIs preserved).
- **5c** Reporting → `src/pipeline/reporting.py`: a `StepReport` writer every step uses; concise +
  actionable (what changed, what needs human action, anomalies/gaps); written to per-stage folders
  `reports/acquire|ingest|resolve|review|translate|optimize/`. Route PromptLogger deep-dive JSONL to
  `reports/common/`.
- **5d** Interactive driver → `src/pipeline/interactive.py` (`python -m pipeline`): shows flow
  position (from DB status: pending/translated/needs_human counts, glossary proposed vs approved,
  last run) + last command (persist to `.pipeline_state.json`, no DDL) + numbered menu invoking
  Steps (recollect terms, mine senses/suggest labels+translations, re-export for review, import
  approvals, translate/retranslate, rerun-stale). Menu items just call Steps — no logic duplication.

### Phase 6 — Isolate prompt-optimization toolchain → `src/optimize/`
- Move into `src/optimize/`: `reset_golden.py`, `run_compare.py`, `scripts/build_sample_200.py`,
  `docs/pilot_sample_*.json` → `src/optimize/samples/`, `prompt_changelog.md`, and a ported loop
  driver `optimize/loop.py` (`python -m optimize.loop`) from `optimize_loop.sh` (keep a thin root
  `optimize_loop.sh` shim).
- Factor `PILOT_SAMPLE` mode out of `translate/pilot.py` into `optimize/`; `pilot.py` keeps only
  corpus-pilot duties. Shared translation goes through Phase 2/3 repos + client.
- **Worktree workflow** (document + enforce in the driver): the loop runs in a dedicated worktree on
  a `prompt-opt` branch; reset→pilot→compare→edit→commit happen there; vetted prompt changes merge
  to `main` deliberately, never auto-pushed.

### Phase 7 — Strip milestone labels; rename milestone-named files
- Sweep `M0`–`M5`/"milestone" from comments & docstrings, rewording to describe behavior.
  Files with refs (counts): `krystal.py`(6), `glossary_repo.py`(5), `report_m2.py`(4),
  `resolver.py`(3), `report.py`(3), `run.py`(2), `pilot.py`(2), `sense_mining.py`(2),
  `pipeline.py`(2), `parser_bahounek.py`(2), `acquire/verify.py`(2), `freddoso.py`(2),
  `export_sheet.py`(1), `gap_terms.py`(1), `deepseek.py`(1), `corpus_db.py`(1) + all migration
  headers + test files. (Design docs stay in `.claude/`.)
- Rename `ingest/report_m2.py → ingest/coverage_report.py` (+ test + imports + pipeline step name).

### Phase 8 — Consolidate DB schema → single annotated `db/schema.sql`
- Generate `db/schema.sql` = `pg_dump --schema-only` of the live DB (currently at migration 007),
  cleaned and **heavily commented per table/column** (purpose + consumer; complements
  `.claude/database.md`). Migrations present: `001_initial`, `003_term_category`,
  `004_translation_status`, `005_run_analytics`, `006_sense_rendering_la_lang`, `007_*` (002 appears
  absent — verify). Move `001`–`007` to `migrations/archive/`. Header documents: fresh setup runs
  `schema.sql`; archive is historical only.
- **No DB change.** Verify `schema.sql` recreates an identical empty schema in a throwaway DB.

### Phase 9 — Domain housekeeping (the only behavioral phase)
- **9a `_oov_stem`**: encapsulate `_oov_stem` + `generate_slovak_forms` (`common/lemmatize.py`) into
  one `SlovakTermMatcher` (`forms(lemma)`, `matches(token, lemma)`); behavior locked by
  `test_prechecks.py`. Optional, non-blocking cleaner impl: MorphoDiTa generation first; for true OOV
  derive stem from longest-common-prefix over generated variants, or a data-driven Slovak suffix
  table (`-osť`, ň-stem). Adopt only if it removes known false positives (`milostivo↔milosť`,
  `vás↔vášeň`) without regressing snapshots.
- **9b `_drop_habere_ppp_constraints`**: run → harden → delete.
  1. `scripts/purge_habere_ppp_usage.py`: find bogus `habitus` `term_usage` rows (same
     `_HABERE_PPP_RE` + `lemmatize_latin` logic the read-time filter uses). **Dry-run, report, STOP
     for human approval, then delete.**
  2. Harden `resolution.resolve_segment` with `pos_tag_latin` (already in `common/lemmatize.py`) so a
     perfect-passive participle + *esse* never maps to the noun term. New resolver test.
  3. Delete `_drop_habere_ppp_constraints`, its call in `translate_segment`, and `_HABERE_PPP_RE`.
     **Keep `_SUFFIX_RE`** (still used by `_build_surface_constraints`). Update loop tests.

### Phase 10 — Final regression gate
- `uv run pytest -q` matches the ≥745 baseline (zero regressions); `ruff` clean.
- Re-run resolver + an optimize/pilot tiny sample; diff `term_usage`/`segment_text` writes vs a
  pre-refactor snapshot — identical. Smoke-run every `python -m …` entry point. Confirm per-stage
  `reports/` folders populate.

### Phase 11 — Record conventions to permanent memory
Write a `project`-type memory entry (Claude memory dir, indexed in `MEMORY.md`):
- Folder map (§4) + rules: typed models over dicts; all SQL via repositories; all DeepSeek calls via
  the client; every Step emits a concise `StepReport` into `reports/<stage>/`; new stages register as
  a `PipelineStep` (+ menu entry); verification is prerequisite step 0; no milestone labels in code;
  prompt-opt changes only via the `prompt-opt` worktree → reviewed merge.

---

## 6. Reference: current `process_approval` contract (locked by tests in `4c7e934`)
`process_approval(conn, row, human_src_id) -> (status, version_bumped)`:
- `NOT_FOUND` if sense missing → (NOT_FOUND, False)
- `CONFLICT` only if `db_version` blank → (CONFLICT, False)  *(version mismatch no longer conflicts)*
- `ALREADY_CONFIRMED` if status already 'approved' → (ALREADY_CONFIRMED, False)
- otherwise (proposed): write human SK rendering; write context_label (empty→NULL, no own bump);
  **always bump**; write la_surface if supplied & different; set status approved → (OK, True)

## 7. Reference: test bloat / pruning candidates (Phase-by-phase, human-confirm deletions)
Prune alongside the code each phase touches (criteria: delete framework-behavior tests and ones
subsumed by characterization tests; collapse near-duplicates via `@pytest.mark.parametrize`; keep
resolution/sense-voting, precheck OOV, verdict parsing, prompt building, parser locators, repository
mappings, loop orchestration, run analytics, import/export semantics).
- `test_loop.py` (1055) ↔ `test_resolver.py` (875) — overlap.
- `test_freddoso.py` (349) — low-value 20%-coverage source.
- `test_prompt_logger.py` (223); `test_glossary_repo_stubs.py` (109) — thin.
- ~10 local FakeConn impls now superseded by `tests/conftest.py` — migrate opportunistically.

## 8. Verification commands (every phase)
```bash
uv run pytest -q                                   # green, ≥745
uv run ruff check
# Phase 8: psql "$DATABASE_URL" -f db/schema.sql   # into a throwaway DB; confirm identical
# Phase 9: uv run python -m scripts.purge_habere_ppp_usage --dry-run   # review → approve → run
# Phase 5: uv run python -m pipeline                # interactive driver
```

---
*The Claude Code plan file mirror lives at `~/.claude/plans/velvety-floating-conway.md`.*
