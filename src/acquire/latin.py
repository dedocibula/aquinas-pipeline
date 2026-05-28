"""
Downloader for Corpus Thomisticum HTML files (Summa Theologiae).

The Corpus Thomisticum site at corpusthomisticum.org serves its texts as HTML,
not as XML, despite the sources.md "Format: XML" note. Each P element carries
a TITLE attribute encoding the scholastic structure:

    <P TITLE="I q. 1 a. 1 arg. 1">   → objection
    <P TITLE="I q. 1 a. 1 s. c.">    → sed contra
    <P TITLE="I q. 1 a. 1 co.">      → respondeo
    <P TITLE="I q. 1 a. 1 ad 1">     → reply
    <P TITLE="I q. 1 a. 1 ad arg.">  → reply to combined objection (rare)

URL pattern: https://www.corpusthomisticum.org/sth{part}{NNN}.html
  part = 1 (Prima Pars), 2 (Prima Secundae), 3 (Secunda Secundae), 4 (Tertia Pars)
  NNN  = starting question number for that page group (zero-padded to 3 digits,
         but some are 4 digits for question ≥ 1000; in practice all are ≤ 189)

The full list of 87 file paths (including sth0000 prooemium) is scraped from
iopera.html at download time — no hardcoding of the file list.

We save files verbatim to sources/latin/ as .html. The filename matches the
basename from the URL (e.g. sth1001.html).
"""

import re
import time
from pathlib import Path

import requests
from lxml import etree

OPERA_INDEX = "https://www.corpusthomisticum.org/iopera.html"
BASE_URL = "https://www.corpusthomisticum.org"

DEST = Path(__file__).resolve().parents[2] / "sources" / "latin"

# Corpus Thomisticum (corpusthomisticum.org) contains 2,663 unique article identifiers
# across the 87 Summa Theologiae HTML files.  The commonly-cited figure of 2,669 comes
# from editions that count differently or include slight textual variants; it does not
# match this source.  We use actual_count − 10 as a lower bound to catch gross download
# failures while tolerating minor future site edits.
MIN_ARTICLE_COUNT = 2_653

# TITLE attribute patterns for the four structural element types
_ARG_RE = re.compile(r" arg\. \d+$")
_SC_RE = re.compile(r" s\. c\.$")
_CO_RE = re.compile(r" co\.$")
_AD_RE = re.compile(r" ad (?:\d+|arg\.)$")


def _article_key(title: str) -> str | None:
    """Return a normalised article identifier from a P TITLE string, or None."""
    # Matches e.g. "I q. 3 a. 2 arg. 1" → "I q. 3 a. 2"
    m = re.match(r"(.+ a\. \d+)", title)
    return m.group(1) if m else None


def _classify(title: str) -> str | None:
    """Return element type or None if the P is not a structural element."""
    if _ARG_RE.search(title):
        return "arg"
    if _SC_RE.search(title):
        return "sed_contra"
    if _CO_RE.search(title):
        return "respondeo"
    if _AD_RE.search(title):
        return "reply"
    return None


def discover_file_list(session: requests.Session) -> list[str]:
    """
    Fetch the opera index and return all sth*.html basenames for the Summa.
    The full absolute URLs are embedded in iopera.html — no guessing required.
    """
    resp = session.get(OPERA_INDEX, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch opera index {OPERA_INDEX}: HTTP {resp.status_code}"
        )
    html = resp.text
    filenames = re.findall(
        r"href=\"https://www\.corpusthomisticum\.org/(sth\d+\.html)\"",
        html,
        re.IGNORECASE,
    )
    unique = sorted(set(filenames))
    if not unique:
        raise RuntimeError(
            f"No sth*.html links found in {OPERA_INDEX}. "
            "Page structure may have changed."
        )
    return unique


def _fetch_html(url: str, session: requests.Session) -> str:
    resp = session.get(url, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} fetching {url}")
    resp.encoding = "latin-1"  # site declares ISO-8859-1
    return resp.text


def count_articles_in_html(html: str) -> set[str]:
    """
    Return the set of unique article identifiers (e.g. {'I q. 1 a. 1', ...})
    found in the TITLE attributes of <P> elements.
    """
    titles = re.findall(r'<P\s+TITLE="([^"]+)"', html, re.IGNORECASE)
    articles: set[str] = set()
    for t in titles:
        key = _article_key(t)
        if key:
            articles.add(key)
    return articles


