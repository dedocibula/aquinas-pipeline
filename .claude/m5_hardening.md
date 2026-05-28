# M5 — Polish, Orchestration & Consistency

**Status:** design-intent (NOT build-locked)
**Do not implement from this file. Open decisions are marked [OPEN].**

---

## Purpose
The final production milestone. Documents what consumes the completed translation
and where the pipeline becomes a durable, resumable production job.

---

## What M5 does
1. Optional constrained polish pass (Claude Sonnet via Anthropic Batch API)
2. Durable orchestration wrapping the full pipeline (Prefect)
3. Post-hoc consistency report (replaces deferred vector discovery)
4. XLIFF export for human theological editor (Gate 3)

---

## Consumes from M1/M2/M4
- `segment_text(sk, model)` — the completed first-pass translations
- `term_usage` — to know which segments contain which terms (consistency check)
- `glossary_sense.version` — to confirm all re-runs are complete before final export

---

## Intended shape

**Polish pass (constrained, possibly selective):**
Anthropic Batch API (50% cost reduction, 24-hour turnaround).
Negative constraints (from style_profile.yaml) applied strictly:
- Do NOT increase literary quality
- Preserve repetition (Aquinas's repetition is semantic, not stylistic)
- Preserve scholastic particles (totiž, teda, však, odtiaľ, ale)
- Preserve sentence boundaries (do not merge two short sentences)
Decision on scope: apply to all segments, or framing elements only (sed_contra,
prologues), or skip entirely — to be determined empirically at Gate 1.

**Consistency report (post-hoc, replaces vector discovery):**
```sql
-- Find terms rendered inconsistently across the corpus:
SELECT tu.term_id, gt.latin_lemma,
       array_agg(DISTINCT sr.content) AS slovak_variants,
       count(DISTINCT sr.content)     AS variant_count
FROM term_usage tu
JOIN glossary_term gt         ON tu.term_id = gt.term_id
JOIN segment_text st          ON tu.segment_id = st.segment_id AND st.lang = 'sk'
JOIN sense_rendering sr       ON tu.sense_id = sr.sense_id AND sr.lang = 'sk'
GROUP BY tu.term_id, gt.latin_lemma
HAVING count(DISTINCT sr.content) > 1
ORDER BY variant_count DESC;
```
Terms appearing with >1 Slovak variant = glossary-expansion candidates for v2.
This is the evidence-driven version of what upfront vector discovery would have
tried to predict.

**XLIFF export:**
Standard XLIFF 2.0 file per tractate or question range, readable in Crowdin/Lokalise.
Human theological editor reads, corrects, approves. Corrections written back as
`segment_text(sk, human)`.

---

## Open decisions
- [OPEN] Whether the polish pass is applied at all / selectively — test at Gate 1
- [OPEN] Prefect flow boundaries and retry configuration
- [OPEN] XLIFF file chunking (per tractate? per question range?)
- [OPEN] Consistency threshold: how many variants before a term is flagged for glossary lock
- [OPEN] Feedback loop: how Gate 3 corrections are written back and trigger version bumps
