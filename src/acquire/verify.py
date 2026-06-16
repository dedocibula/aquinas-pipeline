"""
Source acceptance checks — one function per source.

Each check returns True on pass, False on failure, and prints its own status
line. Crashes loudly only for unexpected exceptions; expected failures are
reported via _fail() and return False. The `CHECKS` list is consumed by
`acquire.steps.VerifySourcesStep`, which drives them as prerequisite step 0 of
the pipeline. Entry point: `python -m acquire.steps`.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from acquire.latin import MIN_ARTICLE_COUNT

ROOT = Path(__file__).resolve().parents[2]


def _ok(label: str, detail: str = "") -> None:
    msg = f"  [OK]  {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)


def _fail(label: str, detail: str) -> None:
    print(f"  [FAIL] {label} — {detail}", file=sys.stderr)


def check_latin() -> bool:
    import re

    from lxml import etree

    dest = ROOT / "sources" / "latin"
    html_files = list(dest.glob("sth*.html"))
    if not html_files:
        _fail("Latin (Corpus Thomisticum)", f"no sth*.html files in {dest}")
        return False

    articles: set[str] = set()
    for path in html_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        titles = re.findall(r'<P\s+TITLE="([^"]+)"', text, re.IGNORECASE)
        for t in titles:
            m = re.match(r"(.+ a\. \d+)", t)
            if m:
                articles.add(m.group(1))
        parser = etree.HTMLParser(recover=True)
        tree = etree.fromstring(text.encode("utf-8"), parser)
        if tree is None:
            _fail("Latin (Corpus Thomisticum)", f"lxml returned None for {path.name}")
            return False

    count = len(articles)
    if count < MIN_ARTICLE_COUNT:
        _fail(
            "Latin (Corpus Thomisticum)",
            f"article count {count} < {MIN_ARTICLE_COUNT}",
        )
        return False

    sample = html_files[len(html_files) // 2]
    text = sample.read_text(encoding="utf-8", errors="replace")
    titles = re.findall(r'<P\s+TITLE="([^"]+)"', text, re.IGNORECASE)
    types_seen: set[str] = set()
    for t in titles:
        if re.search(r" arg\. \d+$", t):
            types_seen.add("arg")
        elif re.search(r" s\. c\.$", t):
            types_seen.add("sed_contra")
        elif re.search(r" co\.$", t):
            types_seen.add("respondeo")
        elif re.search(r" ad (?:\d+|arg\.)$", t):
            types_seen.add("reply")
    missing_types = {"arg", "sed_contra", "respondeo", "reply"} - types_seen
    if missing_types:
        _fail(
            "Latin (Corpus Thomisticum)",
            f"sample {sample.name} missing element types: {missing_types}",
        )
        return False

    _ok("Latin (Corpus Thomisticum)", f"{len(html_files)} files, {count:,} articles")
    return True


def check_bahounek() -> bool:
    import re

    dest = ROOT / "sources" / "czech" / "bahounek"
    pars_checks = [
        ("pars_I.html", "I"),
        ("pars_I-II.html", "I-II"),
        ("pars_II-II.html", "II-II"),
        ("pars_III.html", "III"),
    ]
    expected = [f for f, _ in pars_checks]
    missing_files = [f for f in expected if not (dest / f).exists()]
    if missing_files:
        _fail("Bahounek Czech", f"missing files: {missing_files}")
        return False
    for filename, pars in pars_checks:
        text = (dest / filename).read_text(encoding="utf-8", errors="replace")
        pattern = re.compile(
            rf"{re.escape(pars)} ot\. \d+ čl\. \d+ (?:arg\. \d+|sc\.|co\.|ad \d+|k \d+|pr\.)"
        )
        if not pattern.search(text):
            _fail(
                "Bahounek Czech",
                f"coordinate tags for pars {pars!r} not found in {filename}",
            )
            return False

    _ok("Bahounek Czech", "4 partes present, coordinate tags confirmed")
    return True


def check_krystal() -> bool:
    from docx import Document

    dest = ROOT / "sources" / "czech" / "krystal"
    docx_files = list(dest.glob("*.docx"))
    if not docx_files:
        _fail("Krystal docx", f"no .docx files in {dest}")
        return False

    doc = Document(docx_files[0])
    para_count = len(doc.paragraphs)
    if para_count < 10:
        _fail("Krystal docx", f"only {para_count} paragraphs — file may be empty")
        return False

    _ok("Krystal docx", f"{docx_files[0].name}, {para_count:,} paragraphs")
    return True


def check_dominican() -> bool:
    from bs4 import BeautifulSoup

    dest = ROOT / "sources" / "english" / "dominican"
    html_files = list(dest.glob("*.htm")) + list(dest.glob("*.html"))
    if not html_files:
        _fail("Dominican English", f"no HTML files in {dest}")
        return False

    if len(html_files) < 614:
        _fail("Dominican English", f"only {len(html_files)} files (expected ≥ 614)")
        return False

    sample = html_files[0]
    soup = BeautifulSoup(
        sample.read_text(encoding="utf-8", errors="replace"), "html.parser"
    )
    if not soup.find("h2", id=lambda v: v and v.startswith("article")):
        _fail("Dominican English", f"sample {sample.name} has no article h2 — may be wrong page")
        return False

    _ok("Dominican English", f"{len(html_files)} files")
    return True


def check_freddoso() -> bool:
    dest = ROOT / "sources" / "english" / "freddoso"
    toc_files = list(dest.glob("TOC-*.html"))
    gaps_file = dest / "coverage_gaps.json"

    if not toc_files:
        _fail("Freddoso English", f"no TOC-*.html files in {dest}")
        return False
    if not gaps_file.exists():
        _fail("Freddoso English", f"coverage_gaps.json missing in {dest}")
        return False

    gaps = json.loads(gaps_file.read_text())
    missing_keys = {"available", "missing", "notes"} - gaps.keys()
    if missing_keys:
        _fail("Freddoso English", f"coverage_gaps.json missing keys: {missing_keys}")
        return False

    _ok(
        "Freddoso English",
        f"{len(toc_files)} TOC files, {len(gaps['available'])} questions available, "
        f"{len(gaps['missing'])} gaps",
    )
    return True


def check_db() -> bool:
    import psycopg2

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        _fail("Database", "DATABASE_URL not set")
        return False

    try:
        conn = psycopg2.connect(db_url)
    except Exception as exc:
        _fail("Database", f"connection failed: {exc}")
        return False

    with conn.cursor() as cur:
        cur.execute(
            "SELECT extname FROM pg_extension"
            " WHERE extname IN ('vector','ltree') ORDER BY extname"
        )
        found = {row[0] for row in cur.fetchall()}
    conn.close()

    missing_ext = {"vector", "ltree"} - found
    if missing_ext:
        _fail("Database", f"extensions not loaded: {missing_ext}")
        return False

    _ok("Database", "connected, vector + ltree extensions present")
    return True


def check_env() -> bool:
    required = ["DATABASE_URL", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"]
    missing = [k for k in required if k not in os.environ]
    if missing:
        _fail(".env", f"missing keys: {missing}")
        return False
    _ok(".env", "all required keys present")
    return True


CHECKS = [
    ("Latin (Corpus Thomisticum)", check_latin),
    ("Bahounek Czech", check_bahounek),
    ("Krystal docx", check_krystal),
    ("Dominican English", check_dominican),
    ("Freddoso English", check_freddoso),
    ("Database", check_db),
    (".env", check_env),
]
# The acceptance run lives in acquire.steps.VerifySourcesStep, which drives
# CHECKS through the pipeline Runner. Entry point: `python -m acquire.steps`.
