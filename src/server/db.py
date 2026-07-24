"""
DB query helpers for the Flask preview server.

All functions accept a psycopg2 connection.
They are intentionally separate from src/common/db.py — that module manages
connection lifecycle; this module owns the server-specific SQL.
"""

from __future__ import annotations

import psycopg2
import psycopg2.extras

from review.glossary_apply import (
    apply_add_term,
    apply_remove_here,
    apply_rendering_change,
    apply_retire_sense,
    apply_sense_here,
)
from storage.db import source_id
from storage.models import (
    ActivityEntry,
    Comment,
    CommentCount,
    CommentThread,
    DigestItem,
    UserDigest,
)
from storage.repositories import GlossaryRepository, ProposalRepository

# Editor glossary-proposal kinds (glossary_proposal.kind CHECK constraint,
# migration 013). Values are locked schema truth — do not change them; these
# are just intuitive Python-side names for the five DB strings.
PROPOSAL_KIND_CHANGE_EVERYWHERE = "rendering"
PROPOSAL_KIND_WRONG_SENSE_HERE = "sense_here"
PROPOSAL_KIND_REMOVE_HERE = "remove_here"
PROPOSAL_KIND_RETIRE_EVERYWHERE = "retire_sense"
PROPOSAL_KIND_ADD_TERM = "add_term"

# ---------------------------------------------------------------------------
# Shared segment SELECT helper
# ---------------------------------------------------------------------------


def _segment_select_sql(where_clause: str) -> str:
    """Return the full segment SELECT+FROM+JOIN block with a caller-supplied WHERE clause.

    Columns returned: segment_id, locator_path, element_type, reply_to,
    translation_status, reviewer_notes, latin, czech, english,
    slovak_model, slovak_polish, slovak_human, human_note, human_reviewed_by, human_version.

    Display precedence: human → polish → model.
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
            sk_polish.content  AS slovak_polish,
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
            SELECT st_p.content
            FROM segment_text st_p
            JOIN source src_p ON src_p.source_id = st_p.source_id
            WHERE st_p.segment_id = s.segment_id
              AND st_p.lang = 'sk'
              AND src_p.code = 'polish'
            LIMIT 1
        ) sk_polish ON true
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
    Ordered by pars_order.ordinal then numeric question number.
    """
    sql = """
        WITH q AS (
            SELECT DISTINCT
                subpath(s.locator_path, 0, 2)                                        AS path,
                COALESCE(po.ordinal, 9999)                                           AS pars_ord,
                (regexp_match(subpath(s.locator_path, 1, 1)::text, '\\d+'))[1]::int AS q_num
            FROM segment s
            LEFT JOIN pars_order po
                ON  po.pars_label = subpath(s.locator_path, 0, 1)::text
                AND po.work_id    = s.work_id
        )
        SELECT ltree2text(path) AS question_path
        FROM q
        ORDER BY pars_ord, q_num
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
        ORDER BY (regexp_match(subpath(s.locator_path, 0, 3)::text, '\\d+$'))[1]::int
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
        ORDER BY
            CASE s.element_type
                WHEN 'article_title' THEN 0
                WHEN 'arg'           THEN 1
                WHEN 'sed_contra'    THEN 2
                WHEN 'respondeo'     THEN 3
                WHEN 'reply'         THEN 4
                ELSE                      5
            END,
            (regexp_match(s.locator_path::text, '\\d+$'))[1]::int NULLS LAST
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
        WITH articles AS (
            SELECT DISTINCT
                subpath(s.locator_path, 0, 3)                                           AS ap,
                COALESCE(po.ordinal, 9999)                                              AS pars_ord,
                (regexp_match(subpath(s.locator_path, 1, 1)::text, '\\d+'))[1]::int    AS q_num,
                (regexp_match(subpath(s.locator_path, 2, 1)::text, '\\d+'))[1]::int    AS a_num
            FROM segment s
            LEFT JOIN pars_order po
                ON  po.pars_label = subpath(s.locator_path, 0, 1)::text
                AND po.work_id    = s.work_id
            WHERE nlevel(s.locator_path) >= 3 AND s.element_type != 'preamble'
        ),
        nav AS (
            SELECT
                ltree2text(ap)                                                  AS ap,
                ltree2text(lag(ap)  OVER (ORDER BY pars_ord, q_num, a_num))    AS prev,
                ltree2text(lead(ap) OVER (ORDER BY pars_ord, q_num, a_num))    AS next
            FROM articles
        )
        SELECT prev, next FROM nav WHERE ap = %s
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
    ``sense_id``, ``latin_lemma``, ``slovak``, ``context_label``.
    Missing segment_ids are not included (caller treats absence as empty list).
    A 'rejected' term_usage row (D10 tombstone) is excluded even if the sense
    itself is still approved.
    """
    if not segment_ids:
        return {}

    placeholders = ", ".join(["%s"] * len(segment_ids))
    sql = f"""
        SELECT DISTINCT ON (tu.segment_id, gs.sense_id)
            tu.segment_id,
            gs.sense_id,
            gt.latin_lemma,
            sr.content        AS slovak,
            gs.context_label
        FROM term_usage tu
        JOIN glossary_sense  gs ON gs.sense_id  = tu.sense_id
        JOIN glossary_term   gt ON gt.term_id   = gs.term_id
        JOIN sense_rendering sr ON sr.sense_id  = gs.sense_id
        JOIN source           s ON s.source_id  = sr.source_id
        WHERE tu.segment_id IN ({placeholders})
          AND tu.status <> 'rejected'
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
                    "sense_id": row["sense_id"],
                    "latin_lemma": row["latin_lemma"],
                    "slovak": row["slovak"],
                    "context_label": row["context_label"],
                }
            )
    return result


def get_term_senses(conn: psycopg2.extensions.connection, sense_id: int) -> dict | None:
    """Return the term owning ``sense_id`` and all of that term's senses.

    Powers the "wrong sense here" dropdown (Stage 3). Includes senses of any
    status (proposed/approved/retired) — the editor must be able to name a
    not-yet-approved sense as the correct one; ``apply_sense_here`` approves
    it on admin approval. Each sense's ``slovak`` is its winning rendering
    (authority_rank ASC), same join shape as ``get_segment_constraints``, or
    None if it has no sk rendering yet. Returns None if ``sense_id`` doesn't
    exist.
    """
    sql = """
        SELECT gt.term_id, gt.latin_lemma
        FROM glossary_sense gs
        JOIN glossary_term  gt ON gt.term_id = gs.term_id
        WHERE gs.sense_id = %s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (sense_id,))
        term = cur.fetchone()
        if term is None:
            return None

        cur.execute(
            """
            SELECT DISTINCT ON (gs.sense_id)
                gs.sense_id,
                gs.context_label,
                gs.status,
                sr.content AS slovak
            FROM glossary_sense gs
            LEFT JOIN sense_rendering sr ON sr.sense_id = gs.sense_id AND sr.lang = 'sk'
            LEFT JOIN source           s ON s.source_id = sr.source_id
            WHERE gs.term_id = %s
            ORDER BY gs.sense_id, s.authority_rank
            """,
            (term["term_id"],),
        )
        senses = [
            {
                "sense_id": row["sense_id"],
                "context_label": row["context_label"],
                "status": row["status"],
                "slovak": row["slovak"],
            }
            for row in cur.fetchall()
        ]

    return {"term_id": term["term_id"], "latin_lemma": term["latin_lemma"], "senses": senses}


