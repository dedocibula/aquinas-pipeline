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
from dataclasses import dataclass
from pathlib import Path

import bs4
from bs4 import BeautifulSoup

from ingest.db import get_conn, source_id
from ingest.parser_latin import _PARS_CODE, TEST_ARTICLES

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

# ── Coordinate parsing ────────────────────────────────────────────────────────

# Matches any Bahounek coordinate tag at the start of a text node.
# Groups: (pars_raw, q, a_or_none, suffix)
_COORD_RE = re.compile(
    r"(I-II|II-II|III|I)\s+ot\.\s*(\d+)\s+(?:čl\.\s*(\d+)\s+(arg\.\s*\d+|protiarg\.|odp\.|k\s+\d+)|pr\.)"
)


@dataclass
class BahouněkElement:
    locator: str        # ltree locator (matches segment.locator_path)
    czech_text: str


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

def _extract_elements_from_file(html_path: Path) -> list[BahouněkElement]:
    """Extract all coordinate-tagged elements from one Bahounek HTML file."""
    content = html_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(content, "lxml")

    elements: list[BahouněkElement] = []
    full_text = soup.get_text(separator="\n")
    lines = full_text.splitlines()

    current_locator: str | None = None
    text_lines: list[str] = []

    def _flush():
        nonlocal current_locator, text_lines
        if current_locator and text_lines:
            text = " ".join(t.strip() for t in text_lines if t.strip())
            if text:
                elements.append(BahouněkElement(current_locator, text))
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


def parse_bahounek_for_articles(article_locators: list[str]) -> list[BahouněkElement]:
    """Parse Bahounek HTML for the given article locators and return matched elements."""
    # Determine which pars files are needed
    pars_needed: set[str] = set()
    for loc in article_locators:
        pars_ltree = loc.split(".")[0]
        pars_needed.add(pars_ltree)

    # Map ltree pars → raw pars (reverse of _PARS_CODE)
    ltree_to_raw = {v: k for k, v in _PARS_CODE.items()}

    # Collect all elements from needed pars files
    all_elements: dict[str, BahouněkElement] = {}  # locator → element
    for pars_ltree in sorted(pars_needed):
        pars_raw = ltree_to_raw[pars_ltree]
        html_path = BAHOUNEK_DIR / _PARS_FILE[pars_raw]
        if not html_path.exists():
            raise RuntimeError(
                f"FAIL: Bahounek file not found: {html_path}. "
                "Was M0 download completed?"
            )
        file_elements = _extract_elements_from_file(html_path)
        for elem in file_elements:
            all_elements[elem.locator] = elem

    # Filter to requested articles + their sub-elements
    result: list[BahouněkElement] = []
    for art_loc in article_locators:
        article_prefix = art_loc + "."
        for loc, elem in sorted(all_elements.items()):
            if loc.startswith(article_prefix) or loc == art_loc:
                result.append(elem)

    return result


# ── DB insertion ──────────────────────────────────────────────────────────────

def insert_bahounek_texts(
    conn,
    elements: list[BahouněkElement],
    src_id: int,
) -> int:
    """Insert segment_text(cs, bahounek) rows. Returns count inserted."""
    cur = conn.cursor()
    inserted = 0

    for elem in elements:
        # Look up the existing segment by locator_path
        cur.execute(
            "SELECT segment_id FROM segment WHERE locator_path = %s::ltree",
            (elem.locator,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"FAIL: Bahounek coordinate {elem.locator!r} has no matching segment. "
                "Run parser_latin.py first."
            )
        seg_id = row[0]
        cur.execute(
            """
            INSERT INTO segment_text (segment_id, lang, content, source_id)
            VALUES (%s, 'cs', %s, %s)
            ON CONFLICT (segment_id, lang, source_id) DO UPDATE SET content = EXCLUDED.content
            """,
            (seg_id, elem.czech_text, src_id),
        )
        inserted += 1

    cur.close()
    return inserted


# ── Spot-check ────────────────────────────────────────────────────────────────

_SPOT_CHECK_ARTICLES = ["I.q3.a1", "I.q13.a5", "I_II.q5.a1", "II_II.q23.a1", "III.q1.a1"]


def spot_check(elements: list[BahouněkElement]) -> None:
    """Print a sample of matched elements for manual verification."""
    print("\nSpot-check (5 articles):")
    shown: dict[str, int] = {}
    for elem in elements:
        art = ".".join(elem.locator.split(".")[:3])
        if art in _SPOT_CHECK_ARTICLES and shown.get(art, 0) < 2:
            print(f"  {elem.locator}: {elem.czech_text[:80]!r}")
            shown[art] = shown.get(art, 0) + 1


# ── Entry point ───────────────────────────────────────────────────────────────

def _articles_from_db() -> list[str]:
    """Return all article locators that have been inserted into segment."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT locator_path::text FROM segment WHERE element_type = 'article_title' ORDER BY locator_path"
            )
            return [row[0] for row in cur.fetchall()]


def run(articles: list[str] | None = None) -> None:
    target_articles = articles or _articles_from_db() or TEST_ARTICLES

    print("Parsing Bahounek HTML for test articles...")
    elements = parse_bahounek_for_articles(target_articles)
    print(f"  Found {len(elements)} Czech text elements")

    spot_check(elements)

    print("\nInserting into DB...")
    with get_conn() as conn:
        src = source_id(conn, "bahounek")
        count = insert_bahounek_texts(conn, elements, src)

    print(f"Done. {count} segment_text(cs, bahounek) rows inserted.")


if __name__ == "__main__":
    try:
        run()
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        sys.exit(1)
