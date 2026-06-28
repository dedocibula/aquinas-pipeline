"""Shared prompt-fragment builders used across translate and polish pipelines."""

from __future__ import annotations

from collections import defaultdict


def build_hard_constraints_block(constraints: list[dict]) -> str:
    """Build the <hard_constraints> XML block from a list of term constraint dicts.

    Each dict must have 'required_slovak' and 'latin_lemma' keys; 'context_label'
    is optional. Returns the block as a single string (no leading/trailing newline).
    """
    parts: list[str] = []
    parts.append("<hard_constraints>")
    if constraints:
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
    else:
        parts.append("  \n</hard_constraints>")
    return "\n".join(parts)
