"""Unit tests for src/translate/prechecks.py.

All tests are DB-free.  A FakeConn/FakeCursor pair injects controlled formula
rows so the module-level cache can be exercised without a real PostgreSQL connection.
"""

from __future__ import annotations

import pytest

from src.translate.prechecks import (
    _clear_formula_cache,
    check_structure,
    check_terminology,
    check_terminology_lemma,
)

# ── Fake DB helpers ────────────────────────────────────────────────────────────

class FakeCursor:
    """Minimal cursor that returns preconfigured rows and supports context manager use."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def execute(self, sql, params=None):
        pass  # no-op

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeConn:
    """Minimal connection that yields a FakeCursor from .cursor()."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def cursor(self, cursor_factory=None):
        return FakeCursor(self._rows)


# Standard formula rows returned by the DB for most tests.
_STANDARD_FORMULA_ROWS = [
    {"latin_lemma": "sed_contra", "slovak_form": "Avšak proti tomu"},
    {"latin_lemma": "respondeo", "slovak_form": "Odpovedám"},
]


def _make_seg(segment_id: int = 1, locator_path: str = "I.q1.a1.sed_contra", element_type: str = "sed_contra") -> dict:
    return {
        "segment_id": segment_id,
        "locator_path": locator_path,
        "element_type": element_type,
    }


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_cache():
    """Ensure a clean formula cache for every test."""
    _clear_formula_cache()
    yield
    _clear_formula_cache()


# ── Structure tests ───────────────────────────────────────────────────────────

def test_sed_contra_ok_when_formula_present():
    """check_structure returns ok=True when sed_contra draft contains the expected formula."""
    conn = FakeConn(_STANDARD_FORMULA_ROWS)
    seg = _make_seg(element_type="sed_contra")
    draft = "Avšak proti tomu je písmo sväté, ktoré hovorí..."
    result = check_structure(seg, draft, conn)
    assert result.ok is True
    assert result.failures == []


def test_sed_contra_fail_when_formula_missing():
    """check_structure returns ok=False when sed_contra draft is missing the formula."""
    conn = FakeConn(_STANDARD_FORMULA_ROWS)
    seg = _make_seg(element_type="sed_contra")
    draft = "Toto je obyčajná veta bez formulky."
    result = check_structure(seg, draft, conn)
    assert result.ok is False
    assert len(result.failures) == 1
    assert "sed_contra" in result.failures[0] or "Avšak proti tomu" in result.failures[0]


def test_respondeo_ok_when_formula_present():
    """check_structure returns ok=True when respondeo draft contains the formula."""
    conn = FakeConn(_STANDARD_FORMULA_ROWS)
    seg = _make_seg(element_type="respondeo", locator_path="I.q1.a1.respondeo")
    draft = "Odpovedám: treba povedať, že..."
    result = check_structure(seg, draft, conn)
    assert result.ok is True
    assert result.failures == []


def test_respondeo_fail_when_formula_missing():
    """check_structure returns ok=False when respondeo draft is missing the formula."""
    conn = FakeConn(_STANDARD_FORMULA_ROWS)
    seg = _make_seg(element_type="respondeo", locator_path="I.q1.a1.respondeo")
    draft = "Táto veta neobsahuje respondeo marker."
    result = check_structure(seg, draft, conn)
    assert result.ok is False
    assert len(result.failures) == 1
    assert "respondeo" in result.failures[0] or "Odpovedám" in result.failures[0]


def test_no_formula_in_db_skips_check():
    """When DB returns no formula rows, check is skipped — ok=True, not a failure."""
    conn = FakeConn([])  # empty rows — no approved formulas
    seg = _make_seg(element_type="sed_contra")
    draft = "Táto veta neobsahuje žiadnu formulku."
    result = check_structure(seg, draft, conn)
    assert result.ok is True
    assert result.failures == []


def test_arg_segment_always_ok():
    """check_structure on an arg (objection) segment returns ok=True — no formula check."""
    conn = FakeConn(_STANDARD_FORMULA_ROWS)
    seg = _make_seg(element_type="arg", locator_path="I.q1.a1.arg1")
    draft = "Zdá sa, že Boh nie je jednoduchý."
    result = check_structure(seg, draft, conn)
    assert result.ok is True
    assert result.failures == []


def test_reply_ok_when_respondeo_absent():
    """check_structure returns ok=True for a reply that correctly omits the respondeo formula."""
    conn = FakeConn(_STANDARD_FORMULA_ROWS)
    seg = _make_seg(element_type="reply", locator_path="I.q1.a1.reply1")
    draft = "K prvému treba povedať, že argument je nesprávny."
    result = check_structure(seg, draft, conn)
    assert result.ok is True
    assert result.failures == []


