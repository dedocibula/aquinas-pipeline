"""
Tests for src/ingest/parser_bahounek.py — pure coordinate parsing logic.
No DB, no live files.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup

from ingest.parser_bahounek import (
    OverlayElement,
    _extract_question_titles,
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


class TestInsertBahouněkTexts:
    def test_gap_log_provided_writes_gap_and_skips(self, fake_conn):
        """When gap_log is given and segment not found, writes [GAP] line and skips."""
        conn = fake_conn(fetchone_results=[])  # lookup returns None → not found
        elem = OverlayElement(locator="I.q99.a1.respondeo", text="Odpověď.")
        gap_log = io.StringIO()

        count = insert_bahounek_texts(conn, [elem], src_id=1, gap_log=gap_log)

        assert count == 0
        gap_log.seek(0)
        line = gap_log.read()
        assert "[GAP]" in line
        assert "I.q99.a1.respondeo" in line
        assert "no_segment_match" in line

    def test_gap_log_provided_inserts_when_found(self, fake_conn):
        """When gap_log is given but segment IS found, the row is inserted normally."""
        conn = fake_conn(fetchone_results=[(42,)])  # lookup returns a segment_id
        elem = OverlayElement(locator="I.q3.a1.respondeo", text="Odpověď.")
        gap_log = io.StringIO()

        count = insert_bahounek_texts(conn, [elem], src_id=1, gap_log=gap_log)

        assert count == 1
        gap_log.seek(0)
        assert gap_log.read() == ""  # nothing written for a successful insert

    def test_no_gap_log_raises_on_missing_segment(self, fake_conn):
        """Without gap_log, a missing segment must raise RuntimeError (fail-loudly)."""
        conn = fake_conn(fetchone_results=[])
        elem = OverlayElement(locator="I.q99.a1.respondeo", text="Odpověď.")

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


class TestExtractQuestionTitles:
    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    def test_extracts_single_title(self):
        html = "<p><span>1. CO JE POSVÁTNÁ NAUKA<br/>Předmluva</span></p>"
        results = _extract_question_titles(self._soup(html), "I")
        assert len(results) == 1
        assert results[0].locator == "I.q1"
        assert results[0].text == "CO JE POSVÁTNÁ NAUKA"

    def test_extracts_multiple_titles(self):
        html = """
        <p><span>1. PRVNÍ OTÁZKA<br/>Předmluva</span></p>
        <p><span>2. DRUHÁ OTÁZKA<br/>Předmluva</span></p>
        """
        results = _extract_question_titles(self._soup(html), "I")
        assert len(results) == 2
        assert results[0].locator == "I.q1"
        assert results[0].text == "PRVNÍ OTÁZKA"
        assert results[1].locator == "I.q2"
        assert results[1].text == "DRUHÁ OTÁZKA"

    def test_ignores_span_without_br(self):
        html = "<p><span>1. SOME TITLE</span></p>"
        assert _extract_question_titles(self._soup(html), "I") == []

    def test_ignores_span_not_matching_number_pattern(self):
        html = "<p><span>Not a title<br/>something</span></p>"
        assert _extract_question_titles(self._soup(html), "I") == []

    def test_uses_pars_ltree_prefix(self):
        html = "<p><span>1. OTÁZKA<br/>Předmluva</span></p>"
        results = _extract_question_titles(self._soup(html), "I_II")
        assert results[0].locator == "I_II.q1"

    def test_does_not_match_coordinate_line(self):
        # Coordinate tags like "I ot. 1 čl. 1 arg. 1" should not produce a title
        html = "<p><span>I ot. 1 čl. 1 arg. 1<br/>some text</span></p>"
        assert _extract_question_titles(self._soup(html), "I") == []
