"""
DB query helpers for the Flask preview server.

All functions accept a psycopg2 connection.
They are intentionally separate from src/storage/db.py — that module manages
connection lifecycle; this module owns the server-specific SQL.
"""

from __future__ import annotations

import dataclasses

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
    ActionResult,
    ActivityEntry,
    Comment,
    CommentCount,
    CommentThread,
    Constraint,
    Proposal,
    Segment,
    UserDigest,
)
from storage.repositories import (
    ActivityRepository,
    CommentRepository,
    GlossaryRepository,
    ProposalRepository,
    SegmentRepository,
)

# Editor glossary-proposal kinds (glossary_proposal.kind CHECK constraint,
# migration 013). Values are locked schema truth — do not change them; these
# are just intuitive Python-side names for the five DB strings.
PROPOSAL_KIND_CHANGE_EVERYWHERE = "rendering"
PROPOSAL_KIND_WRONG_SENSE_HERE = "sense_here"
PROPOSAL_KIND_REMOVE_HERE = "remove_here"
PROPOSAL_KIND_RETIRE_EVERYWHERE = "retire_sense"
PROPOSAL_KIND_ADD_TERM = "add_term"

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
) -> list[Segment]:
    """Return all segments for an article with Latin, Czech, English, and Slovak text.

    Returns separate machine (slovak_model) and human (slovak_human) Slovak columns,
    plus human-review metadata from segment_review.
    """
    return SegmentRepository(conn).get_article_segments(article_path)


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
) -> Segment | None:
    """Return the question_title segment for a question, or None if absent."""
    return SegmentRepository(conn).get_question_title_segment(question_path)


def get_segment_constraints(
    conn: psycopg2.extensions.connection,
    segment_ids: list[int],
) -> dict[int, list[Constraint]]:
    """Return approved term constraints used for each segment.

    Joins term_usage → glossary_sense (status='approved') → sense_rendering (lang='sk')
    → glossary_term to surface the Latin lemma and optional context_label.

    Returns a dict keyed by segment_id; each value is a list of ``Constraint``.
    Missing segment_ids are not included (caller treats absence as empty list).
    A 'rejected' term_usage row (D10 tombstone) is excluded even if the sense
    itself is still approved.
    """
    return GlossaryRepository(conn).locked_terms_for(segment_ids)


def get_term_senses(conn: psycopg2.extensions.connection, sense_id: int) -> dict | None:
    """Return the term owning ``sense_id`` and all of that term's senses.

    Powers the "wrong sense here" dropdown (Stage 3). Includes senses of any
    status (proposed/approved/retired) — the editor must be able to name a
    not-yet-approved sense as the correct one; ``apply_sense_here`` approves
    it on admin approval. Each ``Sense.sk_content`` is its winning rendering
    (authority_rank ASC), same join shape as ``get_segment_constraints``, or
    None if it has no sk rendering yet. Returns
    ``{"term_id", "latin_lemma", "senses": list[Sense]}``, or None if
    ``sense_id`` doesn't exist.
    """
    glossary = GlossaryRepository(conn)
    found = glossary.term_lemma_for_sense(sense_id)
    if found is None:
        return None
    term_id, latin_lemma = found
    senses = glossary.senses_for_term(term_id)
    return {"term_id": term_id, "latin_lemma": latin_lemma, "senses": senses}


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


# Public: the single source of truth for these groupings. app.py imports them
# directly; do not re-declare local copies elsewhere in Python.
SENSE_WIDE_KINDS = (PROPOSAL_KIND_CHANGE_EVERYWHERE, PROPOSAL_KIND_RETIRE_EVERYWHERE)
PER_SEGMENT_KINDS = (PROPOSAL_KIND_WRONG_SENSE_HERE, PROPOSAL_KIND_REMOVE_HERE)


def _sense_blast_radius(conn: psycopg2.extensions.connection, sense_id: int) -> dict:
    """Segments locked by ``sense_id``, split by translation_status, plus the
    marginal restage count — segments that are not already stale.
    """
    return GlossaryRepository(conn).sense_blast_radius(sense_id)


def _origin_locator(conn: psycopg2.extensions.connection, segment_id: int) -> str | None:
    return SegmentRepository(conn).get_locators([segment_id]).get(segment_id)


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


