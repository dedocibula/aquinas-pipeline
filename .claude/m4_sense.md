# context_label Backfill + Prompt Simplification Brief

## Background

`glossary_sense.context_label` (text NULL) exists in the schema but is largely unpopulated.
It was designed to disambiguate multi-sense Latin terms (e.g. `species` as logical category
vs. epistemological form). The same field can carry disambiguation cues for all four
`glossary_term.category` types: `term`, `name`, `formula`, `prose`.

`sense_rendering` has an `en` row per sense whose `content` is the English segment text —
this is NOT a gloss or cue, it is the English translation of the Summa passage. Do not
use it as a context label.

The goal is twofold:
1. Backfill `context_label` for approved senses so the translator prompt can surface it
2. Strengthen `translator_system.txt` with a Czech-ceiling rule alongside the existing
   GRAMMAR examples — not replace them

---

## Part 1 — Translator prompt change

### What the pilot run revealed

Removing the GRAMMAR block caused immediate regression on seg 187 — both forms the
examples guarded against (`mala iná náuka` for `haberi`, `odovzdané` for `traduntur`)
reappeared in the first run without them. The one-line passive infinitive rule alone
was insufficient.

**Root cause:** CLTK lemmatizes `haberi` → `habeo`, `traduntur/tradi` → `trado`. Neither
`habeo` nor `trado` exists in `glossary_term`. The glossary mechanism structurally cannot
handle these — they are high-frequency common verbs whose correct Slovak rendering depends
on grammatical construction, not word meaning. `habeo` usually means "to have"; only the
passive infinitive construction `haberi` means "to exist/be considered." Entering `habeo →
existovať` as a glossary constraint would be wrong in most occurrences.

**The Czech reference already solves this at scale.** In seg 187's user turn, Czech renders
`haberi` as `byla` and `traduntur` as `pojednávají` — exactly the right grammatical calls.
The model already sees the Czech reference. It just wasn't told to follow Czech's
grammatical lead, only to match its legibility floor.

**Three-way pilot result (R1=GRAMMAR, R2=no rule, R3=Czech-ceiling):** R3 produced the
same calque forms as R2 on seg 187 — `mať iné učenie` and `odovzdané`. The Czech-ceiling
rule alone is insufficient because V3 uses Czech as a passive reference, not an active
self-check. The GRAMMAR examples work because they pre-empt the literal pull by showing
the specific failure mode before generation, not after.

**R1 (GRAMMAR) > R3 (Czech-ceiling) > R2 (no rule). Keep both.**

### Target state (`prompts/translator_system.txt`)

Keep the GRAMMAR block. Add the Czech-ceiling rule to LEGIBILITY alongside it. They are
complementary — the examples guard known specific failure modes, the Czech-ceiling rule
covers unknown construction-level cases without enumeration.

**Replace the LEGIBILITY line with:**
```
LEGIBILITY: The Slovak output must be at least as legible as the provided Czech reference.
  Recast confusing phrases natively while preserving exact Scholastic sentence boundaries.
  For verb forms and grammatical constructions not covered by term constraints,
  follow the Czech reference's grammatical approach — never produce a more literal
  construction than Czech.
```

**Keep the GRAMMAR block unchanged:**
```
GRAMMAR — Latin passive infinitives:
  Do not calque Latin passive infinitives (haberi, tradi, esse + passive) literally.
  Render them with natural Slovak existential or stative verbs (byť, existovať, nachádzať sa).
  WRONG: aliam doctrinam haberi → aby sa mala aj iná náuka
  RIGHT: aliam doctrinam haberi → aby existovala aj iná náuka
  WRONG: sufficienter traduntur → je dostatočne odovzdané
  RIGHT: sufficienter traduntur → je dostatočne podané / rozoberané
```

---

## Part 2 — `build_user_turn` change (`translate/translator.py`)

### Current state

```python
parts.append("HARD TERM CONSTRAINTS (verbatim, no exceptions):")
for c in constraints:
    parts.append(f"  {c['latin_lemma']} → {c['required_slovak']}")
```

### Target state

Surface `context_label` when present by threading it directly through the constraint
pipeline — no sidecar dictionary needed.

**`translate_segment()` in `loop.py`** — include `context_label` in the constraints
list from the start:

```python
# Replaces the existing constraints list comprehension
constraints = [
    {
        "latin_lemma": t["latin_lemma"],
        "required_slovak": t["required_slovak"],
        "context_label": t.get("context_label"),  # None for single-sense terms
    }
    for t in locked_terms
]
translator_constraints = _build_surface_constraints(seg.get("latin") or "", constraints)
```

