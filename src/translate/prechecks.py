"""Pre-checks that run before handing a draft to the R1 reviewer.

One deterministic check — no LLM involved:
  check_terminology_lemma — verifies required Slovak term constraints appear in draft

Returns a CheckResult and never raises.  Failures are logged to stderr with a
[PRECHECK] prefix so they are easy to grep.
"""

from __future__ import annotations

import re as _re
import sys
import unicodedata
from dataclasses import dataclass, field

from dotenv import load_dotenv

from common.lemmatize import generate_slovak_forms

load_dotenv()

# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    ok: bool
    failures: list[str] = field(default_factory=list)  # human-readable failure descriptions
    failed_terms: list[str] = field(default_factory=list)  # required_slovak of unmet constraints


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
    if forms and forms & draft_tokens:
        return True
    # Stem-prefix fallback — covers both OOV lemmas ('čnosť', 'habitus') AND
    # MorfFlex coverage gaps (e.g. 'pamäť' generates only {'pamäti'}, missing
    # 'pamäťou', 'pamätiam', etc.). Always applied as a second-chance check.
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
            # Strip trailing punctuation before building the pattern: a regex
            # \b after re.escape("takto.") would require the next char to be \w,
            # but sentence-ending periods are always followed by a space — the
            # match would never fire. Stripping lets "takto." match "takto. " and
            # "takto:" and "takto," without losing the leading \b anchor.
            req_norm = _normalise(required).rstrip(".,;:!?")
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
