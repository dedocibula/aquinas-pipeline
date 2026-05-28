"""
Tests for src/acquire/latin.py.

All tests use fixtures or inline HTML — no live network calls.
"""

import pytest

from acquire.latin import (
    _article_key,
    _classify,
    count_articles_in_html,
    verify_structural_elements,
    verify_wellformed,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_ARTICLE_HTML = """\
<HTML><HEAD><TITLE>Test</TITLE></HEAD><BODY>
<P TITLE="I q. 1 a. 1 arg. 1">Objection 1.</P>
<P TITLE="I q. 1 a. 1 arg. 2">Objection 2.</P>
<P TITLE="I q. 1 a. 1 s. c.">Sed contra.</P>
<P TITLE="I q. 1 a. 1 co.">Respondeo dicendum.</P>
<P TITLE="I q. 1 a. 1 ad 1">Reply to 1.</P>
<P TITLE="I q. 1 a. 1 ad 2">Reply to 2.</P>
</BODY></HTML>
"""

TWO_ARTICLE_HTML = """\
<HTML><HEAD><TITLE>Test</TITLE></HEAD><BODY>
<P TITLE="I q. 1 pr.">Preamble.</P>
<P TITLE="I q. 1 a. 1 arg. 1">Objection 1.</P>
<P TITLE="I q. 1 a. 1 s. c.">Sed contra.</P>
<P TITLE="I q. 1 a. 1 co.">Respondeo.</P>
<P TITLE="I q. 1 a. 1 ad 1">Reply 1.</P>
<P TITLE="I q. 1 a. 2 arg. 1">Objection 1 art 2.</P>
<P TITLE="I q. 1 a. 2 s. c.">Sed contra art 2.</P>
<P TITLE="I q. 1 a. 2 co.">Respondeo art 2.</P>
<P TITLE="I q. 1 a. 2 ad 1">Reply 1 art 2.</P>
</BODY></HTML>
"""

# "ad arg." variant — valid reply type used by CT for combined-objection articles
AD_ARG_VARIANT_HTML = """\
<HTML><HEAD><TITLE>Test</TITLE></HEAD><BODY>
<P TITLE="I q. 1 a. 4 arg. 1">Objection.</P>
<P TITLE="I q. 1 a. 4 s. c.">Sed contra.</P>
<P TITLE="I q. 1 a. 4 co.">Respondeo.</P>
<P TITLE="I q. 1 a. 4 ad arg.">Reply to combined.</P>
</BODY></HTML>
"""

MULTI_PARS_HTML = """\
<HTML><HEAD><TITLE>Test</TITLE></HEAD><BODY>
<P TITLE="I q. 1 a. 1 arg. 1">P1 obj.</P>
<P TITLE="I q. 1 a. 1 s. c.">P1 sc.</P>
<P TITLE="I q. 1 a. 1 co.">P1 co.</P>
<P TITLE="I q. 1 a. 1 ad 1">P1 reply.</P>
<P TITLE="I-II q. 1 a. 1 arg. 1">P2 obj.</P>
<P TITLE="I-II q. 1 a. 1 s. c.">P2 sc.</P>
<P TITLE="I-II q. 1 a. 1 co.">P2 co.</P>
<P TITLE="I-II q. 1 a. 1 ad 1">P2 reply.</P>
</BODY></HTML>
"""


# ---------------------------------------------------------------------------
# _article_key
# ---------------------------------------------------------------------------

class TestArticleKey:
    def test_standard_format(self):
        assert _article_key("I q. 1 a. 1 arg. 1") == "I q. 1 a. 1"

    def test_sc_format(self):
        assert _article_key("I q. 1 a. 1 s. c.") == "I q. 1 a. 1"

    def test_co_format(self):
        assert _article_key("I q. 1 a. 1 co.") == "I q. 1 a. 1"

    def test_ad_format(self):
        assert _article_key("I q. 1 a. 1 ad 2") == "I q. 1 a. 1"

    def test_ad_arg_variant(self):
        assert _article_key("I q. 1 a. 4 ad arg.") == "I q. 1 a. 4"

    def test_preamble_returns_none(self):
        # Preamble has no "a. N" component
        assert _article_key("I q. 1 pr.") is None

    def test_prima_secundae(self):
        assert _article_key("I-II q. 3 a. 2 arg. 1") == "I-II q. 3 a. 2"

    def test_tertia_pars_double_digit(self):
        assert _article_key("III q. 15 a. 12 co.") == "III q. 15 a. 12"

    def test_none_for_plain_text(self):
        assert _article_key("some random text") is None


# ---------------------------------------------------------------------------
# _classify
# ---------------------------------------------------------------------------

class TestClassify:
    def test_arg(self):
        assert _classify("I q. 1 a. 1 arg. 1") == "arg"

    def test_arg_multi_digit(self):
        assert _classify("II-II q. 10 a. 3 arg. 12") == "arg"

    def test_sed_contra(self):
        assert _classify("I q. 1 a. 1 s. c.") == "sed_contra"

    def test_respondeo(self):
        assert _classify("I q. 1 a. 1 co.") == "respondeo"

    def test_reply_numbered(self):
        assert _classify("I q. 1 a. 1 ad 3") == "reply"

    def test_reply_ad_arg(self):
        assert _classify("I q. 1 a. 4 ad arg.") == "reply"

    def test_preamble_returns_none(self):
        assert _classify("I q. 1 pr.") is None

    def test_unrecognised_returns_none(self):
        assert _classify("Summa theologiae, pr.") is None


# ---------------------------------------------------------------------------
# count_articles_in_html
# ---------------------------------------------------------------------------

class TestCountArticlesInHtml:
    def test_single_article(self):
        articles = count_articles_in_html(MINIMAL_ARTICLE_HTML)
        assert articles == {"I q. 1 a. 1"}

    def test_two_articles(self):
        articles = count_articles_in_html(TWO_ARTICLE_HTML)
        assert articles == {"I q. 1 a. 1", "I q. 1 a. 2"}

    def test_preamble_not_counted(self):
        # The pr. paragraph must not appear as an article
        articles = count_articles_in_html(TWO_ARTICLE_HTML)
        assert not any("pr." in a for a in articles)

    def test_multi_pars(self):
        articles = count_articles_in_html(MULTI_PARS_HTML)
        assert "I q. 1 a. 1" in articles
        assert "I-II q. 1 a. 1" in articles
        assert len(articles) == 2

    def test_empty_html(self):
        articles = count_articles_in_html("<html><body></body></html>")
        assert articles == set()

    def test_ad_arg_variant_counted(self):
        articles = count_articles_in_html(AD_ARG_VARIANT_HTML)
        assert "I q. 1 a. 4" in articles


# ---------------------------------------------------------------------------
# verify_wellformed
# ---------------------------------------------------------------------------

class TestVerifyWellformed:
    def test_valid_html_passes(self):
        verify_wellformed(MINIMAL_ARTICLE_HTML, "test.html")  # must not raise

    def test_empty_body_passes(self):
        verify_wellformed("<html><body></body></html>", "empty.html")

    def test_none_result_raises(self):
        # lxml's HTMLParser returns None only for completely empty input —
        # simulate by patching; the real guard is the None check in the function.
        # We test the empty-string path instead, which lxml handles gracefully.
        # An actually empty bytes input raises a parse error:
        with pytest.raises(RuntimeError, match="empty.html"):
            verify_wellformed("", "empty.html")


# ---------------------------------------------------------------------------
# verify_structural_elements
# ---------------------------------------------------------------------------

class TestVerifyStructuralElements:
    def test_complete_article_passes(self):
        verify_structural_elements(MINIMAL_ARTICLE_HTML, "test.html")

    def test_missing_sed_contra_raises(self):
        html = """\
<HTML><BODY>
<P TITLE="I q. 1 a. 1 arg. 1">Obj.</P>
<P TITLE="I q. 1 a. 1 co.">Co.</P>
<P TITLE="I q. 1 a. 1 ad 1">Ad.</P>
</BODY></HTML>"""
        with pytest.raises(RuntimeError) as exc_info:
            verify_structural_elements(html, "missing_sc.html")
        assert "missing_sc.html" in str(exc_info.value)
        assert "sed_contra" in str(exc_info.value)

    def test_missing_respondeo_raises(self):
        html = """\
<HTML><BODY>
<P TITLE="I q. 1 a. 1 arg. 1">Obj.</P>
<P TITLE="I q. 1 a. 1 s. c.">Sc.</P>
<P TITLE="I q. 1 a. 1 ad 1">Ad.</P>
</BODY></HTML>"""
        with pytest.raises(RuntimeError) as exc_info:
            verify_structural_elements(html, "missing_co.html")
        assert "respondeo" in str(exc_info.value)

    def test_missing_arg_raises(self):
        html = """\
<HTML><BODY>
<P TITLE="I q. 1 a. 1 s. c.">Sc.</P>
<P TITLE="I q. 1 a. 1 co.">Co.</P>
<P TITLE="I q. 1 a. 1 ad 1">Ad.</P>
</BODY></HTML>"""
        with pytest.raises(RuntimeError) as exc_info:
            verify_structural_elements(html, "missing_arg.html")
        assert "arg" in str(exc_info.value)

    def test_missing_reply_raises(self):
        html = """\
<HTML><BODY>
<P TITLE="I q. 1 a. 1 arg. 1">Obj.</P>
<P TITLE="I q. 1 a. 1 s. c.">Sc.</P>
<P TITLE="I q. 1 a. 1 co.">Co.</P>
</BODY></HTML>"""
        with pytest.raises(RuntimeError) as exc_info:
            verify_structural_elements(html, "missing_reply.html")
        assert "reply" in str(exc_info.value)

    def test_ad_arg_variant_counts_as_reply(self):
        # "ad arg." is a valid reply; must satisfy the reply requirement
        verify_structural_elements(AD_ARG_VARIANT_HTML, "ad_arg_variant.html")

    def test_error_includes_filename(self):
        html = "<HTML><BODY><P TITLE='I q. 1 a. 1 co.'>Co.</P></BODY></HTML>"
        with pytest.raises(RuntimeError) as exc_info:
            verify_structural_elements(html, "my_specific_file.html")
        assert "my_specific_file.html" in str(exc_info.value)

    def test_multi_pars_html_passes(self):
        verify_structural_elements(MULTI_PARS_HTML, "multi.html")
