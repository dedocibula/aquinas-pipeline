"""
Latin parser for Corpus Thomisticum HTML files.

Parses the 10 test articles, writes:
  - segment rows (body + placeholder title rows)
  - segment_text(la, corpus_thomisticum) rows for body elements

Title segments (question_title, article_title) are created without Latin text;
the English ingest (Step 5) will supply the text from Dominican HTML.

HTML format: <P TITLE="I q. 3 a. 1 arg. 1"> — TITLE attribute encodes coordinate.

ltree label rules: only [A-Za-z0-9_] allowed.
  Pars mapping:   I → I,  I-II → I_II,  II-II → II_II,  III → III

Run:
  uv run python -m ingest.parser_latin
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from ingest.db import get_conn, source_id, work_id

ROOT = Path(__file__).resolve().parents[2]
LATIN_DIR = ROOT / "sources" / "latin"

# ── Test set ──────────────────────────────────────────────────────────────────

# Locators use ltree-safe pars codes (I_II, II_II).
# Short/long articles chosen by scanning corpus segment counts.
TEST_ARTICLES: list[str] = [
    "I.q3.a1",
    "I.q13.a5",
    "I_II.q5.a1",
    "I_II.q94.a2",
    "II_II.q23.a1",
    "II_II.q64.a7",
    "III.q1.a1",
    "III.q75.a4",
    # edge cases — determined by count pass at runtime (see _choose_edge_cases)
]

# ── TITLE attribute parsing ───────────────────────────────────────────────────

# Maps ltree-safe pars label → raw HTML pars prefix (with hyphen)
_PARS_RAW: dict[str, str] = {
    "I": "I",
    "I_II": "I-II",
    "II_II": "II-II",
    "III": "III",
}

# Reverse: raw HTML → ltree label
_PARS_CODE: dict[str, str] = {v: k for k, v in _PARS_RAW.items()}


def _parse_title(title: str) -> tuple[str, str] | None:
    """Parse a TITLE attribute into (locator_path, element_type).

    Returns None for unrecognised or preamble elements.
    Preamble ('pr.') is parsed separately — see _parse_title_full.
    """
    return _parse_title_full(title)


def _parse_title_full(title: str) -> tuple[str, str] | None:
    """Parse TITLE attribute → (locator_path_ltree, element_type) | None."""
    title = title.strip()

    # Detect pars prefix
    pars_raw = None
    for p in ("I-II", "II-II", "III", "I"):
        if title.startswith(p + " "):
            pars_raw = p
            rest = title[len(p):].strip()
            break
    if pars_raw is None:
        return None

    pars = _PARS_CODE[pars_raw]

    # Match: q. N pr.
    m = re.fullmatch(r"q\.\s*(\d+)\s+pr\.", rest)
    if m:
        q = m.group(1)
        return f"{pars}.q{q}.preamble", "preamble"

    # Match: q. N a. M ...
    m = re.match(r"q\.\s*(\d+)\s+a\.\s*(\d+)\s+(.*)", rest)
    if not m:
        return None
    q, a, suffix = m.group(1), m.group(2), m.group(3).strip()
    base = f"{pars}.q{q}.a{a}"

    # arg. K
    m2 = re.fullmatch(r"arg\.\s*(\d+)", suffix)
    if m2:
        return f"{base}.arg{m2.group(1)}", "arg"

    # s. c.  or  s. c. N  (some articles have multiple numbered sed_contra)
    # Both map to the same sed_contra locator; parser merges text on collision.
    if re.fullmatch(r"s\.\s*c\.(?:\s*\d+)?", suffix):
        return f"{base}.sed_contra", "sed_contra"

    # co.
    if suffix == "co.":
        return f"{base}.respondeo", "respondeo"

    # ad N
    m2 = re.fullmatch(r"ad\s+(\d+)", suffix)
    if m2:
        return f"{base}.reply{m2.group(1)}", "reply"

    # ad arg.  (combined-objection reply)
    if re.fullmatch(r"ad\s+arg\.", suffix):
        return f"{base}.reply0", "reply"

    return None


def _article_locator(title: str) -> str | None:
    """Return the article-level locator (e.g. 'I.q3.a1') for a TITLE string, or None."""
    parsed = _parse_title_full(title)
    if parsed is None:
        return None
    locator, etype = parsed
    if etype == "preamble":
        return None
    # strip the last label to get the article locator
    parts = locator.split(".")
    if len(parts) < 4:
        return None
    return ".".join(parts[:3])  # pars.qN.aM


def _question_locator(title: str) -> str | None:
    """Return question-level locator (e.g. 'I.q3') or None."""
    pars_raw = None
    for p in ("I-II", "II-II", "III", "I"):
        if title.strip().startswith(p + " "):
            pars_raw = p
            rest = title.strip()[len(p):].strip()
            break
    if pars_raw is None:
        return None
    m = re.match(r"q\.\s*(\d+)", rest)
    if not m:
        return None
    return f"{_PARS_CODE[pars_raw]}.q{m.group(1)}"


# ── HTML parsing ──────────────────────────────────────────────────────────────

@dataclass
class ParsedElement:
    locator: str
    element_type: str
    latin_text: str
    reply_number: int | None  # N from "ad N" / "reply N"; None otherwise


def _extract_text(p_tag) -> str:
    """Return clean Latin text from a <P> tag, stripping the SPAN.ref prefix."""
    # Remove the SPAN.ref child (contains "[28321] I&ordf; q. 3 pr. ")
    for span in p_tag.find_all("span", class_="ref"):
        span.decompose()
    return p_tag.get_text(separator=" ", strip=True)


def parse_html_file(html_path: Path) -> list[ParsedElement]:
    """Parse all recognisable structural P elements from one CT HTML file.

    Multiple sed_contra paragraphs (s. c. 1, s. c. 2, …) share the same locator;
    their texts are concatenated into one ParsedElement.
    """
    content = html_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(content, "lxml")

    # Use an ordered dict to merge same-locator entries (e.g. multiple sed_contra)
    seen: dict[str, ParsedElement] = {}

    for p in soup.find_all("p"):
        title_attr = p.get("title", "").strip()
        if not title_attr:
            title_attr = p.get("TITLE", "").strip()
        if not title_attr:
            continue

        parsed = _parse_title_full(title_attr)
        if parsed is None:
            continue

        locator, etype = parsed
        text = _extract_text(p)

        reply_num = None
        if etype == "reply":
            m = re.search(r"reply(\d+)$", locator)
            reply_num = int(m.group(1)) if m else None

        if locator in seen:
            # Merge: append text (handles numbered s. c. 1, s. c. 2, etc.)
            existing = seen[locator]
            seen[locator] = ParsedElement(
                locator, etype, existing.latin_text + " " + text, reply_num
            )
        else:
            seen[locator] = ParsedElement(locator, etype, text, reply_num)

    return list(seen.values())


# ── Article completeness check ────────────────────────────────────────────────

_REQUIRED_ETYPES = {"arg", "sed_contra", "respondeo", "reply"}


def _check_article(locator: str, elements: list[ParsedElement]) -> None:
    """Crash loudly if the article is structurally incomplete."""
    etypes = {e.element_type for e in elements}
    missing = _REQUIRED_ETYPES - etypes
    if missing:
        raise RuntimeError(
            f"FAIL: article {locator!r} is missing structural elements: {sorted(missing)}"
        )


# ── Edge-case selection ───────────────────────────────────────────────────────

def _choose_edge_cases(all_elements: dict[str, list[ParsedElement]]) -> tuple[str, str]:
    """Return (shortest_locator, longest_locator) based on segment count."""
    body_types = {"arg", "sed_contra", "respondeo", "reply"}
    counts = {
        loc: sum(1 for e in elems if e.element_type in body_types)
        for loc, elems in all_elements.items()
    }
    shortest = min(counts, key=counts.__getitem__)
    longest = max(counts, key=counts.__getitem__)
    return shortest, longest


# ── DB insertion ─────────────────────────────────────────────────────────────

def _insert_article(
    conn,
    article_locator: str,
    elements: list[ParsedElement],
    work_id_val: int,
    src_id: int,
) -> None:
    """Insert segment + segment_text rows for one article. Idempotent."""
    cur = conn.cursor()

    # Wipe existing rows for this article (idempotency)
    cur.execute(
        """
        DELETE FROM segment_text
        WHERE segment_id IN (
            SELECT segment_id FROM segment
            WHERE locator_path <@ %s::ltree AND work_id = %s
        )
        """,
        (article_locator, work_id_val),
    )
    cur.execute(
        "DELETE FROM segment WHERE locator_path <@ %s::ltree AND work_id = %s",
        (article_locator, work_id_val),
    )

    # Create placeholder title segments for this article (no text)
    q_locator = ".".join(article_locator.split(".")[:2])  # e.g. I.q3

    for title_locator, title_etype in [
        (q_locator, "question_title"),
        (article_locator, "article_title"),
    ]:
        # Only insert if not already present (shared across articles in same question)
        cur.execute(
            "SELECT 1 FROM segment WHERE locator_path = %s::ltree AND work_id = %s",
            (title_locator, work_id_val),
        )
        if cur.fetchone() is None:
            cur.execute(
                """
                INSERT INTO segment (work_id, locator_path, element_type)
                VALUES (%s, %s::ltree, %s)
                """,
                (work_id_val, title_locator, title_etype),
            )

    # Map locator → segment_id for reply_to linking
    locator_to_id: dict[str, int] = {}

    # Insert body segments; collect segment_ids
    for elem in elements:
        cur.execute(
            """
            INSERT INTO segment (work_id, locator_path, element_type)
            VALUES (%s, %s::ltree, %s)
            RETURNING segment_id
            """,
            (work_id_val, elem.locator, elem.element_type),
        )
        seg_id = cur.fetchone()[0]
        locator_to_id[elem.locator] = seg_id

    # Set reply_to on reply segments
    for elem in elements:
        if elem.element_type != "reply" or elem.reply_number is None:
            continue
        if elem.reply_number == 0:
            continue  # combined-reply (ad arg.) — no specific arg to link
        base = ".".join(elem.locator.split(".")[:-1])  # strip .replyN
        arg_locator = f"{base}.arg{elem.reply_number}"
        arg_seg_id = locator_to_id.get(arg_locator)
        if arg_seg_id is None:
            # Some articles have extra replies with no matching objection
            # (Aquinas replies to an implied or extended objection). Leave reply_to NULL.
            print(
                f"  NOTE: {elem.locator!r} has no matching {arg_locator!r} — reply_to left NULL",
                flush=True,
            )
            continue
        cur.execute(
            "UPDATE segment SET reply_to = %s WHERE segment_id = %s",
            (arg_seg_id, locator_to_id[elem.locator]),
        )

    # Insert segment_text (Latin) for body elements
    for elem in elements:
        if not elem.latin_text:
            continue
        cur.execute(
            """
            INSERT INTO segment_text (segment_id, lang, content, source_id)
            VALUES (%s, 'la', %s, %s)
            ON CONFLICT (segment_id, lang, source_id) DO UPDATE SET content = EXCLUDED.content
            """,
            (locator_to_id[elem.locator], elem.latin_text, src_id),
        )

    cur.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def _article_to_html_locator(article_locator: str) -> tuple[str, int, int]:
    """Return (pars_raw, q_num, a_num) from an ltree article locator."""
    parts = article_locator.split(".")
    pars_ltree = parts[0]
    pars_raw = _PARS_RAW[pars_ltree]
    q_num = int(parts[1][1:])   # 'q3' → 3
    a_num = int(parts[2][1:])   # 'a1' → 1
    return pars_raw, q_num, a_num


def _load_article_elements(
    pars_raw: str, q_num: int, a_num: int
) -> tuple[list[ParsedElement], Path]:
    """Scan LATIN_DIR for the HTML file containing this article and parse it."""
    # Build expected TITLE prefix to match
    title_prefix = f"{pars_raw} q. {q_num} a. {a_num} "

    for html_file in sorted(LATIN_DIR.glob("sth*.html")):
        if html_file.stem == "sth0000":
            continue  # index file
        content = html_file.read_text(encoding="utf-8", errors="replace")
        if title_prefix not in content:
            continue
        elements = parse_html_file(html_file)
        pars_code = _PARS_CODE[pars_raw]
        article_locator = f"{pars_code}.q{q_num}.a{a_num}"
        article_elems = [
            e for e in elements
            if e.locator.startswith(article_locator + ".") and e.element_type != "question_title"
        ]
        if article_elems:
            return article_elems, html_file

    raise RuntimeError(
        f"FAIL: no HTML file in {LATIN_DIR} contains article "
        f"'{pars_raw} q. {q_num} a. {a_num}'"
    )


def run(articles: list[str] | None = None) -> None:
    """Parse and insert the test articles. Crashes loudly on any anomaly."""
    target_articles = articles or TEST_ARTICLES

    # Collect all article elements first (for edge-case selection)
    all_elements: dict[str, list[ParsedElement]] = {}
    for locator in target_articles:
        pars_raw, q_num, a_num = _article_to_html_locator(locator)
        elems, html_file = _load_article_elements(pars_raw, q_num, a_num)
        _check_article(locator, elems)
        body_count = sum(1 for e in elems if e.element_type in {"arg", "sed_contra", "respondeo", "reply"})
        print(f"  {locator}: {body_count} body segments  [{html_file.name}]")
        all_elements[locator] = elems

    # Select edge cases if not already in the set
    if articles is None:
        print("\nSelecting edge-case articles from full corpus...")
        # Quick count across all files to find extreme articles
        all_counts: dict[str, int] = {}
        for html_file in sorted(LATIN_DIR.glob("sth*.html")):
            if html_file.stem == "sth0000":
                continue
            file_elems = parse_html_file(html_file)
            # Group by article locator
            grouped: dict[str, list] = {}
            for e in file_elems:
                parts = e.locator.split(".")
                # Only group proper article locators (3 parts: pars.qN.aM)
                if len(parts) < 3 or not parts[2].startswith("a"):
                    continue
                art = ".".join(parts[:3])
                if e.element_type in {"arg", "sed_contra", "respondeo", "reply"}:
                    grouped.setdefault(art, []).append(e)
            for art_loc, art_elems in grouped.items():
                body = len(art_elems)
                # Only count articles with at least one of each required type
                etypes = {e.element_type for e in art_elems}
                if {"arg", "sed_contra", "respondeo", "reply"}.issubset(etypes):
                    all_counts[art_loc] = body
        # Exclude already selected articles; prefer complete articles with ≥4 segments
        remaining = {k: v for k, v in all_counts.items() if k not in all_elements}
        if remaining:
            short_loc = min(remaining, key=remaining.__getitem__)
            long_loc = max(remaining, key=remaining.__getitem__)
            print(f"  Short: {short_loc} ({remaining[short_loc]} body segments)")
            print(f"  Long:  {long_loc} ({remaining[long_loc]} body segments)")
            for loc in (short_loc, long_loc):
                pars_raw, q_num, a_num = _article_to_html_locator(loc)
                elems, html_file = _load_article_elements(pars_raw, q_num, a_num)
                _check_article(loc, elems)
                all_elements[loc] = elems

    print(f"\nInserting {len(all_elements)} articles...")
    with get_conn() as conn:
        wid = work_id(conn, "summa_articulus")
        src = source_id(conn, "corpus_thomisticum")
        for locator, elems in all_elements.items():
            _insert_article(conn, locator, elems, wid, src)
            print(f"  ✓ {locator}")

    print(f"\nDone. {len(all_elements)} articles inserted.")


if __name__ == "__main__":
    print("Parsing Corpus Thomisticum HTML for test articles...")
    print()
    print("Article segment counts:")
    try:
        run()
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        sys.exit(1)
