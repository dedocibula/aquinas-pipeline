# M1 — Resolution Core

**Status:** build-locked
**Reads:** database.md, decisions.md, sources.md
**Estimate:** 2 days
**Prerequisite:** M0 complete (all sources on disk, DB running)

---

## User story
*As the engineer, I need a proven, reproducible mechanism that takes a Summa
article in Latin, locates its known theological terms, resolves each to a
pre-approved Slovak rendering using Krystal-glossary authority with Czech/English
evidence for ambiguous ones, and records exactly how each was resolved — so that
before scaling to the full corpus I have hard, auditable evidence the resolution
path is correct on real text.*

## Objective
Build the schema and the term-resolution engine. Prove both end-to-end on a
10-article test set. This is the novel, risky core — everything downstream
depends on it. Success = a provenance report a non-engineer can read.

---

## Locked scope

- Summa-specific build. Two generality seams only: `ltree` locator path; `source.authority_rank`.
- Term authority: Krystal (single→silent; multi→evidence-vote or flag) → Bahounek-derived →
  English-derived → model-proposed. Krystal always wins where it has an entry.
- Ambiguous resolution = weighted evidence vote. Auto-resolve only when signals are
  consistent AND ≥1 strong (Krystal-derived) signal is present. Never bare-majority
  auto-resolve on weak signals. Never silently flatten a multi-sense term.
- Provenance recorded on every resolution: method, signals, confidence.
- **No vectors. No LangGraph.**

---

## Test set (10 articles)
Spread to exercise structural variety and multi-sense terms:

| locator | why |
|---|---|
| I.q3.a1 | short, simple structure |
| I.q13.a5 | contains multi-sense terms (ratio, species) |
| I-II.q5.a1 | Prima-Secundae structure |
| I-II.q94.a2 | contains lex, ratio, virtus |
| II-II.q23.a1 | contains gratia, fides, caritas |
| II-II.q64.a7 | contains actus, intentio |
| III.q1.a1 | Tertia Pars structure |
| III.q75.a4 | contains substantia, forma, species |
| + 1 deliberately short article | edge case: minimal structure |
| + 1 deliberately long article | edge case: many objections |

---

## Steps

### Step 1 — Schema (STOP: human review before step 2)
Produce `migrations/001_initial.sql` containing:
- `CREATE EXTENSION IF NOT EXISTS ltree;`
- `CREATE EXTENSION IF NOT EXISTS vector;`
- All table DDL from `database.md` in dependency order
- All indexes and CHECK constraints
- Both views (`v_segment`, `v_sense`)
- The `source` seed data

Do not run the migration. Show the complete SQL and wait for approval.

### Step 2 — Krystal preseed
Parse `sources/czech/krystal/Teologicka__Suma_u_zus_-_verze_4.docx` using
`python-docx`. Extract:

1. Term pairs → `glossary_term` + `glossary_sense` + `sense_rendering`
   - Single-sense term → one `glossary_sense` row (context_label=NULL),
     four `sense_rendering` rows (la, cs, en, sk)
   - Multi-sense term → one `glossary_term` row + N `glossary_sense` rows,
     each with a distinct context_label and its own cs/en/sk renderings
   - Known multi-sense terms: concupiscentia, gratia, fides, intellectus,
     providentia, ratio, passio, forma, virtus, bonum, actus, potentia,
     species, intentio, sensus

2. Style rules → `style_profile.yaml`
   Extract heading templates, citation rules, name forms, orthography rules.
   Do not lose the "Nepřekládáme Bibli, ale TA" rule.

Show the extracted data (term count, sense count, multi-sense terms found)
for review before inserting.

### Step 3 — Latin parser
Parse Corpus Thomisticum XML for the 10 test articles only.

Output per article: five `segment` rows with:
- `locator_path` as `ltree` (e.g. `I.q3.a1.arg1`, `I.q3.a1.sed_contra`)
- `element_type` correctly classified
- `reply_to` populated for reply elements (reply 1 → segment_id of arg 1)
- Corresponding `segment_text(la, corpus_thomisticum)` rows

**Fail loudly:** if an article is missing an expected structural element,
crash with the locator and the anomaly. Do not skip silently.

Verify: all 10 articles produce exactly the expected number of segments.
Log the count per article before inserting.

### Step 4 — Bahounek parser
Parse the Bahounek HTML for the same 10 articles using its native coordinate tags.

Coordinate tag format: `I ot. N čl. N arg. N` → map to ltree `I.qN.aN.argN`.
Confirm the mapping logic is correct on at least 5 articles by manual spot-check.

Output: `segment_text(cs, bahounek)` rows matched to existing segment locators.

**Fail loudly:** if a Bahounek coordinate cannot be matched to an existing
Latin segment, log the unmatchable coordinate and crash. Do not silently drop it.

### Step 5 — English ingest
Attach English reference text for the same 10 articles.

