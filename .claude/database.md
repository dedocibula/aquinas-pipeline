# Database Schema

PostgreSQL 16.

**CRITICAL — run before creating any tables:**
```sql
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS vector;
```

Language-agnostic: languages and sources are rows, not columns.
Closed sets use CHECK enums. Open-ended sets use FK tables.

**Build rule:** produce DDL + migration + seed data; stop for human review
before any parser writes a row. `term_usage` is populated corpus-wide and
is expensive to fix retroactively — get the schema right first.

---

## `work`
One row per text translated.

| column | type | notes |
|---|---|---|
| `work_id` | serial PK | |
| `author` | text | e.g. 'Thomas Aquinas' |
| `title` | text | e.g. 'Summa Theologiae' |
| `structure_type` | text | e.g. 'summa_articulus' — drives parser dispatch |
| `source_lang` | text | e.g. 'la' — makes source/reference distinction derivable |

Populated by: manual seed.
Read by: parser dispatch; query scoping.
Future use: a second Aquinas work is a new row with a different `structure_type`.
`source_lang` means we never hardcode "Latin is the source" anywhere else.

---

## `source`
The precedence dimension. Authority order is data, not code.

| column | type | notes |
|---|---|---|
| `source_id` | serial PK | |
| `code` | text UNIQUE | 'krystal','bahounek','corpus_thomisticum','dominican','model','human' |
| `lang` | text | language this source provides ('la','cs','en','sk') |
| `kind` | text | 'glossary' \| 'reference' \| 'source_text' \| 'machine' \| 'review' |
| `authority_rank` | int UNIQUE | lower = higher authority; **UNIQUE enforced at DB level** |
| `note` | text | |

Populated by: seed data (part of first reviewable deliverable).
Read by: resolver (which evidence wins); translator prompt (which reference to trust most).

`authority_rank` is `UNIQUE` on purpose. If two sources share a rank, the resolver's
evidence vote becomes non-deterministic, violating the "Krystal wins" principle.
The DB constraint prevents a bad seed from breaking the hierarchy silently.

**Seed data:**
```sql
INSERT INTO source (code, lang, kind, authority_rank, note) VALUES
  ('human',              'sk', 'review',      1,  'Theologian reviewer'),
  ('corpus_thomisticum', 'la', 'source_text',  5,  'Corpus Thomisticum XML'),
  ('krystal',            'cs', 'glossary',    10,  'Krystal OP glossary + style rules'),
  ('bahounek',           'cs', 'reference',   20,  'Bahounek modern Czech revision'),
  ('dominican',          'en', 'reference',   30,  'Dominican Province translation'),
  ('freddoso',           'en', 'reference',   35,  'Freddoso translation (partial)'),
  ('model',              'sk', 'machine',     90,  'Model-generated draft');
```

---

## `segment`
The spine. One row per atomic translatable unit.

| column | type | notes |
|---|---|---|
| `segment_id` | serial PK | |
| `work_id` | FK→work | |
| `locator_path` | ltree NOT NULL | hierarchical coordinate, e.g. 'I.q3.a1.arg2' |
| `element_type` | text | CHECK IN ('arg','sed_contra','respondeo','reply') |
| `reply_to` | int NULL | FK→segment_id of the objection this reply answers |
| `translation_status` | text NOT NULL DEFAULT 'pending' | CHECK IN ('pending','translated','needs_human'); set by M4 translation loop |
| `reviewer_notes` | jsonb NULL | advisory JSON from R1 reviewer, e.g. `{"iteration":2,"register":"phrase X is colloquial"}`; NULL until translated |

Added by migration 004 (M4). Partial index on `translation_status = 'pending'` for fast pilot batch queries.

Populated by: Latin parser (M1).
Read by: resolver; translator (M4); reviewer structural check (M4); re-run engine (M4).

