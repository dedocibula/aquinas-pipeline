"""Unit tests for SegmentRepository — per-segment loads/writes + corpus queries."""

from __future__ import annotations

from storage.models import Segment
from storage.repositories import SegmentRepository


def _seg_row(**overrides) -> dict:
    base = {
        "segment_id": 1,
        "locator_path": "I.q1.a1",
        "element_type": "respondeo",
        "reply_to": None,
        "translation_status": "pending",
        "latin": "Respondeo dicendum quod...",
        "czech": "Odpovídám...",
        "english": "I answer that...",
    }
    base.update(overrides)
    return base


# ── get_segment ────────────────────────────────────────────────────────────────


def test_get_segment_returns_model(fake_conn):
    conn = fake_conn(fetchone_results=[_seg_row()])
    seg = SegmentRepository(conn).get_segment(1)
    assert isinstance(seg, Segment)
    assert seg.translation_status == "pending"
    assert seg.latin.startswith("Respondeo")


def test_get_segment_none_when_missing(fake_conn):
    conn = fake_conn(fetchone_results=[])
    assert SegmentRepository(conn).get_segment(999) is None


def test_load_body_segments_returns_models(fake_conn):
    rows = [_seg_row(segment_id=1), _seg_row(segment_id=2, latin=None)]
    conn = fake_conn(fetchall_rows=rows)
    segs = SegmentRepository(conn).load_body_segments(1)
    assert [s.segment_id for s in segs] == [1, 2]
    assert all(isinstance(s, Segment) for s in segs)


# ── locator lookups (used by the overlay parsers) ───────────────────────────────


def test_get_segment_id_by_locator_returns_id(fake_conn):
    conn = fake_conn(fetchone_results=[(7,)])
    assert SegmentRepository(conn).get_segment_id_by_locator("I.q1.a1") == 7
    sql, params = conn.executed[-1]
    assert "work_id" not in sql
    assert params == ("I.q1.a1",)


def test_get_segment_id_by_locator_none_when_missing(fake_conn):
    conn = fake_conn(fetchone_results=[])
    assert SegmentRepository(conn).get_segment_id_by_locator("I.q9.a9") is None


def test_get_segment_id_by_locator_scopes_to_work(fake_conn):
    conn = fake_conn(fetchone_results=[(3,)])
    assert SegmentRepository(conn).get_segment_id_by_locator("I.q1", work_id=1) == 3
    sql, params = conn.executed[-1]
    assert "work_id = %s" in sql
    assert params == ("I.q1", 1)


def test_get_article_title_locators(fake_conn):
    conn = fake_conn(fetchall_rows=[("I.q1.a1",), ("I.q2.a3",)])
    assert SegmentRepository(conn).get_article_title_locators() == ["I.q1.a1", "I.q2.a3"]
    sql, _ = conn.executed[-1]
    assert "element_type = 'article_title'" in sql


# ── structural writes (Latin segment-graph creation) ────────────────────────────


def test_wipe_article_deletes_in_fk_order(fake_conn):
    conn = fake_conn()
    SegmentRepository(conn).wipe_article("I.q1.a1", 1)
    tables = [sql.split("DELETE FROM ")[1].split(" ")[0] for sql, _ in conn.executed]
    assert tables == ["run_segment", "term_usage", "segment_text", "segment"]
    # subtree match for an article (descendants included)
    assert all("<@" in sql for sql, _ in conn.executed)


def test_wipe_segment_exact_match_in_fk_order(fake_conn):
    conn = fake_conn()
    SegmentRepository(conn).wipe_segment("I.q1.preamble", 1)
    tables = [sql.split("DELETE FROM ")[1].split(" ")[0] for sql, _ in conn.executed]
    assert tables == ["run_segment", "term_usage", "segment_text", "segment"]
    # exact match for a leaf segment (no subtree operator)
    assert all("<@" not in sql for sql, _ in conn.executed)


def test_create_segment_returns_new_id(fake_conn):
    conn = fake_conn(fetchone_results=[(99,)])
    seg_id = SegmentRepository(conn).create_segment(1, "I.q1.a1.arg1", "arg")
    assert seg_id == 99
    sql, params = conn.executed[-1]
    assert "RETURNING segment_id" in sql
    assert params == (1, "I.q1.a1.arg1", "arg")


def test_set_reply_to(fake_conn):
    conn = fake_conn()
    SegmentRepository(conn).set_reply_to(10, 5)
    sql, params = conn.executed[-1]
    assert "UPDATE segment SET reply_to" in sql
    assert params == (5, 10)


def test_body_text_coverage(fake_conn):
    conn = fake_conn(
        fetchone_results=[(50,), (100,)],
        fetchall_rows=[("I.q1.a1.arg1",), ("I.q1.a1.sed_contra",)],
    )
    with_text, total, missing = SegmentRepository(conn).body_text_coverage("cs")
    assert (with_text, total) == (50, 100)
    assert missing == ["I.q1.a1.arg1", "I.q1.a1.sed_contra"]


# ── writes ─────────────────────────────────────────────────────────────────────


