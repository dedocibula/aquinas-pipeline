# Session State

## Current Milestone
M3 — **NOT STARTED** — M2 complete and accepted.

## M2 Final DB State
| Table | Rows | Notes |
|---|---|---|
| `segment` | 25,782 | full corpus |
| `segment_text` | 68,760 | la + cs + en |
| `term_usage` | 395,987 | fully resolved |
| `glossary_term` | 3,630 | 3,496 gap terms + 134 Krystal |
| `glossary_term.category` | 3,496 set | all gap terms categorized |
| `glossary_sense` | 3,639 | 3,496 proposed + 143 approved |

## M2 Coverage Summary (reports/m2_coverage.txt — Jun 3)
- 2,663 articles; 22,621 segments; 8 anomalous (see m2_parser_anomalies.txt)
- Auto-resolved (no review needed): 9.3%
- Needs human review: 90.7% — 3,503 unique terms
- Gap terms proposed by DeepSeek V3: 3,496 at ~$0.0002
- No bracketed stubs; no-stub guardrail passed

## Exact Next Step
Build M3: `src/review/export_sheet.py` and `src/review/import_approvals.py`.

**Prerequisites before coding:**
1. Create Google Cloud service account with Sheets API enabled
2. Store JSON key at `.secrets/gsheets_service_account.json` (gitignored)
3. Add `GSHEETS_SPREADSHEET_ID` to `.env` and `.env.example`
4. Add `gspread>=6.0` and `google-auth>=2.0` to `pyproject.toml`

**Then build (per m3_review.md):**
- `export_sheet.py` — exports dedup roll-up to Google Sheets (idempotent); separate "Auto-resolved" tab for krystal_single terms
- `import_approvals.py` — reads ticked rows, writes `sense_rendering(sk, human)`, bumps version if content changed, conflict detection on `db_version`

**Key design constraints:**
- No migration needed — M2 schema already has everything M3 needs
- Version bump only when Slovak content changed (triggers M4 stale query)
- M3 is not a blocking gate — M4 may start as soon as some terms are approved
