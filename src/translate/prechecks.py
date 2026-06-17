"""Pre-checks that run before handing a draft to the R1 reviewer.

One deterministic check — no LLM involved:
  check_terminology_lemma — verifies required Slovak term constraints appear in draft

Returns a CheckResult and never raises.  Failures are logged to stderr with a
[PRECHECK] prefix so they are easy to grep.
"""

from __future__ import annotations

import re as _re
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv

from common.lemmatize import SlovakTermMatcher, generate_slovak_forms, normalise

load_dotenv()

# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    ok: bool
    failures: list[str] = field(default_factory=list)  # human-readable failure descriptions
    failed_terms: list[str] = field(default_factory=list)  # required_slovak of unmet constraints


# ── check_terminology_lemma ───────────────────────────────────────────────────

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

    # Build the matcher per call so a monkeypatched `generate_slovak_forms`
    # (the unit-test seam) is picked up at call time.
    matcher = SlovakTermMatcher(generate=generate_slovak_forms)

    draft_tokens = {
        t.lower() for t in _re.findall(r"[^\W\d_]+", draft, flags=_re.UNICODE)
    }
    draft_tokens_norm = {normalise(t) for t in draft_tokens}

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
            req_norm = normalise(required).rstrip(".,;:!?")
            draft_norm = normalise(draft)
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
                if not matcher.matches(word, draft_tokens, draft_tokens_norm)
            ]
            if missing_words:
                msg = f"missing components {missing_words} for '{required}' ({c['latin_lemma']})"
                print(f"[PRECHECK] terminology FAIL: {msg}", file=sys.stderr)
                failures.append(msg)
                failed_terms.append(required)

    return CheckResult(ok=len(failures) == 0, failures=failures, failed_terms=failed_terms)


# ── check_terminology ─────────────────────────────────────────────────────────

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

    normalised_draft = normalise(draft)
    failures: list[str] = []

    for c in constraints:
        latin_lemma = c["latin_lemma"]
        required_slovak = c["required_slovak"]
        if normalise(required_slovak) not in normalised_draft:
            print(
                f"[PRECHECK] terminology FAIL: '{required_slovak}' (for {latin_lemma}) not found in draft",
                file=sys.stderr,
            )
            failures.append(f"'{required_slovak}' (for {latin_lemma}) not found in draft")

    return CheckResult(ok=len(failures) == 0, failures=failures)
