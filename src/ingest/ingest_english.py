"""
English text ingest from Dominican Province HTML files.

Writes segment_text(en, dominican) rows for the 10 test articles,
including question_title and article_title segments.

File naming: sources/english/dominican/{pars_digit}{question:03d}.html
  pars_digit: I→1, I_II→2, II_II→3, III→4

Structural markers (strong tags in paragraph text):
  "Objection N."          → arg N
  "On the contrary,"      → sed_contra
  "I answer that,"        → respondeo
  "Reply to Objection N." → reply N

NO_SEGMENT skips: if English has more args/replies than Latin for an article,
the extra locators are logged to m2_english_gaps.txt and skipped.

Run:
  uv run python -m ingest.ingest_english
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from ingest.db import get_conn, source_id
from ingest.parser_latin import TEST_ARTICLES

ROOT = Path(__file__).resolve().parents[2]
DOMINICAN_DIR = ROOT / "sources" / "english" / "dominican"
FREDDOSO_DIR = ROOT / "sources" / "english" / "freddoso"
FREDDOSO_GAPS = FREDDOSO_DIR / "coverage_gaps.json"

_PARS_DIGIT: dict[str, int] = {
    "I": 1,
    "I_II": 2,
    "II_II": 3,
    "III": 4,
}

# Questions for which Freddoso article HTML files are available (loaded lazily)
_FREDDOSO_AVAILABLE: set[str] | None = None


def _load_freddoso_available() -> set[str]:
    global _FREDDOSO_AVAILABLE
    if _FREDDOSO_AVAILABLE is None:
        if FREDDOSO_GAPS.exists():
            import json
            data = json.loads(FREDDOSO_GAPS.read_text(encoding="utf-8"))
            _FREDDOSO_AVAILABLE = set(data.get("available", []))
        else:
            _FREDDOSO_AVAILABLE = set()
    return _FREDDOSO_AVAILABLE


def _freddoso_file(pars_ltree: str, q_num: int) -> Path:
    """Return expected Freddoso per-question HTML path (may not exist)."""
    digit = _PARS_DIGIT[pars_ltree]
    return FREDDOSO_DIR / f"{digit}{q_num:03d}.html"


# ── Parsing ───────────────────────────────────────────────────────────────────

@dataclass
class EnglishElement:
    locator: str
    english_text: str


def _question_file(pars_ltree: str, q_num: int) -> Path:
    digit = _PARS_DIGIT[pars_ltree]
    return DOMINICAN_DIR / f"{digit}{q_num:03d}.html"


def _strip_strong_prefix(p_tag: Tag) -> str:
    """Return text of a <p> tag with its leading <strong> text removed."""
    strong = p_tag.find("strong")
    if strong:
        strong.decompose()
    return p_tag.get_text(separator=" ", strip=True)


def _parse_article(
    soup: BeautifulSoup,
    pars_ltree: str,
    q_num: int,
    a_num: int,
) -> list[EnglishElement]:
    """Extract English elements for one article from an already-parsed soup."""
    elements: list[EnglishElement] = []
    q_loc = f"{pars_ltree}.q{q_num}"
    a_loc = f"{q_loc}.a{a_num}"

    div = soup.find("div", id="springfield2")
    if div is None:
        raise RuntimeError(
            f"FAIL: #springfield2 not found in Dominican HTML for {pars_ltree}.q{q_num}"
        )

    # ── Question title ────────────────────────────────────────────────────────
    h1 = div.find("h1") or soup.find("h1")
    if h1:
        raw_title = h1.get_text(strip=True)
        # Strip "Question N. " prefix
        title_text = re.sub(r"^Question\s+\d+\.\s*", "", raw_title, flags=re.I)
        elements.append(EnglishElement(q_loc, title_text))

    # ── Locate the article's <h2> ─────────────────────────────────────────────
    article_h2 = div.find("h2", id=f"article{a_num}")
    if article_h2 is None:
        raise RuntimeError(
            f"FAIL: <h2 id='article{a_num}'> not found in Dominican HTML "
            f"for {a_loc}"
        )

    # Article title
    raw_art_title = article_h2.get_text(strip=True)
    art_title_text = re.sub(r"^Article\s+\d+\.\s*", "", raw_art_title, flags=re.I)
    elements.append(EnglishElement(a_loc, art_title_text))

    # ── Collect body paragraphs until the next article or end ─────────────────
    next_h2 = article_h2.find_next_sibling("h2")

    def _in_article(tag) -> bool:
        if next_h2 is None:
            return True
        try:
            # tag comes before next_h2 in document order
            for sibling in tag.next_siblings:
                if sibling is next_h2:
                    return True
            return False
        except Exception:
            return True

    for p in article_h2.find_next_siblings("p"):
        if next_h2 and p.find_previous_sibling("h2") is not article_h2:
            break
        # Only stop when we actually hit the next article's territory
        if next_h2 and next_h2 in list(p.previous_siblings):
            break
        strong = p.find("strong")
        if not strong:
            continue
        marker = strong.get_text(strip=True)
        text = _strip_strong_prefix(p)

        # Objection N.
        m = re.fullmatch(r"Objection\s+(\d+)\.", marker)
        if m:
            elements.append(EnglishElement(f"{a_loc}.arg{m.group(1)}", text))
            continue

        # On the contrary,
        if marker.lower().startswith("on the contrary"):
            elements.append(EnglishElement(f"{a_loc}.sed_contra", text))
            continue

        # I answer that,
        if marker.lower().startswith("i answer that"):
            elements.append(EnglishElement(f"{a_loc}.respondeo", text))
            continue

        # Reply to Objection N.
        m = re.fullmatch(r"Reply to Objection\s+(\d+)\.", marker)
        if m:
            elements.append(EnglishElement(f"{a_loc}.reply{m.group(1)}", text))
            continue

    return elements


def parse_english_for_articles(
    article_locators: list[str],
    coverage_gap_log: list[str] | None = None,
) -> list[EnglishElement]:
    """Parse English HTML for the given article locators.

    Uses Freddoso where a per-question HTML file is available, Dominican Province
    otherwise. Appends coverage gap notes to coverage_gap_log if provided.
    """
    from collections import defaultdict

    freddoso_available = _load_freddoso_available()

    by_file: dict[tuple[str, int], list[int]] = defaultdict(list)
    for loc in article_locators:
        parts = loc.split(".")
        pars_ltree = parts[0]
        q_num = int(parts[1][1:])
        a_num = int(parts[2][1:])
        by_file[(pars_ltree, q_num)].append(a_num)

    all_elements: list[EnglishElement] = []
    seen_question_titles: set[str] = set()

    for (pars_ltree, q_num), article_nums in sorted(by_file.items()):
        q_loc = f"{pars_ltree}.q{q_num}"
        q_key = q_loc.replace("_", "-")  # coverage_gaps.json uses "I-II.q1" style

        # Prefer Freddoso if article file exists; fall back to Dominican
        freddoso_path = _freddoso_file(pars_ltree, q_num)
        if freddoso_path.exists():
            html_path = freddoso_path
            src_label = "freddoso"
        else:
            html_path = _question_file(pars_ltree, q_num)
            src_label = "dominican"
            # Log coverage gap if Freddoso was listed as available but file isn't present
            if coverage_gap_log is not None and q_key in freddoso_available:
                coverage_gap_log.append(
                    f"[FREDDOSO_MISSING] {q_loc} listed in coverage_gaps.json but "
                    f"file not found; fell back to Dominican"
                )

        if not html_path.exists():
            print(f"  [SKIP] missing {src_label} file: {html_path.name}", flush=True)
            if coverage_gap_log is not None:
                coverage_gap_log.append(f"[NO_ENGLISH] {q_loc} no {src_label} file available")
            continue
        content = html_path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(content, "lxml")

        for a_num in sorted(article_nums):
            elems = _parse_article(soup, pars_ltree, q_num, a_num)
            for elem in elems:
                if elem.locator == q_loc:
                    if q_loc in seen_question_titles:
                        continue
                    seen_question_titles.add(q_loc)
                all_elements.append(elem)

    return all_elements


# ── DB insertion ──────────────────────────────────────────────────────────────

def insert_english_texts(
    conn,
    elements: list[EnglishElement],
    src_id: int,
    gap_log: list[str] | None = None,
) -> int:
    """Insert segment_text(en, dominican) rows. Returns count inserted.

    Elements whose locator has no matching segment row (English has more args than
    Latin for some articles) are logged to gap_log and skipped.
    """
    cur = conn.cursor()
    inserted = 0

    for elem in elements:
        cur.execute(
            "SELECT segment_id FROM segment WHERE locator_path = %s::ltree",
            (elem.locator,),
        )
        row = cur.fetchone()
        if row is None:
            if gap_log is not None:
                gap_log.append(f"[NO_SEGMENT] locator={elem.locator}")
            continue
        seg_id = row[0]
        cur.execute(
            """
            INSERT INTO segment_text (segment_id, lang, content, source_id)
            VALUES (%s, 'en', %s, %s)
            ON CONFLICT (segment_id, lang, source_id) DO UPDATE SET content = EXCLUDED.content
            """,
            (seg_id, elem.english_text, src_id),
        )
        inserted += 1

    cur.close()
    return inserted


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

    print("Parsing English (Freddoso-first, Dominican fallback) for articles...")
    coverage_gaps: list[str] = []
    elements = parse_english_for_articles(target_articles, coverage_gap_log=coverage_gaps)
    print(f"  Found {len(elements)} English text elements")

    if coverage_gaps:
        gap_log_path = ROOT / "reports" / "m2_english_gaps.txt"
        gap_log_path.parent.mkdir(parents=True, exist_ok=True)
        gap_log_path.write_text("\n".join(coverage_gaps) + "\n", encoding="utf-8")
        print(f"  {len(coverage_gaps)} coverage gaps → {gap_log_path}")

    # Spot-check titles
    print("\nTitle spot-check:")
    for elem in elements:
        if elem.locator.count(".") <= 1:  # question_title or article_title
            print(f"  {elem.locator}: {elem.english_text[:70]!r}")

    print("\nInserting into DB...")
    insert_gaps: list[str] = []
    with get_conn() as conn:
        src = source_id(conn, "dominican")
        count = insert_english_texts(conn, elements, src, gap_log=insert_gaps)

    if insert_gaps:
        gap_log_path = ROOT / "reports" / "m2_english_gaps.txt"
        gap_log_path.parent.mkdir(parents=True, exist_ok=True)
        with gap_log_path.open("a", encoding="utf-8") as f:
            for line in insert_gaps:
                f.write(line + "\n")
        print(f"  {len(insert_gaps)} no-segment skips appended → {gap_log_path}")

    print(f"Done. {count} segment_text(en, dominican) rows inserted.")


if __name__ == "__main__":
    try:
        run()
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        sys.exit(1)
