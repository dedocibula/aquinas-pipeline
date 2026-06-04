# M5 — Full Translation, Polish & Export

**Status:** build-locked (Steps 1–2 locked; Steps 3–4 design-intent)
**Reads:** database.md, decisions.md, sources.md
**Estimate:** Step 1: 6–10 hrs compute + 1 day build; Steps 2–4: 2 days
**Prerequisite:** M4 Gate 1 passed (pilot quality acceptable)

---

## User story
*As the engineer, I need to translate the full Summa corpus under Prefect orchestration,
optionally polish the output, verify consistency, and export a standard XLIFF file for
the theological editor — so that the complete Slovak translation is delivered as a
portable, professionally reviewable document.*

## Objective
Four sequential steps. Step 1 is build-locked and should be built immediately after
Gate 1. Steps 2–4 are design-intent; build them after Step 1 completes and the output
is reviewed. Do not build Steps 2–4 speculatively.

---

## Step 1 — Full corpus translation (build-locked)

Absorbs the M4 translation loop at scale. Same loop logic, same models, same prompts.
New: Prefect orchestration for durability, the stale-segment re-run engine, and the
`needs_human` triage report.

### Prefect flow

```python
from prefect import flow, task
from concurrent.futures import ThreadPoolExecutor

MAX_WORKERS = int(os.getenv('MAX_WORKERS', 10))

@task(retries=3, retry_delay_seconds=30, name="translate-article")
def translate_article(locator_prefix: str, db_url: str):
    """One Prefect task = one article. Retried on API failure, not on loop failure."""
    db = Repository(db_url)
    segments = db.get_pending_segments_for_article(locator_prefix)
    for seg_id in segments:
        translate_segment(seg_id, db)   # same function as M4

@flow(name="translate-corpus")
def translate_corpus(work_id: int = 1):
    articles = db.get_all_article_locators(work_id)
    pending  = [a for a in articles if db.has_pending_segments(a)]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(translate_article, loc, DB_URL) for loc in pending]
        for f in futures:
            f.result()

@flow(name="rerun-stale")
def rerun_stale(work_id: int = 1):
    """
    Run after import_approvals.py bumps sense versions.
    Resets stale segments to pending; re-runs translate_corpus.
    """
    stale = db.get_stale_segments(work_id)
    if not stale:
        log.info("No stale segments.")
        return
    db.reset_translation_status(stale, 'pending')
    log.info(f"Reset {len(stale)} stale segments → re-translating")
    translate_corpus(work_id)
```

**Running:**
```bash
# Full corpus run
uv run prefect run src/translate/run.py:translate_corpus

# After M3 import_approvals.py bumps versions
uv run prefect run src/translate/run.py:rerun_stale

# Crash recovery: just re-run — Prefect skips completed tasks automatically
uv run prefect run src/translate/run.py:translate_corpus
```

**Stale segment query (re-run engine):**
```sql
SELECT DISTINCT tu.segment_id
FROM term_usage tu
JOIN glossary_sense gs ON tu.sense_id = gs.sense_id
WHERE tu.sense_version_used < gs.version;
```
After re-translation: update `term_usage.sense_version_used = gs.version` for all
rows in that segment. Each segment re-translated at most once per batch.

### Pre-run checklist
Before launching the full corpus run:
- [ ] All M3 approvals imported (`import_approvals.py` run, 0 conflicts)
- [ ] `rerun_stale` run to clear any stale segments from M4 pilot
- [ ] `pilot.txt` reviewed; Gate 1 passed
- [ ] Prompt caching confirmed firing (check M4 pilot logs)
- [ ] `MAX_WORKERS` set (default 10; increase if rate limits allow)
- [ ] Estimated cost reviewed: ~$60–90 for full corpus

### Production run report
`reports/m5_production.txt`:
```
FULL CORPUS RUN SUMMARY
  Total segments:    N
  Translated:        N  (X%)
  Needs human:       N  (X%)  ← segments for triage, see below
  Avg iterations:    N.N
  Cache hit rate:    X%
  API cost:          ~$X
  Wall time:         Xh Xm
```

### Needs-human triage
After the run, produce `reports/m5_needs_human.txt` listing every flagged segment
with its locator, last reviewer feedback, and iteration count. This goes to the
theological editor alongside the XLIFF — they see exactly why each segment was
flagged and what the best attempt was.

