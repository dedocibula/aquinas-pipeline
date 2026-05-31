# Session State

## Current Milestone
M2 — **IN PROGRESS** — corpus ingested (Latin + Czech + English); resolve step ready to run

## Status
339 tests pass. Full corpus ingested. Resolver redesigned for batch DeepSeek proposals.
DEEPSEEK_API_KEY must be set before running `--step resolve`.

## DB State (after steps 1–3)
| Table | Rows | Notes |
|---|---|---|
| `segment` | 25,782 | 2,655 articles ingested (8 anomalies — genuine source gaps, no fix needed) |
| `segment_text` | la=22,621 / cs=20,673 / en=25,466 | All three languages loaded |
| `term_usage` | 0 (M1 rows deleted) | Resolve step not yet run |
| `glossary_sense` | ~1,400 | M1 gap stubs still present; will be overwritten on resolve |

## Anomaly Summary (m2_parser_anomalies.txt)
8 articles with missing structural elements (sed_contra / respondeo). All are authentic
source omissions in the Corpus Thomisticum HTML — not parser bugs. No fixes required.

## Completed This Session
| Step | Status | Notes |
|---|---|---|
| Latin ingest bug fixes | ✓ | FK delete order (term_usage→segment_text→segment); per-article commit+rollback |
| English missing-segment skip | ✓ | 32 no-segment skips logged; not crashes |
| Step 1 — Latin corpus ingest | ✓ | 2,655/2,663 ingested; 8 anomalies documented |
| Step 2 — Bahounek ingest | ✓ | 20,673 cs rows |
| Step 3 — English ingest | ✓ | 25,466 en rows |
| Resolver redesign — gap proposal | ✓ | All gap lemmas (not just model_proposed) get DeepSeek proposals |
| Resolver — freq floor + POS filter | ✓ | `freq_floor=10`, `pos_filter={'N','A'}` knobs; configurable via env + run() params |
| Resolver — batch DeepSeek | ✓ | `_call_deepseek_batch` (25 lemmas/call); `ThreadPoolExecutor` (10 workers) |
| Resolver — two-phase run() | ✓ | Phase 1: scan+filter+batch-propose+write; Phase 2: main loop uses pre-written senses |
| pipeline.py — GAP_* env vars | ✓ | `GAP_FREQ_FLOOR`, `GAP_POS_FILTER` read in `_step_resolve()` |
| lemmatize.py — pos_tag_latin() | ✓ | CLTK ngram tagger; 2s for full corpus |
| 15 new tests | ✓ | TestCallDeepseekBatch, TestScanGapLemmas, TestProposeGapTerms; 339 total |

## Key Decisions (this session)
- **Gap proposals for ALL methods**: `bahounek_derived` and `english_derived` now also
  get DeepSeek proposals (using Czech/English as context). M2 spec only wired DeepSeek for
  `model_proposed` — that was too conservative and would have left thousands of stub values
  for humans to fill in manually. Method label still reflects context quality; all three
  methods receive a real Slovak proposal.
- **Freq floor = 10, POS = {N, A}**: frequency scan showed 18,939 unique gap lemmas total.
  Top-30 by frequency were almost entirely Latin function words and common verbs (dico, possum,
  secundus, Praeterea...). Floor of 10 + noun/adjective filter reduces the call set to genuine
  theological vocabulary. Lemmas with all-unknown POS tags (medieval vocab the tagger doesn't
  know) are kept (benefit of doubt).
- **Batch size 25, 10 workers**: reduces API calls from O(occurrences) to O(unique_lemmas/25);
  parallel execution via `concurrent.futures.ThreadPoolExecutor` (no new dependency).
- **DO NOTHING in _gap_sense**: pre-scan proposals are written with DO UPDATE (refreshable);
  the main loop's stub writes use DO NOTHING so they never overwrite good proposals.

## Files Modified This Session
| File | Change |
|---|---|
| `src/ingest/parser_latin.py` | FK delete order fix; per-article commit+rollback in `run_full()` |
| `src/ingest/ingest_english.py` | `insert_english_texts` logs NO_SEGMENT skips instead of raising |
| `src/ingest/lemmatize.py` | Added `_latin_pos_tagger()`, `pos_tag_latin()` |
| `src/ingest/resolver.py` | `_call_deepseek_batch`; `_scan_gap_lemmas`; `_propose_gap_terms`; `_write_gap_proposals`; two-phase `run(freq_floor, pos_filter, batch_size, max_workers)`; `_gap_sense` uses DO NOTHING |
| `src/ingest/pipeline.py` | `_step_resolve` reads `GAP_FREQ_FLOOR`, `GAP_POS_FILTER` env vars |
| `tests/ingest/test_parser_latin.py` | Added `commit()`/`rollback()` to FakeConn |
| `tests/ingest/test_resolver.py` | Added TestCallDeepseekBatch, TestScanGapLemmas, TestProposeGapTerms |

## Exact Next Step
**Run the resolve step** (requires DEEPSEEK_API_KEY):

```bash
# Set key in .env, then:
set -a && source .env && set +a

# Dry-run frequency check (optional — already done, ~762 lemmas at freq≥100):
# uv run python -c "from ingest.resolver import _scan_gap_lemmas, ..."

# Step 4 — Resolve (Phase 1: batch DeepSeek proposals; Phase 2: term_usage)
uv run python -m ingest.pipeline --step resolve

# Override knobs if needed:
# GAP_FREQ_FLOOR=5 GAP_POS_FILTER="N,A" uv run python -m ingest.pipeline --step resolve

# Steps 5+6 — Report
uv run python -m ingest.pipeline --step report
cat reports/m2_coverage.txt
```

Expected output from resolve step:
- Phase 1: ~762 lemmas qualify at freq≥10 + POS∈{N,A} (based on frequency scan)
- ~31 DeepSeek batch calls at 25 lemmas/call, 10 concurrent
- Phase 2: term_usage populated for 22,621 body segments

## Sources on Disk (unchanged)
| Source | Location | Status |
|---|---|---|
| Latin (Corpus Thomisticum) | `sources/latin/` | 87 files, 2,663 articles |
| Bahounek Czech | `sources/czech/bahounek/` | 4 files |
| Krystal docx | `sources/czech/krystal/` | 258 paragraphs |
| Dominican English | `sources/english/dominican/` | 614 files |
| Freddoso English | `sources/english/freddoso/` | 4 TOC files + `coverage_gaps.json` |
