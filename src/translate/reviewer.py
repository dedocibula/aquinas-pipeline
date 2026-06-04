"""DeepSeek R1 reviewer agent for Slovak translation quality control."""

from __future__ import annotations

import os
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

from common.pricing import UsageInfo, extract_usage

load_dotenv()

_DEEPSEEK_URL = os.environ.get(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions"
)
_DEEPSEEK_R1_MODEL = os.environ.get("DEEPSEEK_R1_MODEL", "deepseek-reasoner")

_SYSTEM_PROMPT = """\
You are a quality reviewer for a Slovak translation of Aquinas's Summa Theologiae.
Evaluate against four axes. Verdict must be one of three options (exact strings).

AXIS 1 — STRUCTURE
Count objections in Latin; confirm same count in draft.
Check: sed_contra and respondeo markers present where applicable.
A missing structural element is always FAIL.

AXIS 2 — TERMINOLOGY
Each required term must appear verbatim in the draft.
A missing required term is always FAIL.

AXIS 3 — SEMANTICS
Does the Slovak convey the logical argument faithfully?
MAJOR failure: argument direction changes, conditional replaces categorical,
  modal distinctions collapse. → REVISION NEEDED
MINOR imprecision: slightly loose rendering, no argument change. → note only, not FAIL.

AXIS 4 — REGISTER
Flag colloquialisms, modern idioms, restructured sentences.
Register issues → notes only, never FAIL (handled in M5 polish).

OUTPUT FORMAT — respond with exactly one of:
  APPROVED
  APPROVED_WITH_NOTES: <bulleted advisory items>
  REVISION_NEEDED: <bulleted required changes — structure/terminology/major-semantic only>\
"""


@dataclass
class ReviewResult:
    verdict: str              # 'APPROVED' | 'APPROVED_WITH_NOTES' | 'REVISION_NEEDED'
    notes: dict | None        # structured notes when APPROVED_WITH_NOTES; None otherwise
    feedback: str | None      # revision instructions when REVISION_NEEDED; None otherwise
    usage: UsageInfo | None = None  # token counts and cost for this R1 call


def call_reviewer_r1(
    latin: str,
    draft: str,
    constraints: list[dict],    # [{latin_lemma, required_slovak}]
) -> ReviewResult:
    """Call DeepSeek R1 to review a Slovak translation draft.

    Args:
        latin: The original Latin segment text.
        draft: The Slovak translation draft to review.
        constraints: List of required term constraints, each with
                     'latin_lemma' and 'required_slovak' keys.

    Returns:
        A ReviewResult with the verdict and any notes or feedback.

    Raises:
        RuntimeError: If DEEPSEEK_API_KEY is not set, the API returns an
                      error status, or the verdict cannot be parsed.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. "
            "Export it before running the reviewer."
        )

    # Build per-segment user turn (not cached)
    term_lines = "\n".join(
        f"  {c['latin_lemma']} → {c['required_slovak']}"
        for c in constraints
    )
    user_content = (
        f"REQUIRED TERMS:\n{term_lines}\n\n"
        f"LATIN:\n{latin}\n\n"
        f"DRAFT:\n{draft}"
    )

    try:
        resp = requests.post(
            _DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": _DEEPSEEK_R1_MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.0,
                "max_tokens": 1024,
            },
            timeout=60,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"DeepSeek R1 network error: {exc}") from exc

    if resp.status_code >= 400:
        raise RuntimeError(
            f"DeepSeek R1 API error (HTTP {resp.status_code}) — "
            "check DEEPSEEK_API_KEY and account credits."
        )

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(
            "DeepSeek R1 returned no choices — API may have filtered the response."
        )
    content = choices[0]["message"]["content"].strip()

    result = _parse_verdict(content)
    result.usage = extract_usage(_DEEPSEEK_R1_MODEL, data)
    return result


def _parse_verdict(content: str) -> ReviewResult:
    """Parse the model response into a ReviewResult.

    Raises:
        RuntimeError: If the first line does not match any known verdict prefix.
    """
    first_line = content.split("\n")[0].strip()
    rest = content[len(first_line):].strip()

    if first_line == "APPROVED":
        return ReviewResult(verdict="APPROVED", notes=None, feedback=None)

    if first_line.startswith("APPROVED_WITH_NOTES:"):
        after_colon = first_line[len("APPROVED_WITH_NOTES:"):].strip()
        notes_text = (after_colon + ("\n" + rest if rest else "")).strip()
        return ReviewResult(
            verdict="APPROVED_WITH_NOTES",
            notes={"raw": notes_text},
            feedback=None,
        )

    if first_line.startswith("REVISION_NEEDED:"):
        after_colon = first_line[len("REVISION_NEEDED:"):].strip()
        feedback_text = (after_colon + ("\n" + rest if rest else "")).strip()
        return ReviewResult(
            verdict="REVISION_NEEDED",
            notes=None,
            feedback=feedback_text,
        )

    raise RuntimeError(
        f"Unrecognised reviewer verdict: {first_line!r} — "
        "segment must be re-reviewed"
    )
