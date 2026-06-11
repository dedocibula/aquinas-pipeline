"""Tests for _load_glossary category + la_surface additions.

DB-free — uses FakeConn/FakeCursor that returns controlled rows.
"""

from __future__ import annotations

from common.glossary_repo import _load_glossary

# ── Fake DB helpers ────────────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self, cursor_factory=None):
        return FakeCursor(self._rows)


def _row(
    term_id=1,
    latin_lemma="ratio",
    is_multiword=False,
    category="term",
    sense_id=10,
    context_label=None,
    version=1,
    cs_lemma=None,
    cs_content=None,
    en_cue=None,
    sk_content="rozum",
    la_surface=None,
):
    return {
        "term_id": term_id,
        "latin_lemma": latin_lemma,
        "is_multiword": is_multiword,
        "category": category,
        "sense_id": sense_id,
        "context_label": context_label,
        "version": version,
        "cs_lemma": cs_lemma,
        "cs_content": cs_content,
        "en_cue": en_cue,
        "sk_content": sk_content,
        "la_surface": la_surface,
    }


# ── category field ─────────────────────────────────────────────────────────────


def test_category_carried_onto_term_dict():
    conn = FakeConn([_row(category="formula")])
    _, singleword = _load_glossary(conn)
    assert singleword[0]["category"] == "formula"


def test_category_term_default():
    conn = FakeConn([_row(category="term")])
    _, singleword = _load_glossary(conn)
    assert singleword[0]["category"] == "term"


def test_category_name():
    conn = FakeConn([_row(category="name")])
    _, singleword = _load_glossary(conn)
    assert singleword[0]["category"] == "name"


# ── la_surface field ───────────────────────────────────────────────────────────


def test_la_surface_none_when_no_rendering():
    conn = FakeConn([_row(la_surface=None)])
    _, singleword = _load_glossary(conn)
    assert singleword[0]["la_surface"] is None


def test_la_surface_carried_onto_term_dict():
    conn = FakeConn([_row(la_surface="Respondeo dicendum quod")])
    _, singleword = _load_glossary(conn)
    assert singleword[0]["la_surface"] == "Respondeo dicendum quod"


def test_la_surface_on_sense_dict():
    conn = FakeConn([_row(la_surface="Sed contra")])
    _, singleword = _load_glossary(conn)
    sense = singleword[0]["senses"][0]
    assert sense["la_surface"] == "Sed contra"


def test_la_surface_multisense_term_carries_column_value():
    """Multi-sense term: la_surface comes from gt.la_surface column (same for all senses)."""
    rows = [
        _row(term_id=1, latin_lemma="ratio", sense_id=10, la_surface="ratio"),
        _row(term_id=1, latin_lemma="ratio", sense_id=11, la_surface="ratio"),
    ]
    conn = FakeConn(rows)
    _, singleword = _load_glossary(conn)
    assert singleword[0]["la_surface"] == "ratio"


def test_la_surface_all_none_stays_none():
    rows = [
        _row(term_id=1, latin_lemma="ratio", sense_id=10, la_surface=None),
        _row(term_id=1, latin_lemma="ratio", sense_id=11, la_surface=None),
    ]
    conn = FakeConn(rows)
    _, singleword = _load_glossary(conn)
    assert singleword[0]["la_surface"] is None


# ── multiword routing ──────────────────────────────────────────────────────────


def test_multiword_term_with_la_surface():
    conn = FakeConn([_row(is_multiword=True, la_surface="actus essendi")])
    multiword, singleword = _load_glossary(conn)
    assert len(multiword) == 1
    assert len(singleword) == 0
    assert multiword[0]["la_surface"] == "actus essendi"


def test_formula_term_is_multiword():
    conn = FakeConn([_row(is_multiword=True, category="formula", la_surface="Sed contra")])
    multiword, _ = _load_glossary(conn)
    assert multiword[0]["category"] == "formula"
    assert multiword[0]["la_surface"] == "Sed contra"
