"""DeepSeek R1 reviewer agent for Slovak translation quality control."""

from __future__ import annotations

import functools
import os
import re as _re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from common.deepseek_client import DeepSeekClient
from common.pricing import UsageInfo

load_dotenv()

_DEEPSEEK_URL = os.environ.get(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions"
)
_DEEPSEEK_R1_MODEL = os.environ.get("DEEPSEEK_R1_MODEL", "deepseek-reasoner")

# R1 spends its token budget on reasoning + output; give it a long ceiling and timeout.
_client = DeepSeekClient(_DEEPSEEK_R1_MODEL, url=_DEEPSEEK_URL, timeout=150)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


@functools.lru_cache(maxsize=None)
def load_reviewer_system_prompt() -> str:
    """Load (and cache) the reviewer system prompt from prompts/reviewer_system.txt."""
    path = _PROMPTS_DIR / "reviewer_system.txt"
    if not path.exists():
        raise RuntimeError(
            f"reviewer_system.txt not found at {path}. "
            "Ensure the file exists under the project-root prompts/ directory."
        )
    return path.read_text(encoding="utf-8")


@dataclass
class ReviewResult:
    verdict: str              # 'APPROVED' | 'APPROVED_WITH_NOTES' | 'REVISION_NEEDED'
    notes: dict | None        # structured notes when APPROVED_WITH_NOTES; None otherwise
    feedback: str | None      # revision instructions when REVISION_NEEDED; None otherwise
    usage: UsageInfo | None = None  # token counts and cost for this R1 call


def build_reviewer_turn(
    latin: str,
    draft: str,
    constraints: list[dict],
    czech: str | None = None,
    english: str | None = None,
) -> str:
    """Build the per-segment user turn sent to the R1 reviewer."""
    term_lines = "\n".join(
        f"  {c['latin_lemma']} → {c['required_slovak']}"
        for c in constraints
    )
    parts = [f"REQUIRED TERMS:\n{term_lines}", f"LATIN:\n{latin}"]
    if czech:
        parts.append(f"CZECH REFERENCE:\n{czech}")
    if english:
        parts.append(f"ENGLISH REFERENCE:\n{english}")
    parts.append(f"DRAFT:\n{draft}")
    return "\n\n".join(parts)


def call_reviewer_r1(
    latin: str,
    draft: str,
    constraints: list[dict],
    czech: str | None = None,
    english: str | None = None,    # [{latin_lemma, required_slovak}]
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
    user_content = build_reviewer_turn(latin, draft, constraints, czech=czech, english=english)

    chat = _client.chat(
        [
            {"role": "system", "content": load_reviewer_system_prompt()},
            {"role": "user", "content": user_content},
        ],
        temperature=0.0,
        max_tokens=8000,  # R1 reasoning + output share this budget; 1024 was too low
    )

    result = _parse_verdict(chat.content.strip())
    result.usage = chat.usage
    return result


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
        result = _parse_verdict_text(xml_match.group(1).strip(), "")
        if result is not None:
            return result

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
