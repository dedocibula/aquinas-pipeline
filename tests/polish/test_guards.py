"""Unit tests for src/polish/guards.py.

Guards are pure functions — no DB, no MorphoDiTa (check_terminology_lemma is
patched at the usage site for locked_term_retention tests).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from polish.guards import (
    SCHOLASTIC_PARTICLES,
    length_ratio,
    locked_term_retention,
    particle_retention,
    run_guards,
    sentence_count_delta,
)
from translate.prechecks import CheckResult

# ── sentence_count_delta ──────────────────────────────────────────────────────


def test_sentence_count_delta_zero():
    assert sentence_count_delta("Boh je dobrý.", "Boh je dobrotivý.") == 0


def test_sentence_count_delta_positive():
    # polished splits one sentence into two
    assert sentence_count_delta("Boh je dobrý.", "Boh je dobrý. A svätý.") == 1


def test_sentence_count_delta_negative():
    # polished merges two sentences into one
    assert sentence_count_delta("Boh je dobrý. Totiž je svätý.", "Boh je dobrý a svätý.") == -1


def test_sentence_count_delta_multi():
    orig = "Prvá veta. Druhá veta. Tretia veta."
    pol  = "Prvá veta. Druhá veta. Tretia veta."
    assert sentence_count_delta(orig, pol) == 0


def test_sentence_count_delta_exclamation():
    # exclamation marks count the same as periods
    assert sentence_count_delta("Boh existuje!", "Boh nepochybne existuje!") == 0


# ── locked_term_retention ─────────────────────────────────────────────────────


def test_locked_term_retention_no_constraints():
    result = locked_term_retention("ľubovoľný text", [])
    assert result["ok"] is True
    assert result["missing_terms"] == []


def test_locked_term_retention_pass():
    with patch("polish.guards.check_terminology_lemma") as mock_check:
        mock_check.return_value = CheckResult(ok=True)
        c = [{"latin_lemma": "ratio", "required_slovak": "rozum", "category": "term"}]
        result = locked_term_retention("rozum je...", c)
    assert result["ok"] is True
    assert result["missing_terms"] == []
    mock_check.assert_called_once_with("rozum je...", c)


def test_locked_term_retention_fail():
    with patch("polish.guards.check_terminology_lemma") as mock_check:
        mock_check.return_value = CheckResult(
            ok=False, failed_terms=["rozum"], failures=["missing rozum"]
        )
        c = [{"latin_lemma": "ratio", "required_slovak": "rozum", "category": "term"}]
        result = locked_term_retention("iné slovo", c)
    assert result["ok"] is False
    assert "rozum" in result["missing_terms"]


def test_locked_term_retention_multiple_failures():
    with patch("polish.guards.check_terminology_lemma") as mock_check:
        mock_check.return_value = CheckResult(
            ok=False, failed_terms=["rozum", "bytie"], failures=["a", "b"]
        )
        result = locked_term_retention("text", [{"latin_lemma": "x", "required_slovak": "y"}])
    assert result["ok"] is False
    assert set(result["missing_terms"]) == {"rozum", "bytie"}


# ── particle_retention ────────────────────────────────────────────────────────


def test_particle_retention_no_particles_in_original():
    # Neither original nor polished has any scholastic particles.
    result = particle_retention("Pes beží.", "Pes rýchlo beží.")
    assert result["ok"] is True
    assert result["missing_particles"] == []


def test_particle_retention_particle_preserved():
    result = particle_retention("Totiž je to tak.", "Totiž je to naozaj tak.")
    assert result["ok"] is True
    assert result["missing_particles"] == []


def test_particle_retention_particle_dropped():
    result = particle_retention("Totiž je to tak.", "Je to naozaj tak.")
    assert result["ok"] is False
    assert "totiž" in result["missing_particles"]


def test_particle_retention_multiple_missing():
    result = particle_retention("Totiž avšak preto.", "Pes beží.")
    assert result["ok"] is False
    assert set(result["missing_particles"]) == {"totiž", "avšak", "preto"}


def test_particle_retention_case_insensitive():
    # "Totiž" (capitalised at sentence start) should still match
    result = particle_retention("Totiž Boh existuje.", "totiž Boh naozaj existuje.")
    assert result["ok"] is True


def test_particle_retention_particle_set_is_correct():
    expected = {"totiž", "teda", "avšak", "lebo", "preto", "však", "odtiaľ", "ale"}
    assert SCHOLASTIC_PARTICLES == expected


# ── length_ratio ──────────────────────────────────────────────────────────────


def test_length_ratio_equal():
    assert length_ratio("abc", "abc") == pytest.approx(1.0)


def test_length_ratio_double():
    assert length_ratio("abc", "abcabc") == pytest.approx(2.0)


def test_length_ratio_half():
    assert length_ratio("abcabc", "abc") == pytest.approx(0.5)


def test_length_ratio_empty_original_returns_one():
    assert length_ratio("", "abc") == pytest.approx(1.0)


# ── run_guards ────────────────────────────────────────────────────────────────


def test_run_guards_all_ok():
    with patch("polish.guards.check_terminology_lemma") as mock_check:
        mock_check.return_value = CheckResult(ok=True)
        flags = run_guards("totiž Boh je dobrý.", "totiž Boh je dobrotivý.", [])
    assert flags["ok"] is True
    assert flags["sentence_delta"] == 0
    assert flags["term_retention_ok"] is True
    assert flags["particle_retention_ok"] is True
    assert 0.5 <= flags["length_ratio"] <= 2.0


def test_run_guards_fail_sentence_delta():
    with patch("polish.guards.check_terminology_lemma") as mock_check:
        mock_check.return_value = CheckResult(ok=True)
        flags = run_guards("Jedna veta.", "Jedna veta. Druhá veta.", [])
    assert flags["ok"] is False
    assert flags["sentence_delta"] == 1


def test_run_guards_fail_missing_term():
    with patch("polish.guards.check_terminology_lemma") as mock_check:
        mock_check.return_value = CheckResult(
            ok=False, failed_terms=["rozum"], failures=["missing"]
        )
        flags = run_guards("Rozum je...", "Iné slovo je...", [{"latin_lemma": "ratio", "required_slovak": "rozum"}])
    assert flags["ok"] is False
    assert flags["term_retention_ok"] is False
    assert "rozum" in flags["missing_terms"]


def test_run_guards_fail_particle_dropped():
    with patch("polish.guards.check_terminology_lemma") as mock_check:
        mock_check.return_value = CheckResult(ok=True)
        flags = run_guards("totiž Boh je.", "Boh je.", [])
    assert flags["ok"] is False
    assert flags["particle_retention_ok"] is False
    assert "totiž" in flags["missing_particles"]


def test_run_guards_fail_length_ratio_too_long():
    with patch("polish.guards.check_terminology_lemma") as mock_check:
        mock_check.return_value = CheckResult(ok=True)
        orig = "a"
        pol  = "a" * 300  # ratio = 300, exceeds max 2.0
        flags = run_guards(orig, pol, [])
    assert flags["ok"] is False
    assert flags["length_ratio"] > 2.0


def test_run_guards_fail_length_ratio_too_short():
    with patch("polish.guards.check_terminology_lemma") as mock_check:
        mock_check.return_value = CheckResult(ok=True)
        orig = "a" * 100
        pol  = "a"  # ratio ≈ 0.01, below 0.5
        flags = run_guards(orig, pol, [])
    assert flags["ok"] is False
    assert flags["length_ratio"] < 0.5


def test_run_guards_returns_all_keys():
    with patch("polish.guards.check_terminology_lemma") as mock_check:
        mock_check.return_value = CheckResult(ok=True)
        flags = run_guards("Boh.", "Boh.", [])
    expected_keys = {
        "ok", "sentence_delta", "term_retention_ok", "missing_terms",
        "particle_retention_ok", "missing_particles", "length_ratio",
    }
    assert set(flags.keys()) == expected_keys
