"""Plain-text email delivery for the comment digest — Gmail API only.

Not SMTP: Railway blocks outbound SMTP (port 587 connections came back
``OSError: Network is unreachable``), so mail goes out over HTTPS via the
Gmail REST API instead. No new dependency: ``google-auth`` (already a
project dependency for gspread) refreshes the OAuth access token, and the
send call itself is stdlib ``urllib``. Config comes from env
(``GMAIL_CLIENT_ID``, ``GMAIL_CLIENT_SECRET``, ``GMAIL_REFRESH_TOKEN``,
``MAIL_FROM``); ``from_env`` fails closed when a required var is missing
rather than silently no-op-ing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

log = logging.getLogger(__name__)

_REQUIRED_ENV = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "MAIL_FROM")
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


class EmailSender:
    """Sends plain-text mail via the Gmail API, authenticated as ``mail_from``."""

    def __init__(self, *, client_id: str, client_secret: str, refresh_token: str, mail_from: str):
        self.mail_from = mail_from
        self._creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=_TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=_SCOPES,
        )

    @classmethod
    def from_env(cls) -> EmailSender:
        """Build from ``GMAIL_CLIENT_ID``/``GMAIL_CLIENT_SECRET``/``GMAIL_REFRESH_TOKEN``/``MAIL_FROM``.

        Raises ``RuntimeError`` if a required var is missing — fail closed rather than
        silently dropping mail.
        """
        missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
        if missing:
            raise RuntimeError(
                f"Missing required env var(s): {', '.join(missing)}. "
                "Set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN and "
                "MAIL_FROM before sending the digest."
            )
        return cls(
            client_id=os.environ["GMAIL_CLIENT_ID"],
            client_secret=os.environ["GMAIL_CLIENT_SECRET"],
            refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
            mail_from=os.environ["MAIL_FROM"],
        )

    def _access_token(self) -> str:
        # Refresh unconditionally: a digest run is infrequent (daily cron) and a Gmail
        # access token is only valid ~1h, so there's no benefit to caching it between runs.
        self._creds.refresh(Request())
        return self._creds.token

    def send(self, to: str, subject: str, text_body: str) -> None:
        message = MIMEText(text_body)
        message["To"] = to
        message["From"] = self.mail_from
        message["Subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

        payload = json.dumps({"raw": raw}).encode("utf-8")
        req = urllib.request.Request(
            _SEND_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gmail API error {exc.code}: {body}") from exc


class DryRunEmailSender:
    """Logs the email instead of sending it — for tests and local runs without Gmail."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, text_body: str) -> None:
        self.sent.append((to, subject, text_body))
        log.info("DRY RUN email to=%s subject=%s\n%s", to, subject, text_body)
