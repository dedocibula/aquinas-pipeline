"""
Tests for src/ingest/parser_bahounek.py — pure coordinate parsing logic.
No DB, no live files.
"""

from __future__ import annotations

from ingest.parser_bahounek import _parse_coord


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
