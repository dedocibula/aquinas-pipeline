"""
Tests for ingest resolver modules — pure resolution logic, no DB.
"""

from __future__ import annotations

from unittest.mock import patch

from common.deepseek import _call_deepseek_batch, _parse_batch_entry, get_api_stats
from ingest.gap_terms import (
    _canonical_lemma,
    _load_existing_gap_terms,
    _propose_gap_terms,
    _scan_gap_lemmas,
    _strip_lemma_suffix,
    pilot_batch_sizes,
)
from ingest.resolver import (
    _match_pattern,
    _resolve_multi,
    _resolve_single,
    _suppressed_habitus_tokens,
    mask_spans,
    phrase_match,
    resolve_segment,
)
from storage.models import Segment, Sense, Term

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _sense(sense_id: int, context_label: str | None = None,
           cs_lemma: str = "", en_cue: str = "", sk_content: str = "") -> Sense:
    return Sense(
        sense_id=sense_id,
        context_label=context_label,
        version=1,
        cs_lemma=cs_lemma,
        cs_content=cs_lemma,
        en_cue=en_cue,
        sk_content=sk_content,
        la_surface=None,
    )


def _term(
    term_id: int,
    lemma: str,
    senses: list[Sense],
    is_multiword: bool = False,
    category: str = "term",
    la_surface: str | None = None,
) -> Term:
    return Term(
        term_id=term_id,
        latin_lemma=lemma,
        is_multiword=is_multiword,
        category=category,
        la_surface=la_surface,
        senses=tuple(senses),
    )


# ── phrase_match ──────────────────────────────────────────────────────────────

class TestPhraseMatch:
    def _mw_term(self, lemma: str) -> dict:
        return _term(1, lemma, [_sense(1)], is_multiword=True)

    def test_finds_exact_phrase(self):
        term = self._mw_term("actus essendi")
        matches = phrase_match("Hoc est actus essendi in rebus.", [term])
        assert len(matches) == 1
        assert matches[0][0].latin_lemma == "actus essendi"

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


# ── _match_pattern ────────────────────────────────────────────────────────────


class TestMatchPattern:
    def _formula_term(self, lemma: str, la_surface: str) -> dict:
        return _term(1, lemma, [_sense(1)], is_multiword=True, category="formula", la_surface=la_surface)

    def _regular_term(self, lemma: str, la_surface: str | None = None) -> dict:
        return _term(1, lemma, [_sense(1)], is_multiword=True, la_surface=la_surface)

    def test_uses_la_surface_over_latin_lemma(self):
        """la_surface is the match pattern when present, not latin_lemma."""
        term = self._regular_term("sed_contra", la_surface="Sed contra")
        pattern = _match_pattern(term)
        assert pattern.search("Sed contra est quod dicit Augustinus")
        assert not pattern.search("sed_contra is a slug")

    def test_falls_back_to_latin_lemma_when_no_la_surface(self):
        """latin_lemma is used when la_surface is absent."""
        term = self._regular_term("per se", la_surface=None)
        pattern = _match_pattern(term)
        assert pattern.search("Movetur per se.")
        assert not pattern.search("something else")

    def test_formula_anchored_at_start(self):
        """Formula pattern matches at start-of-text only."""
        term = self._formula_term("respondeo", la_surface="Respondeo dicendum")
        pattern = _match_pattern(term)
        assert pattern.match("Respondeo dicendum quod...")
        assert not pattern.search("Ideo Respondeo dicendum quod...")  # mid-text must not match

    def test_non_formula_not_anchored(self):
        """Non-formula multiword term matches anywhere in text."""
        term = self._regular_term("actus essendi", la_surface=None)
        pattern = _match_pattern(term)
        assert pattern.search("Hoc est actus essendi rerum.")

    def test_formula_case_insensitive(self):
        """Formula anchor match is case-insensitive."""
        term = self._formula_term("sed_contra", la_surface="Sed contra")
        pattern = _match_pattern(term)
        assert pattern.match("sed contra est quod...")

    def test_phrase_match_uses_la_surface(self):
        """phrase_match uses la_surface as the match pattern end-to-end."""
        term = _term(1, "sed_contra", [_sense(1)], is_multiword=True,
                     category="formula", la_surface="Sed contra")
        matches = phrase_match("Sed contra est quod dicit Augustinus.", [term])
        assert len(matches) == 1

    def test_phrase_match_formula_not_matched_mid_text(self):
        """Formula opener in the middle of text is not matched by phrase_match."""
        term = _term(1, "sed_contra", [_sense(1)], is_multiword=True,
                     category="formula", la_surface="Sed contra")
        # "Sed contra" not at start — no match expected
        matches = phrase_match("Ideo, Sed contra est quod dicit.", [term])
        assert len(matches) == 0

    def test_mask_spans_uses_la_surface(self):
        """mask_spans masks via la_surface, not latin_lemma slug."""
        term = _term(1, "sed_contra", [_sense(1)], is_multiword=True,
                     category="formula", la_surface="Sed contra")
        result = mask_spans("Sed contra est quod dicit.", [term])
        assert "sed_contra" not in result
        assert "Sed contra" not in result


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
        assert res.sense.sense_id == 10

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
        assert res.sense.sense_id == 102

    def test_voted_by_en_signal_with_strong_cs(self):
        # English says "passion" → sense 102; cs_rank=20 ≤ threshold → strong
        term = self._two_sense_term()
        # CS matches dychtění → sense_102 (rank 20 = strong threshold)
        res = _resolve_multi(term, "dychtění", "of passion", cs_rank=20, en_rank=30)
        assert res.method == "krystal_multi_voted"
        assert res.sense.sense_id == 102

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
        assert res.sense.sense_id == 201  # primary (context_label=None)

    def test_deterministic_with_same_input(self):
        term = self._two_sense_term()
        r1 = _resolve_multi(term, "dychtění", None, cs_rank=20, en_rank=30)
        r2 = _resolve_multi(term, "dychtění", None, cs_rank=20, en_rank=30)
        assert r1.method == r2.method
        assert r1.sense.sense_id == r2.sense.sense_id


