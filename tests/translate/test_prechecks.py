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
