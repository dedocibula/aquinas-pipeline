"""Unit tests for src/polish/polisher.py.

All DB and API calls are mocked. The _client parameter injects a PolisherBase
subclass (or MagicMock with a .polish method). SegmentRepository is patched to
control what text is "in" the DB without touching a real connection.
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
        model="deepseek-v4-flash",
        cache_hit_tokens=200,
        cache_miss_tokens=50,
        completion_tokens=40,
        cost_usd=0.00065,
    )


def _fake_client(content: str = POLISHED_TEXT) -> MagicMock:
    """Return a mock PolisherBase — implements .polish(user_content, system)."""
    c = MagicMock()
    c.polish.return_value = ChatResult(content=content, usage=_make_usage(), raw={})
    return c


def _make_seg_repo(human: str | None = None, model: str | None = MODEL_TEXT) -> MagicMock:
    """Return a mock SegmentRepository with configurable sk text returns."""
    repo = MagicMock()

    def _get_sk(seg_id, src_code):
        if src_code == "human":
            return human
        if src_code == "model":
            return model
        return None

    repo.get_sk_text.side_effect = _get_sk
    repo.get_reviewer_notes_text.return_value = None
    return repo


def _mock_gloss_repo(locked_terms=None) -> MagicMock:
    repo = MagicMock()
    repo.locked_terms.return_value = locked_terms or []
    return repo


# ── skip when (sk, human) exists ─────────────────────────────────────────────


def test_polish_segment_skips_when_human_exists():
    conn = MagicMock()
    seg_repo = _make_seg_repo(human="human draft")

    with patch("polish.polisher.SegmentRepository", return_value=seg_repo):
        status, usages, outcome = polish_segment(42, conn, _client=_fake_client())

    assert status == "skipped"
    assert usages == []
    assert isinstance(outcome, PolishOutcome)
    assert outcome.segment_id == 42
    assert outcome.guard_flags == {}
    conn.commit.assert_not_called()


# ── no_source when no (sk, model) text ───────────────────────────────────────


def test_polish_segment_no_source_when_no_model_text():
    conn = MagicMock()
    seg_repo = _make_seg_repo(human=None, model=None)

    with patch("polish.polisher.SegmentRepository", return_value=seg_repo):
        status, usages, outcome = polish_segment(42, conn, _client=_fake_client())

    assert status == "no_source"
    assert usages == []
    conn.commit.assert_not_called()


# ── error on client exception ─────────────────────────────────────────────────


def test_polish_segment_error_on_client_exception():
    conn = MagicMock()
    bad_client = MagicMock()
    bad_client.polish.side_effect = RuntimeError("API down")
    seg_repo = _make_seg_repo()

    with patch("polish.polisher.SegmentRepository", return_value=seg_repo):
        with patch("polish.polisher.GlossaryRepository", return_value=_mock_gloss_repo()):
            status, usages, outcome = polish_segment(42, conn, _client=bad_client)

    assert status == "error"
    assert usages == []
    assert outcome.guard_flags == {}
    conn.commit.assert_not_called()


# ── happy path ────────────────────────────────────────────────────────────────


def test_polish_segment_happy_path():
    conn = MagicMock()
    seg_repo = _make_seg_repo()

    with patch("polish.polisher.SegmentRepository", return_value=seg_repo):
        with patch("polish.polisher.GlossaryRepository", return_value=_mock_gloss_repo()):
            with patch("polish.polisher.source_id", return_value=POLISH_SRC_ID):
                with patch("polish.polisher.run_guards", return_value=_GUARD_OK):
                    status, usages, outcome = polish_segment(
                        42, conn, _client=_fake_client()
                    )

    assert status == "polished"
    assert len(usages) == 1
    assert usages[0].model == "deepseek-v4-flash"
    assert isinstance(outcome, PolishOutcome)
    assert outcome.segment_id == 42
    assert outcome.guard_flags == _GUARD_OK
    seg_repo.write_segment_text.assert_called_once_with(42, "sk", POLISH_SRC_ID, POLISHED_TEXT)
    conn.commit.assert_called_once()


# ── (sk, model) must not be modified ─────────────────────────────────────────


def test_polish_segment_does_not_touch_model_row():
    """write_segment_text is called exactly once, with the polish source id."""
    conn = MagicMock()
    seg_repo = _make_seg_repo()

    with patch("polish.polisher.SegmentRepository", return_value=seg_repo):
        with patch("polish.polisher.GlossaryRepository", return_value=_mock_gloss_repo()):
            with patch("polish.polisher.source_id", return_value=POLISH_SRC_ID):
                with patch("polish.polisher.run_guards", return_value=_GUARD_OK):
                    polish_segment(42, conn, _client=_fake_client())

    calls = seg_repo.write_segment_text.call_args_list
    assert len(calls) == 1
    positional_args = calls[0][0]
    assert positional_args[2] == POLISH_SRC_ID


# ── guard flags are advisory: polished always written regardless ──────────────


def test_polish_segment_writes_even_on_guard_failure():
    """Guards are advisory in polish_segment — write happens even when ok=False."""
    conn = MagicMock()
    seg_repo = _make_seg_repo()
    bad_flags = {**_GUARD_OK, "ok": False, "sentence_delta": 1}

    with patch("polish.polisher.SegmentRepository", return_value=seg_repo):
        with patch("polish.polisher.GlossaryRepository", return_value=_mock_gloss_repo()):
            with patch("polish.polisher.source_id", return_value=POLISH_SRC_ID):
                with patch("polish.polisher.run_guards", return_value=bad_flags):
                    status, _, outcome = polish_segment(42, conn, _client=_fake_client())

    assert status == "polished"
    assert outcome.guard_flags["ok"] is False
    seg_repo.write_segment_text.assert_called_once()
    conn.commit.assert_called_once()


# ── polisher receives user_content and system prompt ─────────────────────────


def test_polish_segment_calls_polisher_with_content_and_system():
    conn = MagicMock()
    client = _fake_client()
    seg_repo = _make_seg_repo()

    with patch("polish.polisher.SegmentRepository", return_value=seg_repo):
        with patch("polish.polisher.GlossaryRepository", return_value=_mock_gloss_repo()):
            with patch("polish.polisher.source_id", return_value=POLISH_SRC_ID):
                with patch("polish.polisher.run_guards", return_value=_GUARD_OK):
                    polish_segment(42, conn, _client=client)

    client.polish.assert_called_once()
    args, _ = client.polish.call_args
    user_content, system = args
    assert MODEL_TEXT in user_content
    assert system and len(system) > 50


# ── usage is always captured from result ─────────────────────────────────────


def test_polish_segment_usage_always_returned():
    conn = MagicMock()
    seg_repo = _make_seg_repo()

    with patch("polish.polisher.SegmentRepository", return_value=seg_repo):
        with patch("polish.polisher.GlossaryRepository", return_value=_mock_gloss_repo()):
            with patch("polish.polisher.source_id", return_value=POLISH_SRC_ID):
                with patch("polish.polisher.run_guards", return_value=_GUARD_OK):
                    _, usages, _ = polish_segment(42, conn, _client=_fake_client())

    assert len(usages) == 1
    assert usages[0].cost_usd == pytest.approx(0.00065)


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

    captured: list[str] = []

    def capture_polish(user_content, system):
        captured.append(user_content)
        return ChatResult(content=POLISHED_TEXT, usage=_make_usage(), raw={})

    client = MagicMock()
    client.polish.side_effect = capture_polish
    seg_repo = _make_seg_repo()

    with patch("polish.polisher.SegmentRepository", return_value=seg_repo):
        with patch("polish.polisher.GlossaryRepository", return_value=mock_gloss):
            with patch("polish.polisher.source_id", return_value=POLISH_SRC_ID):
                polish_segment(42, conn, _client=client)

    user_content = captured[0]
    assert 'latin="ratio"' in user_content
    assert "rozum" in user_content
    assert user_content.count('latin="ratio"') == 1
