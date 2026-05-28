"""
Tests for src/acquire/bahounek.py.
All tests use fixture HTML — no live network calls.
"""

import re
import textwrap
from pathlib import Path

import pytest

from acquire.bahounek import (
    BASE_URL,
    COORD_RE,
    check_all_partes_covered,
    extract_section_urls,
    verify_coordinate_tags,
)


# ---------------------------------------------------------------------------
# Coordinate tag detection
# ---------------------------------------------------------------------------

FIXTURE_GOOD_I = textwrap.dedent("""\
    <html><body>
    <p>I ot. 1 čl. 1 arg. 1<br/>Zdá se, že ...<br/>
    I ot. 1 čl. 1 arg. 2<br/>Dále ...<br/>
    I ot. 1 čl. 1 sc.<br/>Naproti tomu ...<br/>
    I ot. 1 čl. 1 co.<br/>Odpovídám ...<br/>
    I ot. 1 čl. 1 ad 1<br/>K prvnímu ...<br/>
    </p></body></html>
""")

FIXTURE_GOOD_I_II = textwrap.dedent("""\
    <html><body>
    <p>I-II ot. 1 čl. 1 arg. 1<br/>Text...<br/>
    I-II ot. 1 čl. 1 sc.<br/>Naproti...<br/>
    I-II ot. 1 čl. 1 co.<br/>Odpovídám...<br/>
    </p></body></html>
""")

FIXTURE_GOOD_II_II = textwrap.dedent("""\
    <html><body>
    <p>II-II ot. 23 čl. 4 arg. 2<br/>Text...</p>
    </body></html>
""")

FIXTURE_GOOD_III = textwrap.dedent("""\
    <html><body>
    <p>III ot. 45 čl. 3 co.<br/>Text...</p>
    </body></html>
""")

FIXTURE_NO_COORDS = textwrap.dedent("""\
    <html><body>
    <p>Some random text without any coordinate tags.</p>
    </body></html>
""")

# A page that has tags for pars I but we ask for pars I-II
FIXTURE_WRONG_PARS = textwrap.dedent("""\
    <html><body>
    <p>I ot. 1 čl. 1 arg. 1<br/>Prima Pars text...</p>
    </body></html>
""")


class TestCoordRegex:
    def test_matches_arg(self):
        assert COORD_RE.search("I ot. 1 čl. 1 arg. 1")

    def test_matches_sc(self):
        assert COORD_RE.search("I-II ot. 5 čl. 3 sc.")

    def test_matches_co(self):
        assert COORD_RE.search("II-II ot. 23 čl. 4 co.")

    def test_matches_ad(self):
        assert COORD_RE.search("III ot. 45 čl. 2 ad 3")

    def test_matches_pr(self):
        assert COORD_RE.search("I ot. 1 čl. 1 pr.")

    def test_no_match_on_plain_text(self):
        assert not COORD_RE.search("some random text")

    def test_multi_digit_question(self):
        assert COORD_RE.search("II-II ot. 189 čl. 10 arg. 2")

    def test_does_not_match_partial(self):
        # Missing the čl. part
        assert not COORD_RE.search("I ot. 1 arg. 1")


class TestVerifyCoordinateTags:
    def test_passes_for_good_pars_I(self):
        verify_coordinate_tags(FIXTURE_GOOD_I, "http://example.com", "I")

    def test_passes_for_good_pars_I_II(self):
        verify_coordinate_tags(FIXTURE_GOOD_I_II, "http://example.com", "I-II")

    def test_passes_for_good_pars_II_II(self):
        verify_coordinate_tags(FIXTURE_GOOD_II_II, "http://example.com", "II-II")

    def test_passes_for_good_pars_III(self):
        verify_coordinate_tags(FIXTURE_GOOD_III, "http://example.com", "III")

    def test_crashes_on_no_coords(self):
        with pytest.raises(RuntimeError) as exc_info:
            verify_coordinate_tags(FIXTURE_NO_COORDS, "http://example.com/page", "I")
        msg = str(exc_info.value)
        assert "http://example.com/page" in msg
        assert "pars='I'" in msg

    def test_crashes_wrong_pars_contains_url(self):
        url = "http://www.cormierop.cz/Summa-teologicka-IIcast-1dil.html"
        with pytest.raises(RuntimeError) as exc_info:
            verify_coordinate_tags(FIXTURE_WRONG_PARS, url, "I-II")
        assert url in str(exc_info.value)

    def test_error_includes_expected_pattern(self):
        with pytest.raises(RuntimeError) as exc_info:
            verify_coordinate_tags(FIXTURE_NO_COORDS, "http://x.com", "III")
        assert "III ot." in str(exc_info.value)


