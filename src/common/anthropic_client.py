"""Synchronous client wrapping the official Anthropic Python SDK.

Mirrors the DeepSeekClient interface: same ChatResult return type, same fail-loud
contract (raise on missing key / API error / empty content).  The system prompt is
cached via Anthropic's prompt-caching API (cache_control: ephemeral) when provided.

Key is read lazily from ANTHROPIC_API_KEY at first call time (load_dotenv() ensures
.env is loaded before the property is accessed).
"""

from __future__ import annotations

import os

import anthropic as _anthropic
from dotenv import load_dotenv

from common.deepseek_client import ChatResult
from common.pricing import extract_anthropic_usage

load_dotenv()


class AnthropicAPIError(RuntimeError):
    """A non-2xx or API-layer Anthropic error.

    Carries the HTTP status so callers can distinguish auth/quota errors from
    transient failures without re-parsing the message string.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class AnthropicClient:
    """A thin, reusable Anthropic chat client bound to one model.

    The API key is resolved lazily from ANTHROPIC_API_KEY at first call time
    (not at construction), so module-level clients can be created at import time
    — before .env is loaded or the key is set in the environment.
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        timeout: int = 60,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self.timeout = timeout
        self._sdk_client: _anthropic.Anthropic | None = None

    @property
    def api_key(self) -> str:
        if self._api_key is not None:
            return self._api_key
        return os.environ.get("ANTHROPIC_API_KEY", "")

    def _client(self) -> _anthropic.Anthropic:
        if self._sdk_client is None:
            if not self.api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set. "
                    "Export it or add it to .env before calling Anthropic."
                )
            self._sdk_client = _anthropic.Anthropic(
                api_key=self.api_key,
                timeout=float(self.timeout),
            )
        return self._sdk_client

    def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        system: str | None = None,
        timeout: int | None = None,
    ) -> ChatResult:
        """Send one chat completion and return content + usage.

        The system prompt, when provided, is sent with cache_control=ephemeral so
        Anthropic caches it across calls that share the same system text.

        Args:
            messages: List of {"role": ..., "content": ...} dicts.
            max_tokens: Hard upper bound on completion length.
            system: Optional system prompt string.  Wrapped with cache_control.
            timeout: Per-call override; falls back to self.timeout.

        Returns:
            ChatResult with content (first text block), usage (UsageInfo with cost),
            and raw (the raw SDK response object as a dict via model_dump()).

        Raises:
            RuntimeError: if the API key is unset, the transport fails, or the
                response has no text content block.
            AnthropicAPIError: on an Anthropic API error (with .status_code).
        """
        sdk = self._client()

        system_param: list[dict] | _anthropic.NotGiven = _anthropic.NOT_GIVEN
        if system is not None:
            system_param = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        call_timeout = float(timeout) if timeout is not None else float(self.timeout)

        try:
            resp = sdk.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system_param,
                messages=messages,
                timeout=call_timeout,
            )
        except _anthropic.APIStatusError as exc:
            raise AnthropicAPIError(
                exc.status_code,
                f"Anthropic API error (HTTP {exc.status_code}): {exc.message}",
            ) from exc
        except _anthropic.APIConnectionError as exc:
            raise RuntimeError(f"Anthropic network error: {exc}") from exc

        text = next(
            (block.text for block in resp.content if block.type == "text"),
            None,
        )
        if not text:
            raise RuntimeError(
                "Anthropic returned no text content block — "
                "API may have filtered or stopped the response."
            )

        usage = extract_anthropic_usage(self.model, resp)
        raw = resp.model_dump()
        return ChatResult(content=text, usage=usage, raw=raw)
