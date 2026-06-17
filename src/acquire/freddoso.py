"""
Acquire Freddoso's English translation of the Summa Theologiae.

Downloads TOC HTML pages and builds a coverage gap map.  Actual article files
are PDFs; the script records their locators and saves the TOC pages, then writes
coverage_gaps.json so the English ingest knows when to fall back to Dominican.

Run:
    uv run python src/acquire/freddoso.py
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://freddoso.com"

# Canonical question counts per part (from the complete Summa).
# Used to compute the missing set without a hard-coded list.
SUMMA_QUESTION_COUNTS = {
    "I": 119,
    "I-II": 114,
    "II-II": 189,
    "III": 90,
}

# TOC pages and the metadata needed to parse them.
TOC_PAGES = [
    {
        "part": "I",
        "toc_url": f"{BASE_URL}/summa-translation/TOC-part1.htm",
        "url_prefix": f"{BASE_URL}/summa-translation/Part%201/",
        "filename_prefix": "st1-ques",
    },
    {
        "part": "I-II",
        "toc_url": f"{BASE_URL}/summa-translation/TOC-part1-2.htm",
        "url_prefix": f"{BASE_URL}/summa-translation/Part%201-2/",
        "filename_prefix": "st1-2-ques",
    },
    {
        "part": "II-II",
        "toc_url": f"{BASE_URL}/summa-translation/TOC-part2-2.htm",
        "url_prefix": f"{BASE_URL}/summa-translation/Part%202-2/",
        "filename_prefix": "st2-2-ques",
    },
    {
        "part": "III",
        "toc_url": f"{BASE_URL}/summa-translation/TOC-part3.htm",
        "url_prefix": f"{BASE_URL}/summa-translation/Part%203/",
        "filename_prefix": "st3-ques",
    },
]

DEST = Path(__file__).resolve().parents[2] / "sources" / "english" / "freddoso"
SLEEP = 0.5


def _get(url: str) -> requests.Response:
    resp = requests.get(url, timeout=30, allow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Unexpected HTTP {resp.status_code} fetching {url!r}"
        )
    return resp


def _extract_question_numbers(
    html: str, toc_url: str, url_prefix: str, filename_prefix: str
) -> list[int]:
    """
    Parse a TOC page and return sorted question numbers whose PDF links are
    present as actual <a href> elements (not plain text references).
    """
    soup = BeautifulSoup(html, "html.parser")
    found: set[int] = set()

    for tag in soup.find_all("a", href=True):
        href: str = tag["href"]
        # Resolve relative hrefs to absolute for consistent matching.
        if href.startswith("/"):
            href = BASE_URL + href

        if not href.startswith(url_prefix):
            continue

        tail = href[len(url_prefix):]
        # Expected tail: "st1-ques03.pdf"
        if not tail.startswith(filename_prefix):
            raise RuntimeError(
                f"Unexpected link tail {tail!r} under {url_prefix!r} "
                f"at TOC {toc_url!r}"
            )
        stem = tail[len(filename_prefix):]
        if not stem.endswith(".pdf"):
            raise RuntimeError(
                f"Expected .pdf suffix, got {stem!r} at TOC {toc_url!r}"
            )
        num_str = stem[: -len(".pdf")]
        if not num_str.isdigit():
            raise RuntimeError(
                f"Non-numeric question number {num_str!r} in href {href!r} "
                f"at TOC {toc_url!r}"
            )
        found.add(int(num_str))

    if not found:
        raise RuntimeError(
            f"No question PDF links found on TOC page {toc_url!r} — "
            f"site structure may have changed"
        )

    return sorted(found)


def fetch_toc_pages(dest: Path) -> dict[str, list[int]]:
    """
    Download each TOC page, save it to disk, and return a mapping of
    part → sorted list of available question numbers.
    """
    dest.mkdir(parents=True, exist_ok=True)
    available_by_part: dict[str, list[int]] = {}

    for entry in TOC_PAGES:
        part = entry["part"]
        toc_url = entry["toc_url"]

        resp = _get(toc_url)
        time.sleep(SLEEP)

        toc_filename = dest / f"TOC-{part}.html"
        toc_filename.write_text(resp.text, encoding="utf-8")

        nums = _extract_question_numbers(
            resp.text,
            toc_url,
            entry["url_prefix"],
            entry["filename_prefix"],
        )
        available_by_part[part] = nums

    return available_by_part


def build_coverage_gaps(
    available_by_part: dict[str, list[int]],
) -> dict:
    """
    Compare available question numbers against the complete Summa counts and
    produce the coverage gap map consumed by the English ingest.

    Locator format matches segment.locator_path prefix: "I.q3", "I-II.q6", etc.
    """
    available_locators: list[str] = []
    missing_locators: list[str] = []
    part_notes: list[str] = []

    for part, total in SUMMA_QUESTION_COUNTS.items():
        linked = set(available_by_part.get(part, []))
        full_set = set(range(1, total + 1))

        present = sorted(linked & full_set)
        absent = sorted(full_set - linked)

        for q in present:
            available_locators.append(f"{part}.q{q}")
        for q in absent:
            missing_locators.append(f"{part}.q{q}")

        if not absent:
            part_notes.append(f"{part} complete ({total} questions)")
        elif not present:
            part_notes.append(f"{part} absent")
        else:
            suffix = " ..." if len(absent) > 10 else ""
            part_notes.append(
                f"{part} partial: {len(present)}/{total} questions "
                f"(missing: {', '.join(str(q) for q in absent[:10])}{suffix})"
            )

    notes = "; ".join(part_notes)

    return {
        "available": available_locators,
        "missing": missing_locators,
        "notes": notes,
    }


def write_coverage_gaps(dest: Path, gaps: dict) -> Path:
    out = dest / "coverage_gaps.json"
    out.write_text(json.dumps(gaps, indent=2), encoding="utf-8")
    return out


def main() -> None:
    print(f"Probing Freddoso's site at {BASE_URL} ...")
    available_by_part = fetch_toc_pages(DEST)

    for part, nums in available_by_part.items():
        total = SUMMA_QUESTION_COUNTS[part]
        print(f"  {part}: {len(nums)}/{total} questions linked")

    gaps = build_coverage_gaps(available_by_part)
    out = write_coverage_gaps(DEST, gaps)
    print(f"\nCoverage gap map written to {out}")
    print(f"  Available: {len(gaps['available'])} questions")
    print(f"  Missing:   {len(gaps['missing'])} questions")
    print(f"  Notes:     {gaps['notes']}")


if __name__ == "__main__":
    main()
