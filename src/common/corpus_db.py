"""Corpus-wide DB query helpers for M5 full-corpus translation orchestration."""

from __future__ import annotations

import psycopg2.extensions
import psycopg2.extras


def get_all_article_locators(
    conn: psycopg2.extensions.connection, work_id: int = 1
) -> list[str]:
    """Return distinct article-level locator prefixes (first 3 ltree components).

    Examples: 'I.q1.a1', 'I.q1.question_title', 'I.q2.a3', ...

    Each prefix represents one unit of orchestration work (one Prefect task).
    Segments at depth >= 3 are grouped by their 3-component prefix. Depth-2
    segments (if any) are excluded — they have no article anchor.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ltree2text(subpath(locator_path, 0, 3)) AS prefix
            FROM segment
            WHERE work_id = %s
              AND nlevel(locator_path) >= 3
            ORDER BY prefix
            """,
            (work_id,),
        )
        return [row[0] for row in cur.fetchall()]


def get_pending_segment_ids_for_article(
    conn: psycopg2.extensions.connection,
    locator_prefix: str,
    work_id: int = 1,
    segment_filter: frozenset[int] | None = None,
) -> list[int]:
    """Return pending segment IDs under locator_prefix that have translatable text.

    Ordered by locator_path so translate_segment calls are deterministic across
    workers even if the DB returns rows in a different physical order.
    work_id guards against returning segments from a different loaded work that
    happens to share the same locator prefix.
    segment_filter: when provided, only those segment IDs are returned.
    """
    with conn.cursor() as cur:
        if segment_filter is not None:
            cur.execute(
                """
                SELECT s.segment_id
                FROM segment s
                WHERE s.locator_path <@ %s::ltree
                  AND s.work_id = %s
                  AND s.translation_status = 'pending'
                  AND s.segment_id = ANY(%s)
                  AND EXISTS (
                      SELECT 1 FROM segment_text st
                      WHERE st.segment_id = s.segment_id AND st.lang IN ('la', 'en')
                  )
                ORDER BY s.locator_path
                """,
                (locator_prefix, work_id, list(segment_filter)),
            )
        else:
            cur.execute(
                """
                SELECT s.segment_id
                FROM segment s
                WHERE s.locator_path <@ %s::ltree
                  AND s.work_id = %s
                  AND s.translation_status = 'pending'
                  AND EXISTS (
                      SELECT 1 FROM segment_text st
                      WHERE st.segment_id = s.segment_id AND st.lang IN ('la', 'en')
                  )
                ORDER BY s.locator_path
                """,
                (locator_prefix, work_id),
            )
        return [row[0] for row in cur.fetchall()]


def has_pending_segments(
    conn: psycopg2.extensions.connection,
    locator_prefix: str,
    work_id: int = 1,
    segment_filter: frozenset[int] | None = None,
) -> bool:
    """Return True if the article has at least one pending segment.

    segment_filter: when provided, only those segment IDs count as pending.
    """
    with conn.cursor() as cur:
        if segment_filter is not None:
            cur.execute(
                """
                SELECT 1
                FROM segment
                WHERE locator_path <@ %s::ltree
                  AND work_id = %s
                  AND translation_status = 'pending'
                  AND segment_id = ANY(%s)
                LIMIT 1
                """,
                (locator_prefix, work_id, list(segment_filter)),
            )
        else:
            cur.execute(
                """
                SELECT 1
                FROM segment
                WHERE locator_path <@ %s::ltree
                  AND work_id = %s
                  AND translation_status = 'pending'
                LIMIT 1
                """,
                (locator_prefix, work_id),
            )
        return cur.fetchone() is not None


def get_stale_segments(conn: psycopg2.extensions.connection, work_id: int = 1) -> list[int]:
    """Return segment IDs whose term_usage references an outdated glossary sense.

    A segment is stale when any sense it used has since been updated
    (sense_version_used < current glossary_sense.version). These segments
    must be reset to 'pending' and re-translated. work_id scopes the result
    to the target work so a multi-work DB never cross-contaminates re-runs.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT tu.segment_id
            FROM term_usage tu
            JOIN glossary_sense gs ON tu.sense_id = gs.sense_id
            JOIN segment s ON s.segment_id = tu.segment_id
            WHERE s.work_id = %s
              AND tu.sense_version_used < gs.version
            ORDER BY tu.segment_id
            """,
            (work_id,),
        )
        return [row[0] for row in cur.fetchall()]


def get_human_edited_segments(
    conn: psycopg2.extensions.connection, segment_ids: list[int]
) -> list[int]:
    """Return the subset of segment_ids that have a human-edited Slovak text row.

    A segment_text(sk) row from the 'human' source means a reviewer already
    touched this segment's final text. rerun_stale must never reset such
    segments to pending — re-translation would overwrite reviewed work.
    """
    if not segment_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT st.segment_id
            FROM segment_text st
            JOIN source s ON s.source_id = st.source_id
            WHERE st.segment_id = ANY(%s)
              AND st.lang = 'sk'
              AND s.code = 'human'
            ORDER BY st.segment_id
            """,
            (segment_ids,),
        )
        return [row[0] for row in cur.fetchall()]


def flag_needs_human(
    conn: psycopg2.extensions.connection, segment_ids: list[int], note: str
) -> None:
    """Set translation_status='needs_human' with a reviewer note, no re-translation.

    The note lands in reviewer_notes.last_feedback so it shows up in the
    needs-human triage report and the preview server detail panel.
    """
    if not segment_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE segment "
            "SET translation_status = 'needs_human', reviewer_notes = %s "
            "WHERE segment_id = ANY(%s)",
            (psycopg2.extras.Json({"last_feedback": note}), segment_ids),
        )


def reset_translation_status(
    conn: psycopg2.extensions.connection, segment_ids: list[int]
) -> None:
    """Reset translation_status to 'pending' for the given segments.

    Existing segment_text(sk, model) and term_usage rows are left in place;
    translate_segment upserts over them on re-run, so no purge is needed.
    reviewer_notes is also cleared since the old feedback is no longer valid.
    """
    if not segment_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE segment "
            "SET translation_status = 'pending', reviewer_notes = NULL "
            "WHERE segment_id = ANY(%s)",
            (segment_ids,),
        )