### Step 1 acceptance criteria
- Every segment `translation_status` in ('translated', 'needs_human'); none 'pending'
- Prefect crash recovery verified: kill mid-run, restart, confirm resume at next incomplete article
- `rerun_stale` verified: bump a sense version manually, run rerun_stale, confirm only stale segments re-translated
- `sense_version_used` updated after re-run (stale query returns empty afterwards)
- `needs_human` rate < 10% of corpus
- Total cost logged and within ~$60–90

---

## Step 2 — Polish pass (design-intent)

**Build only after Step 1 is complete and the output is read in the preview server.**

Intended approach: Claude Sonnet via Anthropic Message Batches API (50% discount,
24-hour turnaround). Apply to `translated` segments only; `needs_human` segments go
to the theological editor unpolished.

Critical constraints from `style_profile.yaml` must be injected:
- Do NOT increase literary quality
- Preserve Aquinas's word repetition
- Preserve scholastic particles (totiž, teda, však, odtiaľ, ale)
- Preserve sentence boundaries
- Do not merge or restructure

**Open decision:** apply to all segments, or only structural framing elements
(sed_contra, preamble, question_title), or skip entirely? Test on 50 segments first.
If polish makes the technical body (respondeo, replies) worse (smoother but less
precise), skip it for those element types. The decision must be empirical, not assumed.

---

## Step 3 — Consistency report (design-intent)

Finds terms rendered inconsistently across the corpus — the post-hoc replacement for
upfront vector discovery.

```sql
SELECT
    gt.latin_lemma,
    array_agg(DISTINCT sr.content ORDER BY sr.content) AS slovak_variants,
    count(DISTINCT sr.content)                          AS variant_count,
    count(*)                                            AS total_occurrences
FROM term_usage tu
JOIN glossary_term gt   ON tu.term_id  = gt.term_id
JOIN sense_rendering sr ON tu.sense_id = sr.sense_id
    AND sr.lang = 'sk'
    AND sr.source_id = (SELECT source_id FROM source WHERE code = 'human'
                        UNION ALL
                        SELECT source_id FROM source WHERE code = 'model'
                        LIMIT 1)
GROUP BY gt.term_id, gt.latin_lemma
HAVING count(DISTINCT sr.content) > 1
ORDER BY variant_count DESC, total_occurrences DESC;
```

Terms with `variant_count > 1` are glossary-expansion candidates for v2.
Export as `reports/m5_consistency.csv`. Flag any with frequency > 50 and
`variant_count > 2` for immediate review — those are the systematic drift cases.

---

## Step 4 — XLIFF export (design-intent)

Standard XLIFF 2.0 output per tractate (one file per Prima Pars, Prima-Secundae, etc.).
Readable in Crowdin, Lokalise, OmegaT. Human theological editor reads, corrects,
approves. Corrections written back as `segment_text(sk, human)` with a version bump
(triggers the re-run engine if corrections change locked terms).

Include in each XLIFF unit:
- Source (Latin), target (best Slovak available)
- `translation_status` as a note
- `reviewer_notes` as a note (so editor sees why a segment was flagged)

**Open decisions:**
- XLIFF chunking: one file per tractate (recommended) or per question range?
- Which Slovak source to export: prefer human over model where available?
- Feedback loop: how Gate 3 corrections trigger version bumps?

---

## Technologies

Step 1: Python 3.12 + uv · prefect >= 3.0 · psycopg2-binary
Step 2: anthropic SDK (Batch API) — add to pyproject.toml at Step 2
Step 3: psycopg2-binary (query only)
Step 4: lxml (XLIFF generation)

---

## Deliverables

**Step 1 (build-locked):**
1. `src/translate/run.py` — Prefect flows (`translate_corpus`, `rerun_stale`)
2. `reports/m5_production.txt` — full corpus run summary
3. `reports/m5_needs_human.txt` — flagged segments with triage info

**Steps 2–4 (build after Step 1 review):**
4. `src/polish/batch_polish.py` — Claude Sonnet Batch API submission + polling
5. `reports/m5_consistency.csv` — inconsistency report
6. `src/export/xliff.py` — XLIFF 2.0 generator
7. `exports/` — XLIFF files per tractate

## Step 1 acceptance criteria (the gate before Steps 2–4)
- Every segment has a `translation_status` of 'translated' or 'needs_human'
- Prefect crash recovery verified
- `rerun_stale` works correctly end-to-end
- Full corpus readable in the M4 preview server before proceeding
- Cost and quality within accepted bounds from pre-run checklist