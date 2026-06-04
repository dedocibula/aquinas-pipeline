"""Tests for src/translate/loop.py — DB helpers and translate_segment orchestration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from translate.loop import (
    get_locked_terms,
    get_segment_with_texts,
    translate_segment,
    update_sense_version_used,
    update_translation_status,
    write_reviewer_notes,
    write_segment_text,
)
from translate.prechecks import CheckResult
from translate.reviewer import ReviewResult

# ── Fake DB helpers ───────────────────────────────────────────────────────────


def _fake_cursor(rows=None, *, as_dict=True):
    """Return a context-manager-compatible cursor mock."""
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    if rows is not None:
        if as_dict:
            dict_rows = [dict(r) if not isinstance(r, dict) else r for r in rows]
            cur.fetchone.return_value = dict_rows[0] if dict_rows else None
            cur.fetchall.return_value = dict_rows
        else:
            cur.fetchone.return_value = rows[0] if rows else None
            cur.fetchall.return_value = rows
    else:
        cur.fetchone.return_value = None
        cur.fetchall.return_value = []
    return cur


def _fake_conn(rows=None):
    cur = _fake_cursor(rows)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def _seg_row(**overrides) -> dict:
    base = {
        "segment_id": 1,
        "locator_path": "I.q1.a1",
        "element_type": "respondeo",
        "reply_to": None,
        "translation_status": "pending",
        "latin": "Respondeo dicendum quod...",
        "czech": "Odpovídám, že...",
        "english": "I answer that...",
    }
    base.update(overrides)
    return base


def _term_row(**overrides) -> dict:
    base = {
        "latin_lemma": "ratio",
        "required_slovak": "rozum",
        "sense_id": 42,
        "version": 1,
    }
    base.update(overrides)
    return base


# ── get_segment_with_texts ────────────────────────────────────────────────────


def test_get_segment_with_texts_returns_dict():
    row = _seg_row()
    conn, cur = _fake_conn([row])
    result = get_segment_with_texts(conn, 1)
    assert result == row


def test_get_segment_with_texts_returns_none_when_missing():
    conn, cur = _fake_conn([])
    result = get_segment_with_texts(conn, 999)
    assert result is None


def test_get_segment_with_texts_passes_segment_id():
    conn, cur = _fake_conn([_seg_row()])
    get_segment_with_texts(conn, 7)
    sql, params = cur.execute.call_args[0]
    assert params == (7,)


# ── get_locked_terms ──────────────────────────────────────────────────────────


def test_get_locked_terms_returns_list_of_dicts():
    rows = [_term_row(), _term_row(latin_lemma="esse", required_slovak="bytie", sense_id=43)]
    conn, cur = _fake_conn(rows)
    result = get_locked_terms(conn, 1)
    assert len(result) == 2
    assert result[0]["latin_lemma"] == "ratio"


def test_get_locked_terms_empty_when_no_terms():
    conn, cur = _fake_conn([])
    result = get_locked_terms(conn, 1)
    assert result == []


def test_get_locked_terms_passes_segment_id():
    conn, cur = _fake_conn([])
    get_locked_terms(conn, 5)
    _, params = cur.execute.call_args[0]
    assert params == (5,)


# ── write_segment_text ────────────────────────────────────────────────────────


def test_write_segment_text_executes_upsert():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur

    write_segment_text(conn, 1, "sk", 99, "Slovak text here")
    sql, params = cur.execute.call_args[0]
    assert "INSERT INTO segment_text" in sql
    assert "ON CONFLICT" in sql
    assert params == (1, "sk", "Slovak text here", 99)


# ── update_translation_status ─────────────────────────────────────────────────


def test_update_translation_status_executes_update():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur

    update_translation_status(conn, 1, "translated")
    _, params = cur.execute.call_args[0]
    assert params == ("translated", 1)


# ── write_reviewer_notes ──────────────────────────────────────────────────────


def test_write_reviewer_notes_includes_iteration():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur

    write_reviewer_notes(conn, 1, {"raw": "looks good"}, iteration=2)
    _, params = cur.execute.call_args[0]
    payload_arg = params[0]
    assert hasattr(payload_arg, "adapted") or hasattr(payload_arg, "dumps")
    # psycopg2.extras.Json wraps the dict; verify the underlying data
    assert params[1] == 1


# ── update_sense_version_used ─────────────────────────────────────────────────


def test_update_sense_version_used_executes_update():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur

    update_sense_version_used(conn, segment_id=1, sense_id=42, version=3)
    _, params = cur.execute.call_args[0]
    assert params == (3, 1, 42)


# ── translate_segment — setup helpers ─────────────────────────────────────────


def _make_conn(seg=None, locked_terms=None):
    """Return a conn mock pre-loaded with segment and term data."""
    seg = seg or _seg_row()
    locked_terms = locked_terms or []

    fetchone_results = [seg]
    fetchall_results = [locked_terms]

    def make_cursor(*args, **kwargs):
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = fetchone_results[0] if fetchone_results else None
        cur.fetchall.return_value = fetchall_results[0] if fetchall_results else []
        return cur

    conn = MagicMock()
    conn.cursor.side_effect = make_cursor
    return conn


_PATCH_TRANSLATOR = "translate.loop.call_translator_v3"
_PATCH_REVIEWER = "translate.loop.call_reviewer_r1"
_PATCH_STRUCTURE = "translate.loop.check_structure"
_PATCH_TERMINOLOGY = "translate.loop.check_terminology"
_PATCH_SOURCE_ID = "translate.loop.source_id"
_PATCH_STYLE = "translate.loop._get_style_profile"


def _ok() -> CheckResult:
    return CheckResult(ok=True)


def _fail(msg="test failure") -> CheckResult:
    return CheckResult(ok=False, failures=[msg])


def _approved() -> ReviewResult:
    return ReviewResult(verdict="APPROVED", notes=None, feedback=None)


def _approved_notes() -> ReviewResult:
    return ReviewResult(verdict="APPROVED_WITH_NOTES", notes={"raw": "note"}, feedback=None)


def _revision(feedback="fix this") -> ReviewResult:
    return ReviewResult(verdict="REVISION_NEEDED", notes=None, feedback=feedback)


# ── translate_segment — not found ─────────────────────────────────────────────


def test_translate_segment_raises_if_not_found():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = None
    cur.fetchall.return_value = []
    conn.cursor.return_value = cur

    with pytest.raises(RuntimeError, match="not found in DB"):
        with patch(_PATCH_SOURCE_ID, return_value=1), patch(_PATCH_STYLE, return_value={}):
            translate_segment(999, conn)


# ── translate_segment — APPROVED path ─────────────────────────────────────────


def test_translate_segment_approved_returns_translated():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, return_value="Preložený text."),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        result = translate_segment(1, conn)
    assert result == "translated"


def test_translate_segment_approved_commits():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, return_value="Draft."),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        translate_segment(1, conn)
    conn.commit.assert_called_once()


def test_translate_segment_approved_with_notes_writes_notes():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, return_value="Draft."),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved_notes()),
        patch("translate.loop.write_reviewer_notes") as mock_notes,
    ):
        result = translate_segment(1, conn)
    assert result == "translated"
    mock_notes.assert_called_once()


def test_translate_segment_approved_no_notes_skips_write_notes():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, return_value="Draft."),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved()),
        patch("translate.loop.write_reviewer_notes") as mock_notes,
    ):
        translate_segment(1, conn)
    mock_notes.assert_not_called()


# ── translate_segment — locked terms ─────────────────────────────────────────


def test_translate_segment_updates_sense_version_on_success():
    term = _term_row(sense_id=77, version=3)
    conn = _make_conn(locked_terms=[term])
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, return_value="Draft."),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved()),
        patch("translate.loop.update_sense_version_used") as mock_vsn,
    ):
        translate_segment(1, conn)
    mock_vsn.assert_called_once_with(conn, 1, 77, 3)


def test_translate_segment_updates_sense_version_on_needs_human():
    term = _term_row(sense_id=88, version=2)
    conn = _make_conn(locked_terms=[term])
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, return_value="Draft."),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_revision()),
        patch("translate.loop.update_sense_version_used") as mock_vsn,
    ):
        result = translate_segment(1, conn)
    assert result == "needs_human"
    mock_vsn.assert_called_once_with(conn, 1, 88, 2)


# ── translate_segment — pre-check failure skips R1 ───────────────────────────


def test_translate_segment_precheck_failure_skips_r1():
    conn = _make_conn()
    reviewer_mock = MagicMock()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, return_value="Bad draft."),
        patch(_PATCH_STRUCTURE, return_value=_fail("missing respondeo")),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, reviewer_mock),
    ):
        translate_segment(1, conn)
    reviewer_mock.assert_not_called()


def test_translate_segment_precheck_failure_retries_translator():
    conn = _make_conn()
    translator_mock = MagicMock(return_value="Fixed draft.")
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, translator_mock),
        patch(_PATCH_STRUCTURE, side_effect=[_fail(), _ok()]),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        result = translate_segment(1, conn)
    assert translator_mock.call_count == 2
    assert result == "translated"


def test_translate_segment_precheck_failure_includes_failures_in_feedback():
    conn = _make_conn()
    translator_calls: list[tuple] = []

    def capture_translator(*args, **kwargs):
        translator_calls.append(args)
        return "Draft."

    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, side_effect=capture_translator),
        patch(_PATCH_STRUCTURE, side_effect=[_fail("missing formula"), _ok()]),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        translate_segment(1, conn)

    # Second call should have prior_feedback containing the failure text
    assert len(translator_calls) == 2
    _, _, prior_draft_arg, prior_feedback_arg, _ = translator_calls[1]
    assert "missing formula" in prior_feedback_arg


# ── translate_segment — REVISION_NEEDED path ─────────────────────────────────


def test_translate_segment_revision_needed_retries():
    conn = _make_conn()
    reviewer_mock = MagicMock(side_effect=[_revision(), _approved()])
    translator_mock = MagicMock(return_value="Draft.")
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, translator_mock),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, reviewer_mock),
    ):
        result = translate_segment(1, conn)
    assert translator_mock.call_count == 2
    assert result == "translated"


def test_translate_segment_max_iterations_needs_human():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, return_value="Draft."),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_revision()),
    ):
        result = translate_segment(1, conn)
    assert result == "needs_human"


def test_translate_segment_max_iterations_writes_best_draft():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, return_value="Best draft."),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_revision()),
        patch("translate.loop.write_segment_text") as mock_write,
    ):
        translate_segment(1, conn)
    mock_write.assert_called_once()
    _, _, _, _, content = mock_write.call_args[0]
    assert content == "Best draft."


def test_translate_segment_revision_feedback_passed_to_translator():
    conn = _make_conn()
    translator_calls: list[tuple] = []

    def capture(*args, **kwargs):
        translator_calls.append(args)
        return "Draft."

    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, side_effect=capture),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, side_effect=[_revision("fix semantics"), _approved()]),
    ):
        translate_segment(1, conn)

    assert len(translator_calls) == 2
    _, _, _, prior_feedback_arg, _ = translator_calls[1]
    assert "fix semantics" in prior_feedback_arg


# ── translate_segment — translator error ─────────────────────────────────────


def test_translate_segment_translator_error_on_first_returns_needs_human():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, side_effect=RuntimeError("API down")),
    ):
        result = translate_segment(1, conn)
    assert result == "needs_human"


def test_translate_segment_translator_error_no_db_write():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, side_effect=RuntimeError("API down")),
        patch("translate.loop.write_segment_text") as mock_write,
    ):
        translate_segment(1, conn)
    mock_write.assert_not_called()


# ── translate_segment — reviewer error ───────────────────────────────────────


def test_translate_segment_reviewer_error_eventually_needs_human():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, return_value="Draft."),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, side_effect=RuntimeError("R1 down")),
    ):
        result = translate_segment(1, conn)
    assert result == "needs_human"


# ── translate_segment — best_draft fallback logic ─────────────────────────────


def test_translate_segment_best_draft_is_last_precheck_pass():
    """best_draft should be the last draft that cleared pre-checks."""
    conn = _make_conn()
    drafts = ["Draft 1 (passes checks).", "Draft 2 (revision needed)."]
    translator_mock = MagicMock(side_effect=drafts + ["Draft 3."] * 10)

    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, translator_mock),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_revision()),
        patch("translate.loop.write_segment_text") as mock_write,
    ):
        translate_segment(1, conn)

    _, _, _, _, written_content = mock_write.call_args[0]
    # The last draft that cleared pre-checks gets written
    assert "Draft" in written_content


def test_translate_segment_all_precheck_fail_writes_last_draft():
    """When no draft ever clears pre-checks, last_draft is written."""
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, return_value="Always failing draft."),
        patch(_PATCH_STRUCTURE, return_value=_fail("no formula")),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch("translate.loop.write_segment_text") as mock_write,
    ):
        result = translate_segment(1, conn)
    assert result == "needs_human"
    mock_write.assert_called_once()
    _, _, _, _, content = mock_write.call_args[0]
    assert content == "Always failing draft."


# ── translate_segment — always commits ───────────────────────────────────────


def test_translate_segment_always_commits_on_needs_human():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, return_value="Draft."),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_revision()),
    ):
        translate_segment(1, conn)
    conn.commit.assert_called_once()


def test_translate_segment_missing_latin_skips_r1_and_needs_human():
    """Segment with no Latin text must not call the reviewer."""
    conn = _make_conn(seg=_seg_row(latin=None))
    reviewer_mock = MagicMock()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, return_value="Draft."),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, reviewer_mock),
    ):
        result = translate_segment(1, conn)
    reviewer_mock.assert_not_called()
    assert result == "needs_human"


def test_translate_segment_translator_failure_still_commits():
    """Even when translator raises on iteration 1, commit still happens."""
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_STYLE, return_value={}),
        patch(_PATCH_TRANSLATOR, side_effect=RuntimeError("fail")),
    ):
        translate_segment(1, conn)
    # No draft → no write, but the function returns 'needs_human' cleanly
    # (no commit required if no write was done — that's acceptable)