# ── _call_deepseek_batch ──────────────────────────────────────────────────────

# ── _strip_lemma_suffix ───────────────────────────────────────────────────────

class TestStripLemmaSuffix:
    def test_strips_trailing_number(self):
        assert _strip_lemma_suffix("dico2") == "dico"

    def test_no_suffix_unchanged(self):
        assert _strip_lemma_suffix("anima") == "anima"

    def test_only_trailing_digits_stripped(self):
        # interior digits are not touched (no such Latin lemmas, but be precise)
        assert _strip_lemma_suffix("homo1") == "homo"


class TestCanonicalLemma:
    def test_lowercases(self):
        assert _canonical_lemma("Actus") == "actus"

    def test_strips_suffix_then_lowercases(self):
        assert _canonical_lemma("Dico2") == "dico"

    def test_plain_lemma_unchanged(self):
        assert _canonical_lemma("virtus") == "virtus"


# ── _parse_batch_entry ────────────────────────────────────────────────────────

class TestParseBatchEntry:
    def test_full_object(self):
        # canonical field is ignored — lemma key is the CLTK output, not corrected
        entry = _parse_batch_entry("divina", {"canonical": "divinus", "category": "term", "slovak": "božský"})
        assert entry == {"category": "term", "slovak": "božský"}

    def test_legacy_string_value(self):
        entry = _parse_batch_entry("ratio", "rozum")
        assert entry == {"category": None, "slovak": "rozum"}

    def test_canonical_field_ignored(self):
        entry = _parse_batch_entry("corpus", {"canonical": "something_else", "category": "term", "slovak": "telo"})
        assert "canonical" not in entry
        assert entry["slovak"] == "telo"

    def test_invalid_category_becomes_none(self):
        entry = _parse_batch_entry("x", {"category": "bogus", "slovak": "y"})
        assert entry["category"] is None

    def test_empty_slovak_is_rejected(self):
        assert _parse_batch_entry("x", {"category": "term", "slovak": ""}) is None

    def test_non_dict_non_str_rejected(self):
        assert _parse_batch_entry("x", 123) is None


# ── _call_deepseek_batch ──────────────────────────────────────────────────────

