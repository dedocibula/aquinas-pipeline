"""
DB-free unit tests for src/server/app.py and src/server/db.py.

Tests cover:
  1–3.  url_to_ltree conversion
  4–8.  Route responses (monkeypatched DB helpers)
  9–15. review_segment DB unit tests
  16–22. /api/segment/<id>/review route tests
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# url_to_ltree unit tests
# ---------------------------------------------------------------------------


def test_url_to_ltree_standard():
    from server.app import url_to_ltree
    assert url_to_ltree("ST.I.Q3.A1") == "I.q3.a1"


def test_url_to_ltree_hyphenated_pars():
    """Hyphens in pars labels must be preserved without crashing."""
    from server.app import url_to_ltree
    result = url_to_ltree("ST.II-I.Q1.A1")
    # Should not raise; hyphen in pars is kept, Q/A lowered
    assert "q1" in result
    assert "a1" in result


def test_url_to_ltree_no_st_prefix():
    """Pars label preserved; Q/A labels still lowercased."""
    from server.app import url_to_ltree
    assert url_to_ltree("I.Q3.A1") == "I.q3.a1"
    assert url_to_ltree("I.q3") == "I.q3"


# ---------------------------------------------------------------------------
# Fake DB data fixtures
# ---------------------------------------------------------------------------

FAKE_QUESTIONS = [
    {"question_path": "I.q1"},
    {"question_path": "I.q2"},
]

FAKE_ARTICLES = [
    {
        "article_path": "I.q3.a1",
        "translated_count": 5,
        "needs_human_count": 0,
        "reviewed_count": 0,
        "total_count": 7,
    },
]

FAKE_ARTICLES_WITH_NEEDS_HUMAN = [
    {
        "article_path": "I.q3.a1",
        "translated_count": 2,
        "needs_human_count": 3,
        "reviewed_count": 1,
        "total_count": 7,
    },
    {
        "article_path": "I.q3.a2",
        "translated_count": 7,
        "needs_human_count": 0,
        "reviewed_count": 0,
        "total_count": 7,
    },
]

FAKE_QUESTIONS_BY_STATUS = [
    {"question_path": "I.q3", "segment_count": 4, "reviewed_count": 0},
    {"question_path": "II-I.q1", "segment_count": 2, "reviewed_count": 0},
]

FAKE_SEGMENTS = [
    {
        "segment_id": 1,
        "locator_path": "I.q3.a1.arg1",
        "element_type": "arg",
        "reply_to": None,
        "translation_status": "pending",
        "reviewer_notes": None,
        "latin": "Videtur quod non.",
        "czech": "Zdá se, že ne.",
        "english": "It seems that not.",
        "slovak_model": None,
        "slovak_polish": None,
        "slovak_human": None,
        "human_note": None,
        "human_reviewed_by": None,
        "human_version": 0,
    },
    {
        "segment_id": 2,
        "locator_path": "I.q3.a1.sed_contra",
        "element_type": "sed_contra",
        "reply_to": None,
        "translation_status": "translated",
        "reviewer_notes": None,
        "latin": "Sed contra est quod.",
        "czech": "Avšak proti tomu.",
        "english": "On the contrary.",
        "slovak_model": "Na druhej strane:",
        "slovak_polish": None,
        "slovak_human": None,
        "human_note": None,
        "human_reviewed_by": None,
        "human_version": 0,
    },
    {
        "segment_id": 3,
        "locator_path": "I.q3.a1.respondeo",
        "element_type": "respondeo",
        "reply_to": None,
        "translation_status": "translated",
        "reviewer_notes": "Checked by reviewer",
        "latin": "Respondeo dicendum.",
        "czech": "Odpovídám.",
        "english": "I answer that.",
        "slovak_model": "Odpoveď:",
        "slovak_polish": None,
        "slovak_human": None,
        "human_note": None,
        "human_reviewed_by": None,
        "human_version": 0,
    },
    {
        "segment_id": 4,
        "locator_path": "I.q3.a1.reply1",
        "element_type": "reply",
        "reply_to": 1,
        "translation_status": "translated",
        "reviewer_notes": None,
        "latin": "Ad primum dicendum.",
        "czech": "K první námitce.",
        "english": "Reply to objection 1.",
        "slovak_model": "K námietke 1.",
        "slovak_polish": None,
        "slovak_human": None,
        "human_note": None,
        "human_reviewed_by": None,
        "human_version": 0,
    },
]

FAKE_PROGRESS = {"pending": 10, "translated": 5, "needs_human": 2, "reviewed": 1}
FAKE_NAV = {"prev": "I.q3.a0", "next": "I.q3.a2"}


# ---------------------------------------------------------------------------
# Helper: fake get_conn context manager
# ---------------------------------------------------------------------------


def make_fake_get_conn():
    """Return a context manager that yields a stub connection object."""
    stub_conn = MagicMock()

    @contextmanager
    def fake_get_conn():
        yield stub_conn

    return fake_get_conn


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """Flask test client with DB helpers and formula loader patched."""
    # Import here so the module is loaded under patch context.
    with (
        patch("server.app.get_conn", make_fake_get_conn()),
        patch("server.app.get_structural_formulas", return_value={}),
        patch("server.app.get_all_questions",         return_value=FAKE_QUESTIONS),
        patch("server.app.get_question_articles",     return_value=FAKE_ARTICLES),
        patch("server.app.get_article_segments",      return_value=FAKE_SEGMENTS),
        patch("server.app.get_prev_next_article",     return_value=FAKE_NAV),
        patch("server.app.get_translation_progress",  return_value=FAKE_PROGRESS),
        patch("server.app.get_questions_by_status",   return_value=FAKE_QUESTIONS_BY_STATUS),
    ):
        # Reset formula cache so before_request fires during test.
        import server.app as _app_module
        from server.app import app
        _app_module._formulas = {}

        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c


def test_index_returns_200(client):
    """GET / returns HTTP 200."""
    response = client.get("/")
    assert response.status_code == 200


def test_index_omits_proposals_badge_for_non_admin(client):
    resp = client.get("/")
    assert b"glossary proposals" not in resp.data


def test_index_shows_proposals_badge_for_admin(client):
    with client.session_transaction() as sess:
        sess["email"] = "admin@example.com"
        sess["is_editor"] = True
        sess["is_admin"] = True
    with patch("server.app.get_pending_proposal_count", return_value=3):
        resp = client.get("/")
    assert b"3 glossary proposals" in resp.data
    assert b'href="/glossary/proposals"' in resp.data


def test_article_view_returns_200(client):
    """GET /~ST.I.Q3.A1 returns 200 when segments are present."""
    response = client.get("/~ST.I.Q3.A1")
    assert response.status_code == 200


def test_question_view_returns_200(client):
    """GET /~ST.I.Q3 returns 200 when articles are present."""
    response = client.get("/~ST.I.Q3")
    assert response.status_code == 200


def test_article_view_404_when_empty(client):
    """GET /~ST.I.Q3.A1 returns 404 when no segments returned."""
    with patch("server.app.get_article_segments", return_value=[]):
        response = client.get("/~ST.I.Q3.A1")
    assert response.status_code == 404


def test_article_view_has_ref_lang_dropdown(client):
    """Article view includes a <select> with Latin, Czech, English options."""
    response = client.get("/~ST.I.Q3.A1")
    html = response.data.decode()
    assert 'id="ref-lang-select"' in html
    assert '<option value="la">Latin</option>' in html
    assert '<option value="cs">Czech</option>' in html
    assert '<option value="en">English</option>' in html


def test_article_view_embeds_all_ref_language_spans(client):
    """Each reference cell has three spans: la (visible), cs and en (hidden)."""
    response = client.get("/~ST.I.Q3.A1")
    html = response.data.decode()
    # Latin span visible by default (no inline display:none)
    assert 'class="ref-text" data-lang="la"' in html
    # Czech and English spans hidden by default
    assert 'data-lang="cs" style="display:none"' in html
    assert 'data-lang="en" style="display:none"' in html
    # Actual Czech and English content present
    assert "Zdá se, že ne." in html
    assert "It seems that not." in html


def test_article_view_has_switcher_script(client):
    """Article view includes the JS listener for the language switcher."""
    response = client.get("/~ST.I.Q3.A1")
    html = response.data.decode()
    assert "ref-lang-select" in html
    assert "querySelectorAll('.ref-text')" in html


def test_status_endpoint_returns_progress_keys(client):
    """GET /api/status returns JSON with pending, translated, needs_human keys."""
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.get_json()
    assert "pending"     in data
    assert "translated"  in data
    assert "needs_human" in data


# ---------------------------------------------------------------------------
# Helper: mock connection builder for DB-level unit tests
# ---------------------------------------------------------------------------


def _make_db_conn(fetchone_side_effect=None, rowcount=1):
    """Return a mock psycopg2 connection with a cursor stub."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.rowcount = rowcount
    if fetchone_side_effect is not None:
        cursor.fetchone.side_effect = fetchone_side_effect
    else:
        cursor.fetchone.return_value = (42,)  # default human source_id = 42
    conn.cursor.return_value = cursor
    return conn, cursor