def get_pending_proposal_counts(
    conn: psycopg2.extensions.connection, sense_ids: list[int]
) -> dict[int, int]:
    """Return {sense_id: pending glossary_proposal count} for the given senses.

    Powers the panel's "proposal pending" badge (Stage 3). Thin wrapper around
    ``ProposalRepository.pending_by_sense`` so the Flask route layer only ever
    imports from ``server.db``, matching this file's existing convention.
    """
    if not sense_ids:
        return {}
    return ProposalRepository(conn).pending_by_sense(sense_ids)


def get_pending_proposal_count(conn: psycopg2.extensions.connection) -> int:
    """Total pending glossary_proposal rows, for the index page's admin badge."""
    return len(ProposalRepository(conn).list_pending())


_SENSE_WIDE_KINDS = (PROPOSAL_KIND_CHANGE_EVERYWHERE, PROPOSAL_KIND_RETIRE_EVERYWHERE)
_PER_SEGMENT_KINDS = (PROPOSAL_KIND_WRONG_SENSE_HERE, PROPOSAL_KIND_REMOVE_HERE)


def _sense_blast_radius(conn: psycopg2.extensions.connection, sense_id: int) -> dict:
    """Segments locked by ``sense_id``, split by translation_status, plus the
    marginal restage count — segments that are not already stale.

    ``reviewed`` piggybacks on the same guard-protected ``segment_review`` join
    as the rest of the server (free — no extra scan of a big table).
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT s.translation_status, count(DISTINCT tu.segment_id) AS n
            FROM term_usage tu
            JOIN segment s ON s.segment_id = tu.segment_id
            WHERE tu.sense_id = %s AND tu.status <> 'rejected'
            GROUP BY s.translation_status
            """,
            (sense_id,),
        )
        by_status = {row["translation_status"]: row["n"] for row in cur.fetchall()}

        cur.execute(
            """
            SELECT count(DISTINCT tu.segment_id)
            FROM term_usage tu
            JOIN segment_review sr ON sr.segment_id = tu.segment_id
            WHERE tu.sense_id = %s AND tu.status <> 'rejected'
            """,
            (sense_id,),
        )
        reviewed = cur.fetchone()["count"]

        cur.execute(
            """
            SELECT count(DISTINCT tu.segment_id)
            FROM term_usage tu
            WHERE tu.sense_id = %s AND tu.status <> 'rejected'
              AND NOT EXISTS (
                  SELECT 1 FROM term_usage tu2
                  JOIN glossary_sense gs2 ON gs2.sense_id = tu2.sense_id
                  WHERE tu2.segment_id = tu.segment_id
                    AND tu2.sense_version_used < gs2.version
              )
            """,
            (sense_id,),
        )
        not_already_stale = cur.fetchone()["count"]

    total = sum(by_status.values())
    return {
        "translated": by_status.get("translated", 0),
        "needs_human": by_status.get("needs_human", 0),
        "pending": by_status.get("pending", 0),
        "reviewed": reviewed,
        "total": total,
        "marginal": not_already_stale,
    }


