"""
Tests for src/ingest/parser_latin.py — pure parsing logic only.
No DB, no live files.
"""

from __future__ import annotations

import pytest

from ingest.parser_latin import (
    ParsedElement,
    _article_locator,
    _check_article,
    _parse_title_full,
    _question_locator,
)

# ── _parse_title_full ─────────────────────────────────────────────────────────

class TestParseTitleFull:
    def test_arg_prima_pars(self):
        locator, etype = _parse_title_full("I q. 3 a. 1 arg. 1")
        assert locator == "I.q3.a1.arg1"
        assert etype == "arg"

    def test_sed_contra(self):
        locator, etype = _parse_title_full("I q. 3 a. 1 s. c.")
        assert locator == "I.q3.a1.sed_contra"
        assert etype == "sed_contra"

    def test_respondeo(self):
        locator, etype = _parse_title_full("I q. 3 a. 1 co.")
        assert locator == "I.q3.a1.respondeo"
        assert etype == "respondeo"

    def test_reply_numbered(self):
        locator, etype = _parse_title_full("I q. 3 a. 1 ad 2")
        assert locator == "I.q3.a1.reply2"
        assert etype == "reply"

    def test_reply_combined_ad_arg(self):
        locator, etype = _parse_title_full("I q. 1 a. 4 ad arg.")
        assert locator == "I.q1.a4.reply0"
        assert etype == "reply"

    def test_preamble(self):
        locator, etype = _parse_title_full("I q. 3 pr.")
        assert locator == "I.q3.preamble"
        assert etype == "preamble"

    def test_prima_secundae(self):
        locator, etype = _parse_title_full("I-II q. 5 a. 1 arg. 1")
        assert locator == "I_II.q5.a1.arg1"
        assert etype == "arg"

    def test_secunda_secundae(self):
        locator, etype = _parse_title_full("II-II q. 23 a. 1 s. c.")
        assert locator == "II_II.q23.a1.sed_contra"
        assert etype == "sed_contra"

    def test_tertia_pars(self):
        locator, etype = _parse_title_full("III q. 75 a. 4 co.")
        assert locator == "III.q75.a4.respondeo"
        assert etype == "respondeo"

    def test_multi_digit_question(self):
        locator, etype = _parse_title_full("I-II q. 94 a. 2 arg. 1")
        assert locator == "I_II.q94.a2.arg1"
        assert etype == "arg"

    def test_multi_digit_reply(self):
        locator, etype = _parse_title_full("II-II q. 64 a. 7 ad 3")
        assert locator == "II_II.q64.a7.reply3"
        assert etype == "reply"

    def test_unrecognised_returns_none(self):
        assert _parse_title_full("Summa Theologiae pr.") is None

    def test_no_pars_returns_none(self):
        assert _parse_title_full("q. 3 a. 1 arg. 1") is None

    def test_leading_whitespace_ignored(self):
        result = _parse_title_full("  I q. 1 a. 1 co.  ")
        assert result is not None
        assert result[0] == "I.q1.a1.respondeo"

    def test_ltree_no_hyphens_in_locator(self):
        # ltree labels must not contain hyphens
        locator, _ = _parse_title_full("I-II q. 5 a. 1 co.")
        assert "-" not in locator

    def test_arg_high_number(self):
        locator, etype = _parse_title_full("I q. 1 a. 1 arg. 12")
        assert locator == "I.q1.a1.arg12"
        assert etype == "arg"


# ── _article_locator ──────────────────────────────────────────────────────────

class TestArticleLocator:
    def test_arg(self):
        assert _article_locator("I q. 3 a. 1 arg. 1") == "I.q3.a1"

    def test_sed_contra(self):
        assert _article_locator("I q. 3 a. 1 s. c.") == "I.q3.a1"

    def test_respondeo(self):
        assert _article_locator("I q. 3 a. 1 co.") == "I.q3.a1"

    def test_reply(self):
        assert _article_locator("I q. 13 a. 5 ad 2") == "I.q13.a5"

    def test_preamble_returns_none(self):
        assert _article_locator("I q. 3 pr.") is None

    def test_unrecognised_returns_none(self):
        assert _article_locator("garbage") is None

    def test_prima_secundae(self):
        assert _article_locator("I-II q. 94 a. 2 co.") == "I_II.q94.a2"


# ── _question_locator ─────────────────────────────────────────────────────────

class TestQuestionLocator:
    def test_prima_pars(self):
        assert _question_locator("I q. 3 a. 1 arg. 1") == "I.q3"

    def test_preamble(self):
        assert _question_locator("I q. 3 pr.") == "I.q3"

    def test_i_ii(self):
        assert _question_locator("I-II q. 5 a. 1 co.") == "I_II.q5"

    def test_ii_ii(self):
        assert _question_locator("II-II q. 23 a. 1 s. c.") == "II_II.q23"

    def test_iii(self):
        assert _question_locator("III q. 75 a. 4 ad 1") == "III.q75"

    def test_garbage_returns_none(self):
        assert _question_locator("random text") is None


# ── _check_article ────────────────────────────────────────────────────────────

def _elem(etype: str, locator: str = "I.q3.a1.x") -> ParsedElement:
    return ParsedElement(locator, etype, "text", None)


class TestCheckArticle:
    def test_complete_article_passes(self):
        elements = [
            _elem("arg"),
            _elem("sed_contra"),
            _elem("respondeo"),
            _elem("reply"),
        ]
        _check_article("I.q3.a1", elements)  # no exception

    def test_missing_sed_contra_raises(self):
        elements = [_elem("arg"), _elem("respondeo"), _elem("reply")]
        with pytest.raises(RuntimeError, match="I.q3.a1"):
            _check_article("I.q3.a1", elements)
        with pytest.raises(RuntimeError, match="sed_contra"):
            _check_article("I.q3.a1", elements)

    def test_missing_respondeo_raises(self):
        elements = [_elem("arg"), _elem("sed_contra"), _elem("reply")]
        with pytest.raises(RuntimeError, match="respondeo"):
            _check_article("I.q3.a1", elements)

    def test_missing_arg_raises(self):
        elements = [_elem("sed_contra"), _elem("respondeo"), _elem("reply")]
        with pytest.raises(RuntimeError, match="arg"):
            _check_article("I.q3.a1", elements)

    def test_missing_reply_raises(self):
        elements = [_elem("arg"), _elem("sed_contra"), _elem("respondeo")]
        with pytest.raises(RuntimeError, match="reply"):
            _check_article("I.q3.a1", elements)

    def test_preamble_not_required(self):
        # preamble is not a required structural element
        elements = [
            _elem("preamble"),
            _elem("arg"),
            _elem("sed_contra"),
            _elem("respondeo"),
            _elem("reply"),
        ]
        _check_article("I.q3.a1", elements)  # no exception