**`locator_path` is `ltree` on purpose.**
`ltree` is PostgreSQL's native hierarchical path type. Use `<@` for ancestry queries:
- `locator_path <@ 'I.q3'` → all segments in question 3 (safe: won't match q30)
- `locator_path ~ 'I.q3.a1.*'` → all elements of article 1
- `LIKE 'I.q3.%'` would be wrong — it matches 'I.q30.a1'. Never use LIKE on this column.

The path is opaque to everything *except* the parser that writes it. A future
Contra Gentiles parser writes 'lib1.cap13'; nothing downstream parses the string.
The `ltree` type just gives us safe, indexed hierarchy queries for free.

`reply_to` is kept even though Krystal's output style drops the "ad primum dicendum"
formula. The M4 reviewer agent's structural-fidelity check requires the objection↔reply
linkage. Output style ≠ internal model. Recognition patterns live in the parser;
output rules live in `style_profile.yaml`.

---

## `segment_text`
Per-language, per-source text for a segment. Languages are rows, not columns.

| column | type | notes |
|---|---|---|
| `segment_id` | FK→segment | |
| `lang` | text | 'la','cs','en','sk' |
| `content` | text | |
| `source_id` | FK→source | |
| | | UNIQUE(segment_id, lang, source_id) |

Populated by:
- Latin parser → (la, corpus_thomisticum)
- Bahounek parser → (cs, bahounek)
- English ingest → (en, dominican) or (en, freddoso)
- Translator (M4) → (sk, model)
- Reviewer (M3/M4) → (sk, human)

Read by: resolver (la for term finding; cs/en as evidence); translator (all as
references, writes sk draft); re-run engine (reads/writes sk rows).

UNIQUE key allows coexistence: `(sk, model)` and `(sk, human)` both live.
- "Give me the draft" = `WHERE lang='sk' AND source_id = <model_id>`
- "Give me the final" = `WHERE lang='sk' AND source_id = <human_id>`

Coexistence is intentional — diff drafts vs finals to measure how much humans change.

---

## `glossary_term`
One row per distinct Latin term (lemma).

| column | type | notes |
|---|---|---|
| `term_id` | serial PK | |
| `latin_lemma` | text UNIQUE | dictionary form; the join target after lemmatization |
| `is_multiword` | bool DEFAULT false | true for 'actus essendi', 'per se', etc. |
| `category` | text NULL | CHECK IN ('term','name','formula','prose'); set by DeepSeek proposal pass for gap terms only; Krystal-seeded terms keep NULL |
| `notes` | text | |

`category` is NULL for all ~150 Krystal-seeded terms — they are authoritative regardless of category.
Only gap terms (lemmas not in Krystal) carry a model-assigned category, set during the M2 DeepSeek proposal pass.
Drives M3 review ordering; fully overridable by a reviewer.

Added by migration 003 (M2).

Populated by: Krystal preseed (M1). Gap terms appended during resolution.
Read by: resolver, as the join target after CLTK lemmatizes a Latin surface form.

**`is_multiword=true` entries must be phrase-matched BEFORE single-token lemmatization.**
Otherwise 'actus essendi' is shredded into two words and neither matches the glossary.
The resolver must process all multiword terms first, then single-token terms.

---

## `glossary_sense`
One row per (term, meaning). A term may have several senses.

| column | type | notes |
|---|---|---|
| `sense_id` | serial PK | |
| `term_id` | FK→glossary_term | |
| `context_label` | text NULL | e.g. 'as passion'; NULL = default/only sense |
| `status` | text | CHECK IN ('proposed','flagged','approved') |
| `version` | int DEFAULT 1 | increments ONLY when sense_rendering(sk).content changes |

Populated by: Krystal preseed — single-sense term → one row (context_label NULL);
multi-sense term → multiple labelled rows. Gap terms → status='proposed'.
Read by: resolver (pick the right sense); translator (M4, via term_usage); reviewer (M3).

**`version` is owned by content, not by status.**
Bump version only when `sense_rendering(sk).content` actually changes — check the diff
before incrementing. A status transition (proposed→approved) with no Slovak content
change does NOT bump version and does NOT trigger re-runs. This prevents spurious
re-runs when a reviewer confirms a correct guess without changing the term.

**`version` is the invalidation engine.** It pairs with `term_usage.sense_version_used`
to answer "which translations are stale?" with one cheap query. See `term_usage`.
Any correction to any approved Slovak term — even months into the project — triggers
a precise, cheap re-run of exactly the affected segments.

A term with multiple senses must NEVER silently flatten to one Slovak term.
It either evidence-resolves to a confirmed sense or is flagged.
Single-sense terms auto-resolve freely.

Multi-sense terms from Krystal (non-exhaustive):
concupiscentia, gratia, fides, intellectus, providentia, ratio, passio, forma,
virtus, bonum, actus, potentia, species, intentio, sensus.

---

## `sense_rendering`
Per-language realization of a sense. One row per (sense, lang, source).

| column | type | notes |
|---|---|---|
| `sense_id` | FK→glossary_sense | |
| `lang` | text | 'cs','en','sk' — no 'la' row; Latin lemma lives in glossary_term |
| `lemma` | text NULL | lemmatized form; only populated for lang='cs' (the reverse-map key) |
| `content` | text | the term/cue/approved-translation text |
| `source_id` | FK→source | |
| | | INDEX on (lang, lemma) |

Populated by: Krystal preseed writes three rows per sense: cs-anchor, en-cue,
sk-approved. No la row — the Latin lemma already lives in `glossary_term.latin_lemma`
and is never looked up via sense_rendering. Reviewer (M3) writes/overwrites sk row
with source=human.
Read by: resolver (cs + en rows are disambiguation evidence keys);
translator (M4, sk row is the hard constraint injected into the prompt).

The cs row's `lemma` is the reverse-map key: find the sense whose cs lemma appears
in this segment's cs text. Index (lang, lemma) makes this join fast.
The sk row's `content` is the actual Slovak term injected as a hard constraint.

---

## `term_usage`
The invalidation backbone. One row per occurrence of a term in a segment.

| column | type | notes |
|---|---|---|
| `usage_id` | serial PK | |
| `segment_id` | FK→segment | where this term was found |
| `sense_id` | FK→glossary_sense | which sense was chosen; join to glossary_sense.term_id for the term |
| `sense_version_used` | int | the sense.version live when this segment was translated |
| `resolution_method` | text | see values below |
| `confidence` | text | CHECK IN ('auto','needs_review') |
| `signals` | jsonb | evidence used, e.g. {"cs":"dychtění→202","en":"desire→202"} |
| `status` | text | CHECK IN ('guessed','confirmed') |

`term_id` is omitted — derivable via `sense_id → glossary_sense.term_id`.
If query performance requires it later, add it back as an explicit denormalization
with a trigger to prevent sense_id/term_id drift.

Populated by: resolver (M1/M2), one row per term per segment.
Read by: provenance report (M1); coverage report (M2); re-run engine (M4); translator (M4).

**`confidence` and `status` track different things and must not be conflated:**

`confidence` is set ONCE by the resolver at resolution time and never changes.
It records the resolver's certainty about the evidence:
- `auto` — evidence was consistent and at least one strong signal was present
- `needs_review` — evidence was absent, split, or only weak signals present

`status` is mutable and tracks human-process state:
- `guessed` — initial state; the resolution has not been confirmed by a human
- `confirmed` — a human reviewer has approved this resolution

Valid combinations and what they mean:
```
confidence='auto',         status='guessed'    → auto-resolved; correct in most cases; human spot-check optional
confidence='auto',         status='confirmed'  → human explicitly blessed an auto-resolution
confidence='needs_review', status='guessed'    → in the review queue; must not be used as final
confidence='needs_review', status='confirmed'  → human reviewed and approved; safe to use
```

Do NOT write a CHECK that treats these as mutually exclusive.
`confidence='auto'` does NOT imply `status='confirmed'` — auto-resolutions are still
guesses until a human confirms them, even if the evidence was strong.

**`resolution_method` values:**
- `krystal_single` — one sense, resolved silently; unlikely to need human review
- `krystal_multi_voted` — multiple senses, auto-resolved by consistent evidence
- `krystal_multi_flagged` — multiple senses, evidence unclear; confidence=needs_review
- `bahounek_derived` — not in Krystal; derived from Bahounek Czech
- `english_derived` — not in Krystal or Bahounek; derived from English
- `model_proposed` — no source available; model proposed; always confidence=needs_review

**The re-run query (M4):**
```sql
SELECT segment_id FROM term_usage
WHERE sense_id = $1
  AND sense_version_used < (SELECT version FROM glossary_sense WHERE sense_id = $1);
```
This returns exactly the stale segments — never the whole corpus.

Do not remove or retype `sense_version_used` or `signals`. Their consumers are M3/M4.

---

## Views

```sql
-- Pivot segment_text back to column-per-language for application ergonomics.
-- Source of truth stays normalized; this is a read convenience.
-- Upgrade to materialized view only if measured slow.
CREATE VIEW v_segment AS
  SELECT
    s.segment_id,
    s.work_id,
    s.locator_path,
    s.element_type,
    s.reply_to,
    max(t.content) FILTER (WHERE t.lang='la')                        AS latin,
    max(t.content) FILTER (WHERE t.lang='cs')                        AS czech,
    max(t.content) FILTER (WHERE t.lang='en')                        AS english,
    max(t.content) FILTER (WHERE t.lang='sk' AND src.code='model')   AS slovak_draft,
    max(t.content) FILTER (WHERE t.lang='sk' AND src.code='human')   AS slovak_final
  FROM segment s
  JOIN segment_text t   USING (segment_id)
  JOIN source src       ON t.source_id = src.source_id
  GROUP BY s.segment_id, s.work_id, s.locator_path, s.element_type, s.reply_to;

-- Pivot sense_rendering back to column-per-language.
CREATE VIEW v_sense AS
  SELECT
    gs.sense_id,
    gs.term_id,
    gt.latin_lemma,
    gs.context_label,
    gs.status,
    gs.version,
    max(r.lemma)   FILTER (WHERE r.lang='cs')                         AS czech_lemma,
    max(r.content) FILTER (WHERE r.lang='cs')                         AS czech_term,
    max(r.content) FILTER (WHERE r.lang='en')                         AS english_cue,
    max(r.content) FILTER (WHERE r.lang='sk')                         AS slovak_term,
    max(src.code)  FILTER (WHERE r.lang='sk')                         AS slovak_source
  FROM glossary_sense gs
  JOIN glossary_term gt   USING (term_id)
  JOIN sense_rendering r  USING (sense_id)
  JOIN source src         ON r.source_id = src.source_id
  GROUP BY gs.sense_id, gs.term_id, gt.latin_lemma, gs.context_label, gs.status, gs.version;
```

---

## External config

**`prompts/translator_system.txt`** and **`prompts/reviewer_system.txt`** (version-controlled with code, not in DB).
Replaced `style_profile.yaml` in M4. Contain Krystal's house rules split by consumer.
Live outside the DB because they drive prompt behavior, not lookups.

`prompts/translator_system.txt` contains:
- Heading templates: sed_contra → "Na druhé straně", respondeo → "Odpověď.",
  replies → "K námitkám"; drop "praeterea"/"ad primum dicendum"; number objections.
- FORMATTING section: rules for how the Slovak draft is structured.
- LEGIBILITY positive instruction + GRAMMAR section with passive infinitive WRONG/RIGHT examples.
- Citation rules: Aristotle by Bekker in footnotes; Fathers by PL/PG;
  Bible by JB abbreviations and ČEP text — EXCEPT translate Thomas's own Bible
  quotations from Thomas's Latin, not from the modern Bible.
  ("Nepřekládáme Bibli, ale TA.")
- Name forms: Augustin, Boethius (no ë), Řehoř; Dionýsios (NOT Diviš);
  Athanasios (NOT Atanáš).
- Orthography: filosofie / teologie / -ismus (not filozofie / theologie / -izmus).
- Negative constraints for polish pass (M5): do not increase literary quality;
  preserve repetition; preserve scholastic particles; preserve sentence boundaries.

`prompts/reviewer_system.txt` contains:
- Semantics + legibility evaluation only (Axes 1 & 2 removed in M4 quality fix).
- `<verdict>` XML tag protocol used by `_parse_verdict` in `reviewer.py`.