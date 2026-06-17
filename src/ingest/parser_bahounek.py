"""
Bahounek Czech HTML parser.

Parses sources/czech/bahounek/pars_*.html for the 10 test articles.
Writes segment_text(cs, bahounek) rows matched to existing segment locators.

Coordinate tag format (as found in the real HTML):
  {PARS} ot. {N} čl. {M} arg. {K}   → arg
  {PARS} ot. {N} čl. {M} protiarg.  → sed_contra
  {PARS} ot. {N} čl. {M} odp.       → respondeo
  {PARS} ot. {N} čl. {M} k {K}      → reply
  {PARS} ot. {N} pr.                 → preamble

PARS tokens: I, I-II, II-II, III
ltree-safe pars mapping: I→I, I-II→I_II, II-II→II_II, III→III

FAIL LOUDLY: crashes if a coordinate cannot be matched to an existing segment.

Run:
  uv run python -m ingest.parser_bahounek
"""

from __future__ import annotations

import re
import sys
import warnings
from pathlib import Path
from typing import IO

import bs4
from bs4 import BeautifulSoup

from ingest.parser_latin import _PARS_CODE, TEST_ARTICLES
from ingest.source_parser import OverlayElement, TextOverlayParser
from storage.db import get_conn, source_id
from storage.repositories import SegmentRepository

# Bahounek HTML is XHTML served without the XML declaration BeautifulSoup expects.
warnings.filterwarnings("ignore", category=bs4.XMLParsedAsHTMLWarning)

ROOT = Path(__file__).resolve().parents[2]
BAHOUNEK_DIR = ROOT / "sources" / "czech" / "bahounek"

_PARS_FILE: dict[str, str] = {
    "I": "pars_I.html",
    "I-II": "pars_I-II.html",
    "II-II": "pars_II-II.html",
    "III": "pars_III.html",
}

# Derive pars ltree code from filename for question-title extraction.
_FILE_PARS_LTREE: dict[str, str] = {
    filename: _PARS_CODE[raw] for raw, filename in _PARS_FILE.items()
}

# ── Coordinate parsing ────────────────────────────────────────────────────────

# Matches any Bahounek coordinate tag at the start of a text node.
# Groups: (pars_raw, q, a_or_none, suffix)
_COORD_RE = re.compile(
    r"(I-II|II-II|III|I)\s+ot\.\s*(\d+)\s+(?:čl\.\s*(\d+)\s+(arg\.\s*\d+|protiarg\.|odp\.|k\s+\d+)|pr\.)"
)


def _parse_coord(raw: str) -> str | None:
    """Parse a raw coordinate tag text into an ltree locator, or None."""
    raw = raw.strip()
    m = _COORD_RE.match(raw)
    if not m:
        return None

    pars_raw = m.group(1)
    pars = _PARS_CODE[pars_raw]
    q = m.group(2)

    # Preamble: {PARS} ot. N pr.
    if m.group(3) is None:
        return f"{pars}.q{q}.preamble"

    a = m.group(3)
    suffix = m.group(4).strip()

    base = f"{pars}.q{q}.a{a}"

    arg_m = re.fullmatch(r"arg\.\s*(\d+)", suffix)
    if arg_m:
        return f"{base}.arg{arg_m.group(1)}"

    if suffix == "protiarg.":
        return f"{base}.sed_contra"

    if suffix == "odp.":
        return f"{base}.respondeo"

    k_m = re.fullmatch(r"k\s+(\d+)", suffix)
    if k_m:
        return f"{base}.reply{k_m.group(1)}"

    return None


# ── HTML parsing ──────────────────────────────────────────────────────────────


