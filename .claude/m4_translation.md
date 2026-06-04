# M4 — Pilot Translation & Preview Server

**Status:** build-locked
**Reads:** database.md, decisions.md, sources.md
**Estimate:** 3 days
**Prerequisite:** M2 complete; at least partial M3 review done (some terms approved)

---

## User story
*As the engineer, I need to run the translation loop on a representative 30-article
pilot and immediately see the output in a readable parallel-text interface — so that
I can evaluate translation quality, calibrate the reviewer rubric, and make a go/no-go
decision on the full corpus run before spending the bulk of the translation budget.*

## Objective
Two deliverables in sequence:
1. **Pilot translation** — 30 articles translated through the full loop (draft → pre-check → R1 review → revise), results in DB.
2. **Preview server** — a local Flask server showing Latin | Slovak side-by-side, navigable by article, inspired by aquinas.cc.

Gate 1 is the human review of the pilot output in the preview server. Full corpus
translation does not begin until Gate 1 sign-off. No Prefect in M4 — 30 articles
run fine as plain Python. Prefect enters in M5 for the multi-day full corpus run.

---

## Schema migration
`migrations/004_translation_status.sql`:

```sql
ALTER TABLE segment
    ADD COLUMN translation_status text NOT NULL DEFAULT 'pending'
        CHECK (translation_status IN ('pending','translated','needs_human')),
    ADD COLUMN reviewer_notes     jsonb;

CREATE INDEX idx_segment_translation_status
    ON segment(translation_status)
    WHERE translation_status = 'pending';
```

`translation_status`: `pending` | `translated` | `needs_human`
`reviewer_notes`: advisory notes from R1, structured by axis:
`{"iteration": 2, "register": "phrase X is colloquial", "semantics_minor": null}`

---

## Part 1 — Pilot translation (30 articles)

### Pilot article set
One complete tractate so the output reads as continuous theology, not isolated samples.
Use Prima Pars Q1–Q6 (On the nature of Sacred Doctrine and the existence/nature of God)
plus the 10-article test set from M1. This gives ~30 articles spanning simple and dense
theological content, covering the most common multi-sense terms.

### The loop (plain Python, no Prefect)

```python
MAX_ITERATIONS = 3

def translate_segment(segment_id: int, db: Repository) -> str:
    seg         = db.get_segment_with_texts(segment_id)   # v_segment
    constraints = db.get_locked_terms(segment_id)         # term_usage → sense_rendering(sk)

    prior_draft    = None
    prior_feedback = None
    best_draft     = None   # last draft that cleared pre-checks, or final draft

    for iteration in range(1, MAX_ITERATIONS + 1):
        draft = call_translator_v3(seg, constraints, prior_draft, prior_feedback)

        # Pre-checks — no LLM, always run first
        structure_ok   = check_structure(seg, draft)
        terminology_ok = check_terminology(draft, constraints)

        if not (structure_ok and terminology_ok):
            prior_feedback = build_precheck_feedback(structure_ok, terminology_ok)
            prior_draft    = draft
            if best_draft is None:
                best_draft = draft
            continue  # back to translator; do NOT call R1

        best_draft = draft  # cleared pre-checks; candidate for best

        review = call_reviewer_r1(seg.latin, draft, constraints)

        if review.verdict in ('APPROVED', 'APPROVED_WITH_NOTES'):
            db.write_segment_text(segment_id, 'sk', 'model', draft)
            db.update_translation_status(segment_id, 'translated')
            if review.notes:
                db.write_reviewer_notes(segment_id, review.notes, iteration)
            return 'translated'

        prior_feedback = review.feedback
        prior_draft    = draft

    # Exhausted — write best_draft (last to clear pre-checks, not necessarily last draft)
    db.write_segment_text(segment_id, 'sk', 'model', best_draft or draft)
    db.update_translation_status(segment_id, 'needs_human')
    log.warning(f"Segment {segment_id} flagged needs_human after {MAX_ITERATIONS} iterations")
    return 'needs_human'
```

### Pre-checks (deterministic, no LLM)

```python
def check_structure(seg: Segment, draft: str) -> CheckResult:
    # Count arg segments for this article via reply_to links
    expected_args = db.count_args_for_article(seg.locator_path)
    # Check sed_contra marker: "Na druhé straně"
    # Check respondeo marker: "Odpoveď" or "Odpoveď:"
    # For reply segments: check reply marker present
    # FAIL if count mismatch or marker absent

def check_terminology(draft: str, constraints: list[TermConstraint]) -> CheckResult:
    # For each (latin_lemma, required_slovak) in constraints:
    #   normalise(draft) must contain normalise(required_slovak)
    # FAIL lists all missing terms — translator sees exactly what to fix
```

Fail loudly — log segment_id, failure type, and the draft excerpt. Never silent-pass.

### Prompt caching (required)