def test_reply_fail_when_respondeo_present():
    """check_structure returns ok=False for a reply that accidentally includes the respondeo formula."""
    conn = FakeConn(_STANDARD_FORMULA_ROWS)
    seg = _make_seg(element_type="reply", locator_path="I.q1.a1.reply1")
    draft = "Odpovedám: k prvému treba povedať..."
    result = check_structure(seg, draft, conn)
    assert result.ok is False
    assert len(result.failures) == 1


def test_formula_loaded_from_db_not_hardcoded():
    """Formula must be loaded from DB: a custom form returned by a fake DB is recognised."""
    custom_rows = [
        {"latin_lemma": "sed_contra", "slovak_form": "VLASTNÁ_FORMULKA"},
        {"latin_lemma": "respondeo", "slovak_form": "VLASTNÁ_ODPOVEĎ"},
    ]
    conn = FakeConn(custom_rows)
    seg = _make_seg(element_type="sed_contra")
    # Draft contains the custom form, not any hardcoded Slovak text.
    draft = "VLASTNÁ_FORMULKA sa nachádza v texte."
    result = check_structure(seg, draft, conn)
    assert result.ok is True


# ── Terminology tests ─────────────────────────────────────────────────────────

def test_terminology_ok_when_all_present():
    """check_terminology returns ok=True when all required terms are present."""
    constraints = [
        {"latin_lemma": "forma", "required_slovak": "forma"},
        {"latin_lemma": "materia", "required_slovak": "matéria"},
    ]
    # Draft contains the exact required forms (not inflected).
    draft = "Každá vec má svoju forma a matéria, ktorá ju tvorí."
    result = check_terminology(draft, constraints)
    assert result.ok is True
    assert result.failures == []


def test_terminology_fail_when_term_missing():
    """check_terminology returns ok=False when a term is missing; failures contain term name."""
    constraints = [
        {"latin_lemma": "forma", "required_slovak": "forma"},
        {"latin_lemma": "actus", "required_slovak": "akt"},
    ]
    # Draft contains "forma" but not "akt".
    draft = "Táto veta obsahuje forma, ale nie druhý termín."
    result = check_terminology(draft, constraints)
    assert result.ok is False
    assert len(result.failures) == 1
    assert "actus" in result.failures[0] or "akt" in result.failures[0]


def test_terminology_case_insensitive():
    """Terminology check is case-insensitive."""
    constraints = [{"latin_lemma": "forma", "required_slovak": "Forma"}]
    # Draft has lowercase "forma"; constraint has title-case "Forma" — should match.
    draft = "každá vec má svoju forma a matériu."
    result = check_terminology(draft, constraints)
    assert result.ok is True


def test_terminology_diacritics_normalised():
    """Diacritics are stripped for comparison — milosť matches milosť."""
    constraints = [{"latin_lemma": "gratia", "required_slovak": "milosť"}]
    # Draft uses same word — confirms the normalisation path is exercised.
    draft = "Boh dáva milosť všetkým, ktorí o ňu prosí."
    result = check_terminology(draft, constraints)
    assert result.ok is True


def test_terminology_diacritics_cross_form():
    """A form with stripped diacritics matches the same word with diacritics in draft."""
    constraints = [{"latin_lemma": "gratia", "required_slovak": "milosť"}]
    # Simulate a draft where the accented character might render differently;
    # after normalisation both sides become "milost".
    draft = "Boh dáva milost (stripped form) všetkým."
    result = check_terminology(draft, constraints)
    assert result.ok is True


def test_terminology_empty_constraints():
    """Empty constraints list returns ok=True immediately."""
    result = check_terminology("Ľubovoľný text.", [])
    assert result.ok is True
    assert result.failures == []


def test_terminology_multiple_missing_all_reported():
    """All missing terms are reported in failures, not just the first."""
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
# These tests mock generate_slovak_forms so they run without the MorphoDiTa model.

def _mock_generate(lemma_to_forms: dict):
    """Return a generate_slovak_forms mock that maps lemma → frozenset of forms.

    Unknown lemmas return frozenset() — the OOV signal, same as MorphoDiTa.
    """
    def _fn(lemma: str) -> frozenset[str]:
        return frozenset(lemma_to_forms.get(lemma.lower(), set()))
    return _fn


def test_terminology_lemma_empty_constraints():
    """Empty constraints returns ok=True immediately, no morphology called."""
    result = check_terminology_lemma("Ľubovoľný text.", [])
    assert result.ok is True
    assert result.failures == []


def test_terminology_lemma_ok_when_inflected_form_matches(monkeypatch):
    """Returns ok=True when a generated declined form appears in the draft."""
    form_map = {"viera": {"viera", "viery", "viere", "vieru", "vierou"}}
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate(form_map))

    constraints = [{"latin_lemma": "fides", "required_slovak": "viera"}]
    draft = "Je silná vierou."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True
    assert result.failures == []


