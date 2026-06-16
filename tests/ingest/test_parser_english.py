"""
Tests for src/ingest/ingest_english.py — pure parsing logic.
No DB, no live files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ingest.ingest_english import parse_english_for_articles

# Minimal Dominican HTML template mirroring the real structure.
_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<body>
<div id="springfield2">
  <h1>Question 3. On the simplicity of God</h1>
  <h2 id="article1">Article 1. Whether God is a body?</h2>
  <p><strong>Objection 1.</strong> It seems that God is a body.</p>
  <p><strong>On the contrary,</strong> John 4:24 says God is spirit.</p>
  <p><strong>I answer that,</strong> It is absolutely true that God is not a body.</p>
  <p><strong>Reply to Objection 1.</strong> The passages of Scripture are metaphorical.</p>
  <h2 id="article2">Article 2. Whether God is composed of matter and form?</h2>
  <p><strong>Objection 1.</strong> It seems that God is composed of matter and form.</p>
  <p><strong>I answer that,</strong> It is impossible that matter should exist in God.</p>
</div>
</body>
</html>
"""


def _write_html(path: Path, content: str = _HTML_TEMPLATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestMissingFile:
    def test_skip_prints_message(self, tmp_path, capsys, monkeypatch):
        """Missing file prints [SKIP] to stdout and returns no elements for that article."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        # No file written — file does not exist
        parse_english_for_articles(["I.q3.a1"])
        captured = capsys.readouterr()
        assert "[SKIP]" in captured.out
        assert "1003.html" in captured.out

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        """Missing file contributes zero elements to the result."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        result = parse_english_for_articles(["I.q3.a1"])
        assert result == []

    def test_missing_file_does_not_raise(self, tmp_path, monkeypatch):
        """No RuntimeError is raised for a missing Dominican file."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        # Must not raise
        parse_english_for_articles(["I.q3.a1"])

    def test_other_articles_not_affected(self, tmp_path, capsys, monkeypatch):
        """When one file is missing, articles whose file exists are still parsed."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        # Write file for q4 but not q3
        _write_html(tmp_path / "1004.html")
        result = parse_english_for_articles(["I.q3.a1", "I.q4.a1"])
        captured = capsys.readouterr()
        assert "[SKIP]" in captured.out
        locators = [e.locator for e in result]
        # q3 absent, q4 present
        assert not any("q3" in loc for loc in locators)
        assert any("q4" in loc for loc in locators)


class TestExistingFile:
    def test_returns_elements(self, tmp_path, monkeypatch):
        """Existing file returns a non-empty list of elements."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        _write_html(tmp_path / "1003.html")
        result = parse_english_for_articles(["I.q3.a1"])
        assert len(result) > 0

    def test_question_title_extracted(self, tmp_path, monkeypatch):
        """Question title element is produced with correct locator."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        _write_html(tmp_path / "1003.html")
        result = parse_english_for_articles(["I.q3.a1"])
        locators = [e.locator for e in result]
        assert "I.q3" in locators

    def test_article_title_extracted(self, tmp_path, monkeypatch):
        """Article title element is produced with correct locator."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        _write_html(tmp_path / "1003.html")
        result = parse_english_for_articles(["I.q3.a1"])
        locators = [e.locator for e in result]
        assert "I.q3.a1" in locators

    def test_objection_extracted(self, tmp_path, monkeypatch):
        """Objection paragraph produces an arg element."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        _write_html(tmp_path / "1003.html")
        result = parse_english_for_articles(["I.q3.a1"])
        locators = [e.locator for e in result]
        assert "I.q3.a1.arg1" in locators

    def test_sed_contra_extracted(self, tmp_path, monkeypatch):
        """'On the contrary' paragraph produces a sed_contra element."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        _write_html(tmp_path / "1003.html")
        result = parse_english_for_articles(["I.q3.a1"])
        locators = [e.locator for e in result]
        assert "I.q3.a1.sed_contra" in locators

    def test_respondeo_extracted(self, tmp_path, monkeypatch):
        """'I answer that' paragraph produces a respondeo element."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        _write_html(tmp_path / "1003.html")
        result = parse_english_for_articles(["I.q3.a1"])
        locators = [e.locator for e in result]
        assert "I.q3.a1.respondeo" in locators

    def test_reply_extracted(self, tmp_path, monkeypatch):
        """'Reply to Objection N' paragraph produces a reply element."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        _write_html(tmp_path / "1003.html")
        result = parse_english_for_articles(["I.q3.a1"])
        locators = [e.locator for e in result]
        assert "I.q3.a1.reply1" in locators

    def test_multiple_articles_same_file(self, tmp_path, monkeypatch):
        """Two articles from the same file are both parsed in one file read."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        _write_html(tmp_path / "1003.html")
        result = parse_english_for_articles(["I.q3.a1", "I.q3.a2"])
        locators = [e.locator for e in result]
        assert "I.q3.a1" in locators
        assert "I.q3.a2" in locators

    def test_question_title_deduplicated(self, tmp_path, monkeypatch):
        """Question title appears only once even when two articles share the same question."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        _write_html(tmp_path / "1003.html")
        result = parse_english_for_articles(["I.q3.a1", "I.q3.a2"])
        q_title_count = sum(1 for e in result if e.locator == "I.q3")
        assert q_title_count == 1

    def test_question_title_prefix_stripped(self, tmp_path, monkeypatch):
        """The 'Question N.' prefix is removed from the question title text."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        _write_html(tmp_path / "1003.html")
        result = parse_english_for_articles(["I.q3.a1"])
        q_elem = next(e for e in result if e.locator == "I.q3")
        assert not q_elem.text.lower().startswith("question")

    def test_no_skip_message_for_present_file(self, tmp_path, capsys, monkeypatch):
        """No [SKIP] message is printed when the file exists."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        _write_html(tmp_path / "1003.html")
        parse_english_for_articles(["I.q3.a1"])
        captured = capsys.readouterr()
        assert "[SKIP]" not in captured.out


class TestNoRuntimeErrorForMissingFile:
    def test_no_runtime_error(self, tmp_path, monkeypatch):
        """RuntimeError is NOT raised for a missing Dominican file (behaviour removed)."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        try:
            parse_english_for_articles(["I.q3.a1"])
        except RuntimeError as exc:
            pytest.fail(f"RuntimeError should not be raised for missing file, got: {exc}")


class TestFreddosoRouting:
    def test_uses_freddoso_when_file_present(self, tmp_path, monkeypatch):
        """When a Freddoso per-question HTML file exists, it is preferred over Dominican."""
        import ingest.ingest_english as mod

        freddoso_dir = tmp_path / "freddoso"
        dominican_dir = tmp_path / "dominican"
        monkeypatch.setattr(mod, "FREDDOSO_DIR", freddoso_dir)
        monkeypatch.setattr(mod, "DOMINICAN_DIR", dominican_dir)
        monkeypatch.setattr(mod, "_FREDDOSO_AVAILABLE", None)  # force reload

        # Write Freddoso file with distinct content
        freddoso_html = _HTML_TEMPLATE.replace("It is absolutely true that God is not a body",
                                               "FREDDOSO_RESPONDEO_MARKER")
        _write_html(freddoso_dir / "1003.html", freddoso_html)
        # Dominican file also present but should not be used
        _write_html(dominican_dir / "1003.html")

        result = parse_english_for_articles(["I.q3.a1"])
        respondeo = next(e for e in result if e.locator == "I.q3.a1.respondeo")
        assert "FREDDOSO_RESPONDEO_MARKER" in respondeo.text

    def test_falls_back_to_dominican_when_freddoso_absent(self, tmp_path, monkeypatch):
        """Falls back to Dominican when no Freddoso file is present."""
        import ingest.ingest_english as mod

        freddoso_dir = tmp_path / "freddoso"
        dominican_dir = tmp_path / "dominican"
        monkeypatch.setattr(mod, "FREDDOSO_DIR", freddoso_dir)
        monkeypatch.setattr(mod, "DOMINICAN_DIR", dominican_dir)
        monkeypatch.setattr(mod, "_FREDDOSO_AVAILABLE", None)

        _write_html(dominican_dir / "1003.html")  # only Dominican present

        result = parse_english_for_articles(["I.q3.a1"])
        assert len(result) > 0

    def test_coverage_gap_logged_when_freddoso_available_but_missing(self, tmp_path, monkeypatch):
        """Coverage gap is logged when coverage_gaps.json lists a question but file is absent."""
        import ingest.ingest_english as mod

        monkeypatch.setattr(mod, "DOMINICAN_DIR", tmp_path)
        monkeypatch.setattr(mod, "FREDDOSO_DIR", tmp_path / "freddoso_empty")
        # Simulate I.q3 being listed as available in Freddoso
        monkeypatch.setattr(mod, "_FREDDOSO_AVAILABLE", {"I.q3"})

        _write_html(tmp_path / "1003.html")  # Dominican present as fallback

        gaps: list[str] = []
        parse_english_for_articles(["I.q3.a1"], coverage_gap_log=gaps)
        assert any("FREDDOSO_MISSING" in g and "I.q3" in g for g in gaps)