def _origin_locator(conn: psycopg2.extensions.connection, segment_id: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT locator_path::text FROM segment WHERE segment_id = %s", (segment_id,)
        )
        row = cur.fetchone()
    return row[0] if row else None


def get_cost_per_segment(conn: psycopg2.extensions.connection) -> float:
    """Return an estimated $/segment for the blast-radius cost preview.

    Prefers the actual last translation run's realised cost; falls back to the
    token-based estimate in ``ingest.coverage_report`` (imported, not copied,
    per the plan) when no run has completed yet or its cost is zero.
    """
    from ingest.coverage_report import _AVG_SEGMENT_TOKENS, _COST_PER_1K_INPUT
    from storage.repositories import RunRepository

    last_run = RunRepository(conn).last_run()
    if last_run and last_run.get("total_segments") and last_run.get("total_cost_usd"):
        cost = float(last_run["total_cost_usd"])
        segments = int(last_run["total_segments"])
        if cost > 0 and segments > 0:
            return cost / segments

    return (_AVG_SEGMENT_TOKENS / 1000) * _COST_PER_1K_INPUT


def _enrich_proposal_display(conn: psycopg2.extensions.connection, row: dict) -> dict:
    """Attach term/sense display fields shared by the pending queue and the
    decided-proposal audit trail.

    Adds ``context_label`` — the acted-on sense's context_label (None for
    add_term); ``proposed_context_label`` / ``proposed_slovak`` — for
    sense_here with a chosen ``proposed_sense_id`` (None for the free-text
    record-only path); ``origin_locator`` — the origin segment's locator
    path, only for per-segment kinds (``sense_here``, ``remove_here``).
    """
    kind = row["kind"]
    sense_id = row["sense_id"]

    row["context_label"] = None
    row["proposed_context_label"] = None
    row["proposed_slovak"] = None
    row["origin_locator"] = None

    if sense_id is not None:
        term = get_term_senses(conn, sense_id)
        if term is not None:
            sense_row = next(
                (s for s in term["senses"] if s["sense_id"] == sense_id), None
            )
            if sense_row is not None:
                row["context_label"] = sense_row["context_label"]

            if row["proposed_sense_id"] is not None:
                proposed_row = next(
                    (
                        s
                        for s in term["senses"]
                        if s["sense_id"] == row["proposed_sense_id"]
                    ),
                    None,
                )
                if proposed_row is not None:
                    row["proposed_context_label"] = proposed_row["context_label"]
                    row["proposed_slovak"] = proposed_row["slovak"]

    if kind in _PER_SEGMENT_KINDS and row["origin_segment_id"] is not None:
        row["origin_locator"] = _origin_locator(conn, row["origin_segment_id"])

    return row


def get_pending_proposals_view(conn: psycopg2.extensions.connection) -> list[dict]:
    """Return every pending proposal enriched for the admin queue (Stage 4).

    Each dict is the raw ``glossary_proposal`` row plus the shared display
    fields from ``_enrich_proposal_display``, plus:
      - ``live_current_sk`` / ``drift`` — the sense's *current* winning rendering
        vs. the snapshot taken at propose time; drift=True means the glossary
        moved since the editor proposed (admin should re-check before approving).
      - ``blast_radius`` — dict from ``_sense_blast_radius``, only for sense-wide
        kinds (``rendering``, ``retire_sense``).
    """
    glossary = GlossaryRepository(conn)
    rows = ProposalRepository(conn).list_pending()

    for row in rows:
        _enrich_proposal_display(conn, row)
        kind = row["kind"]
        sense_id = row["sense_id"]

        row["live_current_sk"] = None
        row["drift"] = False
        row["blast_radius"] = None

        if sense_id is not None:
            current = glossary.get_current_sense(sense_id)
            if current is not None:
                live_sk = glossary.get_sk_rendering_content(sense_id)
                row["live_current_sk"] = live_sk
                row["drift"] = live_sk != row["current_sk"]

        if kind in _SENSE_WIDE_KINDS and sense_id is not None:
            row["blast_radius"] = _sense_blast_radius(conn, sense_id)

    return rows


def get_decided_proposals_view(
    conn: psycopg2.extensions.connection, limit: int = 200
) -> list[dict]:
    """Return the most recent decided proposals for the admin audit trail.

    Same shared display fields as ``get_pending_proposals_view`` (minus
    drift/blast-radius, which only matter before a decision is made) so the
    history table can reuse the same rendering as the live queue.
    """
    rows = ProposalRepository(conn).list_decided(limit=limit)
    for row in rows:
        _enrich_proposal_display(conn, row)
    return rows


