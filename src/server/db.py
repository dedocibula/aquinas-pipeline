"""
DB query helpers for the Flask preview server.

All functions accept a psycopg2 connection.
They are intentionally separate from src/common/db.py — that module manages
connection lifecycle; this module owns the server-specific SQL.
"""

from __future__ import annotations

import psycopg2
import psycopg2.extras

from common.db import source_id


def get_all_questions(conn: psycopg2.extensions.connection) -> list[dict]:
    """Return distinct question-level locator paths (depth 2, e.g. 'I.q1').

    Each dict has a single key ``question_path``.
    Ordered by locator_path so pars appear in natural document order.
    """
    sql = """
        SELECT DISTINCT
            ltree2text(subpath(locator_path, 0, 2)) AS question_path,
            subpath(locator_path, 0, 2)             AS _sort_key
        FROM segment
        ORDER BY _sort_key
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
    """Return all segments for an article with Latin, Czech, English, and preferred Slovak text.

    Prefers the human SK source over the model source when both are present.
    Returns: segment_id, locator_path, element_type, reply_to,
             translation_status, reviewer_notes, latin, czech, english, slovak.
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
            czech.content   AS czech,
            english.content AS english,
            slovak.content  AS slovak
        FROM segment s
        JOIN segment_text latin
            ON  latin.segment_id = s.segment_id
            AND latin.lang = 'la'
        LEFT JOIN LATERAL (
            SELECT st3.content
            FROM segment_text st3
            JOIN source src3 ON src3.source_id = st3.source_id
            WHERE st3.segment_id = s.segment_id
              AND st3.lang = 'cs'
            ORDER BY src3.authority_rank ASC
            LIMIT 1
        ) czech ON true
        LEFT JOIN LATERAL (
            SELECT st4.content
            FROM segment_text st4
            JOIN source src4 ON src4.source_id = st4.source_id
            WHERE st4.segment_id = s.segment_id
              AND st4.lang = 'en'
            ORDER BY src4.authority_rank ASC
            LIMIT 1
        ) english ON true
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


def get_segment_constraints(
    conn: psycopg2.extensions.connection,
    segment_ids: list[int],
) -> dict[int, list[dict]]:
    """Return approved term constraints used for each segment.

    Joins term_usage → glossary_sense (status='approved') → sense_rendering (lang='sk')
    → glossary_term to surface the Latin lemma and optional context_label.

    Returns a dict keyed by segment_id; each value is a list of dicts with keys
    ``latin_lemma``, ``slovak``, ``context_label``.
    Missing segment_ids are not included (caller treats absence as empty list).
    """
    if not segment_ids:
        return {}

    placeholders = ", ".join(["%s"] * len(segment_ids))
    sql = f"""
        SELECT DISTINCT ON (tu.segment_id, gs.sense_id)
            tu.segment_id,
            gt.latin_lemma,
            sr.content        AS slovak,
            gs.context_label
        FROM term_usage tu
        JOIN glossary_sense  gs ON gs.sense_id  = tu.sense_id
        JOIN glossary_term   gt ON gt.term_id   = gs.term_id
        JOIN sense_rendering sr ON sr.sense_id  = gs.sense_id
        JOIN source           s ON s.source_id  = sr.source_id
        WHERE tu.segment_id IN ({placeholders})
          AND gs.status = 'approved'
          AND sr.lang   = 'sk'
        ORDER BY tu.segment_id, gs.sense_id, s.authority_rank
    """
    result: dict[int, list[dict]] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, segment_ids)
        for row in cur.fetchall():
            sid = row["segment_id"]
            result.setdefault(sid, []).append(
                {
                    "latin_lemma":   row["latin_lemma"],
                    "slovak":        row["slovak"],
                    "context_label": row["context_label"],
                }
            )
    return result


def approve_segment(
    conn: psycopg2.extensions.connection,
    segment_id: int,
) -> bool:
    """Flip translation_status from 'needs_human' to 'translated'.

    Returns True if the row was updated, False if it was not in needs_human state
    (idempotent; no error raised for other statuses).
    """
    sql = """
        UPDATE segment
        SET translation_status = 'translated'
        WHERE segment_id = %s
          AND translation_status = 'needs_human'
    """
    with conn.cursor() as cur:
        cur.execute(sql, (segment_id,))
        updated = cur.rowcount
    return updated > 0


def save_segment_text(
    conn: psycopg2.extensions.connection,
    segment_id: int,
    text: str,
) -> bool:
    """Upsert Slovak segment text from the human source; set status=translated.

    Returns True always. Does NOT commit — caller's get_conn() handles commit.
    Raises RuntimeError if the 'human' source row is missing.
    """
    with conn.cursor() as cur:
        # Verify the segment exists before touching segment_text.
        cur.execute(
            "SELECT 1 FROM segment WHERE segment_id = %s AND translation_status != 'pending'",
            (segment_id,),
        )
        if cur.fetchone() is None:
            # Covers both non-existent segment_id and pending segments (no UI path
            # renders the Edit button for pending rows, so this is an API misuse).
            return False

        human_src_id = source_id(conn, "human")
        cur.execute(
            """
            INSERT INTO segment_text (segment_id, lang, content, source_id)
            VALUES (%s, 'sk', %s, %s)
            ON CONFLICT (segment_id, lang, source_id) DO UPDATE
                SET content = EXCLUDED.content
            """,
            (segment_id, text, human_src_id),
        )
        cur.execute(
            "UPDATE segment SET translation_status = 'translated' WHERE segment_id = %s",
            (segment_id,),
        )
    return True


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
