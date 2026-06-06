# Session State

## Current Milestone
M4 — **IN PROGRESS** — translation loop debugged; prompt logging live; CLTK surface-form
constraints wired; multiword formula design captured; full pilot pending.

## M4 Deliverables (status)
- `migrations/004_translation_status.sql` — **applied**; `translation_status` + `reviewer_notes` columns live
- `src/translate/translator.py` — DeepSeek V3 caller; `build_system_prompt` / `build_user_turn` public
- `src/translate/reviewer.py` — DeepSeek R1 caller; `max_tokens` raised to 8000 (was 1024 — caused empty verdicts); `build_reviewer_turn` extracted
- `src/translate/prechecks.py` — structure pre-check; `check_terminology` not in gate (Slovak inflection)
- `src/translate/loop.py` — `translate_segment()`; CLTK surface-form constraints via `_build_surface_constraints()`; optional `PromptLogger`
- `src/translate/prompt_logger.py` — JSONL per-iteration + final records for prompt analysis
- `src/translate/pilot.py` — **debug mode**: first 10 segments of I.q1; wires `PromptLogger`; restoring to Q1–Q6 is next step
- `src/common/lemmatize.py` — **moved from `src/ingest/`**; shared by both ingest and translate
- `src/server/app.py` + `src/server/db.py` + templates — Flask preview server at `localhost:5000`
- `reports/m4_pilot.txt` — written; last debug run: 10 segments, all translated, 0 needs_human

## Key Bugs Fixed This Session
- **R1 `max_tokens: 1024`** — R1 shares thinking + output tokens; on complex segments reasoning
  consumed the full budget, leaving `content` empty → `Unrecognised reviewer verdict: ''`.
  Root cause of the original 60% needs_human rate. Fixed: `max_tokens=8000`, `timeout=90s`.
- **`check_terminology` removed from pre-check gate** — Slovak exact-match rejects correct
  declined forms. Enforcement delegated to R1 reviewer AXIS 2.
- **`get_locked_terms` DISTINCT ON** — added `DISTINCT ON (gs.sense_id)` + `authority_rank`
  ordering so the highest-authority source rendering wins per sense.

## CLTK Surface-Form Constraints (implemented)
`_build_surface_constraints()` in `loop.py` runs CLTK on the segment's Latin text at
translation time, maps each approved lemma to the inflected surface forms that actually
appear (`rationem → rozum` instead of `ratio → rozum`). Multiword terms kept as-is.
Fallback to lemma form if CLTK finds no match. Reviewer still receives lemma-form
constraints (more semantic for auditing). `src/common/lemmatize.py` shared by both
`src/ingest/` and `src/translate/`.

## Multiword Formula Terms (designed, not yet built)
Design captured in `.claude/multiword_formula_design.md`. Summary:
- Problem: `term_usage` was populated by M2; terms added post-M2 have no `term_usage` rows
  → invisible to `get_locked_terms()` → never flow into REQUIRED TERMS
- **Forward path**: add `_get_multiword_phrase_constraints()` in `loop.py` — supplemental
  phrase-match for approved `is_multiword=True` terms; lazy `term_usage` writes
- **Backward path**: `src/ingest/reseed_multiword_usages.py` — phrase-scans all Latin
  segments, writes `term_usage` rows with `sense_version_used=0` to trigger stale detection
- **`import_approvals.py`**: extend to create new glossary_term+sense for blank-sense_id
  rows (no segment scanning — that's reseed's job)
- **Migration needed**: `migrations/005_human_phrase_method.sql` — add `'human_phrase'` to
  `term_usage.resolution_method` CHECK constraint (DDL — requires human review before apply)

## Pilot Run State
- Debug pilot: first 10 segments of I.q1 — all 10 `translated`, 0 `needs_human` ✓
- Prompt logs at `reports/debug_*.jsonl` — JSONL per iteration + final record
- **Next action:** restore pilot scope to Q1–Q6 and run full pilot
  1. Change `_DEBUG_QUESTION = "I.q1"` + `_DEBUG_LIMIT = 10` back to `_PILOT_QUESTIONS` in `pilot.py`
  2. Run `uv run python -m translate.pilot` — watch abort thresholds
  3. Review output at `http://localhost:5000` and record Gate 1 sign-off before M5

## Style / Prompt Quality Observations
From prompt log analysis (see `reports/debug_*.jsonl`):
- Translations are faithful but **Latinised** — `"Zachovávať hranice viet"` preserves
  sentence boundaries at the cost of natural Slovak rhythm
- Model leans heavily on Czech (Bahounek) reference which is itself a literal Latin translation
- `style_profile.yaml` system prompt is **entirely negative constraints** — no positive
  register guidance, no target-audience note
- R1 reviewer AXIS 4 explicitly defers register to M5 — stiff prose passes the gate today
- **Potential improvements** (not yet applied):
  - Add positive register line to `style_profile.yaml`: natural academic Slovak, not Latinised
  - Soften `"Zachovávať hranice viet"` to allow exceptions where Slovak word order is clearly better
  - Add within-segment term consistency instruction

## DB State (post debug pilot)
| Table | Rows | Notes |
|---|---|---|
| `segment` | 25,782 | Q1 partially translated/needs_human; rest pending |
| `segment_text` | 68,760+ | sk rows added for translated Q1 segments |
| `glossary_sense` | 3,639 | 144 approved (Krystal); 3,495 proposed |
| `term_usage` | 395,987 | M2 baseline; grows as pilot runs |

## M3 Sheet Layout
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
| I | `frequency` | computed from term_usage at export time |
| J | `sample_locator` | ltree path of sample segment |
| K | `sense_id` | hidden; idempotency key; blank = new term (future) |
| L | `group_id` | hidden; `dense_rank()` within category at export time |
| M | `db_version` | hidden; conflict detection |

## Known Gaps
- `glossary_term.category` is NULL for the 116 Krystal-seeded terms (predate DeepSeek categorization)
- All 58 formula entries in glossary are `proposed` — none `approved` — none flow into REQUIRED TERMS
- Multiword formula terms ("sed contra" etc.) not yet in glossary or resolver
- `style_profile.yaml` has no positive register guidance — addressed before full corpus run
