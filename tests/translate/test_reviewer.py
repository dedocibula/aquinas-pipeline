"""
Tests for the DeepSeek R1 reviewer agent — no real API calls.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from translate.reviewer import ReviewResult, call_reviewer_r1, _SYSTEM_PROMPT


# ── Fixtures ───────────────────────────────────────────────────────────────────

_LATIN = "Utrum Deus sit."
_DRAFT = "Či Boh je."
_CONSTRAINTS = [{"latin_lemma": "Deus", "required_slovak": "Boh"}]


def _fake_response(content: str, status_code: int = 200):
    mock = MagicMock()
    mock.status_code = status_code
    if status_code >= 400:
        mock.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        mock.raise_for_status = lambda: None
    mock.json.return_value = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }
    return mock


# ── TestCallReviewerR1 ─────────────────────────────────────────────────────────

class TestCallReviewerR1:
    def test_approved_verdict(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        with patch("translate.reviewer.requests.post") as mock_post:
            mock_post.return_value = _fake_response("APPROVED")
            result = call_reviewer_r1(_LATIN, _DRAFT, _CONSTRAINTS)
        assert result == ReviewResult(verdict="APPROVED", notes=None, feedback=None)

    def test_approved_with_notes_verdict(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        content = "APPROVED_WITH_NOTES: - Consider a more literal rendering of 'est'"
        with patch("translate.reviewer.requests.post") as mock_post:
            mock_post.return_value = _fake_response(content)
            result = call_reviewer_r1(_LATIN, _DRAFT, _CONSTRAINTS)
        assert result.verdict == "APPROVED_WITH_NOTES"
        assert result.notes is not None
        assert result.feedback is None

    def test_revision_needed_verdict(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        content = "REVISION_NEEDED: - Required term 'Boh' is missing from draft"
        with patch("translate.reviewer.requests.post") as mock_post:
            mock_post.return_value = _fake_response(content)
            result = call_reviewer_r1(_LATIN, _DRAFT, _CONSTRAINTS)
        assert result.verdict == "REVISION_NEEDED"
        assert result.feedback is not None
        assert result.notes is None

    def test_unrecognised_verdict_raises(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        with patch("translate.reviewer.requests.post") as mock_post:
            mock_post.return_value = _fake_response("LGTM")
            with pytest.raises(RuntimeError, match="Unrecognised reviewer verdict"):
                call_reviewer_r1(_LATIN, _DRAFT, _CONSTRAINTS)

    def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            call_reviewer_r1(_LATIN, _DRAFT, _CONSTRAINTS)

    def test_raises_on_http_401(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "bad-key")
        with patch("translate.reviewer.requests.post") as mock_post:
            mock_post.return_value = _fake_response("", status_code=401)
            with pytest.raises(RuntimeError, match="HTTP 401"):
                call_reviewer_r1(_LATIN, _DRAFT, _CONSTRAINTS)

    def test_raises_on_network_error(self, monkeypatch):
        import requests as req_mod
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        with patch("translate.reviewer.requests.post") as mock_post:
            mock_post.side_effect = req_mod.ConnectionError("timeout")
            with pytest.raises(RuntimeError, match="network error"):
                call_reviewer_r1(_LATIN, _DRAFT, _CONSTRAINTS)

    def test_raises_on_empty_choices(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        empty = MagicMock()
        empty.status_code = 200
        empty.json.return_value = {"choices": []}
        with patch("translate.reviewer.requests.post", return_value=empty):
            with pytest.raises(RuntimeError, match="no choices"):
                call_reviewer_r1(_LATIN, _DRAFT, _CONSTRAINTS)


# ── TestSystemPrompt ───────────────────────────────────────────────────────────

class TestSystemPrompt:
    def test_system_prompt_contains_revision_needed(self):
        assert "REVISION_NEEDED" in _SYSTEM_PROMPT


# ── TestUserTurn ───────────────────────────────────────────────────────────────

class TestUserTurn:
    def _capture_user_content(self, monkeypatch, content: str = "APPROVED") -> str:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        captured = {}
        with patch("translate.reviewer.requests.post") as mock_post:
            mock_post.return_value = _fake_response(content)

            def capture(*args, **kwargs):
                messages = kwargs.get("json", {}).get("messages", [])
                for msg in messages:
                    if msg.get("role") == "user":
                        captured["user"] = msg["content"]
                return _fake_response(content)

            mock_post.side_effect = capture
            call_reviewer_r1(_LATIN, _DRAFT, _CONSTRAINTS)
        return captured.get("user", "")

    def test_user_turn_contains_required_terms_block(self, monkeypatch):
        user_content = self._capture_user_content(monkeypatch)
        assert "REQUIRED TERMS:" in user_content
        assert "Deus → Boh" in user_content

    def test_user_turn_contains_latin_text(self, monkeypatch):
        user_content = self._capture_user_content(monkeypatch)
        assert "LATIN:" in user_content
        assert _LATIN in user_content

    def test_user_turn_contains_draft_text(self, monkeypatch):
        user_content = self._capture_user_content(monkeypatch)
        assert "DRAFT:" in user_content
        assert _DRAFT in user_content