def _enrich_proposal_display(
    conn: psycopg2.extensions.connection,
    proposal: Proposal,
    term_cache: dict[int, dict | None] | None = None,
) -> Proposal:
    """Return a copy of ``proposal`` with term/sense display fields filled in —
    shared by the pending queue and the decided-proposal audit trail.

    Sets ``context_label`` — the acted-on sense's context_label (None for
    add_term); ``proposed_context_label`` / ``proposed_slovak`` — for
    sense_here with a chosen ``proposed_sense_id`` (None for the free-text
    record-only path); ``origin_locator`` — the origin segment's locator
    path, only for per-segment kinds (``sense_here``, ``remove_here``).

    ``term_cache`` lets callers iterating many proposals memoize
    ``get_term_senses`` by ``sense_id`` across the whole list, since several
    proposals commonly share a sense (e.g. a sense-wide plus a per-segment
    proposal on the same term).
    """
    context_label = None
    proposed_context_label = None
    proposed_slovak = None
    origin_locator = None

    if proposal.sense_id is not None:
        if term_cache is not None:
            if proposal.sense_id not in term_cache:
                term_cache[proposal.sense_id] = get_term_senses(conn, proposal.sense_id)
            term = term_cache[proposal.sense_id]
        else:
            term = get_term_senses(conn, proposal.sense_id)
        if term is not None:
            sense_row = next(
                (s for s in term["senses"] if s.sense_id == proposal.sense_id), None
            )
            if sense_row is not None:
                context_label = sense_row.context_label

            if proposal.proposed_sense_id is not None:
                proposed_row = next(
                    (
                        s
                        for s in term["senses"]
                        if s.sense_id == proposal.proposed_sense_id
                    ),
                    None,
                )
                if proposed_row is not None:
                    proposed_context_label = proposed_row.context_label
                    proposed_slovak = proposed_row.sk_content

    if proposal.kind in PER_SEGMENT_KINDS and proposal.origin_segment_id is not None:
        origin_locator = _origin_locator(conn, proposal.origin_segment_id)

    return dataclasses.replace(
        proposal,
        context_label=context_label,
        proposed_context_label=proposed_context_label,
        proposed_slovak=proposed_slovak,
        origin_locator=origin_locator,
    )


def get_pending_proposals_view(conn: psycopg2.extensions.connection) -> list[Proposal]:
    """Return every pending proposal enriched for the admin queue (Stage 4).

    Each ``Proposal`` carries the shared display fields from
    ``_enrich_proposal_display``, plus:
      - ``live_current_sk`` / ``drift`` — the sense's *current* winning rendering
        vs. the snapshot taken at propose time; drift=True means the glossary
        moved since the editor proposed (admin should re-check before approving).
      - ``blast_radius`` — dict from ``_sense_blast_radius``, only for sense-wide
        kinds (``rendering``, ``retire_sense``).
    """
    glossary = GlossaryRepository(conn)
    rows = ProposalRepository(conn).list_pending()

    # Memoized per sense_id: several pending proposals commonly share a sense
    # (e.g. a sense-wide plus a per-segment proposal on the same term), so
    # cache each of these lookups across the whole list rather than repeating
    # them per proposal.
    term_cache: dict[int, dict | None] = {}
    current_sense_cache: dict[int, object] = {}
    sk_rendering_cache: dict[int, str | None] = {}
    blast_radius_cache: dict[int, dict] = {}

    result = []
    for row in rows:
        row = _enrich_proposal_display(conn, row, term_cache=term_cache)
        live_current_sk = None
        drift = False
        blast_radius = None

        if row.sense_id is not None:
            if row.sense_id not in current_sense_cache:
                current_sense_cache[row.sense_id] = glossary.get_current_sense(row.sense_id)
            current = current_sense_cache[row.sense_id]
            if current is not None:
                if row.sense_id not in sk_rendering_cache:
                    sk_rendering_cache[row.sense_id] = glossary.get_sk_rendering_content(
                        row.sense_id
                    )
                live_current_sk = sk_rendering_cache[row.sense_id]
                drift = live_current_sk != row.current_sk

        if row.kind in SENSE_WIDE_KINDS and row.sense_id is not None:
            if row.sense_id not in blast_radius_cache:
                blast_radius_cache[row.sense_id] = _sense_blast_radius(conn, row.sense_id)
            blast_radius = blast_radius_cache[row.sense_id]

        result.append(
            dataclasses.replace(
                row,
                live_current_sk=live_current_sk,
                drift=drift,
                blast_radius=blast_radius,
            )
        )

    return result


