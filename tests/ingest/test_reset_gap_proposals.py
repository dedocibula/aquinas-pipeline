"""
Tests for src/ingest/reset_gap_proposals.py — DB-free, using a fake connection
that records the SQL it receives. No live database.
"""

from __future__ import annotations

import re

import ingest.reset_gap_proposals as rgp
from ingest.reset_gap_proposals import (
    find_gap_proposal_state,
    main,
    reset_gap_proposals,
)

# ── Fake connection ───────────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, conn: "FakeConn"):
        self._conn = conn
        self._last_sql = ""
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append(_norm(sql))
        self._last_sql = _norm(sql)
        # rowcount for DELETE statements is driven by the configured map.
        self.rowcount = self._conn.delete_rowcounts_for(self._last_sql)

    def fetchone(self):
        # count(*) SELECTs return a single scalar in a 1-tuple.
        return (self._conn.count_for(self._last_sql),)

    def fetchall(self):
        # The only fetchall() is the gap term_id materialization.
        return [(tid,) for tid in self._conn.gap_term_ids]


class FakeConn:
    def __init__(self, *, gap_term_ids=None, counts=None, delete_rowcounts=None):
        self.executed: list[str] = []
        self.committed = 0
        self.rolled_back = 0
        self.gap_term_ids = gap_term_ids if gap_term_ids is not None else [101, 202]
        # keyword → count for count(*) queries
        self._counts = counts or {
            "term_usage": 7,
            "sense_rendering": 5,
            "glossary_sense": 5,
            "glossary_term": 2,
        }
        self._delete_rowcounts = delete_rowcounts or {
            "term_usage": 7,
            "sense_rendering": 5,
            "glossary_sense": 5,
            "glossary_term": 2,
        }

    def cursor(self, *a, **k):
        return FakeCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass

    # context-manager so it can stand in for get_conn()'s yielded value too
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def count_for(self, sql: str) -> int:
        # Key off the FIRST table named after "count(*) from", not any table that
        # merely appears inside a subquery.
        m = re.match(r"select count\(\*\) from (\w+)", sql)
        if m:
            return self._counts.get(m.group(1), 0)
        return 0

    def delete_rowcounts_for(self, sql: str) -> int:
        m = re.match(r"delete from (\w+)", sql)
        if m:
            return self._delete_rowcounts.get(m.group(1), 0)
        return 0


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


def _deletes(conn: FakeConn) -> list[str]:
    return [s for s in conn.executed if s.startswith("delete from")]


def _delete_table_order(conn: FakeConn) -> list[str]:
    order = []
    for s in _deletes(conn):
        m = re.match(r"delete from (\w+)", s)
        if m:
            order.append(m.group(1))
    return order


# ── find_gap_proposal_state ───────────────────────────────────────────────────


class TestFindGapProposalState:
    def test_returns_all_four_counts(self):
        conn = FakeConn()
        counts = find_gap_proposal_state(conn)
        assert counts == {
            "term_usage": 7,
            "sense_rendering": 5,
            "glossary_sense": 5,
            "glossary_term": 2,
        }

    def test_issues_no_delete(self):
        conn = FakeConn()
        find_gap_proposal_state(conn)
        assert _deletes(conn) == []

    def test_only_select_count_queries(self):
        conn = FakeConn()
        find_gap_proposal_state(conn)
        assert conn.executed, "expected some queries"
        for sql in conn.executed:
            assert sql.startswith("select count(*)"), sql

    def test_does_not_commit(self):
        conn = FakeConn()
        find_gap_proposal_state(conn)
        assert conn.committed == 0


# ── reset_gap_proposals ───────────────────────────────────────────────────────


