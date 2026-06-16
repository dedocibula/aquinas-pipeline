"""DeepSeek V3 translator for Aquinas Summa Theologiae segments.

Builds a multi-message prompt so the system message is eligible for DeepSeek
prompt caching on iteration 1.  On retry iterations the caller appends
[assistant, user] turns to the messages list before calling again — the model
now sees its own prior draft as a real assistant turn and the feedback as a
real user correction, rather than both flattened into one big user message.
"""

from __future__ import annotations

import functools
import os
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

from common.deepseek_client import DeepSeekClient
from common.pricing import UsageInfo

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

_DEEPSEEK_URL = os.environ.get(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions"
)
_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
TRANSLATOR_TEMPERATURE = 0.3  # also recorded in translation_run for run comparison

_client = DeepSeekClient(_DEEPSEEK_MODEL, url=_DEEPSEEK_URL, timeout=60)

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
    messages: list[dict],
) -> tuple[str, UsageInfo]:
    """Call DeepSeek V3 with the given messages list and return (draft, usage).

    The caller is responsible for building the messages list.  For a first-pass
    translation use build_initial_messages(); for retries append
    [{"role": "assistant", ...}, {"role": "user", ...}] to the same list.

    Raises:
        RuntimeError: On missing API key, 4xx/5xx HTTP errors, or empty response.
    """
    result = _client.chat(
        messages,
        temperature=TRANSLATOR_TEMPERATURE,
        max_tokens=2048,
    )
    draft = result.content.strip()
    if not draft:
        raise RuntimeError("DeepSeek translator returned empty content.")
    return draft, result.usage


# ── Prompt builders ───────────────────────────────────────────────────────────

def build_initial_user_turn(
    seg: dict,
    constraints: list[dict],
) -> str:
    """Build the first user message for a translation request.

    Contains hard term constraints, Czech/English references, and the Latin
    source.  Does not include prior drafts or feedback — those become
    separate assistant/user turns in the messages list on retries.
    """
    parts: list[str] = []

    # Hard term constraints via XML.
    # Group surface-form entries by (required_slovak, context_label) so that
    # e.g. 'virtute → čnosť' and 'virtutem → čnosť' collapse into one line.
    # This prevents the model from seeing the same Slovak obligation many times,
    # which inflates prompt length and dilutes attention on each constraint.
    parts.append("<hard_constraints>")
    if constraints:
        grouped: dict[tuple, list[str]] = defaultdict(list)
        for c in constraints:
            key = (c["required_slovak"], c.get("context_label") or "")
            grouped[key].append(c["latin_lemma"])
        for (required_slovak, context_label), latin_forms in grouped.items():
            latin = ", ".join(sorted(set(latin_forms)))
            qualifier = f' context="{context_label}"' if context_label else ""
            parts.append(f'  <term latin="{latin}" required_slovak="{required_slovak}"{qualifier} />')
        parts.append("</hard_constraints>")
        parts.append(
            "\n⚠ CRITICAL: The terms in <hard_constraints> are compiler locks. "
            "They must appear exactly as required, inflected for Slovak grammar. "
            "No synonyms are permitted; failure results in immediate machine rejection."
        )
    else:
        parts.append("  \n</hard_constraints>")
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

    # Source text — use Latin when available, fall back to English for title segments
    latin = seg.get("latin") or ""
    if latin:
        parts.append("Translate this Latin segment:")
        parts.append(latin)
    else:
        parts.append("Translate this English title to Slovak (no Latin source available):")
        parts.append(seg.get("english") or "")

    return "\n".join(parts)


def build_initial_messages(seg: dict, constraints: list[dict]) -> list[dict]:
    """Build the initial [system, user] messages list for a first-pass translation."""
    return [
        {"role": "system", "content": load_translator_system_prompt()},
        {"role": "user", "content": build_initial_user_turn(seg, constraints)},
    ]
