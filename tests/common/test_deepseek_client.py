"""Tests for common.deepseek_client.DeepSeekClient — no real API calls."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from common.deepseek_client import ChatResult, DeepSeekAPIError, DeepSeekClient


def _fake_response(content: str = "ok", status_code: int = 200, usage: dict | None = None):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = {
        "choices": [{"message": {"content": content}}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 40},
    }
    return mock


_MESSAGES = [{"role": "user", "content": "hi"}]


def _client(**kw) -> DeepSeekClient:
    return DeepSeekClient("deepseek-chat", url="https://example.test/v1", **kw)


class TestChat:
    def test_returns_content_and_usage(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        with patch("common.deepseek_client.requests.post") as post:
            post.return_value = _fake_response("hello")
            result = _client().chat(_MESSAGES, temperature=0.3, max_tokens=128)
        assert isinstance(result, ChatResult)
        assert result.content == "hello"          # returned verbatim, not stripped
        assert result.usage.cost_usd > 0
        assert result.raw["usage"]["prompt_tokens"] == 100

    def test_payload_carries_model_and_sampling_knobs(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        with patch("common.deepseek_client.requests.post") as post:
            post.return_value = _fake_response()
            _client().chat(_MESSAGES, temperature=0.0, max_tokens=512)
        body = post.call_args.kwargs["json"]
        assert body["model"] == "deepseek-chat"
        assert body["messages"] == _MESSAGES
        assert body["temperature"] == 0.0
        assert body["max_tokens"] == 512
        assert "response_format" not in body  # omitted unless requested
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer k"

    def test_response_format_passthrough(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        with patch("common.deepseek_client.requests.post") as post:
            post.return_value = _fake_response()
            _client().chat(
                _MESSAGES, temperature=0.0, max_tokens=16,
                response_format={"type": "json_object"},
            )
        assert post.call_args.kwargs["json"]["response_format"] == {"type": "json_object"}

    def test_explicit_api_key_overrides_env(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with patch("common.deepseek_client.requests.post") as post:
            post.return_value = _fake_response()
            _client(api_key="explicit").chat(_MESSAGES, temperature=0.0, max_tokens=16)
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer explicit"

    def test_default_timeout_used(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        with patch("common.deepseek_client.requests.post") as post:
            post.return_value = _fake_response()
            _client(timeout=99).chat(_MESSAGES, temperature=0.0, max_tokens=16)
        assert post.call_args.kwargs["timeout"] == 99

    def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            _client().chat(_MESSAGES, temperature=0.0, max_tokens=16)

    def test_raises_deepseek_api_error_on_4xx(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        with patch("common.deepseek_client.requests.post") as post:
            post.return_value = _fake_response("", status_code=402)
            with pytest.raises(DeepSeekAPIError) as exc:
                _client().chat(_MESSAGES, temperature=0.0, max_tokens=16)
        assert exc.value.status_code == 402
        assert "HTTP 402" in str(exc.value)
        assert isinstance(exc.value, RuntimeError)  # callers can catch as RuntimeError

    def test_raises_runtime_error_on_network_failure(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        with patch("common.deepseek_client.requests.post") as post:
            post.side_effect = requests.ConnectionError("boom")
            with pytest.raises(RuntimeError, match="network error"):
                _client().chat(_MESSAGES, temperature=0.0, max_tokens=16)

    def test_raises_on_empty_choices(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        empty = MagicMock()
        empty.status_code = 200
        empty.json.return_value = {"choices": []}
        with patch("common.deepseek_client.requests.post", return_value=empty):
            with pytest.raises(RuntimeError, match="no choices"):
                _client().chat(_MESSAGES, temperature=0.0, max_tokens=16)
