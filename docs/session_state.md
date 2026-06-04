# Session State

## Current Milestone
M3 — **COMPLETE** — M4 is next.

## M3 Deliverables (all complete)
- `src/review/export_sheet.py` — exports dedup roll-up to Google Sheets (idempotent); two tabs: Review (3,523 rows) + Auto-resolved (116 krystal_single rows)
- `src/review/import_approvals.py` — reads ticked rows, writes `sense_rendering(sk, human)`, bumps version if content changed, conflict detection on `db_version`
- `src/review/sheets.py` — shared helpers: auth, header, idempotent batch write, checkbox validation
- `reports/m3_import_summary.txt` — output of first real import run (1 approved, idempotent re-run confirmed)
- `.env.example` updated with `GSHEETS_SPREADSHEET_ID`
- `.secrets/` gitignored

## M3 Sheet Layout (actual, differs from spec — improved)
| Col | Header | Notes |
|---|---|---|
| A | `approved` | checkbox; preserved on re-export |
| B | `category` | term/name/formula/prose |
| C | `latin_lemma` | |
| D | `proposed_slovak` | **editable, preserved on re-export** |
| E | `latin_occurrence` | full Latin segment text from sample occurrence |
| F | `czech_occurrence` | full Czech segment text |
| G | `english_occurrence` | full English segment text |
| H | `resolution_method` | |
| I | `frequency` | |
| J | `sample_locator` | ltree path of sample segment |
| K | `sense_id` | hidden; idempotency key |
| L | `group_id` | hidden |
| M | `db_version` | hidden; conflict detection |

## Known Gaps
- `glossary_term.category` is NULL for the 116 Krystal-seeded terms (predate DeepSeek categorization); Auto-resolved tab shows blank category for these rows — data gap, not a code bug

## M2 Final DB State
| Table | Rows | Notes |
|---|---|---|
| `segment` | 25,782 | full corpus |
| `segment_text` | 68,760 | la + cs + en |
| `term_usage` | 395,987 | fully resolved |
| `glossary_term` | 3,630 | 3,496 gap terms + 134 Krystal |
| `glossary_term.category` | 3,496 set | all gap terms categorized |
| `glossary_sense` | 3,639 | 3,496 proposed + 143 approved |

## Exact Next Step
Build M4: translation loop.

**Before coding, read:**
- `.claude/m4_translation.md`
- `.claude/decisions.md`
- `.claude/database.md`

**Key M4 design constraints (from m4_translation.md):**
- Uses `anthropic` Batch API — add to `pyproject.toml` at M4 start
- Hard constraints: approved Slovak terms injected into prompt; model translates prose around them
- Re-run scope: only stale segments (`term_usage.sense_version_used < glossary_sense.version`)
- M3 is not a blocking gate — M4 may begin as soon as some terms are approved
