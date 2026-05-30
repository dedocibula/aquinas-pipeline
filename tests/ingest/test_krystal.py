"""
Tests for src/ingest/krystal.py — parsing logic only. No DB, no DOCX file.
"""

from __future__ import annotations

from ingest.krystal import (
    _clean_latin,
    _extract_term_and_label,
    _parse_senses,
    _split_latin_czech,
    _split_outside_parens,
)

# ── _split_latin_czech ────────────────────────────────────────────────────────

class TestSplitLatinCzech:
    def test_em_dash(self):
        result = _split_latin_czech("essentia – esence")
        assert result == ("essentia", "esence")

    def test_hyphen_minus(self):
        result = _split_latin_czech("dolositas - lstivost")
        assert result == ("dolositas", "lstivost")

    def test_no_separator_returns_none(self):
        assert _split_latin_czech("SLOVNÍČEK TERMÍNŮ") is None

    def test_multiword_latin(self):
        result = _split_latin_czech("per se – o sobě (případně: jako takový)")
        assert result is not None
        assert result[0] == "per se"

    def test_czech_with_parens(self):
        result = _split_latin_czech("concupiscentia – žádostivost (důsledek dědičného hříchu), dychtění (vášeň)")
        assert result is not None
        assert result[0] == "concupiscentia"
        assert "žádostivost" in result[1]

    def test_strips_whitespace(self):
        result = _split_latin_czech("  anima  –  duše  ")
        assert result == ("anima", "duše")


# ── _clean_latin ──────────────────────────────────────────────────────────────

class TestCleanLatin:
    def test_simple(self):
        assert _clean_latin("essentia") == ("essentia", False)

    def test_multiword(self):
        lemma, is_multi = _clean_latin("per se")
        assert lemma == "per se"
        assert is_multi is True

    def test_strips_parenthetical(self):
        lemma, is_multi = _clean_latin("species (impressa, intelligibilis)")
        assert lemma == "species"
        assert is_multi is False

    def test_multiword_with_paren(self):
        lemma, is_multi = _clean_latin("fomes (peccati)")
        assert lemma == "fomes"
        assert is_multi is False

    def test_multiword_two_words(self):
        lemma, is_multi = _clean_latin("actus essendi")
        assert lemma == "actus essendi"
        assert is_multi is True


# ── _extract_term_and_label ───────────────────────────────────────────────────

class TestExtractTermAndLabel:
    def test_term_with_label(self):
        assert _extract_term_and_label("dychtění (vášeň)") == ("dychtění", "vášeň")

    def test_term_without_label(self):
        assert _extract_term_and_label("esence") == ("esence", None)

    def test_strips_whitespace(self):
        term, label = _extract_term_and_label("  milost  (  ctnost  )  ")
        assert term == "milost"
        assert label == "ctnost"

    def test_multi_word_label(self):
        term, label = _extract_term_and_label("prozřetelnost (u Boha)")
        assert term == "prozřetelnost"
        assert label == "u Boha"


# ── _split_outside_parens ─────────────────────────────────────────────────────

class TestSplitOutsideParens:
    def test_simple_split(self):
        assert _split_outside_parens("a, b, c", ",") == ["a", " b", " c"]

    def test_no_split_inside_parens(self):
        result = _split_outside_parens("žádostivost (dědičný hřích), dychtění (vášeň)", ",")
        assert len(result) == 2
        assert result[0] == "žádostivost (dědičný hřích)"

    def test_nested_parens(self):
        result = _split_outside_parens("a (b, c), d", ",")
        assert len(result) == 2

    def test_no_delimiter(self):
        result = _split_outside_parens("jednoduchý text", ",")
        assert result == ["jednoduchý text"]


# ── _parse_senses ─────────────────────────────────────────────────────────────