class ProposalRaceError(Exception):
    """Raised when a proposal was decided by someone else between read and decide.

    Signals the caller's ``with get_conn()`` block to roll back any writes the
    dispatched glossary_apply service already made, then respond 409.
    """


def approve_proposal(
    conn: psycopg2.extensions.connection,
    proposal_id: int,
    admin_email: str,
    decision_note: str | None,
) -> tuple[str, dict | None]:
    """Apply an admin-approved glossary_proposal and mark it approved (Stage 4).

    Dispatches to the ``review.glossary_apply`` service matching the
    proposal's kind, then marks the row approved, then auto-supersedes
    competing pending sense-wide proposals on the same sense (D5). Everything
    runs in the caller's transaction — ``get_conn`` commits on clean exit, so
    a failure at any point (including the race below) rolls back the whole
    thing, including any glossary/term_usage write the service already made.

    Returns ``(status, result)``:
      "ok"          -> result is the service's result dict. A ``sense_here``
                       proposal with no ``proposed_sense_id`` (the free-text,
                       record-only gold-label path, D9) calls no service and
                       returns ``{"acknowledged": True}``.
      "not_found"   -> proposal_id doesn't exist; result is None.
      "not_pending" -> already decided (by an earlier, already-committed
                       request); result is None. No writes have happened yet
                       at this point, so nothing to roll back.

    Raises ``ValueError`` (a glossary_apply service's message — including
    "term_exists" for add_term, or a stale-proposal guard) or
    ``ProposalRaceError`` (the ``decide()`` UPDATE below affected zero rows — a
    concurrent admin request won the race). Both must propagate out of the
    caller's ``with get_conn()`` block so the transaction rolls back; the
    caller catches them outside that block to build the 409 response.
    """
    repo = ProposalRepository(conn)
    proposal = repo.get(proposal_id)
    if proposal is None:
        return ("not_found", None)
    if proposal["status"] != "pending":
        return ("not_pending", None)

    kind = proposal["kind"]
    sense_id = proposal["sense_id"]

    if kind == PROPOSAL_KIND_CHANGE_EVERYWHERE:
        result = apply_rendering_change(conn, sense_id, proposal["proposed_sk"])
    elif kind == PROPOSAL_KIND_WRONG_SENSE_HERE:
        if proposal["proposed_sense_id"] is not None:
            result = apply_sense_here(
                conn,
                proposal["origin_segment_id"],
                sense_id,
                proposal["proposed_sense_id"],
            )
        else:
            result = {"acknowledged": True}
    elif kind == PROPOSAL_KIND_REMOVE_HERE:
        result = apply_remove_here(conn, proposal["origin_segment_id"], sense_id)
    elif kind == PROPOSAL_KIND_RETIRE_EVERYWHERE:
        result = apply_retire_sense(conn, sense_id)
    else:  # add_term
        result = apply_add_term(
            conn, proposal["latin_lemma"], proposal["proposed_sk"], proposal["note"]
        )

    if not repo.decide(proposal_id, "approved", admin_email, decision_note):
        raise ProposalRaceError(f"proposal {proposal_id} was already decided")

    if kind in _SENSE_WIDE_KINDS:
        repo.supersede_sense_wide_siblings(sense_id, proposal_id, admin_email)

    return ("ok", result)


def reject_proposal(
    conn: psycopg2.extensions.connection,
    proposal_id: int,
    admin_email: str,
    decision_note: str | None,
) -> str:
    """Reject a pending proposal. Returns "ok" / "not_found" / "not_pending"."""
    repo = ProposalRepository(conn)
    proposal = repo.get(proposal_id)
    if proposal is None:
        return "not_found"
    if proposal["status"] != "pending":
        return "not_pending"
    if not repo.decide(proposal_id, "rejected", admin_email, decision_note):
        return "not_pending"
    return "ok"


def reopen_proposal(
    conn: psycopg2.extensions.connection, proposal_id: int, admin_email: str
) -> tuple[str, int | None]:
    """Re-open a rejected proposal for reconsideration.

    The rejected row is never mutated — reopening only clones its content into
    a brand-new pending row (D5's dedup applies to it like any other propose
    call, keyed on the *original* proposer, not ``admin_email`` — see
    ``ProposalRepository.clone_as_pending``), so the original rejection stays
    permanently visible in the audit trail. Only ``rejected`` proposals may be
    reopened: an ``approved`` one already changed the glossary, and
    re-deciding it here would not undo that; a ``superseded`` one lost to a
    sibling sense-wide decision, not a standalone rejection.

    Fails loudly (raises ``ValueError``) if the proposal's target no longer
    exists — a sense retired or a segment removed since the original rejection
    would otherwise silently re-enter the live queue pointing at nothing.

    Returns ``(status, new_proposal_id)``:
      "ok"           -> new_proposal_id is the freshly created pending row.
      "not_found"    -> proposal_id doesn't exist; new_proposal_id is None.
      "not_rejected" -> proposal exists but isn't rejected; new_proposal_id is None.
    """
    repo = ProposalRepository(conn)
    proposal = repo.get(proposal_id)
    if proposal is None:
        return ("not_found", None)
    if proposal["status"] != "rejected":
        return ("not_rejected", None)

    sense_id = proposal["sense_id"]
    if sense_id is not None and GlossaryRepository(conn).get_current_sense(sense_id) is None:
        raise ValueError(f"sense {sense_id} no longer exists")
    origin_segment_id = proposal["origin_segment_id"]
    if origin_segment_id is not None and not segment_exists(conn, origin_segment_id):
        raise ValueError(f"segment {origin_segment_id} no longer exists")

    new_id = repo.clone_as_pending(proposal_id)
    return ("ok", new_id)


