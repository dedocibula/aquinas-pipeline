# M4 — Translation Loop

**Status:** build-locked
**Reads:** database.md, decisions.md, sources.md
**Estimate:** 3 days (build + calibration on 10 articles); production run 6–10 hrs
**Prerequisite:** M2 complete; M3 export_sheet.py working (M3 review can run in parallel)

---

## User story
*As the engineer, I need a Prefect-orchestrated translation pipeline that processes
each Summa segment through a constrained translator and an axis-specific reviewer,
writes Slovak drafts to the DB, handles re-translation of stale segments when glossary
terms change, and resumes from the exact point of failure if the process crashes — so
that the full corpus is translated in one supervised overnight run with no data loss.*

## Objective
Translate all segments using locked Slovak terms as hard constraints. Run a
draft→review→revise loop per segment. Use Prefect for outer orchestration (article-level
tasks, resumable). Use plain Python for the inner loop. Re-run stale segments when M3
imports new approvals.

---

## Schema migration (run before building)
`migrations/004_translation_status.sql`:

```sql
-- Pipeline state per segment. Separate from segment_text presence checks.
ALTER TABLE segment
    ADD COLUMN translation_status text NOT NULL DEFAULT 'pending'
        CHECK (translation_status IN ('pending','translated','needs_human')),
    ADD COLUMN reviewer_notes     jsonb;

-- Index for the translation run queue
CREATE INDEX idx_segment_translation_status
    ON segment(translation_status)
    WHERE translation_status = 'pending';
```

`translation_status` values:
- `pending` — not yet translated (or marked stale by re-run engine)
- `translated` — loop completed; `segment_text(sk, model)` written
- `needs_human` — failed all 3 iterations; flagged for human review

`reviewer_notes` — advisory notes from R1 (register/minor-semantic issues).
Stored per segment so the Gate 3 theological editor can read them alongside the XLIFF.
Structure: `{"iteration": 2, "register": "phrase X is colloquial", "semantics": null}`

---

## Architecture

```
Prefect flow: translate_corpus()
│
├── @task per article (2,669 tasks, 10 concurrent)
│   │
│   └── for each segment in article (sequential within task):
│       │
│       ├── [pre-checks: structure + terminology — deterministic, no LLM]
│       │   ├── FAIL → immediate feedback to translator (no R1 call)
│       │   └── PASS ↓
│       ├── [Reviewer: DeepSeek R1]
│       │   ├── APPROVED → write segment_text(sk,model), status=translated
│       │   ├── APPROVED WITH NOTES → write + store reviewer_notes, status=translated
│       │   └── REVISION NEEDED → feedback → back to translator
│       │       [max 3 iterations total]
│       │       [on 3rd failure → write best draft, status=needs_human]
│       └── [Translator: DeepSeek V3 — called at loop start and on each revision]
│
└── @task: stale_segment_rerun()
    └── runs after translate_corpus() OR on manual trigger
        → finds segments WHERE sense_version_used < current version
        → resets their translation_status = 'pending'
        → re-runs translate_corpus() scoped to those segments only
```

---

## Prompt caching (required, not optional)

Cache prefix = everything stable across segments. Variable content always goes last.

**Translator prompt structure:**
```
[SYSTEM — mark as cache prefix]
You are translating a segment of Thomas Aquinas's Summa Theologiae
from Scholastic Latin into Slovak.

STYLE RULES (apply to all translations):
  sed_contra     → "Na druhé straně:"
  respondeo      → "Odpověď:"
  replies        → "K námitkám:"
  Bible quotes   → translate from Thomas's Latin, NOT from modern Slovak Bible
                   ("Nepřekládáme Bibli, ale TA")
  Name forms     → Dionýsios (NOT Diviš), Augustin, Boethius, Řehoř, Athanasios
  Orthography    → filosofia, teológia, -izmus
  Numbering      → number objections and replies; drop "praeterea"/"ad primum"

NEGATIVE CONSTRAINTS (apply to all translations):
  - Do NOT improve literary quality
  - Preserve Aquinas's word repetition — it is intentional and load-bearing
  - Preserve scholastic particles: totiž, teda, však, odtiaľ, ale
  - Do not merge or reorganise sentences for readability
  - Translate FROM the Latin — Czech and English are references only

[END CACHE PREFIX]

[USER — variable, NOT cached]
HARD TERM CONSTRAINTS (apply exactly, no exceptions):
  concupiscentia → dychtenie
  gratia → milosť
  [... locked terms for this segment from term_usage + sense_rendering(sk) ...]

CZECH REFERENCE (draft only, not authoritative for terms):
  [segment_text(cs, bahounek) content]

ENGLISH REFERENCE (semantic anchor):
  [segment_text(en, dominican|freddoso) content]

Translate this Latin segment:
[segment_text(la, corpus_thomisticum) content]
```

On **revision** — append before the Latin segment:
```
PRIOR DRAFT:
[previous Slovak output]

REVIEWER FEEDBACK — address each point specifically:
[reviewer bullet list]
```