class TestParseSenses:
    # --- single-sense cases ---

    def test_simple_single(self):
        senses = _parse_senses("esence")
        assert len(senses) == 1
        assert senses[0].context_label is None
        assert senses[0].cs_rendering == "esence"

    def test_single_with_clarification_paren(self):
        # Parenthetical without colon or multi-word comma → single-sense
        senses = _parse_senses("vášeň (emoce)")
        assert len(senses) == 1
        assert senses[0].context_label is None

    def test_single_with_also_marker(self):
        # "někdy i" → "also" marker → single-sense
        senses = _parse_senses("ctnost (někdy i síla)")
        assert len(senses) == 1

    def test_single_nekdy_tez(self):
        # "někdy też: vztek" is an "also" marker — single-sense
        senses = _parse_senses("hněv (někdy też: vztek)")
        assert len(senses) == 1, f"Expected 1 sense, got {[(s.context_label, s.cs_rendering) for s in senses]}"

    def test_single_synonyms_no_labels(self):
        # Comma-separated synonyms with NO context labels → single-sense
        senses = _parse_senses("nevědomost, neznalost")
        assert len(senses) == 1

    def test_single_ale_i(self):
        senses = _parse_senses("rozum (ale i: důvod, argument)")
        assert len(senses) == 1

    def test_single_popradne(self):
        senses = _parse_senses("přirozenost (popř. příroda, povaha)")
        assert len(senses) == 1

    # --- multi-sense Pattern 1: comma-separated ---

    def test_pattern1_two_senses(self):
        senses = _parse_senses("žádostivost (důsledek dědičného hříchu), dychtění (vášeň)")
        assert len(senses) == 2
        assert senses[0].cs_rendering == "žádostivost"
        assert senses[0].context_label == "důsledek dědičného hříchu"
        assert senses[1].cs_rendering == "dychtění"
        assert senses[1].context_label == "vášeň"

    def test_pattern1_providentia(self):
        senses = _parse_senses("prozřetelnost (u Boha), předvídavost (u lidí)")
        assert len(senses) == 2
        cs = {s.cs_rendering for s in senses}
        assert "prozřetelnost" in cs
        assert "předvídavost" in cs

    # --- multi-sense Pattern 2: colon in parenthetical ---

    def test_pattern2_fides(self):
        senses = _parse_senses("víra (jako část spravedlnosti: věrnost)")
        assert len(senses) == 2
        assert senses[0].context_label is None
        assert senses[0].cs_rendering == "víra"
        assert senses[1].context_label == "jako část spravedlnosti"
        assert senses[1].cs_rendering == "věrnost"

    def test_pattern2_gratia(self):
        senses = _parse_senses("milost (v případě ctnosti též: vděčnost)")
        assert len(senses) == 2
        cs = {s.cs_rendering for s in senses}
        assert "milost" in cs
        assert "vděčnost" in cs

    def test_pattern2_context_label_stripped_of_trailing_also(self):
        # "v případě ctnosti też" → context label should be "v případě ctnosti"
        senses = _parse_senses("milost (v případě ctnosti též: vděčnost)")
        multi_sense = [s for s in senses if s.context_label is not None]
        assert multi_sense
        assert multi_sense[0].context_label == "v případě ctnosti"

    def test_pattern2_intellectus(self):
        senses = _parse_senses("intelekt (v případě rozumové ctnosti: intelektový vhled)")
        assert len(senses) == 2
        assert senses[0].cs_rendering == "intelekt"
        assert "intelektový vhled" in senses[1].cs_rendering

    def test_pattern2_context_label_extracted(self):
        senses = _parse_senses("statečnost (u Daru Ducha svatého: síla)")
        assert len(senses) == 2
        assert senses[1].context_label == "u Daru Ducha svatého"
        assert senses[1].cs_rendering == "síla"

    # --- edge cases ---

    def test_empty_string(self):
        senses = _parse_senses("")
        assert len(senses) == 1

    def test_multiword_rendering(self):
        senses = _parse_senses("intencionální obraz (vtištěný, inteligibilní)")
        assert len(senses) == 1  # parenthetical is type-annotation, not senses
        assert senses[0].cs_rendering == "intencionální obraz"

    def test_single_returns_none_context(self):
        for text in ["forma", "esence", "habitus", "syneresis"]:
            senses = _parse_senses(text)
            assert len(senses) == 1
            assert senses[0].context_label is None
