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

    with patch("server.app.oauth") as mock_oauth:
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


def test_index_translated_badge_is_a_link(client):
    """The 'translated' badge on the index page links to /status/translated."""
    resp = client.get("/")
    html = resp.data.decode()
    assert 'href="/status/translated"' in html


def test_index_needs_human_badge_is_a_link(client):
    """The 'needs review' badge on the index page links to /status/needs_human."""
    resp = client.get("/")
    html = resp.data.decode()
    assert 'href="/status/needs_human"' in html


def test_index_pending_badge_is_not_a_link(client):
    """The 'pending' badge is a plain span — no link."""
    resp = client.get("/")
    html = resp.data.decode()
    assert 'href="/status/pending"' not in html


# ---------------------------------------------------------------------------
# /status/<status> route
# ---------------------------------------------------------------------------


def test_status_list_translated_returns_200(client):
    """GET /status/translated returns 200 for a valid status."""
    resp = client.get("/status/translated")
    assert resp.status_code == 200


def test_status_list_needs_human_returns_200(client):
    """GET /status/needs_human returns 200 for a valid status."""
    resp = client.get("/status/needs_human")
    assert resp.status_code == 200


def test_status_list_pending_returns_200(client):
    """GET /status/pending returns 200 for a valid status."""
    resp = client.get("/status/pending")
    assert resp.status_code == 200


def test_status_list_invalid_status_returns_404(client):
    """GET /status/bogus returns 404 for an unrecognised status."""
    resp = client.get("/status/bogus")
    assert resp.status_code == 404


def test_status_list_groups_questions_by_pars(client):
    """Status list renders a pars-section heading for each pars in the result."""
    resp = client.get("/status/needs_human")
    html = resp.data.decode()
    # FAKE_QUESTIONS_BY_STATUS has I and II-I pars
    assert "Pars I" in html
    assert "Pars II-I" in html


def test_status_list_shows_question_links(client):
    """Status list renders href links to each question's URL locator."""
    resp = client.get("/status/translated")
    html = resp.data.decode()
    # I.q3 → ST.I.Q3
    assert "/~ST.I.Q3" in html


def test_status_list_shows_segment_counts(client):
    """Status list annotates each question with its segment count."""
    resp = client.get("/status/translated")
    html = resp.data.decode()
    # FAKE_QUESTIONS_BY_STATUS has counts 4 and 2
    assert "4 segment" in html
    assert "2 segment" in html


def test_status_list_page_title_reflects_status(client):
    """Status list <title> contains the human-readable status label."""
    resp = client.get("/status/needs_human")
    html = resp.data.decode()
    assert "Needs review" in html


# ---------------------------------------------------------------------------
# Question view: Needs Review column
# ---------------------------------------------------------------------------


def test_question_view_has_needs_review_header(client):
    """Question article summary table has a 'Needs Review' column header."""
    resp = client.get("/~ST.I.Q3")
    html = resp.data.decode()
    assert "Needs Review" in html


def test_question_view_zero_needs_human_shows_plain_zero(client):
    """Articles with needs_human_count=0 display a plain '0', not a badge."""
    resp = client.get("/~ST.I.Q3")
    html = resp.data.decode()
    # FAKE_ARTICLES has needs_human_count=0; should not render badge-warn for it
    assert "badge-warn" not in html


def test_question_view_nonzero_needs_human_renders_badge(client):
    """Articles with needs_human_count>0 display a badge-warn with the count."""
    with patch("server.app.get_question_articles", return_value=FAKE_ARTICLES_WITH_NEEDS_HUMAN):
        resp = client.get("/~ST.I.Q3")
    html = resp.data.decode()
    assert 'class="badge badge-warn"' in html
    assert ">3<" in html


def test_question_view_highlights_needs_human_article_row(client):
    """Article rows with needs_human_count>0 get the row-needs-human CSS class."""
    with patch("server.app.get_question_articles", return_value=FAKE_ARTICLES_WITH_NEEDS_HUMAN):
        resp = client.get("/~ST.I.Q3")
    html = resp.data.decode()
    assert "row-needs-human" in html


def test_question_view_clean_article_has_no_highlight(client):
    """Article rows with needs_human_count=0 do not get the row-needs-human class."""
    resp = client.get("/~ST.I.Q3")
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
# /api/segment/<id>/polish route tests
# ---------------------------------------------------------------------------


def test_polish_route_returns_403_for_non_editor(client):
    """POST /api/segment/<id>/polish returns 403 for non-editor."""
    resp = client.post("/api/segment/1/polish")
    assert resp.status_code == 403
    assert resp.get_json() == {"ok": False, "error": "forbidden"}


def test_polish_route_polishes_translated_segment(editor_client):
    """POST /api/segment/<id>/polish returns 200 with polished_text + guard_flags."""
    from polish.polisher import PolishOutcome

    outcome = PolishOutcome(
        segment_id=1,
        guard_flags={"ok": True, "length_ratio": 1.02},
        polished_text="Polished text.",
    )
    with (
        patch("server.app.get_conn", make_fake_get_conn()),
        patch(
            "server.app.polish_segment",
            return_value=("polished", [], outcome),
        ),
    ):
        # Patch the DB query for translation_status inside get_conn
        from contextlib import contextmanager
        stub = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = ("translated",)
        stub.cursor.return_value = cur

        @contextmanager
        def fake_gc_translated():
            yield stub

        with patch("server.app.get_conn", fake_gc_translated):
            resp = editor_client.post("/api/segment/1/polish")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["polished_text"] == "Polished text."
    assert data["guard_flags"] == {"ok": True, "length_ratio": 1.02}
    assert data["flipped"] is False


