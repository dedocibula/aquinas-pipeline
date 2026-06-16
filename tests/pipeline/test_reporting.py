"""Tests for the per-step report renderer/writer."""

from __future__ import annotations

from datetime import datetime

from pipeline import StepReport, StepResult


def _report(result, *, stage="ingest", elapsed=1.5):
    return StepReport(
        stage=stage,
        result=result,
        started_at=datetime(2026, 6, 16, 12, 0, 0),
        elapsed_s=elapsed,
    )


class TestRender:
    def test_header_status_and_summary(self):
        r = StepResult(name="latin", ok=True, summary="60/60 articles")
        text = _report(r).render()
        assert "# ingest · latin" in text
        assert "- status: ok" in text
        assert "- elapsed: 1.5s" in text
        assert "- when: 2026-06-16T12:00:00" in text
        assert "- summary: 60/60 articles" in text

    def test_failed_status(self):
        r = StepResult(name="resolve", ok=False, summary="boom")
        text = _report(r).render()
        assert "- status: FAILED" in text
        assert "## action required" in text
        assert "- step failed: boom" in text

    def test_details_scalars(self):
        r = StepResult(name="latin", ok=True, details={"ingested": 60, "anomalies": 2})
        text = _report(r).render()
        assert "## details" in text
        assert "- ingested: 60" in text
        assert "- anomalies: 2" in text

    def test_details_nested_dict_and_failing_checks(self):
        r = StepResult(
            name="verify-sources",
            ok=False,
            summary="3/4 source checks passed",
            details={"checks": {"latin": True, "db": False}},
        )
        text = _report(r).render()
        # nested dict expanded with bool → pass/FAIL
        assert "    - latin: pass" in text
        assert "    - db: FAIL" in text
        # action section names the failing check, not the generic fallback
        assert "- failing checks: db" in text
        assert "step failed" not in text

    def test_details_list_preview_and_count(self):
        r = StepResult(
            name="latin",
            ok=True,
            details={"anomalies": ["a", "b", "c", "d", "e", "f", "g"]},
        )
        text = _report(r).render()
        assert "- anomalies (7): a, b, c, d, e (+2 more)" in text


class TestWrite:
    def test_writes_named_file(self, tmp_path):
        r = StepResult(name="latin", ok=True, summary="done")
        path = _report(r).write(tmp_path / "ingest")
        assert path == tmp_path / "ingest" / "latin.md"
        assert path.read_text(encoding="utf-8").startswith("# ingest · latin")

    def test_creates_stage_dir(self, tmp_path):
        r = StepResult(name="x", ok=True)
        target = tmp_path / "deep" / "stage"
        _report(r, stage="stage").write(target)
        assert (target / "x.md").is_file()
