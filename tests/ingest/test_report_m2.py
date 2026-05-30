"""
Tests for src/ingest/report_m2.py — pure logic, no DB, no live files.
"""

from __future__ import annotations

import csv
import json
from unittest.mock import MagicMock, patch

from ingest.report_m2 import (
    _count_anomalies,
    _load_api_stats,
    _load_latin_stats,
    generate_coverage_report,
    write_coverage_report,
    write_dedup_rollup,
)

# ── _count_anomalies ──────────────────────────────────────────────────────────

class TestCountAnomalies:
    def test_counts_anomaly_lines(self, tmp_path):
        log = tmp_path / "anomalies.txt"
        log.write_text(
            "[ANOMALY] locator=I.q1.a1 file=sth0001.html type=RuntimeError excerpt='missing'\n"
            "[ANOMALY] locator=I.q2.a1 file=sth0002.html type=RuntimeError excerpt='missing'\n"
            "Some other line\n",
            encoding="utf-8",
        )
        assert _count_anomalies(log) == 2

    def test_returns_zero_if_no_anomaly_lines(self, tmp_path):
        log = tmp_path / "anomalies.txt"
        log.write_text("COVERAGE: some=stuff\n", encoding="utf-8")
        assert _count_anomalies(log) == 0

    def test_returns_zero_if_file_missing(self, tmp_path):
        assert _count_anomalies(tmp_path / "nonexistent.txt") == 0


# ── _load_api_stats ───────────────────────────────────────────────────────────

