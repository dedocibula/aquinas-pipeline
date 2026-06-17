"""Tests for src/optimize/run_compare.py — cross-run regression report."""

from __future__ import annotations

from collections import Counter
from unittest.mock import MagicMock, patch

from optimize.run_compare import (
    build_report,
    fetch_failure_class_counts,
    fetch_run_summary,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _summary(**overrides) -> dict:
    base = {
        "flow_name": "translate_corpus",
        "started_at": "2026-06-10 10:00",
        "finished_at": "2026-06-10 12:00",
        "git_sha": "abc1234",
        "prompt_hash": "deadbeefdeadbeef",
        "glossary_snapshot": {"approved_senses": 2500, "max_version": 3},
        "translator_model": "deepseek-chat",
        "reviewer_model": "deepseek-reasoner",
        "filters": None,
        "total_segments": 100,
        "total_translated": 95,
        "total_needs_human": 5,
        "total_cost_usd": 1.25,
        "avg_iterations": 1.2,
    }
    base.update(overrides)
    return base


def _cursor(rows):
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None
    return cur


# ── fetch_run_summary ─────────────────────────────────────────────────────────


def test_fetch_run_summary_maps_columns():
    row = (
        "translate_corpus", "t0", "t1", "abc", "hash", {"approved_senses": 1},
        "deepseek-chat", "deepseek-reasoner", None, 10, 9, 1, 0.5, 1.1,
    )
    conn = MagicMock()
    conn.cursor.return_value = _cursor([row])
    s = fetch_run_summary(conn, 1)
    assert s["flow_name"] == "translate_corpus"
    assert s["total_segments"] == 10
    assert s["avg_iterations"] == 1.1


def test_fetch_run_summary_raises_when_missing():
    conn = MagicMock()
    conn.cursor.return_value = _cursor([])
    try:
        fetch_run_summary(conn, 99)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "run_id=99" in str(exc)


# ── fetch_failure_class_counts ────────────────────────────────────────────────


def test_fetch_failure_class_counts_keys_terms():
    conn = MagicMock()
    conn.cursor.return_value = _cursor(
        [("precheck_terminology", "rozum", 5), ("reviewer_revision", None, 3)]
    )
    counts = fetch_failure_class_counts(conn, 1)
    assert counts["precheck_terminology(rozum)"] == 5
    assert counts["reviewer_revision"] == 3


# ── build_report ──────────────────────────────────────────────────────────────


def test_build_report_lists_flips_and_deltas():
    flips = [
        (10, "I.q1.a1.respondeo", "needs_human", "translated"),
        (11, "I.q2.a1.arg1", "translated", "needs_human"),
    ]
    with (
        patch(
            "optimize.run_compare.fetch_run_summary",
            side_effect=[_summary(), _summary(git_sha="def5678")],
        ),
        patch("optimize.run_compare.fetch_status_flips", return_value=flips),
        patch(
            "optimize.run_compare.fetch_failure_class_counts",
            side_effect=[
                Counter({"precheck_terminology(rozum)": 5}),
                Counter({"precheck_terminology(rozum)": 1}),
            ],
        ),
    ):
        report = build_report(MagicMock(), 1, 2)

    assert "RUN COMPARISON: 1 (baseline) → 2 (candidate)" in report
    assert "improved (needs_human → translated): 1" in report
    assert "+ I.q1.a1.respondeo" in report
    assert "regressed (translated → needs_human): 1" in report
    assert "- I.q2.a1.arg1" in report
    assert "precheck_terminology(rozum)" in report
    assert "5 → 1" in report
    assert "git sha differs" in report


def test_build_report_notes_prompt_change():
    with (
        patch(
            "optimize.run_compare.fetch_run_summary",
            side_effect=[_summary(), _summary(prompt_hash="0123456789abcdef")],
        ),
        patch("optimize.run_compare.fetch_status_flips", return_value=[]),
        patch(
            "optimize.run_compare.fetch_failure_class_counts",
            side_effect=[Counter(), Counter()],
        ),
    ):
        report = build_report(MagicMock(), 1, 2)

    assert "prompt hash differs" in report
    assert "(no failures recorded in either run)" in report
