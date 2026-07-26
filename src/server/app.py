"""
Flask preview server — Latin | Slovak parallel text viewer.

URL structure: /~ST.I.Q3.A1
Read-only for anonymous visitors. Editors authenticate via Google OAuth and
can approve/edit segments. Editor emails are stored in the `editor` DB table.
"""

from __future__ import annotations

import os
from functools import wraps

from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from common.path import (
    article_path_from_locator,
    locator_to_title,
    ltree_depth,
    ltree_to_url_locator,
    url_to_ltree,
)

load_dotenv()

from export.xliff import export_pars_bytes  # noqa: E402
from server.db import (  # noqa: E402
    PER_SEGMENT_KINDS,
    PROPOSAL_KIND_ADD_TERM,
    PROPOSAL_KIND_CHANGE_EVERYWHERE,
    PROPOSAL_KIND_REMOVE_HERE,
    PROPOSAL_KIND_RETIRE_EVERYWHERE,
    PROPOSAL_KIND_WRONG_SENSE_HERE,
    SENSE_WIDE_KINDS,
    ProposalRaceError,
    add_comment,
    approve_proposal,
    approve_segment,
    delete_comment,
    get_activity_feed,
    get_all_questions,
    get_article_segments,
    get_comment_counts,
    get_cost_per_segment,
    get_decided_proposals_view,
    get_distinct_pars,
    get_pending_proposal_count,
    get_pending_proposal_counts,
    get_pending_proposals_view,
    get_prev_next_article,
    get_question_articles,
    get_question_preamble_segment,
    get_question_title_segment,
    get_questions_by_status,
    get_segment_constraints,
    get_structural_formulas,
    get_term_senses,
    get_translation_progress,
    is_admin,
    is_editor,
    list_comments,
    mark_thread_read,
    propose_add_term,
    propose_sense_change,
    reject_proposal,
    reopen_proposal,
    reopen_thread,
    resolve_thread,
    review_segment,
    segment_exists,
    unapprove_segment,
)
from storage.db import get_conn  # noqa: E402 — must come after load_dotenv

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

_WORK_ID = 1  # single-work pipeline; all routes use this

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


def requires_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_editor") or not session.get("is_admin"):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        return f(*args, **kwargs)
    return decorated

# Populated on the first request; lives for the process lifetime.
_formulas: dict[str, str] = {}


# ---------------------------------------------------------------------------
# status -> HTTP dispatch helper
# ---------------------------------------------------------------------------

# Every status string an action function in server.db can return. A missing
# key is a bug — fail loudly via KeyError rather than defaulting silently.
_STATUS_HTTP = {
    "ok": 200,
    "notfound": 404,
    "not_found": 404,
    "conflict": 409,
    "forbidden": 403,
    "not_pending": 409,
    "not_rejected": 409,
    "wrong_status": 409,
    "already_polished": 409,
    "proposed_sk_required": 400,
    "missing_target": 400,
    "no_change": 400,
    "wrong_term": 400,
    "not_locked_here": 400,
    "term_exists": 400,
}


def _json(status: str, **payload):
    return jsonify({"ok": status == "ok", **payload}), _STATUS_HTTP[status]


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
        admin = is_admin(conn, email) if editor else False
    session["email"] = email
    session["is_editor"] = editor
    session["is_admin"] = admin
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
        "is_admin": session.get("is_admin", False),
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
        if session.get("is_admin"):
            progress["pending_proposals"] = get_pending_proposal_count(conn)

    # Group by pars (first component of the path).
    grouped: dict[str, list[dict]] = {}
    for q in questions:
        pars = q["question_path"].split(".")[0]
        grouped.setdefault(pars, []).append(q)

    # Build display-friendly URL labels.
    for q in questions:
        q["url_locator"] = ltree_to_url_locator(q["question_path"])

    return render_template(
        "index.html",
        grouped=grouped,
        progress=progress,
        ltree_to_url=ltree_to_url_locator,
    )


