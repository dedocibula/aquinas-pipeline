"""API pricing and usage extraction for DeepSeek and Anthropic.

Single source of truth for model rates and the UsageInfo dataclass.
All prices are in USD per token.

Sources:
  DeepSeek: https://api-docs.deepseek.com/quick_start/pricing
    Pipeline defaults: deepseek-v4-flash (translator, thinking disabled) and
    deepseek-v4-flash (reviewer, thinking enabled).  Legacy aliases deepseek-chat
    and deepseek-reasoner retire 2026-07-24 and are kept for pricing lookups only.
  Anthropic: https://www.anthropic.com/pricing (claude-sonnet-4-6, claude-haiku-4-5-20251001)
    Batch API = 50% off all rates.
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
    # Anthropic — cache_creation is the cost to *write* a cache slot (distinct from
    # cache_miss which is regular uncached input).  Both are tracked separately so
    # extract_anthropic_usage can compute an exact cost despite different per-token rates.
    "claude-sonnet-4-6": {
        "cache_hit":      0.30 / 1_000_000,   # cache read
        "cache_miss":     3.00 / 1_000_000,   # uncached input (input_tokens field)
        "cache_creation": 3.75 / 1_000_000,   # writing a new cache slot
        "output":        15.00 / 1_000_000,
    },
    # Batch API is 50% off every rate above.
    "claude-sonnet-4-6-batch": {
        "cache_hit":      0.15 / 1_000_000,
        "cache_miss":     1.50 / 1_000_000,
        "cache_creation": 1.875 / 1_000_000,
        "output":         7.50 / 1_000_000,
    },
    "claude-haiku-4-5-20251001": {
        "cache_hit":      0.08 / 1_000_000,
        "cache_miss":     0.80 / 1_000_000,
        "cache_creation": 1.00 / 1_000_000,
        "output":         4.00 / 1_000_000,
    },
    "claude-haiku-4-5-20251001-batch": {
        "cache_hit":      0.04 / 1_000_000,
        "cache_miss":     0.40 / 1_000_000,
        "cache_creation": 0.50 / 1_000_000,
        "output":         2.00 / 1_000_000,
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


def extract_anthropic_usage(model: str, resp: object) -> UsageInfo:
    """Parse a UsageInfo from a synchronous Anthropic SDK response object.

    Anthropic usage field mapping:
      cache_read_input_tokens        → cache_hit_tokens
      input_tokens                   → uncached miss (regular input, no cache slot)
      cache_creation_input_tokens    → cache write (combined into cache_miss_tokens,
                                       but billed at the higher cache_creation rate)
      output_tokens                  → completion_tokens

    cache_miss_tokens = input_tokens + cache_creation_input_tokens (combined in UsageInfo
    because the dataclass has a single miss field; cost is computed with exact per-tier rates).

    Raises:
        ValueError: If model is not in PRICING.
        RuntimeError: If the response has no usage attribute.
    """
    rates = PRICING.get(model)
    if rates is None:
        raise ValueError(
            f"No pricing entry for model {model!r}. "
            f"Known models: {list(PRICING)}"
        )

    usage = getattr(resp, "usage", None)
    if usage is None:
        raise RuntimeError(
            f"Anthropic response for model {model!r} has no 'usage' attribute. "
            "Cannot compute cost."
        )

    hit        = int(getattr(usage, "cache_read_input_tokens",    0) or 0)
    miss       = int(getattr(usage, "input_tokens",               0) or 0)
    creation   = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    completion = int(getattr(usage, "output_tokens",              0) or 0)

    cost = (
        hit        * rates["cache_hit"]
        + miss       * rates["cache_miss"]
        + creation   * rates.get("cache_creation", rates["cache_miss"])
        + completion * rates["output"]
    )

    return UsageInfo(
        model=model,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss + creation,
        completion_tokens=completion,
        cost_usd=cost,
    )
