"""Unit tests for ProposalRepository — glossary_proposal upsert/decide semantics (D5)."""

from __future__ import annotations

from storage.repositories import ProposalRepository


def _kwargs(**overrides) -> dict:
    base = {
        "kind": "rendering",
        "sense_id": 42,
        "proposed_sense_id": None,
        "latin_lemma": "ratio",
        "current_sk": "rozum",
        "proposed_sk": "úsudok",
        "note": "Krystal uses this elsewhere",
        "origin_segment_id": None,
        "proposed_by": "editor@example.com",
    }
    base.update(overrides)
    return base


# ── create_or_update_pending ────────────────────────────────────────────────


def test_create_or_update_pending_inserts_when_no_match(fake_conn):
    conn = fake_conn(fetchone_results=[None, {"proposal_id": 7}])
    result = ProposalRepository(conn).create_or_update_pending(**_kwargs())
    assert result == 7
    sql, _ = conn.executed[-1]
    assert "INSERT INTO glossary_proposal" in sql
    assert "RETURNING proposal_id" in sql


def test_create_or_update_pending_reproposal_updates_in_place(fake_conn):
    """Same editor, same kind, same sense — no origin segment (sense-wide kind)."""
    conn = fake_conn(fetchone_results=[{"proposal_id": 3}])
    result = ProposalRepository(conn).create_or_update_pending(**_kwargs())
    assert result == 3
    sql, params = conn.executed[-1]
    assert "UPDATE glossary_proposal" in sql
    assert "created_at = now()" in sql
    assert params[-1] == 3


def test_create_or_update_pending_different_editor_inserts_new_row(fake_conn):
    """Match lookup keys on proposed_by — a different editor never matches."""
    conn = fake_conn(fetchone_results=[None, {"proposal_id": 9}])
    ProposalRepository(conn).create_or_update_pending(**_kwargs(proposed_by="other@example.com"))
    select_sql, select_params = conn.executed[0]
    assert "proposed_by = %s" in select_sql
    assert select_params[1] == "other@example.com"


def test_create_or_update_pending_per_segment_kind_keyed_by_origin_segment(fake_conn):
    """Two proposals on the same sense, different origin segments -> two independent rows.

    The lookup query includes origin_segment_id, so a proposal for segment 101 must not
    match an existing pending row for segment 202.
    """
    conn = fake_conn(fetchone_results=[None, {"proposal_id": 11}])
    ProposalRepository(conn).create_or_update_pending(
        **_kwargs(kind="sense_here", proposed_sense_id=43, origin_segment_id=202)
    )
    select_sql, select_params = conn.executed[0]
    assert "origin_segment_id IS NOT DISTINCT FROM %s" in select_sql
    assert select_params[-1] == 202


def test_create_or_update_pending_add_term_matches_on_lemma_not_sense(fake_conn):
    conn = fake_conn(fetchone_results=[None, {"proposal_id": 15}])
    ProposalRepository(conn).create_or_update_pending(
        **_kwargs(kind="add_term", sense_id=None, latin_lemma="subiectum", proposed_sk="podmet")
    )
    select_sql, select_params = conn.executed[0]
    assert "kind = 'add_term' OR sense_id = %s" in select_sql
    assert "lower(latin_lemma) = lower(%s)" in select_sql
    assert select_params[3] == "subiectum"


def test_create_or_update_pending_update_preserves_proposal_id(fake_conn):
    conn = fake_conn(fetchone_results=[{"proposal_id": 3}])
    ProposalRepository(conn).create_or_update_pending(**_kwargs(proposed_sk="nový preklad"))
    _, params = conn.executed[-1]
    assert "nový preklad" in params


# ── get / list_pending ──────────────────────────────────────────────────────


def test_get_returns_dict(fake_conn):
    conn = fake_conn(fetchone_results=[{"proposal_id": 5, "kind": "rendering"}])
    assert ProposalRepository(conn).get(5) == {"proposal_id": 5, "kind": "rendering"}


def test_get_returns_none_when_missing(fake_conn):
    conn = fake_conn(fetchone_results=[None])
    assert ProposalRepository(conn).get(999) is None


def test_list_pending_orders_oldest_first(fake_conn):
    conn = fake_conn(fetchall_rows=[{"proposal_id": 1}, {"proposal_id": 2}])
    result = ProposalRepository(conn).list_pending()
    assert result == [{"proposal_id": 1}, {"proposal_id": 2}]
    sql, _ = conn.executed[-1]
    assert "status = 'pending'" in sql
    assert "ORDER BY created_at ASC" in sql


# ── pending_by_sense ─────────────────────────────────────────────────────────


def test_pending_by_sense_returns_counts(fake_conn):
    conn = fake_conn(fetchall_rows=[(42, 2), (43, 1)])
    assert ProposalRepository(conn).pending_by_sense([42, 43]) == {42: 2, 43: 1}


def test_pending_by_sense_empty_list_short_circuits(fake_conn):
    conn = fake_conn()
    assert ProposalRepository(conn).pending_by_sense([]) == {}
    assert conn.executed == []


# ── decide ───────────────────────────────────────────────────────────────────


def test_decide_pending_row_returns_true(fake_conn):
    conn = fake_conn()
    conn._cursor.rowcount = 1
    result = ProposalRepository(conn).decide(5, "approved", "admin@example.com", "looks good")
    assert result is True
    sql, params = conn.executed[-1]
    assert "status = 'pending'" in sql
    assert params == ("approved", "admin@example.com", "looks good", 5)


def test_decide_non_pending_row_returns_false(fake_conn):
    conn = fake_conn()
    conn._cursor.rowcount = 0
    assert ProposalRepository(conn).decide(5, "rejected", "admin@example.com") is False


# ── supersede_sense_wide_siblings ───────────────────────────────────────────


def test_supersede_sense_wide_siblings_targets_rendering_and_retire_only(fake_conn):
    conn = fake_conn()
    conn._cursor.rowcount = 2
    result = ProposalRepository(conn).supersede_sense_wide_siblings(42, 7, "admin@example.com")
    assert result == 2
    sql, params = conn.executed[-1]
    assert "kind IN ('rendering', 'retire_sense')" in sql
    assert "proposal_id != %s" in sql
    assert params == ("admin@example.com", 42, 7)


def test_supersede_sense_wide_siblings_excludes_per_segment_kinds_from_sql(fake_conn):
    """sense_here / remove_here must not appear in the superseded-kind filter."""
    conn = fake_conn()
    conn._cursor.rowcount = 0
    ProposalRepository(conn).supersede_sense_wide_siblings(42, 7, "admin@example.com")
    sql, _ = conn.executed[-1]
    assert "sense_here" not in sql
    assert "remove_here" not in sql