**Reviewer prompt structure:**
```
[SYSTEM — mark as cache prefix]
You are a quality reviewer for a Slovak translation of Thomas Aquinas's Summa Theologiae.

Evaluate the draft against four axes. For each axis: PASS or FAIL + one specific,
actionable reason (one sentence). Then give a verdict.

AXIS 1 — STRUCTURE (deterministic pre-check; do not re-evaluate if pre-check ran)
  Count: do objection paragraphs match the Latin count?
  Check: sed_contra, respondeo, replies all present?
  A missing element is always FAIL regardless of prose quality.

AXIS 2 — TERMINOLOGY (deterministic pre-check; do not re-evaluate if pre-check ran)
  Each locked term must appear verbatim in the draft.
  Any missing or substituted locked term is always FAIL.

AXIS 3 — SEMANTICS
  Does the Slovak accurately convey the logical argument?
  Focus on:
  - Causal connectives: quia/totiž, enim/totiž, ergo/teda — direction matters
  - Modal distinctions: possibile/možné, necessarium/nutné, impossibile/nemožné
  - Conditional vs categorical: 'si... tunc' vs a categorical claim
  - Argument direction: does the conclusion follow from the stated premises?
  Severity: MAJOR if the argument's meaning changes; MINOR if imprecision only.
  MAJOR → FAIL. MINOR → note it but do not FAIL.

AXIS 4 — REGISTER
  Is the Slovak in Scholastic theological register?
  Flag: colloquialisms, modern idioms, sentences restructured for readability,
  literary flourishes that smooth Aquinas's deliberate dryness.
  Register issues → note only, never FAIL (handled in M5 polish pass).

VERDICT must be exactly one of:
  APPROVED
  APPROVED WITH NOTES: [bullet list of advisory items — minor semantic + register]
  REVISION NEEDED: [bullet list of specific required changes — structure/terminology/major semantic only]

[END CACHE PREFIX]

[USER — variable, NOT cached]
LOCKED TERM REQUIREMENTS:
  [same term list as translator]

LATIN ORIGINAL:
[latin text]

SLOVAK DRAFT:
[draft text]
```

**Caching implementation:**
Both DeepSeek V3 and R1 support prefix caching. Mark the system prompt section
with the provider's cache-control header/parameter. The system prompt is ~800–1,200
tokens; at 90% discount on cached tokens, this saves ~$0.10–0.20/1k segments.
Lock this in the API call, not as an afterthought.

---

## The loop — plain Python

```python
MAX_ITERATIONS = 3

def translate_segment(segment_id: int, db: Repository) -> str:
    seg = db.get_segment_with_texts(segment_id)      # v_segment
    constraints = db.get_locked_terms(segment_id)    # term_usage → sense_rendering(sk)

    prior_draft = None
    prior_feedback = None
    best_draft = None

    for iteration in range(1, MAX_ITERATIONS + 1):
        # 1. Translate
        draft = call_translator_v3(seg, constraints, prior_draft, prior_feedback)

        # 2. Pre-checks (no LLM)
        structure_result  = check_structure(seg.latin, draft)
        terminology_result = check_terminology(draft, constraints)

        if structure_result.failed or terminology_result.failed:
            feedback = build_precheck_feedback(structure_result, terminology_result)
            prior_draft    = draft
            prior_feedback = feedback
            if best_draft is None:
                best_draft = draft
            continue   # re-translate without calling R1

        # 3. Reviewer (R1) — only if pre-checks pass
        review = call_reviewer_r1(seg.latin, draft, constraints)

        if review.verdict == 'APPROVED':
            db.write_segment_text(segment_id, lang='sk', source='model', content=draft)
            db.update_translation_status(segment_id, 'translated')
            return 'translated'

        if review.verdict == 'APPROVED_WITH_NOTES':
            db.write_segment_text(segment_id, lang='sk', source='model', content=draft)
            db.update_translation_status(segment_id, 'translated')
            db.write_reviewer_notes(segment_id, review.notes, iteration)
            return 'translated_with_notes'

        # REVISION NEEDED
        best_draft     = draft
        prior_draft    = draft
        prior_feedback = review.feedback

    # Exhausted iterations — write best draft, flag for human
    db.write_segment_text(segment_id, lang='sk', source='model', content=best_draft)
    db.update_translation_status(segment_id, 'needs_human')
    log.warning(f"Segment {segment_id} needs human review after {MAX_ITERATIONS} iterations")
    return 'needs_human'
```

---

## Pre-checks (deterministic, no LLM)

```python
def check_structure(latin: str, draft: str) -> CheckResult:
    # Count objection paragraphs in Latin (arg count from segment.reply_to or latin text)
    # Count corresponding paragraphs in draft
    # Check sed_contra marker present ("Na druhé straně" or equivalent)
    # Check respondeo marker present ("Odpoveď" or equivalent)
    # For reply segments: check "K námitkám" marker
    # FAIL if any count mismatch or marker missing

def check_terminology(draft: str, constraints: list[TermConstraint]) -> CheckResult:
    # For each (latin_lemma, required_slovak) pair:
    #   Check required_slovak appears in draft (case-insensitive, diacritic-normalised)
    # FAIL if any required term missing
    # Report which terms are missing
```

