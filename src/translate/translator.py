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
    messages: list[dict],
) -> tuple[str, UsageInfo]:
    """Call DeepSeek V3 with the given messages list and return (draft, usage).

    The caller is responsible for building the messages list.  For a first-pass
    translation use build_initial_messages(); for retries append
    [{"role": "assistant", ...}, {"role": "user", ...}] to the same list.

    Raises:
        RuntimeError: On missing API key, 4xx/5xx HTTP errors, or empty response.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. "
            "Export it or add it to .env before running the translator."
        )

    try:
        resp = requests.post(
            _DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": _DEEPSEEK_MODEL,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 2048,
            },
            timeout=60,
        )
        resp.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        raise RuntimeError(
            f"DeepSeek translator HTTP {status}."
        ) from exc

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("DeepSeek translator returned no choices.")
    draft = choices[0]["message"]["content"].strip()
    if not draft:
        raise RuntimeError("DeepSeek translator returned empty content.")
    usage = extract_usage(_DEEPSEEK_MODEL, data)
    return draft, usage


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


def build_initial_messages(seg: dict, constraints: list[dict]) -> list[dict]:
    """Build the initial [system, user] messages list for a first-pass translation."""
    return [
        {"role": "system", "content": load_translator_system_prompt()},
        {"role": "user", "content": build_initial_user_turn(seg, constraints)},
    ]
