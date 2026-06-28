"""Guards that validate a polished Slovak draft against the source draft.

Each guard is a pure function that returns a sub-dict.  run_guards() aggregates
all guards into one flags dict with a top-level 'ok' key.

Guards are advisory in the synchronous polish pass (Phase 2/3/5); the batch pass
(Phase 6) skips writing guard-failing segments.

Particle list is the union of both authoritative lists:
  translator_system.txt: totiž, teda, avšak, lebo, preto
  database.md polish note: totiž, teda, však, odtiaľ, ale
"""

from __future__ import annotations

import re

from translate.prechecks import check_terminology_lemma

# Union of both scholastic particle lists — to be preserved across the polish pass.
SCHOLASTIC_PARTICLES: frozenset[str] = frozenset([
    "totiž", "teda", "avšak", "lebo", "preto", "však", "odtiaľ", "ale",
])


def _count_sentences(text: str) -> int:
    """Count sentence-ending punctuation marks followed by whitespace or end-of-string."""
    return len(re.findall(r"[.!?]+(?:\s|$)", text)) or 1


def sentence_count_delta(original: str, polished: str) -> int:
    """Return (polished sentence count) − (original sentence count). Zero is ideal."""
    return _count_sentences(polished) - _count_sentences(original)


def locked_term_retention(polished: str, constraints: list[dict]) -> dict:
    """Check that all locked term constraints survive in the polished draft.

    Delegates to check_terminology_lemma (generation-based Slovak morphology)
    so the same precheck logic applies to both translation and polish.
    """
    if not constraints:
        return {"ok": True, "missing_terms": []}
    result = check_terminology_lemma(polished, constraints)
    return {"ok": result.ok, "missing_terms": result.failed_terms}


def particle_retention(original: str, polished: str) -> dict:
    """Check that scholastic particles present in the original appear in the polished draft.

    Particles are uninflected conjunctions and adverbs — exact token matching
    (lowercased) is sufficient; no lemmatization needed.
    """
    orig_tokens = {t.lower() for t in re.findall(r"[^\W\d_]+", original, flags=re.UNICODE)}
    pol_tokens  = {t.lower() for t in re.findall(r"[^\W\d_]+", polished, flags=re.UNICODE)}
    present_in_orig = SCHOLASTIC_PARTICLES & orig_tokens
    missing = sorted(present_in_orig - pol_tokens)
    return {"ok": not missing, "missing_particles": missing}


def length_ratio(original: str, polished: str) -> float:
    """Return len(polished) / len(original).  1.0 means identical length."""
    if not original:
        return 1.0
    return len(polished) / len(original)


def run_guards(original: str, polished: str, constraints: list[dict]) -> dict:
    """Run all guards and return a single flags dict.

    'ok' is True only when ALL of:
      - sentence_delta == 0 (no sentence added or dropped)
      - term_retention_ok (all locked terms present)
      - particle_retention_ok (all original particles present)
      - 0.5 <= length_ratio <= 2.0 (polished is neither half nor double the original)
    """
    delta  = sentence_count_delta(original, polished)
    terms  = locked_term_retention(polished, constraints)
    parts  = particle_retention(original, polished)
    ratio  = length_ratio(original, polished)
    ok = delta == 0 and terms["ok"] and parts["ok"] and 0.5 <= ratio <= 2.0
    return {
        "ok": ok,
        "sentence_delta": delta,
        "term_retention_ok": terms["ok"],
        "missing_terms": terms["missing_terms"],
        "particle_retention_ok": parts["ok"],
        "missing_particles": parts["missing_particles"],
        "length_ratio": round(ratio, 4),
    }
