"""
Tests for src/ingest/resolver.py — pure resolution logic, no DB.
"""

from __future__ import annotations

from unittest.mock import patch

from ingest.resolver import (
    _call_deepseek,
    _resolve_multi,
    _resolve_single,
    get_api_stats,
    mask_spans,
    phrase_match,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _sense(sense_id: int, context_label: str | None = None,
           cs_lemma: str = "", en_cue: str = "", sk_content: str = "") -> dict:
    return {
        "sense_id": sense_id,
        "context_label": context_label,
        "version": 1,
        "cs_lemma": cs_lemma,
        "cs_content": cs_lemma,
        "en_cue": en_cue,
        "sk_content": sk_content,
    }


def _term(term_id: int, lemma: str, senses: list[dict], is_multiword: bool = False) -> dict:
    return {
        "term_id": term_id,
        "latin_lemma": lemma,
        "is_multiword": is_multiword,
        "senses": senses,
    }


# ── phrase_match ──────────────────────────────────────────────────────────────

class TestPhraseMatch:
    def _mw_term(self, lemma: str) -> dict:
        return _term(1, lemma, [_sense(1)], is_multiword=True)

    def test_finds_exact_phrase(self):
        term = self._mw_term("actus essendi")
        matches = phrase_match("Hoc est actus essendi in rebus.", [term])
        assert len(matches) == 1
        assert matches[0][0]["latin_lemma"] == "actus essendi"

    def test_case_insensitive(self):
        term = self._mw_term("per se")
        matches = phrase_match("Per se notum est.", [term])
        assert len(matches) == 1

    def test_no_match(self):
        term = self._mw_term("actus essendi")
        matches = phrase_match("Homo est animal rationale.", [term])
        assert matches == []

    def test_multiple_terms(self):
        t1 = self._mw_term("per se")
        t2 = self._mw_term("per accidens")
        text = "Aliquid dicitur per se et per accidens."
        matches = phrase_match(text, [t1, t2])
        assert len(matches) == 2

    def test_no_overlapping_matches(self):
        # "per se" is a prefix of "per seipsum" — should not double-match
        t1 = self._mw_term("per se")
        t2 = self._mw_term("per seipsum")
        text = "Movetur per seipsum."
        matches = phrase_match(text, [t1, t2])
        # Only the longer or first non-overlapping match should win
        assert len(matches) == 1


# ── mask_spans ────────────────────────────────────────────────────────────────

class TestMaskSpans:
    def test_masks_matched_term(self):
        term = _term(1, "per se", [_sense(1)], is_multiword=True)
        result = mask_spans("Notum est per se.", [term])
        assert "per se" not in result.lower()

    def test_unmasked_text_preserved(self):
        term = _term(1, "per se", [_sense(1)], is_multiword=True)
        result = mask_spans("Homo per se vivit.", [term])
        assert "Homo" in result
        assert "vivit" in result

    def test_empty_terms_noop(self):
        text = "Deus est ens."
        assert mask_spans(text, []) == text


# ── _resolve_single ───────────────────────────────────────────────────────────

class TestResolveSingle:
    def test_returns_krystal_single(self):
        term = _term(1, "essentia", [_sense(10)])
        res = _resolve_single(term)
        assert res.method == "krystal_single"
        assert res.confidence == "auto"
        assert res.sense["sense_id"] == 10

    def test_no_signals(self):
        term = _term(1, "essentia", [_sense(10)])
        res = _resolve_single(term)
        assert res.signals == {}


# ── _resolve_multi ────────────────────────────────────────────────────────────

class TestResolveMulti:
    def _two_sense_term(self) -> dict:
        return _term(1, "concupiscentia", [
            _sense(101, "důsledek dědičného hříchu", cs_lemma="žádostivost", en_cue="desire"),
            _sense(102, "vášeň", cs_lemma="dychtění", en_cue="passion"),
        ])

    def test_voted_by_cs_signal(self):
        # Czech text contains "dychtění" → sense 102
        term = self._two_sense_term()
        res = _resolve_multi(term, "dychtění touhou", None, cs_rank=20, en_rank=30)
        assert res.method == "krystal_multi_voted"
        assert res.confidence == "auto"
        assert res.sense["sense_id"] == 102

    def test_voted_by_en_signal_with_strong_cs(self):
        # English says "passion" → sense 102; cs_rank=20 ≤ threshold → strong
        term = self._two_sense_term()
        # CS matches dychtění → sense_102 (rank 20 = strong threshold)
        res = _resolve_multi(term, "dychtění", "of passion", cs_rank=20, en_rank=30)
        assert res.method == "krystal_multi_voted"
        assert res.sense["sense_id"] == 102

    def test_flagged_when_no_signals(self):
        term = self._two_sense_term()
        res = _resolve_multi(term, None, None, cs_rank=20, en_rank=30)
        assert res.method == "krystal_multi_flagged"
        assert res.confidence == "needs_review"

    def test_flagged_when_split_signals(self):
        # CS says sense_101, EN says sense_102 → split → flag
        term = self._two_sense_term()
        # Czech "žádostivost" → sense 101; English "passion" → sense 102
        res = _resolve_multi(term, "žádostivost", "passion", cs_rank=20, en_rank=30)
        assert res.method == "krystal_multi_flagged"
        assert res.confidence == "needs_review"

    def test_flagged_when_only_weak_signal(self):
        # Only EN signal (rank=30 > _STRONG_RANK_THRESHOLD=20) → no strong signal → flag
        term = self._two_sense_term()
        res = _resolve_multi(term, None, "passion", cs_rank=20, en_rank=30)
        assert res.method == "krystal_multi_flagged"
        assert res.confidence == "needs_review"

    def test_voted_signals_recorded(self):
        term = self._two_sense_term()
        res = _resolve_multi(term, "dychtění", None, cs_rank=20, en_rank=30)
        assert res.method == "krystal_multi_voted"
        assert res.signals  # non-empty

    def test_primary_sense_chosen_when_flagged(self):
        # When flagged, the sense with context_label=None is chosen
        term = _term(1, "gratia", [
            _sense(201, None, cs_lemma="milost", en_cue="grace"),
            _sense(202, "v případě ctnosti", cs_lemma="vděčnost", en_cue="gratitude"),
        ])
        res = _resolve_multi(term, None, None, cs_rank=20, en_rank=30)
        assert res.method == "krystal_multi_flagged"
        assert res.sense["sense_id"] == 201  # primary (context_label=None)

    def test_deterministic_with_same_input(self):
        term = self._two_sense_term()
        r1 = _resolve_multi(term, "dychtění", None, cs_rank=20, en_rank=30)
        r2 = _resolve_multi(term, "dychtění", None, cs_rank=20, en_rank=30)
        assert r1.method == r2.method
        assert r1.sense["sense_id"] == r2.sense["sense_id"]


# ── _call_deepseek ────────────────────────────────────────────────────────────

class TestCallDeepseek:
    def test_returns_proposed_term(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

        fake_response = {
            "choices": [{"message": {"content": "rozum"}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5},
        }

        with patch("ingest.resolver.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.raise_for_status = lambda: None
            mock_post.return_value.json = lambda: fake_response

            result = _call_deepseek("ratio", "context text", "rozum", "reason")

        assert result == "rozum"

    def test_accumulates_api_stats(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

        initial_calls = get_api_stats()["calls"]

        fake_response = {
            "choices": [{"message": {"content": "duša"}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 3},
        }

        with patch("ingest.resolver.requests.post") as mock_post:
            mock_post.return_value.raise_for_status = lambda: None
            mock_post.return_value.json = lambda: fake_response

            _call_deepseek("anima", "context", "", "soul")

        stats = get_api_stats()
        assert stats["calls"] == initial_calls + 1
        assert stats["output_tokens"] >= 3

    def test_returns_stub_on_api_error(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

        with patch("ingest.resolver.requests.post") as mock_post:
            mock_post.side_effect = Exception("network error")
            result = _call_deepseek("corpus", "context", "", "")

        assert result == "[model_proposed: corpus]"

    def test_increments_calls_on_error(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        before = get_api_stats()["calls"]

        with patch("ingest.resolver.requests.post") as mock_post:
            mock_post.side_effect = Exception("timeout")
            _call_deepseek("spiritus", "context", "", "")

        assert get_api_stats()["calls"] == before + 1

    def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        import pytest
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            _call_deepseek("ratio", "context", "", "")
