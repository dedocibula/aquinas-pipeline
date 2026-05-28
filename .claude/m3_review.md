# M3 — Glossary Review & Re-run Trigger

**Status:** design-intent (NOT build-locked)
**Do not implement from this file. Open decisions are marked [OPEN].**

---

## Purpose
This file exists so M1/M2 schema elements have a documented consumer.
Specifically: `glossary_sense.version`, `glossary_sense.status`,
`term_usage.sense_version_used`, `term_usage.status`, and `term_usage.confidence`
are all written in M1/M2 and consumed here.

---

## What M3 does
Takes M2's raw deduplicated term list and turns it into two things:
1. A human-reviewable surface (Google Sheets) where a theologian approves/edits terms
2. A write-back mechanism that updates the DB and triggers targeted re-translation

M3 is **not a blocking gate**. Translation (M4) may start before, during, or after
review. Review emits resolution events; M4 consumes them via the invalidation query.

---

## Consumes from M1/M2
- `glossary_sense` (status, version) — reviewer changes status to 'approved', bumps version
- `sense_rendering` (sk, human) — reviewer writes the confirmed Slovak term here
- `term_usage` (sense_version_used, confidence, status) — stale query runs against this
- M2's dedup roll-up — source for the Sheets export

---

## Intended shape

**Export to Sheets:**
One row per (term, sense). Columns: latin_lemma, context_label, proposed_slovak,
czech_anchor, english_cue, resolution_method, confidence, frequency, sample_locators.
Terms ordered by: flagged first, then by frequency descending.
Auto-resolved single-sense Krystal terms hidden by default (available in a separate tab).

**Write-back:**
When reviewer edits a row's `proposed_slovak` and marks it 'approved':
1. Update `sense_rendering(sk, human)` with the approved term
2. Update `glossary_sense.status = 'approved'`, increment `glossary_sense.version`
3. The stale query in M4 picks up the version bump and re-translates affected segments

**Re-run trigger query (to be wired in M4):**
```sql
SELECT DISTINCT segment_id FROM term_usage
WHERE sense_id = $1
  AND sense_version_used < (SELECT version FROM glossary_sense WHERE sense_id = $1);
```

---

## Open decisions
- [OPEN] Sheets sync mechanism: gspread polling vs webhook vs manual CSV round-trip
- [OPEN] Reviewer auth: who has write access to the Sheet
- [OPEN] How to present sample context to the reviewer (locator links? excerpts?)
- [OPEN] Batch re-run trigger: automatic on approval, or manual "run re-translation" button
- [OPEN] Whether M3 needs any UI beyond Sheets + a nightly sync script