# ---------------------------------------------------------------------------
# review_segment DB unit tests
# ---------------------------------------------------------------------------


def test_review_segment_save_on_pending_writes_text_and_review():
    """save action creates segment_review and segment_text, leaves translation_status alone."""
    from server.db import review_segment

    conn, cursor = _make_db_conn()
    # fetchone calls: existence check → found; upsert RETURNING → version 1
    cursor.fetchone.side_effect = [(1,), (1,)]

    with patch("server.db.source_id", return_value=42):
        result, new_version = review_segment(
            conn, segment_id=1, action="save",
            expected_version=0,
            reviewer_email="ed@example.com",
            text="Preložený text.",
        )

    assert result == "ok"
    assert new_version == 1

    sql_calls = [c[0][0].strip() for c in cursor.execute.call_args_list]
    assert not any("translation_status" in c for c in sql_calls), \
        "save must not touch translation_status"
    assert any("INSERT INTO segment_review" in c for c in sql_calls)
    assert any("INSERT INTO segment_text" in c for c in sql_calls)
    conn.commit.assert_not_called()


def test_review_segment_accept_creates_review_row_no_text():
    """accept action creates segment_review without writing segment_text."""
    from server.db import review_segment

    conn, cursor = _make_db_conn()
    cursor.fetchone.side_effect = [(1,), (1,)]  # existence + RETURNING

    result, new_version = review_segment(
        conn, segment_id=2, action="accept",
        expected_version=0,
        reviewer_email="ed@example.com",
    )

    assert result == "ok"
    assert new_version == 1

    sql_calls = [c[0][0].strip() for c in cursor.execute.call_args_list]
    assert any("INSERT INTO segment_review" in c for c in sql_calls)
    assert not any("INSERT INTO segment_text" in c for c in sql_calls)


def test_review_segment_note_roundtrips():
    """note action upserts segment_review with human_note, does not write segment_text."""
    from server.db import review_segment

    conn, cursor = _make_db_conn()
    cursor.fetchone.side_effect = [(1,), (2,)]  # existence + RETURNING (version bump)

    result, new_version = review_segment(
        conn, segment_id=3, action="note",
        expected_version=1,
        reviewer_email="ed@example.com",
        note="Terminological note here.",
    )

    assert result == "ok"
    assert new_version == 2

    sql_calls = [c[0][0] for c in cursor.execute.call_args_list]
    assert any("human_note" in c for c in sql_calls)
    assert not any("segment_text" in c for c in sql_calls)


def test_review_segment_reset_deletes_both():
    """reset action deletes segment_review and segment_text rows."""
    from server.db import review_segment

    conn, cursor = _make_db_conn(rowcount=1)
    cursor.fetchone.side_effect = [(1,)]  # segment existence check

    with patch("server.db.source_id", return_value=42):
        result, new_version = review_segment(
            conn, segment_id=4, action="reset",
            expected_version=1,
            reviewer_email="ed@example.com",
        )

    assert result == "ok"
    assert new_version == 0

    sql_calls = [c[0][0].strip() for c in cursor.execute.call_args_list]
    assert any("DELETE FROM segment_review" in c for c in sql_calls)
    assert any("DELETE FROM segment_text" in c for c in sql_calls)


def test_review_segment_stale_version_returns_conflict():
    """Stale expected_version on save returns conflict without writing anything."""
    from server.db import review_segment

    conn, cursor = _make_db_conn()
    # existence check → found; upsert RETURNING → None (version guard rejected)
    cursor.fetchone.side_effect = [(1,), None]

    result, new_version = review_segment(
        conn, segment_id=5, action="save",
        expected_version=0,    # stale — real version is 1
        reviewer_email="ed@example.com",
        text="Some text",
    )

    assert result == "conflict"
    assert new_version is None


def test_review_segment_unknown_segment_returns_notfound():
    """Unknown segment_id returns notfound immediately."""
    from server.db import review_segment

    conn, cursor = _make_db_conn()
    cursor.fetchone.return_value = None  # segment does not exist

    result, new_version = review_segment(
        conn, segment_id=9999, action="save",
        expected_version=0,
        reviewer_email="ed@example.com",
        text="text",
    )

    assert result == "notfound"
    assert new_version is None


def test_review_segment_reset_ok_when_no_review_row():
    """reset with expected_version=0 and no review row returns ok (already clean state)."""
    from server.db import review_segment

    conn, cursor = _make_db_conn()
    # DELETE matches 0 rows; SELECT finds nothing → not a conflict, just already reset
    cursor.rowcount = 0
    cursor.fetchone.side_effect = [(1,), None]  # segment exists; no review row

    with patch("server.db.source_id", return_value=42):
        result, new_version = review_segment(
            conn, segment_id=7, action="reset",
            expected_version=0,
            reviewer_email="ed@example.com",
        )

    assert result == "ok"
    assert new_version == 0


def test_review_segment_reset_conflict_when_row_exists_with_different_version():
    """reset with wrong expected_version returns conflict when the row still exists."""
    from server.db import review_segment

    conn, cursor = _make_db_conn()
    # DELETE matched 0 rows (wrong version), then SELECT finds the row still there
    cursor.rowcount = 0
    cursor.fetchone.side_effect = [(1,), (1,)]  # segment exists; review row still exists

    result, _ = review_segment(
        conn, segment_id=6, action="reset",
        expected_version=0,   # wrong; actual is 2
        reviewer_email="ed@example.com",
    )

    assert result == "conflict"