No `label_map` needed. No separate variable to thread through call sites.

**`_build_surface_constraints` in `loop.py` — line 169 only:**

Change the surface-expansion dict from a hardcoded two-key construction to a full
pass-through that only overrides `latin_lemma`:

```python
# Before (line 169):
result.append({"latin_lemma": surface, "required_slovak": c["required_slovak"]})

# After:
result.append({**c, "latin_lemma": surface})
```

The multiword passthrough (`result.append(c)` on line 162) and the no-surface fallback
(`result.append(c)` on line 171) already pass the full dict — no change needed there.

**Why this eliminates the collision:** `required_slovak` as a sole lookup key fails if
two different Latin terms in the same segment map to the same Slovak word (e.g. both
`ratio` and `intellectus` mapped to `rozum`). Threading `context_label` through the
dict means each constraint carries its own label — no lookup, no collision possible.

**`build_user_turn` in `translator.py`** — read `context_label` directly off `c`:

```python
def build_user_turn(
    seg: dict,
    constraints: list[dict],
    prior_draft: str | None,
    prior_feedback: str | None,
    # No label_map parameter needed
) -> str:
    ...
    parts.append("HARD TERM CONSTRAINTS (verbatim, no exceptions):")
    for c in constraints:
        label = c.get("context_label") or ""
        qualifier = f" [{label}]" if label else ""
        parts.append(f"  {c['latin_lemma']}{qualifier} → {c['required_slovak']}")
```

`build_user_turn` signature is unchanged from today — no new parameters, full backward
compatibility with all existing call sites and tests.

### `get_locked_terms` query change (`translate/loop.py`)

Add `context_label` to the SELECT — it is already on `glossary_sense`, no join needed:

```python
cur.execute(
    """
    SELECT DISTINCT ON (gs.sense_id)
        gt.latin_lemma,
        sr.content      AS required_slovak,
        gs.sense_id,
        gs.version,
        gs.context_label
    FROM term_usage tu
    JOIN glossary_sense gs  ON gs.sense_id = tu.sense_id AND gs.status = 'approved'
    JOIN glossary_term  gt  ON gt.term_id  = gs.term_id
    JOIN sense_rendering sr ON sr.sense_id = gs.sense_id AND sr.lang = 'sk'
    JOIN source          s  ON s.source_id  = sr.source_id
    WHERE tu.segment_id = %s
      AND sr.content IS NOT NULL
    ORDER BY gs.sense_id, s.authority_rank
    """,
    (segment_id,),
)
```

No schema migration needed — `context_label` is already on the table.

Examples of what the constraint block produces in the prompt after changes:
```
gratiam [sanctifying grace] → milosť
gratiae [as virtue (gratitude)] → vďačnosť
speciem [intentional cognitive form] → intencionálny obraz
rationem → rozum                       ← single-sense, context_label=None, no qualifier
```

---

## Part 3 — `context_label` backfill (data task)

### What the sheet data reveals (verified 2026-06-07)

**Sheet structure:**
- **Auto-resolved** (116 rows, `Aquinas_Summa_Theologiae_-_Auto-resolved.csv`): Krystal
  `krystal_single` terms. These are `status='approved'` in the DB. The `approved=FALSE`
  column is a human-review checkbox that starts unchecked — it does not mean unapproved
  in the DB sense.
- **Review** (3,523 rows, `Aquinas_Summa_Theologiae_-_Review.csv`): Everything requiring
  human disambiguation — `krystal_multi_voted`, `krystal_multi_flagged`, `bahounek_derived`,
  `english_derived`. These are `status='proposed'` in DB. None are active constraints yet.