def segment_has_locked_sense(
    conn: psycopg2.extensions.connection, segment_id: int, sense_id: int
) -> bool:
    """True if ``sense_id`` is one of ``segment_id``'s locked terms (D8).

    Mirrors the constraint shape of ``GlossaryRepository.locked_terms`` /
    ``get_segment_constraints``: a non-rejected term_usage row for an approved
    sense with a Slovak rendering. Guards the propose endpoints against a
    client submitting sense_here/remove_here for a (segment, sense) pair that
    was never actually rendered as a locked term in that segment's panel.
    """
    sql = """
        SELECT 1
        FROM term_usage tu
        JOIN glossary_sense gs  ON gs.sense_id = tu.sense_id AND gs.status = 'approved'
        JOIN sense_rendering sr ON sr.sense_id = gs.sense_id AND sr.lang = 'sk'
        WHERE tu.segment_id = %s AND tu.sense_id = %s AND tu.status <> 'rejected'
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (segment_id, sense_id))
        return cur.fetchone() is not None


def propose_sense_change(
    conn: psycopg2.extensions.connection,
    sense_id: int,
    kind: str,
    *,
    proposed_sk: str | None,
    proposed_sense_id: int | None,
    note: str | None,
    origin_segment_id: int | None,
    proposed_by: str,
) -> tuple[str, int | None]:
    """Validate and record an editor's proposed sense-targeted glossary change.

    ``kind`` is one of the PROPOSAL_KIND_CHANGE_EVERYWHERE / WRONG_SENSE_HERE /
    REMOVE_HERE / RETIRE_EVERYWHERE constants (add_term uses propose_add_term
    instead). ``proposed_sk`` should already be stripped by the caller.

    Does NOT commit — caller's ``get_conn()`` handles the commit.

    Returns ``(status, proposal_id)``:
      "ok"               -> proposal recorded, proposal_id set
      "not_found"        -> sense_id doesn't exist
      "no_change"        -> proposed_sk/proposed_sense_id equals current state
      "wrong_term"       -> proposed_sense_id isn't a sense of the same term
      "not_locked_here"  -> origin_segment_id doesn't actually lock sense_id (D8)
      "proposed_sk_required" -> rendering kind needs a non-empty proposed_sk
      "missing_target"   -> sense_here needs proposed_sense_id or proposed_sk
    proposal_id is None unless status == "ok".
    """
    glossary = GlossaryRepository(conn)
    current_sense = glossary.get_current_sense(sense_id)
    if current_sense is None:
        return ("not_found", None)

    term = get_term_senses(conn, sense_id)
    current_sk = glossary.get_sk_rendering_content(sense_id)

    resolved_proposed_sk: str | None = None
    resolved_proposed_sense_id: int | None = None

    if kind == PROPOSAL_KIND_CHANGE_EVERYWHERE:
        if not proposed_sk:
            return ("proposed_sk_required", None)
        if proposed_sk == (current_sk or ""):
            return ("no_change", None)
        resolved_proposed_sk = proposed_sk

    elif kind == PROPOSAL_KIND_WRONG_SENSE_HERE:
        if not segment_has_locked_sense(conn, origin_segment_id, sense_id):
            return ("not_locked_here", None)
        if proposed_sense_id is not None:
            if proposed_sense_id == sense_id:
                return ("no_change", None)
            if not any(s["sense_id"] == proposed_sense_id for s in term["senses"]):
                return ("wrong_term", None)
            resolved_proposed_sense_id = proposed_sense_id
        elif proposed_sk:
            if proposed_sk == (current_sk or ""):
                return ("no_change", None)
            resolved_proposed_sk = proposed_sk
        else:
            return ("missing_target", None)

    elif kind == PROPOSAL_KIND_REMOVE_HERE:
        if not segment_has_locked_sense(conn, origin_segment_id, sense_id):
            return ("not_locked_here", None)

    else:  # PROPOSAL_KIND_RETIRE_EVERYWHERE
        pass

    proposal_id = ProposalRepository(conn).create_or_update_pending(
        kind=kind,
        sense_id=sense_id,
        proposed_sense_id=resolved_proposed_sense_id,
        latin_lemma=term["latin_lemma"],
        current_sk=current_sk,
        proposed_sk=resolved_proposed_sk,
        note=note,
        origin_segment_id=origin_segment_id,
        proposed_by=proposed_by,
    )
    return ("ok", proposal_id)


def propose_add_term(
    conn: psycopg2.extensions.connection,
    *,
    latin_lemma: str,
    proposed_sk: str,
    note: str | None,
    origin_segment_id: int | None,
    proposed_by: str,
) -> tuple[str, int | None]:
    """Record an editor's suggestion for a missing glossary term (kind=add_term).

    Does NOT commit — caller's ``get_conn()`` handles the commit.

    Returns ``(status, proposal_id)``:
      "ok"          -> proposal recorded, proposal_id set
      "term_exists" -> latin_lemma is already a glossary term
    """
    if GlossaryRepository(conn).find_term_by_lemma(latin_lemma) is not None:
        return ("term_exists", None)

    proposal_id = ProposalRepository(conn).create_or_update_pending(
        kind=PROPOSAL_KIND_ADD_TERM,
        sense_id=None,
        proposed_sense_id=None,
        latin_lemma=latin_lemma,
        current_sk=None,
        proposed_sk=proposed_sk,
        note=note,
        origin_segment_id=origin_segment_id,
        proposed_by=proposed_by,
    )
    return ("ok", proposal_id)


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
        WITH q AS (
            SELECT
                ltree2text(subpath(s.locator_path, 0, 2))                           AS question_path,
                COUNT(*)                                                             AS segment_count,
                COUNT(sr.segment_id)                                                AS reviewed_count,
                COALESCE(MAX(po.ordinal), 9999)                                     AS pars_ord,
                (regexp_match(subpath(s.locator_path, 1, 1)::text, '\\d+'))[1]::int AS q_num
            FROM segment s
            LEFT JOIN segment_review sr ON sr.segment_id = s.segment_id
            LEFT JOIN pars_order po
                ON  po.pars_label = subpath(s.locator_path, 0, 1)::text
                AND po.work_id    = s.work_id
            WHERE s.translation_status = %s
            GROUP BY question_path, subpath(s.locator_path, 1, 1)
        )
        SELECT question_path, segment_count, reviewed_count
        FROM q
        ORDER BY pars_ord, q_num
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
                row_exists = cur.fetchone() is not None
                # Conflict if wrong version (row still there) OR row was already deleted
                # by another editor (expected_version > 0 but row is gone).
                if row_exists or expected_version != 0:
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


# ---------------------------------------------------------------------------
# Comment threads (editor-internal, per segment)
# ---------------------------------------------------------------------------

_COMMENT_COLUMNS = (
    "comment_id, segment_id, author, body, created_at, resolved, resolved_by, resolved_at"
)


def segment_exists(conn: psycopg2.extensions.connection, segment_id: int) -> bool:
    """True if segment_id is a real segment.

    Callers that insert into segment_comment / comment_thread_state (both FK'd to
    segment) must check this first — otherwise an unknown segment_id surfaces as an
    unhandled IntegrityError (500) instead of a clean 404, unlike review_segment's
    explicit notfound check.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM segment WHERE segment_id = %s", (segment_id,))
        return cur.fetchone() is not None


