"""
Tests for src/verify.py check functions.
All use temp directories — no live network or real source files required.
"""
from __future__ import annotations

import json
from pathlib import Path

import acquire.steps as steps
import acquire.verify as verify_sources  # noqa: F401 — tests use verify_sources.* names
from acquire.steps import VerifySourcesStep
from pipeline import PipelineContext

ROOT = Path(__file__).resolve().parents[1]


def _ctx(tmp_path) -> PipelineContext:
    return PipelineContext(reports_dir=tmp_path)


# ── helpers ──────────────────────────────────────────────────────────────────

def _latin_html(article: str = "I q. 1 a. 1") -> str:
    return (
        f'<html><body>'
        f'<P TITLE="{article} arg. 1">obj</P>'
        f'<P TITLE="{article} s. c.">sc</P>'
        f'<P TITLE="{article} co.">resp</P>'
        f'<P TITLE="{article} ad 1">reply</P>'
        f'</body></html>'
    )


def _bahounek_html(pars: str = "I") -> str:
    return f"<html><body><p>{pars} ot. 1 čl. 1 arg. 1 text</p></body></html>"


def _dominican_html(code: str = "1001") -> str:
    return (
        f'<html><body id="{code}.htm" class="summa">'
        f'<div id="springfield2"><h2 id="article1">Art. 1</h2></div>'
        f'</body></html>'
    )


# ── Latin checks ──────────────────────────────────────────────────────────────

