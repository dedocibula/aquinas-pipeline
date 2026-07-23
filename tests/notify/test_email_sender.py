"""Tests for EmailSender (Resend HTTP API) — monkeypatched, no real network."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from notify.email_sender import DryRunEmailSender, EmailSender


def test_from_env_fails_closed_on_missing_vars(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("MAIL_FROM", raising=False)
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        EmailSender.from_env()


def test_from_env_builds_sender(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("MAIL_FROM", "aquinas@example.com")

    sender = EmailSender.from_env()
    assert sender.api_key == "re_test_key"
    assert sender.mail_from == "aquinas@example.com"


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"id": "fake"}'


def test_send_posts_to_resend_api(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse()

    monkeypatch.setattr("notify.email_sender.urllib.request.urlopen", fake_urlopen)

    sender = EmailSender(api_key="re_test_key", mail_from="aquinas@example.com")
    sender.send("alice@example.com", "subject line", "body text")

    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["method"] == "POST"
    assert captured["headers"]["authorization"] == "Bearer re_test_key"
    assert captured["body"] == {
        "from": "aquinas@example.com",
        "to": "alice@example.com",
        "subject": "subject line",
        "text": "body text",
    }


def test_send_raises_on_http_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 422, "Unprocessable Entity", {}, io.BytesIO(b'{"message": "bad from"}')
        )

    monkeypatch.setattr("notify.email_sender.urllib.request.urlopen", fake_urlopen)

    sender = EmailSender(api_key="re_test_key", mail_from="aquinas@example.com")
    with pytest.raises(RuntimeError, match="Resend API error 422"):
        sender.send("alice@example.com", "subject", "body")


def test_dry_run_sender_logs_without_network():
    sender = DryRunEmailSender()
    sender.send("alice@example.com", "subject", "body")
    assert sender.sent == [("alice@example.com", "subject", "body")]
