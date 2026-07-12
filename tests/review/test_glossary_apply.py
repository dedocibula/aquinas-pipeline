"""Tests for src/review/glossary_apply.py — the five proposal-kind apply functions.

Pure logic against fake_conn, no real DB. FakeConn shares one cursor across
cursor() calls, so fetchone_results/fetchall_rows are consumed in exactly the
order the real code threads multiple repository calls over one connection.
"""

from __future__ import annotations

import pytest

from review.glossary_apply import (
    apply_add_term,
    apply_remove_here,
    apply_rendering_change,
    apply_retire_sense,
    apply_sense_here,
)

# ── apply_rendering_change ──────────────────────────────────────────────────────


def test_apply_rendering_change_empty_raises_no_db_call(fake_conn):
    conn = fake_conn()
    with pytest.raises(ValueError):
        apply_rendering_change(conn, 42, "   ")
    assert conn.executed == []


def test_apply_rendering_change_no_change_no_bump(fake_conn):
    conn = fake_conn(fetchone_results=[("rozum",)])
    result = apply_rendering_change(conn, 42, "rozum")
    assert result == {
        "changed": False,
        "bumped": False,
        "old_sk": "rozum",
        "new_sk": "rozum",
        "new_version": None,
    }
    # only the read — no write, no bump (D3)
    assert len(conn.executed) == 1


def test_apply_rendering_change_writes_and_bumps(fake_conn):
    conn = fake_conn(fetchone_results=[("rozum",), (5,), (2,)])
    result = apply_rendering_change(conn, 42, "úsudok")
    assert result == {
        "changed": True,
        "bumped": True,
        "old_sk": "rozum",
        "new_sk": "úsudok",
        "new_version": 2,
    }
    assert any("sense_rendering" in e[0] for e in conn.executed)
    assert any("version = version + 1" in e[0] for e in conn.executed)


# ── apply_sense_here ─────────────────────────────────────────────────────────────


def test_apply_sense_here_different_terms_raises(fake_conn):
    conn = fake_conn(fetchone_results=[(7,), (8,)])
    with pytest.raises(ValueError):
        apply_sense_here(conn, segment_id=501, from_sense_id=10, to_sense_id=20)


def test_apply_sense_here_missing_sense_raises(fake_conn):
    conn = fake_conn(fetchone_results=[(7,), None])
    with pytest.raises(ValueError):
        apply_sense_here(conn, segment_id=501, from_sense_id=10, to_sense_id=999)


def test_apply_sense_here_retired_target_raises(fake_conn):
    conn = fake_conn(fetchone_results=[(7,), (7,), (20, 3, "retired")])
    with pytest.raises(ValueError):
        apply_sense_here(conn, segment_id=501, from_sense_id=10, to_sense_id=20)


def test_apply_sense_here_stale_update_raises(fake_conn):
    """No matching term_usage row (rowcount 0) — proposal is stale, must not
    silently report success or reset the segment."""
    conn = fake_conn(fetchone_results=[(7,), (7,), (20, 3, "approved")])
    conn._cursor.rowcount = 0
    with pytest.raises(ValueError):
        apply_sense_here(conn, segment_id=501, from_sense_id=10, to_sense_id=20)
    assert not any("translation_status" in e[0] for e in conn.executed)


def test_apply_sense_here_confirms_and_resets_pending(fake_conn):
    conn = fake_conn(
        fetchone_results=[(7,), (7,), (20, 3, "approved")],
        fetchall_rows=[],  # no human-edited segments
    )
    conn._cursor.rowcount = 1
    result = apply_sense_here(conn, segment_id=501, from_sense_id=10, to_sense_id=20)
    assert result == {
        "confirmed": True,
        "target_sense_approved": False,
        "segment_reset": "pending",
    }
    update = [e for e in conn.executed if "UPDATE term_usage" in e[0]][0]
    assert update[1] == (20, 3, 501, 10)  # to_sense_id, current version, segment, from_sense_id
    assert not any("status = %s WHERE sense_id" in e[0] for e in conn.executed)


