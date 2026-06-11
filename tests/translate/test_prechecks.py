"""Unit tests for src/translate/prechecks.py.

All tests are DB-free.
"""

from __future__ import annotations

from src.translate.prechecks import (
    check_terminology,
    check_terminology_lemma,
)

# ── check_terminology tests ───────────────────────────────────────────────────

def test_terminology_ok_when_all_present():
    constraints = [
        {"latin_lemma": "forma", "required_slovak": "forma"},
        {"latin_lemma": "materia", "required_slovak": "matéria"},
    ]
    draft = "Každá vec má svoju forma a matéria, ktorá ju tvorí."
    result = check_terminology(draft, constraints)
    assert result.ok is True
    assert result.failures == []


def test_terminology_fail_when_term_missing():
    constraints = [
        {"latin_lemma": "forma", "required_slovak": "forma"},
        {"latin_lemma": "actus", "required_slovak": "akt"},
    ]
    draft = "Táto veta obsahuje forma, ale nie druhý termín."
    result = check_terminology(draft, constraints)
    assert result.ok is False
    assert len(result.failures) == 1
    assert "actus" in result.failures[0] or "akt" in result.failures[0]


def test_terminology_case_insensitive():
    constraints = [{"latin_lemma": "forma", "required_slovak": "Forma"}]
    draft = "každá vec má svoju forma a matériu."
    result = check_terminology(draft, constraints)
    assert result.ok is True


def test_terminology_diacritics_normalised():
    constraints = [{"latin_lemma": "gratia", "required_slovak": "milosť"}]
    draft = "Boh dáva milosť všetkým, ktorí o ňu prosí."
    result = check_terminology(draft, constraints)
    assert result.ok is True


def test_terminology_diacritics_cross_form():
    constraints = [{"latin_lemma": "gratia", "required_slovak": "milosť"}]
    draft = "Boh dáva milost (stripped form) všetkým."
    result = check_terminology(draft, constraints)
    assert result.ok is True


def test_terminology_empty_constraints():
    result = check_terminology("Ľubovoľný text.", [])
    assert result.ok is True
    assert result.failures == []


def test_terminology_multiple_missing_all_reported():
    constraints = [
        {"latin_lemma": "forma", "required_slovak": "forma"},
        {"latin_lemma": "actus", "required_slovak": "akt"},
        {"latin_lemma": "potentia", "required_slovak": "potencia"},
    ]
    draft = "Tento text neobsahuje žiadny z požadovaných termínov."
    result = check_terminology(draft, constraints)
    assert result.ok is False
    assert len(result.failures) == 3


# ── check_terminology_lemma tests ─────────────────────────────────────────────


def _mock_generate(lemma_to_forms: dict):
    def _fn(lemma: str) -> frozenset[str]:
        return frozenset(lemma_to_forms.get(lemma.lower(), set()))
    return _fn


def test_terminology_lemma_empty_constraints():
    result = check_terminology_lemma("Ľubovoľný text.", [])
    assert result.ok is True
    assert result.failures == []


def test_terminology_lemma_ok_when_inflected_form_matches(monkeypatch):
    form_map = {"viera": {"viera", "viery", "viere", "vieru", "vierou"}}
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate(form_map))

    constraints = [{"latin_lemma": "fides", "required_slovak": "viera"}]
    draft = "Je silná vierou."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True
    assert result.failures == []


def test_terminology_lemma_fail_when_no_form_present(monkeypatch):
    form_map = {"viera": {"viera", "viery", "vierou"}}
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate(form_map))

    constraints = [{"latin_lemma": "fides", "required_slovak": "viera"}]
    draft = "Tento text neobsahuje požadovaný termín."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is False
    assert len(result.failures) == 1
    assert "viera" in result.failures[0]
    assert "fides" in result.failures[0]


def test_terminology_lemma_case_insensitive(monkeypatch):
    form_map = {"boh": {"boh", "boha", "bohu"}}
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate(form_map))

    constraints = [{"latin_lemma": "deus", "required_slovak": "Boh"}]
    draft = "Boh je veľký."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_multiple_constraints_all_reported(monkeypatch):
    form_map = {"viera": {"viera", "vierou"}, "rozum": {"rozum", "rozumu"}}
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate(form_map))

    constraints = [
        {"latin_lemma": "fides", "required_slovak": "viera"},
        {"latin_lemma": "ratio", "required_slovak": "rozum"},
    ]
    draft = "Toto sú iné slová."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is False
    assert len(result.failures) == 2


def test_terminology_lemma_no_substring_false_positive(monkeypatch):
    form_map = {"forma": {"forma", "formy", "forme", "formou"}}
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate(form_map))

    constraints = [{"latin_lemma": "forma", "required_slovak": "forma"}]
    draft = "Text obsahuje informácia."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is False


# ── check_terminology_lemma — multi-word term (per-word form sets) ────────────

