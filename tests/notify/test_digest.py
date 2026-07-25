"""Tests for notify.digest: render_digest formatting and the send_comment_digest flow."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from notify.digest import render_digest, send_comment_digest
from notify.email_sender import DryRunEmailSender
from storage.models import DigestItem, UserDigest


def _digest():
    return UserDigest(
        user_email="alice@example.com",
        items=[
            DigestItem(
                segment_id=42, locator="I.q3.a1.arg2", author="bob@example.com",
                created_at=datetime(2026, 7, 20, 10, 0), body="reply one",
            ),
            DigestItem(
                segment_id=42, locator="I.q3.a1.arg2", author="carol@example.com",
                created_at=datetime(2026, 7, 20, 11, 0), body="reply two",
            ),
        ],
    )


def test_render_digest_subject_counts_items():
    subject, body = render_digest(_digest(), "https://aquinas.example.com")
    assert subject == "Aquinas Pipeline: 2 new replies to threads you're in"
    assert "Aquinas Pipeline — Summa Theologiae" in body


def test_render_digest_groups_by_locator_and_builds_deep_link():
    _, body = render_digest(_digest(), "https://aquinas.example.com")
    assert "I.q3.a1.arg2" in body
    assert "https://aquinas.example.com/~ST.I.Q3.A1#seg-42" in body
    assert "bob@example.com" in body and "reply one" in body
    assert "carol@example.com" in body and "reply two" in body


def test_render_digest_singular_reply():
    single = UserDigest(user_email="alice@example.com", items=_digest().items[:1])
    subject, _ = render_digest(single, "https://aquinas.example.com")
    assert subject == "Aquinas Pipeline: 1 new reply to threads you're in"


@patch("notify.digest.mark_thread_notified")
@patch("notify.digest.collect_digests")
def test_send_comment_digest_sends_and_marks_notified(mock_collect, mock_mark, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://aquinas.example.com")
    mock_collect.return_value = [_digest()]

    with patch("notify.digest.get_conn"):
        sender = DryRunEmailSender()
        sent_to = send_comment_digest(sender=sender)

    assert sent_to == ["alice@example.com"]
    assert len(sender.sent) == 1
    to, subject, body = sender.sent[0]
    assert to == "alice@example.com"
    assert "2 new replies" in subject
    mock_mark.assert_called_once()
    args, _ = mock_mark.call_args
    assert args[1:] == (42, "alice@example.com")


@patch("notify.digest.mark_thread_notified")
@patch("notify.digest.collect_digests")
def test_send_comment_digest_empty_is_idempotent(mock_collect, mock_mark, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://aquinas.example.com")
    mock_collect.return_value = []

    with patch("notify.digest.get_conn"):
        sent_to = send_comment_digest(sender=DryRunEmailSender())

    assert sent_to == []
    mock_mark.assert_not_called()
