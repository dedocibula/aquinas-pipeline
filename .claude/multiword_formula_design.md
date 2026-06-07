# Multiword Formula Terms — Design Notes

## Context

During M4 analysis we identified that structural Latin phrases ("sed contra",
"respondeo dicendum", "ad primum dicendum") need consistent Slovak renderings
locked as hard constraints, just like single-word glossary terms.

The existing glossary infrastructure supports multiword terms (`glossary_term.is_multiword`,
`phrase_match()` in `resolution.py`) but the current 14 multiword entries all came from
Krystal and were seeded before M2 ran. All 58 existing formula entries are single-word
and `proposed` — none are `approved`, so none flow into `REQUIRED TERMS` today.

---

## The Core Architectural Problem

`get_locked_terms()` in `loop.py` joins through `term_usage`:

```sql
FROM term_usage tu
JOIN glossary_sense gs ON gs.sense_id = tu.sense_id AND gs.status = 'approved'
```

`term_usage` was **entirely populated by M2** (CLTK lemmatization + phrase_match on every
segment). A term added to the DB *after* M2 ran creates an approved `glossary_sense` with
**zero `term_usage` rows**. It is invisible to the translator — the join produces nothing.

The stale-detection mechanism (`sense_version_used < version`) also cannot fire on rows that
don't exist. Already-translated segments have no way to be automatically marked stale for
a term that was added after their `term_usage` map was built.

---

## Why the Simple Backfill in `import_approvals.py` Is Wrong

The obvious fix — scan all Latin segments in `import_approvals.py` and write `term_usage`
rows when a new term is approved — has two problems:

1. **Wrong concern boundary.** `import_approvals.py` handles Sheet → DB approval flow.
   Segment scanning belongs to the resolver, not the approval importer.

2. **Already-translated segments still stale.** Even if we write `term_usage` rows with
   `sense_version_used = current_version`, those segments won't be re-translated because
   the stale gate checks `sense_version_used < version` — and both sides would be equal.
   To trigger re-translation you'd need to write `sense_version_used = 0`, which is a
   separate semantic action that import_approvals shouldn't own.

---

## Correct Two-Part Solution

### Part A — Forward path: supplemental phrase-match in `loop.py`

For pending segments being translated for the first time, add a supplemental lookup
alongside `get_locked_terms()`:

```python
def _get_multiword_phrase_constraints(conn, latin: str, segment_id: int) -> list[dict]:
    """Return approved multiword terms that phrase-match this segment's Latin text.

    Writes term_usage rows lazily (sense_version_used = current version) so that
    frequency counts and stale detection work going forward.
    """
```

This queries `glossary_term WHERE is_multiword = TRUE`, runs `phrase_match()` against the
Latin text, and for any hit not already in `term_usage` for this segment:
- Appends the constraint to the translator's `REQUIRED TERMS`
- Writes a `term_usage` row with the current `sense_version_used`

`import_approvals.py` creates term + sense only — **no segment scanning, no backfill**.
`term_usage` rows accumulate organically as segments are (re-)translated.

### Part B — Backward path: targeted re-resolution for already-translated segments

When a new multiword term is approved, existing translated segments that contain the phrase
have no `term_usage` row to trigger stale detection. The correct mechanism:

Write `term_usage` rows with **`sense_version_used = 0`** for all matching segments.
Since `version = 1` (new term), `0 < 1` is true → stale detection fires →
those segments get re-queued for translation.

This belongs in a small standalone script `src/ingest/reseed_multiword_usages.py`, not
in `import_approvals.py`. It uses the existing `phrase_match()` from `resolution.py`
and is run manually after importing new multiword approvals.

---

## Required DB Migration

The `term_usage.resolution_method` CHECK constraint must be extended to include
`'human_phrase'` for rows written by the supplemental lookup and the reseed script:

```sql
-- migrations/005_human_phrase_method.sql
ALTER TABLE term_usage
    DROP CONSTRAINT term_usage_resolution_method_check;

ALTER TABLE term_usage
    ADD CONSTRAINT term_usage_resolution_method_check
    CHECK (resolution_method = ANY (ARRAY[
        'krystal_single', 'krystal_multi_voted', 'krystal_multi_flagged',
        'bahounek_derived', 'english_derived', 'model_proposed', 'human_phrase'
    ]));
```

This is a DDL change — run only after human review per CLAUDE.md.

---

## Concrete File Changes

| File | Change |
|---|---|
| `src/translate/loop.py` | Add `_get_multiword_phrase_constraints(conn, latin, segment_id)` called after `get_locked_terms()`; lazy term_usage writes |
| `src/review/import_approvals.py` | Handle blank `sense_id` rows to create new glossary_term + glossary_sense — no segment scanning |
| `src/ingest/reseed_multiword_usages.py` | New script: phrase-scans all Latin segments for a given multiword sense, writes term_usage rows with `sense_version_used=0` |
| `migrations/005_human_phrase_method.sql` | Add `'human_phrase'` to resolution_method CHECK |

---

## Reviewer Workflow (once built)

1. Reviewer adds a row to the Google Sheet Review tab:
   - Column A (approved): TRUE
   - Column C (latin_lemma): `sed contra`
   - Column D (proposed_slovak): `Avšak proti`
   - Column K (sense_id): *(blank — signals new term)*
   - Column M (db_version): *(blank)*

2. Run `uv run python -m review.import_approvals` — creates the term + sense as `approved`.

3. Run `uv run python -m ingest.reseed_multiword_usages --sense-id <new_id>` — writes
   `term_usage` rows with `sense_version_used=0` for all segments containing the phrase.
   This triggers stale detection on already-translated segments.

4. Run `uv run python -m translate.pilot` (or full corpus run) — stale segments are
   re-translated and now receive `sed contra → Avšak proti` as a hard constraint.

5. Re-export the Sheet — the new term appears with correct frequency and sense_id.

---

## Auto-Detection (Deferred)

The gap scanner (`gap_terms.py`) could be extended with a bigram scan: consecutive token
pairs where at least one token is below the `_GAP_MIN_LEN=5` gate individually but the
combined phrase is long enough and frequent enough. This would surface "sed contra",
"per se" (already in DB), "ad primum" etc. automatically for human review.

Deferred because: the reviewer already knows what formulas need locking, and a curated
human decision is more reliable than frequency-based n-gram mining for this domain.
Revisit if the reviewer workflow becomes a bottleneck.