def list_comments(conn: psycopg2.extensions.connection, segment_id: int) -> CommentThread:
    """Return the full comment thread for a segment, oldest first."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {_COMMENT_COLUMNS}
            FROM segment_comment
            WHERE segment_id = %s
            ORDER BY created_at ASC
            """,
            (segment_id,),
        )
        comments = [Comment(**dict(row)) for row in cur.fetchall()]
    open_count = sum(1 for c in comments if not c.resolved)
    resolved = bool(comments) and open_count == 0
    return CommentThread(comments=comments, resolved=resolved, open_count=open_count)


def add_comment(conn: psycopg2.extensions.connection, segment_id: int, author: str, body: str) -> Comment:
    """Insert a new comment. A new comment on a resolved thread implicitly reopens it."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            INSERT INTO segment_comment (segment_id, author, body)
            VALUES (%s, %s, %s)
            RETURNING {_COMMENT_COLUMNS}
            """,
            (segment_id, author, body),
        )
        row = cur.fetchone()
    return Comment(**dict(row))


def resolve_thread(conn: psycopg2.extensions.connection, segment_id: int, resolver_email: str) -> int:
    """Mark every open comment in the thread as resolved. Returns the number of rows flipped."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE segment_comment
               SET resolved = true, resolved_by = %s, resolved_at = now()
             WHERE segment_id = %s AND resolved = false
            """,
            (resolver_email, segment_id),
        )
        return cur.rowcount


def reopen_thread(conn: psycopg2.extensions.connection, segment_id: int) -> int:
    """Clear resolved state on every comment in the thread. Returns rows flipped."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE segment_comment
               SET resolved = false, resolved_by = NULL, resolved_at = NULL
             WHERE segment_id = %s AND resolved = true
            """,
            (segment_id,),
        )
        return cur.rowcount