class TestCallDeepseekBatch:
    def _fake_response(self, content: str):
        from unittest.mock import MagicMock
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
        return mock

    def test_parses_structured_response(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        batch = [
            {"lemma": "ratio", "best_latin": "ratio est", "best_czech": "", "best_english": "reason"},
            {"lemma": "anima", "best_latin": "anima vivit", "best_czech": "duše", "best_english": "soul"},
        ]
        content = (
            '{"ratio": {"category": "term", "slovak": "rozum"}, '
            '"anima": {"category": "term", "slovak": "duša"}}'
        )
        with patch("common.deepseek_client.requests.post") as mock_post:
            mock_post.return_value = self._fake_response(content)
            result = _call_deepseek_batch(batch)
        assert result["ratio"] == {"category": "term", "slovak": "rozum"}
        assert result["anima"]["slovak"] == "duša"

    def test_strips_markdown_fences(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        batch = [{"lemma": "corpus", "best_latin": "", "best_czech": "", "best_english": ""}]
        content = '```json\n{"corpus": {"category": "term", "slovak": "telo"}}\n```'
        with patch("common.deepseek_client.requests.post") as mock_post:
            mock_post.return_value = self._fake_response(content)
            result = _call_deepseek_batch(batch)
        assert result == {"corpus": {"category": "term", "slovak": "telo"}}

    def test_omits_malformed_entries(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        batch = [{"lemma": "a", "best_latin": "", "best_czech": "", "best_english": ""}]
        content = '{"a": {"category": "term", "slovak": "x"}, "b": {"slovak": ""}}'
        with patch("common.deepseek_client.requests.post") as mock_post:
            mock_post.return_value = self._fake_response(content)
            result = _call_deepseek_batch(batch)
        assert "a" in result and "b" not in result

    def test_returns_empty_on_api_error(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        batch = [{"lemma": "virtus", "best_latin": "", "best_czech": "", "best_english": ""}]
        with patch("common.deepseek_client.requests.post") as mock_post:
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
        content = '{"pax": {"category": "term", "slovak": "pokoj"}}'
        with patch("common.deepseek_client.requests.post") as mock_post:
            mock_post.return_value = self._fake_response(content)
            _call_deepseek_batch(batch)
        assert get_api_stats()["calls"] == before + 1


# ── _scan_gap_lemmas ──────────────────────────────────────────────────────────

class TestScanGapLemmas:
    def _seg(self, latin="", czech="", english="", seg_id=1):
        return Segment(segment_id=seg_id, locator_path="I.q1.a1.arg1",
                       element_type="arg", latin=latin, czech=czech, english=english)

    def test_respects_freq_floor(self):
        # 'virtus' appears in 2 segments, floor=3 → excluded
        segs = [
            self._seg("virtus bona est", seg_id=1),
            self._seg("virtus magna est", seg_id=2),
        ]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=3,
                                  freq_ceiling_pct=1.0)
        assert "virtus" not in result

    def test_includes_above_floor(self):
        segs = [self._seg(f"virtus in segmento {i}", seg_id=i) for i in range(5)]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=3,
                                  freq_ceiling_pct=1.0)
        assert "virtus" in result

    def test_excludes_krystal_lemmas(self):
        segs = [self._seg("essentia divina est", seg_id=i) for i in range(5)]
        result = _scan_gap_lemmas(segs, krystal_lemmas={"essentia"}, freq_floor=1,
                                  freq_ceiling_pct=1.0)
        assert "essentia" not in result

    def test_capital_variant_collapses_to_single_lowercase_key(self):
        # CLTK preserves token case ("Virtus" → ["Virtus"], "virtus" → ["virtus"]);
        # both must canonicalize to one lowercase gap term, not two — capital-variant
        # duplicates are pure noise for the reviewer.
        segs = [self._seg("Virtus est", seg_id=1), self._seg("virtus bona", seg_id=2)]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=1,
                                  freq_ceiling_pct=1.0)
        assert "Virtus" not in result
        assert "virtus" in result
        assert result["virtus"]["freq"] == 2  # both segments fold into one lemma

    def test_krystal_exclusion_is_case_insensitive(self):
        # A sentence-initial "Caritas" must resolve to the lowercase Krystal
        # "caritas", not leak as a capitalized gap proposal.
        segs = [self._seg("Caritas est", seg_id=i) for i in range(5)]
        result = _scan_gap_lemmas(segs, krystal_lemmas={"caritas"}, freq_floor=1,
                                  freq_ceiling_pct=1.0)
        assert "Caritas" not in result
        assert "caritas" not in result

    def test_skips_short_tokens(self):
        # len gate: with min_len=5, 'deus' (4 chars) is excluded
        segs = [self._seg("deus bonus", seg_id=i) for i in range(5)]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=1,
                                  min_len=5, freq_ceiling_pct=1.0)
        assert "deus" not in result

    def test_default_min_len_accepts_four_char_words(self):
        # Default min_len=3 → 'deus' (4 chars, not in Krystal) qualifies
        segs = [self._seg("deus bonus", seg_id=i) for i in range(5)]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=1,
                                  freq_ceiling_pct=1.0)
        assert "deus" in result

    def test_min_len_is_configurable(self):
        segs = [self._seg("deus bonus", seg_id=i) for i in range(5)]
        assert "deus" not in _scan_gap_lemmas(segs, set(), freq_floor=1,
                                              min_len=5, freq_ceiling_pct=1.0)
        assert "deus" in _scan_gap_lemmas(segs, set(), freq_floor=1,
                                          min_len=3, freq_ceiling_pct=1.0)

    def test_cltk_stopwords_excluded(self):
        # CLTK STOPS words are excluded; a non-stop word in the same text still passes.
        # 'virtus' (6 chars, not a stop word) acts as the positive control.
        segs = [self._seg("enim virtus magna", seg_id=i) for i in range(5)]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=1,
                                  freq_ceiling_pct=1.0)
        assert "enim" not in result
        assert "virtus" in result  # positive control: non-stop word still appears

    def test_ignored_lemmas_excluded(self):
        # DB-sourced stopwords are silenced; a non-ignored word in the same text still passes.
        segs = [self._seg("virtus magna", seg_id=i) for i in range(5)]
        # Confirm 'virtus' appears WITHOUT ignored_lemmas
        result_without = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=1,
                                          freq_ceiling_pct=1.0)
        assert "virtus" in result_without
        # Confirm 'virtus' is suppressed WITH ignored_lemmas={'virtus'}
        result_with = _scan_gap_lemmas(
            segs, krystal_lemmas=set(), freq_floor=1,
            ignored_lemmas=frozenset({"virtus"}),
            freq_ceiling_pct=1.0,
        )
        assert "virtus" not in result_with

    def test_freq_ceiling_excludes_ubiquitous_lemmas(self):
        # A lemma appearing in 90% of segments should be filtered by freq ceiling
        segs_with = [self._seg("virtus magna", seg_id=i) for i in range(90)]
        segs_without = [self._seg("aliquid aliud", seg_id=90 + i) for i in range(10)]
        result = _scan_gap_lemmas(
            segs_with + segs_without,
            krystal_lemmas=set(),
            freq_floor=1,
            freq_ceiling_pct=0.40,
        )
        assert "virtus" not in result

    def test_freq_ceiling_keeps_moderate_frequency(self):
        # A lemma at 20% frequency survives a 40% ceiling
        segs_with = [self._seg("virtus magna", seg_id=i) for i in range(20)]
        segs_without = [self._seg("aliquid aliud", seg_id=20 + i) for i in range(80)]
        result = _scan_gap_lemmas(
            segs_with + segs_without,
            krystal_lemmas=set(),
            freq_floor=1,
            freq_ceiling_pct=0.40,
        )
        assert "virtus" in result

    def test_no_pos_filter_keeps_verbs(self):
        # Non-CLTK-stop verbs pass through; model categorizes them later
        segs = [self._seg("dicunt homines verba", seg_id=i) for i in range(12)]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=10,
                                  freq_ceiling_pct=1.0)
        assert len(result) > 0

    def test_strips_numeric_suffix_in_key(self):
        # CLTK may lemmatize 'dicunt' → 'dico2'; the stored key is suffix-stripped
        segs = [self._seg("dicunt multa", seg_id=i) for i in range(12)]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=10,
                                  freq_ceiling_pct=1.0)
        for lemma in result:
            assert not lemma[-1].isdigit()

    def test_collects_context(self):
        segs = [self._seg("virtus magna", czech="ctnost", english="virtue", seg_id=i)
                for i in range(5)]
        result = _scan_gap_lemmas(segs, krystal_lemmas=set(), freq_floor=3,
                                  freq_ceiling_pct=1.0)
        assert "virtus" in result, "virtus must appear — check CLTK lemmatization"
        assert result["virtus"]["best_czech"] == "ctnost"
        assert result["virtus"]["best_english"] == "virtue"


