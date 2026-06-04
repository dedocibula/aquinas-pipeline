"""DeepSeek V3 translator for Aquinas Summa Theologiae segments.

Builds a two-message prompt (stable system + per-segment user turn) so that the
system message is eligible for DeepSeek prompt caching.  Returns the Slovak draft
text; raises RuntimeError loudly on all failure modes.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

from common.pricing import UsageInfo, extract_usage

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

_DEEPSEEK_URL = os.environ.get(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions"
)
_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_STYLE_PROFILE_PATH = _PROJECT_ROOT / "style_profile.yaml"


# ── Public API ────────────────────────────────────────────────────────────────

def load_style_profile(path: str | None = None) -> dict:
    """Load and return the style_profile.yaml as a dict.

    Defaults to the project-root style_profile.yaml.  Raises RuntimeError if the
    file is missing or cannot be parsed.
    """
    target = Path(path) if path is not None else _STYLE_PROFILE_PATH
    if not target.exists():
        raise RuntimeError(
            f"style_profile.yaml not found at {target}. "
            "Ensure the file exists at the project root."
        )
    with target.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise RuntimeError(
            f"style_profile.yaml at {target} did not parse as a mapping."
        )
    return data


def call_translator_v3(
    seg: dict,
    constraints: list[dict],
    prior_draft: str | None,
    prior_feedback: str | None,
    style_profile: dict,
) -> tuple[str, UsageInfo]:
    """Call DeepSeek V3 to produce a Slovak translation draft for one segment.

    Args:
        seg: Row from v_segment view — must contain keys: segment_id, locator_path,
             element_type, latin, czech, english.
        constraints: List of {latin_lemma, required_slovak} dicts — hard term constraints.
        prior_draft: Previous Slovak draft, or None for first-pass translation.
        prior_feedback: Reviewer feedback addressing the prior draft, or None.
        style_profile: Loaded style_profile.yaml dict.

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

    system_content = _build_system_prompt(style_profile)
    user_content = _build_user_turn(seg, constraints, prior_draft, prior_feedback)

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


# ── Prompt builders ───────────────────────────────────────────────────────────

def _build_system_prompt(style_profile: dict) -> str:
    """Build the stable (cache-eligible) system prompt from style_profile."""
    orth = style_profile.get("orthography", {})
    prefer = orth.get("prefer", [])
    avoid = orth.get("avoid", [])

    name_forms = style_profile.get("name_forms", {})

    lines = [
        "You are translating Thomas Aquinas's Summa Theologiae from Scholastic Latin into Slovak.",
        "",
        "STYLE RULES:",
    ]

    # Spelling
    if prefer or avoid:
        prefer_str = ", ".join(str(p) for p in prefer) if prefer else "(none)"
        avoid_str = ", ".join(str(a) for a in avoid) if avoid else "(none)"
        lines.append(f"  Spelling    → {prefer_str} (not {avoid_str})")

    # Name forms
    for key, value in name_forms.items():
        lines.append(f"  {key}    → {value}")

    # Negative constraints
    neg = style_profile.get("negative_constraints", [])
    if neg:
        lines.append("")
        lines.append("NEGATIVE CONSTRAINTS:")
        for constraint in neg:
            lines.append(f"  {constraint}")

    return "\n".join(lines)


def _build_user_turn(
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
    if constraints:
        parts.append("HARD TERM CONSTRAINTS (verbatim, no exceptions):")
        for c in constraints:
            parts.append(f"  {c['latin_lemma']} → {c['required_slovak']}")
    else:
        parts.append("HARD TERM CONSTRAINTS (verbatim, no exceptions):")
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