def delete_comment(conn: psycopg2.extensions.connection, comment_id: int, requester_email: str) -> str:
    """Delete a comment iff the requester is its author.

    Returns ``"ok"``, ``"notfound"``, or ``"forbidden"``.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT author FROM segment_comment WHERE comment_id = %s", (comment_id,))
        row = cur.fetchone()
        if row is None:
            return "notfound"
        if row[0] != requester_email:
            return "forbidden"
        cur.execute("DELETE FROM segment_comment WHERE comment_id = %s", (comment_id,))
    return "ok"


def mark_thread_read(conn: psycopg2.extensions.connection, segment_id: int, user_email: str) -> None:
    """Bump the viewer's read watermark for a segment's thread (clears the unread dot)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO comment_thread_state (segment_id, user_email, last_read_at)
            VALUES (%s, %s, now())
            ON CONFLICT (segment_id, user_email) DO UPDATE SET last_read_at = now()
            """,
            (segment_id, user_email),
        )


def get_comment_counts(
    conn: psycopg2.extensions.connection, segment_ids: list[int], viewer_email: str
) -> dict[int, CommentCount]:
    """Return per-segment comment badge counts for the given segments.

    ``unread`` counts comments by other authors newer than the viewer's
    ``last_read_at`` watermark (NULL watermark means everything is unread).
    Segments with no comments are omitted from the result.
    """
    if not segment_ids:
        return {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                c.segment_id,
                count(*)                                                      AS total,
                count(*) FILTER (WHERE NOT c.resolved)                        AS open_count,
                count(*) FILTER (
                    WHERE c.author <> %s
                      AND c.created_at > COALESCE(st.last_read_at, '-infinity'::timestamptz)
                )                                                             AS unread
            FROM segment_comment c
            LEFT JOIN comment_thread_state st
                   ON st.segment_id = c.segment_id AND st.user_email = %s
            WHERE c.segment_id = ANY(%s)
            GROUP BY c.segment_id
            """,
            (viewer_email, viewer_email, segment_ids),
        )
        rows = cur.fetchall()
    return {
        row["segment_id"]: CommentCount(
            total=int(row["total"]), open_count=int(row["open_count"]), unread=int(row["unread"])
        )
        for row in rows
    }


def get_activity_feed(
    conn: psycopg2.extensions.connection, *, before: str | None = None, limit: int = 50
) -> list[ActivityEntry]:
    """Return the admin `/timeline` activity feed: reviews, comments, and run markers.

    Merges the three sources newest-first. ``before`` (an ISO timestamp) paginates
    to entries strictly older than it. Uses only existing timestamps — no new
    per-row timestamp column.
    """
    sql = """
        SELECT ts, kind, author, segment_id, locator, summary,
               translated, needs_human, cost
        FROM (
            SELECT sr.human_reviewed_at AS ts, 'review' AS kind,
                   sr.human_reviewed_by AS author, sr.segment_id,
                   s.locator_path::text AS locator,
                   (CASE WHEN sr.human_note IS NOT NULL THEN 'noted' ELSE 'reviewed' END) AS summary,
                   NULL::int AS translated, NULL::int AS needs_human, NULL::numeric AS cost
            FROM segment_review sr
            JOIN segment s ON s.segment_id = sr.segment_id

            UNION ALL

            SELECT c.created_at, 'comment', c.author, c.segment_id,
                   s.locator_path::text, left(c.body, 140),
                   NULL, NULL, NULL
            FROM segment_comment c
            JOIN segment s ON s.segment_id = c.segment_id

            UNION ALL

            SELECT COALESCE(r.finished_at, r.started_at), 'run', NULL, NULL, NULL,
                   r.flow_name, r.total_translated, r.total_needs_human, r.total_cost_usd
            FROM translation_run r
        ) feed
        WHERE (%(before)s::timestamptz IS NULL OR ts < %(before)s::timestamptz)
        ORDER BY ts DESC
        LIMIT %(limit)s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, {"before": before, "limit": limit})
        rows = cur.fetchall()
    return [
        ActivityEntry(
            ts=row["ts"],
            kind=row["kind"],
            author=row["author"],
            segment_id=row["segment_id"],
            locator=row["locator"],
            summary=row["summary"],
            translated=row["translated"],
            needs_human=row["needs_human"],
            cost=float(row["cost"]) if row["cost"] is not None else None,
        )
        for row in rows
    ]


def collect_digests(conn: psycopg2.extensions.connection) -> list[UserDigest]:
    """Return, per recipient, the unread comment replies their daily digest should cover.

    Recipients = thread participants (everyone who has commented on the segment) ∪ the
    segment's reviewer, minus each comment's own author. A comment is digest-worthy for a
    recipient when it postdates both their read and their last-notified watermark (NULL
    watermark means everything qualifies).
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            WITH participants AS (
                SELECT DISTINCT segment_id, author AS user_email FROM segment_comment
                UNION
                SELECT sr.segment_id, sr.human_reviewed_by
                FROM segment_review sr
                WHERE sr.human_reviewed_by IS NOT NULL
                  AND sr.segment_id IN (SELECT DISTINCT segment_id FROM segment_comment)
            )
            SELECT p.user_email, c.segment_id, s.locator_path::text AS locator,
                   c.author, c.created_at, c.body
            FROM participants p
            JOIN segment_comment c ON c.segment_id = p.segment_id
            JOIN segment s        ON s.segment_id = c.segment_id
            LEFT JOIN comment_thread_state st
                   ON st.segment_id = p.segment_id AND st.user_email = p.user_email
            WHERE c.author <> p.user_email
              AND c.created_at > COALESCE(GREATEST(st.last_read_at, st.last_notified_at),
                                          '-infinity'::timestamptz)
            ORDER BY p.user_email, c.created_at
            """
        )
        rows = cur.fetchall()

    digests: dict[str, list[DigestItem]] = {}
    for row in rows:
        digests.setdefault(row["user_email"], []).append(
            DigestItem(
                segment_id=row["segment_id"],
                locator=row["locator"],
                author=row["author"],
                created_at=row["created_at"],
                body=row["body"],
            )
        )
    return [UserDigest(user_email=email, items=items) for email, items in digests.items()]


def mark_thread_notified(conn: psycopg2.extensions.connection, segment_id: int, user_email: str) -> None:
    """Bump the recipient's notified watermark for a segment's thread (digest de-dupe)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO comment_thread_state (segment_id, user_email, last_notified_at)
            VALUES (%s, %s, now())
            ON CONFLICT (segment_id, user_email) DO UPDATE SET last_notified_at = now()
            """,
            (segment_id, user_email),
        )


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


def get_distinct_pars(conn: psycopg2.extensions.connection, work_id: int) -> list[str]:
    """Return sorted list of pars labels that have translated/needs_human segments.

    Queries v_segment (same surface as the export query) so only pars that
    would actually produce output are returned.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT subpath(locator_path, 0, 1)::text"
            " FROM v_segment"
            " WHERE work_id = %s"
            "   AND translation_status IN ('translated', 'needs_human')"
            " ORDER BY 1",
            (work_id,),
        )
        return [r[0] for r in cur.fetchall()]