**Translator — cache prefix (system prompt, stable across all segments):**
```
You are translating Thomas Aquinas's Summa Theologiae from Scholastic Latin into Slovak.

STYLE RULES:
  Maintain the hard term constraints given below as terms, names, formulas and prose.

  Spelling    → filozofia, teológia, -izmus

NEGATIVE CONSTRAINTS:
  Do NOT improve literary quality
  Preserve Aquinas's word repetition — it is intentional
  Preserve: totiž, teda, však, odtiaľ, ale
  Do not merge sentences or reorganise for readability
  Translate FROM Latin; Czech and English are references only
```

Variable per-segment (NOT cached — goes in user turn):
```
HARD TERM CONSTRAINTS (verbatim, no exceptions):
  [locked terms for this segment]

CZECH REFERENCE (draft, not authoritative for terms):
  [bahounek text]

ENGLISH REFERENCE (semantic anchor):
  [dominican/freddoso text]

Translate:
[latin text]
```

On revision, prepend to user turn:
```
PRIOR DRAFT:
[previous draft]

REVIEWER FEEDBACK — address each point:
[bullet list]
```

**Reviewer — cache prefix (system prompt + rubric):**
```
You are a quality reviewer for a Slovak translation of Aquinas's Summa Theologiae.
Evaluate against four axes. Verdict must be one of three options (exact strings).

AXIS 1 — STRUCTURE
Count objections in Latin; confirm same count in draft.
Check: sed_contra, respondeo, and replies present.
A missing element is always FAIL.

AXIS 2 — TERMINOLOGY
Each required term must appear verbatim in the draft.
A missing term is always FAIL.

AXIS 3 — SEMANTICS
Does the Slovak convey the logical argument faithfully?
MAJOR failure: argument direction changes, conditional replaces categorical,
  modal distinctions collapse. → REVISION NEEDED
MINOR imprecision: slightly loose rendering, no argument change. → note only, not FAIL.

AXIS 4 — REGISTER
Flag colloquialisms, modern idioms, restructured sentences.
Register issues → notes only, never FAIL (handled in M5 polish).

VERDICT (output exactly one):
  APPROVED
  APPROVED WITH NOTES: [bulleted advisory items]
  REVISION NEEDED: [bulleted required changes — structure/terminology/major-semantic only]
```

Variable per-segment (NOT cached):
```
REQUIRED TERMS: [term list]
LATIN: [latin text]
DRAFT: [draft text]
```

### Pilot run script
```bash
uv run python -m translate.pilot   # translates the 30-article pilot set
```

Outputs `reports/m4_pilot.txt`:
```
PILOT RUN SUMMARY
  Articles:          30
  Segments:          ~150
  Translated:        N  (X%)
  Needs human:       N  (X%)
  Avg iterations:    N.N
  Cache hit rate:    X%  (verify caching is firing)
  API cost:          ~$X
  Time elapsed:      Xm Xs
```

Abort if `needs_human > 20%` — rubric is too strict; adjust before proceeding.
Abort if `avg_iterations > 2.5` — translator prompt needs tuning.

---

## Part 2 — Preview server

A local Flask server. Read-only. Queries the DB. No auth. Dev tool only.

### URL structure (mirrors aquinas.cc)
```
/                           → index: all questions as a navigable list
/la/sk/~ST.I.Q3.A1          → article view: I.q3.a1 in Latin | Slovak
/la/sk/~ST.I.Q3             → question view: all articles in question 3
/api/status                 → JSON: translation progress stats
```

URL to ltree conversion: `ST.I.Q3.A1` → `I.q3.a1`
(strip `ST.`, lowercase, `Q` → `q`, `A` → `a`)

### Article view layout

```
┌────────────────────────────────────────────────────┐
│  ST I, Q3, A1 —                                    |
│  [← Prev article]               [Next article →]   │
├─────────────────────────┬──────────────────────────┤
│ Whether God is a body?  │ Či je Boh telo?          │
├─────────────────────────┼──────────────────────────┤
│ LATIN                   │ SLOVAK           [status]│
├─────────────────────────┼──────────────────────────┤
│ Objection 1. ...        │ Námietka 1. ...          │
├─────────────────────────┼──────────────────────────┤
│ On the contrary. ...    │ Na druhej strane: ...    │
├─────────────────────────┼──────────────────────────┤
│ I answer that ...      │ Odpovedám že: ...         │
├─────────────────────────┼──────────────────────────┤
│ Reply to obj. 1. ...    │ K námietkám: ...         │
└─────────────────────────┴──────────────────────────┘
```

Each row is one segment (`element_type`). Rows are ordered by `locator_path`.
`[status]` badge per row: ✓ translated · ⚠ needs_human · … pending
If `reviewer_notes` is non-null, show a ℹ icon; hover reveals the note.
If Slovak is pending, show "— awaiting translation —" in the right column.

