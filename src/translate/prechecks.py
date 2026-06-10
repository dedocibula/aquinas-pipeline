"""Pre-checks that run before handing a draft to the R1 reviewer.

Two deterministic checks — no LLM involved:
  check_structure  — verifies structural formula markers are present/absent
  check_terminology — verifies required Slovak term constraints appear in draft

Both functions return a CheckResult and never raise.  Failures are logged to
stderr with a [PRECHECK] prefix so they are easy to grep.
"""

from __future__ import annotations

import re as _re
import sys
import unicodedata
from dataclasses import dataclass, field

import psycopg2.extras
from dotenv import load_dotenv

from common.lemmatize import generate_slovak_forms

load_dotenv()

# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    ok: bool
    failures: list[str] = field(default_factory=list)  # human-readable failure descriptions
    failed_terms: list[str] = field(default_factory=list)  # required_slovak of unmet constraints


# ── Module-level formula cache ─────────────────────────────────────────────────
# Keyed by latin_lemma → Slovak form string.
# Populated on first call to check_structure; cleared by tests via _clear_formula_cache().

_formula_cache: dict[str, str] = {}


def _clear_formula_cache() -> None:
    """Reset the formula cache. Intended for use in tests only."""
    _formula_cache.clear()


# ── Formula DB query ──────────────────────────────────────────────────────────

_FORMULA_SQL = """
SELECT gt.latin_lemma, sr.content AS slovak_form
FROM glossary_term gt
JOIN glossary_sense gs USING (term_id)
JOIN sense_rendering sr ON sr.sense_id = gs.sense_id AND sr.lang = 'sk'
WHERE gt.latin_lemma IN ('respondeo', 'sed_contra', 'praeterea')
  AND gs.status = 'approved'
"""

_FORMULA_LEMMAS = ("respondeo", "sed_contra", "praeterea")


def _load_formulas(db_conn) -> None:
    """Populate _formula_cache from DB if not already loaded."""
    if _formula_cache:
        return
    try:
        with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_FORMULA_SQL)
            rows = cur.fetchall()
    except Exception as exc:  # pragma: no cover
        print(f"[PRECHECK] ERROR: failed to load structural formulas from DB: {exc}", file=sys.stderr)
        return

    for row in rows:
        _formula_cache[row["latin_lemma"]] = row["slovak_form"]

    for lemma in _FORMULA_LEMMAS:
        if lemma not in _formula_cache:
            print(
                f"[PRECHECK] WARNING: no approved formula for '{lemma}' — skipping check",
                file=sys.stderr,
            )


# ── check_structure ───────────────────────────────────────────────────────────

def check_structure(seg: dict, draft: str, db_conn) -> CheckResult:
    """Check that a translated draft contains (or correctly omits) structural
    formula markers, as required by the segment's element_type.

    Args:
        seg:     v_segment row dict with keys: segment_id, locator_path, element_type.
        draft:   Slovak translation draft.
        db_conn: Live psycopg2 connection (read-only; no commit performed here).

    Returns:
        CheckResult with ok=True if all applicable checks pass.
    """
    segment_id = seg["segment_id"]
    element_type = seg["element_type"]
    failures: list[str] = []

    # Load formulas on first call (module-level cache).
    _load_formulas(db_conn)

    if element_type == "arg":
        # No structural formula check needed for objection segments.
        return CheckResult(ok=True)

    if element_type == "sed_contra":
        formula = _formula_cache.get("sed_contra")
        if formula is None:
            # Warning already printed inside _load_formulas; skip check.
            pass
        else:
            if formula not in draft:
                reason = f"expected sed_contra formula '{formula}' not found in draft"
                print(
                    f"[PRECHECK] segment_id={segment_id} element_type={element_type} FAIL: {reason}",
                    file=sys.stderr,
                )
                failures.append(reason)

    elif element_type == "respondeo":
        formula = _formula_cache.get("respondeo")
        if formula is None:
            pass
        else:
            if formula not in draft:
                reason = f"expected respondeo formula '{formula}' not found in draft"
                print(
                    f"[PRECHECK] segment_id={segment_id} element_type={element_type} FAIL: {reason}",
                    file=sys.stderr,
                )
                failures.append(reason)

    elif element_type == "reply":
        formula = _formula_cache.get("respondeo")
        if formula is None:
            pass
        else:
            if formula in draft:
                reason = f"respondeo formula '{formula}' must NOT appear in reply drafts"
                print(
                    f"[PRECHECK] segment_id={segment_id} element_type={element_type} FAIL: {reason}",
                    file=sys.stderr,
                )
                failures.append(reason)

    return CheckResult(ok=len(failures) == 0, failures=failures)


# ── check_terminology_lemma ───────────────────────────────────────────────────

def _oov_stem(word: str) -> str:
    """Derive a normalised stem prefix for a lemma MorphoDiTa cannot generate.

    Latin loans decline without their -us ending (habitus → habitu/habitom),
    so it is stripped. Otherwise trailing vowels are inflectional endings.
    Stems shorter than 3 characters are too prefix-happy; keep the full word.
    """
    w = _normalise(word)
    if w.endswith("us") and len(w) >= 5:
        stem = w[:-2]
    else:
        stem = w.rstrip("aeiouy")
    return stem if len(stem) >= 3 else w


