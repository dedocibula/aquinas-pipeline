"""Plain-text email delivery for the comment digest — Resend HTTP API only.

Not SMTP: Railway blocks outbound SMTP (port 587 connections came back
``OSError: Network is unreachable``), so mail goes out over HTTPS via Resend's
REST API instead — no new dependency, stdlib ``urllib`` only. Config comes from
env (``RESEND_API_KEY``, ``MAIL_FROM``); ``from_env`` fails closed when a
required var is missing rather than silently no-op-ing.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

_REQUIRED_ENV = ("RESEND_API_KEY", "MAIL_FROM")
_RESEND_URL = "https://api.resend.com/emails"


class EmailSender:
    """Sends plain-text mail via the Resend HTTP API."""

    def __init__(self, *, api_key: str, mail_from: str):
        self.api_key = api_key
        self.mail_from = mail_from

    @classmethod
    def from_env(cls) -> EmailSender:
        """Build from ``RESEND_API_KEY`` + ``MAIL_FROM``.

        Raises ``RuntimeError`` if a required var is missing — fail closed rather than
        silently dropping mail.
        """
        missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
        if missing:
            raise RuntimeError(
                f"Missing required env var(s): {', '.join(missing)}. "
                "Set RESEND_API_KEY and MAIL_FROM before sending the digest."
            )
        return cls(api_key=os.environ["RESEND_API_KEY"], mail_from=os.environ["MAIL_FROM"])

    def send(self, to: str, subject: str, text_body: str) -> None:
        payload = json.dumps(
            {"from": self.mail_from, "to": to, "subject": subject, "text": text_body}
        ).encode("utf-8")

        req = urllib.request.Request(
            _RESEND_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                # Resend sits behind Cloudflare, which blocks the default
                # "Python-urllib/x.y" User-Agent as a bot signature (its own
                # error 1010) before the request ever reaches Resend's API.
                "User-Agent": "aquinas-pipeline-digest/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Resend API error {exc.code}: {body}") from exc


class DryRunEmailSender:
    """Logs the email instead of sending it — for tests and local runs without Resend."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, text_body: str) -> None:
        self.sent.append((to, subject, text_body))
        log.info("DRY RUN email to=%s subject=%s\n%s", to, subject, text_body)
