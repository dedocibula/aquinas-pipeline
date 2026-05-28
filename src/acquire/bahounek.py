"""
Scraper for Bahounek's Czech Summa Theologiae from cormierop.cz.

The site serves each pars as one monolithic HTML file (~2–4 MB).
We save them verbatim; later parsing splits into segments.

Coordinate tag format found on the site:
  Prima Pars      : "I ot. N čl. N arg. N"
  Prima Secundae  : "I-II ot. N čl. N arg. N"
  Secunda Secundae: "II-II ot. N čl. N arg. N"
  Tertia Pars     : "III ot. N čl. N arg. N"
  Supplementum    : not present — Bahounek's text ends at III ot. 90 with
                    an explicit note that St. Thomas died there.

Segment-part suffixes: arg. N | sc. | co. | ad N | pr.
"""

import random
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "http://www.cormierop.cz"

PARTES = [
    {
        "pars": "I",
        "url": f"{BASE_URL}/Summa-teologicka-Icast.html",
        "filename": "pars_I.html",
        "coord_prefix": r"I ot\.",
    },
    {
        "pars": "I-II",
        "url": f"{BASE_URL}/Summa-teologicka-IIcast-1dil.html",
        "filename": "pars_I-II.html",
        "coord_prefix": r"I-II ot\.",
    },
    {
        "pars": "II-II",
        "url": f"{BASE_URL}/Summa-teologicka-IIcast-2dil.html",
        "filename": "pars_II-II.html",
        "coord_prefix": r"II-II ot\.",
    },
    {
        "pars": "III",
        "url": f"{BASE_URL}/Summa-teologicka-IIIcast.html",
        "filename": "pars_III.html",
        "coord_prefix": r"III ot\.",
    },
]

DEST = Path(__file__).resolve().parents[2] / "sources" / "czech" / "bahounek"

COORD_RE = re.compile(
    r"(?:I-II|II-II|III|I) ot\. \d+ čl\. \d+ (?:arg\. \d+|sc\.|co\.|ad \d+|pr\.)"
)


def fetch(url: str, session: requests.Session) -> str:
    resp = session.get(url, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Unexpected HTTP {resp.status_code} fetching {url}"
        )
    resp.encoding = "utf-8"
    return resp.text


def verify_coordinate_tags(html: str, url: str, pars: str) -> None:
    pars_re = re.compile(
        rf"{re.escape(pars)} ot\. \d+ čl\. \d+ (?:arg\. \d+|sc\.|co\.|ad \d+|pr\.)"
    )
    matches = pars_re.findall(html)
    if not matches:
        snippet = html[:500].replace("\n", " ")
        raise RuntimeError(
            f"No coordinate tags found for pars={pars!r} at {url}\n"
            f"Expected pattern: '{pars} ot. N čl. N arg./sc./co./ad N'\n"
            f"Page begins with: {snippet!r}"
        )


def extract_section_urls(index_html: str, base_url: str) -> list[str]:
    """
    Return all Summa-teologicka-*.html links found on the Aquinas index page.
    Used to confirm the four partes are discoverable.
    """
    soup = BeautifulSoup(index_html, "html.parser")
    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.match(r"Summa-teologicka-.+\.html", href):
            urls.append(f"{base_url}/{href}")
    return urls


def check_all_partes_covered(saved_files: list[Path]) -> None:
    required = {"pars_I.html", "pars_I-II.html", "pars_II-II.html", "pars_III.html"}
    found = {f.name for f in saved_files}
    missing = required - found
    if missing:
        raise RuntimeError(
            f"Coverage check failed — missing partes files: {sorted(missing)}\n"
            f"Found: {sorted(found)}"
        )


def download_all(dest: Path = DEST, *, skip_existing: bool = True) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    session = requests.Session()
    session.headers["User-Agent"] = (
        "aquinas-pipeline/0.1 (research; dedo.cibula@gmail.com)"
    )

    for entry in PARTES:
        out_path = dest / entry["filename"]

        if skip_existing and out_path.exists() and out_path.stat().st_size > 100_000:
            print(f"[skip]  {entry['filename']} already on disk")
            saved.append(out_path)
            continue

        print(f"[fetch] {entry['url']} → {entry['filename']}")
        html = fetch(entry["url"], session)

        verify_coordinate_tags(html, entry["url"], entry["pars"])

        out_path.write_text(html, encoding="utf-8")
        print(f"[ok]    {entry['filename']} ({len(html):,} chars)")
        saved.append(out_path)

        time.sleep(0.5)

    check_all_partes_covered(saved)
    return saved


def verify_download(dest: Path = DEST) -> None:
    for entry in PARTES:
        path = dest / entry["filename"]
        if not path.exists():
            raise RuntimeError(
                f"Verification failed: {path} does not exist. Run download first."
            )
        html = path.read_text(encoding="utf-8")
        verify_coordinate_tags(html, entry["url"], entry["pars"])

        sample = COORD_RE.findall(html)
        if len(sample) < 10:
            raise RuntimeError(
                f"Verification failed for {path.name}: found only {len(sample)} "
                f"coordinate tags (expected hundreds). URL: {entry['url']}"
            )
        print(
            f"[verify] {entry['filename']}: {len(sample):,} coordinate tags "
            f"(sample: {random.choice(sample)!r})"
        )

    print("[verify] All four partes present and tagged. Coverage: OK.")


if __name__ == "__main__":
    saved = download_all()
    verify_download()
    print(f"\nDone. {len(saved)} files saved to {DEST}")
