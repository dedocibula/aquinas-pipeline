# Session State

## Current Milestone
M0 — **COMPLETE**

## Status
`verify_sources.py` passes all 7 checks. All sources are on disk. 136 tests pass.

## Sources on Disk
| Source | Location | Status |
|---|---|---|
| Latin (Corpus Thomisticum) | `sources/latin/` | 87 files, 2,663 articles |
| Bahounek Czech | `sources/czech/bahounek/` | 4 files (`pars_I.html` … `pars_III.html`) |
| Krystal docx | `sources/czech/krystal/` | 258 paragraphs |
| Dominican English | `sources/english/dominican/` | 614 files |
| Freddoso English | `sources/english/freddoso/` | 4 TOC files + `coverage_gaps.json` |

## Key Decisions (permanent record)

### Latin
- Site serves HTML not XML (87 files via `iopera.html` index; TITLE attributes encode structure)
- Real article count: 2,663 (Supplementum absent from this edition; `MIN_ARTICLE_COUNT = 2_653`)

### Bahounek
- 4 monolithic HTML files saved as `pars_{part}.html`; no Supplementum
- `k N` reply format exists alongside `ad N` — M1 parser must handle both

### Freddoso
- Articles are PDFs; coverage map in `coverage_gaps.json`
- Coverage: I (119/119), I-II (114/114), II-II (189/189), III (78/90 — q79–q90 missing)

### Dominican Province
- 614 pages; code scheme: pars-digit + zero-padded question number

## Exact Next Step
Begin **M1**: schema creation.
- Read `.claude/database.md` and `.claude/m1_resolution.md` before writing any DDL
- **Stop for DDL** — per CLAUDE.md, pause and request human review before executing any schema creation scripts