def test_polish_route_flips_needs_human_to_translated(editor_client):
    """POST /api/segment/<id>/polish flips needs_human → translated and sets flipped=True.

    Atomicity: both the (sk,polish) write (inside polish_segment with _autocommit=False)
    and the status flip must be committed exactly once, together.
    """
    from contextlib import contextmanager

    from polish.polisher import PolishOutcome

    outcome = PolishOutcome(
        segment_id=2,
        guard_flags={"ok": True, "length_ratio": 1.0},
        polished_text="Polished.",
    )
    stub = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = ("needs_human",)
    stub.cursor.return_value = cur

    @contextmanager
    def fake_gc():
        yield stub

    with (
        patch("server.app.get_conn", fake_gc),
        patch("server.app.polish_segment", return_value=("polished", [], outcome)),
    ):
        resp = editor_client.post("/api/segment/2/polish")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["flipped"] is True
    # The status UPDATE must be present
    sql_calls = [c[0][0] for c in cur.execute.call_args_list]
    assert any("translation_status" in s and "translated" in s for s in sql_calls)
    # Exactly one commit: (sk,polish) write + status flip are atomic
    assert stub.commit.call_count == 1


def test_polish_route_single_commit_when_no_flip(editor_client):
    """No status flip for translated segments — still exactly one commit."""
    from contextlib import contextmanager

    from polish.polisher import PolishOutcome

    outcome = PolishOutcome(
        segment_id=2,
        guard_flags={"ok": True, "length_ratio": 1.0},
        polished_text="Polished.",
    )
    stub = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = ("translated",)
    stub.cursor.return_value = cur

    @contextmanager
    def fake_gc():
        yield stub

    with (
        patch("server.app.get_conn", fake_gc),
        patch("server.app.polish_segment", return_value=("polished", [], outcome)),
    ):
        resp = editor_client.post("/api/segment/2/polish")

    assert resp.status_code == 200
    assert resp.get_json()["flipped"] is False
    assert stub.commit.call_count == 1


def test_polish_route_calls_polish_segment_with_autocommit_false(editor_client):
    """The endpoint must pass _autocommit=False so both writes share one commit.

    Without _autocommit=False, polish_segment would commit the (sk,polish) write
    internally and the status flip would land in a separate transaction.
    """
    from contextlib import contextmanager

    from polish.polisher import PolishOutcome

    outcome = PolishOutcome(
        segment_id=6,
        guard_flags={"ok": True, "length_ratio": 1.0},
        polished_text="Polished.",
    )
    stub = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = ("translated",)
    stub.cursor.return_value = cur

    captured_kwargs: dict = {}

    def fake_polish(segment_id, conn, **kwargs):
        captured_kwargs.update(kwargs)
        return "polished", [], outcome

    @contextmanager
    def fake_gc():
        yield stub

    with (
        patch("server.app.get_conn", fake_gc),
        patch("server.app.polish_segment", side_effect=fake_polish),
    ):
        editor_client.post("/api/segment/6/polish")

    assert captured_kwargs.get("_autocommit") is False


def test_polish_route_returns_409_when_human_exists(editor_client):
    """POST /api/segment/<id>/polish returns 409 when (sk,human) row blocks polish."""
    from contextlib import contextmanager

    from polish.polisher import PolishOutcome

    stub = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = ("translated",)
    stub.cursor.return_value = cur

    @contextmanager
    def fake_gc():
        yield stub

    outcome = PolishOutcome(segment_id=3)
    with (
        patch("server.app.get_conn", fake_gc),
        patch("server.app.polish_segment", return_value=("skipped", [], outcome)),
    ):
        resp = editor_client.post("/api/segment/3/polish")

    assert resp.status_code == 409
    assert resp.get_json()["error"] == "human text exists"


def test_polish_route_returns_404_for_missing_segment(editor_client):
    """POST /api/segment/<id>/polish returns 404 when segment not found."""
    from contextlib import contextmanager

    stub = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = None   # segment does not exist
    stub.cursor.return_value = cur

    @contextmanager
    def fake_gc():
        yield stub

    with patch("server.app.get_conn", fake_gc):
        resp = editor_client.post("/api/segment/9999/polish")

    assert resp.status_code == 404


def test_polish_route_returns_404_when_no_model_draft(editor_client):
    """POST /api/segment/<id>/polish returns 404 when no (sk,model) draft exists."""
    from contextlib import contextmanager

    from polish.polisher import PolishOutcome

    stub = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = ("translated",)
    stub.cursor.return_value = cur

    @contextmanager
    def fake_gc():
        yield stub

    outcome = PolishOutcome(segment_id=4)
    with (
        patch("server.app.get_conn", fake_gc),
        patch("server.app.polish_segment", return_value=("no_source", [], outcome)),
    ):
        resp = editor_client.post("/api/segment/4/polish")

    assert resp.status_code == 404


def test_polish_route_returns_502_on_api_error(editor_client):
    """POST /api/segment/<id>/polish returns 502 when the Anthropic API fails."""
    from contextlib import contextmanager

    from polish.polisher import PolishOutcome

    stub = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = ("translated",)
    stub.cursor.return_value = cur

    @contextmanager
    def fake_gc():
        yield stub

    outcome = PolishOutcome(segment_id=5)
    with (
        patch("server.app.get_conn", fake_gc),
        patch("server.app.polish_segment", return_value=("error", [], outcome)),
    ):
        resp = editor_client.post("/api/segment/5/polish")

    assert resp.status_code == 502
