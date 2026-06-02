"""
M2 coverage report and dedup roll-up.

Produces:
  reports/m2_coverage.txt    — go/no-go deliverable for translation spend
  reports/m2_dedup_rollup.csv — corpus-wide term list for M3 review surface

Run:
  uv run python -m ingest.report_m2
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import psycopg2.extras

from ingest.db import get_conn

ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = ROOT / "reports"

ANOMALY_LOG = REPORTS_DIR / "m2_parser_anomalies.txt"
LATIN_STATS_FILE = REPORTS_DIR / "m2_latin_stats.json"
API_STATS_FILE = REPORTS_DIR / "m2_api_stats.json"
COVERAGE_REPORT = REPORTS_DIR / "m2_coverage.txt"
DEDUP_ROLLUP_CSV = REPORTS_DIR / "m2_dedup_rollup.csv"

_BODY_TYPES = ("arg", "sed_contra", "respondeo", "reply")

# DeepSeek V3 pricing (per 1k tokens, as of 2024)
_COST_PER_1K_INPUT = 0.00014
_COST_PER_1K_OUTPUT = 0.00028
# Re-translation cost estimate: avg ~400 tokens per segment
_AVG_SEGMENT_TOKENS = 400
_RETRANSLATION_COST_PER_TOKEN = 0.00014 / 1000  # DeepSeek input pricing


# ── Queries ───────────────────────────────────────────────────────────────────

def _count_articles(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(DISTINCT locator_path) FROM segment WHERE element_type = 'article_title'"
        )
        return cur.fetchone()[0]


def _count_body_segments(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM segment WHERE element_type = ANY(%s)",
            (list(_BODY_TYPES),),
        )
        return cur.fetchone()[0]


def _resolution_breakdown(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT resolution_method, count(*) FROM term_usage GROUP BY resolution_method"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _unique_needs_review(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(DISTINCT sense_id) FROM term_usage WHERE confidence = 'needs_review'"
        )
        return cur.fetchone()[0]


def _segments_with_flagged_term(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(DISTINCT segment_id) FROM term_usage WHERE confidence = 'needs_review'"
        )
        return cur.fetchone()[0]


def _bahounek_coverage(conn) -> tuple[int, int]:
    """Return (segments_with_czech, total_body_segments)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(DISTINCT st.segment_id) FROM segment_text st "
            "JOIN segment s ON st.segment_id = s.segment_id "
            "WHERE st.lang = 'cs' AND s.element_type = ANY(%s)",
            (list(_BODY_TYPES),),
        )
        with_czech = cur.fetchone()[0]
    total = _count_body_segments(conn)
    return with_czech, total


