"""
Provenance report generator — Step 8 of M1.

Reads term_usage rows and produces reports/m1_provenance.txt — a plain-text,
human-readable record of every resolution: which Slovak term was chosen for
each Latin term found, by what method, with what evidence.

Run:
  uv run python -m ingest.report
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg2.extras

from ingest.db import get_conn, work_id

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "reports" / "m1_provenance.txt"

_BODY_TYPES = {"arg", "sed_contra", "respondeo", "reply"}


def _load_usages(conn, wid: int) -> list[dict]:
    """Load all term_usage rows for the work, joined to segment + term info."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                s.locator_path::text            AS locator,
                s.element_type,
                gt.latin_lemma,
                sr_sk.content                   AS sk_term,
                sr_cs.content                   AS cs_term,
                tu.resolution_method,
                tu.confidence,
                tu.signals
            FROM term_usage tu
            JOIN segment s            ON s.segment_id  = tu.segment_id
            JOIN glossary_sense gs    ON gs.sense_id   = tu.sense_id
            JOIN glossary_term gt     ON gt.term_id    = gs.term_id
            LEFT JOIN sense_rendering sr_sk
                ON sr_sk.sense_id = tu.sense_id AND sr_sk.lang = 'sk'
            LEFT JOIN sense_rendering sr_cs
                ON sr_cs.sense_id = tu.sense_id AND sr_cs.lang = 'cs'
            WHERE s.work_id = %s
            ORDER BY s.locator_path, gt.latin_lemma
        """, (wid,))
        return cur.fetchall()


def _extract_article(locator: str) -> str:
    """Return article-level locator from a segment locator."""
    parts = locator.split(".")
    return ".".join(parts[:3]) if len(parts) >= 3 else locator


def generate_report(usages: list[dict]) -> str:
    """Build the full provenance report text."""
    lines: list[str] = []

    # Group by article then segment
    articles: dict[str, dict[str, list[dict]]] = {}
    for row in usages:
        art = _extract_article(row["locator"])
        seg = row["locator"]
        articles.setdefault(art, {}).setdefault(seg, []).append(row)

    method_counts: dict[str, int] = {}

    for art_loc in sorted(articles):
        lines.append(f"ARTICLE: {art_loc}")
        segs = articles[art_loc]
        for seg_loc in sorted(segs):
            lines.append(f"  SEGMENT: {seg_loc}")
            seg_rows = segs[seg_loc]
            max_lemma = max(len(r["latin_lemma"]) for r in seg_rows)
            max_term = max(len(r["sk_term"] or r["cs_term"] or "?") for r in seg_rows)

            for row in sorted(seg_rows, key=lambda r: r["latin_lemma"]):
                lemma = row["latin_lemma"]
                sk = row["sk_term"] or row["cs_term"] or "?"
                method = row["resolution_method"]
                conf = row["confidence"]
                method_counts[method] = method_counts.get(method, 0) + 1

                lines.append(
                    f"    {lemma:<{max_lemma}}  →  {sk:<{max_term}}"
                    f"  [{method}, {conf}]"
                )
                if row["signals"]:
                    sig_str = ", ".join(
                        f"{k}" for k in sorted(row["signals"].keys())
                    )
                    lines.append(f"      signals: {sig_str}")

        lines.append("")

    # Summary
    total = sum(method_counts.values())
    lines.append("SUMMARY")
    for method in [
        "krystal_single",
        "krystal_multi_voted",
        "krystal_multi_flagged",
        "bahounek_derived",
        "english_derived",
        "model_proposed",
    ]:
        count = method_counts.get(method, 0)
        pct = f"{100 * count / total:.1f}%" if total else "0.0%"
        lines.append(f"  {method:<26}  {count:>4}  ({pct})")
    lines.append(f"  {'TOTAL':<26}  {total:>4}")

    return "\n".join(lines)


def run() -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_conn() as conn:
        wid = work_id(conn, "summa_articulus")
        usages = _load_usages(conn, wid)

    if not usages:
        print("No term_usage rows found. Run the resolver first.", file=sys.stderr)
        sys.exit(1)

    text = generate_report(usages)
    REPORT_PATH.write_text(text, encoding="utf-8")
    print(f"Report written to {REPORT_PATH} ({len(usages)} resolutions)")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        sys.exit(1)
