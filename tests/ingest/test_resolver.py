"""
Tests for src/ingest/resolver.py — pure resolution logic, no DB.
"""

from __future__ import annotations

from unittest.mock import patch

from ingest.resolver import (
    _call_deepseek,
    _call_deepseek_batch,
    _propose_gap_terms,
    _resolve_multi,
    _resolve_single,
    _scan_gap_lemmas,
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


# ── _call_deepseek_batch ──────────────────────────────────────────────────────

class TestCallDeepseekBatch:
    def _fake_response(self, content: str):
        from unittest.mock import MagicMock
        mock = MagicMock()
        mock.raise_for_status = lambda: None
        mock.json.return_value = {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
        return mock

    def test_parses_json_response(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        batch = [
            {"lemma": "ratio", "best_latin": "ratio est", "best_czech": "", "best_english": "reason"},
            {"lemma": "anima", "best_latin": "anima vivit", "best_czech": "duše", "best_english": "soul"},
        ]
        with patch("ingest.resolver.requests.post") as mock_post:
            mock_post.return_value = self._fake_response('{"ratio": "rozum", "anima": "duša"}')
            result = _call_deepseek_batch(batch)
        assert result == {"ratio": "rozum", "anima": "duša"}

    def test_strips_markdown_fences(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        batch = [{"lemma": "corpus", "best_latin": "", "best_czech": "", "best_english": ""}]
        with patch("ingest.resolver.requests.post") as mock_post:
            mock_post.return_value = self._fake_response('```json\n{"corpus": "telo"}\n```')
            result = _call_deepseek_batch(batch)
        assert result == {"corpus": "telo"}

    def test_returns_empty_on_api_error(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        batch = [{"lemma": "virtus", "best_latin": "", "best_czech": "", "best_english": ""}]
        with patch("ingest.resolver.requests.post") as mock_post:
            mock_post.side_effect = Exception("timeout")
            result = _call_deepseek_batch(batch)
        assert result == {}

    def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        import pytest
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            _call_deepseek_batch([])

    def test_increments_call_count(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        before = get_api_stats()["calls"]
        batch = [{"lemma": "pax", "best_latin": "", "best_czech": "", "best_english": ""}]
        with patch("ingest.resolver.requests.post") as mock_post:
            mock_post.return_value = self._fake_response('{"pax": "pokoj"}')
            _call_deepseek_batch(batch)
        assert get_api_stats()["calls"] == before + 1


# ── _scan_gap_lemmas ──────────────────────────────────────────────────────────

class TestScanGapLemmas:
    def _seg(self, latin="", czech="", english="", seg_id=1):
        return {"segment_id": seg_id, "latin": latin, "czech": czech,
                "english": english, "element_type": "arg", "locator_path": "I.q1.a1.arg1"}

    def test_respects_freq_floor(self):
        # 'virtus' appears in 2 segments, floor=3 → excluded
        segs = [
            self._seg("virtus bona est", seg_id=1),
            self._seg("virtus magna est", seg_id=2),
        ]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=3, pos_filter=None)
        assert "virtus" not in result

    def test_includes_above_floor(self):
        segs = [self._seg(f"virtus in segmento {i}", seg_id=i) for i in range(5)]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=3, pos_filter=None)
        # virtus should appear (freq=5 >= 3)
        assert "virtus" in result

    def test_excludes_krystal_lemmas(self):
        segs = [self._seg("essentia divina est", seg_id=i) for i in range(5)]
        result = _scan_gap_lemmas(segs, krystal_lemmas={"essentia"}, freq_floor=1, pos_filter=None)
        assert "essentia" not in result

    def test_skips_short_tokens(self):
        # 'deus' (4 chars) should be filtered by the >5 char rule
        segs = [self._seg("deus bonus", seg_id=i) for i in range(5)]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=1, pos_filter=None)
        assert "deus" not in result

    def test_pos_filter_excludes_verbs(self):
        # 'dico' lemmatizes and typically tags as verb (V) — should be excluded
        segs = [self._seg("dico tibi verum", seg_id=i) for i in range(15)]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=10,
                                   pos_filter=frozenset({"N", "A"}))
        # Verbs should not appear (dico = V)
        for lemma in result:
            assert lemma != "dico2"

    def test_no_pos_filter_includes_verbs(self):
        # With pos_filter=None, freq-qualified lemmas of any POS are included.
        # Use tokens >5 chars that the lemmatizer/POS tagger will recognise.
        segs = [self._seg("virtutes animae dicuntur bonae", seg_id=i) for i in range(15)]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=10, pos_filter=None)
        assert len(result) > 0

    def test_collects_context(self):
        segs = [self._seg("virtus magna", czech="ctnost", english="virtue", seg_id=i) for i in range(5)]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=3, pos_filter=None)
        if "virtus" in result:
            assert result["virtus"]["best_czech"] == "ctnost"
            assert result["virtus"]["best_english"] == "virtue"


# ── _propose_gap_terms ────────────────────────────────────────────────────────

class TestProposeGapTerms:
    def test_uses_batch_call_and_merges(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        gap_data = {
            "virtus": {"freq": 20, "best_latin": "virtus", "best_czech": "", "best_english": "virtue"},
            "anima": {"freq": 15, "best_latin": "anima", "best_czech": "duše", "best_english": "soul"},
        }
        with patch("ingest.resolver._call_deepseek_batch") as mock_batch:
            mock_batch.return_value = {"virtus": "cnosť", "anima": "duša"}
            result = _propose_gap_terms(gap_data, batch_size=10, max_workers=1)
        assert result["virtus"] == "cnosť"
        assert result["anima"] == "duša"

    def test_fills_missing_with_stubs(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        gap_data = {"ratio": {"freq": 10, "best_latin": "", "best_czech": "", "best_english": ""}}
        with patch("ingest.resolver._call_deepseek_batch") as mock_batch:
            mock_batch.return_value = {}  # batch call returns nothing
            result = _propose_gap_terms(gap_data, batch_size=10, max_workers=1)
        assert result["ratio"] == "[model_proposed: ratio]"

    def test_batches_correctly(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        gap_data = {
            f"lemma{i}": {"freq": 10, "best_latin": "", "best_czech": "", "best_english": ""}
            for i in range(10)
        }
        calls = []
        def fake_batch(batch):
            calls.append(len(batch))
            return {item["lemma"]: f"sk_{item['lemma']}" for item in batch}

        with patch("ingest.resolver._call_deepseek_batch", side_effect=fake_batch):
            result = _propose_gap_terms(gap_data, batch_size=3, max_workers=1)

        assert len(result) == 10
        # 10 lemmas at batch_size=3 → 4 batches (3+3+3+1)
        assert len(calls) == 4
