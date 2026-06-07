# Translation Pipeline — Bug Fix Briefing

## Context

Multi-agent Slovak translation pipeline for Aquinas's *Summa Theologiae*.
Architecture: DeepSeek-V3 (translator) → Python pre-checks → DeepSeek-R1 (reviewer), up to 3 iterations per segment.

Relevant files:
- `translate/prechecks.py` — deterministic pre-checks (structure + terminology)
- `translate/reviewer.py` — R1 call + `_parse_verdict`
- `translate/loop.py` — orchestration loop
- `prompts/reviewer_system.txt` — R1 system prompt
- `prompts/translator_system.txt` — V3 system prompt

---

## Prompt Files

### `prompts/translator_system.txt` — REPLACE IN FULL

```
You are translating Thomas Aquinas's Summa Theologiae from Scholastic Latin into Slovak.

FORMATTING:
  Author names in running text → SMALL CAPS (e.g. AUGUSTÍN, ARISTOTELES)
  Work titles → italics (e.g. *O Trojici*, *Fyzika*)
  Scripture citations → standard abbreviated form with chapter:verse
    (e.g. Sir 3:22, Iz 64:4, Rim 1:19)

DO NOT:
  - Raise the literary quality above the original — do not improve Aquinas's style
  - Vary vocabulary for stylistic effect — if Aquinas repeats a word, repeat it
  - Modernise scholastic connectives (totiž, teda, avšak, lebo, preto must be preserved)
  - Split or merge sentences — one Latin sentence produces exactly one Slovak sentence

LEGIBILITY: The Slovak output must be at least as legible as the provided Czech reference.
  Recast confusing phrases natively while preserving exact Scholastic sentence boundaries.

GRAMMAR — Latin passive infinitives:
  Do not calque Latin passive infinitives (haberi, tradi, esse + passive) literally.
  Render them with natural Slovak existential or stative verbs (byť, existovať, nachádzať sa).
  WRONG: aliam doctrinam haberi → aby sa mala aj iná náuka
  RIGHT: aliam doctrinam haberi → aby existovala aj iná náuka
  WRONG: sufficienter traduntur → je dostatočne odovzdané
  RIGHT: sufficienter traduntur → je dostatočne podané / rozoberané
```

---

### `prompts/reviewer_system.txt` — REPLACE IN FULL

```
You are a Thomistic scholar reviewing a Slovak translation of Aquinas's Summa Theologiae.

Structure and hard terminology have already passed automated pre-checks.
Evaluate SEMANTICS and LEGIBILITY only.

SEMANTICS (Requires REVISION_NEEDED):
  - Argument direction reversed (affirmative → negative or vice versa)
  - Categorical statement weakened to conditional or possibility
  - Modal collapse (necessarium → možné, debet → môže, etc.)
  - Malformed grammar that obscures meaning

LEGIBILITY (Requires APPROVED_WITH_NOTES):
  - Flag phrasing that is unusually difficult to parse or structurally un-Slovak.
  - Never block approval for legibility alone — handled in a later polish phase.

<evaluation>
Semantics: [analysis]
Legibility: [analysis]
Notes: [bulleted list or "none"]
</evaluation>

<verdict>
[EXACTLY ONE: APPROVED | APPROVED_WITH_NOTES: <notes> | REVISION_NEEDED: <revisions>]
</verdict>
```

---

## Bug 1 — `check_terminology` is dead code (CRITICAL)

**Root cause of the observed seg-205 failure** (`scientia → vedomosti` instead of `poznanie`).

**Location:** `prechecks.py:156–190`, `loop.py:19`

`check_terminology()` exists and is structurally correct but is **never imported or called** in `loop.py`. Only `check_structure` is wired into the loop. The existing function uses full-string normalised matching which fails on declined Slovak forms — this is why it was left unwired — but a stem-prefix check is sufficient and requires no external dependency.

**Fix:** Add `check_terminology_lemma()` in `prechecks.py` using the existing MorphoDiTa
infrastructure already in `common/lemmatize.py` — Slovak support is a near-zero-effort
extension of the Czech model already in use.

