"""Tests for EmailSender (Gmail API) — monkeypatched, no real network."""

from __future__ import annotations

import base64
import io
import json
import urllib.error

import pytest

from notify.email_sender import DryRunEmailSender, EmailSender


def _no_refresh(self, request):
    self.token = "fake-access-token"


def test_from_env_fails_closed_on_missing_vars(monkeypatch):
    for name in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "MAIL_FROM"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="GMAIL_CLIENT_ID"):
        EmailSender.from_env()


def test_from_env_builds_sender(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "test-refresh-token")
    monkeypatch.setenv("MAIL_FROM", "aquinas@example.com")

    sender = EmailSender.from_env()
    assert sender.mail_from == "aquinas@example.com"
    assert sender._creds.client_id == "test-client-id"
    assert sender._creds.refresh_token == "test-refresh-token"


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"id": "fake"}'


def _make_sender():
    return EmailSender(
        client_id="test-client-id",
        client_secret="test-client-secret",
        refresh_token="test-refresh-token",
        mail_from="aquinas@example.com",
    )


def test_send_posts_to_gmail_api(monkeypatch):
    monkeypatch.setattr("notify.email_sender.Credentials.refresh", _no_refresh)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse()

    monkeypatch.setattr("notify.email_sender.urllib.request.urlopen", fake_urlopen)

    sender = _make_sender()
    sender.send("alice@example.com", "subject line", "body text")

    assert captured["url"] == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    assert captured["method"] == "POST"
    assert captured["headers"]["authorization"] == "Bearer fake-access-token"

    raw = base64.urlsafe_b64decode(captured["body"]["raw"]).decode("utf-8")
    assert "To: alice@example.com" in raw
    assert "From: aquinas@example.com" in raw
    assert "Subject: subject line" in raw
    assert "body text" in raw


def test_send_raises_on_http_error(monkeypatch):
    monkeypatch.setattr("notify.email_sender.Credentials.refresh", _no_refresh)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 422, "Unprocessable Entity", {}, io.BytesIO(b'{"message": "bad from"}')
        )

    monkeypatch.setattr("notify.email_sender.urllib.request.urlopen", fake_urlopen)

    sender = _make_sender()
    with pytest.raises(RuntimeError, match="Gmail API error 422"):
        sender.send("alice@example.com", "subject", "body")


def test_dry_run_sender_logs_without_network():
    sender = DryRunEmailSender()
    sender.send("alice@example.com", "subject", "body")
    assert sender.sent == [("alice@example.com", "subject", "body")]
