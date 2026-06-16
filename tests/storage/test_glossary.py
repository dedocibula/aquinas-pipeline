"""Unit tests for GlossaryRepository — canned rows → typed models, write SQL."""

from __future__ import annotations

from storage.models import Constraint, Sense, Term
from storage.repositories import GlossaryRepository


def _gloss_row(**overrides) -> dict:
    base = {
        "term_id": 1,
        "latin_lemma": "ratio",
        "is_multiword": False,
        "category": None,
        "la_surface": None,
        "sense_id": 42,
        "context_label": None,
        "version": 1,
        "cs_lemma": "rozum",
        "cs_content": "rozum",
        "en_cue": "reason",
        "sk_content": "rozum",
    }
    base.update(overrides)
    return base


def _locked_row(**overrides) -> dict:
    base = {
        "latin_lemma": "ratio",
        "category": None,
        "latin_surface": None,
        "required_slovak": "rozum",
        "sense_id": 42,
        "version": 1,
        "context_label": None,
    }
    base.update(overrides)
    return base


# ── load_glossary ──────────────────────────────────────────────────────────────


def test_load_glossary_groups_senses_under_terms(fake_conn):
    rows = [
        _gloss_row(sense_id=42),
        _gloss_row(sense_id=43, context_label="faculty"),
    ]
    conn = fake_conn(fetchall_rows=rows)
    multiword, singleword = GlossaryRepository(conn).load_glossary()

    assert multiword == []
    assert len(singleword) == 1
    term = singleword[0]
    assert isinstance(term, Term)
    assert term.latin_lemma == "ratio"
    assert len(term.senses) == 2
    assert all(isinstance(s, Sense) for s in term.senses)
    assert term.senses[1].context_label == "faculty"


def test_load_glossary_splits_multiword(fake_conn):
    rows = [
        _gloss_row(term_id=1, latin_lemma="esse", is_multiword=False, sense_id=1),
        _gloss_row(term_id=2, latin_lemma="actus purus", is_multiword=True, sense_id=2),
    ]
    conn = fake_conn(fetchall_rows=rows)
    multiword, singleword = GlossaryRepository(conn).load_glossary()

    assert [t.latin_lemma for t in multiword] == ["actus purus"]
    assert [t.latin_lemma for t in singleword] == ["esse"]


def test_load_glossary_sorted_by_lemma(fake_conn):
    rows = [
        _gloss_row(term_id=1, latin_lemma="ratio", sense_id=1),
        _gloss_row(term_id=2, latin_lemma="actus", sense_id=2),
    ]
    conn = fake_conn(fetchall_rows=rows)
    _, singleword = GlossaryRepository(conn).load_glossary()
    assert [t.latin_lemma for t in singleword] == ["actus", "ratio"]


def test_load_glossary_carries_category(fake_conn):
    conn = fake_conn(fetchall_rows=[_gloss_row(category="formula")])
    _, singleword = GlossaryRepository(conn).load_glossary()
    assert singleword[0].category == "formula"


def test_load_glossary_carries_la_surface_onto_term_and_sense(fake_conn):
    conn = fake_conn(fetchall_rows=[_gloss_row(la_surface="Sed contra")])
    _, singleword = GlossaryRepository(conn).load_glossary()
    term = singleword[0]
    assert term.la_surface == "Sed contra"
    assert term.senses[0].la_surface == "Sed contra"


def test_load_glossary_la_surface_none_stays_none(fake_conn):
    conn = fake_conn(fetchall_rows=[_gloss_row(la_surface=None)])
    _, singleword = GlossaryRepository(conn).load_glossary()
    assert singleword[0].la_surface is None


def test_load_glossary_multiword_carries_la_surface(fake_conn):
    conn = fake_conn(fetchall_rows=[_gloss_row(is_multiword=True, la_surface="actus essendi")])
    multiword, singleword = GlossaryRepository(conn).load_glossary()
    assert singleword == []
    assert multiword[0].la_surface == "actus essendi"


# ── locked_terms ───────────────────────────────────────────────────────────────


def test_locked_terms_returns_constraints(fake_conn):
    conn = fake_conn(fetchall_rows=[_locked_row(), _locked_row(latin_lemma="esse", sense_id=43)])
    result = GlossaryRepository(conn).locked_terms(1)
    assert len(result) == 2
    assert all(isinstance(c, Constraint) for c in result)
    assert result[0].latin_lemma == "ratio"


def test_locked_terms_preserves_null_category(fake_conn):
    conn = fake_conn(fetchall_rows=[_locked_row(category=None)])
    assert GlossaryRepository(conn).locked_terms(1)[0].category is None


def test_locked_terms_passes_segment_id(fake_conn):
    conn = fake_conn(fetchall_rows=[])
    GlossaryRepository(conn).locked_terms(5)
    _, params = conn.executed[-1]
    assert params == (5,)


# ── writes ─────────────────────────────────────────────────────────────────────


def test_update_sense_status_executes_update(fake_conn):
    conn = fake_conn()
    GlossaryRepository(conn).update_sense_status(42, "approved")
    sql, params = conn.executed[-1]
    assert "UPDATE glossary_sense SET status" in sql
    assert params == ("approved", 42)


def test_bump_sense_version_returns_new_value(fake_conn):
    conn = fake_conn(fetchone_results=[(3,)])
    assert GlossaryRepository(conn).bump_sense_version(42) == 3
    sql, params = conn.executed[-1]
    assert "version = version + 1" in sql
    assert params == (42,)


def test_write_human_rendering_upserts(fake_conn):
    conn = fake_conn()
    GlossaryRepository(conn).write_human_rendering(42, "milosť", 9)
    sql, params = conn.executed[-1]
    assert "INSERT INTO sense_rendering" in sql
    assert params == (42, "milosť", 9)
