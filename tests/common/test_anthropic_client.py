"""Tests for common.anthropic_client.AnthropicClient — no real API calls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from common.anthropic_client import AnthropicAPIError, AnthropicClient
from common.deepseek_client import ChatResult

_MODEL = "claude-sonnet-4-6"
_MESSAGES = [{"role": "user", "content": "translate this"}]


def _make_resp(
    text: str = "preložený text",
    cache_read: int = 0,
    input_tokens: int = 100,
    cache_creation: int = 0,
    output_tokens: int = 40,
) -> MagicMock:
    """Build a minimal fake Anthropic SDK response."""
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(
        cache_read_input_tokens=cache_read,
        input_tokens=input_tokens,
        cache_creation_input_tokens=cache_creation,
        output_tokens=output_tokens,
    )
    resp = MagicMock()
    resp.content = [block]
    resp.usage = usage
    resp.model_dump.return_value = {"model": _MODEL, "content": [{"type": "text", "text": text}]}
    return resp


def _client(**kw) -> AnthropicClient:
    return AnthropicClient(_MODEL, **kw)


def _patch_sdk(resp: MagicMock):
    """Return a context manager that patches `anthropic.Anthropic` and pre-wires the response."""
    mock_sdk = MagicMock()
    mock_sdk.messages.create.return_value = resp
    return patch("common.anthropic_client._anthropic.Anthropic", return_value=mock_sdk)


class TestChatBasics:
    def test_returns_chat_result(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        resp = _make_resp("dobrý výsledok")
        with _patch_sdk(resp):
            result = _client().chat(_MESSAGES, max_tokens=256)
        assert isinstance(result, ChatResult)
        assert result.content == "dobrý výsledok"

    def test_usage_cost_positive(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        resp = _make_resp(output_tokens=100)
        with _patch_sdk(resp):
            result = _client().chat(_MESSAGES, max_tokens=256)
        assert result.usage.cost_usd > 0
        assert result.usage.completion_tokens == 100

    def test_raw_is_dict(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        resp = _make_resp()
        with _patch_sdk(resp):
            result = _client().chat(_MESSAGES, max_tokens=64)
        assert isinstance(result.raw, dict)


class TestSystemPrompt:
    def test_system_prompt_sent_with_cache_control(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        resp = _make_resp()
        with _patch_sdk(resp) as mock_cls:
            sdk_instance = mock_cls.return_value
            _client().chat(_MESSAGES, max_tokens=64, system="Be a translator.")
        call_kwargs = sdk_instance.messages.create.call_args.kwargs
        sys_param = call_kwargs["system"]
        assert isinstance(sys_param, list)
        assert sys_param[0]["type"] == "text"
        assert sys_param[0]["text"] == "Be a translator."
        assert sys_param[0]["cache_control"] == {"type": "ephemeral"}

    def test_no_system_prompt_omits_system_param(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        import anthropic as _anthropic
        resp = _make_resp()
        with _patch_sdk(resp) as mock_cls:
            sdk_instance = mock_cls.return_value
            _client().chat(_MESSAGES, max_tokens=64)
        call_kwargs = sdk_instance.messages.create.call_args.kwargs
        assert call_kwargs["system"] is _anthropic.NOT_GIVEN


class TestApiKeyHandling:
    def test_explicit_key_used_before_env(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        resp = _make_resp()
        with _patch_sdk(resp) as mock_cls:
            _client(api_key="explicit-key").chat(_MESSAGES, max_tokens=64)
        mock_cls.assert_called_once()
        init_kwargs = mock_cls.call_args.kwargs
        assert init_kwargs["api_key"] == "explicit-key"

    def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            _client().chat(_MESSAGES, max_tokens=64)


class TestErrorHandling:
    def test_raises_anthropic_api_error_on_api_status_error(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        import anthropic as _anthropic

        api_err = _anthropic.APIStatusError(
            message="Unauthorized",
            response=MagicMock(status_code=401),
            body=None,
        )
        resp = _make_resp()
        with _patch_sdk(resp) as mock_cls:
            sdk_instance = mock_cls.return_value
            sdk_instance.messages.create.side_effect = api_err
            with pytest.raises(AnthropicAPIError) as exc_info:
                _client().chat(_MESSAGES, max_tokens=64)
        assert exc_info.value.status_code == 401
        assert isinstance(exc_info.value, RuntimeError)

    def test_raises_runtime_error_on_connection_error(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        import anthropic as _anthropic

        net_err = _anthropic.APIConnectionError(request=MagicMock())
        resp = _make_resp()
        with _patch_sdk(resp) as mock_cls:
            sdk_instance = mock_cls.return_value
            sdk_instance.messages.create.side_effect = net_err
            with pytest.raises(RuntimeError, match="network error"):
                _client().chat(_MESSAGES, max_tokens=64)

    def test_raises_on_no_text_content_block(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        resp = _make_resp()
        resp.content = []  # empty content blocks
        with _patch_sdk(resp):
            with pytest.raises(RuntimeError, match="no text content block"):
                _client().chat(_MESSAGES, max_tokens=64)

    def test_raises_on_non_text_content_block(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        resp = _make_resp()
        resp.content = [SimpleNamespace(type="tool_use", id="x")]  # no text block
        with _patch_sdk(resp):
            with pytest.raises(RuntimeError, match="no text content block"):
                _client().chat(_MESSAGES, max_tokens=64)


class TestTimeout:
    def test_default_timeout_passed_to_sdk_create(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        resp = _make_resp()
        with _patch_sdk(resp) as mock_cls:
            sdk_instance = mock_cls.return_value
            _client(timeout=90).chat(_MESSAGES, max_tokens=64)
        call_kwargs = sdk_instance.messages.create.call_args.kwargs
        assert call_kwargs["timeout"] == 90.0

    def test_per_call_timeout_overrides_default(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        resp = _make_resp()
        with _patch_sdk(resp) as mock_cls:
            sdk_instance = mock_cls.return_value
            _client(timeout=60).chat(_MESSAGES, max_tokens=64, timeout=10)
        call_kwargs = sdk_instance.messages.create.call_args.kwargs
        assert call_kwargs["timeout"] == 10.0
