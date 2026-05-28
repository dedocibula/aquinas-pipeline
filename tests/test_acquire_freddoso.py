"""
Tests for src/acquire/freddoso.py.

No live network calls: all HTML is supplied as fixture strings.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from acquire.freddoso import (
    BASE_URL,
    SUMMA_QUESTION_COUNTS,
    TOC_PAGES,
    _extract_question_numbers,
    build_coverage_gaps,
    write_coverage_gaps,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_toc_html(
    url_prefix: str,
    filename_prefix: str,
    question_numbers: list[int],
    extra_links: list[str] | None = None,
) -> str:
    """Build a minimal TOC HTML page with .pdf links for the given questions."""
    links = []
    for q in question_numbers:
        href = f"{url_prefix}{filename_prefix}{q:02d}.pdf"
        links.append(f'<a href="{href}">Q. {q}</a>')
    if extra_links:
        links.extend(f'<a href="{h}">extra</a>' for h in extra_links)
    body = "\n".join(links)
    return f"<html><body>{body}</body></html>"


# ---------------------------------------------------------------------------
# _extract_question_numbers
# ---------------------------------------------------------------------------

class TestExtractQuestionNumbers:
    def _entry(self, part_idx: int) -> dict:
        return TOC_PAGES[part_idx]

    def test_part1_typical(self):
        entry = self._entry(0)
        # q1-119 minus q99 (mirrors live site)
        qs = [q for q in range(1, 120) if q != 99]
        html = _make_toc_html(
            entry["url_prefix"], entry["filename_prefix"], qs
        )
        result = _extract_question_numbers(
            html, entry["toc_url"], entry["url_prefix"], entry["filename_prefix"]
        )
        assert result == qs

    def test_part1_2_complete(self):
        entry = self._entry(1)
        qs = list(range(1, 115))
        html = _make_toc_html(
            entry["url_prefix"], entry["filename_prefix"], qs
        )
        result = _extract_question_numbers(
            html, entry["toc_url"], entry["url_prefix"], entry["filename_prefix"]
        )
        assert result == qs

    def test_part3_partial(self):
        entry = self._entry(3)
        # q1-78 only (q79-90 absent on live site)
        qs = list(range(1, 79))
        html = _make_toc_html(
            entry["url_prefix"], entry["filename_prefix"], qs
        )
        result = _extract_question_numbers(
            html, entry["toc_url"], entry["url_prefix"], entry["filename_prefix"]
        )
        assert result == qs

    def test_ignores_non_pdf_links(self):
        entry = self._entry(0)
        html = _make_toc_html(
            entry["url_prefix"],
            entry["filename_prefix"],
            [1, 2, 3],
            extra_links=[
                "https://amazon.com/some-book",
                "/summa-translation/tableofcontents-part1.pdf",
                "https://www.freddoso.com/",
            ],
        )
        result = _extract_question_numbers(
            html, entry["toc_url"], entry["url_prefix"], entry["filename_prefix"]
        )
        assert result == [1, 2, 3]

    def test_returns_sorted(self):
        entry = self._entry(0)
        # Feed them in reverse order; result must still be sorted.
        qs = [5, 3, 1, 4, 2]
        html = _make_toc_html(
            entry["url_prefix"], entry["filename_prefix"], qs
        )
        result = _extract_question_numbers(
            html, entry["toc_url"], entry["url_prefix"], entry["filename_prefix"]
        )
        assert result == [1, 2, 3, 4, 5]

    def test_crashes_on_empty_toc(self):
        entry = self._entry(0)
        html = "<html><body><p>No links here.</p></body></html>"
        with pytest.raises(RuntimeError, match="No question PDF links found"):
            _extract_question_numbers(
                html, entry["toc_url"], entry["url_prefix"], entry["filename_prefix"]
            )

    def test_crashes_on_unexpected_suffix(self):
        entry = self._entry(0)
        # A link that matches the url_prefix but has a .htm suffix instead of .pdf
        bad_href = f"{entry['url_prefix']}{entry['filename_prefix']}01.htm"
        html = f'<html><body><a href="{bad_href}">Q.1</a></body></html>'
        with pytest.raises(RuntimeError, match="Expected .pdf suffix"):
            _extract_question_numbers(
                html, entry["toc_url"], entry["url_prefix"], entry["filename_prefix"]
            )

    def test_crashes_on_non_numeric_question(self):
        entry = self._entry(0)
        bad_href = f"{entry['url_prefix']}{entry['filename_prefix']}foo.pdf"
        html = f'<html><body><a href="{bad_href}">Q.foo</a></body></html>'
        with pytest.raises(RuntimeError, match="Non-numeric question number"):
            _extract_question_numbers(
                html, entry["toc_url"], entry["url_prefix"], entry["filename_prefix"]
            )


# ---------------------------------------------------------------------------
# build_coverage_gaps
# ---------------------------------------------------------------------------

class TestBuildCoverageGaps:
    def _full_available(self) -> dict[str, list[int]]:
        """Simulate all parts complete."""
        return {part: list(range(1, total + 1)) for part, total in SUMMA_QUESTION_COUNTS.items()}

    def test_available_locator_format(self):
        gaps = build_coverage_gaps({"I": [1, 2, 3], "I-II": [], "II-II": [], "III": []})
        assert "I.q1" in gaps["available"]
        assert "I.q3" in gaps["available"]

    def test_missing_locator_format(self):
        # Give Part I only q1; everything else missing.
        available = {"I": [1], "I-II": [], "II-II": [], "III": []}
        gaps = build_coverage_gaps(available)
        # q2 through q119 should all be in missing for Part I
        assert "I.q2" in gaps["missing"]
        assert "I.q119" in gaps["missing"]
        # All of I-II, II-II, III should be missing
        assert "I-II.q1" in gaps["missing"]
        assert "II-II.q1" in gaps["missing"]
        assert "III.q1" in gaps["missing"]

    def test_no_overlap_between_available_and_missing(self):
        available = {"I": list(range(1, 100)), "I-II": list(range(1, 115)), "II-II": [], "III": []}
        gaps = build_coverage_gaps(available)
        av_set = set(gaps["available"])
        mi_set = set(gaps["missing"])
        assert av_set.isdisjoint(mi_set)

    def test_full_coverage_produces_empty_missing(self):
        gaps = build_coverage_gaps(self._full_available())
        assert gaps["missing"] == []
        assert len(gaps["available"]) == sum(SUMMA_QUESTION_COUNTS.values())

    def test_live_site_approximation(self):
        # Mirrors actual Freddoso coverage based on site probe.
        available = {
            "I": [q for q in range(1, 120) if q != 99],  # q99 missing
            "I-II": list(range(1, 115)),                   # complete
            "II-II": list(range(1, 190)),                  # complete
            "III": list(range(1, 79)),                     # q79-90 missing
        }
        gaps = build_coverage_gaps(available)
        # q99 missing from Part I
        assert "I.q99" in gaps["missing"]
        # I-II complete
        assert not any(loc.startswith("I-II.") for loc in gaps["missing"])
        # II-II complete
        assert not any(loc.startswith("II-II.") for loc in gaps["missing"])
        # III q79-90 missing
        assert "III.q79" in gaps["missing"]
        assert "III.q90" in gaps["missing"]
        assert "III.q78" in gaps["available"]

    def test_notes_field_is_string(self):
        gaps = build_coverage_gaps(self._full_available())
        assert isinstance(gaps["notes"], str)
        assert len(gaps["notes"]) > 0

    def test_notes_describes_partial_part(self):
        available = {"I": list(range(1, 50)), "I-II": [], "II-II": [], "III": []}
        gaps = build_coverage_gaps(available)
        assert "partial" in gaps["notes"]
        assert "I " in gaps["notes"]

    def test_notes_describes_complete_part(self):
        available = {"I": list(range(1, 120)), "I-II": [], "II-II": [], "III": []}
        gaps = build_coverage_gaps(available)
        assert "complete" in gaps["notes"]


# ---------------------------------------------------------------------------
# JSON output structure
# ---------------------------------------------------------------------------

class TestJsonOutputStructure:
    def test_required_keys_present(self):
        gaps = build_coverage_gaps({"I": [1], "I-II": [], "II-II": [], "III": []})
        assert set(gaps.keys()) == {"available", "missing", "notes"}

    def test_available_is_list_of_strings(self):
        gaps = build_coverage_gaps({"I": [1, 2], "I-II": [], "II-II": [], "III": []})
        assert isinstance(gaps["available"], list)
        assert all(isinstance(x, str) for x in gaps["available"])

    def test_missing_is_list_of_strings(self):
        gaps = build_coverage_gaps({"I": [1], "I-II": [], "II-II": [], "III": []})
        assert isinstance(gaps["missing"], list)
        assert all(isinstance(x, str) for x in gaps["missing"])

    def test_notes_is_string(self):
        gaps = build_coverage_gaps({"I": [1], "I-II": [], "II-II": [], "III": []})
        assert isinstance(gaps["notes"], str)

    def test_write_produces_valid_json(self, tmp_path: Path):
        gaps = build_coverage_gaps(
            {"I": [1, 2], "I-II": [1], "II-II": [], "III": []}
        )
        out = write_coverage_gaps(tmp_path, gaps)
        assert out.exists()
        loaded = json.loads(out.read_text())
        assert loaded["available"] == gaps["available"]
        assert loaded["missing"] == gaps["missing"]
        assert loaded["notes"] == gaps["notes"]

    def test_write_filename(self, tmp_path: Path):
        gaps = build_coverage_gaps({"I": [], "I-II": [], "II-II": [], "III": []})
        out = write_coverage_gaps(tmp_path, gaps)
        assert out.name == "coverage_gaps.json"


# ---------------------------------------------------------------------------
# Fall-back detection logic
# ---------------------------------------------------------------------------

class TestFallbackDetection:
    """
    The M1 ingest reads coverage_gaps.json to decide whether to fall back
    to Dominican Province for a given locator.  These tests verify the
    locator format and set membership logic that enables that decision.
    """

    def _gaps_from_live(self) -> dict:
        return build_coverage_gaps(
            {
                "I": [q for q in range(1, 120) if q != 99],
                "I-II": list(range(1, 115)),
                "II-II": list(range(1, 190)),
                "III": list(range(1, 79)),
            }
        )

    def _needs_fallback(self, gaps: dict, locator: str) -> bool:
        """True when Freddoso does NOT cover this locator → use Dominican."""
        return locator in gaps["missing"]

    def test_available_question_does_not_need_fallback(self):
        gaps = self._gaps_from_live()
        assert not self._needs_fallback(gaps, "I.q1")
        assert not self._needs_fallback(gaps, "I-II.q114")
        assert not self._needs_fallback(gaps, "II-II.q189")
        assert not self._needs_fallback(gaps, "III.q78")

    def test_missing_question_triggers_fallback(self):
        gaps = self._gaps_from_live()
        assert self._needs_fallback(gaps, "I.q99")
        assert self._needs_fallback(gaps, "III.q79")
        assert self._needs_fallback(gaps, "III.q90")

    def test_locator_set_is_mutually_exclusive(self):
        gaps = self._gaps_from_live()
        av = set(gaps["available"])
        mi = set(gaps["missing"])
        assert av.isdisjoint(mi), "A locator cannot be both available and missing"

    def test_union_covers_all_summa_questions(self):
        gaps = self._gaps_from_live()
        all_locs = set(gaps["available"]) | set(gaps["missing"])
        expected_total = sum(SUMMA_QUESTION_COUNTS.values())
        assert len(all_locs) == expected_total
