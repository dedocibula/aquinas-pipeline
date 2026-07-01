"""
Flask preview server — Latin | Slovak parallel text viewer.

URL structure: /~ST.I.Q3.A1
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
    get_questions_by_status,
    get_segment_constraints,
    get_structural_formulas,
    get_translation_progress,
    is_editor,
    review_segment,
    unapprove_segment,
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
        editor = is_editor(conn, email)
    session["email"] = email
    session["is_editor"] = editor
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


@app.route("/~<path:st_locator>")
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


_VALID_STATUSES = {"translated", "needs_human", "pending"}


@app.route("/status/<status>")
def status_list(status: str):
    """List questions that have at least one segment with the given translation status."""
    if status not in _VALID_STATUSES:
        abort(404)
    with get_conn() as conn:
        questions = get_questions_by_status(conn, status)

    for q in questions:
        q["url_locator"] = _ltree_to_url_locator(q["question_path"])

    grouped: dict[str, list[dict]] = {}
    for q in questions:
        pars = q["question_path"].split(".")[0]
        grouped.setdefault(pars, []).append(q)

    return render_template(
        "status_list.html",
        status=status,
        grouped=grouped,
        total=len(questions),
    )


@app.route("/api/status")
def status():
    """JSON translation progress summary."""
    with get_conn() as conn:
        progress = get_translation_progress(conn)
    return jsonify(progress)


@app.route("/api/segment/<int:segment_id>/review", methods=["POST"])
@requires_editor
def review_segment_route(segment_id: int):
    """Create or update a human review for a segment.

    Body: ``{action, text?, note?, expected_version}``.
    ``action`` must be one of: save, accept, reset, note.
    For ``action=note``, an empty/absent ``note`` value clears the stored note.
    ``expected_version`` is the optimistic-lock token last read by the client (0 = no review yet).

    Returns 200 ``{ok:true, human_version:<new>}`` on success,
    400 on bad input, 404 on unknown segment, 409 ``{ok:false, error:"conflict"}``
    on concurrent edit.
    """
    data = request.get_json(silent=True) or {}
    action = data.get("action", "")
    if action not in {"save", "accept", "reset", "note"}:
        return jsonify({"ok": False, "error": "invalid action"}), 400

    try:
        expected_version = int(data.get("expected_version", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid expected_version"}), 400

    text: str | None = None
    if action == "save":
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "empty text"}), 400

    note: str | None = None
    if action == "note":
        raw = (data.get("note") or "").strip()
        note = raw or None  # None = clear note (writes NULL)
    reviewer_email: str = session["email"]

    with get_conn() as conn:
        result, new_version = review_segment(
            conn, segment_id, action,
            expected_version=expected_version,
            reviewer_email=reviewer_email,
            text=text,
            note=note,
        )

    if result == "ok":
        return jsonify({"ok": True, "human_version": new_version})
    if result == "notfound":
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": False, "error": "conflict"}), 409


# ---------------------------------------------------------------------------
# Approve / Unapprove endpoints
# ---------------------------------------------------------------------------


@app.route("/api/segment/<int:segment_id>/approve", methods=["POST"])
@requires_editor
def approve_segment_route(segment_id: int):
    """Flip a needs_human segment to translated, queuing it for batch polish.

    Returns:
        200  {ok: true}
        404  segment not found
        409  segment is not needs_human
    """
    with get_conn() as conn:
        result = approve_segment(conn, segment_id)
    if result == "ok":
        return jsonify({"ok": True})
    if result == "notfound":
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": False, "error": "wrong_status"}), 409


@app.route("/api/segment/<int:segment_id>/unapprove", methods=["POST"])
@requires_editor
def unapprove_segment_route(segment_id: int):
    """Flip a translated segment back to needs_human (only before batch polish runs).

    Returns:
        200  {ok: true}
        404  segment not found
        409  segment is not translated, or batch polish already wrote a (sk, polish) row
    """
    with get_conn() as conn:
        result = unapprove_segment(conn, segment_id)
    if result == "ok":
        return jsonify({"ok": True})
    if result == "notfound":
        return jsonify({"ok": False, "error": "not found"}), 404
    if result == "already_polished":
        return jsonify({"ok": False, "error": "already_polished"}), 409
    return jsonify({"ok": False, "error": "wrong_status"}), 409


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=False, port=5000)
