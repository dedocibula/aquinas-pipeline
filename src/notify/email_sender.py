"""Plain-text email delivery for the comment digest — stdlib smtplib only.

No new dependency: SMTP via ``smtplib`` + ``email.message.EmailMessage``. Config comes
from env (``SMTP_HOST/PORT/USER/PASS``, ``MAIL_FROM``); ``from_env`` fails closed when a
required var is missing rather than silently no-op-ing.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)

_REQUIRED_ENV = ("SMTP_HOST", "SMTP_PORT", "MAIL_FROM")


class EmailSender:
    """Sends plain-text mail over SMTP (STARTTLS if ``SMTP_USER``/``SMTP_PASS`` are set)."""

    def __init__(self, *, host: str, port: int, user: str | None, password: str | None, mail_from: str):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.mail_from = mail_from

    @classmethod
    def from_env(cls) -> EmailSender:
        """Build from ``SMTP_HOST/PORT/USER/PASS`` + ``MAIL_FROM``.

        Raises ``RuntimeError`` if a required var is missing — fail closed rather than
        silently dropping mail.
        """
        missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
        if missing:
            raise RuntimeError(
                f"Missing required SMTP env var(s): {', '.join(missing)}. "
                "Set SMTP_HOST, SMTP_PORT, MAIL_FROM (and SMTP_USER/SMTP_PASS if the "
                "server requires auth) before sending the digest."
            )
        return cls(
            host=os.environ["SMTP_HOST"],
            port=int(os.environ["SMTP_PORT"]),
            user=os.environ.get("SMTP_USER") or None,
            password=os.environ.get("SMTP_PASS") or None,
            mail_from=os.environ["MAIL_FROM"],
        )

    def send(self, to: str, subject: str, text_body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.mail_from
        msg["To"] = to
        msg.set_content(text_body)

        with smtplib.SMTP(self.host, self.port) as smtp:
            smtp.starttls()
            if self.user and self.password:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)


class DryRunEmailSender:
    """Logs the email instead of sending it — for tests and local runs without SMTP."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, text_body: str) -> None:
        self.sent.append((to, subject, text_body))
        log.info("DRY RUN email to=%s subject=%s\n%s", to, subject, text_body)