**Why not a stem/regex approach:** Any fixed-length slice (`[:5]`) still fails on irregular
paradigms — `viera[:5]` = `viera` does not match `vierou` with simple substring search, and
a word-boundary regex `\bviera` still won't match `vierou`. The only correct solution for a
morphologically rich language is proper lemmatization. MorphoDiTa achieves 96.3% lemma
accuracy on Slovak PDT data.

**Model to add to `scripts/download_models.py`:**
```
slovak-morfflex-pdt-170914   http://hdl.handle.net/11234/1-3278
```
File to download: `slovak-morfflex-pdt-170914.dict` (~17MB), placed under `models/`.
License: CC BY-NC-SA (same as Czech model already in use).

**`common/lemmatize.py` changes** (see `lemmatize.py` output file — full replacement):

The Czech-specific `_czech_morpho()` and `lemmatize_czech()` are refactored to share a
`_load_morpho(glob, label)` helper and `_extract_lemmas(morpho, surface)` helper. Slovak
is then a two-function addition mirroring Czech exactly:

```python
_SLOVAK_DICT_GLOB = "slovak-morfflex*.dict"

@functools.lru_cache(maxsize=1)
def _slovak_morpho():
    return _load_morpho(_SLOVAK_DICT_GLOB, "Slovak")

def lemmatize_slovak(surface: str) -> list[str]:
    """Return candidate lemmas for a Slovak surface form.
    Examples:
        lemmatize_slovak('vierou')    -> ['viera']
        lemmatize_slovak('rozumu')    -> ['rozum']
        lemmatize_slovak('poznaniu')  -> ['poznanie']
    """
    return _extract_lemmas(_slovak_morpho(), surface)
```

**`prechecks.py` — add `check_terminology_lemma()`:**

```python
import re as _re
from common.lemmatize import lemmatize_slovak

def check_terminology_lemma(draft: str, constraints: list[dict]) -> CheckResult:
    """Lemma-exact terminology check using MorphoDiTa Slovak model.

    Tokenizes the draft, lemmatizes every token, and checks that each
    required_slovak lemma appears in the resulting lemma set.

    Zero false negatives on declension (vierou → viera ✓).
    Zero false positives from substring containment (forma ≠ informácia ✓).

    NOTE: multi-word constraints (e.g. "prvotná matéria") are not supported by
    the current glossary schema (latin_lemma is always a single token). If that
    changes, replace the flat-set membership check with issubset over the phrase's
    individual lemmas — adjacency is not enforced but is an acceptable trade-off
    for a fast pre-check gate.
    """
    if not constraints:
        return CheckResult(ok=True)

    # Tokenize: split on anything that isn't a Slovak word character.
    tokens = _re.findall(r"[^\W\d_]+", draft, flags=_re.UNICODE)

    # Build lowercase lemma set — MorphoDiTa preserves capitalisation (Boh, nie boh),
    # so normalise both sides to avoid false negatives on proper nouns in constraints.
    draft_lemmas: set[str] = set()
    for token in tokens:
        draft_lemmas.update(l.lower() for l in lemmatize_slovak(token))

    failures: list[str] = []
    for c in constraints:
        required = c["required_slovak"].lower()
        if required not in draft_lemmas:
            msg = f"lemma '{c['required_slovak']}' (for {c['latin_lemma']}) not found in draft"
            print(f"[PRECHECK] terminology FAIL: {msg}", file=sys.stderr)
            failures.append(msg)

    return CheckResult(ok=len(failures) == 0, failures=failures)
```

**`loop.py` — wire in alongside `check_structure`:**

