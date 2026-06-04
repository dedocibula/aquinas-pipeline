# Session State

## Current Milestone
M4 — **IN PROGRESS** — translation loop built; preview server verified; pilot run pending.

## M4 Deliverables (status)
- `migrations/004_translation_status.sql` — **applied**; `translation_status` + `reviewer_notes` columns live
- `src/translate/translator.py` — DeepSeek V3 caller with cached system prompt
- `src/translate/reviewer.py` — DeepSeek R1 caller with cached rubric; verdict parser
- `src/translate/prechecks.py` — structure + terminology pre-checks (no LLM)
- `src/translate/loop.py` — `translate_segment()` — MAX_ITERATIONS=3 loop
- `src/translate/pilot.py` — pilot runner for Q1–Q6 (294 segments); writes `reports/m4_pilot.txt`
- `src/server/app.py` + `src/server/db.py` + templates — **Flask preview server verified** (`localhost:5000`)
- `reports/m4_pilot.txt` — **NOT YET WRITTEN** — pilot run not started

## Preview Server
Running at `http://localhost:5000`. Start with:
```
uv run flask --app src/server/app.py run --port 5000
```
Routes verified:
- `/` → index (200)
- `/la/sk/~ST.I.Q1.A1` → article view (200, pending segments show "— awaiting translation —")
- `/la/sk/~ST.I.Q1` → question view (200)
- `/api/status` → `{"pending":25782,"translated":0,"needs_human":0}`

Bug fixed this session: `server/db.py:get_all_questions()` — `SELECT DISTINCT … ORDER BY` referenced a non-select expression; fixed by adding `_sort_key` to the select list.

## Pilot Run State
- 294 segments pending in Q1–Q6 (Q1=8a, Q2=3a, Q3=8a, Q4=3a, Q5=6a, Q6=4a × ~10 segs/article)
- 0 translated so far
- **Next action:** run `uv run python -m translate.pilot` — will abort if needs_human > 20% or avg_iters > 2.5

## M4 DB State (pre-pilot)
| Table | Rows | Notes |
|---|---|---|
| `segment` | 25,782 | `translation_status='pending'` for all |
| `segment_text` | 68,760 | la + cs + en; no sk yet |

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
Run the pilot:
```
uv run python -m translate.pilot
```
Watch for abort conditions:
- `needs_human > 20%` → adjust `reviewer.py` rubric
- `avg_iterations > 2.5` → tune translator prompt

After pilot completes, review output at `http://localhost:5000` and record Gate 1 sign-off before M5.