class TestResetGapProposals:
    def test_delete_order_is_fk_safe(self):
        conn = FakeConn()
        reset_gap_proposals(conn)
        assert _delete_table_order(conn) == [
            "term_usage",
            "sense_rendering",
            "glossary_sense",
            "glossary_term",
        ]

    def test_term_usage_deleted_before_sense_rendering(self):
        conn = FakeConn()
        reset_gap_proposals(conn)
        order = _delete_table_order(conn)
        assert order.index("term_usage") < order.index("sense_rendering")

    def test_sense_rendering_before_glossary_sense(self):
        conn = FakeConn()
        reset_gap_proposals(conn)
        order = _delete_table_order(conn)
        assert order.index("sense_rendering") < order.index("glossary_sense")

    def test_glossary_sense_before_glossary_term(self):
        conn = FakeConn()
        reset_gap_proposals(conn)
        order = _delete_table_order(conn)
        assert order.index("glossary_sense") < order.index("glossary_term")

    def test_returns_deleted_counts(self):
        conn = FakeConn()
        deleted = reset_gap_proposals(conn)
        assert deleted == {
            "term_usage": 7,
            "sense_rendering": 5,
            "glossary_sense": 5,
            "glossary_term": 2,
        }

    def test_commits_once(self):
        conn = FakeConn()
        reset_gap_proposals(conn)
        assert conn.committed == 1

    def test_materializes_gap_term_ids_before_deleting(self):
        # The first statement must read the gap term_ids (a SELECT), not a DELETE.
        conn = FakeConn()
        reset_gap_proposals(conn)
        assert conn.executed[0].startswith("select term_id from glossary_sense")
        assert "bool_and" in conn.executed[0]

    def test_no_op_when_no_gap_terms(self):
        conn = FakeConn(gap_term_ids=[])
        deleted = reset_gap_proposals(conn)
        assert deleted == {
            "term_usage": 0,
            "sense_rendering": 0,
            "glossary_sense": 0,
            "glossary_term": 0,
        }
        assert _deletes(conn) == []
        assert conn.committed == 1


# ── main (CLI) ────────────────────────────────────────────────────────────────


def _patch_get_conn(monkeypatch, conn: FakeConn):
    from contextlib import contextmanager

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(rgp, "get_conn", fake_get_conn)


class TestMainCLI:
    def test_dry_run_default_does_not_delete(self, monkeypatch):
        conn = FakeConn()
        _patch_get_conn(monkeypatch, conn)
        called = {"reset": False}
        monkeypatch.setattr(
            rgp, "reset_gap_proposals",
            lambda c: called.__setitem__("reset", True) or {},
        )
        rc = main([])
        assert rc == 0
        assert called["reset"] is False
        # only the count SELECTs ran
        assert _deletes(conn) == []

    def test_dry_run_still_reads_counts(self, monkeypatch):
        conn = FakeConn()
        _patch_get_conn(monkeypatch, conn)
        monkeypatch.setattr(rgp, "reset_gap_proposals", lambda c: {})
        main([])
        assert any(s.startswith("select count(*)") for s in conn.executed)

    def test_execute_flag_calls_reset(self, monkeypatch):
        conn = FakeConn()
        _patch_get_conn(monkeypatch, conn)
        called = {"reset": False}

        def fake_reset(c):
            called["reset"] = True
            return {
                "term_usage": 7,
                "sense_rendering": 5,
                "glossary_sense": 5,
                "glossary_term": 2,
            }

        monkeypatch.setattr(rgp, "reset_gap_proposals", fake_reset)
        rc = main(["--execute"])
        assert rc == 0
        assert called["reset"] is True

    def test_execute_prints_counts_before_reset(self, monkeypatch, capsys):
        conn = FakeConn()
        _patch_get_conn(monkeypatch, conn)
        order = []
        monkeypatch.setattr(
            rgp, "reset_gap_proposals",
            lambda c: order.append("reset") or {
                "term_usage": 0, "sense_rendering": 0,
                "glossary_sense": 0, "glossary_term": 0,
            },
        )
        # find runs inside main (real), then reset (patched). Capture proves
        # the dry-run counts were emitted before the delete summary.
        main(["--execute"])
        out = capsys.readouterr().out
        assert "WOULD be deleted" in out
        assert "Deleted" in out
        assert out.index("WOULD be deleted") < out.index("Deleted")
        assert order == ["reset"]
