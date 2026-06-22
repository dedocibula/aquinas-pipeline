"""
DB query helpers for the Flask preview server.

All functions accept a psycopg2 connection.
They are intentionally separate from src/common/db.py — that module manages
connection lifecycle; this module owns the server-specific SQL.
"""

from __future__ import annotations

import psycopg2
import psycopg2.extras

from storage.db import source_id

# ---------------------------------------------------------------------------
# Shared segment SELECT helper
# ---------------------------------------------------------------------------


def _segment_select_sql(where_clause: str) -> str:
    """Return the full segment SELECT+FROM+JOIN block with a caller-supplied WHERE clause.

    Columns returned: segment_id, locator_path, element_type, reply_to,
    translation_status, reviewer_notes, latin, czech, english,
    slovak_model, slovak_human, human_note, human_reviewed_by, human_version.
    """
    return f"""
        SELECT
            s.segment_id,
            s.locator_path::text,
            s.element_type,
            s.reply_to,
            s.translation_status,
            s.reviewer_notes,
            latin.content      AS latin,
            czech.content      AS czech,
            english.content    AS english,
            sk_model.content   AS slovak_model,
            sk_human.content   AS slovak_human,
            sr.human_note,
            sr.human_reviewed_by,
            COALESCE(sr.human_version, 0) AS human_version
        FROM segment s
        LEFT JOIN segment_text latin
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
            SELECT st_m.content
            FROM segment_text st_m
            JOIN source src_m ON src_m.source_id = st_m.source_id
            WHERE st_m.segment_id = s.segment_id
              AND st_m.lang = 'sk'
              AND src_m.code = 'model'
            LIMIT 1
        ) sk_model ON true
        LEFT JOIN LATERAL (
            SELECT st_h.content
            FROM segment_text st_h
            JOIN source src_h ON src_h.source_id = st_h.source_id
            WHERE st_h.segment_id = s.segment_id
              AND st_h.lang = 'sk'
              AND src_h.code = 'human'
            LIMIT 1
        ) sk_human ON true
        LEFT JOIN segment_review sr ON sr.segment_id = s.segment_id
        {where_clause}
    """


# ---------------------------------------------------------------------------
# Public query helpers
# ---------------------------------------------------------------------------


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

    Each dict: ``{article_path, translated_count, needs_human_count, reviewed_count, total_count}``.
    ``question_path`` is an ltree string like 'I.q3'.
    """
    sql = """
        SELECT
            ltree2text(subpath(s.locator_path, 0, 3)) AS article_path,
            COUNT(*) FILTER (WHERE s.translation_status = 'translated')  AS translated_count,
            COUNT(*) FILTER (WHERE s.translation_status = 'needs_human') AS needs_human_count,
            COUNT(sr.segment_id)                                          AS reviewed_count,
            COUNT(*)                                                      AS total_count
        FROM segment s
        LEFT JOIN segment_review sr ON sr.segment_id = s.segment_id
        WHERE s.locator_path <@ %s::ltree
          AND nlevel(s.locator_path) >= 3
          AND s.element_type != 'preamble'
        GROUP BY article_path, subpath(s.locator_path, 0, 3)
        ORDER BY subpath(s.locator_path, 0, 3)
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (question_path,))
        return [dict(row) for row in cur.fetchall()]