### DB query for article view

```sql
SELECT
    s.segment_id,
    s.locator_path::text,
    s.element_type,
    s.reply_to,
    s.translation_status,
    s.reviewer_notes,
    latin.content       AS latin,
    slovak.content      AS slovak
FROM segment s
JOIN segment_text latin
    ON latin.segment_id = s.segment_id AND latin.lang = 'la'
LEFT JOIN segment_text slovak
    ON slovak.segment_id = s.segment_id
    AND slovak.lang = 'sk'
    AND slovak.source_id = (SELECT source_id FROM source WHERE code = 'human'
                            UNION ALL
                            SELECT source_id FROM source WHERE code = 'model'
                            LIMIT 1)  -- prefer human, fall back to model
WHERE s.locator_path <@ $1::ltree   -- e.g. 'I.q3.a1'
ORDER BY s.locator_path;
```

(Prefer human-reviewed Slovak over model draft if both exist.)

### Flask app structure

```
src/server/
├── app.py          ← Flask app, routes, DB connection
├── templates/
│   ├── base.html   ← layout, nav, CSS
│   ├── index.html  ← question list
│   ├── article.html ← parallel text view
│   └── question.html ← all articles in a question
└── static/
    └── style.css   ← minimal; two-column layout, element_type colours
```

```python
# app.py sketch
from flask import Flask, render_template, abort
import re

app = Flask(__name__)

def url_to_ltree(st_locator: str) -> str:
    """ST.I.Q3.A1 → I.q3.a1"""
    s = st_locator.replace('ST.', '')
    s = re.sub(r'Q(\d+)', r'q\1', s)
    s = re.sub(r'A(\d+)', r'a\1', s)
    return s.lower()

@app.route('/')
def index():
    questions = db.get_all_questions()  # distinct locators at q depth
    return render_template('index.html', questions=questions)

@app.route('/la/sk/~<path:st_locator>')
def text_view(st_locator: str):
    ltree_loc = url_to_ltree(st_locator)
    depth = ltree_loc.count('.') + 1

    if depth == 2:  # I.q3 — question level
        articles = db.get_question_articles(ltree_loc)
        return render_template('question.html', locator=ltree_loc, articles=articles)
    if depth == 3:  # I.q3.a1 — article level
        segments = db.get_article_segments(ltree_loc)
        if not segments:
            abort(404)
        nav = db.get_prev_next_article(ltree_loc)
        return render_template('article.html',
                               locator=ltree_loc, segments=segments, nav=nav)
    abort(404)

@app.route('/api/status')
def status():
    return db.get_translation_progress()  # JSON: counts by translation_status
```

### Running the server
```bash
uv run flask --app src/server/app.py run --port 5000
# open http://localhost:5000/la/sk/~ST.I.Q3.A1
```

---

## Gate 1 — pilot review
Read the 30-article pilot in the preview server. Check:
- Does the Slovak flow naturally as theological prose?
- Are locked terms rendering correctly and consistently?
- Are the `needs_human` segments genuinely hard, or is the rubric miscalibrated?
- Does the Scholastic structure (objections/sed contra/respondeo/replies) read correctly?

If `needs_human > 10%` after rubric review: adjust system prompts, re-run pilot.
If quality is acceptable: proceed to M5.

**Do not run the full corpus until Gate 1 passes.**

---

## Technologies
Python 3.12 + uv · Flask · Jinja2 · psycopg2-binary
DeepSeek V3 (translator) · DeepSeek R1 (reviewer) via direct API

New in `pyproject.toml`:
```toml
flask = ">=3.0"
```

No Prefect in M4. No LangGraph. No LangChain.

---

## Deliverables
1. `migrations/004_translation_status.sql` — reviewed before any translation
2. `src/translate/translator.py` — V3 call with cached system prompt
3. `src/translate/reviewer.py` — R1 call with cached rubric; verdict parser
4. `src/translate/prechecks.py` — structure + terminology checks
5. `src/translate/loop.py` — `translate_segment()`
6. `src/translate/pilot.py` — pilot runner script
7. `src/server/app.py` + templates — Flask preview server
8. `reports/m4_pilot.txt` — pilot summary (cost, iterations, needs_human rate)

## Acceptance criteria
- Migration runs cleanly
- Pilot completes: `needs_human < 20%`, `avg_iterations < 2.5`, cache hit rate logged
- Pre-checks proven: a draft missing an objection never triggers an R1 call
- Preview server opens at `localhost:5000`; Latin | Slovak renders side-by-side
- Navigating to `~ST.I.Q3.A1` shows the correct article; prev/next works
- Pending segments show "— awaiting translation —"; not an error
- `needs_human` segments show ⚠ badge; reviewer_notes visible on hover
- Gate 1 review completed; human sign-off recorded before M5 begins