# ---------------------------------------------------------------------------
# Editor client fixture — sets is_editor=True in the Flask session
# ---------------------------------------------------------------------------


@pytest.fixture()
def editor_client():
    """Flask test client with an active editor session."""
    with (
        patch("server.app.get_conn", make_fake_get_conn()),
        patch("server.app.get_structural_formulas", return_value={}),
        patch("server.app.get_all_questions",           return_value=FAKE_QUESTIONS),
        patch("server.app.get_question_articles",       return_value=FAKE_ARTICLES),
        patch("server.app.get_article_segments",        return_value=FAKE_SEGMENTS),
        patch("server.app.get_prev_next_article",       return_value=FAKE_NAV),
        patch("server.app.get_translation_progress",    return_value=FAKE_PROGRESS),
        patch("server.app.get_questions_by_status",     return_value=FAKE_QUESTIONS_BY_STATUS),
        patch("server.app.get_segment_constraints",     return_value={}),
        patch("server.app.get_question_title_segment",  return_value=None),
        patch("server.app.get_question_preamble_segment", return_value=None),
    ):
        import server.app as _app_module
        from server.app import app
        _app_module._formulas = {}

        app.config["TESTING"] = True
        app.secret_key = "test-secret"
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["email"] = "editor@example.com"
                sess["is_editor"] = True
            yield c


# ---------------------------------------------------------------------------
# /api/segment/<id>/review route tests
# ---------------------------------------------------------------------------