# ---------------------------------------------------------------------------
# Section URL extraction
# ---------------------------------------------------------------------------

FIXTURE_INDEX_PAGE = textwrap.dedent("""\
    <html><body>
    <ul>
      <li><a href="Summa-teologicka-Icast.html">I. část</a></li>
      <li><a href="Summa-teologicka-IIcast-1dil.html">II. část 1. díl</a></li>
      <li><a href="Summa-teologicka-IIcast-2dil.html">II. část 2. díl</a></li>
      <li><a href="Summa-teologicka-IIIcast.html">III. část</a></li>
      <li><a href="Summa-proti-pohanum-1kniha.html">Contra Gentiles 1</a></li>
      <li><a href="OTHER-PAGE.html">Unrelated page</a></li>
    </ul>
    </body></html>
""")


class TestExtractSectionUrls:
    def test_extracts_all_four_partes(self):
        urls = extract_section_urls(FIXTURE_INDEX_PAGE, BASE_URL)
        assert len(urls) == 4

    def test_urls_are_absolute(self):
        urls = extract_section_urls(FIXTURE_INDEX_PAGE, BASE_URL)
        for url in urls:
            assert url.startswith("http://")

    def test_correct_urls_present(self):
        urls = extract_section_urls(FIXTURE_INDEX_PAGE, BASE_URL)
        assert f"{BASE_URL}/Summa-teologicka-Icast.html" in urls
        assert f"{BASE_URL}/Summa-teologicka-IIcast-1dil.html" in urls
        assert f"{BASE_URL}/Summa-teologicka-IIcast-2dil.html" in urls
        assert f"{BASE_URL}/Summa-teologicka-IIIcast.html" in urls

    def test_excludes_non_teologicka_links(self):
        urls = extract_section_urls(FIXTURE_INDEX_PAGE, BASE_URL)
        for url in urls:
            assert "pohanum" not in url
            assert "OTHER-PAGE" not in url

    def test_empty_page_returns_empty_list(self):
        urls = extract_section_urls("<html><body></body></html>", BASE_URL)
        assert urls == []


# ---------------------------------------------------------------------------
# Coverage check
# ---------------------------------------------------------------------------

class TestCheckAllPartesCovered:
    def test_passes_when_all_present(self, tmp_path):
        files = []
        for name in ["pars_I.html", "pars_I-II.html", "pars_II-II.html", "pars_III.html"]:
            p = tmp_path / name
            p.write_text("x")
            files.append(p)
        check_all_partes_covered(files)

    def test_fails_when_one_missing(self, tmp_path):
        files = []
        for name in ["pars_I.html", "pars_I-II.html", "pars_III.html"]:
            p = tmp_path / name
            p.write_text("x")
            files.append(p)
        with pytest.raises(RuntimeError) as exc_info:
            check_all_partes_covered(files)
        assert "pars_II-II.html" in str(exc_info.value)

    def test_fails_when_all_missing(self):
        with pytest.raises(RuntimeError):
            check_all_partes_covered([])

    def test_error_lists_missing_files(self, tmp_path):
        files = [tmp_path / "pars_I.html"]
        files[0].write_text("x")
        with pytest.raises(RuntimeError) as exc_info:
            check_all_partes_covered(files)
        msg = str(exc_info.value)
        assert "pars_I-II.html" in msg
        assert "pars_II-II.html" in msg
        assert "pars_III.html" in msg