def test_write_segment_text_upserts(fake_conn):
    conn = fake_conn()
    SegmentRepository(conn).write_segment_text(1, "sk", 3, "preklad")
    sql, params = conn.executed[-1]
    assert "INSERT INTO segment_text" in sql
    assert params == (1, "sk", "preklad", 3)


def test_update_translation_status(fake_conn):
    conn = fake_conn()
    SegmentRepository(conn).update_translation_status(1, "translated")
    _, params = conn.executed[-1]
    assert params == ("translated", 1)


def test_update_sense_version_used(fake_conn):
    conn = fake_conn()
    SegmentRepository(conn).update_sense_version_used(1, 42, 2)
    sql, params = conn.executed[-1]
    assert "UPDATE term_usage SET sense_version_used" in sql
    assert params == (2, 1, 42)


def test_write_reviewer_notes_wraps_payload_with_iteration(fake_conn):
    conn = fake_conn()
    SegmentRepository(conn).write_reviewer_notes(1, {"raw": "looks good"}, iteration=2)
    sql, params = conn.executed[-1]
    assert "UPDATE segment SET reviewer_notes" in sql
    # The payload is wrapped in psycopg2.extras.Json; iteration is folded in.
    assert hasattr(params[0], "adapted") or hasattr(params[0], "dumps")
    assert params[1] == 1


# ── corpus-wide queries ────────────────────────────────────────────────────────


def test_get_all_article_locators(fake_conn):
    conn = fake_conn(fetchall_rows=[("I.q1.a1",), ("I.q2.a3",)])
    assert SegmentRepository(conn).get_all_article_locators(1) == ["I.q1.a1", "I.q2.a3"]


def test_get_pending_with_filter_passes_segment_list(fake_conn):
    conn = fake_conn(fetchall_rows=[(5,)])
    result = SegmentRepository(conn).get_pending_segment_ids_for_article(
        "I.q1.a1", 1, frozenset({5, 6})
    )
    assert result == [5]
    sql, params = conn.executed[-1]
    assert "segment_id = ANY(%s)" in sql
    assert params[0] == "I.q1.a1" and params[1] == 1 and sorted(params[2]) == [5, 6]


def test_get_pending_without_filter_omits_any(fake_conn):
    conn = fake_conn(fetchall_rows=[])
    SegmentRepository(conn).get_pending_segment_ids_for_article("I.q1.a1", 1)
    sql, params = conn.executed[-1]
    assert "segment_id = ANY(%s)" not in sql
    assert params == ("I.q1.a1", 1)


def test_has_pending_segments_true(fake_conn):
    conn = fake_conn(fetchone_results=[(1,)])
    assert SegmentRepository(conn).has_pending_segments("I.q1.a1") is True


def test_get_stale_segments(fake_conn):
    conn = fake_conn(fetchall_rows=[(3,), (4,)])
    assert SegmentRepository(conn).get_stale_segments(1) == [3, 4]


def test_get_translated_body_segment_ids(fake_conn):
    conn = fake_conn(fetchall_rows=[(11,), (12,)])
    assert SegmentRepository(conn).get_translated_body_segment_ids(1) == [11, 12]
    sql, params = conn.executed[-1]
    assert "translation_status = 'translated'" in sql
    assert "NOT IN ('question_title', 'article_title')" in sql
    assert params == (1,)


def test_get_needs_human_segments_returns_raw_notes(fake_conn):
    conn = fake_conn(
        fetchall_rows=[
            ("I.q1.a1.respondeo", {"iteration": 3, "last_feedback": "Missing term X"}),
            ("I.q2.a1.arg1", None),
        ]
    )
    rows = SegmentRepository(conn).get_needs_human_segments(1)
    assert rows == [
        {"locator_path": "I.q1.a1.respondeo",
         "reviewer_notes": {"iteration": 3, "last_feedback": "Missing term X"}},
        {"locator_path": "I.q2.a1.arg1", "reviewer_notes": None},
    ]
    sql, params = conn.executed[-1]
    assert "translation_status = 'needs_human'" in sql
    assert params == (1,)


def test_get_human_edited_empty_short_circuits(fake_conn):
    conn = fake_conn()
    assert SegmentRepository(conn).get_human_edited_segments([]) == []
    assert conn.executed == []


def test_flag_needs_human_empty_short_circuits(fake_conn):
    conn = fake_conn()
    SegmentRepository(conn).flag_needs_human([], "note")
    assert conn.executed == []


def test_reset_translation_status(fake_conn):
    conn = fake_conn()
    SegmentRepository(conn).reset_translation_status([7, 8])
    sql, params = conn.executed[-1]
    assert "translation_status = 'pending'" in sql
    assert params == ([7, 8],)


def test_translation_status_counts(fake_conn):
    conn = fake_conn(fetchall_rows=[("pending", 40), ("translated", 55), ("needs_human", 5)])
    counts = SegmentRepository(conn).translation_status_counts(work_id=1)
    assert counts == {"pending": 40, "translated": 55, "needs_human": 5}
    sql, params = conn.executed[-1]
    assert "GROUP BY translation_status" in sql
    assert params == (1,)


def test_translation_status_counts_empty(fake_conn):
    conn = fake_conn(fetchall_rows=[])
    assert SegmentRepository(conn).translation_status_counts() == {}
