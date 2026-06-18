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


# ── get_current_sense ────────────────────────────────────────────────────────


def test_get_current_sense_returns_dict(fake_conn):
    conn = fake_conn(fetchone_results=[(101, 2, "proposed")])
    assert GlossaryRepository(conn).get_current_sense(101) == {
        "sense_id": 101,
        "version": 2,
        "status": "proposed",
    }


def test_get_current_sense_returns_none_when_missing(fake_conn):
    conn = fake_conn(fetchone_results=[None])
    assert GlossaryRepository(conn).get_current_sense(999) is None


def test_get_current_sense_queries_correct_table(fake_conn):
    conn = fake_conn(fetchone_results=[(1, 1, "proposed")])
    GlossaryRepository(conn).get_current_sense(5)
    sql, params = conn.executed[0]
    assert "glossary_sense" in sql
    assert params == (5,)


# ── get_la_surface ───────────────────────────────────────────────────────────


def test_get_la_surface_returns_content(fake_conn):
    conn = fake_conn(fetchone_results=[("Sed contra",)])
    assert GlossaryRepository(conn).get_la_surface(101) == "Sed contra"


def test_get_la_surface_returns_none_when_missing(fake_conn):
    conn = fake_conn(fetchone_results=[None])
    assert GlossaryRepository(conn).get_la_surface(101) is None


def test_get_la_surface_queries_glossary_term(fake_conn):
    conn = fake_conn(fetchone_results=[("Sed contra",)])
    GlossaryRepository(conn).get_la_surface(55)
    sql, params = conn.executed[0]
    assert "glossary_term" in sql
    assert "la_surface" in sql
    assert params == (55,)


# ── write_context_label ──────────────────────────────────────────────────────


def test_write_context_label_updates_sense(fake_conn):
    conn = fake_conn()
    GlossaryRepository(conn).write_context_label(101, "sanctifying grace")
    sql, params = conn.executed[-1]
    assert "UPDATE glossary_sense SET context_label" in sql
    assert params == ("sanctifying grace", 101)


def test_write_context_label_none_writes_null(fake_conn):
    conn = fake_conn()
    GlossaryRepository(conn).write_context_label(101, None)
    _, params = conn.executed[-1]
    assert params[0] is None


# ── write_human_surface ──────────────────────────────────────────────────────


def test_write_human_surface_updates_glossary_term(fake_conn):
    conn = fake_conn()
    GlossaryRepository(conn).write_human_surface(101, "Respondeo dicendum quod")
    sql, params = conn.executed[-1]
    assert "UPDATE glossary_term" in sql
    assert "la_surface" in sql
    assert "Respondeo dicendum quod" in params
    assert 101 in params


def test_write_human_surface_targets_correct_term(fake_conn):
    conn = fake_conn()
    GlossaryRepository(conn).write_human_surface(55, "Sed contra")
    sql, params = conn.executed[-1]
    assert "glossary_sense" in sql  # subselect resolves sense_id → term_id
    assert 55 in params


def test_sense_status_counts(fake_conn):
    conn = fake_conn(fetchall_rows=[("proposed", 12), ("approved", 88)])
    counts = GlossaryRepository(conn).sense_status_counts()
    assert counts == {"proposed": 12, "approved": 88}
    sql, _ = conn.executed[-1]
    assert "FROM glossary_sense GROUP BY status" in sql


def test_sense_status_counts_empty(fake_conn):
    conn = fake_conn(fetchall_rows=[])
    assert GlossaryRepository(conn).sense_status_counts() == {}


# ── find_term_by_lemma ───────────────────────────────────────────────────────


def test_find_term_by_lemma_returns_term_id(fake_conn):
    conn = fake_conn(fetchone_results=[(7,)])
    assert GlossaryRepository(conn).find_term_by_lemma("circe") == 7


def test_find_term_by_lemma_returns_none_when_missing(fake_conn):
    conn = fake_conn(fetchone_results=[None])
    assert GlossaryRepository(conn).find_term_by_lemma("circe") is None


def test_find_term_by_lemma_uses_case_insensitive_comparison(fake_conn):
    conn = fake_conn(fetchone_results=[(7,)])
    GlossaryRepository(conn).find_term_by_lemma("Circe")
    sql, params = conn.executed[0]
    assert "lower(latin_lemma)" in sql
    assert params == ("Circe",)


# ── insert_glossary_term ─────────────────────────────────────────────────────


def test_insert_glossary_term_returns_term_id(fake_conn):
    conn = fake_conn(fetchone_results=[(42,)])
    result = GlossaryRepository(conn).insert_glossary_term("circe", "name", "Circe, Circes")
    assert result == 42


