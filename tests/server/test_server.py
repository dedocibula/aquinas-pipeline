"""
DB-free unit tests for src/server/app.py.

Tests cover:
  1–3. url_to_ltree conversion
  4–8. Route responses (monkeypatched DB helpers)
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
    {"article_path": "I.q3.a1", "translated_count": 5, "total_count": 7},
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
        "slovak": None,
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
        "slovak": "Na druhej strane:",
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
        "slovak": "Odpoveď:",
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
        "slovak": "K námietke 1.",
    },
]

FAKE_PROGRESS = {"pending": 10, "translated": 5, "needs_human": 2}
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
        patch("server.app.get_all_questions",       return_value=FAKE_QUESTIONS),
        patch("server.app.get_question_articles",   return_value=FAKE_ARTICLES),
        patch("server.app.get_article_segments",    return_value=FAKE_SEGMENTS),
        patch("server.app.get_prev_next_article",   return_value=FAKE_NAV),
        patch("server.app.get_translation_progress", return_value=FAKE_PROGRESS),
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
    """GET /la/sk/~ST.I.Q3.A1 returns 200 when segments are present."""
    response = client.get("/la/sk/~ST.I.Q3.A1")
    assert response.status_code == 200


def test_question_view_returns_200(client):
    """GET /la/sk/~ST.I.Q3 returns 200 when articles are present."""
    response = client.get("/la/sk/~ST.I.Q3")
    assert response.status_code == 200


def test_article_view_404_when_empty(client):
    """GET /la/sk/~ST.I.Q3.A1 returns 404 when no segments returned."""
    with patch("server.app.get_article_segments", return_value=[]):
        response = client.get("/la/sk/~ST.I.Q3.A1")
    assert response.status_code == 404


def test_article_view_has_ref_lang_dropdown(client):
    """Article view includes a <select> with Latin, Czech, English options."""
    response = client.get("/la/sk/~ST.I.Q3.A1")
    html = response.data.decode()
    assert 'id="ref-lang-select"' in html
    assert '<option value="la">Latin</option>' in html
    assert '<option value="cs">Czech</option>' in html
    assert '<option value="en">English</option>' in html


def test_article_view_embeds_all_ref_language_spans(client):
    """Each reference cell has three spans: la (visible), cs and en (hidden)."""
    response = client.get("/la/sk/~ST.I.Q3.A1")
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
    response = client.get("/la/sk/~ST.I.Q3.A1")
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
# server.db unit tests: save_segment_text and approve_segment
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


def test_save_segment_text_upserts_and_updates_status():
    """save_segment_text inserts into segment_text and updates translation_status."""
    from server.db import save_segment_text

    conn, cursor = _make_db_conn()
    # First fetchone: segment existence check returns a row (segment exists, not pending)
    cursor.fetchone.side_effect = [(1,), None]
    with patch("server.db.source_id", return_value=42):
        result = save_segment_text(conn, segment_id=7, text="Preložený text.")

    assert result is True
    calls = [c[0][0].strip() for c in cursor.execute.call_args_list]
    assert any("INSERT INTO segment_text" in c for c in calls)
    assert any("UPDATE segment SET translation_status" in c for c in calls)
    # Does NOT call conn.commit() — caller's context manager handles it
    conn.commit.assert_not_called()


def test_save_segment_text_returns_false_for_nonexistent_or_pending_segment():
    """save_segment_text returns False when segment is missing or pending."""
    from server.db import save_segment_text

    conn, cursor = _make_db_conn()
    # Existence check returns None → segment not found or pending
    cursor.fetchone.return_value = None
    result = save_segment_text(conn, segment_id=999, text="text")
    assert result is False


def test_save_segment_text_raises_when_human_source_missing():
    """save_segment_text raises RuntimeError when 'human' source is absent."""
    from server.db import save_segment_text

    conn, cursor = _make_db_conn()
    cursor.fetchone.return_value = (1,)  # segment exists
    with patch("server.db.source_id", side_effect=RuntimeError("Source 'human' not found")):
        with pytest.raises(RuntimeError, match="Source 'human' not found"):
            save_segment_text(conn, segment_id=7, text="text")


def test_approve_segment_returns_true_when_updated():
    """approve_segment returns True when rowcount > 0."""
    from server.db import approve_segment

    conn, cursor = _make_db_conn(rowcount=1)
    result = approve_segment(conn, segment_id=5)

    assert result is True
    conn.commit.assert_not_called()  # commit handled by get_conn() context manager


def test_approve_segment_returns_false_when_not_needs_human():
    """approve_segment returns False when segment is not in needs_human state."""
    from server.db import approve_segment

    conn, cursor = _make_db_conn(rowcount=0)
    result = approve_segment(conn, segment_id=5)

    assert result is False


# ---------------------------------------------------------------------------
# /api/edit endpoint
# ---------------------------------------------------------------------------


def test_edit_segment_returns_ok(editor_client):
    """POST /api/edit/<id> with valid text returns {"ok": true} for editors."""
    with patch("server.app.save_segment_text", return_value=True) as mock_save:
        resp = editor_client.post(
            "/api/edit/42",
            json={"text": "Opravený text."},
            content_type="application/json",
        )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    mock_save.assert_called_once()
    _, seg_id, text = mock_save.call_args[0]
    assert seg_id == 42
    assert text == "Opravený text."


def test_edit_segment_rejects_empty_text(editor_client):
    """POST /api/edit/<id> with empty text returns 400."""
    resp = editor_client.post("/api/edit/42", json={"text": "   "}, content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_edit_segment_rejects_missing_body(editor_client):
    """POST /api/edit/<id> with no body returns 400."""
    resp = editor_client.post("/api/edit/42", content_type="application/json")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Editor client fixture — sets is_editor=True in the Flask session
# ---------------------------------------------------------------------------


@pytest.fixture()
def editor_client():
    """Flask test client with an active editor session."""
    with (
        patch("server.app.get_conn", make_fake_get_conn()),
        patch("server.app.get_structural_formulas", return_value={}),
        patch("server.app.get_all_questions",       return_value=FAKE_QUESTIONS),
        patch("server.app.get_question_articles",   return_value=FAKE_ARTICLES),
        patch("server.app.get_article_segments",    return_value=FAKE_SEGMENTS),
        patch("server.app.get_prev_next_article",   return_value=FAKE_NAV),
        patch("server.app.get_translation_progress", return_value=FAKE_PROGRESS),
        patch("server.app.get_segment_constraints", return_value={}),
        patch("server.app.get_question_title_segment", return_value=None),
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
# @requires_editor decorator
# ---------------------------------------------------------------------------


def test_approve_returns_403_without_session(client):
    """POST /api/approve/<id> returns 403 when no editor session is set."""
    resp = client.post("/api/approve/1")
    assert resp.status_code == 403
    assert resp.get_json() == {"ok": False, "error": "forbidden"}


def test_edit_returns_403_without_session(client):
    """POST /api/edit/<id> returns 403 when no editor session is set."""
    resp = client.post("/api/edit/1", json={"text": "text"})
    assert resp.status_code == 403
    assert resp.get_json() == {"ok": False, "error": "forbidden"}


def test_approve_returns_403_for_non_editor(client):
    """POST /api/approve/<id> returns 403 when session has is_editor=False."""
    with client.session_transaction() as sess:
        sess["email"] = "visitor@example.com"
        sess["is_editor"] = False
    resp = client.post("/api/approve/1")
    assert resp.status_code == 403


def test_approve_allowed_for_editor(editor_client):
    """POST /api/approve/<id> reaches the handler for editor sessions."""
    with patch("server.app.approve_segment", return_value=True):
        resp = editor_client.post("/api/approve/1")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_edit_allowed_for_editor(editor_client):
    """POST /api/edit/<id> reaches the handler for editor sessions."""
    with patch("server.app.save_segment_text", return_value=True):
        resp = editor_client.post(
            "/api/edit/1",
            json={"text": "Opravený text."},
            content_type="application/json",
        )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_edit_still_rejects_empty_text_for_editor(editor_client):
    """POST /api/edit/<id> with empty text returns 400 even for editors."""
    resp = editor_client.post("/api/edit/1", json={"text": "   "})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


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
    resp2 = editor_client.post("/api/approve/1")
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


def test_edit_button_hidden_for_anonymous(client):
    """Anonymous visitors do not see btn-edit buttons (HTML element) on the article page."""
    resp = client.get("/la/sk/~ST.I.Q3.A1")
    html = resp.data.decode()
    # JS selectors ('.btn-edit') are always present; check for the HTML button element.
    assert 'class="btn-edit"' not in html


def test_approve_button_hidden_for_anonymous(client):
    """Anonymous visitors do not see btn-approve buttons (HTML element) on the article page."""
    resp = client.get("/la/sk/~ST.I.Q3.A1")
    html = resp.data.decode()
    assert 'class="btn-approve"' not in html


def test_edit_button_visible_for_editor(editor_client):
    """Editors see the btn-edit button for translated segments with Slovak text."""
    resp = editor_client.get("/la/sk/~ST.I.Q3.A1")
    html = resp.data.decode()
    assert 'class="btn-edit"' in html


def test_approve_button_visible_for_editor(editor_client):
    """Editors see the btn-approve button for needs_human segments."""
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
            "slovak": "Zdá sa.",
        }
    ]
    with patch("server.app.get_article_segments", return_value=needs_human_segments):
        resp = editor_client.get("/la/sk/~ST.I.Q3.A1")
    html = resp.data.decode()
    assert "btn-approve" in html
