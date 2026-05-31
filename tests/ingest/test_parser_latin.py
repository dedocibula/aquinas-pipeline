"""
Tests for src/ingest/parser_latin.py — pure parsing logic only.
No DB, no live files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ingest.parser_latin import (
    ParsedElement,
    _article_locator,
    _check_article,
    _group_elements_by_article,
    _parse_title_full,
    _question_locator,
    run_full,
)

# ── _parse_title_full ─────────────────────────────────────────────────────────

class TestParseTitleFull:
    def test_arg_prima_pars(self):
        locator, etype = _parse_title_full("I q. 3 a. 1 arg. 1")
        assert locator == "I.q3.a1.arg1"
        assert etype == "arg"

    def test_sed_contra(self):
        locator, etype = _parse_title_full("I q. 3 a. 1 s. c.")
        assert locator == "I.q3.a1.sed_contra"
        assert etype == "sed_contra"

    def test_respondeo(self):
        locator, etype = _parse_title_full("I q. 3 a. 1 co.")
        assert locator == "I.q3.a1.respondeo"
        assert etype == "respondeo"

    def test_reply_numbered(self):
        locator, etype = _parse_title_full("I q. 3 a. 1 ad 2")
        assert locator == "I.q3.a1.reply2"
        assert etype == "reply"

    def test_reply_combined_ad_arg(self):
        locator, etype = _parse_title_full("I q. 1 a. 4 ad arg.")
        assert locator == "I.q1.a4.reply0"
        assert etype == "reply"

    def test_preamble(self):
        locator, etype = _parse_title_full("I q. 3 pr.")
        assert locator == "I.q3.preamble"
        assert etype == "preamble"

    def test_prima_secundae(self):
        locator, etype = _parse_title_full("I-II q. 5 a. 1 arg. 1")
        assert locator == "I_II.q5.a1.arg1"
        assert etype == "arg"

    def test_secunda_secundae(self):
        locator, etype = _parse_title_full("II-II q. 23 a. 1 s. c.")
        assert locator == "II_II.q23.a1.sed_contra"
        assert etype == "sed_contra"

    def test_tertia_pars(self):
        locator, etype = _parse_title_full("III q. 75 a. 4 co.")
        assert locator == "III.q75.a4.respondeo"
        assert etype == "respondeo"

    def test_multi_digit_question(self):
        locator, etype = _parse_title_full("I-II q. 94 a. 2 arg. 1")
        assert locator == "I_II.q94.a2.arg1"
        assert etype == "arg"

    def test_multi_digit_reply(self):
        locator, etype = _parse_title_full("II-II q. 64 a. 7 ad 3")
        assert locator == "II_II.q64.a7.reply3"
        assert etype == "reply"

    def test_unrecognised_returns_none(self):
        assert _parse_title_full("Summa Theologiae pr.") is None

    def test_no_pars_returns_none(self):
        assert _parse_title_full("q. 3 a. 1 arg. 1") is None

    def test_leading_whitespace_ignored(self):
        result = _parse_title_full("  I q. 1 a. 1 co.  ")
        assert result is not None
        assert result[0] == "I.q1.a1.respondeo"

    def test_ltree_no_hyphens_in_locator(self):
        # ltree labels must not contain hyphens
        locator, _ = _parse_title_full("I-II q. 5 a. 1 co.")
        assert "-" not in locator

    def test_arg_high_number(self):
        locator, etype = _parse_title_full("I q. 1 a. 1 arg. 12")
        assert locator == "I.q1.a1.arg12"
        assert etype == "arg"


# ── _article_locator ──────────────────────────────────────────────────────────

class TestArticleLocator:
    def test_arg(self):
        assert _article_locator("I q. 3 a. 1 arg. 1") == "I.q3.a1"

    def test_sed_contra(self):
        assert _article_locator("I q. 3 a. 1 s. c.") == "I.q3.a1"

    def test_respondeo(self):
        assert _article_locator("I q. 3 a. 1 co.") == "I.q3.a1"

    def test_reply(self):
        assert _article_locator("I q. 13 a. 5 ad 2") == "I.q13.a5"

    def test_preamble_returns_none(self):
        assert _article_locator("I q. 3 pr.") is None

    def test_unrecognised_returns_none(self):
        assert _article_locator("garbage") is None

    def test_prima_secundae(self):
        assert _article_locator("I-II q. 94 a. 2 co.") == "I_II.q94.a2"


# ── _question_locator ─────────────────────────────────────────────────────────

class TestQuestionLocator:
    def test_prima_pars(self):
        assert _question_locator("I q. 3 a. 1 arg. 1") == "I.q3"

    def test_preamble(self):
        assert _question_locator("I q. 3 pr.") == "I.q3"

    def test_i_ii(self):
        assert _question_locator("I-II q. 5 a. 1 co.") == "I_II.q5"

    def test_ii_ii(self):
        assert _question_locator("II-II q. 23 a. 1 s. c.") == "II_II.q23"

    def test_iii(self):
        assert _question_locator("III q. 75 a. 4 ad 1") == "III.q75"

    def test_garbage_returns_none(self):
        assert _question_locator("random text") is None


# ── _check_article ────────────────────────────────────────────────────────────

def _elem(etype: str, locator: str = "I.q3.a1.x") -> ParsedElement:
    return ParsedElement(locator, etype, "text", None)


class TestCheckArticle:
    def test_complete_article_passes(self):
        elements = [
            _elem("arg"),
            _elem("sed_contra"),
            _elem("respondeo"),
            _elem("reply"),
        ]
        _check_article("I.q3.a1", elements)  # no exception

    def test_missing_sed_contra_raises(self):
        elements = [_elem("arg"), _elem("respondeo"), _elem("reply")]
        with pytest.raises(RuntimeError, match="I.q3.a1"):
            _check_article("I.q3.a1", elements)
        with pytest.raises(RuntimeError, match="sed_contra"):
            _check_article("I.q3.a1", elements)

    def test_missing_respondeo_raises(self):
        elements = [_elem("arg"), _elem("sed_contra"), _elem("reply")]
        with pytest.raises(RuntimeError, match="respondeo"):
            _check_article("I.q3.a1", elements)

    def test_missing_arg_raises(self):
        elements = [_elem("sed_contra"), _elem("respondeo"), _elem("reply")]
        with pytest.raises(RuntimeError, match="arg"):
            _check_article("I.q3.a1", elements)

    def test_missing_reply_raises(self):
        elements = [_elem("arg"), _elem("sed_contra"), _elem("respondeo")]
        with pytest.raises(RuntimeError, match="reply"):
            _check_article("I.q3.a1", elements)

    def test_preamble_not_required(self):
        # preamble is not a required structural element
        elements = [
            _elem("preamble"),
            _elem("arg"),
            _elem("sed_contra"),
            _elem("respondeo"),
            _elem("reply"),
        ]
        _check_article("I.q3.a1", elements)  # no exception


# ── _group_elements_by_article ────────────────────────────────────────────────

def _pelm(locator: str, etype: str = "arg") -> ParsedElement:
    return ParsedElement(locator, etype, "text", None)


class TestGroupElementsByArticle:
    def test_groups_by_article(self):
        elems = [
            _pelm("I.q3.a1.arg1", "arg"),
            _pelm("I.q3.a1.respondeo", "respondeo"),
            _pelm("I.q3.a2.arg1", "arg"),
        ]
        grouped = _group_elements_by_article(elems)
        assert set(grouped.keys()) == {"I.q3.a1", "I.q3.a2"}
        assert len(grouped["I.q3.a1"]) == 2
        assert len(grouped["I.q3.a2"]) == 1

    def test_skips_non_article_locators(self):
        # preamble is at question level (pars.qN.preamble), not article level
        elems = [
            _pelm("I.q3.preamble", "preamble"),
            _pelm("I.q3.a1.arg1", "arg"),
        ]
        grouped = _group_elements_by_article(elems)
        assert "I.q3.a1" in grouped
        assert "I.q3" not in grouped

    def test_skips_question_level_locators(self):
        # question_title is at pars.qN level (2 parts only)
        elems = [
            _pelm("I.q3", "question_title"),
            _pelm("I.q3.a1.respondeo", "respondeo"),
        ]
        grouped = _group_elements_by_article(elems)
        assert list(grouped.keys()) == ["I.q3.a1"]

    def test_empty_input(self):
        assert _group_elements_by_article([]) == {}

    def test_multiple_pars(self):
        elems = [
            _pelm("I.q1.a1.arg1", "arg"),
            _pelm("I_II.q5.a1.respondeo", "respondeo"),
            _pelm("III.q75.a4.reply1", "reply"),
        ]
        grouped = _group_elements_by_article(elems)
        assert set(grouped.keys()) == {"I.q1.a1", "I_II.q5.a1", "III.q75.a4"}


# ── run_full (anomaly logging) ────────────────────────────────────────────────

def _make_latin_html(tmp_path: Path, filename: str, titles: list[str]) -> Path:
    """Create a minimal CT HTML file with the given TITLE attributes."""
    paragraphs = "\n".join(f'<p title="{t}">Text for {t}.</p>' for t in titles)
    html = f"<html><body>{paragraphs}</body></html>"
    p = tmp_path / filename
    p.write_text(html, encoding="utf-8")
    return p


class TestRunFull:
    def test_logs_anomaly_and_continues(self, tmp_path, monkeypatch):
        """run_full catches per-article structural errors, logs them, continues."""
        import ingest.parser_latin as pl

        # sth0001: complete article (I.q1.a1)
        _make_latin_html(tmp_path, "sth0001.html", [
            "I q. 1 a. 1 arg. 1",
            "I q. 1 a. 1 s. c.",
            "I q. 1 a. 1 co.",
            "I q. 1 a. 1 ad 1",
        ])
        # sth0002: incomplete article (I.q2.a1 — missing sed_contra/respondeo/reply)
        _make_latin_html(tmp_path, "sth0002.html", [
            "I q. 2 a. 1 arg. 1",
        ])

        inserted = []

        def fake_insert(conn, locator, elems, wid, src):
            inserted.append(locator)

        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def commit(self): pass
            def rollback(self): pass

        from contextlib import contextmanager

        @contextmanager
        def fake_get_conn():
            yield FakeConn()

        monkeypatch.setattr(pl, "_insert_article", fake_insert)
        monkeypatch.setattr(pl, "get_conn", fake_get_conn)
        monkeypatch.setattr(pl, "work_id", lambda conn, s: 1)
        monkeypatch.setattr(pl, "source_id", lambda conn, s: 2)

        log_path = tmp_path / "anomalies.txt"
        result = run_full(log_path, latin_dir=tmp_path)

        assert result["total"] == 2
        assert result["ingested"] == 1
        assert result["anomalies"] == 1
        assert inserted == ["I.q1.a1"]

        log_content = log_path.read_text()
        assert "[ANOMALY]" in log_content
        assert "I.q2.a1" in log_content
        assert "sth0002.html" in log_content

    def test_skips_index_file(self, tmp_path, monkeypatch):
        """sth0000.html is never parsed."""
        import ingest.parser_latin as pl

        # sth0000 has content — must be ignored
        (tmp_path / "sth0000.html").write_text("<html><body></body></html>")
        # sth0001: complete article
        _make_latin_html(tmp_path, "sth0001.html", [
            "I q. 1 a. 1 arg. 1",
            "I q. 1 a. 1 s. c.",
            "I q. 1 a. 1 co.",
            "I q. 1 a. 1 ad 1",
        ])

        inserted = []

        def fake_insert(conn, locator, elems, wid, src):
            inserted.append(locator)

        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def commit(self): pass
            def rollback(self): pass

        from contextlib import contextmanager

        @contextmanager
        def fake_get_conn():
            yield FakeConn()

        monkeypatch.setattr(pl, "_insert_article", fake_insert)
        monkeypatch.setattr(pl, "get_conn", fake_get_conn)
        monkeypatch.setattr(pl, "work_id", lambda conn, s: 1)
        monkeypatch.setattr(pl, "source_id", lambda conn, s: 2)

        log_path = tmp_path / "anomalies.txt"
        run_full(log_path, latin_dir=tmp_path)

        assert inserted == ["I.q1.a1"]

    def test_creates_log_directory(self, tmp_path, monkeypatch):
        """Log parent directories are created if they don't exist."""
        import ingest.parser_latin as pl

        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def commit(self): pass
            def rollback(self): pass

        from contextlib import contextmanager

        @contextmanager
        def fake_get_conn():
            yield FakeConn()

        monkeypatch.setattr(pl, "get_conn", fake_get_conn)
        monkeypatch.setattr(pl, "work_id", lambda conn, s: 1)
        monkeypatch.setattr(pl, "source_id", lambda conn, s: 2)

        empty_dir = tmp_path / "empty_latin"
        empty_dir.mkdir()
        nested_log = tmp_path / "reports" / "nested" / "anomalies.txt"
        run_full(nested_log, latin_dir=empty_dir)
        assert nested_log.exists()

    def test_returns_zero_counts_for_empty_corpus(self, tmp_path, monkeypatch):
        import ingest.parser_latin as pl

        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def commit(self): pass
            def rollback(self): pass

        from contextlib import contextmanager

        @contextmanager
        def fake_get_conn():
            yield FakeConn()

        monkeypatch.setattr(pl, "get_conn", fake_get_conn)
        monkeypatch.setattr(pl, "work_id", lambda conn, s: 1)
        monkeypatch.setattr(pl, "source_id", lambda conn, s: 2)

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        result = run_full(tmp_path / "log.txt", latin_dir=empty_dir)
        assert result == {"total": 0, "ingested": 0, "anomalies": 0}