Use Freddoso where available; fall back to Dominican Province.
Record the actual source used via `source_id` (do not mix them into one row).

Output: `segment_text(en, freddoso|dominican)` rows.

### Step 6 — Lemmatizers
Implement two lemmatization functions and verify them before the resolver runs:

```python
def lemmatize_latin(surface: str) -> list[str]:
    # CLTK; returns list of candidate lemmas for a surface form
    # Test: lemmatize_latin('essentiam') should return ['essentia']

def lemmatize_czech(surface: str) -> list[str]:
    # MorphoDiTa; returns list of candidate lemmas
    # Test: lemmatize_czech('dychtění') should return ['dychtění']
    # Test: lemmatize_czech('dychtěním') should return ['dychtění']
```

Write unit tests for both. Run them before touching the resolver.

### Step 7 — Resolver
Core logic. Process each segment in the test set.

**Order of operations (strict):**

1. **Phrase-match multiword terms first.**
   Scan `latin_text` for all `glossary_term` entries where `is_multiword=true`.
   Match as substrings after normalizing whitespace. Record matches.
   Remove matched spans from further single-token processing.

2. **Lemmatize remaining tokens.**
   Run CLTK on unmatched Latin tokens. For each lemma, look up `glossary_term`.

3. **For each matched term, resolve sense:**

   **Single-sense (one `glossary_sense` row):**
   → `resolution_method='krystal_single'`, `confidence='auto'`, `status='guessed'`
   → Write `term_usage` row.

   **Multi-sense (N `glossary_sense` rows):**
   → Gather evidence:
     - Czech signal: lemmatize the segment's `cs` text (MorphoDiTa);
       check if any result matches a sense's `sense_rendering.lemma` where lang='cs'
     - English signal: check if segment's `en` text contains a sense's `english_cue`
     - Look up signal sources via `source.authority_rank` for weighting
   → If signals are consistent AND ≥1 is from a source with rank ≤ 20:
     → `resolution_method='krystal_multi_voted'`, `confidence='auto'`, `status='guessed'`
   → Otherwise:
     → `resolution_method='krystal_multi_flagged'`, `confidence='needs_review'`, `status='guessed'`
   → Write `term_usage` row with `signals` JSONB.

   **Not in Krystal:**
   → If Bahounek Czech is available: derive Slovak proposal from the lemmatized Czech token.
     `resolution_method='bahounek_derived'`, `confidence='needs_review'`, `status='guessed'`
   → Else if English is available: derive from English.
     `resolution_method='english_derived'`, `confidence='needs_review'`, `status='guessed'`
   → Else: stub `model_proposed` (LLM call deferred; write a placeholder for now).
   → In all gap cases: create a `glossary_sense` row with `status='proposed'`
     and a `sense_rendering(sk)` row with the proposed term.

4. **Write `term_usage` rows** with full provenance for all resolved terms.

### Step 8 — Provenance report
Generate `reports/m1_provenance.txt` (plain text, human-readable).

Structure:
```
ARTICLE: I.q3.a1
  SEGMENT: I.q3.a1.arg1
    concupiscentia → dychtenie [krystal_multi_voted, auto]
      signals: cs=dychtění→sense_202, en=desire→sense_202
    homo           → človek     [krystal_single, auto]
  SEGMENT: I.q3.a1.respondeo
    ...

SUMMARY
  krystal_single:       N  (X%)
  krystal_multi_voted:  N  (X%)
  krystal_multi_flagged: N (X%)
  bahounek_derived:     N  (X%)
  english_derived:      N  (X%)
  model_proposed:       N  (X%)
  TOTAL:                N
```

This is the plain-language deliverable. A non-engineer can read it and
confirm what Slovak was chosen for each term and why.

---

## Technologies
Python 3.12 + uv · lxml · python-docx · PyYAML · psycopg2-binary
CLTK (Latin lemmatization) · MorphoDiTa / ufal.morphodita (Czech lemmatization)
LLM for gap-term proposals: stub in M1 (return placeholder string); wire DeepSeek V3 in M2.

## Deliverables
1. `migrations/001_initial.sql` — reviewed and approved DDL
2. Krystal glossary loaded (terms, senses, renderings; multi-sense correctly split)
3. Latin + Bahounek parsers + English ingest working on 10 articles
4. Lemmatizer unit tests passing
5. Resolver producing `term_usage` with provenance
6. `reports/m1_provenance.txt` — the human-readable deliverable

## Acceptance criteria
- 10 articles parse with all five structural parts and correct `reply_to` links
- Every Krystal single-sense term resolves silently and correctly
- Every multi-sense term either auto-resolves with a visible strong-signal justification
  or is flagged — none silently flattened to one sense
- Provenance report readable by a non-engineer; method counts total 100% of terms found
- **Determinism:** re-running on the same input yields identical resolutions
- No model API calls in this milestone (gap terms stubbed)