def _extract_question_titles(soup: BeautifulSoup, pars_ltree: str) -> list[OverlayElement]:
    """Extract Czech question title text from Bahounek pars HTML.

    Bahounek marks question titles as ``<p><span>N. TITLE TEXT<br/>...</span></p>``.
    The question number N is extracted and mapped to ``{pars_ltree}.qN.question_title``.
    """
    results: list[OverlayElement] = []
    for p in soup.find_all("p"):
        span = p.find("span")
        if span is None or span.find("br") is None:
            continue
        # Collect text nodes that precede the first <br> child.
        title_parts: list[str] = []
        for child in span.children:
            if getattr(child, "name", None) == "br":
                break
            text = child.get_text() if hasattr(child, "get_text") else str(child)
            text = text.strip()
            if text:
                title_parts.append(text)
        raw = " ".join(title_parts).strip()
        m = re.match(r"^(\d+)\.\s+(.+)$", raw)
        if not m:
            continue
        q_num = m.group(1)
        title_text = m.group(2).strip()
        # Bahounek question headings are ALL CAPS; skip mixed-case lines which
        # are article body text accidentally matching the N. text pattern.
        alpha = [c for c in title_text if c.isalpha()]
        if not alpha or sum(1 for c in alpha if c.isupper()) / len(alpha) < 0.8:
            continue
        # The question_title segment lives at the question locator itself (e.g. I.q1),
        # not at a sub-locator — matching the schema set by parser_latin.
        results.append(OverlayElement(f"{pars_ltree}.q{q_num}", title_text))
    return results


def _extract_elements_from_file(html_path: Path) -> list[OverlayElement]:
    """Extract all elements from one Bahounek HTML file.

    Returns question_title elements (from titled question headings) followed by
    body elements (coordinate-tagged paragraphs).
    """
    content = html_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(content, "lxml")

    pars_ltree = _FILE_PARS_LTREE.get(html_path.name, "")
    elements: list[OverlayElement] = []

    if pars_ltree:
        elements.extend(_extract_question_titles(soup, pars_ltree))

    full_text = soup.get_text(separator="\n")
    lines = full_text.splitlines()

    current_locator: str | None = None
    text_lines: list[str] = []

    def _flush():
        nonlocal current_locator, text_lines
        if current_locator and text_lines:
            text = " ".join(t.strip() for t in text_lines if t.strip())
            if text:
                elements.append(OverlayElement(current_locator, text))
        current_locator = None
        text_lines = []

    for line in lines:
        loc = _parse_coord(line)
        if loc is not None:
            _flush()
            current_locator = loc
        elif current_locator is not None:
            text_lines.append(line)

    _flush()
    return elements


def parse_bahounek_for_articles(article_locators: list[str]) -> list[OverlayElement]:
    """Parse Bahounek HTML for the given article locators and return matched elements.

    Includes question_title elements for the parent question of each requested article.
    """
    # Determine which pars files are needed
    pars_needed: set[str] = set()
    for loc in article_locators:
        pars_ltree = loc.split(".")[0]
        pars_needed.add(pars_ltree)

    # Map ltree pars → raw pars (reverse of _PARS_CODE)
    ltree_to_raw = {v: k for k, v in _PARS_CODE.items()}

    # Collect all elements from needed pars files
    all_elements: dict[str, OverlayElement] = {}  # locator → element
    for pars_ltree in sorted(pars_needed):
        pars_raw = ltree_to_raw[pars_ltree]
        html_path = BAHOUNEK_DIR / _PARS_FILE[pars_raw]
        if not html_path.exists():
            raise RuntimeError(
                f"FAIL: Bahounek file not found: {html_path}. "
                "Was the source download completed?"
            )
        file_elements = _extract_elements_from_file(html_path)
        for elem in file_elements:
            all_elements[elem.locator] = elem

    result: list[OverlayElement] = []

    # Include Czech question titles and preambles for parent questions of requested articles.
    parent_q_locs = {".".join(loc.split(".")[:2]) for loc in article_locators}
    for q_loc in sorted(parent_q_locs):
        if q_loc in all_elements:
            result.append(all_elements[q_loc])
        preamble_loc = f"{q_loc}.preamble"
        if preamble_loc in all_elements:
            result.append(all_elements[preamble_loc])

    # Body segments for each requested article
    for art_loc in article_locators:
        article_prefix = art_loc + "."
        for loc, elem in sorted(all_elements.items()):
            if loc.startswith(article_prefix) or loc == art_loc:
                result.append(elem)

    return result


# ── DB insertion ──────────────────────────────────────────────────────────────