Fail loudly: log the segment_id, the specific failure, and the draft text.
Do not silently pass a segment that failed pre-checks.

---

## Prefect orchestration

```python
from prefect import flow, task
from concurrent.futures import ThreadPoolExecutor

@task(retries=3, retry_delay_seconds=30, name="translate-article")
def translate_article(work_id: int, locator_prefix: str, db_url: str):
    """One Prefect task = one article. Retried on API failure, not on loop failure."""
    db = Repository(db_url)
    segments = db.get_pending_segments_for_article(locator_prefix)
    for seg_id in segments:
        translate_segment(seg_id, db)

@flow(name="translate-corpus")
def translate_corpus(work_id: int = 1, max_workers: int = 10):
    articles = db.get_article_locators(work_id)  # list of ltree prefixes e.g. 'I.q3.a1'

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(translate_article, work_id, loc, DB_URL)
            for loc in articles
        ]
        for f in futures:
            f.result()

@flow(name="rerun-stale")
def rerun_stale_segments(work_id: int = 1):
    """Run after import_approvals.py bumps sense versions."""
    stale = db.get_stale_segments(work_id)   # sense_version_used < current version
    db.reset_translation_status(stale, 'pending')
    log.info(f"Reset {len(stale)} stale segments to pending")
    translate_corpus(work_id)
```

**Running:**
```bash
# Full translation run
uv run prefect run translate.py translate-corpus

# Re-run stale segments after import_approvals.py
uv run prefect run translate.py rerun-stale

# Resume after crash (Prefect handles this automatically via task checkpointing)
# Just re-run the same command — completed tasks are skipped
uv run prefect run translate.py translate-corpus
```

`translate_article` retries on API errors (rate limits, timeouts) — `retries=3`.
It does NOT retry on loop failure (segment gets `needs_human` status) — that is
intentional and not an API error.

---

## Stale segment re-run

The re-run query (also in database.md):
```sql
-- Used by rerun_stale_segments() to find what needs re-translating
SELECT DISTINCT tu.segment_id
FROM term_usage tu
JOIN glossary_sense gs ON tu.sense_id = gs.sense_id
WHERE tu.sense_version_used < gs.version;
```

After re-translation, update `term_usage.sense_version_used = gs.version` for all
`term_usage` rows in that segment. This marks the segment as no longer stale.

**Cost control:** run `rerun-stale` once per approval batch, not per approval.
Batch all M3 approvals for a session first, then trigger one re-run. Each segment
is re-translated at most once per batch regardless of how many of its terms changed.

---

## Parallelism and cost

- 10 concurrent article tasks (configurable via `MAX_WORKERS` env var)
- ~2,669 articles × ~5 segments = ~13,345 segments
- Estimated wall time: 6–10 hours at 10 workers
- Expected cost (DeepSeek V3 + R1 with prompt caching): ~$60–90 total

Before the full run, do a **pilot run** on 50 articles:
```bash
uv run python -m translate.run --pilot 50
```
Print per-article cost, average iteration count, `needs_human` rate.
Adjust `MAX_WORKERS` and verify caching is firing before committing to the full corpus.

---

## Technologies
Python 3.12 + uv · psycopg2-binary · prefect · requests (DeepSeek API calls)

New in `pyproject.toml`:
```toml
prefect = ">=3.0"
```

DeepSeek V3 and R1 via direct API (OpenAI-compatible endpoint). No LangChain.
No LangGraph. Do not add either as a dependency.

---

## Deliverables
1. `migrations/004_translation_status.sql` — reviewed and approved before any translation
2. `src/translate/translator.py` — V3 call with cached system prompt
3. `src/translate/reviewer.py` — R1 call with cached rubric; structured verdict parsing
4. `src/translate/prechecks.py` — structure + terminology checks
5. `src/translate/loop.py` — `translate_segment()` function
6. `src/translate/run.py` — Prefect flows (`translate_corpus`, `rerun_stale`)
7. `reports/m4_pilot.txt` — output of 50-article pilot (cost, iteration distribution, needs_human count)
8. `reports/m4_production.txt` — output of full corpus run

## Acceptance criteria
- Migration runs cleanly; `translation_status` and `reviewer_notes` columns exist
- Pilot (50 articles): `needs_human` rate < 10%; average iterations < 2.0; prompt
  caching confirmed firing (logged cache hit rate)
- Pre-checks fire before R1: a draft with a missing objection never triggers an R1 call
- A segment that fails all 3 iterations: `translation_status='needs_human'`, best draft
  written to `segment_text(sk, model)`, human-readable log entry
- Prefect crash recovery: kill the process mid-run, restart, confirm it resumes at
  the next incomplete article (not from the beginning)
- `rerun_stale()`: after bumping a sense version manually, only stale segments are
  re-translated; `sense_version_used` is updated after re-run
- Full corpus run completes with `translation_status` in ('translated','needs_human')
  for every segment; no segment left 'pending'
- Total API cost logged in `m4_production.txt` and within expected range (~$60–90)