def test_terminology_lemma_multiword_term_all_components_ok(monkeypatch):
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "per se", "required_slovak": "o sebe", "category": None}]
    draft = "Boh existuje o sebe."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True
    assert result.failures == []


def test_terminology_lemma_multiword_term_inflected_components(monkeypatch):
    form_map = {
        "intencionálny": {"intencionálny", "intencionálneho", "intencionálnemu"},
        "obraz": {"obraz", "obrazu", "obrazom"},
    }
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate(form_map))

    constraints = [{"latin_lemma": "species", "required_slovak": "intencionálny obraz", "category": "term"}]
    draft = "Nie je tu intencionálneho obrazu."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True
    assert result.failures == []


def test_terminology_lemma_multiword_term_partial_missing(monkeypatch):
    form_map = {
        "intencionálny": {"intencionálny", "intencionálneho"},
        "obraz": {"obraz", "obrazu"},
    }
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate(form_map))

    constraints = [{"latin_lemma": "species", "required_slovak": "intencionálny obraz", "category": "term"}]
    draft = "Je intencionálny."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is False
    assert "obraz" in result.failures[0]


# ── check_terminology_lemma — OOV fallback ────────────────────────────────────

def test_terminology_lemma_oov_verbatim_match(monkeypatch):
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "hybris", "required_slovak": "hybris", "category": "term"}]
    draft = "Text obsahuje hybris ako termín."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_oov_stem_prefix_match(monkeypatch):
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "virtus", "required_slovak": "čnosť", "category": "term"}]
    draft = "Smeruje k rozličným čnostiam."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_oov_latin_loan_declined(monkeypatch):
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "habitus", "required_slovak": "habitus", "category": "term"}]
    draft = "Disponuje habitom čnosti."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_oov_absent_fails(monkeypatch):
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "habitus", "required_slovak": "habitus", "category": "term"}]
    draft = "Tento text hovorí o inom pojme."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is False
    assert "habitus" in result.failures[0]


# ── check_terminology_lemma — formula category (regex) ───────────────────────

def test_terminology_lemma_formula_match(monkeypatch):
    def _boom(_):
        raise AssertionError("generate_slovak_forms must not be called for formula constraints")

    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _boom)

    constraints = [{"latin_lemma": "sed_contra", "required_slovak": "Avšak proti", "category": "formula"}]
    draft = "Avšak proti je to, čo hovorí Augustín."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_formula_word_boundary_no_false_positive(monkeypatch):
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "per se", "required_slovak": "o sebe", "category": "formula"}]
    draft = "Veci nasledujú po sebe v čase."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is False


def test_terminology_lemma_formula_fail_when_absent(monkeypatch):
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "respondeo", "required_slovak": "Odpovedám", "category": "formula"}]
    draft = "Táto veta neobsahuje formulku."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is False
    assert "Odpovedám" in result.failures[0]


def test_terminology_lemma_formula_diacritics_normalised(monkeypatch):
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "sed_contra", "required_slovak": "Avšak proti", "category": "formula"}]
    draft = "Avsak proti je to, co hovori Augustín."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_formula_exact_punctuation(monkeypatch):
    """Formula with colon and comma: 'Odpovedám: treba povedať, že' matches verbatim in draft."""
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [
        {"latin_lemma": "respondeo", "required_slovak": "Odpovedám: treba povedať, že", "category": "formula"}
    ]
    draft = "Odpovedám: treba povedať, že Boh je jeden a jednoduchý."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_formula_draft_ending_in_period(monkeypatch):
    """Formula at end of draft followed by period still matches (period is not part of formula)."""
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [
        {"latin_lemma": "ad_nonum_dicendum", "required_slovak": "k deviatej sa postupuje takto", "category": "formula"}
    ]
    draft = "k deviatej sa postupuje takto."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_formula_required_ends_with_period(monkeypatch):
    """Formula whose required_slovak ends with a period still matches in running text.

    Regression: the trailing period in 'Pri tretej sa postupuje takto.' caused
    re.escape to produce '...takto\\.', then the trailing \\b required the next
    char to be \\w — a sentence-ending period is always followed by a space, so
    the regex never fired. Fix: strip trailing punctuation from req_norm first.
    """
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [
        {
            "latin_lemma": "ad_tertium_sic_proceditur",
            "required_slovak": "Pri tretej sa postupuje takto.",
            "category": "formula",
        }
    ]
    draft = "Pri tretej sa postupuje takto. Zdá sa, že Boh je teleso."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_formula_near_miss_word_boundary(monkeypatch):
    """Near-miss: 'k deviatej' alone does not satisfy 'k deviatej sa postupuje takto'."""
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [
        {"latin_lemma": "ad_nonum_dicendum", "required_slovak": "k deviatej sa postupuje takto", "category": "formula"}
    ]
    draft = "Hovorí sa k deviatej otázke."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is False
