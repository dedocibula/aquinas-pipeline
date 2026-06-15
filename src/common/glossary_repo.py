"""Glossary DB helpers — thin wrappers over ``GlossaryRepository``.

The SQL now lives in ``storage.repositories``. These module-level
functions remain as a transition shim returning the legacy dict shapes so the
resolver and review code keep working until they consume models directly.
"""

from __future__ import annotations

from storage.repositories import GlossaryRepository


def _load_glossary(conn) -> tuple[list[dict], list[dict]]:
    """Return (multiword_terms, singleword_terms) as legacy term dicts.

    Each term dict: {term_id, latin_lemma, is_multiword, category, la_surface, senses: [...]}
    Each sense dict: {sense_id, context_label, cs_lemma, en_cue, sk_content, version, la_surface}
    term.la_surface = glossary_term.la_surface column (NULL → fall back to latin_lemma).
    """
    multiword, singleword = GlossaryRepository(conn).load_glossary()
    return [t.as_dict() for t in multiword], [t.as_dict() for t in singleword]


def _load_segments(conn, wid: int) -> list[dict]:
    """Return body segments with la/cs/en text for the given work, sorted by locator."""
    from storage.repositories import SegmentRepository

    return [s.as_dict() for s in SegmentRepository(conn).load_body_segments(wid)]


def update_sense_status(conn, sense_id: int, status: str) -> None:
    """Set glossary_sense.status for a reviewer approval or rejection."""
    GlossaryRepository(conn).update_sense_status(sense_id, status)


def bump_sense_version(conn, sense_id: int) -> int:
    """Increment glossary_sense.version and return the new value."""
    return GlossaryRepository(conn).bump_sense_version(sense_id)


def write_human_rendering(conn, sense_id: int, sk_text: str, src_id: int) -> None:
    """Persist a reviewer-confirmed Slovak rendering."""
    GlossaryRepository(conn).write_human_rendering(sense_id, sk_text, src_id)
