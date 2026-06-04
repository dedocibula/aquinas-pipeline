"""
DB query helpers for the Flask preview server.

All functions accept a psycopg2 connection.
They are intentionally separate from src/common/db.py — that module manages
connection lifecycle; this module owns the server-specific SQL.
"""

from __future__ import annotations

import psycopg2
import psycopg2.extras


def get_all_questions(conn: psycopg2.extensions.connection) -> list[dict]:
    """Return distinct question-level locator paths (depth 2, e.g. 'I.q1').

    Each dict has a single key ``question_path``.
    Ordered by locator_path so pars appear in natural document order.
    """
    sql = """
        SELECT DISTINCT
            ltree2text(subpath(locator_path, 0, 2)) AS question_path
        FROM segment
        ORDER BY subpath(locator_path, 0, 2)
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return [dict(row) for row in cur.fetchall()]


def get_question_articles(
    conn: psycopg2.extensions.connection,
    question_path: str,
) -> list[dict]:
    """Return articles within a question (depth 3) with translation-status summary.

    Each dict: ``{article_path, translated_count, total_count}``.
    ``question_path`` is an ltree string like 'I.q3'.
    """
    sql = """
        SELECT
            ltree2text(subpath(locator_path, 0, 3)) AS article_path,
            COUNT(*) FILTER (WHERE translation_status = 'translated') AS translated_count,
            COUNT(*)                                                   AS total_count
        FROM segment
        WHERE locator_path <@ %s::ltree
          AND nlevel(locator_path) >= 3
        GROUP BY article_path, subpath(locator_path, 0, 3)
        ORDER BY subpath(locator_path, 0, 3)
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (question_path,))
        return [dict(row) for row in cur.fetchall()]


def get_article_segments(
    conn: psycopg2.extensions.connection,
    article_path: str,
) -> list[dict]:
    """Return all segments for an article with Latin and preferred Slovak text.

    Prefers the human SK source over the model source when both are present.
    Returns: segment_id, locator_path, element_type, reply_to,
             translation_status, reviewer_notes, latin, slovak.
    """
    sql = """
        SELECT
            s.segment_id,
            s.locator_path::text,
            s.element_type,
            s.reply_to,
            s.translation_status,
            s.reviewer_notes,
            latin.content   AS latin,
            slovak.content  AS slovak
        FROM segment s
        JOIN segment_text latin
            ON  latin.segment_id = s.segment_id
            AND latin.lang = 'la'
        LEFT JOIN LATERAL (
            SELECT st2.content
            FROM segment_text st2
            JOIN source src ON src.source_id = st2.source_id
            WHERE st2.segment_id = s.segment_id
              AND st2.lang = 'sk'
            ORDER BY (src.code = 'human') DESC
            LIMIT 1
        ) slovak ON true
        WHERE s.locator_path <@ %s::ltree
        ORDER BY s.locator_path
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (article_path,))
        return [dict(row) for row in cur.fetchall()]


def get_prev_next_article(
    conn: psycopg2.extensions.connection,
    article_path: str,
) -> dict:
    """Return the neighbouring article locator paths.

    Returns ``{"prev": str|None, "next": str|None}``.
    Articles are ordered by their ltree locator_path.
    """
    sql = """
        SELECT prev, next FROM (
            SELECT
                ltree2text(ap)                                   AS ap,
                ltree2text(lag(ap)  OVER (ORDER BY ap))          AS prev,
                ltree2text(lead(ap) OVER (ORDER BY ap))          AS next
            FROM (
                SELECT DISTINCT subpath(locator_path, 0, 3) AS ap
                FROM segment
                WHERE nlevel(locator_path) >= 3
            ) t
        ) windowed
        WHERE ap = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (article_path,))
        row = cur.fetchone()

    if row is None:
        return {"prev": None, "next": None}
    return {"prev": row[0], "next": row[1]}


def get_translation_progress(conn: psycopg2.extensions.connection) -> dict:
    """Return counts per translation_status across all segments.

    Returns ``{"pending": N, "translated": N, "needs_human": N}``.
    """
    sql = """
        SELECT
            COUNT(*) FILTER (WHERE translation_status = 'pending')     AS pending,
            COUNT(*) FILTER (WHERE translation_status = 'translated')  AS translated,
            COUNT(*) FILTER (WHERE translation_status = 'needs_human') AS needs_human
        FROM segment
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return {
        "pending":     int(row[0]),
        "translated":  int(row[1]),
        "needs_human": int(row[2]),
    }


def get_structural_formulas(conn: psycopg2.extensions.connection) -> dict[str, str]:
    """Load approved Slovak forms for sed_contra, respondeo, praeterea.

    Queries glossary_term + glossary_sense + sense_rendering(lang='sk', status='approved').
    Returns e.g. {"sed_contra": "Na druhej strane:", "respondeo": "Odpoveď:"}
    Missing formulas are silently omitted (never raises).
    """
    latin_terms = ("sed_contra", "respondeo", "praeterea")
    placeholders = ", ".join(["%s"] * len(latin_terms))
    sql = f"""
        SELECT
            gt.latin_lemma,
            sr.content
        FROM glossary_term gt
        JOIN glossary_sense gs  ON gs.term_id  = gt.term_id
        JOIN sense_rendering sr ON sr.sense_id = gs.sense_id
        WHERE gt.latin_lemma IN ({placeholders})
          AND sr.lang         = 'sk'
          AND gs.status       = 'approved'
        ORDER BY gt.latin_lemma
    """
    result: dict[str, str] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(sql, latin_terms)
            for row in cur.fetchall():
                # Keep the first approved rendering per term (in case of duplicates).
                lemma, content = row[0], row[1]
                if lemma not in result:
                    result[lemma] = content
    except Exception:
        # Structural formulas are non-critical; fall back to hardcoded defaults in the
        # template.  Log loudly so the operator knows something is wrong.
        import traceback
        traceback.print_exc()
    return result
