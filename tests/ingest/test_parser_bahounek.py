"""
Tests for src/ingest/parser_bahounek.py — pure coordinate parsing logic.
No DB, no live files.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest

from ingest.parser_bahounek import (
    BahouněkElement,
    _parse_coord,
    insert_bahounek_texts,
    write_bahounek_coverage,
)


class TestParseCoord:
    def test_arg_prima_pars(self):
        assert _parse_coord("I ot. 3 čl. 1 arg. 1") == "I.q3.a1.arg1"

    def test_protiarg_is_sed_contra(self):
        assert _parse_coord("I ot. 3 čl. 1 protiarg.") == "I.q3.a1.sed_contra"

    def test_odp_is_respondeo(self):
        assert _parse_coord("I ot. 3 čl. 1 odp.") == "I.q3.a1.respondeo"

    def test_k_is_reply(self):
        assert _parse_coord("I ot. 3 čl. 1 k 1") == "I.q3.a1.reply1"

    def test_preamble(self):
        assert _parse_coord("I ot. 3 pr.") == "I.q3.preamble"

    def test_prima_secundae(self):
        assert _parse_coord("I-II ot. 5 čl. 1 arg. 1") == "I_II.q5.a1.arg1"

    def test_secunda_secundae(self):
        assert _parse_coord("II-II ot. 23 čl. 1 protiarg.") == "II_II.q23.a1.sed_contra"

    def test_tertia_pars(self):
        assert _parse_coord("III ot. 75 čl. 4 odp.") == "III.q75.a4.respondeo"

    def test_multi_digit_question(self):
        assert _parse_coord("I-II ot. 94 čl. 2 arg. 1") == "I_II.q94.a2.arg1"

    def test_multi_digit_reply(self):
        assert _parse_coord("II-II ot. 64 čl. 7 k 3") == "II_II.q64.a7.reply3"

    def test_no_hyphens_in_ltree(self):
        result = _parse_coord("I-II ot. 5 čl. 1 odp.")
        assert result is not None
        assert "-" not in result

    def test_arg_multi_digit_number(self):
        assert _parse_coord("I ot. 1 čl. 1 arg. 12") == "I.q1.a1.arg12"

    def test_garbage_returns_none(self):
        assert _parse_coord("some random text") is None

    def test_missing_cl_part_returns_none(self):
        assert _parse_coord("I ot. 1 arg. 1") is None

    def test_leading_whitespace_handled(self):
        result = _parse_coord("  I ot. 3 čl. 1 odp.  ")
        assert result == "I.q3.a1.respondeo"

    def test_i_ii_preamble(self):
        assert _parse_coord("I-II ot. 5 pr.") == "I_II.q5.preamble"

    def test_iii_reply(self):
        assert _parse_coord("III ot. 1 čl. 1 k 2") == "III.q1.a1.reply2"


def _make_conn(fetchone_return):
    """Return a mock connection whose cursor().fetchone() returns fetchone_return."""
    cur = MagicMock()
    cur.fetchone.return_value = fetchone_return
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


class TestInsertBahouněkTexts:
    def test_gap_log_provided_writes_gap_and_skips(self):
        """When gap_log is given and segment not found, writes [GAP] line and skips."""
        conn, cur = _make_conn(None)  # fetchone returns None → not found
        elem = BahouněkElement(locator="I.q99.a1.respondeo", czech_text="Odpověď.")
        gap_log = io.StringIO()

        count = insert_bahounek_texts(conn, [elem], src_id=1, gap_log=gap_log)

        assert count == 0
        gap_log.seek(0)
        line = gap_log.read()
        assert "[GAP]" in line
        assert "I.q99.a1.respondeo" in line
        assert "no_segment_match" in line

    def test_gap_log_provided_inserts_when_found(self):
        """When gap_log is given but segment IS found, the row is inserted normally."""
        conn, cur = _make_conn((42,))  # fetchone returns a segment_id
        elem = BahouněkElement(locator="I.q3.a1.respondeo", czech_text="Odpověď.")
        gap_log = io.StringIO()

        count = insert_bahounek_texts(conn, [elem], src_id=1, gap_log=gap_log)

        assert count == 1
        gap_log.seek(0)
        assert gap_log.read() == ""  # nothing written for a successful insert

    def test_no_gap_log_raises_on_missing_segment(self):
        """Without gap_log, a missing segment must raise RuntimeError (fail-loudly)."""
        conn, _cur = _make_conn(None)
        elem = BahouněkElement(locator="I.q99.a1.respondeo", czech_text="Odpověď.")

        with pytest.raises(RuntimeError, match="no matching segment"):
            insert_bahounek_texts(conn, [elem], src_id=1)


class TestWriteBahouněkCoverage:
    def _make_conn(self, with_czech: int, total: int, missing: list[str] | None = None):
        cur = MagicMock()
        cur.fetchone.side_effect = [(with_czech,), (total,)]
        cur.fetchall.return_value = [(loc,) for loc in (missing or [])]
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn

    def test_produces_correct_summary_line(self):
        conn = self._make_conn(50, 100)
        gap_log = io.StringIO()
        write_bahounek_coverage(conn, gap_log)
        gap_log.seek(0)
        content = gap_log.read()
        assert "segments_with_czech=50" in content
        assert "total_body_segments=100" in content
        assert "pct=50.0%" in content

    def test_zero_total_produces_zero_pct(self):
        conn = self._make_conn(0, 0)
        gap_log = io.StringIO()
        write_bahounek_coverage(conn, gap_log)
        gap_log.seek(0)
        assert "pct=0.0%" in gap_log.read()

    def test_writes_missing_czech_lines(self):
        conn = self._make_conn(1, 3, missing=["I.q1.a1.arg1", "I.q1.a1.sed_contra"])
        gap_log = io.StringIO()
        write_bahounek_coverage(conn, gap_log)
        gap_log.seek(0)
        content = gap_log.read()
        assert "MISSING_CZECH: locator=I.q1.a1.arg1" in content
        assert "MISSING_CZECH: locator=I.q1.a1.sed_contra" in content

    def test_no_missing_czech_lines_when_full_coverage(self):
        conn = self._make_conn(100, 100, missing=[])
        gap_log = io.StringIO()
        write_bahounek_coverage(conn, gap_log)
        gap_log.seek(0)
        assert "MISSING_CZECH" not in gap_log.read()
