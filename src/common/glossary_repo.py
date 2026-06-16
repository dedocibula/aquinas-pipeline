"""Glossary write helpers — thin wrappers over ``GlossaryRepository``.

The SQL now lives in ``storage.repositories``. These functions remain as a
transition shim so the review import code (review.import_approvals) keeps its
existing import surface until it is migrated to ``GlossaryRepository`` directly.
"""

from __future__ import annotations

import psycopg2.extensions

from storage.repositories import GlossaryRepository


def update_sense_status(
    conn: psycopg2.extensions.connection, sense_id: int, status: str
) -> None:
    """Set glossary_sense.status for a reviewer approval or rejection."""
    GlossaryRepository(conn).update_sense_status(sense_id, status)


def bump_sense_version(conn: psycopg2.extensions.connection, sense_id: int) -> int:
    """Increment glossary_sense.version and return the new value."""
    return GlossaryRepository(conn).bump_sense_version(sense_id)


def write_human_rendering(
    conn: psycopg2.extensions.connection, sense_id: int, sk_text: str, src_id: int
) -> None:
    """Persist a reviewer-confirmed Slovak rendering."""
    GlossaryRepository(conn).write_human_rendering(sense_id, sk_text, src_id)