def get_article_segments(
    conn: psycopg2.extensions.connection,
    article_path: str,
) -> list[dict]:
    """Return all segments for an article with Latin, Czech, English, and Slovak text.

    Returns separate machine (slovak_model) and human (slovak_human) Slovak columns,
    plus human-review metadata from segment_review.
    """
    sql = _segment_select_sql("""
        WHERE s.locator_path <@ %s::ltree
          AND (latin.content IS NOT NULL OR s.element_type = 'article_title')
        ORDER BY s.locator_path
    """)
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
                WHERE nlevel(locator_path) >= 3 AND element_type != 'preamble'
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
    """Return counts per translation_status across all segments, plus reviewed count.

    Returns ``{"pending": N, "translated": N, "needs_human": N, "reviewed": N}``.
    """
    sql = """
        SELECT
            COUNT(*) FILTER (WHERE translation_status = 'pending')     AS pending,
            COUNT(*) FILTER (WHERE translation_status = 'translated')  AS translated,
            COUNT(*) FILTER (WHERE translation_status = 'needs_human') AS needs_human,
            COUNT(*) FILTER (
                WHERE EXISTS (
                    SELECT 1 FROM segment_review sr
                    WHERE sr.segment_id = segment.segment_id
                )
            ) AS reviewed
        FROM segment
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return {
        "pending": int(row[0]),
        "translated": int(row[1]),
        "needs_human": int(row[2]),
        "reviewed": int(row[3]),
    }


def get_question_title_segment(
    conn: psycopg2.extensions.connection,
    question_path: str,
) -> dict | None:
    """Return the question_title segment for a question, or None if absent."""
    sql = _segment_select_sql(
        "WHERE s.locator_path = %s::ltree AND s.element_type = 'question_title'"
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (question_path,))
        row = cur.fetchone()
    return dict(row) if row else None


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
                    "latin_lemma": row["latin_lemma"],
                    "slovak": row["slovak"],
                    "context_label": row["context_label"],
                }
            )
    return result


def get_question_preamble_segment(
    conn: psycopg2.extensions.connection,
    question_path: str,
) -> dict | None:
    """Return the preamble segment for a question, or None if absent.

    Preambles sit at ``<question_path>.preamble`` (e.g. 'I.q1.preamble').
    """
    sql = _segment_select_sql(
        "WHERE s.locator_path = (%(qpath)s || '.preamble')::ltree AND s.element_type = 'preamble'"
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, {"qpath": question_path})
        row = cur.fetchone()
    return dict(row) if row else None


def get_questions_by_status(
    conn: psycopg2.extensions.connection,
    status: str,
) -> list[dict]:
    """Return question paths that have at least one segment with the given status.

    Each dict: ``{question_path, segment_count, reviewed_count}``.
    Ordered by locator_path so pars appear in natural document order.
    ``status`` should be one of 'translated', 'needs_human', 'pending'.
    """
    sql = """
        SELECT
            ltree2text(subpath(s.locator_path, 0, 2)) AS question_path,
            subpath(s.locator_path, 0, 2)             AS _sort_key,
            COUNT(*)                                   AS segment_count,
            COUNT(sr.segment_id)                       AS reviewed_count
        FROM segment s
        LEFT JOIN segment_review sr ON sr.segment_id = s.segment_id
        WHERE s.translation_status = %s
        GROUP BY question_path, _sort_key
        ORDER BY _sort_key
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (status,))
        return [dict(row) for row in cur.fetchall()]


def review_segment(
    conn: psycopg2.extensions.connection,
    segment_id: int,
    action: str,
    *,
    expected_version: int,
    reviewer_email: str,
    text: str | None = None,
    note: str | None = None,
) -> tuple[str, int | None]:
    """Create or update the human review for a segment.

    ``action`` must be one of: ``save``, ``accept``, ``note``, ``reset``.
    ``expected_version`` is the optimistic-lock token the caller last read
    (0 means no review row existed when the caller loaded the segment).

    Returns ``("ok", new_version)``, ``("conflict", None)``, or ``("notfound", None)``.
    Does NOT commit — caller's ``get_conn()`` handles the commit.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM segment WHERE segment_id = %s", (segment_id,))
        if cur.fetchone() is None:
            return ("notfound", None)

        if action == "reset":
            cur.execute(
                "DELETE FROM segment_review WHERE segment_id = %s AND human_version = %s",
                (segment_id, expected_version),
            )
            if cur.rowcount == 0:
                cur.execute("SELECT 1 FROM segment_review WHERE segment_id = %s", (segment_id,))
                if cur.fetchone() is not None:
                    return ("conflict", None)
            human_src_id = source_id(conn, "human")
            cur.execute(
                "DELETE FROM segment_text WHERE segment_id = %s AND lang = 'sk' AND source_id = %s",
                (segment_id, human_src_id),
            )
            return ("ok", 0)

        # Build the ON CONFLICT SET clause: include human_note only for the "note" action.
        if action == "note":
            note_set_clause = "human_note = EXCLUDED.human_note,"
            insert_note = note
        else:
            note_set_clause = ""
            insert_note = None  # preserve any existing note on save/accept

        upsert_sql = f"""
            INSERT INTO segment_review
                (segment_id, human_reviewed_by, human_reviewed_at, human_note, human_version)
            VALUES (%s, %s, now(), %s, 1)
            ON CONFLICT (segment_id) DO UPDATE
               SET human_reviewed_by = EXCLUDED.human_reviewed_by,
                   human_reviewed_at = EXCLUDED.human_reviewed_at,
                   {note_set_clause}
                   human_version = segment_review.human_version + 1
               WHERE segment_review.human_version = %s
            RETURNING human_version
        """
        cur.execute(upsert_sql, (segment_id, reviewer_email, insert_note, expected_version))
        row = cur.fetchone()
        if row is None:
            return ("conflict", None)

        new_version: int = row[0]

        if action == "save":
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

        return ("ok", new_version)


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
