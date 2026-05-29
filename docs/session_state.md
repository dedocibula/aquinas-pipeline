# Session State

## Current Milestone
M1 — **IN PROGRESS**

## Status
Schema live. Latin parser + lemmatizers written and tested. 178 tests pass (7 skipped — MorphoDiTa model downloading).

## Completed This Session
| Step | Status | Notes |
|---|---|---|
| M0 test cleanup | ✓ | Moved to `tests/acquire/`; all 136 pass |
| Step 1 — Schema DDL | ✓ | `migrations/001_initial.sql` reviewed and applied |
| `src/ingest/db.py` | ✓ | DB connection helper |
| `src/ingest/lemmatize.py` | ✓ | CLTK Latin lemmatizer working; MorphoDiTa waiting on model |
| `tests/ingest/test_lemmatize.py` | ✓ | 7 Latin pass; 7 Czech skip until model available |
| `src/ingest/parser_latin.py` | ✓ | TITLE-attribute parser; all locator/element logic tested |
| `tests/ingest/test_parser_latin.py` | ✓ | 35/35 pass |

## Key Decisions (permanent record)

### Latin
- Site serves HTML not XML (87 files via `iopera.html` index; TITLE attributes encode structure)
- Real article count: 2,663 (Supplementum absent from this edition; `MIN_ARTICLE_COUNT = 2_653`)
- ltree label constraint: `-` not allowed → `I-II` → `I_II`, `II-II` → `II_II`

### Bahounek
- 4 monolithic HTML files saved as `pars_{part}.html`; no Supplementum
- `k N` reply format exists alongside `ad N` — M1 parser must handle both

### Freddoso
- Articles are PDFs; coverage map in `coverage_gaps.json`
- Coverage: I (119/119), I-II (114/114), II-II (189/189), III (78/90 — q79–q90 missing)

### Dominican Province
- 614 pages; code scheme: pars-digit + zero-padded question number
- Has clean `<h1>` / `<h2>` heading markup for question_title and article_title

### Schema
- `element_type` has no CHECK constraint — open text field, parser-owned
- `glossary_term` has no `pos` column
- `sense_rendering` has no `la` row (Latin lemma in `glossary_term.latin_lemma`)
- `term_usage` has no `term_id` (derivable via sense_id)
- `glossary_sense.version` bumps ONLY on `sense_rendering(sk).content` changes
- Title segments (`question_title`, `article_title`) stored as segment rows at `I.q3`, `I.q3.a1`

### MorphoDiTa
- Czech model: `czech-morfflex-pdt-161115.dict` — downloading from LINDAT (61MB zip)
- Model path: `models/czech-morfflex-pdt-161115/` (extracted from zip)

## HTML File Map (test articles)
| Article | HTML File | Pars Raw |
|---|---|---|
| I.q3.a1, I.q13.a5 | sth1003.html | I |
| I_II.q5.a1 | sth2001.html | I-II |
| I_II.q94.a2 | sth2094.html | I-II |
| II_II.q23.a1 | sth3023.html | II-II |
| II_II.q64.a7 | sth3061.html | II-II |
| III.q1.a1 | sth4001.html | III |
| III.q75.a4 | sth4074.html | III |

## Sources on Disk
| Source | Location | Status |
|---|---|---|
| Latin (Corpus Thomisticum) | `sources/latin/` | 87 files, 2,663 articles |
| Bahounek Czech | `sources/czech/bahounek/` | 4 files |
| Krystal docx | `sources/czech/krystal/` | 258 paragraphs |
| Dominican English | `sources/english/dominican/` | 614 files |
| Freddoso English | `sources/english/freddoso/` | 4 TOC files + `coverage_gaps.json` |

## Exact Next Steps
1. Wait for MorphoDiTa model download → run Czech lemmatizer tests
2. Implement `src/ingest/krystal.py` (Krystal DOCX parser — peek at docx structure first)
3. Implement `src/ingest/parser_bahounek.py`
4. Implement `src/ingest/ingest_english.py`
5. Run `parser_latin.py` against live DB to insert test article segments
6. Run resolver + provenance report
