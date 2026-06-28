"""Tests for Anthropic pricing in common.pricing.

Covers extract_anthropic_usage() math and edge cases.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from common.pricing import PRICING, UsageInfo, extract_anthropic_usage, zero_usage


def _fake_usage(
    cache_read: int = 0,
    input_tokens: int = 0,
    cache_creation: int = 0,
    output_tokens: int = 0,
) -> object:
    """Minimal object mimicking an Anthropic SDK usage object."""
    return SimpleNamespace(
        cache_read_input_tokens=cache_read,
        input_tokens=input_tokens,
        cache_creation_input_tokens=cache_creation,
        output_tokens=output_tokens,
    )


def _fake_resp(usage_obj: object) -> object:
    return SimpleNamespace(usage=usage_obj)


MODEL = "claude-sonnet-4-6"


class TestExtractAnthropicUsage:
    def test_all_zeros_produces_zero_cost(self):
        resp = _fake_resp(_fake_usage())
        info = extract_anthropic_usage(MODEL, resp)
        assert info.cost_usd == 0.0
        assert info.cache_hit_tokens == 0
        assert info.cache_miss_tokens == 0
        assert info.completion_tokens == 0

    def test_hit_tokens_map_correctly(self):
        resp = _fake_resp(_fake_usage(cache_read=1_000_000))
        info = extract_anthropic_usage(MODEL, resp)
        expected_cost = 0.30  # $0.30 per 1M cache-read tokens
        assert abs(info.cost_usd - expected_cost) < 1e-9
        assert info.cache_hit_tokens == 1_000_000
        assert info.cache_miss_tokens == 0

    def test_input_tokens_billed_at_miss_rate(self):
        resp = _fake_resp(_fake_usage(input_tokens=1_000_000))
        info = extract_anthropic_usage(MODEL, resp)
        expected_cost = 3.00  # $3.00 per 1M input tokens
        assert abs(info.cost_usd - expected_cost) < 1e-9
        assert info.cache_miss_tokens == 1_000_000

    def test_cache_creation_billed_at_creation_rate(self):
        resp = _fake_resp(_fake_usage(cache_creation=1_000_000))
        info = extract_anthropic_usage(MODEL, resp)
        expected_cost = 3.75  # $3.75 per 1M cache-write tokens
        assert abs(info.cost_usd - expected_cost) < 1e-9
        assert info.cache_miss_tokens == 1_000_000  # creation folds into miss

    def test_output_tokens_billed_at_output_rate(self):
        resp = _fake_resp(_fake_usage(output_tokens=1_000_000))
        info = extract_anthropic_usage(MODEL, resp)
        expected_cost = 15.00  # $15.00 per 1M output tokens
        assert abs(info.cost_usd - expected_cost) < 1e-9
        assert info.completion_tokens == 1_000_000

    def test_mixed_usage_cost_is_sum_of_tiers(self):
        resp = _fake_resp(
            _fake_usage(
                cache_read=100,
                input_tokens=200,
                cache_creation=50,
                output_tokens=300,
            )
        )
        info = extract_anthropic_usage(MODEL, resp)
        rates = PRICING[MODEL]
        expected = (
            100 * rates["cache_hit"]
            + 200 * rates["cache_miss"]
            + 50 * rates["cache_creation"]
            + 300 * rates["output"]
        )
        assert abs(info.cost_usd - expected) < 1e-12
        assert info.cache_hit_tokens == 100
        assert info.cache_miss_tokens == 250   # input + creation
        assert info.completion_tokens == 300

    def test_prompt_tokens_property(self):
        resp = _fake_resp(_fake_usage(cache_read=100, input_tokens=200, cache_creation=50))
        info = extract_anthropic_usage(MODEL, resp)
        # prompt_tokens = cache_hit + cache_miss (where cache_miss = input + creation)
        assert info.prompt_tokens == 100 + 250

    def test_raises_on_unknown_model(self):
        resp = _fake_resp(_fake_usage(output_tokens=1))
        with pytest.raises(ValueError, match="No pricing entry"):
            extract_anthropic_usage("gpt-99", resp)

    def test_raises_on_missing_usage(self):
        resp = SimpleNamespace()  # no .usage attribute
        with pytest.raises(RuntimeError, match="no 'usage' attribute"):
            extract_anthropic_usage(MODEL, resp)

    def test_none_fields_default_to_zero(self):
        """Anthropic SDK sometimes returns None for unused fields."""
        usage = SimpleNamespace(
            cache_read_input_tokens=None,
            input_tokens=None,
            cache_creation_input_tokens=None,
            output_tokens=500,
        )
        resp = _fake_resp(usage)
        info = extract_anthropic_usage(MODEL, resp)
        assert info.cache_hit_tokens == 0
        assert info.cache_miss_tokens == 0
        assert info.completion_tokens == 500

    def test_batch_model_fifty_percent_discount(self):
        resp = _fake_resp(_fake_usage(output_tokens=1_000_000))
        info = extract_anthropic_usage("claude-sonnet-4-6-batch", resp)
        assert abs(info.cost_usd - 7.50) < 1e-9

    def test_returns_usageinfo_instance(self):
        resp = _fake_resp(_fake_usage(output_tokens=1))
        info = extract_anthropic_usage(MODEL, resp)
        assert isinstance(info, UsageInfo)
        assert info.model == MODEL


class TestZeroUsage:
    def test_zero_usage_model_preserved(self):
        info = zero_usage(MODEL)
        assert info.model == MODEL
        assert info.cost_usd == 0.0
        assert info.cache_hit_tokens == 0
        assert info.completion_tokens == 0
