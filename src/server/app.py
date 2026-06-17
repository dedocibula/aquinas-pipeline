"""
Flask preview server — Latin | Slovak parallel text viewer.

URL structure mirrors aquinas.cc: /la/sk/~ST.I.Q3.A1
Read-only for anonymous visitors. Editors authenticate via Google OAuth and
can approve/edit segments. Editor emails are stored in the `editor` DB table.
"""

from __future__ import annotations

import os
import re
from functools import wraps

from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

load_dotenv()

from server.db import (  # noqa: E402
    approve_segment,
    get_all_questions,
    get_article_segments,
    get_prev_next_article,
    get_question_articles,
    get_question_preamble_segment,
    get_question_title_segment,
    get_segment_constraints,
    get_structural_formulas,
    get_translation_progress,
    save_segment_text,
)
from storage.db import get_conn  # noqa: E402 — must come after load_dotenv

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

_client_id = os.environ.get("GOOGLE_CLIENT_ID")
_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
if not _client_id or not _client_secret:
    raise RuntimeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set")

oauth = OAuth(app)
oauth.register(
    name="google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_id=_client_id,
    client_secret=_client_secret,
    client_kwargs={"scope": "openid email profile"},
)


def requires_editor(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_editor"):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        return f(*args, **kwargs)
    return decorated

# Populated on the first request; lives for the process lifetime.
_formulas: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def url_to_ltree(st_locator: str) -> str:
    """Convert an aquinas.cc-style locator to an ltree path.

    Examples:
        ST.I.Q3.A1    → I.q3.a1
        ST.II-I.Q1.A1 → II-I.q1.a1
        I.Q3.A1       → I.q3.a1
    """
    s = st_locator
    if s.upper().startswith("ST."):
        s = s[3:]
    # Only lowercase Q→q and A→a; pars labels (I, II-I) are uppercase in DB.
    s = re.sub(r"Q(\d+)", lambda m: f"q{m.group(1)}", s)
    s = re.sub(r"A(\d+)", lambda m: f"a{m.group(1)}", s)
    return s


def _ltree_depth(path: str) -> int:
    """Count the number of labels in an ltree path string (dot-separated)."""
    return len(path.split("."))


def _locator_to_title(ltree_path: str) -> str:
    """Turn an ltree path like 'I.q3.a1' into 'ST I, Q3, A1'."""
    parts = ltree_path.split(".")
    labels = []
    for p in parts:
        if p.startswith("q"):
            labels.append("Q" + p[1:])
        elif p.startswith("a"):
            labels.append("A" + p[1:])
        else:
            labels.append(p.upper())
    return "ST " + ", ".join(labels)


def _ltree_to_url_locator(ltree_path: str) -> str:
    """Convert ltree path back to ST.X.QN.AN form for URL construction."""
    parts = ltree_path.split(".")
    result = []
    for p in parts:
        if p.startswith("q") and p[1:].isdigit():
            result.append("Q" + p[1:])
        elif p.startswith("a") and p[1:].isdigit():
            result.append("A" + p[1:])
        else:
            result.append(p.upper())
    return "ST." + ".".join(result)


# ---------------------------------------------------------------------------
# Before-request: warm formula cache
# ---------------------------------------------------------------------------


@app.before_request
def _load_formulas() -> None:
    global _formulas
    if not _formulas:
        with get_conn() as conn:
            _formulas = get_structural_formulas(conn)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.route("/login")
def login():
    redirect_uri = url_for("auth_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    try:
        token = oauth.google.authorize_access_token()
    except Exception:
        return jsonify({"ok": False, "error": "OAuth error"}), 400
    userinfo = token.get("userinfo") or oauth.google.userinfo()
    if not userinfo.get("email_verified"):
        return jsonify({"ok": False, "error": "email not verified"}), 403
    email = userinfo.get("email")
    if not email:
        return jsonify({"ok": False, "error": "no email in token"}), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM editor WHERE email = %s", (email,))
            is_editor = cur.fetchone() is not None
    session["email"] = email
    session["is_editor"] = is_editor
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.context_processor
def _inject_user():
    return {
        "current_user_email": session.get("email"),
        "is_editor": session.get("is_editor", False),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Index page: list all questions grouped by pars."""
    with get_conn() as conn:
        questions = get_all_questions(conn)
        progress = get_translation_progress(conn)

    # Group by pars (first component of the path).
    grouped: dict[str, list[dict]] = {}
    for q in questions:
        pars = q["question_path"].split(".")[0]
        grouped.setdefault(pars, []).append(q)

    # Build display-friendly URL labels.
    for q in questions:
        q["url_locator"] = _ltree_to_url_locator(q["question_path"])

    return render_template(
        "index.html",
        grouped=grouped,
        progress=progress,
        ltree_to_url=_ltree_to_url_locator,
    )


@app.route("/la/sk/~<path:st_locator>")
def text_view(st_locator: str):
    """Dispatch to question or article view based on path depth."""
    ltree_path = url_to_ltree(st_locator)
    depth = _ltree_depth(ltree_path)

    if depth == 1:
        abort(404)  # pars-level view not implemented
    elif depth == 2:
        return _question_view(ltree_path, st_locator)
    elif depth == 3:
        return _article_view(ltree_path, st_locator)
    else:
        abort(404)


def _question_view(ltree_path: str, st_locator: str):
    with get_conn() as conn:
        articles = get_question_articles(conn, ltree_path)
        title_seg = get_question_title_segment(conn, ltree_path)
        preamble_seg = get_question_preamble_segment(conn, ltree_path)
        constraint_ids = []
        if title_seg:
            constraint_ids.append(title_seg["segment_id"])
        if preamble_seg:
            constraint_ids.append(preamble_seg["segment_id"])
        all_constraints = get_segment_constraints(conn, constraint_ids) if constraint_ids else {}
        title_constraints = all_constraints.get(title_seg["segment_id"], []) if title_seg else []
        preamble_constraints = all_constraints.get(preamble_seg["segment_id"], []) if preamble_seg else []

    if not articles:
        abort(404)

    for a in articles:
        a["url_locator"] = _ltree_to_url_locator(a["article_path"])
        a["title"] = _locator_to_title(a["article_path"])

    return render_template(
        "question.html",
        question_path=ltree_path,
        question_title=_locator_to_title(ltree_path),
        st_locator=st_locator,
        articles=articles,
        title_seg=title_seg,
        title_constraints=title_constraints,
        preamble_seg=preamble_seg,
        preamble_constraints=preamble_constraints,
    )


def _article_view(ltree_path: str, st_locator: str):
    with get_conn() as conn:
        segments = get_article_segments(conn, ltree_path)
        if not segments:
            abort(404)
        nav = get_prev_next_article(conn, ltree_path)
        segment_ids = [s["segment_id"] for s in segments]
        constraints = get_segment_constraints(conn, segment_ids)

    # Build arg/reply numbering maps.
    # arg_number[segment_id] = sequential 1-based index among args in this article.
    # reply_number[segment_id] = matches the arg number via reply_to.
    arg_number: dict[int, int] = {}
    arg_counter = 0
    for seg in segments:
        if seg["element_type"] == "arg":
            arg_counter += 1
            arg_number[seg["segment_id"]] = arg_counter

    reply_number: dict[int, int] = {}
    for seg in segments:
        if seg["element_type"] == "reply":
            reply_to_id = seg["reply_to"]
            if reply_to_id is not None and reply_to_id in arg_number:
                reply_number[seg["segment_id"]] = arg_number[reply_to_id]

    # Convert nav paths to URL locators.
    nav_urls = {
        "prev": _ltree_to_url_locator(nav["prev"]) if nav["prev"] else None,
        "next": _ltree_to_url_locator(nav["next"]) if nav["next"] else None,
    }

    return render_template(
        "article.html",
        article_path=ltree_path,
        article_title=_locator_to_title(ltree_path),
        st_locator=st_locator,
        segments=segments,
        arg_number=arg_number,
        reply_number=reply_number,
        nav=nav_urls,
        formulas=_formulas,
        constraints=constraints,
    )


@app.route("/api/status")
def status():
    """JSON translation progress summary."""
    with get_conn() as conn:
        progress = get_translation_progress(conn)
    return jsonify(progress)


@app.route("/api/approve/<int:segment_id>", methods=["POST"])
@requires_editor
def approve(segment_id: int):
    """Flip a segment from needs_human → translated.

    Returns {"ok": true} if the status was changed, {"ok": false} if the
    segment was not in needs_human state (idempotent; never 4xx for that case).
    """
    with get_conn() as conn:
        changed = approve_segment(conn, segment_id)
    return jsonify({"ok": changed})


@app.route("/api/edit/<int:segment_id>", methods=["POST"])
@requires_editor
def edit_segment(segment_id: int):
    """Save a human-edited Slovak translation.

    Body: {"text": "..."}. Returns {"ok": true} on success, 400 on empty text.
    Always sets translation_status=translated.
    """
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "empty text"}), 400
    with get_conn() as conn:
        updated = save_segment_text(conn, segment_id, text)
    if not updated:
        return jsonify({"ok": False, "error": "segment not found or not editable"}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=False, port=5000)
