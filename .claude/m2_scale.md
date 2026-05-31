# M2 — Scale & Harden Ingestion

**Status:** build-locked
**Reads:** database.md, decisions.md, sources.md
**Estimate:** 3 days (allow 4 if parser hardening finds many anomalies)
**Prerequisite:** M1 complete and accepted

---

## User story
*As the engineer, I need to run the proven M1 resolution path across all 2,669
articles, fix every structural quirk that only appears at scale, and produce a
coverage report that quantifies exactly how much human review and segment
re-translation the project will require — so I can cost the rework before
any translation spend.*

## Objective
Run M1's components **unchanged in logic** over the full corpus. The work is
robustness, coverage accounting, and the corpus-wide deduplicated term roll-up.

**If this milestone requires new resolution logic, M1 was mis-scoped.**
That is an acceptance criterion, not a warning.

---

## Steps

### Step 1 — Full ingest (Latin)
Run the Latin parser over all 2,669 articles.

Do not suppress errors. Log every structural anomaly to `reports/m2_parser_anomalies.txt`:
- Missing structural elements (no sed_contra, no respondeo)
- Sub-articles or nested questions
- Unexpected XML tags or nesting
- Any locator that cannot be expressed as a valid ltree path

For each anomaly: record the locator, the anomaly type, and a brief excerpt.
After the first pass, review the anomaly list before fixing anything — categorise
anomalies by type and fix by category, not one-by-one.

Target: 100% of articles either ingest cleanly or appear in a catalogued exception
list with a stated reason for the exception.

### Step 2 — Full ingest (Bahounek)
Run the Bahounek parser over all available sections.

Log every unmatched Bahounek coordinate to `reports/m2_bahounek_gaps.txt`.
Produce a coverage map: which Latin locators have a Bahounek Czech row vs. none.
This map is used in Step 5 (rework estimate) and by the M4 translator.

### Step 3 — Full ingest (English)
Run English ingest over all articles. Use Freddoso where available,
Dominican Province elsewhere. Log coverage gaps.

### Step 4 — Full resolution
Run the M1 resolver over all segments in the DB.
Populate `term_usage` corpus-wide.

**Two-phase execution:**

**Phase 1 — Gap term pre-scan (before the main loop):**
- Scan all body segments once to collect every Latin lemma not in Krystal.
- Filter by: `freq_floor` (default 10 — must appear in ≥10 segments) and
  `pos_filter` (default {N, A} — nouns and adjectives only; lemmas with
  all-unknown POS tags are kept as benefit of doubt for medieval theological vocab).
- Batch-call DeepSeek V3 with `batch_size` lemmas per call (default 25), using
  Czech (Bahounek) and English (Dominican) excerpts as context.
  Calls are parallelised via `ThreadPoolExecutor` (`max_workers=10`).
- Pre-write `glossary_sense` + `sense_rendering(sk, model)` rows before the main loop.
- All three gap methods receive a real Slovak proposal:
  - `bahounek_derived` — Czech context available (higher quality)
  - `english_derived`  — English context only
  - `model_proposed`   — no reference context
  The method label indicates what context was available; the proposal is always
  from DeepSeek.

**Phase 2 — Main resolution loop:**
- Resolves all segments using Krystal terms + pre-written gap senses.
- No inline DeepSeek calls; gap senses are already in DB.
- `_gap_sense` uses `ON CONFLICT DO NOTHING` to preserve Phase 1 proposals.

**Knobs (configurable via env vars or `run()` params):**
  - `GAP_FREQ_FLOOR`  — min segment frequency (default 10)
  - `GAP_POS_FILTER`  — comma-separated CLTK POS codes (default "N,A"; "" = no filter)

Track and log: total API calls made, total cost incurred.

**Why ALL gap methods, not just model_proposed:**
Leaving `bahounek_derived` and `english_derived` as bracketed stubs
(`[bahounek_derived: continentia]`) would require a human reviewer to hand-write
thousands of Slovak terms in M3. The reviewer's job is to *approve or correct*
proposals, not generate them from scratch. DeepSeek proposes; humans verify.

### Step 5 — Dedup roll-up
Aggregate per-segment resolutions into a corpus-wide glossary view.

One row per (term, sense) containing:
- `latin_lemma`
- `context_label`
- `proposed_slovak` (from sense_rendering)
- `frequency` (count of segments this sense was used in)
- `resolution_method` distribution (how many via krystal_single vs flagged etc.)
- `confidence` (auto vs needs_review)
- All occurrence locators (as an array)

This is the raw, pre-review term list that M3 will export to Google Sheets.

### Step 6 — Coverage report
Produce `reports/m2_coverage.txt`. This is the key deliverable.

Contents:
```
CORPUS OVERVIEW
  Total articles:    2,669
  Total segments:    ~13,000  (estimated; print actual)
  Articles clean:    N
  Articles anomalous: N  (see m2_parser_anomalies.txt)

TERM RESOLUTION BREAKDOWN
  krystal_single:        N  (X%)  → no human review needed
  krystal_multi_voted:   N  (X%)  → no human review needed (spot-check optional)
  krystal_multi_flagged: N  (X%)  → NEEDS human review
  bahounek_derived:      N  (X%)  → NEEDS human review
  english_derived:       N  (X%)  → NEEDS human review
  model_proposed:        N  (X%)  → NEEDS human review
  ─────────────────────────────────
  Auto-resolved (no review needed): X%
  Needs human review:               X%

REVIEW SCOPE
  Unique terms needing review:  N
  (Each unique term reviewed once regardless of how many times it appears.)

RE-TRANSLATION SCOPE (if reviewer changes a term)
  Segments containing ≥1 flagged term:  N
  Estimated max re-run cost:
    N segments × avg ~400 tokens × $0.00014/1k = ~$X
  Note: each segment re-translated AT MOST ONCE regardless of how many
  of its terms were changed. Batch all term changes before re-running.

BAHOUNEK COVERAGE
  Segments with Czech reference:    N  (X%)
  Segments without Czech reference: N  (X%)

GAP TERM PROPOSALS
  Terms proposed by model: N
  API cost incurred:       ~$X
```

A reviewer reads this report and decides whether the human review burden
and re-run cost are acceptable before any translation spend.

---

## Technologies
Same as M1. DeepSeek V3 API added for gap-term proposals.
Prefect optional for run management (engineer's call); if used, add to pyproject.toml
and document the decision in decisions.md.

## Deliverables
1. Full corpus ingested into `segment` + `segment_text`
2. `reports/m2_parser_anomalies.txt` (every structural exception documented)
3. `reports/m2_bahounek_gaps.txt` (Bahounek coverage map)
4. Corpus-wide `term_usage` populated
5. Dedup roll-up available (queryable via the DB or a generated CSV)
6. `reports/m2_coverage.txt` — the key deliverable

## Acceptance criteria
- Every article either ingests cleanly or is in `m2_parser_anomalies.txt` with a reason
- Coverage report states as hard numbers: unique terms needing review,
  segments needing re-run, estimated re-run cost
- Every gap `sense_rendering(sk, model)` row contains a real Slovak proposal,
  not a bracketed stub — the reviewer corrects proposals, never fills blanks
- A reviewer can read the coverage report and make a go/no-go decision
  on translation spend without reading any code
- Total API cost for gap-term proposals is logged and within expected range (~$5–10)
