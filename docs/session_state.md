# Session State

## Current Milestone
M2 — **IN PROGRESS** — pipeline code complete; full corpus run pending

## Status
313 tests pass. All M2 code written and tested. Full corpus ingest not yet executed (Step 1 pause point: review anomaly log before proceeding).

## Completed This Session
| Step | Status | Notes |
|---|---|---|
| M2 planning | ✓ | DB state verified, idempotency confirmed, plan approved |
| decisions.md | ✓ | Prefect deferred to M4+ decision recorded |
| `parser_latin.py` — `run_full()` | ✓ | Scans all sth*.html, logs anomalies, never crashes |
| `parser_bahounek.py` — gap logging | ✓ | `insert_bahounek_texts` logs gaps instead of raising; `write_bahounek_coverage` added |
| `ingest_english.py` — missing file skip | ✓ | `[SKIP]` print + continue instead of raise |
| `resolver.py` — source_id bug fix | ✓ | Gap stubs now use `src_model` (source_id=7), not `src_krystal` |
| `resolver.py` — DeepSeek V3 | ✓ | `_call_deepseek()` wired for model_proposed terms; `_api_stats` tracks cost |
| `report_m2.py` | ✓ | Coverage report + dedup roll-up CSV |
| `pipeline.py` | ✓ | Single CLI: `--step latin/bahounek/english/resolve/report` or `--all` |

## Key Decisions (this session)
- **Prefect deferred to M4+**: M2 uses plain `pipeline.py` CLI; Prefect adds value only at the translation loop (remote machine, checkpoint/resume). See decisions.md.
- **Gap stub source_id fixed**: M1 bug — gap `sense_rendering` rows were attributed to `krystal` source. M2 corrects this to `model` source via `ON CONFLICT DO UPDATE` on re-run.
- **DeepSeek via `requests`**: No new SDK dependency. Uses existing `requests` package.

## Files Modified / Created (M2)
| File | Change |
|---|---|
| `src/ingest/parser_latin.py` | Added `run_full(anomaly_log, latin_dir)` + `_group_elements_by_article()` |
| `src/ingest/parser_bahounek.py` | Gap logging; `write_bahounek_coverage()`; `run(gap_log_path)` |
| `src/ingest/ingest_english.py` | Missing file: skip instead of raise |
| `src/ingest/resolver.py` | `src_model` fix; `_call_deepseek()`; `_api_stats`; `get_api_stats()` |
| `src/ingest/report_m2.py` | New: coverage report + dedup roll-up |
| `src/ingest/pipeline.py` | New: single CLI orchestrator |
| `tests/ingest/test_parser_latin.py` | Added `TestGroupElementsByArticle`, `TestRunFull` |
| `tests/ingest/test_parser_bahounek.py` | Added gap logging + coverage tests |
| `tests/ingest/test_parser_english.py` | New: missing file skip tests |
| `tests/ingest/test_resolver.py` | Added `TestCallDeepseek` |
| `tests/ingest/test_report_m2.py` | New: coverage report + rollup tests |
| `tests/ingest/test_pipeline.py` | New: step dispatch + --all tests |
| `.claude/decisions.md` | Prefect deferred to M4+ entry |
| `docs/session_state.md` | This file |

## Exact Next Step
**Run the full corpus:**
```bash
# Step 1 — Latin ingest (PAUSE and review anomaly log after)
uv run python -m ingest.pipeline --step latin

# Review reports/m2_parser_anomalies.txt
# Categorise anomalies by type, fix by category, then:

# Steps 2+3 — Bahounek + English (can run in parallel)
uv run python -m ingest.pipeline --step bahounek
uv run python -m ingest.pipeline --step english

# Step 4 — Resolve (requires DEEPSEEK_API_KEY)
export DEEPSEEK_API_KEY=...
uv run python -m ingest.pipeline --step resolve

# Steps 5+6 — Report
uv run python -m ingest.pipeline --step report
cat reports/m2_coverage.txt
```

## Sources on Disk (unchanged)
| Source | Location | Status |
|---|---|---|
| Latin (Corpus Thomisticum) | `sources/latin/` | 87 files, 2,663 articles |
| Bahounek Czech | `sources/czech/bahounek/` | 4 files |
| Krystal docx | `sources/czech/krystal/` | 258 paragraphs |
| Dominican English | `sources/english/dominican/` | 614 files |
| Freddoso English | `sources/english/freddoso/` | 4 TOC files + `coverage_gaps.json` |
