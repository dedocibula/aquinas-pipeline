"""Tests for EmailSender (stdlib smtplib) — monkeypatched, no real network."""

from __future__ import annotations

import pytest

from notify.email_sender import DryRunEmailSender, EmailSender


def test_from_env_fails_closed_on_missing_vars(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_PORT", raising=False)
    monkeypatch.delenv("MAIL_FROM", raising=False)
    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        EmailSender.from_env()


def test_from_env_builds_sender(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("MAIL_FROM", "aquinas@example.com")
    monkeypatch.setenv("SMTP_USER", "user")
    monkeypatch.setenv("SMTP_PASS", "pass")

    sender = EmailSender.from_env()
    assert sender.host == "smtp.example.com"
    assert sender.port == 587
    assert sender.mail_from == "aquinas@example.com"
    assert sender.user == "user"
    assert sender.password == "pass"


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.started_tls = False
        self.login_args = None
        self.sent = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        self.sent = msg


def test_send_uses_smtplib_and_authenticates(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr("notify.email_sender.smtplib.SMTP", _FakeSMTP)

    sender = EmailSender(
        host="smtp.example.com", port=587, user="user", password="pass",
        mail_from="aquinas@example.com",
    )
    sender.send("alice@example.com", "subject line", "body text")

    smtp = _FakeSMTP.instances[0]
    assert smtp.started_tls
    assert smtp.login_args == ("user", "pass")
    assert smtp.sent["To"] == "alice@example.com"
    assert smtp.sent["From"] == "aquinas@example.com"
    assert smtp.sent["Subject"] == "subject line"
    assert smtp.sent.get_content().strip() == "body text"


def test_send_skips_login_without_credentials(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr("notify.email_sender.smtplib.SMTP", _FakeSMTP)

    sender = EmailSender(
        host="smtp.example.com", port=25, user=None, password=None,
        mail_from="aquinas@example.com",
    )
    sender.send("alice@example.com", "subject", "body")

    smtp = _FakeSMTP.instances[0]
    assert smtp.login_args is None


def test_dry_run_sender_logs_without_network():
    sender = DryRunEmailSender()
    sender.send("alice@example.com", "subject", "body")
    assert sender.sent == [("alice@example.com", "subject", "body")]