@app.route("/~<path:st_locator>")
def text_view(st_locator: str):
    """Dispatch to question or article view based on path depth."""
    ltree_path = url_to_ltree(st_locator)
    depth = ltree_depth(ltree_path)

    if depth == 1:
        abort(404)  # pars-level view not implemented
    elif depth == 2:
        return _question_view(ltree_path, st_locator)
    elif depth == 3:
        # 'preamble' is the one element_type whose locator sits at article depth
        # (<question_path>.preamble) but is rendered on the question page, not
        # its own article page (see get_question_articles / get_question_preamble_segment).
        if ltree_path.rsplit(".", 1)[-1].lower() == "preamble":
            question_path = ltree_path.rsplit(".", 1)[0]
            return _question_view(question_path, ltree_to_url_locator(question_path))
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
            constraint_ids.append(title_seg.segment_id)
        if preamble_seg:
            constraint_ids.append(preamble_seg.segment_id)
        all_constraints = get_segment_constraints(conn, constraint_ids) if constraint_ids else {}
        title_constraints = all_constraints.get(title_seg.segment_id, []) if title_seg else []
        preamble_constraints = all_constraints.get(preamble_seg.segment_id, []) if preamble_seg else []
        pending_sense_ids = sorted(
            {c.sense_id for lst in (title_constraints, preamble_constraints) for c in lst}
        )
        pending_counts = get_pending_proposal_counts(conn, pending_sense_ids)
        comment_counts = (
            get_comment_counts(conn, constraint_ids, session["email"]) if session.get("is_editor") else {}
        )

    if not articles:
        abort(404)

    for a in articles:
        a["url_locator"] = ltree_to_url_locator(a["article_path"])
        a["title"] = locator_to_title(a["article_path"])

    return render_template(
        "question.html",
        question_path=ltree_path,
        question_title=locator_to_title(ltree_path),
        st_locator=st_locator,
        articles=articles,
        title_seg=title_seg,
        title_constraints=title_constraints,
        preamble_seg=preamble_seg,
        preamble_constraints=preamble_constraints,
        pending_counts=pending_counts,
        comment_counts=comment_counts,
    )


def _article_view(ltree_path: str, st_locator: str):
    with get_conn() as conn:
        segments = get_article_segments(conn, ltree_path)
        if not segments:
            abort(404)
        nav = get_prev_next_article(conn, ltree_path)
        segment_ids = [s.segment_id for s in segments]
        constraints = get_segment_constraints(conn, segment_ids)
        pending_sense_ids = sorted({c.sense_id for lst in constraints.values() for c in lst})
        pending_counts = get_pending_proposal_counts(conn, pending_sense_ids)
        comment_counts = (
            get_comment_counts(conn, segment_ids, session["email"]) if session.get("is_editor") else {}
        )

    # Build arg/reply numbering maps.
    # arg_number[segment_id] = sequential 1-based index among args in this article.
    # reply_number[segment_id] = matches the arg number via reply_to.
    arg_number: dict[int, int] = {}
    arg_counter = 0
    for seg in segments:
        if seg.element_type == "arg":
            arg_counter += 1
            arg_number[seg.segment_id] = arg_counter

    reply_number: dict[int, int] = {}
    for seg in segments:
        if seg.element_type == "reply":
            reply_to_id = seg.reply_to
            if reply_to_id is not None and reply_to_id in arg_number:
                reply_number[seg.segment_id] = arg_number[reply_to_id]

    # Convert nav paths to URL locators.
    nav_urls = {
        "prev": ltree_to_url_locator(nav["prev"]) if nav["prev"] else None,
        "next": ltree_to_url_locator(nav["next"]) if nav["next"] else None,
    }

    return render_template(
        "article.html",
        article_path=ltree_path,
        article_title=locator_to_title(ltree_path),
        st_locator=st_locator,
        segments=segments,
        arg_number=arg_number,
        reply_number=reply_number,
        nav=nav_urls,
        formulas=_formulas,
        constraints=constraints,
        pending_counts=pending_counts,
        comment_counts=comment_counts,
        ltree_to_url=ltree_to_url_locator,
    )


_VALID_STATUSES = {"translated", "needs_human", "pending"}