**`group_id` is not semantic clustering.** It is a resolver batching artifact. Terms in
the same group (e.g. `ratio`'s group contains `operativus`, `Malachiáš`, `demonstro`) are
unrelated. Do not use `group_id` for any semantic purpose.

**Multi-sense terms in the Review sheet** (all `status='proposed'`, none yet usable as
hard constraints):

| latin_lemma    | sense_id | proposed_slovak             | resolution_method       |
|----------------|----------|-----------------------------|-------------------------|
| concupiscentia | 27       | žiadostivosť                | krystal_multi_flagged   |
| concupiscentia | 28       | dychtenie                   | krystal_multi_voted     |
| fides          | 46       | viera                       | krystal_multi_voted     |
| fides          | 47       | vernosť                     | krystal_multi_voted     |
| gratia         | 58       | milosť                      | krystal_multi_voted     |
| gratia         | 59       | vďačnosť                    | krystal_multi_voted     |
| intellectus    | 71       | intelekt                    | (empty)                 |
| intellectus    | 72       | intelektové nahliadnutie    | (empty)                 |
| providentia    | 106      | prozreteľnosť               | krystal_multi_voted     |
| providentia    | 107      | predvídavosť                | (empty)                 |
| affectus       | 7        | afekt                       | (empty)                 |
| affectus       | 8        | citlivosť                   | (empty)                 |
| fomes          | 51       | trúd                        | krystal_multi_voted     |
| fomes          | 52       | náklonnosť k hriechu        | krystal_multi_flagged   |
| fortitudo      | 54       | statočnosť                  | krystal_multi_voted     |
| fortitudo      | 55       | sila                        | krystal_multi_voted     |
| religio        | 115      | náboženstvo                 | krystal_multi_flagged   |
| religio        | 116      | nábožnosť                   | krystal_multi_voted     |

**Consequence for backfill sequencing:** `context_label` on a `status='proposed'` sense
has no effect on the translation pipeline — proposed senses are filtered out by the
`gs.status = 'approved'` clause in `get_locked_terms`. The SQL backfill and the
`label_map` code change are independent: the code change is safe to ship now and
degrades gracefully to the current behavior until senses are approved.

---

### Step 1: SQL — migrate existing Czech labels to English

These run immediately. They target the `status='approved'` senses where Czech labels
were found in the DB audit. Use `UPDATE ... FROM` syntax with the Czech label as a
safety guard (will update 0 rows if the label has already been changed):

```sql
-- concupiscentia: two approved senses with Czech labels
UPDATE glossary_sense gs
SET context_label = 'consequence of original sin'
FROM glossary_term gt
WHERE gs.term_id = gt.term_id
  AND gt.latin_lemma = 'concupiscentia'
  AND gs.context_label = 'důsledek dědičného hříchu';

UPDATE glossary_sense gs
SET context_label = 'as passion / disordered desire'
FROM glossary_term gt
WHERE gs.term_id = gt.term_id
  AND gt.latin_lemma = 'concupiscentia'
  AND gs.context_label = 'vášeň';

-- providentia: two approved senses with Czech labels
UPDATE glossary_sense gs
SET context_label = 'in God (divine providence)'
FROM glossary_term gt
WHERE gs.term_id = gt.term_id
  AND gt.latin_lemma = 'providentia'
  AND gs.context_label = 'u Boha';

UPDATE glossary_sense gs
SET context_label = 'in humans (prudential foresight)'
FROM glossary_term gt
WHERE gs.term_id = gt.term_id
  AND gt.latin_lemma = 'providentia'
  AND gs.context_label = 'u lidí';
```

`version` does NOT increment — `context_label` is metadata, not `sense_rendering(sk).content`.
No re-runs triggered.

---

### Step 2: Add English `context_label` when approving Review sheet senses

When a human approves a multi-sense term in the Review sheet, `import_approvals.py`
writes `sense_rendering(sk, human)` and sets `status='approved'`. At that point
`context_label` should also be written in the same DB transaction.

**Proposed English labels for all multi-sense Review terms:**

| latin_lemma    | sense_id | proposed_slovak          | context_label (English)               |
|----------------|----------|--------------------------|---------------------------------------|
| concupiscentia | 27       | žiadostivosť             | as disordered appetite (moral sense)  |
| concupiscentia | 28       | dychtenie                | as sensitive passion                  |
| fides          | 46       | viera                    | as theological virtue                 |
| fides          | 47       | vernosť                  | as keeping faith / fidelity           |
| gratia         | 58       | milosť                   | sanctifying grace                     |
| gratia         | 59       | vďačnosť                 | as virtue (gratitude)                 |
| intellectus    | 71       | intelekt                 | as intellective power / faculty       |
| intellectus    | 72       | intelektové nahliadnutie | as intellectual virtue (first principles) |
| providentia    | 106      | prozreteľnosť            | in God (divine providence)            |
| providentia    | 107      | predvídavosť             | in humans (prudential foresight)      |
| affectus       | 7        | afekt                    | as stirring of appetite               |
| affectus       | 8        | citlivosť                | as emotional disposition              |
| fomes          | 51       | trúd                     | as tinder (technical term)            |
| fomes          | 52       | náklonnosť k hriechu     | as inclination to sin                 |
| fortitudo      | 54       | statočnosť               | as cardinal virtue (courage)          |
| fortitudo      | 55       | sila                     | as physical strength                  |
| religio        | 115      | náboženstvo              | as external religious practice        |
| religio        | 116      | nábožnosť                | as virtue of religion                 |

These labels are proposed — the human reviewer confirms or corrects them when approving
each sense. The correct workflow is: human approves sense in Sheet → `import_approvals.py`
writes SK rendering + sets `context_label` in the same UPDATE.

**`import_approvals.py` change needed (Gap 4 from Claude Code audit):**
The import script currently only writes `sense_rendering(sk, human)` and bumps version.
It needs a new code path. Handle empty string explicitly — a reviewer clearing a cell
in the sheet sends `""` from the CSV reader, which must write `NULL` to the DB, not
be silently skipped:

```python
# After writing sense_rendering, also write context_label unconditionally
context_label = (row.get("context_label") or "").strip()
cur.execute(
    "UPDATE glossary_sense SET context_label = %s WHERE sense_id = %s",
    (context_label if context_label else None, sense_id),
)
# Do NOT bump version — context_label is metadata, not rendering content
```

Writing `None` (→ SQL `NULL`) when the cell is blank ensures the DB mirrors the sheet
state exactly. Skipping the update on empty string would let stale labels persist after
a reviewer intentionally clears one.

---

### Label format rules

All labels **English**, 3–6 words, lowercase, describing semantic domain or grammatical
function. Consistent prefix conventions:

```
as + [role/virtue/sense]     → "as theological virtue", "as cardinal virtue (courage)"
[domain] + grace/power/etc   → "sanctifying grace", "as intellective power"
in + [subject]               → "in God (divine providence)", "in humans (prudential foresight)"
as + [technical term]        → "as tinder (technical term)"
```

Bad labels: `'meaning 1'`, `'see context'`, anything over 8 words.

---

## Decisions (from sheet data analysis, 2026-06-07)

- **Label language: English.** Four existing Czech labels (`důsledek dědičného hříchu`,
  `vášeň`, `u Boha`, `u lidí`) are migrated to English by Step 1 SQL above.
- **`approved=FALSE` in sheets ≠ unapproved in DB.** It is a human-review checkbox.
  The 116 auto-resolved rows are `status='approved'` in DB regardless of the Sheet value.
- **Multi-sense Krystal terms are all `status='proposed'`.** `fides`, `gratia`,
  `intellectus`, `concupiscentia`, `providentia` and others are in the Review sheet
  awaiting human approval. `context_label` is threaded through the constraint dict and
  activates automatically as each sense is approved.
- **`group_id` is a batching artifact, not semantic grouping.** Do not use it for any
  purpose other than Sheet display ordering.
- **`haberi`, `traduntur`, `tradi`: absent from DB, cannot be glossarized.** `habeo`/`trado`
  are polysemous common verbs — a single SK rendering would be wrong in most uses.
  The GRAMMAR block in the system prompt plus the Czech-ceiling rule together handle these.
- **`bonum`, `actus`: absent from DB.** Post-pilot task.
- **`species`: single approved sense** (`intencionálny obraz`). The categorical `druh`
  sense does not exist. Adding it requires resolver work. Post-pilot.

---

## Sequence

1. **Part 1** — add Czech-ceiling rule to LEGIBILITY in `translator_system.txt`; keep
   GRAMMAR block unchanged. Three-way pilot confirmed GRAMMAR examples are load-bearing —
   Czech-ceiling alone regressed identically to no-rule on seg 187. Both together is the
   correct state.
2. **Part 2** — add `context_label` to `get_locked_terms` SELECT; include it in the
   constraints comprehension; update `_build_surface_constraints` to `{**c, "latin_lemma":
   surface}`; read `c.get("context_label")` directly in `build_user_turn`. No `label_map`,
   no new parameters, no schema change. Ships now, activates as senses are approved.
3. **Step 1 SQL** — run 4 Czech→English migration UPDATEs for `concupiscentia` and
   `providentia` approved senses. Each anchored with `AND gs.status = 'approved'`.
   Verify row count (should be exactly 1 each) before committing.
4. **Sheet tooling** (parallel with 1–3):
   - `export_sheet.py`: add `context_label` at col D; `proposed_slovak` shifts to E
   - `import_approvals.py`: read `context_label` from col D; write unconditionally
     (no version bump; empty string → NULL)
5. **Human approval pass** — work through Review sheet multi-sense terms using the
   proposed labels table above. Each approved sense immediately activates in the pipeline.
6. **Run debug pilot** (`_DEBUG_LIMIT=10`) after steps 1–3 — reset segments to `pending`
   first. Verify `concupiscentia` and `providentia` constraint lines show English qualifiers.
   All other multi-sense qualifiers appear after step 5.
7. **Post-pilot backlog**: `species` second sense, `bonum`/`actus` insertion,
   sense-split workflow documentation.