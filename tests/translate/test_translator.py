"""
Tests for translate.translator — no real API calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ── Fixtures ───────────────────────────────────────────────────────────────────

def _fake_response(content: str, status_code: int = 200):
    mock = MagicMock()
    if status_code != 200:
        http_err = __import__("requests").HTTPError(response=MagicMock(status_code=status_code))
        http_err.response = MagicMock(status_code=status_code)
        mock.raise_for_status.side_effect = http_err
    else:
        mock.raise_for_status = lambda: None
    mock.json.return_value = {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 150,
            "completion_tokens": 40,
            "prompt_cache_hit_tokens": 120,
            "prompt_cache_miss_tokens": 30,
        },
    }
    return mock


_MINIMAL_SEG = {
    "segment_id": 42,
    "locator_path": "I.q1.a1.arg1",
    "element_type": "arg",
    "latin": "Videtur quod Deus non sit.",
    "czech": "Zdá se, že Bůh není.",
    "english": "It seems that God does not exist.",
}

_CONSTRAINTS = [
    {"latin_lemma": "Deus", "required_slovak": "Boh"},
    {"latin_lemma": "esse", "required_slovak": "byť"},
]


# ── TestCallTranslatorV3 ───────────────────────────────────────────────────────

class TestCallTranslatorV3:
    def test_returns_nonempty_string_on_success(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        expected = "Zdá sa, že Boh nejestvuje."
        with patch("translate.translator.requests.post") as mock_post:
            mock_post.return_value = _fake_response(expected)
            from translate.translator import call_translator_v3
            draft, usage = call_translator_v3(_MINIMAL_SEG, _CONSTRAINTS, None, None)
        assert isinstance(draft, str)
        assert draft == expected
        assert usage.cost_usd > 0

    def test_system_prompt_contains_negative_constraints(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        with patch("translate.translator.requests.post") as mock_post:
            mock_post.return_value = _fake_response("Preklad.")
            from translate.translator import call_translator_v3
            call_translator_v3(_MINIMAL_SEG, [], None, None)

        call_args = mock_post.call_args
        messages = call_args.kwargs["json"]["messages"]
        system_msg = next(m for m in messages if m["role"] == "system")
        assert "NEGATIVE CONSTRAINTS" in system_msg["content"]
        assert "Nezvyšovať literárnu kvalitu nad originál." in system_msg["content"]

    def test_user_turn_contains_hard_term_constraints(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        with patch("translate.translator.requests.post") as mock_post:
            mock_post.return_value = _fake_response("Preklad.")
            from translate.translator import call_translator_v3
            call_translator_v3(_MINIMAL_SEG, _CONSTRAINTS, None, None)

        call_args = mock_post.call_args
        messages = call_args.kwargs["json"]["messages"]
        user_msg = next(m for m in messages if m["role"] == "user")
        assert "HARD TERM CONSTRAINTS" in user_msg["content"]
        assert "Deus → Boh" in user_msg["content"]
        assert "esse → byť" in user_msg["content"]

    def test_user_turn_contains_latin_text(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        with patch("translate.translator.requests.post") as mock_post:
            mock_post.return_value = _fake_response("Preklad.")
            from translate.translator import call_translator_v3
            call_translator_v3(_MINIMAL_SEG, [], None, None)

        call_args = mock_post.call_args
        messages = call_args.kwargs["json"]["messages"]
        user_msg = next(m for m in messages if m["role"] == "user")
        assert _MINIMAL_SEG["latin"] in user_msg["content"]

    def test_revision_user_turn_contains_prior_draft_and_feedback(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        prior_draft = "Zdá sa, že Boh nejestvuje."
        prior_feedback = "Použite 'existovať' namiesto 'jestvovať'."
        with patch("translate.translator.requests.post") as mock_post:
            mock_post.return_value = _fake_response("Revidovaný preklad.")
            from translate.translator import call_translator_v3
            call_translator_v3(
                _MINIMAL_SEG, [], prior_draft, prior_feedback
            )

        call_args = mock_post.call_args
        messages = call_args.kwargs["json"]["messages"]
        user_msg = next(m for m in messages if m["role"] == "user")
        assert "PRIOR DRAFT:" in user_msg["content"]
        assert prior_draft in user_msg["content"]
        assert "REVIEWER FEEDBACK" in user_msg["content"]
        assert prior_feedback in user_msg["content"]

    def test_raises_runtime_error_when_api_key_missing(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        from translate.translator import call_translator_v3
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            call_translator_v3(_MINIMAL_SEG, [], None, None)

    def test_raises_runtime_error_on_http_401(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "bad-key")
        with patch("translate.translator.requests.post") as mock_post:
            mock_post.return_value = _fake_response("", status_code=401)
            from translate.translator import call_translator_v3
            with pytest.raises(RuntimeError, match="401"):
                call_translator_v3(_MINIMAL_SEG, [], None, None)

    def test_raises_runtime_error_on_empty_choices(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        empty_choices = MagicMock()
        empty_choices.raise_for_status = lambda: None
        empty_choices.json.return_value = {"choices": []}
        with patch("translate.translator.requests.post", return_value=empty_choices):
            from translate.translator import call_translator_v3
            with pytest.raises(RuntimeError, match="no choices"):
                call_translator_v3(_MINIMAL_SEG, [], None, None)


# ── TestLoadTranslatorSystemPrompt ────────────────────────────────────────────

class TestLoadTranslatorSystemPrompt:
    def test_raises_runtime_error_when_file_not_found(self, monkeypatch, tmp_path):
        import translate.translator as mod
        mod.load_translator_system_prompt.cache_clear()
        monkeypatch.setattr(mod, "_PROMPTS_DIR", tmp_path)
        with pytest.raises(RuntimeError, match="translator_system.txt not found"):
            mod.load_translator_system_prompt()

    def test_returns_nonempty_string_when_file_exists(self):
        from translate.translator import load_translator_system_prompt
        result = load_translator_system_prompt()
        assert isinstance(result, str)
        assert len(result) > 0
        assert "NEGATIVE CONSTRAINTS" in result