def get_decided_proposals_view(
    conn: psycopg2.extensions.connection, limit: int = 200
) -> list[Proposal]:
    """Return the most recent decided proposals for the admin audit trail.

    Same shared display fields as ``get_pending_proposals_view`` (minus
    drift/blast-radius, which only matter before a decision is made) so the
    history table can reuse the same rendering as the live queue.
    """
    rows = ProposalRepository(conn).list_decided(limit=limit)
    term_cache: dict[int, dict | None] = {}
    return [_enrich_proposal_display(conn, row, term_cache=term_cache) for row in rows]


class ProposalRaceError(Exception):
    """Raised when a proposal was decided by someone else between read and decide.

    Signals the caller's ``with get_conn()`` block to roll back any writes the
    dispatched glossary_apply service already made, then respond 409.
    """


_EDIT_BEFORE_APPROVE_KINDS = (PROPOSAL_KIND_CHANGE_EVERYWHERE, PROPOSAL_KIND_ADD_TERM)


def approve_proposal(
    conn: psycopg2.extensions.connection,
    proposal_id: int,
    admin_email: str,
    decision_note: str | None,
    edited_sk: str | None = None,
) -> ActionResult:
    """Apply an admin-approved glossary_proposal and mark it approved (Stage 4).

    Dispatches to the ``review.glossary_apply`` service matching the
    proposal's kind, then marks the row approved, then auto-supersedes
    competing pending sense-wide proposals on the same sense (D5). Everything
    runs in the caller's transaction — ``get_conn`` commits on clean exit, so
    a failure at any point (including the race below) rolls back the whole
    thing, including any glossary/term_usage write the service already made.

    ``edited_sk``, when given, lets the approving admin lightly edit the
    editor's proposed text before it's applied — e.g. fixing a typo — instead
    of rejecting and asking for a resubmit. Only proposal kinds carrying
    free-text ``proposed_sk`` accept this: ``rendering``, ``add_term``, and
    ``sense_here`` in its free-text/record-only branch (``proposed_sense_id``
    is None). Raises ``ValueError`` if given for any other kind, or if blank.

    Returns an ``ActionResult``:
      "ok"          -> payload is the service's result dict. A ``sense_here``
                       proposal with no ``proposed_sense_id`` (the free-text,
                       record-only gold-label path, D9) calls no service and
                       returns ``{"acknowledged": True}``.
      "not_found"   -> proposal_id doesn't exist; payload carries an error message.
      "not_pending" -> already decided (by an earlier, already-committed
                       request); payload carries an error message. No writes
                       have happened yet at this point, so nothing to roll back.

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
        return ActionResult("not_found", {"error": "not found"})
    if proposal.status != "pending":
        return ActionResult("not_pending", {"error": "not pending"})

    kind = proposal.kind
    sense_id = proposal.sense_id

    accepts_edit = kind in _EDIT_BEFORE_APPROVE_KINDS or (
        kind == PROPOSAL_KIND_WRONG_SENSE_HERE and proposal.proposed_sense_id is None
    )
    if edited_sk is not None:
        if not accepts_edit:
            raise ValueError(f"proposal kind '{kind}' does not support edit-before-approve")
        edited_sk = edited_sk.strip()
        if not edited_sk:
            raise ValueError("edited proposed_sk cannot be empty")

    proposed_sk = edited_sk if edited_sk is not None else proposal.proposed_sk

    if kind == PROPOSAL_KIND_CHANGE_EVERYWHERE:
        result = apply_rendering_change(conn, sense_id, proposed_sk)
    elif kind == PROPOSAL_KIND_WRONG_SENSE_HERE:
        if proposal.proposed_sense_id is not None:
            result = apply_sense_here(
                conn,
                proposal.origin_segment_id,
                sense_id,
                proposal.proposed_sense_id,
            )
        else:
            result = {"acknowledged": True}
    elif kind == PROPOSAL_KIND_REMOVE_HERE:
        result = apply_remove_here(conn, proposal.origin_segment_id, sense_id)
    elif kind == PROPOSAL_KIND_RETIRE_EVERYWHERE:
        result = apply_retire_sense(conn, sense_id)
    else:  # add_term
        result = apply_add_term(conn, proposal.latin_lemma, proposed_sk, proposal.note)

    if not repo.decide(
        proposal_id, "approved", admin_email, decision_note, proposed_sk=edited_sk
    ):
        raise ProposalRaceError(f"proposal {proposal_id} was already decided")

    if kind in SENSE_WIDE_KINDS:
        repo.supersede_sense_wide_siblings(sense_id, proposal_id, admin_email)

    return ActionResult("ok", result)


def reject_proposal(
    conn: psycopg2.extensions.connection,
    proposal_id: int,
    admin_email: str,
    decision_note: str | None,
) -> ActionResult:
    """Reject a pending proposal. Returns an ``ActionResult`` "ok" / "not_found" / "not_pending"."""
    repo = ProposalRepository(conn)
    proposal = repo.get(proposal_id)
    if proposal is None:
        return ActionResult("not_found", {"error": "not found"})
    if proposal.status != "pending":
        return ActionResult("not_pending", {"error": "not pending"})
    if not repo.decide(proposal_id, "rejected", admin_email, decision_note):
        return ActionResult("not_pending", {"error": "not pending"})
    return ActionResult("ok")


def reopen_proposal(
    conn: psycopg2.extensions.connection, proposal_id: int, admin_email: str
) -> ActionResult:
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

    Returns an ``ActionResult``:
      "ok"           -> payload's proposal_id is the freshly created pending row.
      "not_found"    -> proposal_id doesn't exist; payload carries an error message.
      "not_rejected" -> proposal exists but isn't rejected; payload carries an error message.
    """
    repo = ProposalRepository(conn)
    proposal = repo.get(proposal_id)
    if proposal is None:
        return ActionResult("not_found", {"error": "not found"})
    if proposal.status != "rejected":
        return ActionResult("not_rejected", {"error": "not rejected"})

    sense_id = proposal.sense_id
    if sense_id is not None and GlossaryRepository(conn).get_current_sense(sense_id) is None:
        raise ValueError(f"sense {sense_id} no longer exists")
    origin_segment_id = proposal.origin_segment_id
    if origin_segment_id is not None and not segment_exists(conn, origin_segment_id):
        raise ValueError(f"segment {origin_segment_id} no longer exists")

    new_id = repo.clone_as_pending(proposal_id)
    return ActionResult("ok", {"proposal_id": new_id})


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
    return GlossaryRepository(conn).is_locked_in_segment(segment_id, sense_id)


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
) -> ActionResult:
    """Validate and record an editor's proposed sense-targeted glossary change.

    ``kind`` is one of the PROPOSAL_KIND_CHANGE_EVERYWHERE / WRONG_SENSE_HERE /
    REMOVE_HERE / RETIRE_EVERYWHERE constants (add_term uses propose_add_term
    instead). ``proposed_sk`` should already be stripped by the caller.

    Does NOT commit — caller's ``get_conn()`` handles the commit.

    Returns an ``ActionResult``:
      "ok"               -> payload's proposal_id is the new/updated pending row
      "not_found"        -> sense_id doesn't exist
      "no_change"        -> proposed_sk/proposed_sense_id equals current state
      "wrong_term"       -> proposed_sense_id isn't a sense of the same term
      "not_locked_here"  -> origin_segment_id doesn't actually lock sense_id (D8)
      "proposed_sk_required" -> rendering kind needs a non-empty proposed_sk
      "missing_target"   -> sense_here needs proposed_sense_id or proposed_sk
    Every non-"ok" status carries an ``error`` message in its payload.
    """
    glossary = GlossaryRepository(conn)
    current_sense = glossary.get_current_sense(sense_id)
    if current_sense is None:
        return ActionResult("not_found", {"error": "not found"})

    term = get_term_senses(conn, sense_id)
    current_sk = glossary.get_sk_rendering_content(sense_id)

    resolved_proposed_sk: str | None = None
    resolved_proposed_sense_id: int | None = None

    if kind == PROPOSAL_KIND_CHANGE_EVERYWHERE:
        if not proposed_sk:
            return ActionResult("proposed_sk_required", {"error": "proposed_sk required"})
        if proposed_sk == (current_sk or ""):
            return ActionResult("no_change", {"error": "no_change"})
        resolved_proposed_sk = proposed_sk

    elif kind == PROPOSAL_KIND_WRONG_SENSE_HERE:
        if not segment_has_locked_sense(conn, origin_segment_id, sense_id):
            return ActionResult("not_locked_here", {"error": "not_locked_here"})
        if proposed_sense_id is not None:
            if proposed_sense_id == sense_id:
                return ActionResult("no_change", {"error": "no_change"})
            if not any(s.sense_id == proposed_sense_id for s in term["senses"]):
                return ActionResult("wrong_term", {"error": "wrong_term"})
            resolved_proposed_sense_id = proposed_sense_id
        elif proposed_sk:
            if proposed_sk == (current_sk or ""):
                return ActionResult("no_change", {"error": "no_change"})
            resolved_proposed_sk = proposed_sk
        else:
            return ActionResult(
                "missing_target", {"error": "proposed_sense_id or proposed_sk required"}
            )

    elif kind == PROPOSAL_KIND_REMOVE_HERE:
        if not segment_has_locked_sense(conn, origin_segment_id, sense_id):
            return ActionResult("not_locked_here", {"error": "not_locked_here"})

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
    return ActionResult("ok", {"proposal_id": proposal_id})


