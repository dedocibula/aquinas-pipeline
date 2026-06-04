"""Glossary DB read/write operations.

Read interface (used by M2 resolution loop):
  _load_glossary(conn)       — approved Krystal terms + senses
  _load_segments(conn, wid)  — body segments with la/cs/en text

Write interface (M3 stubs — implemented in M3):
  update_sense_status(conn, sense_id, status)        — approve/reject a proposed sense
  bump_sense_version(conn, sense_id)                 — increment version, marks usages stale
  write_human_rendering(conn, sense_id, sk_text, src_id) — persist reviewer-confirmed Slovak
"""

from __future__ import annotations

import psycopg2.extras

# Element types to run the resolver on (skip title/preamble segments).
# Duplicated from resolution.py to avoid a circular import.
_BODY_TYPES = {"arg", "sed_contra", "respondeo", "reply"}


def _load_glossary(conn) -> tuple[list[dict], list[dict]]:
    """Return (multiword_terms, singleword_terms) sorted for deterministic processing.

    Each term dict: {term_id, latin_lemma, is_multiword, senses: [...]}
    Each sense dict: {sense_id, context_label, cs_lemma, en_cue, sk_content, version}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Only load 'approved' senses into the Krystal lookup.
        # 'proposed' senses belong to gap terms and must continue to be resolved
        # via gap methods (bahounek_derived etc.) on every run, not promoted to
        # krystal_single just because they were created in a previous run.
        cur.execute("""
            SELECT gt.term_id, gt.latin_lemma, gt.is_multiword,
                   gs.sense_id, gs.context_label, gs.version,
                   max(sr_cs.lemma)   FILTER (WHERE sr_cs.lang = 'cs') AS cs_lemma,
                   max(sr_cs.content) FILTER (WHERE sr_cs.lang = 'cs') AS cs_content,
                   max(sr_en.content) FILTER (WHERE sr_en.lang = 'en') AS en_cue,
                   max(sr_sk.content) FILTER (WHERE sr_sk.lang = 'sk') AS sk_content
            FROM glossary_term gt
            JOIN glossary_sense gs USING (term_id)
            LEFT JOIN sense_rendering sr_cs ON sr_cs.sense_id = gs.sense_id AND sr_cs.lang = 'cs'
            LEFT JOIN sense_rendering sr_en ON sr_en.sense_id = gs.sense_id AND sr_en.lang = 'en'
            LEFT JOIN sense_rendering sr_sk ON sr_sk.sense_id = gs.sense_id AND sr_sk.lang = 'sk'
            WHERE gs.status = 'approved'
            GROUP BY gt.term_id, gt.latin_lemma, gt.is_multiword,
                     gs.sense_id, gs.context_label, gs.version
            ORDER BY gt.latin_lemma, gs.sense_id
        """)
        rows = cur.fetchall()

    terms: dict[int, dict] = {}
    for row in rows:
        tid = row["term_id"]
        if tid not in terms:
            terms[tid] = {
                "term_id": tid,
                "latin_lemma": row["latin_lemma"],
                "is_multiword": row["is_multiword"],
                "senses": [],
            }
        terms[tid]["senses"].append({
            "sense_id": row["sense_id"],
            "context_label": row["context_label"],
            "version": row["version"],
            "cs_lemma": row["cs_lemma"],
            "cs_content": row["cs_content"],
            "en_cue": row["en_cue"],
            "sk_content": row["sk_content"],
        })

    all_terms = sorted(terms.values(), key=lambda t: t["latin_lemma"])
    multiword = [t for t in all_terms if t["is_multiword"]]
    singleword = [t for t in all_terms if not t["is_multiword"]]
    return multiword, singleword


def _load_segments(conn, wid: int) -> list[dict]:
    """Return body segments with la/cs/en text for the given work, sorted by locator."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT s.segment_id, s.locator_path::text AS locator_path, s.element_type,
                   max(t.content) FILTER (WHERE t.lang = 'la') AS latin,
                   max(t.content) FILTER (WHERE t.lang = 'cs') AS czech,
                   max(t.content) FILTER (WHERE t.lang = 'en') AS english
            FROM segment s
            LEFT JOIN segment_text t USING (segment_id)
            WHERE s.work_id = %s
              AND s.element_type = ANY(%s)
            GROUP BY s.segment_id, s.locator_path, s.element_type
            ORDER BY s.locator_path
        """, (wid, list(_BODY_TYPES)))
        return cur.fetchall()


# ── M3 write stubs ────────────────────────────────────────────────────────────


def update_sense_status(conn, sense_id: int, status: str) -> None:
    """Set glossary_sense.status for a reviewer approval or rejection.

    Valid statuses: 'approved', 'rejected'. 'proposed' is the initial state set
    by the M2 gap-term preseed and must not be set here.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE glossary_sense SET status = %s WHERE sense_id = %s",
            (status, sense_id),
        )


def bump_sense_version(conn, sense_id: int) -> int:
    """Increment glossary_sense.version and return the new value.

    M4's stale-segment query uses sense_version_used < current version to find
    segments that need re-translation after a reviewer correction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE glossary_sense SET version = version + 1 "
            "WHERE sense_id = %s RETURNING version",
            (sense_id,),
        )
        return cur.fetchone()[0]


def write_human_rendering(conn, sense_id: int, sk_text: str, src_id: int) -> None:
    """Persist a reviewer-confirmed Slovak rendering.

    Writes to sense_rendering(lang='sk', source_id=src_id) with the human-confirmed
    text. The model-proposed rendering (source_id=model) is preserved alongside it
    for audit.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sense_rendering (sense_id, lang, content, source_id)
            VALUES (%s, 'sk', %s, %s)
            ON CONFLICT (sense_id, lang, source_id) DO UPDATE
                SET content = EXCLUDED.content
            """,
            (sense_id, sk_text, src_id),
        )