# ── _propose_gap_terms ────────────────────────────────────────────────────────

class TestProposeGapTerms:
    def _entry(self, category="term", slovak="sk"):
        return {"category": category, "slovak": slovak}

    def test_returns_structured_result(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        gap_data = {
            "virtus": {"freq": 20, "best_latin": "virtus", "best_czech": "", "best_english": "virtue"},
            "anima": {"freq": 15, "best_latin": "anima", "best_czech": "duše", "best_english": "soul"},
        }
        with patch("ingest.gap_terms._call_deepseek_batch") as mock_batch:
            mock_batch.return_value = {
                "virtus": self._entry(slovak="cnosť"),
                "anima": self._entry(slovak="duša"),
            }
            result = _propose_gap_terms(gap_data, batch_size=10, max_workers=1)
        assert set(result["terms"]) == {"virtus", "anima"}
        assert result["terms"]["virtus"]["slovak"] == "cnosť"
        assert result["terms"]["anima"]["freq"] == 15
        assert result["dropped"] == []

    def test_each_cltk_lemma_is_its_own_key(self, monkeypatch):
        # divina, divino, divinus are separate entries — no merging
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        gap_data = {
            "divina":  {"freq": 10, "best_latin": "la", "best_czech": "", "best_english": ""},
            "divino":  {"freq": 8,  "best_latin": "la", "best_czech": "", "best_english": ""},
            "divinus": {"freq": 5,  "best_latin": "la", "best_czech": "", "best_english": ""},
        }
        def fake_batch(batch):
            return {item["lemma"]: self._entry(slovak="božský") for item in batch}
        with patch("ingest.gap_terms._call_deepseek_batch", side_effect=fake_batch):
            result = _propose_gap_terms(gap_data, batch_size=10, max_workers=1)
        assert set(result["terms"]) == {"divina", "divino", "divinus"}
        assert result["terms"]["divina"]["freq"] == 10
        assert result["terms"]["divino"]["freq"] == 8
        assert result["terms"]["divinus"]["freq"] == 5
        assert all(t["slovak"] == "božský" for t in result["terms"].values())

    def test_missing_lemmas_are_dropped_not_stubbed(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        gap_data = {"ratio": {"freq": 10, "best_latin": "", "best_czech": "", "best_english": ""}}
        with patch("ingest.gap_terms._call_deepseek_batch") as mock_batch:
            mock_batch.return_value = {}  # batch returns nothing
            result = _propose_gap_terms(gap_data, batch_size=10, max_workers=1)
        assert result["terms"] == {}
        assert result["dropped"] == ["ratio"]

    def test_batches_correctly(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        gap_data = {
            f"lemma{i}": {"freq": 10, "best_latin": "", "best_czech": "", "best_english": ""}
            for i in range(10)
        }
        calls = []
        def fake_batch(batch):
            calls.append(len(batch))
            return {item["lemma"]: self._entry(slovak=f"sk_{item['lemma']}") for item in batch}

        with patch("ingest.gap_terms._call_deepseek_batch", side_effect=fake_batch):
            result = _propose_gap_terms(gap_data, batch_size=3, max_workers=1)

        assert len(result["terms"]) == 10
        # 10 lemmas at batch_size=3 → 4 batches (3+3+3+1)
        assert len(calls) == 4

    def test_gap_terms_db_empty_without_conn(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        gap_data = {"virtus": {"freq": 10, "best_latin": "", "best_czech": "", "best_english": ""}}
        with patch("ingest.gap_terms._call_deepseek_batch") as mock_batch:
            mock_batch.return_value = {"virtus": self._entry(slovak="cnosť")}
            result = _propose_gap_terms(gap_data, batch_size=10, max_workers=1)
        # Without conn no DB writes happen — gap_terms_db is empty
        assert result["gap_terms_db"] == {}


# ── _load_existing_gap_terms ─────────────────────────────────────────────────


class TestLoadExistingGapTerms:
    def test_returns_empty_when_no_rows(self, fake_conn):
        conn = fake_conn(fetchall_rows=[])
        assert _load_existing_gap_terms(conn) == {}

    def test_returns_indexed_by_lemma(self, fake_conn):
        rows = [
            ("virtus", 1, 10, 2, "term", "cnosť"),
            ("anima",  2, 20, 1, "name", "duša"),
        ]
        conn = fake_conn(fetchall_rows=rows)
        result = _load_existing_gap_terms(conn)
        assert set(result) == {"virtus", "anima"}
        assert result["virtus"] == {
            "term_id": 1, "sense_id": 10, "version": 2, "category": "term", "slovak": "cnosť"
        }
        assert result["anima"]["slovak"] == "duša"


# ── _write_gap_proposals (Krystal collision guard) ───────────────────────────


class TestWriteGapProposalsCollision:
    """Verify the Krystal-collision guard: a lemma that already has an approved sense
    must be skipped (it resolves via the Krystal path) rather than getting a duplicate
    proposed sense that would violate the glossary_sense_single_unique constraint."""

    def _make_conn(self, existing_status=None):
        """Fake psycopg2 connection.

        existing_status: None → no existing sense; 'approved' → Krystal term; 'proposed' → gap term.

        fetchone() call sequence inside _write_gap_proposals (per lemma):
          1. _ensure_glossary_term INSERT RETURNING term_id → (1,)
          2. SELECT sense_id, status (collision check) → None / (10, status)
          3. INSERT sense RETURNING sense_id → (10,)  [only when existing is None]
          4. SELECT version → (1,)
        """
        from unittest.mock import MagicMock

        if existing_status == "approved":
            # Term exists with approved sense → collision guard fires after call #2
            responses = iter([(1,), (10, "approved")])
        elif existing_status == "proposed":
            # Existing proposed sense reused → no INSERT sense, but still SELECT version
            responses = iter([(1,), (10, "proposed"), (1,)])
        else:
            # New term: INSERT sense → (10,), then SELECT version → (1,)
            responses = iter([(1,), None, (10,), (1,)])

        cur = MagicMock()
        cur.fetchone.side_effect = lambda: next(responses, None)
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)

        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def test_skips_krystal_term_does_not_add_to_gap_terms_db(self):
        from ingest.gap_terms import _write_gap_proposals
        conn, _ = self._make_conn(existing_status="approved")
        proposals = {"terms": {"ratio": {"slovak": "rozum", "category": "term",
                                          "freq": 10, "best_latin": "", "best_czech": "", "best_english": ""}}}
        result = _write_gap_proposals(conn, proposals, src_model=99)
        assert "ratio" not in result  # Krystal term → skipped, not in gap_terms_db

    def test_gap_term_with_existing_proposed_sense_reuses_sense_id(self):
        from ingest.gap_terms import _write_gap_proposals
        conn, _ = self._make_conn(existing_status="proposed")
        proposals = {"terms": {"virtus": {"slovak": "cnosť", "category": "term",
                                           "freq": 20, "best_latin": "", "best_czech": "", "best_english": ""}}}
        result = _write_gap_proposals(conn, proposals, src_model=99)
        assert "virtus" in result
        assert result["virtus"]["sense_id"] == 10  # reused existing proposed sense

    def test_new_gap_term_creates_sense(self):
        from ingest.gap_terms import _write_gap_proposals
        conn, _ = self._make_conn(existing_status=None)
        proposals = {"terms": {"disciplina": {"slovak": "disciplína", "category": "term",
                                               "freq": 5, "best_latin": "", "best_czech": "", "best_english": ""}}}
        result = _write_gap_proposals(conn, proposals, src_model=99)
        assert "disciplina" in result
        assert result["disciplina"]["sense_id"] == 10


# ── pilot_batch_sizes ─────────────────────────────────────────────────────────

class TestPilotBatchSizes:
    def _gap_data(self, n: int = 20) -> dict:
        return {
            f"lemma{i:02d}": {
                "freq": n - i,
                "best_latin": f"latin context {i}",
                "best_czech": "",
                "best_english": "",
            }
            for i in range(n)
        }

    def _entry(self, lemma, category="term", slovak=None):
        return {"category": category, "slovak": slovak or f"sk_{lemma}"}

    def _batch(self, batch):
        return {item["lemma"]: self._entry(item["lemma"]) for item in batch}

    def test_returns_one_result_per_batch_size(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        with patch("ingest.gap_terms._call_deepseek_batch", side_effect=self._batch):
            results = pilot_batch_sizes(self._gap_data(), top_n=10, batch_sizes=[5, 10])
        assert len(results) == 2
        assert results[0]["batch_size"] == 5
        assert results[1]["batch_size"] == 10

    def test_selects_top_n_by_frequency(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        gap_data = self._gap_data(20)  # lemma00 has freq=20, lemma19 has freq=1
        seen: list[str] = []

        def fake_batch(batch):
            seen.extend(item["lemma"] for item in batch)
            return self._batch(batch)

        with patch("ingest.gap_terms._call_deepseek_batch", side_effect=fake_batch):
            pilot_batch_sizes(gap_data, top_n=5, batch_sizes=[10])

        unique = set(seen)
        assert len(unique) == 5
        assert "lemma00" in unique  # highest freq
        assert "lemma19" not in unique  # lowest freq, excluded

    def test_result_contains_required_keys(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        with patch("ingest.gap_terms._call_deepseek_batch", return_value={}):
            results = pilot_batch_sizes(self._gap_data(5), top_n=5, batch_sizes=[5])
        r = results[0]
        for key in ("batch_size", "calls", "input_tokens", "output_tokens", "cost_usd",
                    "cost_per_lemma", "category_counts", "samples"):
            assert key in r, f"missing key: {key}"
        assert "merges" not in r  # canonical merging removed

    def test_tracks_delta_input_tokens(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        content = (
            '{"lemma00": {"category": "term", "slovak": "sl"}, '
            '"lemma01": {"category": "term", "slovak": "sl"}}'
        )
        fake_resp = {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 10},
        }
        with patch("common.deepseek_client.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = fake_resp
            results = pilot_batch_sizes(self._gap_data(2), top_n=2, batch_sizes=[10])
        r = results[0]
        assert r["input_tokens"] == 200
        assert r["output_tokens"] == 10
        assert r["cost_usd"] >= 0

    def test_smaller_batch_size_makes_more_api_calls(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        # Mocking at _call_deepseek_batch bypasses the stat increment (calls stays 0),
        # so we verify batching via the mock invocation count itself.
        batch_calls: list[int] = []

        def fake_batch(batch):
            batch_calls.append(len(batch))
            return self._batch(batch)

        gap_data = self._gap_data(10)
        with patch("ingest.gap_terms._call_deepseek_batch", side_effect=fake_batch):
            pilot_batch_sizes(gap_data, top_n=10, batch_sizes=[5, 10])

        # Total calls = 2 (bs=5, 10 lemmas → 2 batches) + 1 (bs=10 → 1 batch)
        assert len(batch_calls) == 3

    def test_category_counts_populated(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        gap_data = self._gap_data(4)
        cats = {"lemma00": "term", "lemma01": "name", "lemma02": "formula", "lemma03": "prose"}
        def fake_batch(batch):
            return {item["lemma"]: self._entry(item["lemma"], category=cats[item["lemma"]])
                    for item in batch}
        with patch("ingest.gap_terms._call_deepseek_batch", side_effect=fake_batch):
            results = pilot_batch_sizes(gap_data, top_n=4, batch_sizes=[10])
        assert results[0]["category_counts"] == {"term": 1, "name": 1, "formula": 1, "prose": 1}

    def test_each_lemma_appears_as_own_term(self, monkeypatch):
        # No canonical merging — each CLTK lemma is its own entry in results
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        gap_data = self._gap_data(3)  # lemma00, lemma01, lemma02
        with patch("ingest.gap_terms._call_deepseek_batch", side_effect=self._batch):
            results = pilot_batch_sizes(gap_data, top_n=3, batch_sizes=[10])
        samples = results[0]["samples"]
        sample_lemmas = {s["lemma"] for s in samples}
        assert sample_lemmas == {"lemma00", "lemma01", "lemma02"}

    def test_empty_gap_data_returns_zero_cost(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        with patch("ingest.gap_terms._call_deepseek_batch", return_value={}):
            results = pilot_batch_sizes({}, top_n=10, batch_sizes=[25])
        assert results[0]["cost_usd"] == 0.0
        assert results[0]["calls"] == 0

    def test_samples_in_frequency_order(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        gap_data = self._gap_data(10)
        with patch("ingest.gap_terms._call_deepseek_batch", side_effect=self._batch):
            results = pilot_batch_sizes(gap_data, top_n=10, batch_sizes=[10], sample_n=3)
        samples = results[0]["samples"]
        assert len(samples) == 3
        # First sample should be highest-freq lemma (lemma00, freq=10)
        assert samples[0]["lemma"] == "lemma00"
        assert samples[0]["freq"] == 10


# ── resolve_segment — gap no-stub invariant ───────────────────────────────────

class TestResolveSegmentGap:
    """The no-stub invariant: a gap lemma becomes a term_usage Resolution only if
    it is present in gap_terms_db. Uses invented alphabetic tokens that the
    lemmatizer passes through unchanged so gap_terms_db keys are deterministic."""

    def _seg(self, latin, czech=None, english=None):
        return Segment(segment_id=1, locator_path="I.q1.a1.arg1", element_type="arg",
                       latin=latin, czech=czech, english=english)

    _GAP_METHODS = {"bahounek_derived", "english_derived", "model_proposed"}

    def _gap_db(self, *lemmas, sense_id=7):
        return {
            lm: {"term_id": 1, "sense_id": sense_id, "version": 1,
                 "category": "term", "slovak": "navrh"}
            for lm in lemmas
        }

    def test_qualified_gap_lemma_resolves(self):
        seg = self._seg("xyzzyword", czech="nieco")
        res = resolve_segment(
            seg, [], {}, cs_rank=20, en_rank=30,
            gap_terms_db=self._gap_db("xyzzyword"),
        )
        gap = [r for r in res if r.method in self._GAP_METHODS]
        assert len(gap) == 1
        assert gap[0].sense.sense_id == 7
        assert gap[0].method == "bahounek_derived"  # czech present
        assert gap[0].confidence == "needs_review"

    def test_unproposed_gap_lemma_produces_no_row_and_no_stub(self):
        seg = self._seg("unmappedword", czech="nieco")
        res = resolve_segment(seg, [], {}, cs_rank=20, en_rank=30, gap_terms_db={})
        assert res == []
        for r in res:
            assert not str(r.sense.sk_content or "").startswith("[")

    def test_method_reflects_available_context(self):
        gdb = self._gap_db("xyzzyword")
        res = resolve_segment(self._seg("xyzzyword", english="thing"), [], {}, 20, 30, gdb)
        assert [r for r in res if r.method in self._GAP_METHODS][0].method == "english_derived"
        res = resolve_segment(self._seg("xyzzyword"), [], {}, 20, 30, gdb)
        assert [r for r in res if r.method in self._GAP_METHODS][0].method == "model_proposed"

    def test_each_cltk_lemma_resolves_independently(self):
        # alphaword and betaword are distinct keys — each gets its own resolution row
        seg = self._seg("alphaword betaword", english="thing")
        gdb = self._gap_db("alphaword", "betaword", sense_id=9)
        res = resolve_segment(seg, [], {}, 20, 30, gdb)
        gap = [r for r in res if r.method in self._GAP_METHODS]
        assert len(gap) == 2
        assert all(r.sense.sense_id == 9 for r in gap)


# ── resolve_segment — perfect-passive habere suppression ──────────────────────

class TestResolveSegmentHabereSuppression:
    """CLTK lemmatizes the PPP 'habitum' (in 'habitum est') to the noun *habitus*.
    The resolver must not write that bogus habitus term_usage row when the
    construction is the segment's only habitus evidence. Uses the real CLTK
    lemmatizer + POS tagger (model-gated, skipped when models are absent)."""

    def _seg(self, latin):
        return Segment(segment_id=1, locator_path="I.q1.a1.arg1", element_type="arg",
                       latin=latin, czech=None, english=None)

    def _habitus_lookup(self):
        return {"habitus": _term(54, "habitus", [_sense(60)])}

    def test_ppp_only_is_suppressed(self):
        # 'habitum est' = "as has been said" — no genuine habitus → no resolution.
        res = resolve_segment(self._seg("Sicut habitum est supra de potentia."),
                              [], self._habitus_lookup(), 20, 30, {})
        assert [r for r in res if r.term.latin_lemma == "habitus"] == []

    def test_accusative_noun_still_resolves(self):
        # 'habitum bonum' (accusative noun, no esse) is genuine habitus → resolved.
        res = resolve_segment(self._seg("Homo habitum bonum habet."),
                              [], self._habitus_lookup(), 20, 30, {})
        assert [r for r in res if r.term.latin_lemma == "habitus"]

    def test_genuine_evidence_elsewhere_keeps_constraint(self):
        # PPP present, but nominative 'habitus' elsewhere → not suppressed.
        res = resolve_segment(self._seg("Habitus est qualitas, ut habitum est supra."),
                              [], self._habitus_lookup(), 20, 30, {})
        assert [r for r in res if r.term.latin_lemma == "habitus"]

    def test_suppressed_tokens_helper(self):
        assert _suppressed_habitus_tokens("Sicut habitum est supra.") == {"habitum"}
        assert _suppressed_habitus_tokens("Homo habitum bonum habet.") == set()
        assert _suppressed_habitus_tokens("Nulla mentio hic.") == set()
