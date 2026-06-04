"""DeepSeek V3 API client for gap-term classification and translation."""

from __future__ import annotations

import json
import os
import re
import threading

import requests

_api_stats: dict[str, int] = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
_api_stats_lock = threading.Lock()

_DEEPSEEK_URL = os.environ.get(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions"
)
_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# Model-assigned gap-term categories (stored in glossary_term.category, overridable
# in M3). 'term'/'name'/'formula' are kept-and-locked; 'prose' is ordinary vocab.
_GAP_CATEGORIES: frozenset[str] = frozenset({"term", "name", "formula", "prose"})


def _parse_batch_entry(input_lemma: str, value) -> dict | None:
    """Normalize one model entry into {category, slovak}.

    Returns None for malformed entries (caller fills a fallback). Accepts a plain
    string (legacy/loose model output) by treating it as the slovak term with no category.
    """
    if isinstance(value, str):
        slovak = value.strip()
        if not slovak:
            return None
        return {"category": None, "slovak": slovak}

    if not isinstance(value, dict):
        return None

    slovak = str(value.get("slovak", "")).strip()
    if not slovak:
        return None
    category = value.get("category")
    if category is not None:
        category = str(category).strip().lower()
        if category not in _GAP_CATEGORIES:
            category = None
    return {"category": category, "slovak": slovak}


def _call_deepseek_batch(batch: list[dict]) -> dict[str, dict]:
    """Classify and translate a batch of Latin gap lemmas in one call.

    Each item: {"lemma": str, "best_latin": str, "best_czech": str, "best_english": str}
    Returns {input_lemma: {"category": str|None, "slovak": str}}.
    Missing/malformed entries are omitted; the caller fills per-lemma fallbacks.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. "
            "Export it before running the resolver."
        )

    lines = []
    for item in batch:
        parts = [f"- {item['lemma']}"]
        if item.get("best_latin"):
            parts.append(f"Latin: {item['best_latin'][:150]}")
        if item.get("best_czech"):
            parts.append(f"Czech: {item['best_czech'][:80]}")
        if item.get("best_english"):
            parts.append(f"English: {item['best_english'][:80]}")
        lines.append(" | ".join(parts))

    prompt = (
        "You are a Slovak theological terminologist working on Thomas Aquinas's Summa Theologiae.\n"
        "For each Latin lemma below (with Czech/English context excerpts), return two fields:\n"
        '  "category" — one of: "term" (theological/philosophical content word),\n'
        '               "name" (proper noun, e.g. Christus, Augustinus, philosophus=Aristotle),\n'
        '               "formula" (recurring structural/formulaic connective, e.g. Praeterea,\n'
        '               Respondeo, Videtur), "prose" (ordinary verb/quantifier/function word).\n'
        '  "slovak"   — the single best Slovak rendering of this lemma.\n'
        'Respond ONLY with a JSON object keyed by the input lemma:\n'
        '  {"<input_lemma>": {"category": "...", "slovak": "..."}, ...}\n'
        "No explanations, no markdown fences, no extra text.\n\n"
        "Lemmas:\n" + "\n".join(lines)
    )

    with _api_stats_lock:
        _api_stats["calls"] += 1
    try:
        resp = requests.post(
            _DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": _DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": len(batch) * 60,
                "temperature": 0.0,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        usage = data.get("usage", {})
        with _api_stats_lock:
            _api_stats["input_tokens"] += usage.get("prompt_tokens", 0)
            _api_stats["output_tokens"] += usage.get("completion_tokens", 0)

        content = data["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if the model wraps the JSON
        content = re.sub(r"```(?:json)?\s*", "", content).replace("```", "").strip()
        result = json.loads(content)

        valid_lemmas = {item["lemma"] for item in batch}
        parsed: dict[str, dict] = {}
        for k, v in result.items():
            if str(k) not in valid_lemmas:
                continue  # model hallucinated a key not in the input batch
            entry = _parse_batch_entry(str(k), v)
            if entry is not None:
                parsed[str(k)] = entry
        return parsed

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 402, 403):
            raise RuntimeError(
                f"DeepSeek API fatal error (HTTP {status}) — "
                "check DEEPSEEK_API_KEY and account credits. Aborting."
            ) from exc
        print(f"  [WARN] DeepSeek batch HTTP error {status} ({len(batch)} lemmas): {exc}", flush=True)
        return {}
    except Exception as exc:
        print(f"  [WARN] DeepSeek batch error ({len(batch)} lemmas): {exc}", flush=True)
        return {}


def get_api_stats() -> dict[str, int]:
    """Return accumulated DeepSeek API usage stats."""
    return dict(_api_stats)