def test_apply_sense_here_approves_proposed_target(fake_conn):
    conn = fake_conn(
        fetchone_results=[(7,), (7,), (20, 1, "proposed")],
        fetchall_rows=[],
    )
    conn._cursor.rowcount = 1
    result = apply_sense_here(conn, segment_id=501, from_sense_id=10, to_sense_id=20)
    assert result["target_sense_approved"] is True
    status_update = [
        e for e in conn.executed
        if "UPDATE glossary_sense SET status" in e[0]
    ][0]
    assert status_update[1] == ("approved", 20)


def test_apply_sense_here_human_edited_segment_flagged_needs_human(fake_conn):
    conn = fake_conn(
        fetchone_results=[(7,), (7,), (20, 3, "approved")],
        fetchall_rows=[(501,)],  # segment 501 is human-edited
    )
    conn._cursor.rowcount = 1
    result = apply_sense_here(conn, segment_id=501, from_sense_id=10, to_sense_id=20)
    assert result["segment_reset"] == "needs_human"
    assert not any("translation_status = 'pending'" in e[0] for e in conn.executed)


# ── apply_remove_here ────────────────────────────────────────────────────────────


def test_apply_remove_here_tombstones_and_resets(fake_conn):
    conn = fake_conn(fetchall_rows=[])
    conn._cursor.rowcount = 1
    result = apply_remove_here(conn, segment_id=501, sense_id=10)
    assert result == {"rejected": True, "segment_reset": "pending"}
    tombstone = [e for e in conn.executed if "status = 'rejected'" in e[0]][0]
    assert tombstone[1] == (501, 10)
    # no version bump (D3)
    assert not any("version = version + 1" in e[0] for e in conn.executed)


def test_apply_remove_here_human_edited_needs_human(fake_conn):
    conn = fake_conn(fetchall_rows=[(501,)])
    conn._cursor.rowcount = 1
    result = apply_remove_here(conn, segment_id=501, sense_id=10)
    assert result["segment_reset"] == "needs_human"


def test_apply_remove_here_stale_update_raises(fake_conn):
    """No matching term_usage row (rowcount 0) — proposal is stale, must not
    silently report success or reset the segment."""
    conn = fake_conn(fetchall_rows=[])
    conn._cursor.rowcount = 0
    with pytest.raises(ValueError):
        apply_remove_here(conn, segment_id=501, sense_id=10)
    assert not any("translation_status" in e[0] for e in conn.executed)


# ── apply_retire_sense ───────────────────────────────────────────────────────────


def test_apply_retire_sense_always_bumps(fake_conn):
    conn = fake_conn(fetchone_results=[(4,)])
    result = apply_retire_sense(conn, sense_id=10)
    assert result == {"retired": True, "new_version": 4}
    status_update = [
        e for e in conn.executed if "UPDATE glossary_sense SET status" in e[0]
    ][0]
    assert status_update[1] == ("retired", 10)
    assert any("version = version + 1" in e[0] for e in conn.executed)


# ── apply_add_term ───────────────────────────────────────────────────────────────


def test_apply_add_term_existing_lemma_raises(fake_conn):
    conn = fake_conn(fetchone_results=[(3,)])  # find_term_by_lemma hits
    with pytest.raises(ValueError, match="term_exists"):
        apply_add_term(conn, "ratio", "rozum", note=None)


def test_apply_add_term_creates_term_sense_and_rendering(fake_conn):
    conn = fake_conn(fetchone_results=[None, (7,), (20,), (5,)])
    # find_term_by_lemma -> None, insert_glossary_term -> 7, insert_glossary_sense -> 20,
    # source_id("human") -> 5
    result = apply_add_term(conn, "novus_terminus", "nový termín", note="chýba v glosári")
    assert result == {"term_id": 7, "sense_id": 20, "existed": False}
    assert any("INSERT INTO glossary_term" in e[0] for e in conn.executed)
    assert any("INSERT INTO glossary_sense" in e[0] for e in conn.executed)
    assert any("sense_rendering" in e[0] for e in conn.executed)