@app.route("/status/<status>")
@requires_editor
def status_list(status: str):
    """List questions that have at least one segment with the given translation status."""
    if status not in _VALID_STATUSES:
        abort(404)
    with get_conn() as conn:
        questions = get_questions_by_status(conn, status)

    for q in questions:
        q["url_locator"] = ltree_to_url_locator(q["question_path"])

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
        result = review_segment(
            conn, segment_id, action,
            expected_version=expected_version,
            reviewer_email=reviewer_email,
            text=text,
            note=note,
        )

    return _json(result.status, **(result.payload or {}))


# ---------------------------------------------------------------------------
# Comment threads (editor-internal, per segment)
# ---------------------------------------------------------------------------


def _thread_json(thread) -> dict:
    return {
        "comments": [_comment_json(c) for c in thread.comments],
        "resolved": thread.resolved,
        "open_count": thread.open_count,
    }


def _comment_json(c) -> dict:
    return {
        "comment_id": c.comment_id,
        "segment_id": c.segment_id,
        "author": c.author,
        "body": c.body,
        "created_at": c.created_at.isoformat(),
        "resolved": c.resolved,
        "resolved_by": c.resolved_by,
        "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
    }


@app.route("/api/segment/<int:segment_id>/comments", methods=["GET"])
@requires_editor
def list_comments_route(segment_id: int):
    """Return a segment's comment thread and mark it read for the current user."""
    with get_conn() as conn:
        if not segment_exists(conn, segment_id):
            return _json("not_found", error="not found")
        thread = list_comments(conn, segment_id)
        mark_thread_read(conn, segment_id, session["email"])
    return _json("ok", **_thread_json(thread))


@app.route("/api/segment/<int:segment_id>/comments", methods=["POST"])
@requires_editor
def add_comment_route(segment_id: int):
    """Add a comment. Body: ``{body}``. Reopens the thread if it was resolved."""
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"ok": False, "error": "empty body"}), 400

    with get_conn() as conn:
        if not segment_exists(conn, segment_id):
            return _json("not_found", error="not found")
        comment = add_comment(conn, segment_id, session["email"], body)
        thread = list_comments(conn, segment_id)
    return _json("ok", comment=_comment_json(comment), open_count=thread.open_count)


@app.route("/api/segment/<int:segment_id>/comments/resolve", methods=["POST"])
@requires_editor
def resolve_thread_route(segment_id: int):
    """Mark every open comment in the segment's thread as resolved."""
    with get_conn() as conn:
        resolve_thread(conn, segment_id, session["email"])
    return jsonify({"ok": True})


@app.route("/api/segment/<int:segment_id>/comments/reopen", methods=["POST"])
@requires_editor
def reopen_thread_route(segment_id: int):
    """Clear resolved state on the segment's thread."""
    with get_conn() as conn:
        reopen_thread(conn, segment_id)
    return jsonify({"ok": True})


@app.route("/api/comment/<int:comment_id>", methods=["DELETE"])
@requires_editor
def delete_comment_route(comment_id: int):
    """Delete a comment. Only its author may delete it."""
    with get_conn() as conn:
        result = delete_comment(conn, comment_id, session["email"])
    if result == "ok":
        return _json("ok")
    if result == "notfound":
        return _json("notfound", error="not found")
    return _json("forbidden", error="forbidden")


# ---------------------------------------------------------------------------
# Editor glossary proposals (Stage 3 of the editor-glossary-proposals plan)
# ---------------------------------------------------------------------------

_PROPOSE_KINDS = {
    PROPOSAL_KIND_CHANGE_EVERYWHERE,
    PROPOSAL_KIND_WRONG_SENSE_HERE,
    PROPOSAL_KIND_REMOVE_HERE,
    PROPOSAL_KIND_RETIRE_EVERYWHERE,
}


