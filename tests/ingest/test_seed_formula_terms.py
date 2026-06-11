"""Tests for seed_formula_terms — DB-free using FakeConn/FakeCursor."""

from __future__ import annotations

from ingest.seed_formula_terms import (
    _ELEMENT_TO_FORMULA,
    STRUCTURAL_FORMULAS,
    _get_formula_sense,
    backfill_term_usage,
    promote_to_multiword,
    write_la_surface,
)

# ── Fake DB helpers ────────────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, rows=None, rowcount: int = 1):
        self._rows = rows or []
        self._rowcount = rowcount
        self.rowcount = 0
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params=None):
        self.executed.append((sql, params or ()))
        self.rowcount = self._rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeConn:
    def __init__(self, rows=None, rowcount: int = 1):
        self._rows = rows or []
        self._rowcount = rowcount

    def cursor(self, cursor_factory=None):
        return FakeCursor(self._rows, rowcount=self._rowcount)


# ── STRUCTURAL_FORMULAS schema ────────────────────────────────────────────────


def test_structural_formulas_has_sed_contra():
    assert "sed_contra" in STRUCTURAL_FORMULAS


def test_structural_formulas_has_respondeo():
    assert "respondeo" in STRUCTURAL_FORMULAS


def test_structural_formulas_sed_contra_surface_matches_latin():
    """la_surface should start with the actual Latin words, not the slug."""
    assert STRUCTURAL_FORMULAS["sed_contra"].startswith("Sed contra")
    assert "_" not in STRUCTURAL_FORMULAS["sed_contra"]


def test_structural_formulas_respondeo_surface_matches_latin():
    assert STRUCTURAL_FORMULAS["respondeo"].startswith("Respondeo")
    assert "_" not in STRUCTURAL_FORMULAS["respondeo"]


def test_element_to_formula_covers_structural_formulas():
    """Every key in STRUCTURAL_FORMULAS has a corresponding element_type mapping."""
    for slug in STRUCTURAL_FORMULAS:
        assert slug in _ELEMENT_TO_FORMULA, f"No element_type mapping for '{slug}'"


def test_element_to_formula_sed_contra_maps_to_sed_contra():
    assert _ELEMENT_TO_FORMULA["sed_contra"] == "sed_contra"


def test_element_to_formula_respondeo_maps_to_respondeo():
    assert _ELEMENT_TO_FORMULA["respondeo"] == "respondeo"


# ── _get_formula_sense ────────────────────────────────────────────────────────


def test_get_formula_sense_returns_dict_when_row_found():
    conn = FakeConn(rows=[(42, 99, 3)])
    result = _get_formula_sense(conn, "sed_contra")
    assert result == {"term_id": 42, "sense_id": 99, "version": 3}


def test_get_formula_sense_returns_none_when_not_found():
    conn = FakeConn(rows=[])
    result = _get_formula_sense(conn, "nonexistent")
    assert result is None


def test_get_formula_sense_queries_approved_formula():
    # Covered by test_get_formula_sense_filters_by_approved_status below via _TrackingConn.
    pass

class _TrackingConn:
    def __init__(self, rows=None, rowcount: int = 1):
        self._rows = rows or []
        self._rowcount = rowcount
        self.last_cursor: FakeCursor | None = None

    def cursor(self, cursor_factory=None):
        self.last_cursor = FakeCursor(self._rows, rowcount=self._rowcount)
        return self.last_cursor

def test_get_formula_sense_filters_by_approved_status():
    conn = _TrackingConn(rows=[(1, 2, 1)])
    _get_formula_sense(conn, "respondeo")
    sql = conn.last_cursor.executed[0][0]
    assert "approved" in sql
    assert "formula" in sql


# ── promote_to_multiword ──────────────────────────────────────────────────────


def test_promote_to_multiword_returns_true_when_updated():
    conn = FakeConn(rowcount=1)
    assert promote_to_multiword(conn, "sed_contra") is True


def test_promote_to_multiword_returns_false_when_already_multiword():
    conn = FakeConn(rowcount=0)
    assert promote_to_multiword(conn, "sed_contra") is False


def test_promote_to_multiword_sets_is_multiword_true():
    conn = _TrackingConn()
    promote_to_multiword(conn, "respondeo")
    sql = conn.last_cursor.executed[0][0]
    assert "is_multiword = true" in sql
    assert "formula" in sql


# ── write_la_surface ──────────────────────────────────────────────────────────


def test_write_la_surface_returns_true_on_insert():
    conn = FakeConn(rowcount=1)
    assert write_la_surface(conn, sense_id=10, surface="Sed contra", seed_src_id=5) is True


def test_write_la_surface_returns_false_on_conflict():
    conn = FakeConn(rowcount=0)
    assert write_la_surface(conn, sense_id=10, surface="Sed contra", seed_src_id=5) is False


def test_write_la_surface_inserts_la_lang():
    conn = _TrackingConn()
    write_la_surface(conn, sense_id=10, surface="Respondeo dicendum quod", seed_src_id=5)
    sql, params = conn.last_cursor.executed[0]
    assert "'la'" in sql
    assert params[1] == "Respondeo dicendum quod"


# ── backfill_term_usage ───────────────────────────────────────────────────────


def test_backfill_term_usage_returns_rowcount():
    conn = FakeConn(rowcount=17)
    result = backfill_term_usage(conn, "sed_contra", sense_id=10, version=2)
    assert result == 17


def test_backfill_term_usage_uses_formula_backfill_method():
    conn = _TrackingConn(rowcount=0)
    backfill_term_usage(conn, "respondeo", sense_id=99, version=1)
    sql = conn.last_cursor.executed[0][0]
    assert "formula_backfill" in sql
    assert "auto" in sql


def test_backfill_term_usage_filters_by_element_type():
    conn = _TrackingConn(rowcount=0)
    backfill_term_usage(conn, "sed_contra", sense_id=10, version=1)
    _, params = conn.last_cursor.executed[0]
    assert "sed_contra" in params


def test_backfill_term_usage_on_conflict_do_nothing():
    conn = _TrackingConn(rowcount=0)
    backfill_term_usage(conn, "respondeo", sense_id=5, version=1)
    sql = conn.last_cursor.executed[0][0]
    assert "NOT EXISTS" in sql or "ON CONFLICT" in sql
