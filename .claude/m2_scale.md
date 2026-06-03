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
- **Mechanical filter only — no POS filter, no static word lists.** Keep lemmas that
  pass `freq_floor` (default 10 — ≥10 segments) and a length gate (`len > min_len`,
  default 5, which drops most Latin function words); strip CLTK's numeric lemma suffix
  (`dico2` → `dico`). That is the whole pre-filter. Precision is **not** decided in code.
- Batch-call DeepSeek V3 with `batch_size` lemmas per call (default 50), Czech (Bahounek)
  and English (Dominican) excerpts as context, parallelised via `ThreadPoolExecutor`
  (`max_workers=10`). Each call does **two** things per lemma in one shot:
  - **classify** — assign a `category`: `term` (theological/philosophical content),
    `name` (proper noun, e.g. Christus/Augustinus/philosophus=Aristotle),
    `formula` (recurring structural connective, e.g. Praeterea/Respondeo/Videtur —
    **kept**, because consistent rendering of the Summa's formulaic scaffolding matters),
    `prose` (ordinary verb/quantifier/function word).
  - **translate** — the Slovak rendering of this lemma.
- **No canonicalization step.** CLTK's output (after numeric suffix strip) is the
  `glossary_term.latin_lemma` key. `divina`, `divino`, `divinus` each get their own row
  with the same Slovak rendering. This eliminates the ephemeral `canonical_map` problem
  and makes M4 lookup trivial: CLTK surface → lemma → direct `glossary_term` hit.
  The ~660 "imperfect" CLTK forms are an acceptable trade-off (~19% near-duplicates).
- Categories are stored on `glossary_term.category` (migration `003_term_category.sql`)
  and are **fully overridable in M3** — terminology decisions live in the DB, never in
  code, consistent with Principle 1 (model proposes/triages; humans decide terminology).
- Pre-write one `glossary_term(category)` + `glossary_sense` +
  `sense_rendering(sk, model)` per CLTK lemma. The resolution **method** label
  (`bahounek_derived` / `english_derived` / `model_proposed`) still records what context
  was available; the proposal itself is always from DeepSeek.
- **Idempotent retry:** each successful batch is written to DB immediately on completion.
  Re-runs skip lemmas already in DB and only call DeepSeek for the remainder.

**Phase 2 — Main resolution loop:**
- Resolves all segments using Krystal terms + the pre-written gap terms.
- No inline DeepSeek calls; gap senses are already in the DB.
- **No-stub invariant:** a gap lemma becomes a `term_usage` row **only if** its CLTK lemma
  (after suffix strip) is present in `gap_terms_db` — i.e. it received a Phase-1 proposal.
  Non-qualifying lemmas create no `term_usage` row and no bracketed stub — the set of terms
  translated equals the set surfaced for review.

**Knobs (configurable via env vars or `run()` params):**
  - `GAP_FREQ_FLOOR`  — min segment frequency (default 10)
  - `GAP_BATCH_SIZE`  — lemmas per DeepSeek call (default 25)
  - `GAP_MAX_WORKERS` — concurrent batch requests (default 10)

Track and log: total API calls made, total cost incurred. Use the pilot to size batches
before the full run: `uv run python -m ingest.pipeline --pilot N --batch-sizes 25,50`
(prints per-batch-size cost and the category distribution).

**Why dynamic categorization, not a static blocklist:**
A hardcoded Latin blocklist is brittle, bakes Summa assumptions into Python, and — fatally —
gives no per-term place to adjust a word's meaning later. Instead every recurring lemma is
categorized, translated, and stored; nothing is hard-dropped, and the reviewer reorders or
re-categorizes anything in M3. DeepSeek proposes and triages; humans verify.

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
  not a bracketed stub — enforced by a loud guardrail that fails the report if any
  `proposed_slovak` in the dedup roll-up begins with `[` (the reviewer corrects
  proposals, never fills blanks)
- Every gap term carries a model `category` (term/name/formula/prose) on
  `glossary_term.category`, overridable in M3
- Each CLTK lemma is its own `glossary_term` row; `divina`, `divino`, `divinus` each have
  a proposed Slovak rendering (same translation, separate rows — acceptable duplication)
- A reviewer can read the coverage report and make a go/no-go decision
  on translation spend without reading any code
- Total API cost for gap-term proposals is logged and within expected range (~$0.10–$1.00)
