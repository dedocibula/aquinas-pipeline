"""DeepSeek API pricing and usage extraction.

Single source of truth for model rates and the UsageInfo dataclass.
All prices are in USD per token.

Source: https://api-docs.deepseek.com/quick_start/pricing
Note: deepseek-chat and deepseek-reasoner are deprecated aliases retiring 2026-07-24;
they currently route to deepseek-v4-flash (non-thinking) and deepseek-v4-pro (thinking).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UsageInfo:
    model: str
    cache_hit_tokens: int
    cache_miss_tokens: int
    completion_tokens: int
    cost_usd: float

    @property
    def prompt_tokens(self) -> int:
        return self.cache_hit_tokens + self.cache_miss_tokens


# Rates: USD per token (not per 1k).
# Keys include both deprecated aliases and canonical V4 names so that
# callers using either model ID resolve correctly.
PRICING: dict[str, dict[str, float]] = {
    "deepseek-chat": {
        "cache_hit":  0.0028 / 1_000_000,
        "cache_miss": 0.14   / 1_000_000,
        "output":     0.28   / 1_000_000,
    },
    "deepseek-reasoner": {
        "cache_hit":  0.003625 / 1_000_000,
        "cache_miss": 0.435    / 1_000_000,
        "output":     0.87     / 1_000_000,
    },
    "deepseek-v4-flash": {
        "cache_hit":  0.0028 / 1_000_000,
        "cache_miss": 0.14   / 1_000_000,
        "output":     0.28   / 1_000_000,
    },
    "deepseek-v4-pro": {
        "cache_hit":  0.003625 / 1_000_000,
        "cache_miss": 0.435    / 1_000_000,
        "output":     0.87     / 1_000_000,
    },
}


def extract_usage(model: str, response_json: dict) -> UsageInfo:
    """Parse a UsageInfo from a DeepSeek API response dict.

    Handles both native DeepSeek field names and the OpenAI-compat form:
      Native:  prompt_cache_hit_tokens / prompt_cache_miss_tokens
      Compat:  prompt_tokens_details.cached_tokens  (hit = cached_tokens,
                                                      miss = prompt_tokens - cached_tokens)

    Raises:
        ValueError: If model is not in PRICING.
        RuntimeError: If the response has no usage field.
    """
    rates = PRICING.get(model)
    if rates is None:
        raise ValueError(
            f"No pricing entry for model {model!r}. "
            f"Known models: {list(PRICING)}"
        )

    usage = response_json.get("usage")
    if not usage:
        raise RuntimeError(
            f"DeepSeek response for model {model!r} has no 'usage' field. "
            "Cannot compute cost."
        )

    if "prompt_cache_hit_tokens" in usage:
        hit_tokens  = int(usage["prompt_cache_hit_tokens"])
        miss_tokens = int(usage["prompt_cache_miss_tokens"])
    else:
        details     = usage.get("prompt_tokens_details") or {}
        hit_tokens  = int(details.get("cached_tokens", 0))
        miss_tokens = max(0, int(usage.get("prompt_tokens", 0)) - hit_tokens)

    completion = int(usage.get("completion_tokens", 0))

    cost = (
        hit_tokens  * rates["cache_hit"]
        + miss_tokens * rates["cache_miss"]
        + completion  * rates["output"]
    )

    return UsageInfo(
        model=model,
        cache_hit_tokens=hit_tokens,
        cache_miss_tokens=miss_tokens,
        completion_tokens=completion,
        cost_usd=cost,
    )


def zero_usage(model: str) -> UsageInfo:
    """Return a UsageInfo with all zeros — safe default when no API call was made."""
    return UsageInfo(
        model=model,
        cache_hit_tokens=0,
        cache_miss_tokens=0,
        completion_tokens=0,
        cost_usd=0.0,
    )
