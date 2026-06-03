# Session State

## Current Milestone
M2 — **IN PROGRESS** — corpus ingested; gap-term resolution redesigned to dynamic
model categorization. Full resolve blocked only on DeepSeek account credit (HTTP 402).

## Status
390 tests pass. Gap-term handling reworked from a static POS/blocklist filter to
**dynamic model categorization**: one DeepSeek call per lemma now classifies (category),
canonicalizes (merges lemmatizer fragments), and translates. Migration `003_term_category.sql`
applied (adds `glossary_term.category`).

## DB State
| Table | Rows | Notes |
|---|---|---|
| `segment` | 25,782 | unchanged |
| `segment_text` | la=22,621 / cs=20,673 / en=25,466 | unchanged |
| `term_usage` | 0 | resolve not yet run (blocked on credits) |
| `glossary_sense` | ~1,400 | incl. **1,274 stale `proposed`** senses from earlier runs |
| `glossary_term.category` | column added (003) | NULL for Krystal; set by model for gap terms |

## Key Decisions (this session)
- **Static blocklist REJECTED.** A hardcoded Latin word list is brittle, Summa-baked,
  and gives no per-term place to adjust meaning later. Replaced by dynamic categorization.
- **Mechanical-only pre-filter:** frequency floor + length gate + CLTK numeric-suffix strip
  (`dico2`→`dico`). No POS filter, no word lists. New scan finds **3,496** qualifying lemmas.
- **Model returns category + canonical headword + Slovak** per lemma in one batch call.
  Categories: `term` / `name` / `formula` (kept — Praeterea/Respondeo) / `prose`.
  Stored on `glossary_term.category`, overridable in M3.
- **Canonical merge:** divina/divino/divinus → one `divinus` term (model-driven, no static map).
- **No-stub invariant:** a gap lemma becomes a `term_usage` row only if its canonical headword
  got a proposal; non-qualifying lemmas create no row and no bracketed stub. Report has a loud
  guardrail that fails if any `proposed_slovak` starts with `[`.
- **Category column via migration 003** (numbered 003 because `002_schema_fixes.sql` already
  existed and was already applied).

## Files Modified / Added This Session
| File | Change |
|---|---|
| `migrations/003_term_category.sql` | NEW — adds `glossary_term.category` CHECK col; **applied** |
| `src/ingest/resolver.py` | `_call_deepseek_batch` returns {canonical,category,slovak}; `_parse_batch_entry`, `_strip_lemma_suffix`; `_scan_gap_lemmas` mechanical-only; `_propose_gap_terms` canonical merge + dropped; `_write_gap_proposals` writes category + returns gap_terms_db; `resolve_segment` no-stub membership (no DB writes); `run()` drops pos_filter; pilot shows category dist + merges |
| `src/ingest/pipeline.py` | `_step_resolve`/`_step_pilot` drop GAP_POS_FILTER; add GAP_BATCH_SIZE/GAP_MAX_WORKERS |
| `src/ingest/report_m2.py` | category in rollup + CSV; `assert_no_stub_proposals` guardrail; gap-category breakdown in coverage |
| `src/ingest/reset_gap_proposals.py` | NEW — dry-run by default; `--execute` deletes gap state (term_usage→sense_rendering→proposed senses→orphan terms), FK-safe, scoped via `bool_and(status='proposed')` |
| `tests/ingest/test_resolver.py` | rewrote batch/scan/propose/pilot tests; added strip/parse/resolve_segment tests |
| `tests/ingest/test_report_m2.py` | category column + stub guardrail tests |
| `tests/ingest/test_reset_gap_proposals.py` | NEW — DB-free FakeConn tests for reset |
| `.claude/m2_scale.md` | Step 4 rewritten (dynamic design); acceptance criteria updated |
| `.claude/m1_resolution.md` | Step 7 M2 forward-pointer note |
| `.claude/m0_setup.md` | verify_sources DeepSeek liveness probe + acceptance line |

## Exact Next Step
**Blocked on DeepSeek credit.** Once the account is funded:
```bash
set -a && source .env && set +a
# 1. (optional) confirm cost/categories on a sample
uv run python -m ingest.pipeline --pilot 50 --batch-sizes 25,50
# 2. clear stale proposals from earlier runs
uv run python -m ingest.reset_gap_proposals --execute
# 3. full resolve (categorize + canonicalize + translate, then term_usage)
GAP_BATCH_SIZE=50 uv run python -m ingest.pipeline --step resolve
# 4. coverage report + dedup rollup (asserts no stub leaks)
uv run python -m ingest.pipeline --step report
cat reports/m2_coverage.txt
```
Expected: ~3,496 lemmas scanned → fewer canonical terms after merge; rollup grouped by
category; no bracketed stubs; logged cost ~$0.10–$10.

## Pre-M3 Refactoring (planned, not yet started)

`resolver.py` (1,120 LOC) mixes three concerns. Split it before M3 adds glossary write-back. Strangler strategy — one commit per step, suite green throughout. **Regression oracle:** `uv run python -m ingest.pipeline --step report` output must be identical before and after.

| Step | New file | What moves | Commit prefix |
|---|---|---|---|
| 1 | `ingest/deepseek.py` | `_call_deepseek_batch`, `get_api_stats`, stats lock, cost constants, model/URL env | `refactor(ingest): extract DeepSeek client` |
| 2 | `ingest/gap_terms.py` | `_strip_lemma_suffix`, `_scan_gap_lemmas`, `_load_existing_gap_terms`, `_ensure_glossary_term`, `_write_gap_proposals`, `_propose_gap_terms`, `pilot_batch_sizes` | `refactor(ingest): extract gap-term scanning and preseed` |
| 3 | `ingest/resolution.py` | `phrase_match`, `mask_spans`, `_parse_batch_entry`, `_resolve_single`, `_resolve_multi`, `resolve_segment`, `_load_glossary`, `_load_segments`, `_write_term_usage` | `refactor(ingest): extract segment resolution logic` |
| 4 | `resolver.py` slim | Retain only `run()` coordinator (~150 lines); re-export public names | `refactor(ingest): slim resolver to thin coordinator` |
| 5 | `ingest/glossary_repo.py` | Move `_load_glossary`, `_load_segments` from resolution.py; add M3 stubs: `update_sense_status`, `bump_sense_version`, `write_human_rendering` (raise NotImplementedError) | `refactor(ingest): introduce glossary_repo as M3 write seam` |

**Excluded from this refactor:**
- Parser base class extraction — deferred until full Latin scan reveals contract (per `.claude/m2_migration.md`)
- `models.py` / dataclasses — add once M3 types emerge from design
- No new dependencies