class TestCheckLatin:
    def test_passes_with_sufficient_articles(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        dest = tmp_path / "sources" / "latin"
        dest.mkdir(parents=True)
        for i in range(1, 10):
            html = "".join(
                _latin_html(f"I q. {i} a. {j}") for j in range(1, 300)
            )
            (dest / f"sth1{i:03d}.html").write_text(html, encoding="utf-8")
        assert verify_sources.check_latin() is True

    def test_fails_with_no_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        (tmp_path / "sources" / "latin").mkdir(parents=True)
        assert verify_sources.check_latin() is False

    def test_fails_below_article_threshold(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        dest = tmp_path / "sources" / "latin"
        dest.mkdir(parents=True)
        (dest / "sth1001.html").write_text(_latin_html(), encoding="utf-8")
        assert verify_sources.check_latin() is False

    def test_fails_missing_element_types(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        dest = tmp_path / "sources" / "latin"
        dest.mkdir(parents=True)
        incomplete = "".join(
            f'<P TITLE="I q. {i} a. 1 arg. 1">obj</P>' for i in range(1, 2670)
        )
        (dest / "sth1001.html").write_text(
            f"<html><body>{incomplete}</body></html>", encoding="utf-8"
        )
        assert verify_sources.check_latin() is False


# ── Bahounek checks ───────────────────────────────────────────────────────────

class TestCheckBahounek:
    def _write_all(self, dest: Path) -> None:
        dest.mkdir(parents=True)
        pairs = [
            ("pars_I.html", "I"),
            ("pars_I-II.html", "I-II"),
            ("pars_II-II.html", "II-II"),
            ("pars_III.html", "III"),
        ]
        for filename, pars in pairs:
            (dest / filename).write_text(_bahounek_html(pars), encoding="utf-8")

    def test_passes_with_all_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        self._write_all(tmp_path / "sources" / "czech" / "bahounek")
        assert verify_sources.check_bahounek() is True

    def test_fails_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        dest = tmp_path / "sources" / "czech" / "bahounek"
        self._write_all(dest)
        (dest / "pars_I.html").unlink()
        assert verify_sources.check_bahounek() is False

    def test_fails_missing_coord_tags(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        dest = tmp_path / "sources" / "czech" / "bahounek"
        self._write_all(dest)
        (dest / "pars_I.html").write_text(
            "<html><body><p>No coordinate tags here</p></body></html>",
            encoding="utf-8",
        )
        assert verify_sources.check_bahounek() is False


# ── Krystal checks ────────────────────────────────────────────────────────────

class TestCheckKrystal:
    def test_fails_with_no_docx(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        (tmp_path / "sources" / "czech" / "krystal").mkdir(parents=True)
        assert verify_sources.check_krystal() is False


# ── Dominican checks ──────────────────────────────────────────────────────────

class TestCheckDominican:
    def test_fails_with_no_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        (tmp_path / "sources" / "english" / "dominican").mkdir(parents=True)
        assert verify_sources.check_dominican() is False

    def test_fails_below_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        dest = tmp_path / "sources" / "english" / "dominican"
        dest.mkdir(parents=True)
        (dest / "1001.htm").write_text(_dominican_html(), encoding="utf-8")
        assert verify_sources.check_dominican() is False

    def test_passes_with_sufficient_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        dest = tmp_path / "sources" / "english" / "dominican"
        dest.mkdir(parents=True)
        for i in range(1, 615):
            (dest / f"{1000 + i}.htm").write_text(
                _dominican_html(str(1000 + i)), encoding="utf-8"
            )
        assert verify_sources.check_dominican() is True


# ── Freddoso checks ───────────────────────────────────────────────────────────

class TestCheckFreddoso:
    def _write_gaps(self, dest: Path) -> None:
        dest.mkdir(parents=True)
        (dest / "TOC-I.html").write_text("<html/>", encoding="utf-8")
        gaps = {"available": ["I.q1"], "missing": [], "notes": "complete"}
        (dest / "coverage_gaps.json").write_text(json.dumps(gaps), encoding="utf-8")

    def test_passes_with_toc_and_gaps(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        self._write_gaps(tmp_path / "sources" / "english" / "freddoso")
        assert verify_sources.check_freddoso() is True

    def test_fails_missing_gaps_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        dest = tmp_path / "sources" / "english" / "freddoso"
        dest.mkdir(parents=True)
        (dest / "TOC-I.html").write_text("<html/>", encoding="utf-8")
        assert verify_sources.check_freddoso() is False

    def test_fails_missing_toc(self, tmp_path, monkeypatch):
        monkeypatch.setattr("acquire.verify.ROOT",tmp_path)
        dest = tmp_path / "sources" / "english" / "freddoso"
        dest.mkdir(parents=True)
        gaps = {"available": [], "missing": [], "notes": "none"}
        (dest / "coverage_gaps.json").write_text(json.dumps(gaps), encoding="utf-8")
        assert verify_sources.check_freddoso() is False


# ── Env checks ────────────────────────────────────────────────────────────────

class TestCheckEnv:
    def test_passes_when_all_keys_set(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@localhost/x")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        assert verify_sources.check_env() is True

    def test_fails_on_missing_key(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert verify_sources.check_env() is False


# ── VerifySourcesStep (the pipeline wrapper) ──────────────────────────────────

class TestVerifySourcesStep:
    def test_ok_when_all_checks_pass(self, tmp_path):
        step = VerifySourcesStep(checks=[("a", lambda: True), ("b", lambda: True)])
        result = step.run(_ctx(tmp_path))
        assert result.ok is True
        assert result.name == "verify-sources"
        assert result.summary == "2/2 source checks passed"
        assert result.details["checks"] == {"a": True, "b": True}

    def test_not_ok_when_a_check_fails(self, tmp_path):
        step = VerifySourcesStep(checks=[("a", lambda: True), ("b", lambda: False)])
        result = step.run(_ctx(tmp_path))
        assert result.ok is False
        assert result.summary == "1/2 source checks passed"

    def test_check_exception_counts_as_failure(self, tmp_path):
        def boom():
            raise RuntimeError("no DB")

        step = VerifySourcesStep(checks=[("a", lambda: True), ("db", boom)])
        result = step.run(_ctx(tmp_path))
        assert result.ok is False
        assert result.details["checks"] == {"a": True, "db": False}

    def test_default_checks_are_the_module_checks(self):
        step = VerifySourcesStep()
        assert step._checks == list(verify_sources.CHECKS)


class TestStepsMain:
    def test_main_returns_0_when_all_pass(self, monkeypatch, tmp_path):
        # isolate the report destination so the run doesn't write into repo reports/
        monkeypatch.setattr(steps, "ROOT", tmp_path)
        monkeypatch.setattr(steps, "CHECKS", [("a", lambda: True)])
        assert steps.main() == 0

    def test_main_returns_1_when_a_check_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(steps, "ROOT", tmp_path)
        monkeypatch.setattr(steps, "CHECKS", [("a", lambda: False)])
        assert steps.main() == 1
