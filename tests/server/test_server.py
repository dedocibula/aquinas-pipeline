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
