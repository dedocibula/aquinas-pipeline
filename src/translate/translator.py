"""DeepSeek V3 translator for Aquinas Summa Theologiae segments.

Builds a two-message prompt (stable system + per-segment user turn) so that the
system message is eligible for DeepSeek prompt caching.  Returns the Slovak draft
text; raises RuntimeError loudly on all failure modes.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from common.pricing import UsageInfo, extract_usage

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

_DEEPSEEK_URL = os.environ.get(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions"
)
_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


# ── Public API ────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=None)
def load_translator_system_prompt() -> str:
    """Load (and cache) the translator system prompt from prompts/translator_system.txt."""
    path = _PROMPTS_DIR / "translator_system.txt"
    if not path.exists():
        raise RuntimeError(
            f"translator_system.txt not found at {path}. "
            "Ensure the file exists under the project-root prompts/ directory."
        )
    return path.read_text(encoding="utf-8")


def call_translator_v3(
    seg: dict,
    constraints: list[dict],
    prior_draft: str | None,
    prior_feedback: str | None,
) -> tuple[str, UsageInfo]:
    """Call DeepSeek V3 to produce a Slovak translation draft for one segment.

    Args:
        seg: Row from v_segment view — must contain keys: segment_id, locator_path,
             element_type, latin, czech, english.
        constraints: List of {latin_lemma, required_slovak} dicts — hard term constraints.
        prior_draft: Previous Slovak draft, or None for first-pass translation.
        prior_feedback: Reviewer feedback addressing the prior draft, or None.

    Returns:
        Tuple of (non-empty Slovak draft string, UsageInfo with token counts and cost).

    Raises:
        RuntimeError: On missing API key, 4xx/5xx HTTP errors, empty response.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. "
            "Export it or add it to .env before running the translator."
        )

    system_content = load_translator_system_prompt()
    user_content = build_user_turn(seg, constraints, prior_draft, prior_feedback)

    try:
        resp = requests.post(
            _DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": _DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.3,
                "max_tokens": 2048,
            },
            timeout=60,
        )
        resp.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        raise RuntimeError(
            f"DeepSeek translator HTTP {status} for segment_id={seg.get('segment_id')}."
        ) from exc

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(
            f"DeepSeek translator returned no choices for segment_id={seg.get('segment_id')}."
        )
    draft = choices[0]["message"]["content"].strip()
    if not draft:
        raise RuntimeError(
            f"DeepSeek translator returned empty content for segment_id={seg.get('segment_id')}."
        )
    usage = extract_usage(_DEEPSEEK_MODEL, data)
    return draft, usage


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_user_turn(
    seg: dict,
    constraints: list[dict],
    prior_draft: str | None,
    prior_feedback: str | None,
) -> str:
    """Build the per-segment user turn (not cached)."""
    parts: list[str] = []

    # Revision block — prepended when a prior draft exists
    if prior_draft is not None:
        parts.append("PRIOR DRAFT:")
        parts.append(prior_draft)
        parts.append("")
        parts.append("REVIEWER FEEDBACK — address each point:")
        parts.append(prior_feedback or "")
        parts.append("")
        parts.append("---")
        parts.append("")

    # Hard term constraints
    parts.append("HARD TERM CONSTRAINTS (verbatim, no exceptions):")
    if constraints:
        for c in constraints:
            label = c.get("context_label") or ""
            qualifier = f" [{label}]" if label else ""
            parts.append(f"  {c['latin_lemma']}{qualifier} → {c['required_slovak']}")
    else:
        parts.append("  (none)")
    parts.append("")

    # Czech reference
    czech = seg.get("czech") or "(unavailable)"
    parts.append("CZECH REFERENCE (draft, not authoritative for terms):")
    parts.append(f"  {czech}")
    parts.append("")

    # English reference
    english = seg.get("english") or "(unavailable)"
    parts.append("ENGLISH REFERENCE (semantic anchor):")
    parts.append(f"  {english}")
    parts.append("")

    # Latin source
    parts.append("Translate this Latin segment:")
    parts.append(seg.get("latin", ""))

    return "\n".join(parts)
