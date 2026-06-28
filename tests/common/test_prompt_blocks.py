"""Tests for common.prompt_blocks.build_hard_constraints_block.

Verifies that the extracted helper produces byte-for-byte identical output to the
inline block that previously lived in translate.translator.build_initial_user_turn,
as well as the edge cases (empty constraints, duplicates, context labels).
"""

from __future__ import annotations

from common.prompt_blocks import build_hard_constraints_block


class TestBuildHardConstraintsBlock:
    def test_empty_constraints_produces_empty_tag(self):
        result = build_hard_constraints_block([])
        assert result == "<hard_constraints>\n  \n</hard_constraints>"

    def test_single_constraint_no_context(self):
        constraints = [{"latin_lemma": "virtus", "required_slovak": "čnosť"}]
        result = build_hard_constraints_block(constraints)
        assert '<term latin="virtus" required_slovak="čnosť" />' in result
        assert "⚠ CRITICAL" in result
        assert result.startswith("<hard_constraints>")
        assert "</hard_constraints>" in result

    def test_duplicate_latin_forms_collapsed(self):
        constraints = [
            {"latin_lemma": "virtute", "required_slovak": "čnosť"},
            {"latin_lemma": "virtutem", "required_slovak": "čnosť"},
        ]
        result = build_hard_constraints_block(constraints)
        # Both latin forms must appear on ONE term line, sorted and comma-separated
        assert 'latin="virtute, virtutem"' in result
        assert result.count("<term ") == 1

    def test_context_label_emitted(self):
        constraints = [
            {"latin_lemma": "actus", "required_slovak": "úkon", "context_label": "moral"},
        ]
        result = build_hard_constraints_block(constraints)
        assert 'context="moral"' in result
        assert 'required_slovak="úkon"' in result

    def test_no_context_label_omitted(self):
        constraints = [{"latin_lemma": "esse", "required_slovak": "bytie"}]
        result = build_hard_constraints_block(constraints)
        assert "context=" not in result

    def test_multiple_distinct_terms(self):
        constraints = [
            {"latin_lemma": "virtus", "required_slovak": "čnosť"},
            {"latin_lemma": "actus", "required_slovak": "úkon"},
        ]
        result = build_hard_constraints_block(constraints)
        assert result.count("<term ") == 2
        assert "čnosť" in result
        assert "úkon" in result

    def test_same_latin_different_slovak_produces_two_lines(self):
        constraints = [
            {"latin_lemma": "bonum", "required_slovak": "dobro"},
            {"latin_lemma": "bonum", "required_slovak": "dobrotivosť"},
        ]
        result = build_hard_constraints_block(constraints)
        assert result.count("<term ") == 2

    def test_critical_warning_absent_when_empty(self):
        result = build_hard_constraints_block([])
        assert "⚠ CRITICAL" not in result

    def test_output_matches_translator_inline_logic(self):
        """Regression: helper must produce same string as the original translator inline block."""
        from collections import defaultdict

        constraints = [
            {"latin_lemma": "virtute", "required_slovak": "čnosť", "context_label": None},
            {"latin_lemma": "virtutem", "required_slovak": "čnosť", "context_label": ""},
            {"latin_lemma": "actus", "required_slovak": "úkon", "context_label": "moral"},
        ]

        # Replicate the original inline logic exactly.
        parts: list[str] = []
        parts.append("<hard_constraints>")
        grouped: dict[tuple, list[str]] = defaultdict(list)
        for c in constraints:
            key = (c["required_slovak"], c.get("context_label") or "")
            grouped[key].append(c["latin_lemma"])
        for (required_slovak, context_label), latin_forms in grouped.items():
            latin = ", ".join(sorted(set(latin_forms)))
            qualifier = f' context="{context_label}"' if context_label else ""
            parts.append(
                f'  <term latin="{latin}" required_slovak="{required_slovak}"{qualifier} />'
            )
        parts.append("</hard_constraints>")
        parts.append(
            "\n⚠ CRITICAL: The terms in <hard_constraints> are compiler locks. "
            "They must appear exactly as required, inflected for Slovak grammar. "
            "No synonyms are permitted; failure results in immediate machine rejection."
        )
        expected = "\n".join(parts)

        assert build_hard_constraints_block(constraints) == expected