def test_review_route_save_returns_ok_with_version(editor_client):
    """POST /api/segment/<id>/review with action=save returns 200 with human_version."""
    with patch("server.app.review_segment", return_value=("ok", 1)) as mock_rv:
        resp = editor_client.post(
            "/api/segment/42/review",
            json={"action": "save", "text": "Preložený text.", "expected_version": 0},
            content_type="application/json",
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["human_version"] == 1
    mock_rv.assert_called_once()


def test_review_route_accept_returns_ok(editor_client):
    """POST /api/segment/<id>/review with action=accept returns 200."""
    with patch("server.app.review_segment", return_value=("ok", 1)):
        resp = editor_client.post(
            "/api/segment/42/review",
            json={"action": "accept", "expected_version": 0},
            content_type="application/json",
        )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_review_route_reset_returns_version_zero(editor_client):
    """POST /api/segment/<id>/review with action=reset returns human_version=0."""
    with patch("server.app.review_segment", return_value=("ok", 0)):
        resp = editor_client.post(
            "/api/segment/42/review",
            json={"action": "reset", "expected_version": 1},
            content_type="application/json",
        )
    assert resp.status_code == 200
    assert resp.get_json()["human_version"] == 0


def test_review_route_conflict_returns_409(editor_client):
    """POST /api/segment/<id>/review returns 409 on stale expected_version."""
    with patch("server.app.review_segment", return_value=("conflict", None)):
        resp = editor_client.post(
            "/api/segment/42/review",
            json={"action": "save", "text": "text", "expected_version": 0},
            content_type="application/json",
        )
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "conflict"


def test_review_route_unknown_segment_returns_404(editor_client):
    """POST /api/segment/<id>/review returns 404 for unknown segment_id."""
    with patch("server.app.review_segment", return_value=("notfound", None)):
        resp = editor_client.post(
            "/api/segment/9999/review",
            json={"action": "accept", "expected_version": 0},
            content_type="application/json",
        )
    assert resp.status_code == 404


def test_review_route_empty_text_returns_400(editor_client):
    """POST /api/segment/<id>/review with action=save and empty text returns 400."""
    resp = editor_client.post(
        "/api/segment/42/review",
        json={"action": "save", "text": "   ", "expected_version": 0},
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_review_route_note_empty_clears_note(editor_client):
    """POST /api/segment/<id>/review with action=note and empty note clears the note (not 400)."""
    with patch("server.app.review_segment", return_value=("ok", 2)) as mock_rev:
        resp = editor_client.post(
            "/api/segment/42/review",
            json={"action": "note", "expected_version": 1},
            content_type="application/json",
        )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    # note kwarg must be None (not empty string) so the DB writes NULL
    _, kwargs = mock_rev.call_args
    assert kwargs["note"] is None


def test_review_route_invalid_action_returns_400(editor_client):
    """POST /api/segment/<id>/review with unknown action returns 400."""
    resp = editor_client.post(
        "/api/segment/42/review",
        json={"action": "bogus", "expected_version": 0},
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_review_route_returns_403_for_non_editor(client):
    """POST /api/segment/<id>/review returns 403 when no editor session is set."""
    resp = client.post(
        "/api/segment/42/review",
        json={"action": "accept", "expected_version": 0},
        content_type="application/json",
    )
    assert resp.status_code == 403
    assert resp.get_json() == {"ok": False, "error": "forbidden"}


# ---------------------------------------------------------------------------
# Comment thread routes
# ---------------------------------------------------------------------------

FAKE_THREAD_OPEN = MagicMock(
    comments=[
        MagicMock(
            comment_id=1, segment_id=42, author="editor@example.com", body="hi",
            created_at=datetime(2026, 7, 1, 10, 0),
            resolved=False, resolved_by=None, resolved_at=None,
        ),
    ],
    resolved=False,
    open_count=1,
)


def test_list_comments_route_marks_thread_read(editor_client):
    """GET .../comments returns the thread and marks it read for the caller."""
    with (
        patch("server.app.list_comments", return_value=FAKE_THREAD_OPEN) as mock_list,
        patch("server.app.mark_thread_read") as mock_mark,
    ):
        resp = editor_client.get("/api/segment/42/comments")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["open_count"] == 1
    assert data["comments"][0]["author"] == "editor@example.com"
    mock_list.assert_called_once_with(mock_list.call_args[0][0], 42)
    mock_mark.assert_called_once_with(mock_mark.call_args[0][0], 42, "editor@example.com")


def test_list_comments_route_403_for_non_editor(client):
    resp = client.get("/api/segment/42/comments")
    assert resp.status_code == 403


def test_add_comment_route_returns_new_comment(editor_client):
    fake_comment = MagicMock(
        comment_id=5, segment_id=42, author="editor@example.com", body="reply",
        created_at=datetime(2026, 7, 1, 10, 0),
        resolved=False, resolved_by=None, resolved_at=None,
    )
    with patch("server.app.add_comment", return_value=fake_comment) as mock_add:
        resp = editor_client.post(
            "/api/segment/42/comments", json={"body": "reply"}, content_type="application/json",
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["comment"]["comment_id"] == 5
    mock_add.assert_called_once_with(
        mock_add.call_args[0][0], 42, "editor@example.com", "reply"
    )


def test_add_comment_route_rejects_empty_body(editor_client):
    resp = editor_client.post(
        "/api/segment/42/comments", json={"body": "  "}, content_type="application/json",
    )
    assert resp.status_code == 400


def test_add_comment_route_403_for_non_editor(client):
    resp = client.post(
        "/api/segment/42/comments", json={"body": "x"}, content_type="application/json",
    )
    assert resp.status_code == 403


def test_resolve_thread_route_returns_ok(editor_client):
    with patch("server.app.resolve_thread") as mock_resolve:
        resp = editor_client.post("/api/segment/42/comments/resolve")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    mock_resolve.assert_called_once_with(
        mock_resolve.call_args[0][0], 42, "editor@example.com"
    )


def test_reopen_thread_route_returns_ok(editor_client):
    with patch("server.app.reopen_thread") as mock_reopen:
        resp = editor_client.post("/api/segment/42/comments/reopen")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    mock_reopen.assert_called_once_with(mock_reopen.call_args[0][0], 42)


def test_delete_comment_route_ok(editor_client):
    with patch("server.app.delete_comment", return_value="ok") as mock_delete:
        resp = editor_client.delete("/api/comment/1")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    mock_delete.assert_called_once_with(mock_delete.call_args[0][0], 1, "editor@example.com")


def test_delete_comment_route_notfound(editor_client):
    with patch("server.app.delete_comment", return_value="notfound"):
        resp = editor_client.delete("/api/comment/999")
    assert resp.status_code == 404


def test_delete_comment_route_forbidden(editor_client):
    with patch("server.app.delete_comment", return_value="forbidden"):
        resp = editor_client.delete("/api/comment/1")
    assert resp.status_code == 403


def test_delete_comment_route_403_for_non_editor(client):
    resp = client.delete("/api/comment/1")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# @requires_editor decorator (new route)
# ---------------------------------------------------------------------------


def test_review_route_returns_403_for_non_editor_session(client):
    """POST /api/segment/<id>/review returns 403 when is_editor=False in session."""
    with client.session_transaction() as sess:
        sess["email"] = "visitor@example.com"
        sess["is_editor"] = False
    resp = client.post(
        "/api/segment/42/review",
        json={"action": "accept", "expected_version": 0},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /login and /logout routes
# ---------------------------------------------------------------------------


def test_login_redirects(client):
    """GET /login redirects to Google (authorize_redirect called)."""
    from flask import Response
    with patch("server.app.oauth") as mock_oauth:
        mock_oauth.google.authorize_redirect.return_value = Response(
            status=302, headers={"Location": "https://accounts.google.com/o/oauth2/auth"}
        )
        resp = client.get("/login")
    assert resp.status_code == 302
    mock_oauth.google.authorize_redirect.assert_called_once()


def test_logout_clears_session(editor_client):
    """GET /logout clears the session and redirects to /."""
    resp = editor_client.get("/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] in ("/", "http://localhost/")
    # After logout, write endpoints return 403.
    resp2 = editor_client.post(
        "/api/segment/1/review",
        json={"action": "accept", "expected_version": 0},
    )
    assert resp2.status_code == 403


def test_auth_callback_sets_editor_session():
    """auth_callback stores is_editor=True in the session for a known editor email."""
    from server.app import app

    app.config["TESTING"] = True
    app.secret_key = "test-secret"

    fake_token = {
        "userinfo": {"email": "editor@example.com", "email_verified": True},
    }

    stub_conn = MagicMock()
    stub_cursor = MagicMock()
    stub_cursor.__enter__ = MagicMock(return_value=stub_cursor)
    stub_cursor.__exit__ = MagicMock(return_value=False)
    stub_cursor.fetchone.return_value = (1,)  # editor row found
    stub_conn.cursor.return_value = stub_cursor

    @contextmanager
    def fake_get_conn_for_callback():
        yield stub_conn

    with (
        patch("server.app.oauth") as mock_oauth,
        patch("server.app.get_conn", fake_get_conn_for_callback),
    ):
        mock_oauth.google.authorize_access_token.return_value = fake_token
        with app.test_client() as c:
            resp = c.get("/auth/callback")
            assert resp.status_code == 302
            with c.session_transaction() as sess:
                assert sess["email"] == "editor@example.com"
                assert sess["is_editor"] is True


def test_auth_callback_rejects_unverified_email():
    """auth_callback returns 403 when Google email is not verified."""
    from server.app import app

    app.config["TESTING"] = True
    app.secret_key = "test-secret"

    fake_token = {
        "userinfo": {"email": "unverified@example.com", "email_verified": False},
    }

    stub_conn = MagicMock()

    @contextmanager
    def fake_get_conn_noop():
        yield stub_conn

    with (
        patch("server.app.oauth") as mock_oauth,
        patch("server.app.get_conn", fake_get_conn_noop),
        patch("server.app.get_structural_formulas", return_value={"dummy": "formula"}),
    ):
        mock_oauth.google.authorize_access_token.return_value = fake_token
        with app.test_client() as c:
            resp = c.get("/auth/callback")
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "email not verified"


def test_auth_callback_sets_non_editor_session():
    """auth_callback stores is_editor=False for an email not in the editor table."""
    from server.app import app

    app.config["TESTING"] = True
    app.secret_key = "test-secret"

    fake_token = {
        "userinfo": {"email": "visitor@example.com", "email_verified": True},
    }

    stub_conn = MagicMock()
    stub_cursor = MagicMock()
    stub_cursor.__enter__ = MagicMock(return_value=stub_cursor)
    stub_cursor.__exit__ = MagicMock(return_value=False)
    stub_cursor.fetchone.return_value = None  # not an editor
    stub_conn.cursor.return_value = stub_cursor

    @contextmanager
    def fake_get_conn_for_callback():
        yield stub_conn

    with (
        patch("server.app.oauth") as mock_oauth,
        patch("server.app.get_conn", fake_get_conn_for_callback),
    ):
        mock_oauth.google.authorize_access_token.return_value = fake_token
        with app.test_client() as c:
            resp = c.get("/auth/callback")
            assert resp.status_code == 302
            with c.session_transaction() as sess:
                assert sess["email"] == "visitor@example.com"
                assert sess["is_editor"] is False


# ---------------------------------------------------------------------------
# Context processor — template variable injection
# ---------------------------------------------------------------------------


def test_login_link_shown_for_anonymous(client):
    """Anonymous visitors see a Login link in the header."""
    resp = client.get("/")
    html = resp.data.decode()
    assert 'href="/login"' in html
    assert "Login" in html


def test_logout_link_shown_for_editor(editor_client):
    """Authenticated editors see their email and a Logout link in the header."""
    resp = editor_client.get("/")
    html = resp.data.decode()
    assert "editor@example.com" in html
    assert 'href="/logout"' in html


# ---------------------------------------------------------------------------
# Template: edit/approve buttons hidden for anonymous visitors
# ---------------------------------------------------------------------------


def test_review_button_hidden_for_anonymous(client):
    """Anonymous visitors do not see btn-review buttons (HTML element) on the article page."""
    resp = client.get("/~ST.I.Q3.A1")
    html = resp.data.decode()
    assert 'class="btn-review"' not in html


def test_review_button_visible_for_editor(editor_client):
    """Editors see the btn-review button for all segments."""
    needs_human_segments = [
        {
            "segment_id": 10,
            "locator_path": "I.q3.a1.arg1",
            "element_type": "arg",
            "reply_to": None,
            "translation_status": "needs_human",
            "reviewer_notes": None,
            "latin": "Videtur quod.",
            "czech": "Zdá se.",
            "english": "It seems.",
            "slovak_model": "Zdá sa.",
            "slovak_human": None,
            "human_note": None,
            "human_reviewed_by": None,
            "human_version": 0,
        }
    ]
    with patch("server.app.get_article_segments", return_value=needs_human_segments):
        resp = editor_client.get("/~ST.I.Q3.A1")
    html = resp.data.decode()
    assert 'class="btn-review' in html


# ---------------------------------------------------------------------------
# Index page: progress badges link to status views
# ---------------------------------------------------------------------------


def test_index_translated_badge_is_a_link(editor_client):
    """The 'translated' badge on the index page links to /status/translated (editor only)."""
    resp = editor_client.get("/")
    html = resp.data.decode()
    assert 'href="/status/translated"' in html


def test_index_needs_human_badge_is_a_link(editor_client):
    """The 'needs review' badge on the index page links to /status/needs_human (editor only)."""
    resp = editor_client.get("/")
    html = resp.data.decode()
    assert 'href="/status/needs_human"' in html


def test_index_pending_badge_is_a_link(editor_client):
    """The 'pending' badge on the index page links to /status/pending (editor only)."""
    resp = editor_client.get("/")
    html = resp.data.decode()
    assert 'href="/status/pending"' in html


def test_index_progress_hidden_for_non_editor(client):
    """Non-editors do not see the translation progress section."""
    resp = client.get("/")
    html = resp.data.decode()
    assert 'Translation progress' not in html
    assert 'href="/status/translated"' not in html


# ---------------------------------------------------------------------------
# /status/<status> route
# ---------------------------------------------------------------------------


def test_status_list_translated_returns_200(editor_client):
    """GET /status/translated returns 200 for editors."""
    assert editor_client.get("/status/translated").status_code == 200


def test_status_list_needs_human_returns_200(editor_client):
    """GET /status/needs_human returns 200 for editors."""
    assert editor_client.get("/status/needs_human").status_code == 200


def test_status_list_pending_returns_200(editor_client):
    """GET /status/pending returns 200 for editors."""
    assert editor_client.get("/status/pending").status_code == 200


def test_status_list_returns_403_for_non_editor(client):
    """GET /status/* returns 403 for non-editors."""
    assert client.get("/status/translated").status_code == 403


def test_status_list_invalid_status_returns_404(editor_client):
    """GET /status/bogus returns 404 for an unrecognised status."""
    assert editor_client.get("/status/bogus").status_code == 404


def test_status_list_groups_questions_by_pars(editor_client):
    """Status list renders a pars-section heading for each pars in the result."""
    html = editor_client.get("/status/needs_human").data.decode()
    assert "Pars I" in html
    assert "Pars II-I" in html


def test_status_list_shows_question_links(editor_client):
    """Status list renders href links to each question's URL locator."""
    html = editor_client.get("/status/translated").data.decode()
    assert "/~ST.I.Q3" in html


def test_status_list_shows_segment_counts(editor_client):
    """Status list annotates each question with its segment count."""
    html = editor_client.get("/status/translated").data.decode()
    assert "4 segment" in html
    assert "2 segment" in html


def test_status_list_page_title_reflects_status(editor_client):
    """Status list <title> contains the human-readable status label."""
    html = editor_client.get("/status/needs_human").data.decode()
    assert "Needs review" in html


# ---------------------------------------------------------------------------
# Question view: Needs Review column
# ---------------------------------------------------------------------------


def test_question_view_has_needs_review_header_for_editor(editor_client):
    """Editors see 'Needs Review' column header in the article summary table."""
    resp = editor_client.get("/~ST.I.Q3")
    html = resp.data.decode()
    assert "Needs Review" in html


def test_question_view_needs_review_header_hidden_for_non_editor(client):
    """Non-editors do not see 'Needs Review' or 'Translated' columns."""
    resp = client.get("/~ST.I.Q3")
    html = resp.data.decode()
    assert "Needs Review" not in html
    assert "Translated" not in html


def test_question_view_zero_needs_human_shows_plain_zero(editor_client):
    """Articles with needs_human_count=0 display a plain '0', not a badge."""
    resp = editor_client.get("/~ST.I.Q3")
    html = resp.data.decode()
    # FAKE_ARTICLES has needs_human_count=0; should not render badge-warn for it
    assert "badge-warn" not in html


def test_question_view_nonzero_needs_human_renders_badge(editor_client):
    """Articles with needs_human_count>0 display a badge-warn with the count."""
    with patch("server.app.get_question_articles", return_value=FAKE_ARTICLES_WITH_NEEDS_HUMAN):
        resp = editor_client.get("/~ST.I.Q3")
    html = resp.data.decode()
    assert 'class="badge badge-warn"' in html
    assert ">3<" in html


def test_question_view_highlights_needs_human_article_row(editor_client):
    """Article rows with needs_human_count>0 get the row-needs-human CSS class for editors."""
    with patch("server.app.get_question_articles", return_value=FAKE_ARTICLES_WITH_NEEDS_HUMAN):
        resp = editor_client.get("/~ST.I.Q3")
    html = resp.data.decode()
    assert "row-needs-human" in html


def test_question_view_needs_human_row_hidden_for_non_editor(client):
    """Non-editors do not see row-needs-human highlight on article rows."""
    with patch("server.app.get_question_articles", return_value=FAKE_ARTICLES_WITH_NEEDS_HUMAN):
        resp = client.get("/~ST.I.Q3")
    html = resp.data.decode()
    assert "row-needs-human" not in html


def test_question_view_clean_article_has_no_highlight(editor_client):
    """Article rows with needs_human_count=0 do not get the row-needs-human class."""
    resp = editor_client.get("/~ST.I.Q3")
    html = resp.data.decode()
    assert "row-needs-human" not in html


# ---------------------------------------------------------------------------
# get_questions_by_status DB function
# ---------------------------------------------------------------------------


def test_get_questions_by_status_queries_correct_status():
    """get_questions_by_status passes the status value as a bind parameter."""
    from server.db import get_questions_by_status

    conn, cursor = _make_db_conn()
    cursor.fetchall.return_value = []
    get_questions_by_status(conn, "needs_human")

    executed_sql, params = cursor.execute.call_args[0]
    assert "translation_status" in executed_sql
    assert params == ("needs_human",)


def test_get_questions_by_status_returns_list_of_dicts():
    """get_questions_by_status converts fetchall rows to plain dicts."""
    from server.db import get_questions_by_status

    conn, cursor = _make_db_conn()
    cursor.fetchall.return_value = [
        {"question_path": "I.q3", "_sort_key": "I.q3", "segment_count": 5, "reviewed_count": 0},
        {"question_path": "I.q4", "_sort_key": "I.q4", "segment_count": 1, "reviewed_count": 0},
    ]
    result = get_questions_by_status(conn, "translated")

    assert len(result) == 2
    assert result[0]["question_path"] == "I.q3"
    assert result[0]["segment_count"] == 5


def test_get_questions_by_status_returns_empty_for_no_matches():
    """get_questions_by_status returns [] when no segments have that status."""
    from server.db import get_questions_by_status

    conn, cursor = _make_db_conn()
    cursor.fetchall.return_value = []
    result = get_questions_by_status(conn, "pending")

    assert result == []


# ---------------------------------------------------------------------------
# get_question_articles: needs_human_count column
# ---------------------------------------------------------------------------


def test_get_question_articles_sql_includes_needs_human_count():
    """get_question_articles issues SQL that counts needs_human segments."""
    from server.db import get_question_articles

    conn, cursor = _make_db_conn()
    cursor.fetchall.return_value = []
    get_question_articles(conn, "I.q3")

    executed_sql, _ = cursor.execute.call_args[0]
    assert "needs_human" in executed_sql.lower()


# ---------------------------------------------------------------------------
# /api/segment/<id>/approve and /unapprove route tests
# ---------------------------------------------------------------------------


def test_approve_route_returns_403_for_non_editor(client):
    """POST /api/segment/<id>/approve returns 403 for non-editor."""
    resp = client.post("/api/segment/1/approve")
    assert resp.status_code == 403
    assert resp.get_json() == {"ok": False, "error": "forbidden"}


def test_approve_route_flips_needs_human_to_translated(editor_client):
    """POST /api/segment/<id>/approve returns 200 when segment is needs_human."""
    with patch("server.app.approve_segment", return_value="ok"):
        resp = editor_client.post("/api/segment/1/approve")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_approve_route_wrong_status(editor_client):
    """POST /api/segment/<id>/approve returns 409 when segment is not needs_human."""
    with patch("server.app.approve_segment", return_value="wrong_status"):
        resp = editor_client.post("/api/segment/1/approve")
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "wrong_status"


def test_approve_route_not_found(editor_client):
    """POST /api/segment/<id>/approve returns 404 when segment does not exist."""
    with patch("server.app.approve_segment", return_value="notfound"):
        resp = editor_client.post("/api/segment/9999/approve")
    assert resp.status_code == 404


def test_unapprove_route_returns_403_for_non_editor(client):
    """POST /api/segment/<id>/unapprove returns 403 for non-editor."""
    resp = client.post("/api/segment/1/unapprove")
    assert resp.status_code == 403


def test_unapprove_route_flips_translated_to_needs_human(editor_client):
    """POST /api/segment/<id>/unapprove returns 200 when segment is translated and not polished."""
    with patch("server.app.unapprove_segment", return_value="ok"):
        resp = editor_client.post("/api/segment/1/unapprove")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_unapprove_route_blocked_when_already_polished(editor_client):
    """POST /api/segment/<id>/unapprove returns 409 when batch polish has already run."""
    with patch("server.app.unapprove_segment", return_value="already_polished"):
        resp = editor_client.post("/api/segment/1/unapprove")
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "already_polished"


def test_unapprove_route_wrong_status(editor_client):
    """POST /api/segment/<id>/unapprove returns 409 when segment is not translated."""
    with patch("server.app.unapprove_segment", return_value="wrong_status"):
        resp = editor_client.post("/api/segment/1/unapprove")
    assert resp.status_code == 409


# (polish route removed — batch pipeline handles polish via uv run python -m pipeline)


# ---------------------------------------------------------------------------
# Editor glossary proposals (Stage 3 — /api/sense/<id>/alternatives,
# /api/sense/<id>/propose, /api/term-proposal)
# ---------------------------------------------------------------------------


FAKE_TERM_SENSES = {
    "term_id": 7,
    "latin_lemma": "ratio",
    "senses": [
        {"sense_id": 100, "context_label": None, "status": "approved", "slovak": "rozum"},
        {"sense_id": 101, "context_label": "faculty", "status": "approved", "slovak": "dôvod"},
    ],
}


def test_alternatives_route_returns_403_for_non_editor(client):
    resp = client.get("/api/sense/100/alternatives")
    assert resp.status_code == 403


def test_alternatives_route_returns_senses(editor_client):
    with patch("server.app.get_term_senses", return_value=FAKE_TERM_SENSES):
        resp = editor_client.get("/api/sense/100/alternatives")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["latin_lemma"] == "ratio"
    assert data["senses"] == FAKE_TERM_SENSES["senses"]


def test_alternatives_route_404_for_unknown_sense(editor_client):
    with patch("server.app.get_term_senses", return_value=None):
        resp = editor_client.get("/api/sense/999/alternatives")
    assert resp.status_code == 404


def test_propose_route_returns_403_for_non_editor(client):
    resp = client.post("/api/sense/100/propose", json={"kind": "rendering", "proposed_sk": "x"})
    assert resp.status_code == 403


def test_propose_route_invalid_kind_returns_400(editor_client):
    resp = editor_client.post("/api/sense/100/propose", json={"kind": "bogus"})
    assert resp.status_code == 400


def test_propose_route_sense_here_missing_origin_returns_400(editor_client):
    """origin_segment_id-required check is pure request-shape validation done
    in app.py before any db.py call — propose_sense_change is never reached."""
    with patch("server.app.propose_sense_change") as mock_propose:
        resp = editor_client.post(
            "/api/sense/100/propose",
            json={"kind": "sense_here", "proposed_sense_id": 101},
        )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "origin_segment_id required"
    mock_propose.assert_not_called()


def test_propose_route_invalid_proposed_sense_id_returns_400(editor_client):
    with patch("server.app.propose_sense_change") as mock_propose:
        resp = editor_client.post(
            "/api/sense/100/propose",
            json={"kind": "sense_here", "origin_segment_id": 42, "proposed_sense_id": "abc"},
        )
    assert resp.status_code == 400
    mock_propose.assert_not_called()


def test_propose_route_invalid_origin_segment_id_returns_400(editor_client):
    with patch("server.app.propose_sense_change") as mock_propose:
        resp = editor_client.post(
            "/api/sense/100/propose",
            json={"kind": "rendering", "proposed_sk": "x", "origin_segment_id": "abc"},
        )
    assert resp.status_code == 400
    mock_propose.assert_not_called()


def test_propose_route_calls_db_with_parsed_fields(editor_client):
    with patch("server.app.propose_sense_change", return_value=("ok", 55)) as mock_propose:
        resp = editor_client.post(
            "/api/sense/100/propose",
            json={
                "kind": "sense_here",
                "origin_segment_id": 42,
                "proposed_sense_id": 101,
                "note": "wrong sense",
            },
        )
    assert resp.status_code == 200
    assert resp.get_json()["proposal_id"] == 55
    args, kwargs = mock_propose.call_args
    assert args[1:] == (100, "sense_here")
    assert kwargs["origin_segment_id"] == 42
    assert kwargs["proposed_sense_id"] == 101
    assert kwargs["note"] == "wrong sense"
    assert kwargs["proposed_by"] == "editor@example.com"


@pytest.mark.parametrize(
    "status,expected_code,expected_error",
    [
        ("not_found", 404, "not found"),
        ("proposed_sk_required", 400, "proposed_sk required"),
        ("missing_target", 400, "proposed_sense_id or proposed_sk required"),
        # no_change / wrong_term / not_locked_here all share app.py's generic
        # fallback branch (return jsonify({"error": status}), 400) — one
        # representative case is enough here; the branches that *produce*
        # each status are covered at the db.py level.
        ("no_change", 400, "no_change"),
    ],
)
def test_propose_route_maps_db_status_to_response(
    editor_client, status, expected_code, expected_error
):
    with patch("server.app.propose_sense_change", return_value=(status, None)):
        resp = editor_client.post(
            "/api/sense/100/propose", json={"kind": "rendering", "proposed_sk": "x"}
        )
    assert resp.status_code == expected_code
    assert resp.get_json()["error"] == expected_error


def test_term_proposal_route_returns_403_for_non_editor(client):
    resp = client.post(
        "/api/term-proposal", json={"latin_lemma": "novum", "proposed_sk": "nové"}
    )
    assert resp.status_code == 403


def test_term_proposal_route_valid_returns_200(editor_client):
    with patch("server.app.propose_add_term", return_value=("ok", 77)) as mock_propose:
        resp = editor_client.post(
            "/api/term-proposal",
            json={"latin_lemma": "novum", "proposed_sk": "nové", "note": "missing"},
        )
    assert resp.status_code == 200
    assert resp.get_json()["proposal_id"] == 77
    _, kwargs = mock_propose.call_args
    assert kwargs["latin_lemma"] == "novum"
    assert kwargs["proposed_sk"] == "nové"
    assert kwargs["proposed_by"] == "editor@example.com"


def test_term_proposal_route_existing_lemma_returns_400(editor_client):
    with patch("server.app.propose_add_term", return_value=("term_exists", None)):
        resp = editor_client.post(
            "/api/term-proposal",
            json={"latin_lemma": "ratio", "proposed_sk": "rozum"},
        )
    assert resp.status_code == 400
    assert "term_exists" in resp.get_json()["error"]


def test_term_proposal_route_missing_fields_returns_400(editor_client):
    resp = editor_client.post("/api/term-proposal", json={"latin_lemma": "novum"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Stage 4 — admin gate + proposal queue
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin_client():
    """Flask test client with an active admin (editor + admin=True) session."""
    with (
        patch("server.app.get_conn", make_fake_get_conn()),
        patch("server.app.get_structural_formulas", return_value={}),
    ):
        import server.app as _app_module
        from server.app import app
        _app_module._formulas = {}

        app.config["TESTING"] = True
        app.secret_key = "test-secret"
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["email"] = "admin@example.com"
                sess["is_editor"] = True
                sess["is_admin"] = True
            yield c


def test_glossary_proposals_page_returns_403_for_anonymous(client):
    resp = client.get("/glossary/proposals")
    assert resp.status_code == 403


def test_glossary_proposals_page_returns_403_for_editor_non_admin(editor_client):
    resp = editor_client.get("/glossary/proposals")
    assert resp.status_code == 403


def test_glossary_proposals_page_returns_200_for_admin(admin_client):
    with (
        patch("server.app.get_pending_proposals_view", return_value=[]),
        patch("server.app.get_cost_per_segment", return_value=0.001),
    ):
        resp = admin_client.get("/glossary/proposals")
    assert resp.status_code == 200


def test_glossary_proposals_page_groups_by_kind(admin_client):
    base = {
        "latin_lemma": "gratia",
        "context_label": None,
        "current_sk": "milosť",
        "proposed_sk": "milosť Božia",
        "drift": False,
        "live_current_sk": "milosť",
        "note": None,
        "proposed_by": "editor@example.com",
        "created_at": "2026-07-01",
        "proposed_sense_id": None,
        "proposed_context_label": None,
        "proposed_slovak": None,
        "origin_locator": None,
        "blast_radius": {"translated": 1, "needs_human": 0, "reviewed": 1, "marginal": 1},
    }
    proposals = [
        {**base, "proposal_id": 1, "kind": "rendering"},
        {**base, "proposal_id": 2, "kind": "retire_sense"},
        {**base, "proposal_id": 3, "kind": "sense_here", "blast_radius": None},
        {**base, "proposal_id": 4, "kind": "remove_here", "blast_radius": None},
        {**base, "proposal_id": 5, "kind": "add_term", "blast_radius": None},
    ]
    with (
        patch("server.app.get_pending_proposals_view", return_value=proposals),
        patch("server.app.get_cost_per_segment", return_value=0.001),
    ):
        resp = admin_client.get("/glossary/proposals")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Weak but DB-free smoke check: each proposal row id is rendered somewhere.
    for p in proposals:
        assert f'prow-{p["proposal_id"]}' in body


def test_approve_route_returns_403_for_non_admin(editor_client):
    resp = editor_client.post("/api/proposal/1/approve")
    assert resp.status_code == 403


def test_reject_route_returns_403_for_non_admin(editor_client):
    resp = editor_client.post("/api/proposal/1/reject")
    assert resp.status_code == 403


def test_approve_route_ok_returns_result(admin_client):
    with patch(
        "server.app.approve_proposal", return_value=("ok", {"acknowledged": True})
    ) as mock_approve:
        resp = admin_client.post("/api/proposal/1/approve", json={"note": "looks good"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["acknowledged"] is True
    args, _ = mock_approve.call_args
    assert args[1] == 1
    assert args[2] == "admin@example.com"
    assert args[3] == "looks good"


def test_approve_route_not_found_returns_404(admin_client):
    with patch("server.app.approve_proposal", return_value=("not_found", None)):
        resp = admin_client.post("/api/proposal/999/approve")
    assert resp.status_code == 404


def test_approve_route_not_pending_returns_409(admin_client):
    with patch("server.app.approve_proposal", return_value=("not_pending", None)):
        resp = admin_client.post("/api/proposal/1/approve")
    assert resp.status_code == 409


def test_approve_route_race_returns_409(admin_client):
    from server.db import ProposalRaceError

    with patch("server.app.approve_proposal", side_effect=ProposalRaceError()):
        resp = admin_client.post("/api/proposal/1/approve")
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "already decided"


def test_approve_route_value_error_returns_409(admin_client):
    with patch("server.app.approve_proposal", side_effect=ValueError("term_exists")):
        resp = admin_client.post("/api/proposal/1/approve")
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "term_exists"


def test_reject_route_ok(admin_client):
    with patch("server.app.reject_proposal", return_value="ok") as mock_reject:
        resp = admin_client.post("/api/proposal/1/reject", json={"note": "duplicate"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    args, _ = mock_reject.call_args
    assert args[1] == 1
    assert args[2] == "admin@example.com"
    assert args[3] == "duplicate"


def test_reject_route_not_found_returns_404(admin_client):
    with patch("server.app.reject_proposal", return_value="not_found"):
        resp = admin_client.post("/api/proposal/999/reject")
    assert resp.status_code == 404


def test_reject_route_not_pending_returns_409(admin_client):
    with patch("server.app.reject_proposal", return_value="not_pending"):
        resp = admin_client.post("/api/proposal/1/reject")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# /timeline
# ---------------------------------------------------------------------------

_FEED_ENTRY_REVIEW = MagicMock(
    ts=datetime(2026, 7, 2, 9, 0),
    kind="review",
    author="editor@example.com",
    segment_id=42,
    locator="I.q3.a1.arg1",
    summary="reviewed",
    translated=None,
    needs_human=None,
    cost=None,
)

_FEED_ENTRY_RUN = MagicMock(
    ts=datetime(2026, 7, 2, 7, 0),
    kind="run",
    author=None,
    segment_id=None,
    locator=None,
    summary="pilot_sample",
    translated=10,
    needs_human=2,
    cost=1.5,
)


def test_timeline_page_returns_403_for_anonymous(client):
    resp = client.get("/timeline")
    assert resp.status_code == 403


def test_timeline_page_returns_403_for_editor_non_admin(editor_client):
    resp = editor_client.get("/timeline")
    assert resp.status_code == 403


def test_timeline_page_returns_200_for_admin(admin_client):
    with patch("server.app.get_activity_feed", return_value=[_FEED_ENTRY_REVIEW, _FEED_ENTRY_RUN]):
        resp = admin_client.get("/timeline")
    assert resp.status_code == 200
    assert b"pilot_sample" in resp.data


def test_timeline_page_passes_before_param(admin_client):
    with patch("server.app.get_activity_feed", return_value=[]) as mock_feed:
        admin_client.get("/timeline?before=2026-07-02T09:00:00")
    _, kwargs = mock_feed.call_args
    assert kwargs["before"] == "2026-07-02T09:00:00"


def test_timeline_page_sets_next_before_when_page_full(admin_client):
    entries = [_FEED_ENTRY_REVIEW] * 50
    with patch("server.app.get_activity_feed", return_value=entries):
        resp = admin_client.get("/timeline")
    assert b"Load older activity" in resp.data


# ---------------------------------------------------------------------------
# db.is_admin — fail-closed matrix
# ---------------------------------------------------------------------------


def test_is_admin_true_when_row_admin_true():
    from server.db import is_admin

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (True,)
    conn.cursor.return_value = cur
    assert is_admin(conn, "admin@example.com") is True


def test_is_admin_false_when_row_admin_false():
    from server.db import is_admin

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (False,)
    conn.cursor.return_value = cur
    assert is_admin(conn, "editor@example.com") is False


def test_is_admin_false_when_no_row():
    from server.db import is_admin

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = None
    conn.cursor.return_value = cur
    assert is_admin(conn, "stranger@example.com") is False


# ---------------------------------------------------------------------------
# db._sense_blast_radius — marginal restage count
# ---------------------------------------------------------------------------


def test_sense_blast_radius_marginal_is_not_already_stale_count():
    """marginal must be the count of segments that are NOT already stale
    (i.e. the segments this approval will newly restage) — not
    total-minus-that-count, which counts the wrong segments and produces a
    materially wrong cost estimate on the admin queue page."""
    from server.db import _sense_blast_radius

    by_status_rows = [
        {"translation_status": "translated", "n": 7},
        {"translation_status": "needs_human", "n": 2},
        {"translation_status": "pending", "n": 1},
    ]
    # total = 10 locked segments; 4 are not already stale (this is the
    # marginal restage count); reviewed = 3.
    fetchall_results = iter([by_status_rows])
    fetchone_results = iter([{"count": 3}, {"count": 4}])

    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.side_effect = lambda: next(fetchall_results)
    cur.fetchone.side_effect = lambda: next(fetchone_results)

    conn = MagicMock()
    conn.cursor.return_value = cur

    result = _sense_blast_radius(conn, sense_id=42)

    assert result["total"] == 10
    assert result["reviewed"] == 3
    assert result["marginal"] == 4


# ---------------------------------------------------------------------------
# db.approve_proposal — dispatch per kind, race, and rollback propagation
# ---------------------------------------------------------------------------


def _make_repo_patch(proposal_row):
    """Patch ProposalRepository so approve_proposal/reject_proposal see one pending row."""
    mock_repo = MagicMock()
    mock_repo.get.return_value = proposal_row
    mock_repo.decide.return_value = True
    mock_repo.supersede_sense_wide_siblings.return_value = None
    return patch("server.db.ProposalRepository", return_value=mock_repo), mock_repo


def test_approve_proposal_not_found_returns_status():
    from server.db import approve_proposal

    patcher, mock_repo = _make_repo_patch(None)
    with patcher:
        status, result = approve_proposal(MagicMock(), 1, "admin@example.com", None)
    assert status == "not_found"
    assert result is None


def test_approve_proposal_not_pending_returns_status():
    from server.db import approve_proposal

    patcher, mock_repo = _make_repo_patch({"proposal_id": 1, "status": "approved"})
    with patcher:
        status, result = approve_proposal(MagicMock(), 1, "admin@example.com", None)
    assert status == "not_pending"
    assert result is None


def test_approve_proposal_dispatches_rendering_to_correct_service():
    from server.db import approve_proposal

    proposal_row = {
        "proposal_id": 1,
        "status": "pending",
        "kind": "rendering",
        "sense_id": 42,
        "proposed_sk": "milost",
    }
    patcher, mock_repo = _make_repo_patch(proposal_row)
    with (
        patcher,
        patch("server.db.apply_rendering_change", return_value={"applied": True}) as mock_apply,
    ):
        status, result = approve_proposal(MagicMock(), 1, "admin@example.com", "note")
    assert status == "ok"
    assert result == {"applied": True}
    mock_apply.assert_called_once()
    mock_repo.decide.assert_called_once()
    mock_repo.supersede_sense_wide_siblings.assert_called_once()


def test_approve_proposal_sense_here_record_only_calls_no_service():
    from server.db import approve_proposal

    proposal_row = {
        "proposal_id": 2,
        "status": "pending",
        "kind": "sense_here",
        "sense_id": 42,
        "proposed_sense_id": None,
        "origin_segment_id": 99,
    }
    patcher, mock_repo = _make_repo_patch(proposal_row)
    with (
        patcher,
        patch("server.db.apply_sense_here") as mock_apply,
    ):
        status, result = approve_proposal(MagicMock(), 2, "admin@example.com", None)
    assert status == "ok"
    assert result == {"acknowledged": True}
    mock_apply.assert_not_called()
    # Record-only sense_here is per-segment, not sense-wide — no sibling supersede.
    mock_repo.supersede_sense_wide_siblings.assert_not_called()


def test_approve_proposal_add_term_exists_raises_value_error():
    from server.db import approve_proposal

    proposal_row = {
        "proposal_id": 3,
        "status": "pending",
        "kind": "add_term",
        "sense_id": None,
        "latin_lemma": "gratia",
        "proposed_sk": "milost",
        "note": None,
    }
    patcher, mock_repo = _make_repo_patch(proposal_row)
    with (
        patcher,
        patch("server.db.apply_add_term", side_effect=ValueError("term_exists")),
    ):
        with pytest.raises(ValueError, match="term_exists"):
            approve_proposal(MagicMock(), 3, "admin@example.com", None)
    # The proposal must still be pending — decide() is only called after a
    # successful apply, and the ValueError must propagate uncaught so the
    # caller's get_conn() rolls back any partial writes.
    mock_repo.decide.assert_not_called()


def test_approve_proposal_race_raises_proposal_race():
    from server.db import ProposalRaceError, approve_proposal

    proposal_row = {
        "proposal_id": 4,
        "status": "pending",
        "kind": "rendering",
        "sense_id": 42,
        "proposed_sk": "milost",
    }
    patcher, mock_repo = _make_repo_patch(proposal_row)
    mock_repo.decide.return_value = False  # lost the race
    with (
        patcher,
        patch("server.db.apply_rendering_change", return_value={"applied": True}),
    ):
        with pytest.raises(ProposalRaceError):
            approve_proposal(MagicMock(), 4, "admin@example.com", None)
    mock_repo.supersede_sense_wide_siblings.assert_not_called()


def test_reject_proposal_not_pending_returns_status():
    from server.db import reject_proposal

    patcher, mock_repo = _make_repo_patch({"proposal_id": 1, "status": "rejected"})
    with patcher:
        status = reject_proposal(MagicMock(), 1, "admin@example.com", None)
    assert status == "not_pending"


