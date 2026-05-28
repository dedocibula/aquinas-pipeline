# M4 — Translation Loop

**Status:** design-intent (NOT build-locked)
**Do not implement from this file. Open decisions are marked [OPEN].**

---

## Purpose
Documents the downstream consumers of every M1/M2 schema element so the build
agent does not strip "unused" infrastructure during M1/M2 development.

---

## What M4 does
Takes segments from `v_segment`, injects locked Slovak terms as hard constraints,
generates Slovak prose, and writes drafts back to `segment_text(sk, model)`.
Also executes the re-run engine: when M3 bumps a sense version, re-translates
only the stale segments.

---

## Consumes from M1/M2
- `v_segment` — Latin source + Czech/English references per segment
- `term_usage` → `sense_rendering(sk)` — the hard term constraints to inject
- `term_usage.sense_version_used` — compared against `glossary_sense.version`
  to identify stale segments for re-translation
- `source.authority_rank` — determines reference hierarchy in the prompt
- `style_profile.yaml` — Krystal house rules injected into every prompt

---

## Intended shape

**Per-segment translation:**
1. Fetch segment via `v_segment` (Latin, Czech, English references)
2. Fetch locked terms: `term_usage WHERE segment_id=X AND status='confirmed'` →
   join to `sense_rendering(sk)` for the approved Slovak term
3. Build prompt:
   - Latin source text to translate
   - Czech reference labelled by authority ('draft Czech reference' for Bahounek)
   - English reference as semantic anchor
   - Hard glossary constraints: "render X as Y" for each locked term
   - `style_profile.yaml` rules (headings, citations, name forms, orthography)
   - Negative constraints: do not improve literary quality; preserve repetition;
     preserve scholastic particles; preserve sentence boundaries
4. Call draft model → write `segment_text(sk, model)`

**Re-run engine:**
```sql
-- Find stale segments after a sense version bump:
SELECT DISTINCT tu.segment_id
FROM term_usage tu
JOIN glossary_sense gs ON tu.sense_id = gs.sense_id
WHERE tu.sense_version_used < gs.version;
```
Mark those segments for re-translation. Each segment re-translated at most once
per batch regardless of how many of its terms changed. Update `sense_version_used`
after re-translation.

**Multi-sense placeholder rule:**
First-pass translation uses the best-guess sense (highest-evidence voted sense,
or default if flagged). The segment is always marked for re-run when the sense
is confirmed by a reviewer — even if the guess was correct. This ensures no
unreviewed multi-sense resolution survives in the final translation.

---

## Open decisions
- [OPEN] Draft model: DeepSeek V3 (current plan) vs alternatives
- [OPEN] Reviewer agent: rubric design (4-axis: structure, terminology, semantics, register)
  and approval thresholds; whether the loop is plain-Python while vs LangGraph
- [OPEN] Loop structure: draft → review → revise (max 3 iterations, escape hatch)
- [OPEN] Whether re-run segments need the polish pass again (probably not)
- [OPEN] Parallelism: how many concurrent segments during the full corpus run
- [OPEN] Orchestration: Prefect wrapping the loop for durability