def _word_in_draft(word: str, draft_tokens: set[str], draft_tokens_norm: set[str]) -> bool:
    """True if any inflected form of `word` appears among the draft's tokens."""
    w = word.lower()
    if w in draft_tokens:
        return True
    forms = generate_slovak_forms(w)
    if forms:
        return bool(forms & draft_tokens)
    # OOV lemma (archaic 'čnosť', loan 'habitus'): stem-prefix match.
    stem = _oov_stem(w)
    return any(t.startswith(stem) for t in draft_tokens_norm)


def check_terminology_lemma(draft: str, constraints: list[dict]) -> CheckResult:
    """Generation-based terminology check using the MorphoDiTa Slovak model.

    Dispatches on constraint category:

    formula — word-boundary regex on normalised text (bypasses morphology,
        which cannot handle prepositional phrases like 'o sebe').
        Word boundaries prevent 'po sebe'/'vo sebe' satisfying 'o sebe'.

    term / name / prose / None (Krystal-seeded) — per-word form-set matching.
        For each word of required_slovak, MorphoDiTa *generates* the closed
        set of inflected forms (reliable for in-dictionary lemmas) and the
        word is satisfied when any draft token is in that set. The draft is
        never analysed — analysis is open-vocabulary, exactly where MorfFlex
        SK has gaps, and it false-failed correct inflections ('čnostiam').
        OOV lemmas fall back to a normalised stem-prefix match.
        Adjacency of multi-word constraints is not enforced — acceptable for
        a fast gate; the R1 reviewer sees the full constraints.
    """
    if not constraints:
        return CheckResult(ok=True)

    draft_tokens = {
        t.lower() for t in _re.findall(r"[^\W\d_]+", draft, flags=_re.UNICODE)
    }
    draft_tokens_norm = {_normalise(t) for t in draft_tokens}

    failures: list[str] = []
    failed_terms: list[str] = []
    for c in constraints:
        required = c["required_slovak"]
        category = c.get("category") or "term"

        if category == "formula":
            # Fixed phrases: word-boundary regex on normalised text.
            # \b prevents 'po sebe'/'vo sebe' false-positives for 'o sebe'.
            req_norm = _normalise(required)
            draft_norm = _normalise(draft)
            if not _re.search(rf"\b{_re.escape(req_norm)}\b", draft_norm):
                msg = f"formula '{required}' (for {c['latin_lemma']}) not found in draft"
                print(f"[PRECHECK] terminology FAIL: {msg}", file=sys.stderr)
                failures.append(msg)
                failed_terms.append(required)
        else:
            # term / name / prose: every word must appear in some inflected form.
            required_words = _re.findall(r"[^\W\d_]+", required, flags=_re.UNICODE)
            missing_words = [
                word
                for word in required_words
                if not _word_in_draft(word, draft_tokens, draft_tokens_norm)
            ]
            if missing_words:
                msg = f"missing components {missing_words} for '{required}' ({c['latin_lemma']})"
                print(f"[PRECHECK] terminology FAIL: {msg}", file=sys.stderr)
                failures.append(msg)
                failed_terms.append(required)

    return CheckResult(ok=len(failures) == 0, failures=failures, failed_terms=failed_terms)


# ── check_terminology ─────────────────────────────────────────────────────────

def _normalise(s: str) -> str:
    """Lowercase and strip diacritics for loose containment comparison."""
    s = s.lower()
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()
    return s


def check_terminology(draft: str, constraints: list[dict]) -> CheckResult:
    """Check that all hard term constraints appear in the draft (exact normalised match).

    NOTE: This check is NOT wired into the translation loop's pre-check gate.
    Slovak is highly inflected and exact matching rejects grammatically correct
    declined forms. Proper enforcement requires a morphological analyser
    (MorphoDiTa); until that is implemented, terminology compliance is delegated
    entirely to the R1 reviewer which receives the full constraints in its prompt.

    This function is kept for future use and for direct diagnostic calls.

    Args:
        draft:       Slovak translation draft.
        constraints: List of {latin_lemma: str, required_slovak: str} dicts.

    Returns:
        CheckResult with ok=True if every required_slovak form is found in draft.
    """
    if not constraints:
        return CheckResult(ok=True)

    normalised_draft = _normalise(draft)
    failures: list[str] = []

    for c in constraints:
        latin_lemma = c["latin_lemma"]
        required_slovak = c["required_slovak"]
        if _normalise(required_slovak) not in normalised_draft:
            print(
                f"[PRECHECK] terminology FAIL: '{required_slovak}' (for {latin_lemma}) not found in draft",
                file=sys.stderr,
            )
            failures.append(f"'{required_slovak}' (for {latin_lemma}) not found in draft")

    return CheckResult(ok=len(failures) == 0, failures=failures)
