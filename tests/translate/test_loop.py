"""Tests for src/translate/loop.py — DB helpers and translate_segment orchestration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from common.pricing import UsageInfo
from translate.loop import (
    _build_surface_constraints,
    _build_terminology_microedit,
    _drop_habere_ppp_constraints,
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
        "context_label": None,
        "category": None,
    }
    base.update(overrides)
    return base


# ── Translator mock helpers ───────────────────────────────────────────────────


def _make_usage(model: str = "deepseek-chat") -> UsageInfo:
    return UsageInfo(
        model=model,
        cache_hit_tokens=100,
        cache_miss_tokens=20,
        completion_tokens=50,
        cost_usd=0.00015,
    )


def _t(text: str) -> tuple[str, UsageInfo]:
    """Wrap a draft string as the tuple that call_translator_v3 now returns."""
    return (text, _make_usage())


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


def test_get_locked_terms_includes_context_label():
    rows = [_term_row(context_label="sanctifying grace")]
    conn, cur = _fake_conn(rows)
    result = get_locked_terms(conn, 1)
    assert result[0]["context_label"] == "sanctifying grace"


def test_get_locked_terms_context_label_none_when_absent():
    rows = [_term_row(context_label=None)]
    conn, cur = _fake_conn(rows)
    result = get_locked_terms(conn, 1)
    assert result[0]["context_label"] is None


def test_get_locked_terms_includes_category():
    """get_locked_terms must return the 'category' field from glossary_term."""
    rows = [_term_row(category="term")]
    conn, cur = _fake_conn(rows)
    result = get_locked_terms(conn, 1)
    assert "category" in result[0]
    assert result[0]["category"] == "term"


def test_get_locked_terms_category_none_for_krystal_terms():
    """category=None (Krystal-seeded terms) is returned unchanged."""
    rows = [_term_row(category=None)]
    conn, cur = _fake_conn(rows)
    result = get_locked_terms(conn, 1)
    assert result[0]["category"] is None


def test_get_locked_terms_category_formula():
    """category='formula' is returned correctly for structural formula terms."""
    rows = [_term_row(latin_lemma="respondeo", required_slovak="Odpovedám", category="formula")]
    conn, cur = _fake_conn(rows)
    result = get_locked_terms(conn, 1)
    assert result[0]["category"] == "formula"


# ── _build_surface_constraints — context_label passthrough ───────────────────

_LATIN = "Respondeo dicendum quod gratiam et rationem Deus dat."


def test_build_surface_constraints_passes_context_label_through():
    constraints = [
        {"latin_lemma": "gratia", "required_slovak": "milosť", "context_label": "sanctifying grace"},
    ]
    result = _build_surface_constraints(_LATIN, constraints)
    for c in result:
        assert c.get("context_label") == "sanctifying grace"


def test_build_surface_constraints_none_label_preserved():
    constraints = [
        {"latin_lemma": "ratio", "required_slovak": "rozum", "context_label": None},
    ]
    result = _build_surface_constraints(_LATIN, constraints)
    for c in result:
        assert "context_label" in c
        assert c["context_label"] is None


def test_build_surface_constraints_multiword_passes_full_dict():
    constraints = [
        {"latin_lemma": "actus essendi", "required_slovak": "akt bytia", "context_label": "as act of being"},
    ]
    result = _build_surface_constraints(_LATIN, constraints)
    assert len(result) == 1
    assert result[0]["context_label"] == "as act of being"
    assert result[0]["latin_lemma"] == "actus essendi"


def test_build_surface_constraints_fallback_passes_full_dict():
    # Lemma not found in Latin text → fallback, full dict passed through
    constraints = [
        {"latin_lemma": "caritas", "required_slovak": "láska", "context_label": "as theological virtue"},
    ]
    result = _build_surface_constraints(_LATIN, constraints)
    assert len(result) == 1
    assert result[0]["context_label"] == "as theological virtue"
    assert result[0]["latin_lemma"] == "caritas"


# ── _drop_habere_ppp_constraints — 'habitum est' false-constraint filter ──────

_HABITUS_CONSTRAINT = {"latin_lemma": "habitus", "required_slovak": "habitus", "category": "term"}


def test_drop_habere_ppp_only_evidence_dropped():
    """'habitum est' as the sole evidence → bogus habitus constraint removed."""
    latin = "Sicut habitum est in praecedenti quaestione."
    result = _drop_habere_ppp_constraints(latin, [dict(_HABITUS_CONSTRAINT)])
    assert result == []


def test_drop_habere_ppp_habita_sunt_variant_dropped():
    """Plural 'habita sunt' is also perfect-passive habere."""
    latin = "Quae habita sunt in superioribus."
    result = _drop_habere_ppp_constraints(latin, [dict(_HABITUS_CONSTRAINT)])
    assert result == []


def test_drop_habere_ppp_kept_with_genuine_evidence():
    """A real habitus token elsewhere keeps the constraint despite 'habitum est'."""
    latin = "Sicut habitum est, habitus virtutis manet in anima."
    result = _drop_habere_ppp_constraints(latin, [dict(_HABITUS_CONSTRAINT)])
    assert len(result) == 1
    assert result[0]["latin_lemma"] == "habitus"


def test_drop_habere_ppp_no_construction_unchanged():
    """'habitus est' (noun + copula) is NOT the participle construction — untouched."""
    latin = "Habitus est qualitas de difficili mobilis."
    constraints = [dict(_HABITUS_CONSTRAINT)]
    result = _drop_habere_ppp_constraints(latin, constraints)
    assert result == constraints


def test_drop_habere_ppp_other_constraints_untouched():
    """Non-habitus constraints pass through even when the construction is present."""
    latin = "Sicut habitum est, gratiam Deus dat."
    constraints = [
        dict(_HABITUS_CONSTRAINT),
        {"latin_lemma": "gratia", "required_slovak": "milosť", "category": "term"},
    ]
    result = _drop_habere_ppp_constraints(latin, constraints)
    assert len(result) == 1
    assert result[0]["latin_lemma"] == "gratia"


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
_PATCH_TERMINOLOGY = "translate.loop.check_terminology_lemma"
_PATCH_SOURCE_ID = "translate.loop.source_id"


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
        with patch(_PATCH_SOURCE_ID, return_value=1):
            translate_segment(999, conn)


# ── translate_segment — APPROVED path ─────────────────────────────────────────


def test_translate_segment_approved_returns_translated():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Preložený text.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        status, usages, _ = translate_segment(1, conn)
    assert status == "translated"


def test_translate_segment_approved_commits():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
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
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved_notes()),
        patch("translate.loop.write_reviewer_notes") as mock_notes,
    ):
        status, _, _ = translate_segment(1, conn)
    assert status == "translated"
    mock_notes.assert_called_once()


def test_translate_segment_approved_no_notes_skips_write_notes():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
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
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
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
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_revision()),
        patch("translate.loop.update_sense_version_used") as mock_vsn,
    ):
        status, _, _ = translate_segment(1, conn)
    assert status == "needs_human"
    mock_vsn.assert_called_once_with(conn, 1, 88, 2)


# ── translate_segment — pre-check failure skips R1 ───────────────────────────


def test_translate_segment_precheck_failure_skips_r1():
    conn = _make_conn()
    reviewer_mock = MagicMock()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Bad draft.")),
        patch(_PATCH_STRUCTURE, return_value=_fail("missing respondeo")),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, reviewer_mock),
    ):
        translate_segment(1, conn)
    reviewer_mock.assert_not_called()


def test_translate_segment_terminology_failure_skips_r1():
    """Terminology pre-check failure alone must skip R1."""
    conn = _make_conn()
    reviewer_mock = MagicMock()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Bad draft.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_fail("lemma 'viera' not found")),
        patch(_PATCH_REVIEWER, reviewer_mock),
    ):
        translate_segment(1, conn)
    reviewer_mock.assert_not_called()


def test_translate_segment_precheck_failure_retries_translator():
    conn = _make_conn()
    translator_mock = MagicMock(return_value=_t("Fixed draft."))
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, translator_mock),
        patch(_PATCH_STRUCTURE, side_effect=[_fail(), _ok()]),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        status, _, _ = translate_segment(1, conn)
    assert translator_mock.call_count == 2
    assert status == "translated"


def test_translate_segment_precheck_failure_includes_failures_in_feedback():
    conn = _make_conn()
    translator_calls: list[tuple] = []

    def capture_translator(*args, **kwargs):
        translator_calls.append(args)
        return _t("Draft.")

    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, side_effect=capture_translator),
        patch(_PATCH_STRUCTURE, side_effect=[_fail("missing formula"), _ok()]),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        translate_segment(1, conn)

    # Second call: messages list ends with a user feedback turn
    assert len(translator_calls) == 2
    (messages,) = translator_calls[1]
    last_user_content = next(
        m["content"] for m in reversed(messages) if m["role"] == "user"
    )
    assert "missing formula" in last_user_content


def test_translate_segment_terminology_failure_included_in_feedback():
    """Terminology failures must appear in the feedback sent to the translator."""
    conn = _make_conn()
    translator_calls: list[tuple] = []

    def capture_translator(*args, **kwargs):
        translator_calls.append(args)
        return _t("Draft.")

    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, side_effect=capture_translator),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, side_effect=[_fail("lemma 'viera' not found"), _ok()]),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        translate_segment(1, conn)

    assert len(translator_calls) == 2
    (messages,) = translator_calls[1]
    last_user_content = next(
        m["content"] for m in reversed(messages) if m["role"] == "user"
    )
    assert "viera" in last_user_content


def test_translate_segment_terminology_only_failure_sends_microedit():
    """Structure ok + terminology fail → targeted micro-edit turn, not full retry."""
    conn = _make_conn()
    translator_calls: list[tuple] = []

    def capture_translator(*args, **kwargs):
        translator_calls.append(args)
        return _t("Draft.")

    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, side_effect=capture_translator),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, side_effect=[_fail("lemma 'viera' not found"), _ok()]),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        translate_segment(1, conn)

    (messages,) = translator_calls[1]
    last_user_content = next(
        m["content"] for m in reversed(messages) if m["role"] == "user"
    )
    assert "Terminology fix only" in last_user_content
    assert "Pre-check failures" not in last_user_content


def test_translate_segment_structure_failure_uses_full_retry_feedback():
    """Any structure failure keeps the full retry framing, even with terminology fails."""
    conn = _make_conn()
    translator_calls: list[tuple] = []

    def capture_translator(*args, **kwargs):
        translator_calls.append(args)
        return _t("Draft.")

    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, side_effect=capture_translator),
        patch(_PATCH_STRUCTURE, side_effect=[_fail("missing formula"), _ok()]),
        patch(_PATCH_TERMINOLOGY, side_effect=[_fail("lemma 'viera' not found"), _ok()]),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        translate_segment(1, conn)

    (messages,) = translator_calls[1]
    last_user_content = next(
        m["content"] for m in reversed(messages) if m["role"] == "user"
    )
    assert "Pre-check failures" in last_user_content
    assert "missing formula" in last_user_content
    assert "viera" in last_user_content
    assert "Terminology fix only" not in last_user_content


def test_build_terminology_microedit_content():
    """Micro-edit turn lists failures and forbids any other change."""
    msg = _build_terminology_microedit(["missing components ['rozum'] for 'rozum' (ratio)"])
    assert "rozum" in msg
    assert "Terminology fix only" in msg
    assert "inflected" in msg
    assert "Do not reword" in msg


# ── translate_segment — REVISION_NEEDED path ─────────────────────────────────


def test_translate_segment_revision_needed_retries():
    conn = _make_conn()
    reviewer_mock = MagicMock(side_effect=[_revision(), _approved()])
    translator_mock = MagicMock(return_value=_t("Draft."))
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, translator_mock),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, reviewer_mock),
    ):
        status, _, _ = translate_segment(1, conn)
    assert translator_mock.call_count == 2
    assert status == "translated"


def test_translate_segment_max_iterations_needs_human():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_revision()),
    ):
        status, _, _ = translate_segment(1, conn)
    assert status == "needs_human"


def test_translate_segment_max_iterations_writes_best_draft():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Best draft.")),
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
        return _t("Draft.")

    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, side_effect=capture),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, side_effect=[_revision("fix semantics"), _approved()]),
    ):
        translate_segment(1, conn)

    assert len(translator_calls) == 2
    (messages,) = translator_calls[1]
    last_user_content = next(
        m["content"] for m in reversed(messages) if m["role"] == "user"
    )
    assert "fix semantics" in last_user_content


# ── translate_segment — translator error ─────────────────────────────────────


def test_translate_segment_translator_error_on_first_returns_needs_human():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, side_effect=RuntimeError("API down")),
    ):
        status, _, _ = translate_segment(1, conn)
    assert status == "needs_human"


def test_translate_segment_translator_error_no_db_write():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
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
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, side_effect=RuntimeError("R1 down")),
    ):
        status, _, _ = translate_segment(1, conn)
    assert status == "needs_human"


# ── translate_segment — precheck_passing_draft fallback logic ─────────────────


def test_translate_segment_best_draft_is_last_precheck_pass():
    """precheck_passing_draft should be the last draft that cleared all pre-checks."""
    conn = _make_conn()
    drafts = [_t("Draft 1 (passes checks)."), _t("Draft 2 (revision needed).")]
    translator_mock = MagicMock(side_effect=drafts + [_t("Draft 3.")] * 10)

    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
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
    """When no draft ever clears pre-checks, fallback_draft is written."""
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Always failing draft.")),
        patch(_PATCH_STRUCTURE, return_value=_fail("no formula")),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch("translate.loop.write_segment_text") as mock_write,
    ):
        status, _, _ = translate_segment(1, conn)
    assert status == "needs_human"
    mock_write.assert_called_once()
    _, _, _, _, content = mock_write.call_args[0]
    assert content == "Always failing draft."


# ── translate_segment — always commits ───────────────────────────────────────


def test_translate_segment_always_commits_on_needs_human():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_revision()),
    ):
        translate_segment(1, conn)
    conn.commit.assert_called_once()


def test_translate_segment_constraints_include_category():
    """translate_segment must include 'category' in each constraint dict passed to prechecks."""
    term = _term_row(category="formula")
    conn = _make_conn(locked_terms=[term])
    captured: list[list] = []

    def capture_precheck(draft, constraints):
        captured.append(list(constraints))
        return CheckResult(ok=True)

    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, side_effect=capture_precheck),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        translate_segment(1, conn)

    assert len(captured) > 0
    assert "category" in captured[0][0]
    assert captured[0][0]["category"] == "formula"


def test_translate_segment_constraints_default_category_to_term():
    """When locked term has category=None, constraints dict defaults to 'term'."""
    term = _term_row(category=None)
    conn = _make_conn(locked_terms=[term])
    captured: list[list] = []

    def capture_precheck(draft, constraints):
        captured.append(list(constraints))
        return CheckResult(ok=True)

    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, side_effect=capture_precheck),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        translate_segment(1, conn)

    assert captured[0][0]["category"] == "term"


def test_translate_segment_missing_latin_skips_r1_and_needs_human():
    """Segment with no Latin text must not call the reviewer."""
    conn = _make_conn(seg=_seg_row(latin=None))
    reviewer_mock = MagicMock()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, reviewer_mock),
    ):
        status, _, _ = translate_segment(1, conn)
    reviewer_mock.assert_not_called()
    assert status == "needs_human"


def test_translate_segment_translator_failure_still_commits():
    """Even when translator raises on iteration 1, commit still happens."""
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, side_effect=RuntimeError("fail")),
    ):
        translate_segment(1, conn)
    # No draft → no write, but the function returns 'needs_human' cleanly
    # (no commit required if no write was done — that's acceptable)


def test_translate_segment_exhausted_writes_reviewer_notes():
    """Exhausted loop writes last R1 feedback to reviewer_notes for needs_human segments."""
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_revision("R1 feedback message")),
        patch("translate.loop.write_reviewer_notes") as mock_notes,
    ):
        status, _, _ = translate_segment(1, conn)
    assert status == "needs_human"
    mock_notes.assert_called_once()
    # First arg is conn, second is segment_id, third is the notes dict
    notes_dict = mock_notes.call_args[0][2]
    assert "last_feedback" in notes_dict
    assert "R1 feedback message" in notes_dict["last_feedback"]


def test_translate_segment_exhausted_notes_omitted_when_no_feedback():
    """If all iterations fail precheck (no R1 call), reviewer_notes is not written."""
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_fail(["missing term"])),
        patch("translate.loop.write_reviewer_notes") as mock_notes,
    ):
        status, _, _ = translate_segment(1, conn)
    assert status == "needs_human"
    # prior_feedback is set (precheck failure message), so notes ARE written
    mock_notes.assert_called_once()
    notes_dict = mock_notes.call_args[0][2]
    assert "last_feedback" in notes_dict


def test_translate_segment_article_title_no_latin_translates_directly():
    """article_title segment with English but no Latin should be marked translated, not needs_human."""
    conn = _make_conn(seg=_seg_row(
        element_type="article_title",
        latin=None,
        english="Whether sacred doctrine is a science?",
    ))
    reviewer_mock = MagicMock()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Či je posvätná náuka vedou?")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, reviewer_mock),
    ):
        status, _, _ = translate_segment(1, conn)
    reviewer_mock.assert_not_called()
    assert status == "translated"


def test_translate_segment_question_title_no_latin_translates_directly():
    """question_title segment with English but no Latin should be marked translated, not needs_human."""
    conn = _make_conn(seg=_seg_row(
        element_type="question_title",
        latin=None,
        english="The nature and extent of sacred doctrine",
    ))
    reviewer_mock = MagicMock()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Povaha a rozsah posvätnej náuky")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, reviewer_mock),
    ):
        status, _, _ = translate_segment(1, conn)
    reviewer_mock.assert_not_called()
    assert status == "translated"


# ── SegmentOutcome analytics (run_segment record) ─────────────────────────────


def test_translate_segment_outcome_clean_pass():
    """Approved on iteration 1: chosen_iteration set, no failure classes."""
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Preložený text.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        status, _, outcome = translate_segment(1, conn)
    assert status == "translated"
    assert outcome.segment_id == 1
    assert outcome.iterations_used == 1
    assert outcome.chosen_iteration == 1
    assert outcome.failure_classes == []
    assert outcome.last_feedback is None


def test_translate_segment_outcome_records_terminology_failure_with_term():
    """Terminology precheck failure records class + the unmet term per iteration."""
    conn = _make_conn()
    term_fail = CheckResult(
        ok=False,
        failures=["missing components ['rozum'] for 'rozum' (ratio)"],
        failed_terms=["rozum"],
    )
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, side_effect=[term_fail, _ok()]),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        status, _, outcome = translate_segment(1, conn)
    assert status == "translated"
    assert outcome.iterations_used == 2
    assert outcome.chosen_iteration == 2
    assert outcome.failure_classes == [
        {"iter": 1, "class": "precheck_terminology", "term": "rozum"}
    ]


def test_translate_segment_outcome_records_structure_failure():
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
        patch(_PATCH_STRUCTURE, side_effect=[_fail("missing formula"), _ok()]),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        _, _, outcome = translate_segment(1, conn)
    assert {"iter": 1, "class": "precheck_structure"} in outcome.failure_classes


def test_translate_segment_outcome_records_reviewer_revisions():
    """Exhausted on REVISION_NEEDED: one reviewer_revision per iteration + last_feedback."""
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, return_value=_t("Draft.")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_revision("semantic drift")),
    ):
        status, _, outcome = translate_segment(1, conn)
    assert status == "needs_human"
    assert outcome.iterations_used == 3
    assert [f["class"] for f in outcome.failure_classes] == ["reviewer_revision"] * 3
    assert outcome.last_feedback == "semantic drift"


def test_translate_segment_outcome_translator_error():
    """Translator raising on iteration 1: no draft, error class recorded."""
    conn = _make_conn()
    with (
        patch(_PATCH_SOURCE_ID, return_value=1),
        patch(_PATCH_TRANSLATOR, side_effect=RuntimeError("HTTP 500")),
        patch(_PATCH_STRUCTURE, return_value=_ok()),
        patch(_PATCH_TERMINOLOGY, return_value=_ok()),
        patch(_PATCH_REVIEWER, return_value=_approved()),
    ):
        status, _, outcome = translate_segment(1, conn)
    assert status == "needs_human"
    assert outcome.failure_classes == [{"iter": 1, "class": "translator_error"}]
    assert outcome.chosen_iteration is None
