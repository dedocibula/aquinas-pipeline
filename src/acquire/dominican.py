"""
Acquire the Dominican Province English translation of the Summa Theologiae
from newadvent.org.

Saves one HTML file per question page to sources/english/dominican/, named by
the four-digit article code used in newadvent.org's URL scheme (e.g. 1001.html
for Prima Pars Q.1).

URL scheme discovered by probing newadvent.org/summa/:
  Prefix 1 (1001–1119) → Prima Pars           (119 questions)
  Prefix 2 (2001–2114) → Prima-Secundae        (114 questions)
  Prefix 3 (3001–3189) → Secunda-Secundae      (189 questions)
  Prefix 4 (4001–4090) → Tertia Pars           (90 questions)
  Prefix 5 (5001–5099) → Supplementum          (99 questions)
  Prefix 6 (6001–6002) → Supplementum Appendix I  (2 questions)
  Prefix 7 (7001)      → Supplementum Appendix II (1 question)

One page may cover an entire question (all articles in that question).
The "article code" in the URL is really a question-level code; multiple
articles within a question share the same page.

Run:
    uv run python src/acquire/dominican.py
"""
from __future__ import annotations

import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.newadvent.org/summa"

# Maps a human-readable pars name to the contiguous range of four-digit codes
# assigned by newadvent.org.  Appendix I and II are part of the Supplementum
# section on the site (linked from 5.htm) and are included for completeness.
PARS_CODES: dict[str, range] = {
    "Prima Pars":              range(1001, 1120),   # 119 questions
    "Prima-Secundae":          range(2001, 2115),   # 114 questions
    "Secunda-Secundae":        range(3001, 3190),   # 189 questions
    "Tertia Pars":             range(4001, 4091),   # 90 questions
    "Supplementum":            range(5001, 5100),   # 99 questions
    "Supplementum Appendix I": range(6001, 6003),   # 2 questions
    "Supplementum Appendix II":range(7001, 7002),   # 1 question
}

DEST = Path(__file__).resolve().parents[2] / "sources" / "english" / "dominican"
SLEEP = 0.5

# The body's id attribute is set to the page filename (e.g. "1001.htm") on
# genuine article pages.  Pages that 404-redirect to the home page carry a
# different body id.  This is the authoritative structural check.
BODY_CLASS = "summa"


def _get(url: str) -> requests.Response:
    resp = requests.get(url, timeout=30, headers={"User-Agent": "aquinas-pipeline/0.1 (research)"})
    if resp.status_code != 200:
        raise RuntimeError(
            f"Unexpected HTTP {resp.status_code} fetching {url!r}"
        )
    return resp


def _assert_article_page(html: str, url: str, code: int) -> BeautifulSoup:
    """
    Parse HTML and verify it is a genuine Summa article page for `code`.

    Raises RuntimeError with the exact URL and anomaly on any structural
    deviation.  Returns the parsed soup for further use.
    """
    soup = BeautifulSoup(html, "html.parser")

    body = soup.find("body")
    if body is None:
        raise RuntimeError(
            f"Structural anomaly at {url!r}: no <body> tag found"
        )

    body_id = body.get("id", "")
    expected_id = f"{code}.htm"
    if body_id != expected_id:
        raise RuntimeError(
            f"Structural anomaly at {url!r}: expected body id={expected_id!r}, "
            f"got {body_id!r} — page may be a redirect to the home page"
        )

    body_classes = body.get("class", [])
    if BODY_CLASS not in body_classes:
        raise RuntimeError(
            f"Structural anomaly at {url!r}: expected body class to include "
            f"{BODY_CLASS!r}, got {body_classes!r}"
        )

    main = soup.find("div", id="springfield2")
    if main is None:
        raise RuntimeError(
            f"Structural anomaly at {url!r}: main content div #springfield2 not found"
        )

    # At least one <h2 id="articleN"> must be present inside the content div.
    first_article = main.find("h2", id=lambda x: x and x.startswith("article"))
    if first_article is None:
        raise RuntimeError(
            f"Structural anomaly at {url!r}: no <h2 id='articleN'> found "
            f"inside #springfield2"
        )

    return soup


def all_codes() -> list[int]:
    """Return all question codes in canonical pars order."""
    codes: list[int] = []
    for pars_range in PARS_CODES.values():
        codes.extend(pars_range)
    return codes


def download_question(code: int, dest: Path) -> Path:
    """
    Download the newadvent.org page for `code`, verify its structure, save it
    to `dest/<code>.html`, and return the saved path.

    Raises RuntimeError on any HTTP or structural anomaly.
    """
    url = f"{BASE_URL}/{code}.htm"
    resp = _get(url)
    _assert_article_page(resp.text, url, code)

    out = dest / f"{code}.html"
    out.write_text(resp.text, encoding="utf-8")
    return out


def verify_coverage(dest: Path) -> dict[str, object]:
    """
    Check which question pages are already present on disk.

    Returns a dict with keys:
      present      – sorted list of codes present on disk
      missing      – sorted list of codes absent
      by_pars      – per-pars coverage summary strings
      complete     – True when no codes are missing
    """
    present: list[int] = []
    missing: list[int] = []
    by_pars: list[str] = []

    for pars_name, pars_range in PARS_CODES.items():
        pars_present = [c for c in pars_range if (dest / f"{c}.html").exists()]
        pars_missing = [c for c in pars_range if not (dest / f"{c}.html").exists()]
        present.extend(pars_present)
        missing.extend(pars_missing)

        total = len(pars_range)
        if pars_missing:
            by_pars.append(
                f"{pars_name}: {len(pars_present)}/{total} "
                f"(missing codes: {pars_missing[:5]}"
                + (" ..." if len(pars_missing) > 5 else ")")
            )
        else:
            by_pars.append(f"{pars_name}: {total}/{total} complete")

    return {
        "present": sorted(present),
        "missing": sorted(missing),
        "by_pars": by_pars,
        "complete": len(missing) == 0,
    }


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    codes = all_codes()
    total = len(codes)

    print(f"Downloading {total} question pages from {BASE_URL} ...")
    print(f"Destination: {DEST.resolve()}")
    print()

    downloaded = 0
    skipped = 0

    for i, code in enumerate(codes, 1):
        out_path = DEST / f"{code}.html"
        if out_path.exists():
            skipped += 1
            if i % 50 == 0:
                print(f"  [{i}/{total}] skipped {code}.html (already on disk)")
            continue

        try:
            download_question(code, DEST)
            downloaded += 1
            print(f"  [{i}/{total}] {code}.html")
        except RuntimeError as exc:
            raise SystemExit(f"FATAL: {exc}") from exc

        time.sleep(SLEEP)

    print()
    print(f"Done. Downloaded: {downloaded}, skipped (already present): {skipped}")
    print()

    coverage = verify_coverage(DEST)
    print("Coverage report:")
    for line in coverage["by_pars"]:
        print(f"  {line}")

    if not coverage["complete"]:
        raise SystemExit(
            f"\nIncomplete download — {len(coverage['missing'])} pages missing: "
            f"{coverage['missing'][:10]}"
            + (" ..." if len(coverage["missing"]) > 10 else "")
        )

    print(f"\nAll {total} pages present. Corpus complete.")


if __name__ == "__main__":
    main()
