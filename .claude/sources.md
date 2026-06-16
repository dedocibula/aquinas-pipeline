# Sources

All raw source files live in `sources/` after M0 completes.
`python -m acquire.steps` (VerifySourcesStep) checks each entry and prints a status report.

---

## Latin — Corpus Thomisticum

| field | value |
|---|---|
| `source.code` | corpus_thomisticum |
| `source.lang` | la |
| `source.authority_rank` | 5 |
| File path | `sources/latin/` |
| Format | XML |

The authoritative, lemmatized digital edition of the Summa maintained by
Enrique Alarcón. Clean, well-structured, machine-readable. The source text
we translate from.

**Acquisition:** download from corpusthomisticum.org. Confirm the article
count matches 2,669 before proceeding.

**XML quirks to check at M1:**
- Sed contra is sometimes nested differently depending on the question
- Citation tags vary (inline vs footnote-style)
- Some questions have sub-articles — confirm parser handles these
- The five structural parts are: arg (objections), sed_contra, respondeo, reply (ad X)

---

## Czech — Bahounek (primary running text)

| field | value |
|---|---|
| `source.code` | bahounek |
| `source.lang` | cs |
| `source.authority_rank` | 20 |
| `source.kind` | reference |
| File path | `sources/czech/bahounek/` |
| Format | HTML (scraped) |

Tomáš Bahounek OP's modern Czech revision of Soukup's 1937–40 translation,
hosted at cormierop.cz. Marked explicitly as a *working text for private study*
(pracovní text). This is the primary Czech running text at most coordinates.

**Authority:** DRAFT. Bahounek's term choices are NOT authoritative — they are
overridden by the Krystal glossary wherever Krystal has an entry. Present this
to the model as "draft Czech reference" in the translation prompt, not as
"authoritative Czech."

**Coordinate tags:** every unit is tagged with a machine-readable coordinate,
e.g. `I ot. 1 čl. 1 arg. 1` (I = Prima Pars, ot = otázka/question,
čl = článek/article, arg = argument/objection). The Bahounek parser matches
these to Latin segment locators directly — minimal alignment needed at the
structural level. Sentence-level alignment within a segment still uses embeddings.

**Acquisition:** scrape cormierop.cz. Confirm coordinate tags are present on
multiple pages before committing to the scrape. Save raw HTML to disk before
parsing. Cover Prima Pars, Prima-Secundae, Secunda-Secundae, Tertia Pars.

**Known divergences from Krystal:**
- Name forms differ (Bahounek uses 'Diviš'; Krystal uses 'Dionýsios')
- Retains scholastic formulae Krystal drops ('ad primum dicendum' etc.)
- Pre-dates Krystal's terminological decisions

---

## Czech — Krystal (glossary + style rules only)

| field | value |
|---|---|
| `source.code` | krystal |
| `source.lang` | cs |
| `source.authority_rank` | 10 |
| `source.kind` | glossary |
| File path | `sources/czech/krystal/Teologicka__Suma_u_zus_-_verze_4.docx` |

The Krystal OP translation project's ~150-entry Latin→Czech glossary plus a
complete style manual. **We have the glossary and rules; we do not have Krystal's
running translation text.**

**Authority:** THE LAW for any term it covers. Krystal beats everything.

**Contents of the docx:**
1. Term lexicon: ~150 Latin→Czech pairs, many with multiple context-labelled senses
2. Translation rules: heading templates, citation conventions, name forms, orthography
3. Work abbreviations for citation handling

**Loaded into:** `glossary_term` + `glossary_sense` + `sense_rendering` at M1 step 2.
Style rules extracted into `style_profile.yaml`.

**Does NOT provide:** running Czech translation text. For most segments,
Bahounek is the only Czech reference.

---

## English — Dominican Province (primary)

| field | value |
|---|---|
| `source.code` | dominican |
| `source.lang` | en |
| `source.authority_rank` | 30 |
| `source.kind` | reference |
| File path | `sources/english/dominican/` |
| Format | HTML or text |

The Fathers of the English Dominican Province translation (1947 Benziger Brothers).
Complete coverage of the Summa. Public domain.

**Acquisition:** newadvent.org or dhspriory.org. Clean HTML, good transcription quality.

**Role in pipeline:** secondary disambiguator and semantic anchor in the translation
prompt. NOT a primary term key. English terms used as `sense_rendering.en` cues
to help the weighted-evidence resolver pick the right sense when Czech is ambiguous.

---

## English — Freddoso (supplement)

| field | value |
|---|---|
| `source.code` | freddoso |
| `source.lang` | en |
| `source.authority_rank` | 30 |
| `source.kind` | reference |
| File path | `sources/english/freddoso/` |

Alfred J. Freddoso's modern annotated translation. Partial coverage (IaIIae complete;
Prima Pars partial; others incomplete). Use where available; fall back to Dominican
Province for the rest. Same authority rank — they play the same role.

**Note on stitching:** at M1, the English ingest must handle the coverage gap cleanly.
Where a Freddoso file exists for a segment, prefer it; otherwise use Dominican Province.
Record which source provided the English for each segment via `source_id`.

---

## Source precedence summary

```
For term authority (which Slovak to trust):
  human review  rank=1   ← always wins once a human has reviewed
  krystal       rank=10  ← wins for all ~150 covered terms
  bahounek      rank=20  ← gap fill only
  dominican     rank=30  ← disambiguation/anchor only
  model         rank=90  ← never authoritative; always flagged for review

For translation prompt (which reference to show the model):
  same ranking — Czech (Bahounek) shown as primary syntactic template;
  English (Dominican/Freddoso) shown as semantic anchor.
  Style rules always come from style_profile.yaml (Krystal).
```