class TestLoadApiStats:
    def test_loads_existing_file(self, tmp_path):
        stats = {"calls": 5, "input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.0042}
        f = tmp_path / "stats.json"
        f.write_text(json.dumps(stats))
        assert _load_api_stats(f) == stats

    def test_returns_zeros_if_missing(self, tmp_path):
        result = _load_api_stats(tmp_path / "missing.json")
        assert result["calls"] == 0
        assert result["cost_usd"] == 0.0


# ── write_dedup_rollup ────────────────────────────────────────────────────────

class TestWriteDedupRollup:
    def test_writes_csv_with_correct_columns(self, tmp_path):
        rows = [
            {
                "latin_lemma": "ratio",
                "context_label": None,
                "proposed_slovak": "rozum",
                "frequency": 42,
                "confidence": "auto",
                "methods": ["krystal_single"],
                "locators": ["I.q1.a1.arg1", "I.q2.a3.respondeo"],
            }
        ]
        path = tmp_path / "rollup.csv"
        write_dedup_rollup(rows, path)

        with path.open() as f:
            reader = csv.DictReader(f)
            data = list(reader)

        assert len(data) == 1
        assert data[0]["latin_lemma"] == "ratio"
        assert data[0]["frequency"] == "42"
        assert data[0]["context_label"] == ""
        assert "|" in data[0]["locators"]  # multiple locators joined

    def test_writes_header_only_for_empty(self, tmp_path):
        path = tmp_path / "rollup.csv"
        write_dedup_rollup([], path)
        content = path.read_text()
        assert "latin_lemma" in content
        assert content.count("\n") == 1  # header only

    def test_creates_parent_directory(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "rollup.csv"
        write_dedup_rollup([], path)
        assert path.exists()


# ── generate_coverage_report ──────────────────────────────────────────────────

def _make_mock_conn(
    articles=10,
    segments=110,
    breakdown=None,
    unique_review=80,
    flagged_segments=60,
    czech_with=95,
):
    if breakdown is None:
        breakdown = {
            "krystal_single": 20,
            "krystal_multi_voted": 2,
            "krystal_multi_flagged": 3,
            "bahounek_derived": 70,
            "english_derived": 5,
            "model_proposed": 2,
        }

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    # fetchone returns differ by call order — build a queue
    from collections import deque
    fetchone_queue = deque([
        (articles,),          # _count_articles
        (segments,),          # _count_body_segments (first call)
        (unique_review,),     # _unique_needs_review
        (flagged_segments,),  # _segments_with_flagged_term
        (czech_with,),        # _bahounek_coverage first (with_czech)
        (segments,),          # _count_body_segments (second call inside _bahounek_coverage)
        (breakdown.get("model_proposed", 0),),  # _model_proposed_count
    ])

    def pop_fetchone():
        return fetchone_queue.popleft() if fetchone_queue else (0,)

    cur.fetchone.side_effect = pop_fetchone
    cur.fetchall.return_value = list(breakdown.items())
    return conn


class TestGenerateCoverageReport:
    def _run(self, tmp_path, **kwargs):
        conn = _make_mock_conn(**kwargs)
        with patch("ingest.report_m2.ANOMALY_LOG", tmp_path / "anomalies.txt"), \
             patch("ingest.report_m2.API_STATS_FILE", tmp_path / "api_stats.json"):
            return generate_coverage_report(conn)

    def test_contains_all_sections(self, tmp_path):
        report = self._run(tmp_path)
        assert "CORPUS OVERVIEW" in report
        assert "TERM RESOLUTION BREAKDOWN" in report
        assert "REVIEW SCOPE" in report
        assert "RE-TRANSLATION SCOPE" in report
        assert "BAHOUNEK COVERAGE" in report
        assert "GAP TERM PROPOSALS" in report

    def test_contains_article_counts(self, tmp_path):
        report = self._run(tmp_path, articles=2663)
        assert "2663" in report

    def test_anomaly_count_shown(self, tmp_path):
        log = tmp_path / "anomalies.txt"
        log.write_text("[ANOMALY] locator=x\n[ANOMALY] locator=y\n")
        conn = _make_mock_conn()
        with patch("ingest.report_m2.ANOMALY_LOG", log), \
             patch("ingest.report_m2.API_STATS_FILE", tmp_path / "missing.json"):
            report = generate_coverage_report(conn)
        assert "2" in report  # 2 anomalies

    def test_all_six_methods_listed(self, tmp_path):
        report = self._run(tmp_path)
        for method in ["krystal_single", "krystal_multi_voted", "krystal_multi_flagged",
                       "bahounek_derived", "english_derived", "model_proposed"]:
            assert method in report

    def test_api_cost_shown_from_stats_file(self, tmp_path):
        stats = {"calls": 3, "input_tokens": 500, "output_tokens": 50, "cost_usd": 0.0007}
        (tmp_path / "api_stats.json").write_text(json.dumps(stats))
        conn = _make_mock_conn()
        with patch("ingest.report_m2.ANOMALY_LOG", tmp_path / "anomalies.txt"), \
             patch("ingest.report_m2.API_STATS_FILE", tmp_path / "api_stats.json"):
            report = generate_coverage_report(conn)
        assert "0.0007" in report


class TestLoadLatinStats:
    def test_returns_none_if_missing(self, tmp_path):
        assert _load_latin_stats(tmp_path / "missing.json") is None

    def test_loads_existing_file(self, tmp_path):
        data = {"total": 2663, "ingested": 2650, "anomalies": 13}
        f = tmp_path / "latin_stats.json"
        f.write_text(json.dumps(data))
        assert _load_latin_stats(f) == data


class TestCoverageReportLatinStats:
    def _run_with_stats(self, tmp_path, latin_data):
        stats_file = tmp_path / "latin_stats.json"
        stats_file.write_text(json.dumps(latin_data))
        conn = _make_mock_conn(articles=latin_data["ingested"])
        with patch("ingest.report_m2.ANOMALY_LOG", tmp_path / "anomalies.txt"), \
             patch("ingest.report_m2.API_STATS_FILE", tmp_path / "api_stats.json"), \
             patch("ingest.report_m2.LATIN_STATS_FILE", stats_file):
            return generate_coverage_report(conn)

    def test_uses_total_from_latin_stats(self, tmp_path):
        report = self._run_with_stats(tmp_path, {"total": 2663, "ingested": 2650, "anomalies": 13})
        assert "2663" in report

    def test_uses_ingested_as_clean(self, tmp_path):
        report = self._run_with_stats(tmp_path, {"total": 2663, "ingested": 2650, "anomalies": 13})
        assert "2650" in report

    def test_anomaly_count_from_latin_stats(self, tmp_path):
        report = self._run_with_stats(tmp_path, {"total": 2663, "ingested": 2650, "anomalies": 13})
        assert "13" in report


class TestWriteCoverageReport:
    def test_writes_file(self, tmp_path):
        conn = _make_mock_conn()
        out = tmp_path / "coverage.txt"
        with patch("ingest.report_m2.ANOMALY_LOG", tmp_path / "anomalies.txt"), \
             patch("ingest.report_m2.API_STATS_FILE", tmp_path / "stats.json"):
            write_coverage_report(conn, path=out)
        assert out.exists()
        assert "CORPUS OVERVIEW" in out.read_text()

    def test_creates_parent_directory(self, tmp_path):
        conn = _make_mock_conn()
        out = tmp_path / "deep" / "report.txt"
        with patch("ingest.report_m2.ANOMALY_LOG", tmp_path / "anomalies.txt"), \
             patch("ingest.report_m2.API_STATS_FILE", tmp_path / "stats.json"):
            write_coverage_report(conn, path=out)
        assert out.exists()