@app.route("/api/sense/<int:sense_id>/alternatives")
@requires_editor
def sense_alternatives_route(sense_id: int):
    """Return the term owning ``sense_id`` and all its senses, for the
    "wrong sense here" dropdown."""
    with get_conn() as conn:
        term = get_term_senses(conn, sense_id)
    if term is None:
        return _json("not_found", error="not found")
    senses = [
        {
            "sense_id": s.sense_id,
            "context_label": s.context_label,
            "status": s.status,
            "slovak": s.sk_content,
        }
        for s in term["senses"]
    ]
    return _json("ok", latin_lemma=term["latin_lemma"], senses=senses)


@app.route("/api/sense/<int:sense_id>/propose", methods=["POST"])
@requires_editor
def propose_sense_change_route(sense_id: int):
    """Record an editor's proposed glossary change (D1 — inert until an admin approves it).

    Body: ``{kind, proposed_sk?, proposed_sense_id?, note?, origin_segment_id?}``.
    ``kind`` is one of the PROPOSAL_KIND_CHANGE_EVERYWHERE / WRONG_SENSE_HERE /
    REMOVE_HERE / RETIRE_EVERYWHERE values (add_term is a separate endpoint —
    see ``/api/term-proposal``).
    """
    data = request.get_json(silent=True) or {}
    kind = data.get("kind", "")
    if kind not in _PROPOSE_KINDS:
        return jsonify({"ok": False, "error": "invalid kind"}), 400

    note = (data.get("note") or "").strip() or None
    proposed_sk = (data.get("proposed_sk") or "").strip() or None

    raw_origin_segment_id = data.get("origin_segment_id")
    origin_segment_id: int | None = None
    if raw_origin_segment_id:
        try:
            origin_segment_id = int(raw_origin_segment_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid origin_segment_id"}), 400
    if kind in (PROPOSAL_KIND_WRONG_SENSE_HERE, PROPOSAL_KIND_REMOVE_HERE) and not origin_segment_id:
        return jsonify({"ok": False, "error": "origin_segment_id required"}), 400

    raw_proposed_sense_id = data.get("proposed_sense_id")
    proposed_sense_id: int | None = None
    if raw_proposed_sense_id:
        try:
            proposed_sense_id = int(raw_proposed_sense_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid proposed_sense_id"}), 400

    with get_conn() as conn:
        result = propose_sense_change(
            conn,
            sense_id,
            kind,
            proposed_sk=proposed_sk,
            proposed_sense_id=proposed_sense_id,
            note=note,
            origin_segment_id=origin_segment_id,
            proposed_by=session["email"],
        )

    return _json(result.status, **(result.payload or {}))


@app.route("/api/term-proposal", methods=["POST"])
@requires_editor
def propose_term_route():
    """Record an editor's suggestion for a missing glossary term (kind=add_term).

    Body: ``{latin_lemma, proposed_sk, note?, origin_segment_id?}``.
    """
    data = request.get_json(silent=True) or {}
    latin_lemma = (data.get("latin_lemma") or "").strip()
    proposed_sk = (data.get("proposed_sk") or "").strip()
    if not latin_lemma or not proposed_sk:
        return jsonify({"ok": False, "error": "latin_lemma and proposed_sk required"}), 400

    note = (data.get("note") or "").strip() or None
    raw_origin_segment_id = data.get("origin_segment_id")
    origin_segment_id: int | None = None
    if raw_origin_segment_id:
        try:
            origin_segment_id = int(raw_origin_segment_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid origin_segment_id"}), 400

    with get_conn() as conn:
        result = propose_add_term(
            conn,
            latin_lemma=latin_lemma,
            proposed_sk=proposed_sk,
            note=note,
            origin_segment_id=origin_segment_id,
            proposed_by=session["email"],
        )

    return _json(result.status, **(result.payload or {}))


# ---------------------------------------------------------------------------
# Admin proposal queue (Stage 4 of the editor-glossary-proposals plan)
# ---------------------------------------------------------------------------

@app.route("/glossary/proposals")
@requires_admin
def glossary_proposals_page():
    """Admin queue: every pending proposal, grouped by impact, with blast
    radius / cost preview (sense-wide) or origin locator (per-segment)."""
    with get_conn() as conn:
        proposals = get_pending_proposals_view(conn)
        cost_per_segment = get_cost_per_segment(conn)
        decided = get_decided_proposals_view(conn)
    return render_template(
        "glossary_proposals.html",
        sense_wide=[p for p in proposals if p.kind in SENSE_WIDE_KINDS],
        per_segment=[p for p in proposals if p.kind in PER_SEGMENT_KINDS],
        add_terms=[p for p in proposals if p.kind == PROPOSAL_KIND_ADD_TERM],
        decided=decided,
        cost_per_segment=cost_per_segment,
        ltree_to_url=ltree_to_url_locator,
        article_path=article_path_from_locator,
    )


@app.route("/timeline")
@requires_admin
def timeline_page():
    """Admin activity feed: reviews, comments, and machine-run markers, newest first."""
    before = request.args.get("before")
    with get_conn() as conn:
        entries = get_activity_feed(conn, before=before, limit=50)
    next_before = entries[-1].ts.isoformat() if len(entries) == 50 else None
    return render_template(
        "timeline.html",
        entries=entries,
        ltree_to_url=ltree_to_url_locator,
        next_before=next_before,
    )


@app.route("/api/proposal/<int:proposal_id>/approve", methods=["POST"])
@requires_admin
def approve_proposal_route(proposal_id: int):
    """Apply an approved proposal ($0 — no translation is ever triggered here, D4).

    Body: ``{note?, proposed_sk?}`` — optional admin decision note, and an
    optional lightly-edited replacement for the editor's proposed text (only
    accepted for kinds carrying free-text ``proposed_sk`` — see
    ``approve_proposal``'s docstring).
    """
    data = request.get_json(silent=True) or {}
    decision_note = (data.get("note") or "").strip() or None
    edited_sk = data.get("proposed_sk")
    if edited_sk is not None:
        edited_sk = edited_sk.strip()

    try:
        with get_conn() as conn:
            result = approve_proposal(
                conn, proposal_id, session["email"], decision_note, edited_sk=edited_sk
            )
    except ProposalRaceError:
        return jsonify({"ok": False, "error": "already decided"}), 409
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 409

    return _json(result.status, **(result.payload or {}))


@app.route("/api/proposal/<int:proposal_id>/reject", methods=["POST"])
@requires_admin
def reject_proposal_route(proposal_id: int):
    """Reject a pending proposal. Body: ``{note?}`` — optional admin decision note."""
    data = request.get_json(silent=True) or {}
    decision_note = (data.get("note") or "").strip() or None
    with get_conn() as conn:
        result = reject_proposal(conn, proposal_id, session["email"], decision_note)
    return _json(result.status, **(result.payload or {}))


@app.route("/api/proposal/<int:proposal_id>/reopen", methods=["POST"])
@requires_admin
def reopen_proposal_route(proposal_id: int):
    """Clone a rejected proposal into a new pending row for reconsideration.

    The rejected row itself is untouched — it stays in the audit trail exactly
    as decided. Only ``rejected`` proposals may be reopened this way.
    """
    try:
        with get_conn() as conn:
            result = reopen_proposal(conn, proposal_id, session["email"])
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return _json(result.status, **(result.payload or {}))


# ---------------------------------------------------------------------------
# XLIFF export
# ---------------------------------------------------------------------------


@app.route("/export/<pars>")
@requires_editor
def export_xliff(pars: str):
    """Download XLIFF 2.0 for one pars (editor only)."""
    with get_conn() as conn:
        valid = get_distinct_pars(conn, _WORK_ID)
    if pars not in valid:
        abort(404)
    with get_conn() as conn:
        data = export_pars_bytes(conn, _WORK_ID, pars)
    return current_app.response_class(
        data,
        mimetype="application/xliff+xml",
        headers={"Content-Disposition": f'attachment; filename="{pars}.xlf"'},
    )


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
        return _json("ok")
    if result == "notfound":
        return _json("notfound", error="not found")
    return _json("wrong_status", error="wrong_status")


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
        return _json("ok")
    if result == "notfound":
        return _json("notfound", error="not found")
    if result == "already_polished":
        return _json("already_polished", error="already_polished")
    return _json("wrong_status", error="wrong_status")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=False, port=5000)
