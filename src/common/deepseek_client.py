"""Single HTTP client for the DeepSeek chat-completions API.

Every DeepSeek call in the pipeline goes through `DeepSeekClient.chat` — the one
place that assembles the request (auth header, model, messages, sampling knobs),
performs the POST, and extracts content + token usage. Callers keep only their own
policy on top of it: the translator/reviewer fail loudly, the gap-term batch
soft-fails to `{}` (except fatal auth/credit errors), and the sense labeller retries.

`chat` fails loudly: a non-2xx response raises `DeepSeekAPIError` (which carries the
status code), a transport failure or an empty `choices` list raises `RuntimeError`.
Both are `RuntimeError` subclasses, so callers that wrap calls in
`try/except RuntimeError` keep working unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import requests

from common.pricing import UsageInfo, extract_usage

DEFAULT_API_URL = os.environ.get(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions"
)


class DeepSeekAPIError(RuntimeError):
    """A non-2xx DeepSeek response.

    Carries the HTTP status so callers can distinguish fatal auth/credit errors
    (401/402/403) from transient ones without re-parsing the message.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ChatResult:
    """One chat-completion: the raw content string, parsed token usage, full JSON."""

    content: str       # choices[0].message.content, verbatim (caller strips/parses)
    usage: UsageInfo   # token counts + cost, from common.pricing.extract_usage
    raw: dict          # the full decoded response (for usage stats etc.)


class DeepSeekClient:
    """A thin, reusable client bound to one model/endpoint.

    The API key is resolved lazily from `DEEPSEEK_API_KEY` at call time (unless one
    is passed explicitly), so module-level clients can be constructed at import time
    — before `.env` is loaded or the key is exported.
    """

    def __init__(
        self,
        model: str,
        *,
        url: str | None = None,
        api_key: str | None = None,
        timeout: int = 60,
    ) -> None:
        self.model = model
        self.url = url or DEFAULT_API_URL
        self._api_key = api_key
        self.timeout = timeout

    @property
    def api_key(self) -> str:
        if self._api_key is not None:
            return self._api_key
        return os.environ.get("DEEPSEEK_API_KEY", "")

    def chat(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        response_format: dict | None = None,
        timeout: int | None = None,
    ) -> ChatResult:
        """POST one chat completion and return its content + usage.

        Raises:
            RuntimeError: if the API key is unset, the transport fails, or the
                response has no choices.
            DeepSeekAPIError: on a non-2xx status (with `.status_code`).
        """
        if not self.api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set. "
                "Export it or add it to .env before calling DeepSeek."
            )

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        try:
            resp = requests.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout if timeout is not None else self.timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"DeepSeek network error: {exc}") from exc

        if resp.status_code >= 400:
            raise DeepSeekAPIError(
                resp.status_code,
                f"DeepSeek API error (HTTP {resp.status_code}) — "
                "check DEEPSEEK_API_KEY and account credits.",
            )

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(
                "DeepSeek returned no choices — API may have filtered the response."
            )
        content = choices[0]["message"]["content"]
        usage = extract_usage(self.model, data)
        return ChatResult(content=content, usage=usage, raw=data)
