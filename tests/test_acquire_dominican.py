"""
Tests for src/acquire/dominican.py.

No live network calls: all HTML is supplied as fixture strings.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from acquire.dominican import (
    BODY_CLASS,
    PARS_CODES,
    _assert_article_page,
    all_codes,
    verify_coverage,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_article_html(code: int, n_articles: int = 3) -> str:
    """Build a minimal but structurally valid newadvent.org article page."""
    articles = "\n".join(
        f'<h2 id="article{i}">Article {i}. Some question text?</h2>'
        f"<p>Objection 1. Blah.</p>"
        f"<p>On the contrary. Blah.</p>"
        f"<p>I answer that. Blah.</p>"
        for i in range(1, n_articles + 1)
    )
    return f"""<!DOCTYPE html>
<html>
<head><title>SUMMA THEOLOGIAE: Something (Pars, Q. {code % 1000})</title></head>
<body id="{code}.htm" class="{BODY_CLASS}">
  <div id="springfield2">
    <h1>Question {code % 1000}.</h1>
    {articles}
  </div>
</body>
</html>"""


def _make_home_html() -> str:
    """Simulate newadvent.org home page (returned when a code doesn't exist)."""
    return """<!DOCTYPE html>
<html>
<head><title>NEW ADVENT: Home</title></head>
<body id="home">
  <div id="capitalcity">Welcome to New Advent.</div>
</body>
</html>"""


def _make_no_body_html() -> str:
    return "<html><head><title>broken</title></head></html>"


def _make_wrong_class_html(code: int) -> str:
    return f"""<html>
<body id="{code}.htm" class="other-section">
  <div id="springfield2"><h2 id="article1">Article 1.</h2></div>
</body></html>"""


def _make_no_main_div_html(code: int) -> str:
    return f"""<html>
<body id="{code}.htm" class="{BODY_CLASS}">
  <div id="wrong-div"><h2 id="article1">Article 1.</h2></div>
</body></html>"""


def _make_no_articles_html(code: int) -> str:
    return f"""<html>
<body id="{code}.htm" class="{BODY_CLASS}">
  <div id="springfield2"><p>Empty question — no articles.</p></div>
</body></html>"""


# ---------------------------------------------------------------------------
# URL generation and code ordering
# ---------------------------------------------------------------------------

class TestAllCodes:
    def test_total_count(self):
        codes = all_codes()
        expected = sum(len(r) for r in PARS_CODES.values())
        assert len(codes) == expected

    def test_prima_pars_range(self):
        codes = all_codes()
        for c in range(1001, 1120):
            assert c in codes

    def test_prima_secundae_range(self):
        codes = all_codes()
        for c in range(2001, 2115):
            assert c in codes

    def test_secunda_secundae_range(self):
        codes = all_codes()
        for c in range(3001, 3190):
            assert c in codes

    def test_tertia_pars_range(self):
        codes = all_codes()
        for c in range(4001, 4091):
            assert c in codes

    def test_supplementum_range(self):
        codes = all_codes()
        for c in range(5001, 5100):
            assert c in codes

    def test_appendix_i_present(self):
        codes = all_codes()
        assert 6001 in codes
        assert 6002 in codes

    def test_appendix_ii_present(self):
        codes = all_codes()
        assert 7001 in codes

    def test_no_duplicates(self):
        codes = all_codes()
        assert len(codes) == len(set(codes))

    def test_pars_order(self):
        """Prima Pars codes come before Prima-Secundae, which come before Secunda-Secundae, etc."""
        codes = all_codes()
        prima_idx = codes.index(1001)
        iiae_idx = codes.index(2001)
        iiiae_idx = codes.index(3001)
        iii_idx = codes.index(4001)
        supp_idx = codes.index(5001)
        assert prima_idx < iiae_idx < iiiae_idx < iii_idx < supp_idx

    def test_no_gaps_within_prima_pars(self):
        codes = set(all_codes())
        assert all(c in codes for c in range(1001, 1120))

    def test_no_gaps_within_tertia_pars(self):
        codes = set(all_codes())
        assert all(c in codes for c in range(4001, 4091))


# ---------------------------------------------------------------------------
# _assert_article_page — structural checks
# ---------------------------------------------------------------------------

class TestAssertArticlePage:
    def test_valid_page_returns_soup(self):
        html = _make_article_html(1001)
        soup = _assert_article_page(html, "https://www.newadvent.org/summa/1001.htm", 1001)
        assert soup is not None

    def test_valid_page_multi_article(self):
        html = _make_article_html(1001, n_articles=10)
        soup = _assert_article_page(html, "https://www.newadvent.org/summa/1001.htm", 1001)
        articles = soup.find("div", id="springfield2").find_all(
            "h2", id=lambda x: x and x.startswith("article")
        )
        assert len(articles) == 10

    def test_raises_on_missing_body(self):
        html = _make_no_body_html()
        with pytest.raises(RuntimeError, match="no <body> tag found"):
            _assert_article_page(html, "https://www.newadvent.org/summa/1001.htm", 1001)

    def test_raises_when_redirected_to_home(self):
        """newadvent returns the home page for non-existent codes."""
        html = _make_home_html()
        with pytest.raises(RuntimeError, match="expected body id="):
            _assert_article_page(html, "https://www.newadvent.org/summa/9999.htm", 9999)

    def test_raises_on_wrong_body_class(self):
        html = _make_wrong_class_html(2001)
        with pytest.raises(RuntimeError, match="expected body class to include"):
            _assert_article_page(html, "https://www.newadvent.org/summa/2001.htm", 2001)

    def test_raises_on_missing_springfield2(self):
        html = _make_no_main_div_html(3001)
        with pytest.raises(RuntimeError, match="#springfield2 not found"):
            _assert_article_page(html, "https://www.newadvent.org/summa/3001.htm", 3001)

    def test_raises_when_no_article_h2(self):
        html = _make_no_articles_html(4001)
        with pytest.raises(RuntimeError, match="no <h2 id='articleN'> found"):
            _assert_article_page(html, "https://www.newadvent.org/summa/4001.htm", 4001)

    def test_error_message_includes_url(self):
        url = "https://www.newadvent.org/summa/9999.htm"
        html = _make_home_html()
        with pytest.raises(RuntimeError) as exc_info:
            _assert_article_page(html, url, 9999)
        assert url in str(exc_info.value)

    def test_wrong_body_id_for_code(self):
        """Page has valid structure but for a different code — catches misrouted responses."""
        html = _make_article_html(1001)
        with pytest.raises(RuntimeError, match="expected body id='5099.htm'"):
            _assert_article_page(html, "https://www.newadvent.org/summa/5099.htm", 5099)


# ---------------------------------------------------------------------------
# verify_coverage
# ---------------------------------------------------------------------------

class TestVerifyCoverage:
    def _write_html(self, dest: Path, code: int) -> None:
        (dest / f"{code}.html").write_text(_make_article_html(code), encoding="utf-8")

    def test_empty_dest_all_missing(self, tmp_path: Path):
        coverage = verify_coverage(tmp_path)
        expected_total = sum(len(r) for r in PARS_CODES.values())
        assert len(coverage["missing"]) == expected_total
        assert len(coverage["present"]) == 0
        assert coverage["complete"] is False

    def test_complete_dest_no_missing(self, tmp_path: Path):
        for code in all_codes():
            self._write_html(tmp_path, code)
        coverage = verify_coverage(tmp_path)
        assert coverage["missing"] == []
        assert coverage["complete"] is True

    def test_partial_coverage(self, tmp_path: Path):
        # Write only the first few Prima Pars pages
        for code in range(1001, 1011):
            self._write_html(tmp_path, code)
        coverage = verify_coverage(tmp_path)
        assert set(range(1001, 1011)).issubset(set(coverage["present"]))
        assert 1011 in coverage["missing"]
        assert 2001 in coverage["missing"]
        assert coverage["complete"] is False

    def test_present_and_missing_are_disjoint(self, tmp_path: Path):
        for code in range(1001, 1050):
            self._write_html(tmp_path, code)
        coverage = verify_coverage(tmp_path)
        assert set(coverage["present"]).isdisjoint(set(coverage["missing"]))

    def test_present_plus_missing_equals_all(self, tmp_path: Path):
        for code in range(1001, 1060):
            self._write_html(tmp_path, code)
        coverage = verify_coverage(tmp_path)
        total = sum(len(r) for r in PARS_CODES.values())
        assert len(coverage["present"]) + len(coverage["missing"]) == total

    def test_by_pars_complete_label(self, tmp_path: Path):
        # Write all Prima Pars pages
        for code in range(1001, 1120):
            self._write_html(tmp_path, code)
        coverage = verify_coverage(tmp_path)
        prima_summary = next(
            line for line in coverage["by_pars"] if "Prima Pars" in line
        )
        assert "complete" in prima_summary

    def test_by_pars_partial_label(self, tmp_path: Path):
        # Write only the first 10 Prima Pars pages — partial
        for code in range(1001, 1011):
            self._write_html(tmp_path, code)
        coverage = verify_coverage(tmp_path)
        prima_summary = next(
            line for line in coverage["by_pars"] if "Prima Pars" in line
        )
        assert "missing" in prima_summary.lower()

    def test_by_pars_has_entry_for_all_partes(self, tmp_path: Path):
        coverage = verify_coverage(tmp_path)
        pars_names = [line.split(":")[0] for line in coverage["by_pars"]]
        for expected in PARS_CODES:
            assert expected in pars_names

    def test_appendix_codes_tracked(self, tmp_path: Path):
        self._write_html(tmp_path, 6001)
        coverage = verify_coverage(tmp_path)
        assert 6001 in coverage["present"]
        assert 6002 in coverage["missing"]

    def test_sorted_present(self, tmp_path: Path):
        for code in [3001, 1001, 4001, 2001]:
            self._write_html(tmp_path, code)
        coverage = verify_coverage(tmp_path)
        assert coverage["present"] == sorted(coverage["present"])

    def test_sorted_missing(self, tmp_path: Path):
        coverage = verify_coverage(tmp_path)
        assert coverage["missing"] == sorted(coverage["missing"])