```python
from translate.prechecks import check_structure, check_terminology_lemma

structure_result = check_structure(seg, draft, conn)
terminology_result = check_terminology_lemma(draft, constraints)

if not structure_result.ok or not terminology_result.ok:
    all_failures = structure_result.failures + terminology_result.failures
    prior_feedback = "Pre-check failures — fix before R1 review:\n" + "\n".join(
        f"  - {f}" for f in all_failures
    )
    if prompt_log:
        prompt_log.log_iteration(
            segment_id=segment_id,
            locator_path=locator,
            iteration=iteration,
            system_prompt=system_prompt,
            user_turn=user_turn,
            draft=draft,
            precheck_ok=False,
            precheck_failures=all_failures,
            reviewer_turn=None,
            verdict=None,
            feedback=prior_feedback,
        )
    prior_draft = draft
    if fallback_draft is None:
        fallback_draft = draft
        fallback_iter = iteration
    continue
```

---

## Bug 2 — Empty notes silently accepted on `APPROVED_WITH_NOTES` (HIGH)

**Location:** `reviewer.py:139–146`, `loop.py:298–315`

When R1 emits `APPROVED_WITH_NOTES:` with nothing after the colon and no subsequent lines,
`notes_text` is `""`. `ReviewResult(notes={"raw": ""})` is truthy, so `write_reviewer_notes`
is called with empty data and the segment is marked translated. Observed in seg-205 where
`feedback` was `null` in the JSONL log. Fixed as part of Bug 3 below (both touch `_parse_verdict`).

---

## Bug 3 — `_parse_verdict` only checks first line (MEDIUM)

**Location:** `reviewer.py:133`

```python
first_line = content.split("\n")[0].strip()
```

R1 sometimes emits reasoning/preamble before the verdict line. When it does, `first_line`
matches nothing, `_parse_verdict` raises `RuntimeError`, the loop catches it at line 280
and `break`s — the segment exhausts its iterations and is flagged `needs_human` even when
a valid verdict exists later in the output.

**Fix — replace `_parse_verdict` entirely (also resolves Bug 2):**

