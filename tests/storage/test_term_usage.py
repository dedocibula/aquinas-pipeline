"""Unit tests for TermUsageRepository.write_term_usage."""

from __future__ import annotations

from types import SimpleNamespace

from storage.repositories import TermUsageRepository


def _res(sense_id=42, version=1, method="krystal_single", confidence="high", signals=None):
    return SimpleNamespace(
        sense=SimpleNamespace(sense_id=sense_id, version=version),
        method=method,
        confidence=confidence,
        signals=signals,
    )


def test_write_term_usage_empty_returns_zero_no_sql(fake_conn):
    conn = fake_conn()
    assert TermUsageRepository(conn).write_term_usage(1, []) == 0
    assert conn.executed == []


def test_write_term_usage_deletes_guessed_then_inserts(fake_conn):
    conn = fake_conn()
    n = TermUsageRepository(conn).write_term_usage(1, [_res(42), _res(43)])
    assert n == 2
    # first statement wipes only guessed rows for the segment
    first_sql, first_params = conn.executed[0]
    assert "DELETE FROM term_usage" in first_sql and "status = 'guessed'" in first_sql
    assert first_params == (1,)
    # then one INSERT per resolution
    inserts = [e for e in conn.executed if "INSERT INTO term_usage" in e[0]]
    assert len(inserts) == 2
    assert inserts[0][1][:3] == (1, 42, 1)


def test_write_term_usage_skips_insert_over_tombstone(fake_conn):
    """D10: the re-INSERT must not create a 'guessed' duplicate over a rejected
    or confirmed row for the same (segment_id, sense_id) — guarded via NOT EXISTS."""
    conn = fake_conn()
    TermUsageRepository(conn).write_term_usage(1, [_res(42)])
    insert_sql, insert_params = [
        e for e in conn.executed if "INSERT INTO term_usage" in e[0]
    ][0]
    assert "NOT EXISTS" in insert_sql
    assert "status IN ('confirmed', 'rejected')" in insert_sql
    # trailing params are the NOT EXISTS guard's segment_id, sense_id
    assert insert_params[-2:] == (1, 42)


def test_write_term_usage_serializes_signals(fake_conn):
    conn = fake_conn()
    TermUsageRepository(conn).write_term_usage(1, [_res(signals={"votes": 3})])
    insert = [e for e in conn.executed if "INSERT INTO term_usage" in e[0]][0]
    # signals param is JSON-serialized (6th bound value; trailing params are the
    # NOT EXISTS guard's segment_id/sense_id)
    assert insert[1][5] == '{"votes": 3}'


# ── any_usage_for_senses / segments_for_senses (Stage 6) ────────────────────


def test_any_usage_for_senses_empty_list_short_circuits(fake_conn):
    conn = fake_conn()
    assert TermUsageRepository(conn).any_usage_for_senses([]) is False
    assert conn.executed == []


def test_any_usage_for_senses_true_when_row_exists(fake_conn):
    conn = fake_conn(fetchone_results=[(1,)])
    assert TermUsageRepository(conn).any_usage_for_senses([42]) is True
    sql, params = conn.executed[-1]
    assert "FROM term_usage" in sql
    assert params == ([42],)


def test_any_usage_for_senses_false_when_none(fake_conn):
    conn = fake_conn(fetchone_results=[None])
    assert TermUsageRepository(conn).any_usage_for_senses([42]) is False


def test_segments_for_senses_empty_list_short_circuits(fake_conn):
    conn = fake_conn()
    assert TermUsageRepository(conn).segments_for_senses([]) == {}
    assert conn.executed == []


def test_segments_for_senses_groups_by_sense(fake_conn):
    conn = fake_conn(fetchall_rows=[(42, 1), (42, 2), (43, 3)])
    result = TermUsageRepository(conn).segments_for_senses([42, 43])
    assert result == {42: [1, 2], 43: [3]}
    sql, params = conn.executed[-1]
    assert "status <> 'rejected'" in sql
    assert params == ([42, 43],)