def test_insert_glossary_term_sql_targets_glossary_term(fake_conn):
    conn = fake_conn(fetchone_results=[(1,)])
    GlossaryRepository(conn).insert_glossary_term("circe", "name", None)
    sql, params = conn.executed[0]
    assert "INSERT INTO glossary_term" in sql
    assert "RETURNING term_id" in sql


def test_insert_glossary_term_passes_all_fields(fake_conn):
    conn = fake_conn(fetchone_results=[(1,)])
    GlossaryRepository(conn).insert_glossary_term("circe", "name", "Circe, Circes")
    _, params = conn.executed[0]
    assert params == ("circe", "name", "Circe, Circes")


def test_insert_glossary_term_blank_category_becomes_none(fake_conn):
    conn = fake_conn(fetchone_results=[(1,)])
    GlossaryRepository(conn).insert_glossary_term("circe", None, None)
    _, params = conn.executed[0]
    assert params[1] is None


# ── insert_glossary_sense ────────────────────────────────────────────────────


def test_insert_glossary_sense_returns_sense_id(fake_conn):
    conn = fake_conn(fetchone_results=[(55,)])
    result = GlossaryRepository(conn).insert_glossary_sense(7, "mythological")
    assert result == 55


def test_insert_glossary_sense_sql_targets_glossary_sense(fake_conn):
    conn = fake_conn(fetchone_results=[(1,)])
    GlossaryRepository(conn).insert_glossary_sense(7, None)
    sql, _ = conn.executed[0]
    assert "INSERT INTO glossary_sense" in sql
    assert "RETURNING sense_id" in sql


def test_insert_glossary_sense_default_status_is_approved(fake_conn):
    conn = fake_conn(fetchone_results=[(1,)])
    GlossaryRepository(conn).insert_glossary_sense(7, None)
    _, params = conn.executed[0]
    assert "approved" in params


def test_insert_glossary_sense_version_starts_at_one(fake_conn):
    conn = fake_conn(fetchone_results=[(1,)])
    GlossaryRepository(conn).insert_glossary_sense(7, "mythological")
    sql, _ = conn.executed[0]
    assert "1" in sql  # version=1 literal in INSERT


def test_insert_glossary_sense_passes_context_label(fake_conn):
    conn = fake_conn(fetchone_results=[(1,)])
    GlossaryRepository(conn).insert_glossary_sense(7, "mythological")
    _, params = conn.executed[0]
    assert params[1] == "mythological"


def test_insert_glossary_sense_null_context_label(fake_conn):
    conn = fake_conn(fetchone_results=[(1,)])
    GlossaryRepository(conn).insert_glossary_sense(7, None)
    _, params = conn.executed[0]
    assert params[1] is None


# ── find_sense_by_label ──────────────────────────────────────────────────────


def test_find_sense_by_label_returns_dict(fake_conn):
    conn = fake_conn(fetchone_results=[
        {"sense_id": 20, "version": 1, "status": "approved", "context_label": "mythological"},
    ])
    result = GlossaryRepository(conn).find_sense_by_label(7, "mythological")
    assert result == {"sense_id": 20, "version": 1, "status": "approved", "context_label": "mythological"}


def test_find_sense_by_label_returns_none_when_missing(fake_conn):
    conn = fake_conn(fetchone_results=[None])
    assert GlossaryRepository(conn).find_sense_by_label(7, "mythological") is None


def test_find_sense_by_label_null_uses_is_null_clause(fake_conn):
    """NULL context_label must use IS NULL, not = NULL."""
    conn = fake_conn(fetchone_results=[None])
    GlossaryRepository(conn).find_sense_by_label(7, None)
    sql, params = conn.executed[0]
    assert "IS NULL" in sql
    assert params == (7,)


def test_find_sense_by_label_non_null_uses_equality(fake_conn):
    conn = fake_conn(fetchone_results=[None])
    GlossaryRepository(conn).find_sense_by_label(7, "mythological")
    sql, params = conn.executed[0]
    assert "context_label = %s" in sql
    assert params == (7, "mythological")


# ── get_sk_rendering_content ─────────────────────────────────────────────────


def test_get_sk_rendering_content_returns_content(fake_conn):
    conn = fake_conn(fetchone_results=[("Kirke",)])
    assert GlossaryRepository(conn).get_sk_rendering_content(20) == "Kirke"


def test_get_sk_rendering_content_returns_none_when_absent(fake_conn):
    conn = fake_conn(fetchone_results=[None])
    assert GlossaryRepository(conn).get_sk_rendering_content(20) is None


def test_get_sk_rendering_content_filters_sk_lang(fake_conn):
    conn = fake_conn(fetchone_results=[None])
    GlossaryRepository(conn).get_sk_rendering_content(20)
    sql, params = conn.executed[0]
    assert "lang = 'sk'" in sql
    assert params == (20,)
