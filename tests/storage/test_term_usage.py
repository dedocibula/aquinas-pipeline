"""Unit tests for TermUsageRepository.write_term_usage."""

from __future__ import annotations

from types import SimpleNamespace

from storage.repositories import TermUsageRepository


def _res(sense_id=42, version=1, method="krystal_single", confidence="high", signals=None):
    return SimpleNamespace(
        sense={"sense_id": sense_id, "version": version},
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


def test_write_term_usage_serializes_signals(fake_conn):
    conn = fake_conn()
    TermUsageRepository(conn).write_term_usage(1, [_res(signals={"votes": 3})])
    insert = [e for e in conn.executed if "INSERT INTO term_usage" in e[0]][0]
    # signals param is JSON-serialized
    assert insert[1][-1] == '{"votes": 3}'