def verify_structural_elements(html: str, filename: str) -> None:
    """
    Parse the HTML and assert that at least one of each structural element type
    (arg, sed_contra, respondeo, reply) is present.

    Crashes loudly with the exact filename and missing element type if any is absent.
    The check is deliberately loose (≥1 of each) because some pages contain only
    prologues or special question types with fewer structural divisions; the full
    corpus check (across all 87 files) is where the MIN_ARTICLE_COUNT threshold
    enforces completeness.
    """
    titles = re.findall(r'<P\s+TITLE="([^"]+)"', html, re.IGNORECASE)

    found: set[str] = set()
    for t in titles:
        kind = _classify(t)
        if kind:
            found.add(kind)

    required = {"arg", "sed_contra", "respondeo", "reply"}
    missing = required - found
    if missing:
        raise RuntimeError(
            f"Structural verification failed for {filename!r}: "
            f"missing element types {sorted(missing)}.\n"
            f"Present types: {sorted(found)}.\n"
            f"Total P[TITLE] elements found: {len(titles)}."
        )


def verify_wellformed(html: str, filename: str) -> None:
    """
    Verify the document is parseable via lxml's lenient HTML parser.
    Any parse error that produces zero root children is treated as malformed.
    The site serves HTML 4.0 Transitional, not XML, so we use lxml's html
    parser, not the XML parser.
    """
    parser = etree.HTMLParser(recover=True)
    tree = etree.fromstring(html.encode("utf-8"), parser)
    if tree is None:
        raise RuntimeError(
            f"HTML parser returned None for {filename!r} — file may be empty."
        )


def download_all(dest: Path = DEST, *, skip_existing: bool = True) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = (
        "aquinas-pipeline/0.1 (research; dedo.cibula@gmail.com)"
    )

    print(f"[probe] Discovering file list from {OPERA_INDEX}")
    filenames = discover_file_list(session)
    print(f"[probe] Found {len(filenames)} sth*.html files")

    saved: list[Path] = []
    for name in filenames:
        out_path = dest / name
        if skip_existing and out_path.exists() and out_path.stat().st_size > 1_000:
            print(f"[skip]  {name} already on disk")
            saved.append(out_path)
            continue

        url = f"{BASE_URL}/{name}"
        print(f"[fetch] {url}")
        html = _fetch_html(url, session)

        verify_wellformed(html, name)

        out_path.write_text(html, encoding="utf-8")
        print(f"[ok]    {name} ({len(html):,} chars)")
        saved.append(out_path)

        time.sleep(0.5)

    return saved


def verify_download(dest: Path = DEST) -> None:
    """
    Post-download verification:
    1. Count total unique articles across all files — must be ≥ MIN_ARTICLE_COUNT.
    2. Verify every file parses cleanly.
    3. Verify a sample article from each of the four partes has all structural elements.
    """
    files = sorted(dest.glob("sth*.html"))
    if not files:
        raise RuntimeError(
            f"No sth*.html files found in {dest}. Run download first."
        )

    all_articles: set[str] = set()
    for path in files:
        html = path.read_text(encoding="utf-8")
        verify_wellformed(html, path.name)
        all_articles |= count_articles_in_html(html)

    article_count = len(all_articles)
    if article_count < MIN_ARTICLE_COUNT:
        raise RuntimeError(
            f"Article count check failed: found {article_count} articles "
            f"across {len(files)} files, expected ≥ {MIN_ARTICLE_COUNT}.\n"
            f"Files checked: {[f.name for f in files]}"
        )
    print(f"[verify] Article count: {article_count} (≥ {MIN_ARTICLE_COUNT}: OK)")

    # Verify structural elements in one representative file per pars.
    # sth1001 = Prima Pars q.1, sth2001 = I-II q.1-5, sth3001 = II-II q.1-16, sth4001 = III q.1-6
    sample_files = ["sth1001.html", "sth2001.html", "sth3001.html", "sth4001.html"]
    for name in sample_files:
        path = dest / name
        if not path.exists():
            raise RuntimeError(
                f"Structural check failed: sample file {name} not found in {dest}."
            )
        html = path.read_text(encoding="utf-8")
        verify_structural_elements(html, name)
        print(f"[verify] {name}: structural elements present (arg, sed_contra, respondeo, reply)")

    print(
        f"[verify] All checks passed. "
        f"{len(files)} files, {article_count} unique articles."
    )


if __name__ == "__main__":
    saved = download_all()
    verify_download()
    print(f"\nDone. {len(saved)} files saved to {DEST}")