def test_terminology_lemma_fail_when_no_form_present(monkeypatch):
    """Returns ok=False when no inflected form of the required term is in the draft."""
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
    """Comparison is case-insensitive on both sides."""
    # Generated forms are lowercase; constraint is "Boh" (title-case), draft capitalised.
    form_map = {"boh": {"boh", "boha", "bohu"}}
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate(form_map))

    constraints = [{"latin_lemma": "deus", "required_slovak": "Boh"}]
    draft = "Boh je veľký."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_multiple_constraints_all_reported(monkeypatch):
    """All missing terms are reported, not just the first."""
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
    """Form-set check does not match on substring — 'forma' does not match 'informácia'."""
    form_map = {"forma": {"forma", "formy", "forme", "formou"}}
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate(form_map))

    constraints = [{"latin_lemma": "forma", "required_slovak": "forma"}]
    draft = "Text obsahuje informácia."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is False


# ── check_terminology_lemma — multi-word term (per-word form sets) ────────────

def test_terminology_lemma_multiword_term_all_components_ok(monkeypatch):
    """Multi-word term: each component word satisfied independently (verbatim here)."""
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    # category=None (Krystal-seeded) defaults to "term" → per-word matching.
    # Both words appear verbatim, satisfied before generation is consulted.
    constraints = [{"latin_lemma": "per se", "required_slovak": "o sebe", "category": None}]
    draft = "Boh existuje o sebe."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True
    assert result.failures == []


def test_terminology_lemma_multiword_term_inflected_components(monkeypatch):
    """Inflected multi-word constraint: 'intencionálneho obrazu' satisfies 'intencionálny obraz'."""
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
    """If only some words of a multi-word term are missing, the check fails."""
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


# ── check_terminology_lemma — OOV fallback (generation returns nothing) ───────

def test_terminology_lemma_oov_verbatim_match(monkeypatch):
    """OOV word present verbatim passes — exact match short-circuits generation."""
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "hybris", "required_slovak": "hybris", "category": "term"}]
    draft = "Text obsahuje hybris ako termín."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_oov_stem_prefix_match(monkeypatch):
    """OOV archaic lemma matches inflected forms via stem prefix (čnosť → čnostiam)."""
    # MorfFlex SK only has modern 'cnosť'; generation returns nothing for 'čnosť'.
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "virtus", "required_slovak": "čnosť", "category": "term"}]
    draft = "Smeruje k rozličným čnostiam."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_oov_latin_loan_declined(monkeypatch):
    """OOV Latin loan in -us matches declined forms that drop the ending (habitom)."""
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "habitus", "required_slovak": "habitus", "category": "term"}]
    draft = "Disponuje habitom čnosti."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_oov_absent_fails(monkeypatch):
    """OOV word with no stem match in the draft still fails."""
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "habitus", "required_slovak": "habitus", "category": "term"}]
    draft = "Tento text hovorí o inom pojme."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is False
    assert "habitus" in result.failures[0]


# ── check_terminology_lemma — formula category (regex) ───────────────────────

def test_terminology_lemma_formula_match(monkeypatch):
    """Formula category uses word-boundary regex; morphology not invoked for check."""
    # Patch generation to raise so any call would explode — confirms it's not used.
    def _boom(_):
        raise AssertionError("generate_slovak_forms must not be called for formula constraints")

    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _boom)

    constraints = [{"latin_lemma": "sed_contra", "required_slovak": "Avšak proti", "category": "formula"}]
    draft = "Avšak proti je to, čo hovorí Augustín."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True


def test_terminology_lemma_formula_word_boundary_no_false_positive(monkeypatch):
    """'po sebe' must NOT satisfy formula 'o sebe' — word boundary enforced."""
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "per se", "required_slovak": "o sebe", "category": "formula"}]
    draft = "Veci nasledujú po sebe v čase."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is False


def test_terminology_lemma_formula_fail_when_absent(monkeypatch):
    """Formula check fails when formula string is not in draft."""
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "respondeo", "required_slovak": "Odpovedám", "category": "formula"}]
    draft = "Táto veta neobsahuje formulku."
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is False
    assert "Odpovedám" in result.failures[0]


def test_terminology_lemma_formula_diacritics_normalised(monkeypatch):
    """Formula check normalises both sides: draft with caron stripped matches accented constraint.

    _normalise("Avšak") → "avsak" (š → s + combining_caron; caron dropped → "avsak").
    A draft rendered with "Avsak" (caron stripped) also normalises to "avsak". They match.
    """
    monkeypatch.setattr("src.translate.prechecks.generate_slovak_forms", _mock_generate({}))

    constraints = [{"latin_lemma": "sed_contra", "required_slovak": "Avšak proti", "category": "formula"}]
    draft = "Avsak proti je to, co hovori Augustín."  # š → s (caron stripped)
    result = check_terminology_lemma(draft, constraints)
    assert result.ok is True