def propose_add_term(
    conn: psycopg2.extensions.connection,
    *,
    latin_lemma: str,
    proposed_sk: str,
    note: str | None,
    origin_segment_id: int | None,
    proposed_by: str,
) -> ActionResult:
    """Record an editor's suggestion for a missing glossary term (kind=add_term).

    Does NOT commit — caller's ``get_conn()`` handles the commit.

    Returns an ``ActionResult``:
      "ok"          -> payload's proposal_id is the new pending row
      "term_exists" -> latin_lemma is already a glossary term; payload carries
                       an error message naming it
    """
    if GlossaryRepository(conn).find_term_by_lemma(latin_lemma) is not None:
        return ActionResult(
            "term_exists",
            {"error": f"term_exists: '{latin_lemma}' is already in the glossary"},
        )

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
    return ActionResult("ok", {"proposal_id": proposal_id})


def get_question_preamble_segment(
    conn: psycopg2.extensions.connection,
    question_path: str,
) -> Segment | None:
    """Return the preamble segment for a question, or None if absent.

    Preambles sit at ``<question_path>.preamble`` (e.g. 'I.q1.preamble').
    """
    return SegmentRepository(conn).get_question_preamble_segment(question_path)


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
) -> ActionResult:
    """Create or update the human review for a segment.

    ``action`` must be one of: ``save``, ``accept``, ``note``, ``reset``.
    ``expected_version`` is the optimistic-lock token the caller last read
    (0 means no review row existed when the caller loaded the segment).

    Returns an ``ActionResult`` with status ``ok`` (payload ``{human_version}``),
    ``conflict``, or ``notfound`` (both carrying an ``error`` message).
    Does NOT commit — caller's ``get_conn()`` handles the commit.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM segment WHERE segment_id = %s", (segment_id,))
        if cur.fetchone() is None:
            return ActionResult("notfound", {"error": "not found"})

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
                    return ActionResult("conflict", {"error": "conflict"})
            human_src_id = source_id(conn, "human")
            cur.execute(
                "DELETE FROM segment_text WHERE segment_id = %s AND lang = 'sk' AND source_id = %s",
                (segment_id, human_src_id),
            )
            return ActionResult("ok", {"human_version": 0})

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
            return ActionResult("conflict", {"error": "conflict"})

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

        return ActionResult("ok", {"human_version": new_version})


# ---------------------------------------------------------------------------
# Comment threads (editor-internal, per segment)
# ---------------------------------------------------------------------------


def segment_exists(conn: psycopg2.extensions.connection, segment_id: int) -> bool:
    """True if segment_id is a real segment.

    Callers that insert into segment_comment / comment_thread_state (both FK'd to
    segment) must check this first — otherwise an unknown segment_id surfaces as an
    unhandled IntegrityError (500) instead of a clean 404, unlike review_segment's
    explicit notfound check.
    """
    return CommentRepository(conn).segment_exists(segment_id)


def list_comments(conn: psycopg2.extensions.connection, segment_id: int) -> CommentThread:
    """Return the full comment thread for a segment, oldest first."""
    return CommentRepository(conn).list_comments(segment_id)


def add_comment(conn: psycopg2.extensions.connection, segment_id: int, author: str, body: str) -> Comment:
    """Insert a new comment. A new comment on a resolved thread implicitly reopens it."""
    return CommentRepository(conn).add_comment(segment_id, author, body)


def resolve_thread(conn: psycopg2.extensions.connection, segment_id: int, resolver_email: str) -> int:
    """Mark every open comment in the thread as resolved. Returns the number of rows flipped."""
    return CommentRepository(conn).resolve_thread(segment_id, resolver_email)


def reopen_thread(conn: psycopg2.extensions.connection, segment_id: int) -> int:
    """Clear resolved state on every comment in the thread. Returns rows flipped."""
    return CommentRepository(conn).reopen_thread(segment_id)


def delete_comment(conn: psycopg2.extensions.connection, comment_id: int, requester_email: str) -> str:
    """Delete a comment iff the requester is its author.

    Returns ``"ok"``, ``"notfound"``, or ``"forbidden"``.
    """
    return CommentRepository(conn).delete_comment(comment_id, requester_email)


def mark_thread_read(conn: psycopg2.extensions.connection, segment_id: int, user_email: str) -> None:
    """Bump the viewer's read watermark for a segment's thread (clears the unread dot)."""
    CommentRepository(conn).mark_thread_read(segment_id, user_email)


def get_comment_counts(
    conn: psycopg2.extensions.connection, segment_ids: list[int], viewer_email: str
) -> dict[int, CommentCount]:
    """Return per-segment comment badge counts for the given segments.

    ``unread`` counts comments by other authors newer than the viewer's
    ``last_read_at`` watermark (NULL watermark means everything is unread).
    Segments with no comments are omitted from the result.
    """
    return CommentRepository(conn).get_comment_counts(segment_ids, viewer_email)


def get_activity_feed(
    conn: psycopg2.extensions.connection, *, before: str | None = None, limit: int = 50
) -> list[ActivityEntry]:
    """Return the admin `/timeline` activity feed: reviews, comments, and run markers.

    Merges the three sources newest-first. ``before`` (an ISO timestamp) paginates
    to entries strictly older than it. Uses only existing timestamps — no new
    per-row timestamp column.
    """
    return ActivityRepository(conn).get_activity_feed(before=before, limit=limit)


def collect_digests(conn: psycopg2.extensions.connection) -> list[UserDigest]:
    """Return, per recipient, the unread comment replies their daily digest should cover.

    Recipients = thread participants (everyone who has commented on the segment) ∪ the
    segment's reviewer, minus each comment's own author. A comment is digest-worthy for a
    recipient when it postdates both their read and their last-notified watermark (NULL
    watermark means everything qualifies).
    """
    return ActivityRepository(conn).collect_digests()


def mark_thread_notified(conn: psycopg2.extensions.connection, segment_id: int, user_email: str) -> None:
    """Bump the recipient's notified watermark for a segment's thread (digest de-dupe)."""
    ActivityRepository(conn).mark_thread_notified(segment_id, user_email)


def get_structural_formulas(conn: psycopg2.extensions.connection) -> dict[str, str]:
    """Load approved Slovak forms for sed_contra, respondeo, praeterea.

    Missing formulas are silently omitted (never raises) — non-critical; the
    template falls back to hardcoded defaults. See GlossaryRepository.get_structural_formulas
    for the query and the (deliberate) broad exception swallow.
    """
    return GlossaryRepository(conn).get_structural_formulas()


def get_distinct_pars(conn: psycopg2.extensions.connection, work_id: int) -> list[str]:
    """Return sorted list of pars labels that have translated/needs_human segments."""
    return SegmentRepository(conn).get_distinct_pars(work_id)


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
    return SegmentRepository(conn).approve_segment(segment_id)


def unapprove_segment(conn: psycopg2.extensions.connection, segment_id: int) -> str:
    """Flip a translated segment back to needs_human (only if batch polish has not run).

    Returns:
        "ok"              — status flipped; caller's get_conn() will commit.
        "notfound"        — segment_id does not exist.
        "wrong_status"    — segment is not translated.
        "already_polished"— a (sk, polish) row exists; cannot un-approve.
    """
    return SegmentRepository(conn).unapprove_segment(segment_id)
