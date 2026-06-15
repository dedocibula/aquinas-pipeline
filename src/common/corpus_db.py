"""Corpus-wide DB query helpers — thin wrappers over ``SegmentRepository``.

The SQL now lives in ``storage.repositories``. These functions remain as
a transition shim so the orchestration code (translate.run) keeps its existing
import surface.
"""

from __future__ import annotations

import psycopg2.extensions

from storage.repositories import SegmentRepository


def get_all_article_locators(
    conn: psycopg2.extensions.connection, work_id: int = 1
) -> list[str]:
    """Return distinct article-level locator prefixes (first 3 ltree components)."""
    return SegmentRepository(conn).get_all_article_locators(work_id)


def get_pending_segment_ids_for_article(
    conn: psycopg2.extensions.connection,
    locator_prefix: str,
    work_id: int = 1,
    segment_filter: frozenset[int] | None = None,
) -> list[int]:
    """Return pending segment IDs under locator_prefix that have translatable text."""
    return SegmentRepository(conn).get_pending_segment_ids_for_article(
        locator_prefix, work_id, segment_filter
    )


def has_pending_segments(
    conn: psycopg2.extensions.connection,
    locator_prefix: str,
    work_id: int = 1,
    segment_filter: frozenset[int] | None = None,
) -> bool:
    """Return True if the article has at least one pending segment."""
    return SegmentRepository(conn).has_pending_segments(
        locator_prefix, work_id, segment_filter
    )


def get_stale_segments(conn: psycopg2.extensions.connection, work_id: int = 1) -> list[int]:
    """Return segment IDs whose term_usage references an outdated glossary sense."""
    return SegmentRepository(conn).get_stale_segments(work_id)


def get_human_edited_segments(
    conn: psycopg2.extensions.connection, segment_ids: list[int]
) -> list[int]:
    """Return the subset of segment_ids that have a human-edited Slovak text row."""
    return SegmentRepository(conn).get_human_edited_segments(segment_ids)


def flag_needs_human(
    conn: psycopg2.extensions.connection, segment_ids: list[int], note: str
) -> None:
    """Set translation_status='needs_human' with a reviewer note, no re-translation."""
    SegmentRepository(conn).flag_needs_human(segment_ids, note)


def reset_translation_status(
    conn: psycopg2.extensions.connection, segment_ids: list[int]
) -> None:
    """Reset translation_status to 'pending' for the given segments."""
    SegmentRepository(conn).reset_translation_status(segment_ids)
