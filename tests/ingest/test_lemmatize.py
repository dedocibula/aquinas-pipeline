"""
Unit tests for src/ingest/lemmatize.py.

Tests are split into two classes:
  TestLemmatizeLatin  — requires CLTK lat_models_cltk corpus
  TestLemmatizeCzech  — requires models/czech-morfflex*.dict

Both classes are skipped (not failed) when the required model is absent,
so CI without the models doesn't produce false failures. The presence check
happens at module import time via the _check_ fixtures.
"""

from __future__ import annotations

import pathlib

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────

def _cltk_models_present() -> bool:
    model_path = (
        pathlib.Path.home()
        / "cltk_data/lat/model/lat_models_cltk/lemmata/backoff"
    )
    return model_path.exists()


def _morphodita_model_present() -> bool:
    models_dir = pathlib.Path(__file__).resolve().parents[2] / "models"
    return any(models_dir.rglob("czech-morfflex*.dict"))


# ── Latin lemmatizer ─────────────────────────────────────────────────────────

@pytest.mark.skipif(not _cltk_models_present(), reason="lat_models_cltk not downloaded")
class TestLemmatizeLatin:
    def setup_method(self):
        from ingest.lemmatize import lemmatize_latin
        self.lemmatize = lemmatize_latin

    def test_essentia(self):
        result = self.lemmatize("essentiam")
        assert "essentia" in result, f"expected 'essentia' in {result}"

    def test_homo(self):
        result = self.lemmatize("hominem")
        assert "homo" in result, f"expected 'homo' in {result}"

    def test_ratio(self):
        result = self.lemmatize("rationem")
        assert "ratio" in result, f"expected 'ratio' in {result}"

    def test_returns_list(self):
        result = self.lemmatize("deus")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_already_lemma_form(self):
        # Nominative singular should return itself (or a valid lemma)
        result = self.lemmatize("homo")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_unknown_word_returns_something(self):
        # Unknown words must still return a non-empty list (fallback to surface)
        result = self.lemmatize("xyzzylatinform")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_no_empty_strings_in_result(self):
        result = self.lemmatize("essentiam")
        assert all(r for r in result), f"empty string in result: {result}"


# ── Czech lemmatizer ──────────────────────────────────────────────────────────

@pytest.mark.skipif(not _morphodita_model_present(), reason="czech-morfflex*.dict not present in models/")
class TestLemmatizeCzech:
    def setup_method(self):
        from ingest.lemmatize import lemmatize_czech
        self.lemmatize = lemmatize_czech

    def test_dychteniv_nominative(self):
        result = self.lemmatize("dychtění")
        assert "dychtění" in result, f"expected 'dychtění' in {result}"

    def test_dychtenim_instrumental(self):
        result = self.lemmatize("dychtěním")
        assert "dychtění" in result, f"expected 'dychtění' in {result}"

    def test_returns_list(self):
        result = self.lemmatize("člověk")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_no_empty_strings_in_result(self):
        result = self.lemmatize("člověka")
        assert all(r for r in result), f"empty string in result: {result}"

    def test_clovek_genitive(self):
        result = self.lemmatize("člověka")
        assert "člověk" in result, f"expected 'člověk' in {result}"

    def test_unknown_word_returns_something(self):
        result = self.lemmatize("xyzzyneznámé")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_lemma_suffix_stripped(self):
        # MorphoDiTa raw lemmas contain suffixes like `_:B_` — must be stripped
        result = self.lemmatize("dychtění")
        for r in result:
            assert "_" not in r, f"raw MorphoDiTa suffix not stripped: {r!r}"