def _gap_category_breakdown(conn) -> dict[str, int]:
    """Count distinct gap terms per category (NULL-category Krystal terms excluded)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gt.category, count(DISTINCT gt.term_id) "
            "FROM glossary_term gt "
            "JOIN glossary_sense gs ON gs.term_id = gt.term_id "
            "JOIN term_usage tu ON tu.sense_id = gs.sense_id "
            "WHERE gt.category IS NOT NULL "
            "GROUP BY gt.category"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _model_proposed_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM term_usage WHERE resolution_method = 'model_proposed'"
        )
        return cur.fetchone()[0]


def _count_anomalies(anomaly_log: Path) -> int:
    if not anomaly_log.exists():
        return 0
    return sum(1 for line in anomaly_log.read_text(encoding="utf-8").splitlines()
               if line.startswith("[ANOMALY]"))


def _load_api_stats(stats_file: Path) -> dict:
    if not stats_file.exists():
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    return json.loads(stats_file.read_text(encoding="utf-8"))


def _load_latin_stats(stats_file: Path) -> dict | None:
    if not stats_file.exists():
        return None
    return json.loads(stats_file.read_text(encoding="utf-8"))


# ── Dedup roll-up ─────────────────────────────────────────────────────────────

def generate_dedup_rollup(conn) -> list[dict]:
    """Aggregate term_usage into one row per (term, sense) with frequency + locators."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                gt.latin_lemma,
                gt.category,
                gs.context_label,
                max(sr_sk.content)  AS proposed_slovak,
                count(*)            AS frequency,
                max(tu.confidence)  AS confidence,
                array_agg(DISTINCT tu.resolution_method ORDER BY tu.resolution_method)
                                    AS methods,
                array_agg(s.locator_path::text ORDER BY s.locator_path)
                                    AS locators
            FROM term_usage tu
            JOIN glossary_sense gs   ON tu.sense_id = gs.sense_id
            JOIN glossary_term gt    ON gs.term_id = gt.term_id
            LEFT JOIN sense_rendering sr_sk
                ON sr_sk.sense_id = gs.sense_id AND sr_sk.lang = 'sk'
            JOIN segment s           ON tu.segment_id = s.segment_id
            GROUP BY gt.latin_lemma, gt.category, gs.context_label, gs.sense_id
            ORDER BY gt.category NULLS FIRST, frequency DESC, gt.latin_lemma
        """)
        return [dict(r) for r in cur.fetchall()]


_STUB_RE = re.compile(r"^\[")


def assert_no_stub_proposals(rows: list[dict]) -> None:
    """Fail loudly if any proposed_slovak is a bracketed stub (e.g. '[model_proposed: x]').

    M2 acceptance criterion: no stub may reach the review export. We raise rather
    than swallow so a bad run aborts before writing a poisoned roll-up.
    """
    offenders = [
        row["latin_lemma"]
        for row in rows
        if row.get("proposed_slovak") and _STUB_RE.match(row["proposed_slovak"])
    ]
    if offenders:
        raise RuntimeError(
            f"Dedup roll-up contains {len(offenders)} bracketed stub proposal(s) "
            f"that must never reach review: {', '.join(sorted(set(offenders)))}"
        )


def write_dedup_rollup(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("latin_lemma,category,context_label,proposed_slovak,frequency,confidence,methods,locators\n")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["latin_lemma", "category", "context_label", "proposed_slovak",
                        "frequency", "confidence", "methods", "locators"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "latin_lemma": row["latin_lemma"],
                "category": row["category"] or "",
                "context_label": row["context_label"] or "",
                "proposed_slovak": row["proposed_slovak"] or "",
                "frequency": row["frequency"],
                "confidence": row["confidence"],
                "methods": "|".join(row["methods"] or []),
                "locators": "|".join(row["locators"] or []),
            })


# ── Coverage report ───────────────────────────────────────────────────────────

def generate_coverage_report(conn) -> str:
    latin_stats = _load_latin_stats(LATIN_STATS_FILE)
    if latin_stats is not None:
        # Prefer persisted run stats: total includes anomalous articles not in DB
        total_articles = latin_stats["total"]
        clean_articles = latin_stats["ingested"]
        anomaly_count = latin_stats["anomalies"]
    else:
        # Fallback: DB-only count (no latin stats file means pipeline ran without pipeline.py)
        clean_articles = _count_articles(conn)
        anomaly_count = _count_anomalies(ANOMALY_LOG)
        total_articles = clean_articles + anomaly_count
    total_segments = _count_body_segments(conn)
    api_stats = _load_api_stats(API_STATS_FILE)

    breakdown = _resolution_breakdown(conn)
    all_methods = [
        "krystal_single",
        "krystal_multi_voted",
        "krystal_multi_flagged",
        "bahounek_derived",
        "english_derived",
        "model_proposed",
    ]
    total_usages = sum(breakdown.values()) or 1  # avoid div/0

    auto_total = breakdown.get("krystal_single", 0) + breakdown.get("krystal_multi_voted", 0)
    review_total = total_usages - auto_total

    unique_review = _unique_needs_review(conn)
    flagged_segments = _segments_with_flagged_term(conn)
    retranslation_cost = flagged_segments * _AVG_SEGMENT_TOKENS * _RETRANSLATION_COST_PER_TOKEN

    czech_with, czech_total = _bahounek_coverage(conn)
    czech_pct = 100.0 * czech_with / czech_total if czech_total else 0.0
    czech_without = czech_total - czech_with

    gap_categories = _gap_category_breakdown(conn)

    api_cost = api_stats.get("cost_usd", 0.0)

    def _pct(n: int) -> str:
        return f"{100.0 * n / total_usages:.1f}%" if total_usages else "0.0%"

    lines = [
        "CORPUS OVERVIEW",
        f"  Total articles:     {total_articles}",
        f"  Total segments:     {total_segments}",
        f"  Articles clean:     {clean_articles}",
        f"  Articles anomalous: {anomaly_count}"
        + (" (see m2_parser_anomalies.txt)" if anomaly_count else ""),
        "",
        "TERM RESOLUTION BREAKDOWN",
    ]

    for method in all_methods:
        n = breakdown.get(method, 0)
        need = "" if method in ("krystal_single", "krystal_multi_voted") else "  → NEEDS human review"
        lines.append(f"  {method:<28} {n:>6}  ({_pct(n)}){need}")

    lines += [
        "  " + "─" * 52,
        f"  Auto-resolved (no review needed): {_pct(auto_total)}",
        f"  Needs human review:               {_pct(review_total)}",
        "",
        "REVIEW SCOPE",
        f"  Unique terms needing review: {unique_review}",
        "  (Each unique term reviewed once regardless of frequency.)",
        "",
        "RE-TRANSLATION SCOPE (if reviewer changes a term)",
        f"  Segments containing ≥1 flagged term: {flagged_segments}",
        "  Estimated max re-run cost:",
        f"    {flagged_segments} segments × avg ~{_AVG_SEGMENT_TOKENS} tokens"
        f" × ${_RETRANSLATION_COST_PER_TOKEN * 1000:.5f}/1k = ~${retranslation_cost:.4f}",
        "  Note: each segment re-translated AT MOST ONCE regardless of how many",
        "  of its terms were changed. Batch all term changes before re-running.",
        "",
        "BAHOUNEK COVERAGE",
        f"  Segments with Czech reference:    {czech_with}  ({czech_pct:.1f}%)",
        f"  Segments without Czech reference: {czech_without}  ({100.0 - czech_pct:.1f}%)",
        "",
        "GAP TERM CATEGORIES",
    ]

    gap_total = sum(gap_categories.values())
    for category in ("term", "name", "formula", "prose"):
        n = gap_categories.get(category, 0)
        lines.append(f"  {category:<28} {n:>6}")
    lines.append("  " + "─" * 36)
    lines.append(f"  {'Distinct gap terms':<28} {gap_total:>6}")

    lines += [
        "",
        "GAP TERM PROPOSALS (DeepSeek V3)",
        f"  Terms proposed by model: {api_stats.get('lemmas_proposed', breakdown.get('model_proposed', 0))}",
        f"  API calls made:          {api_stats.get('calls', 0)}",
        f"  API cost incurred:       ~${api_cost:.4f}",
    ]

    return "\n".join(lines) + "\n"


def write_coverage_report(conn, path: Path = COVERAGE_REPORT) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    report = generate_coverage_report(conn)
    path.write_text(report, encoding="utf-8")
    return report


# ── Entry point ───────────────────────────────────────────────────────────────

def run() -> None:
    print("Generating M2 coverage report and dedup roll-up...")
    with get_conn() as conn:
        rows = generate_dedup_rollup(conn)
        assert_no_stub_proposals(rows)
        write_dedup_rollup(rows, DEDUP_ROLLUP_CSV)
        print(f"  Dedup roll-up: {len(rows)} rows → {DEDUP_ROLLUP_CSV}")

        report = write_coverage_report(conn)

    print(f"  Coverage report → {COVERAGE_REPORT}")
    print()
    print(report)


if __name__ == "__main__":
    run()
