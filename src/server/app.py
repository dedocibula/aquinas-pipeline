"""
Flask preview server — Latin | Slovak parallel text viewer.

URL structure mirrors aquinas.cc: /la/sk/~ST.I.Q3.A1
Read-only; no auth; queries the production DB directly.
"""

from __future__ import annotations

import re

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template

load_dotenv()

from common.db import get_conn  # noqa: E402 — must come after load_dotenv
from server.db import (  # noqa: E402
    get_all_questions,
    get_article_segments,
    get_prev_next_article,
    get_question_articles,
    get_structural_formulas,
    get_translation_progress,
)

app = Flask(__name__)

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
    )


def _article_view(ltree_path: str, st_locator: str):
    with get_conn() as conn:
        segments = get_article_segments(conn, ltree_path)
        nav = get_prev_next_article(conn, ltree_path)

    if not segments:
        abort(404)

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
    )


@app.route("/api/status")
def status():
    """JSON translation progress summary."""
    with get_conn() as conn:
        progress = get_translation_progress(conn)
    return jsonify(progress)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=False, port=5000)