def is_editor(conn: psycopg2.extensions.connection, email: str) -> bool:
    """Return True if email is registered in the editor table."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM editor WHERE email = %s", (email,))
        return cur.fetchone() is not None


def is_admin(conn: psycopg2.extensions.connection, email: str) -> bool:
    """Return True if email is an editor row with admin = true (D6, migration 014).

    No admin rows (or no row at all for this email) means False — fail closed,
    same spirit as the old env-var default. DB-backed, resolved once per login.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT admin FROM editor WHERE email = %s", (email,))
        row = cur.fetchone()
        return row is not None and bool(row[0])


def approve_segment(conn: psycopg2.extensions.connection, segment_id: int) -> str:
    """Flip a needs_human segment to translated (queues it for batch polish).

    Returns:
        "ok"           — status flipped; caller's get_conn() will commit.
        "notfound"     — segment_id does not exist.
        "wrong_status" — segment is not needs_human.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE segment SET translation_status = 'translated'"
            " WHERE segment_id = %s AND translation_status = 'needs_human'"
            " RETURNING segment_id",
            (segment_id,),
        )
        if cur.fetchone() is not None:
            return "ok"
        cur.execute("SELECT 1 FROM segment WHERE segment_id = %s", (segment_id,))
        if cur.fetchone() is None:
            return "notfound"
        return "wrong_status"


def unapprove_segment(conn: psycopg2.extensions.connection, segment_id: int) -> str:
    """Flip a translated segment back to needs_human (only if batch polish has not run).

    Returns:
        "ok"              — status flipped; caller's get_conn() will commit.
        "notfound"        — segment_id does not exist.
        "wrong_status"    — segment is not translated.
        "already_polished"— a (sk, polish) row exists; cannot un-approve.
    """
    with conn.cursor() as cur:
        # Atomic: only flip if translated AND no (sk, polish) row exists.
        cur.execute(
            "UPDATE segment SET translation_status = 'needs_human'"
            " WHERE segment_id = %s AND translation_status = 'translated'"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM segment_text st"
            "     JOIN source s ON s.source_id = st.source_id"
            "     WHERE st.segment_id = %s AND st.lang = 'sk' AND s.code = 'polish'"
            "   )"
            " RETURNING segment_id",
            (segment_id, segment_id),
        )
        if cur.fetchone() is not None:
            return "ok"
        # UPDATE matched nothing — disambiguate the reason.
        cur.execute("SELECT 1 FROM segment WHERE segment_id = %s", (segment_id,))
        if cur.fetchone() is None:
            return "notfound"
        cur.execute(
            "SELECT translation_status FROM segment WHERE segment_id = %s", (segment_id,)
        )
        row = cur.fetchone()
        if row[0] != "translated":
            return "wrong_status"
        return "already_polished"
