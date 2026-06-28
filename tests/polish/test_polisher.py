"""Unit tests for src/polish/polisher.py.

All DB and Anthropic calls are mocked.  The _client parameter injects a fake
AnthropicClient; polish.polisher._get_sk_text is patched to control what text is
"in" the DB without touching a real connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from common.deepseek_client import ChatResult
from common.pricing import UsageInfo
from polish.polisher import PolishOutcome, polish_segment

MODEL_TEXT = "Teda Boh je dokonalý a dobrý."
POLISHED_TEXT = "Teda Boh je dokonalý a dobrotivý."
POLISH_SRC_ID = 8

_GUARD_OK = {
    "ok": True,
    "sentence_delta": 0,
    "term_retention_ok": True,
    "missing_terms": [],
    "particle_retention_ok": True,
    "missing_particles": [],
    "length_ratio": 1.02,
}


def _make_usage() -> UsageInfo:
    return UsageInfo(
        model="claude-sonnet-4-6",
        cache_hit_tokens=200,
        cache_miss_tokens=50,
        completion_tokens=40,
        cost_usd=0.00065,
    )


def _fake_client(content: str = POLISHED_TEXT) -> MagicMock:
    c = MagicMock()
    c.chat.return_value = ChatResult(content=content, usage=_make_usage(), raw={})
    return c


def _mock_seg_repo() -> MagicMock:
    return MagicMock()


def _mock_gloss_repo(locked_terms=None) -> MagicMock:
    repo = MagicMock()
    repo.locked_terms.return_value = locked_terms or []
    return repo


# ── skip when (sk, human) exists ─────────────────────────────────────────────


def test_polish_segment_skips_when_human_exists():
    conn = MagicMock()
    with patch("polish.polisher._get_sk_text", return_value="human draft") as mock_get:
        status, usages, outcome = polish_segment(42, conn, _client=_fake_client())

    assert status == "skipped"
    assert usages == []
    assert isinstance(outcome, PolishOutcome)
    assert outcome.segment_id == 42
    assert outcome.guard_flags == {}
    # human check was the only call; no model text lookup
    mock_get.assert_called_once_with(conn, 42, "human")
    conn.commit.assert_not_called()


# ── no_source when no (sk, model) text ───────────────────────────────────────


def test_polish_segment_no_source_when_no_model_text():
    conn = MagicMock()
    # first call (human) → None; second call (model) → None
    with patch("polish.polisher._get_sk_text", side_effect=[None, None]):
        status, usages, outcome = polish_segment(42, conn, _client=_fake_client())

    assert status == "no_source"
    assert usages == []
    conn.commit.assert_not_called()


# ── error on client exception ─────────────────────────────────────────────────


def test_polish_segment_error_on_client_exception():
    conn = MagicMock()
    bad_client = MagicMock()
    bad_client.chat.side_effect = RuntimeError("Anthropic API down")

    with patch("polish.polisher._get_sk_text", side_effect=[None, MODEL_TEXT]):
        with patch("polish.polisher.GlossaryRepository", return_value=_mock_gloss_repo()):
            status, usages, outcome = polish_segment(42, conn, _client=bad_client)

    assert status == "error"
    assert usages == []
    assert outcome.guard_flags == {}
    conn.commit.assert_not_called()


# ── happy path ────────────────────────────────────────────────────────────────


def test_polish_segment_happy_path():
    conn = MagicMock()
    mock_repo = _mock_seg_repo()

    with patch("polish.polisher._get_sk_text", side_effect=[None, MODEL_TEXT]):
        with patch("polish.polisher.SegmentRepository", return_value=mock_repo):
            with patch("polish.polisher.GlossaryRepository", return_value=_mock_gloss_repo()):
                with patch("polish.polisher.source_id", return_value=POLISH_SRC_ID):
                    with patch("polish.polisher.run_guards", return_value=_GUARD_OK):
                        status, usages, outcome = polish_segment(
                            42, conn, _client=_fake_client()
                        )

    assert status == "polished"
    assert len(usages) == 1
    assert usages[0].model == "claude-sonnet-4-6"
    assert isinstance(outcome, PolishOutcome)
    assert outcome.segment_id == 42
    assert outcome.guard_flags == _GUARD_OK
    mock_repo.write_segment_text.assert_called_once_with(42, "sk", POLISH_SRC_ID, POLISHED_TEXT)
    conn.commit.assert_called_once()


# ── (sk, model) must not be modified ─────────────────────────────────────────


def test_polish_segment_does_not_touch_model_row():
    """write_segment_text is called exactly once, with the polish source id."""
    conn = MagicMock()
    mock_repo = _mock_seg_repo()

    with patch("polish.polisher._get_sk_text", side_effect=[None, MODEL_TEXT]):
        with patch("polish.polisher.SegmentRepository", return_value=mock_repo):
            with patch("polish.polisher.GlossaryRepository", return_value=_mock_gloss_repo()):
                with patch("polish.polisher.source_id", return_value=POLISH_SRC_ID):
                    with patch("polish.polisher.run_guards", return_value=_GUARD_OK):
                        polish_segment(42, conn, _client=_fake_client())

    calls = mock_repo.write_segment_text.call_args_list
    assert len(calls) == 1
    # call signature: write_segment_text(segment_id, lang, src_id, content)
    positional_args = calls[0][0]
    assert positional_args[2] == POLISH_SRC_ID


# ── guard flags are advisory: polished always written regardless ──────────────


def test_polish_segment_writes_even_on_guard_failure():
    """Guards are advisory in Phase 2 — write happens even when ok=False."""
    conn = MagicMock()
    mock_repo = _mock_seg_repo()
    bad_flags = {**_GUARD_OK, "ok": False, "sentence_delta": 1}

    with patch("polish.polisher._get_sk_text", side_effect=[None, MODEL_TEXT]):
        with patch("polish.polisher.SegmentRepository", return_value=mock_repo):
            with patch("polish.polisher.GlossaryRepository", return_value=_mock_gloss_repo()):
                with patch("polish.polisher.source_id", return_value=POLISH_SRC_ID):
                    with patch("polish.polisher.run_guards", return_value=bad_flags):
                        status, _, outcome = polish_segment(42, conn, _client=_fake_client())

    assert status == "polished"
    assert outcome.guard_flags["ok"] is False
    mock_repo.write_segment_text.assert_called_once()
    conn.commit.assert_called_once()


# ── AnthropicClient receives system prompt and user content ──────────────────


def test_polish_segment_calls_client_with_system_and_user():
    conn = MagicMock()
    client = _fake_client()

    with patch("polish.polisher._get_sk_text", side_effect=[None, MODEL_TEXT]):
        with patch("polish.polisher.GlossaryRepository", return_value=_mock_gloss_repo()):
            with patch("polish.polisher.source_id", return_value=POLISH_SRC_ID):
                with patch("polish.polisher.run_guards", return_value=_GUARD_OK):
                    polish_segment(42, conn, _client=client)

    client.chat.assert_called_once()
    args, kwargs = client.chat.call_args
    messages = args[0]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert MODEL_TEXT in messages[0]["content"]
    assert kwargs.get("max_tokens") == 2048
    # system prompt is set (non-empty string)
    system = kwargs.get("system")
    assert system and len(system) > 50


# ── usage is always captured from result ─────────────────────────────────────


def test_polish_segment_usage_always_returned():
    """result.usage is unconditionally added to usages (ChatResult.usage is non-optional)."""
    conn = MagicMock()

    with patch("polish.polisher._get_sk_text", side_effect=[None, MODEL_TEXT]):
        with patch("polish.polisher.GlossaryRepository", return_value=_mock_gloss_repo()):
            with patch("polish.polisher.source_id", return_value=POLISH_SRC_ID):
                with patch("polish.polisher.run_guards", return_value=_GUARD_OK):
                    _, usages, _ = polish_segment(42, conn, _client=_fake_client())

    assert len(usages) == 1
    assert usages[0].cost_usd == pytest.approx(0.00065)  # noqa: E501


# ── constraints block contains required_slovak but no Latin surface duplicates ─


def test_polish_segment_constraints_use_lemma_form():
    """Polisher passes lemma-form constraints (no CLTK surface expansion)."""
    from storage.models import Constraint

    conn = MagicMock()
    constraint = Constraint(
        latin_lemma="ratio",
        required_slovak="rozum",
        context_label=None,
        category="term",
        sense_id=1,
        version=1,
    )
    mock_gloss = MagicMock()
    mock_gloss.locked_terms.return_value = [constraint]

    captured_messages = []

    def capture_chat(messages, **kwargs):
        captured_messages.extend(messages)
        return ChatResult(content=POLISHED_TEXT, usage=_make_usage(), raw={})

    client = MagicMock()
    client.chat.side_effect = capture_chat

    with patch("polish.polisher._get_sk_text", side_effect=[None, MODEL_TEXT]):
        with patch("polish.polisher.GlossaryRepository", return_value=mock_gloss):
            with patch("polish.polisher.source_id", return_value=POLISH_SRC_ID):
                polish_segment(42, conn, _client=client)

    user_content = captured_messages[0]["content"]
    # The lemma "ratio" must appear (not inflected Latin surface forms like "rationem")
    assert 'latin="ratio"' in user_content
    assert "rozum" in user_content
    # No surface-expansion duplicates: "ratio" appears exactly once in hard_constraints
    assert user_content.count('latin="ratio"') == 1