class BahounekParser(TextOverlayParser):
    """Czech overlay parser: writes segment_text(cs) from Bahounek HTML."""

    lang = "cs"

    def parse(self, article_locators: list[str]) -> list[OverlayElement]:
        return parse_bahounek_for_articles(article_locators)


_BAHOUNEK = BahounekParser()


def insert_bahounek_texts(
    conn,
    elements: list[OverlayElement],
    src_id: int,
    gap_log: IO[str] | None = None,
) -> int:
    """Insert segment_text(cs, bahounek) rows. Returns count inserted.

    When gap_log is provided, missing segments are logged and skipped rather
    than raising. When gap_log is None, a missing segment raises RuntimeError
    (test-mode behaviour — fail loudly).
    """

    def on_missing(locator: str) -> None:
        if gap_log is not None:
            gap_log.write(f"[GAP] locator={locator} reason=no_segment_match\n")
            return
        raise RuntimeError(
            f"FAIL: Bahounek coordinate {locator!r} has no matching segment. "
            "Run parser_latin.py first."
        )

    return _BAHOUNEK.store(conn, elements, src_id, on_missing)


def write_bahounek_coverage(conn, gap_log: IO[str]) -> None:
    """Append coverage summary and per-locator missing-Czech map to gap_log.

    Writes:
      COVERAGE: segments_with_czech=N total_body_segments=M pct=X%
      MISSING_CZECH: locator=... (one line per body segment without Czech text)
    """
    segments_with_czech, total_body_segments, missing_locators = (
        SegmentRepository(conn).body_text_coverage("cs")
    )

    if total_body_segments > 0:
        pct = round(100.0 * segments_with_czech / total_body_segments, 1)
    else:
        pct = 0.0

    gap_log.write(
        f"COVERAGE: segments_with_czech={segments_with_czech} "
        f"total_body_segments={total_body_segments} "
        f"pct={pct}%\n"
    )
    for loc in missing_locators:
        gap_log.write(f"MISSING_CZECH: locator={loc}\n")


# ── Spot-check ────────────────────────────────────────────────────────────────

_SPOT_CHECK_ARTICLES = ["I.q3.a1", "I.q13.a5", "I_II.q5.a1", "II_II.q23.a1", "III.q1.a1"]


def spot_check(elements: list[OverlayElement]) -> None:
    """Print a sample of matched elements for manual verification."""
    print("\nSpot-check (5 articles):")
    shown: dict[str, int] = {}
    for elem in elements:
        art = ".".join(elem.locator.split(".")[:3])
        if art in _SPOT_CHECK_ARTICLES and shown.get(art, 0) < 2:
            print(f"  {elem.locator}: {elem.text[:80]!r}")
            shown[art] = shown.get(art, 0) + 1


# ── Entry point ───────────────────────────────────────────────────────────────

def _articles_from_db() -> list[str]:
    """Return all article locators that have been inserted into segment."""
    with get_conn() as conn:
        return SegmentRepository(conn).get_article_title_locators()


def run(articles: list[str] | None = None, gap_log_path: Path | None = None) -> None:
    target_articles = articles or _articles_from_db() or TEST_ARTICLES

    print("Parsing Bahounek HTML for test articles...")
    elements = parse_bahounek_for_articles(target_articles)
    print(f"  Found {len(elements)} Czech text elements")

    spot_check(elements)

    print("\nInserting into DB...")
    if gap_log_path is not None:
        with gap_log_path.open("a", encoding="utf-8") as gap_log:
            with get_conn() as conn:
                src = source_id(conn, "bahounek")
                count = insert_bahounek_texts(conn, elements, src, gap_log=gap_log)
                write_bahounek_coverage(conn, gap_log)
    else:
        with get_conn() as conn:
            src = source_id(conn, "bahounek")
            count = insert_bahounek_texts(conn, elements, src)

    print(f"Done. {count} segment_text(cs, bahounek) rows inserted.")


if __name__ == "__main__":
    # Full-corpus runs hit Latin-source gaps (segments Bahounek has but Latin
    # parser never created).  Log those and continue rather than hard-failing.
    _gap_log = ROOT / "reports" / "bahounek_gaps.log"
    _gap_log.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(gap_log_path=_gap_log)
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        sys.exit(1)