Scan from the **bottom up** (`reversed(lines)`) to avoid matching a verdict keyword that
R1 writes hypothetically inside its chain-of-thought reasoning block (e.g. *"if the argument
were reversed I would output REVISION_NEEDED..."*). The real verdict is always the last
occurrence. Also extract from `<verdict>` XML tags first, since the updated reviewer prompt
uses them — falling back to line scanning for any response that doesn't use tags.

```python
import re as _re

def _parse_verdict(content: str) -> ReviewResult:
    """Extract verdict from R1 output.

    Strategy (in order):
    1. Look for <verdict>...</verdict> XML tags (preferred — reviewer prompt uses them).
    2. Fall back to bottom-up line scan — finds the LAST occurrence of a verdict keyword,
       which avoids false matches on hypothetical verdict text in R1's chain-of-thought.
    """
    # ── Strategy 1: XML tags ──────────────────────────────────────────────────
    xml_match = _re.search(r"<verdict>\s*(.*?)\s*</verdict>", content, _re.DOTALL)
    if xml_match:
        return _parse_verdict_text(xml_match.group(1).strip(), "")

    # ── Strategy 2: bottom-up line scan ──────────────────────────────────────
    lines = content.splitlines()
    for i, line in enumerate(reversed(lines)):
        line = line.strip()
        # rest = lines after this one in original order (content below verdict)
        rest = "\n".join(lines[len(lines) - i:]).strip()
        result = _parse_verdict_text(line, rest)
        if result is not None:
            return result

    raise RuntimeError(f"No verdict found in R1 output: {content[:200]!r}")


def _parse_verdict_text(line: str, rest: str) -> ReviewResult | None:
    """Parse a single candidate verdict line. Returns None if line is not a verdict."""
    # APPROVED must match as a standalone word — not as a prefix of APPROVED_WITH_NOTES.
    # Use a word-boundary check to handle bullet-prefixed lines like "* APPROVED".
    if _re.search(r"\bAPPROVED\b", line) and "APPROVED_WITH_NOTES" not in line:
        return ReviewResult(verdict="APPROVED", notes=None, feedback=None)

    if "APPROVED_WITH_NOTES:" in line:
        after_colon = line.split("APPROVED_WITH_NOTES:", 1)[1].strip()
        notes_text = (after_colon + ("\n" + rest if rest else "")).strip()
        if not notes_text:
            raise RuntimeError(
                "APPROVED_WITH_NOTES emitted without note content — treating as parse failure"
            )
        return ReviewResult(
            verdict="APPROVED_WITH_NOTES",
            notes={"raw": notes_text},
            feedback=None,
        )

    if "REVISION_NEEDED:" in line:
        after_colon = line.split("REVISION_NEEDED:", 1)[1].strip()
        feedback_text = (after_colon + ("\n" + rest if rest else "")).strip()
        return ReviewResult(
            verdict="REVISION_NEEDED",
            notes=None,
            feedback=feedback_text,
        )

    return None
```

---

## Bug 4 — R1 system prompt asks it to do Axes 1 & 2 (MEDIUM)

**Location:** `prompts/reviewer_system.txt`

Axis 1 (structure counting) and Axis 2 (verbatim term checking) are now handled by Python
pre-checks. Keeping them in the R1 prompt wastes token budget and creates false confidence —
R1 approved the seg-205 terminology failure precisely because it "checked" Axis 2 loosely
and found `poznanie` elsewhere in the draft.

**Fix:** Replaced in the Prompt Files section above.

---

## Bug 5 — `best_draft` naming misleads; stores precheck-failing drafts (LOW)

**Location:** `loop.py:211–216, 260–262`

```python
if best_draft is None:
    best_draft = draft        # ← this draft FAILED the precheck
    best_draft_iteration = iteration
```

The variable is named `best_draft` and documented as "last draft that cleared pre-checks",
but on iteration 1 precheck failure it stores a precheck-failing draft. This won't cause
incorrect output (it's a last resort), but it causes `_iteration_count` in `pilot.py` to
misreport since `reviewer_notes` is never written for precheck-only failure paths.

**Fix — replace the four sentinel declarations at the top of the loop:**

```python
# loop.py — replace lines 211–216
precheck_passing_draft: str | None = None   # last draft that cleared ALL pre-checks
precheck_passing_iter: int | None = None
fallback_draft: str | None = None           # any draft produced; absolute last resort
fallback_iter: int | None = None
```

**Replace the exhaustion block (after the for loop):**

```python
final_draft = precheck_passing_draft if precheck_passing_draft is not None else fallback_draft
chosen_iter = precheck_passing_iter if precheck_passing_iter is not None else fallback_iter
```

**Inside the loop, when pre-checks pass, set:**
```python
precheck_passing_draft = draft
precheck_passing_iter = iteration
```

**Inside the loop, always set fallback on every new draft:**
```python
last_draft = draft          # keep existing last_draft for the latin check guard
fallback_draft = draft
fallback_iter = iteration
```

---

## Implementation order

1. **Prompts** — replace both `translator_system.txt` and `reviewer_system.txt` (zero code risk)
2. **Bug 1** — add `lemmatize_slovak` to `common/lemmatize.py` (model download first), then add `check_terminology_lemma` to `prechecks.py` + wire into `loop.py` (also update sentinel names per Bug 5 in the same pass since the loop block is being touched)
3. **Bug 3 + 2** — replace `_parse_verdict` in `reviewer.py` (single function, both bugs resolved together)
4. **Bug 5** — remaining sentinel renames in `loop.py` not already touched in step 2

---

## Validation

After fixes, re-run `translate.pilot` in debug mode (`_DEBUG_LIMIT = 10`) against Q1.
Confirm in the JSONL log:

- `lemmatize_slovak('vierou')` returns `['viera']` before running the pilot (smoke-test the model)
- Seg-205 `sed_contra`: `precheck_ok: false` fires on terminology before R1 is called
- No `APPROVED_WITH_NOTES` records with `feedback: null`
- R1 responses with reasoning preamble still yield a parsed verdict (check any seg where R1 output is long)
- Seg-187 `arg1` on re-run: draft should no longer contain `mala aj iná náuka`; expect `existovala` or `bola`