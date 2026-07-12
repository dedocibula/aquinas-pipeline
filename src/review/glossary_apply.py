"""Apply an admin-approved glossary_proposal to the glossary / term_usage tables.

One function per proposal kind (D9). Callers own the connection lifecycle
(get_conn commits on clean exit) — nothing here commits. Never touches a
(sk, human) segment_text row. Raises ValueError on an invalid target so
callers can respond with a specific error rather than silently applying a
wrong change.

apply_rendering_change is also used by the retiring Sheets importer
(review.import_approvals.process_approval) — it is the shared implementation
of "approve a rendering", not glossary_apply-specific.
"""

from __future__ import annotations

from storage.db import source_id
from storage.repositories import GlossaryRepository, SegmentRepository, TermUsageRepository


def apply_rendering_change(conn, sense_id: int, new_sk: str) -> dict:
    """Approve a 'rendering' proposal: sense's SK rendering is wrong everywhere.

    Writes the human rendering and bumps the sense version — but only when the
    rendering actually differs from the current winning one (D3: bump_sense_version
    is not idempotent; a spurious bump restages every segment using this sense).
    """
    if not new_sk or not new_sk.strip():
        raise ValueError("proposed_sk is empty")

    glossary = GlossaryRepository(conn)
    old_sk = glossary.get_sk_rendering_content(sense_id)
    if new_sk == old_sk:
        return {
            "changed": False,
            "bumped": False,
            "old_sk": old_sk,
            "new_sk": new_sk,
            "new_version": None,
        }

    glossary.write_human_rendering(sense_id, new_sk, source_id(conn, "human"))
    new_version = glossary.bump_sense_version(sense_id)
    return {
        "changed": True,
        "bumped": True,
        "old_sk": old_sk,
        "new_sk": new_sk,
        "new_version": new_version,
    }


def apply_sense_here(conn, segment_id: int, from_sense_id: int, to_sense_id: int) -> dict:
    """Approve a 'sense_here' proposal: wrong sense chosen in this one segment.

    Re-points the segment's term_usage row at the editor-chosen sense (confirmed,
    never re-guessed), approves the target sense if it was still 'proposed' (it
    must constrain from now on), and resets the segment (D3: no version bump —
    only this one segment needs to change).
    """
    glossary = GlossaryRepository(conn)
    from_term = glossary.sense_term_id(from_sense_id)
    to_term = glossary.sense_term_id(to_sense_id)
    if from_term is None or to_term is None or from_term != to_term:
        raise ValueError("sense_here target must be a sense of the same glossary_term")

    target = glossary.get_current_sense(to_sense_id)
    if target["status"] == "retired":
        raise ValueError("sense_here target sense is retired")

    target_approved = False
    if target["status"] == "proposed":
        glossary.update_sense_status(to_sense_id, "approved")
        target_approved = True

    updated = TermUsageRepository(conn).update_sense_for_segment(
        segment_id, from_sense_id, to_sense_id, target["version"]
    )
    if updated == 0:
        raise ValueError(
            "no term_usage row for this segment/sense — proposal is stale"
        )
    statuses = SegmentRepository(conn).reset_segments(
        [segment_id], "editor corrected sense choice — verify"
    )
    return {
        "confirmed": True,
        "target_sense_approved": target_approved,
        "segment_reset": statuses[segment_id],
    }


def apply_remove_here(conn, segment_id: int, sense_id: int) -> dict:
    """Approve a 'remove_here' proposal: term falsely detected in this segment.

    Tombstones the segment's term_usage row (D10 — permanent, the resolver must
    never re-guess it) and resets the segment. No version bump (D3).
    """
    updated = TermUsageRepository(conn).mark_rejected(segment_id, sense_id)
    if updated == 0:
        raise ValueError(
            "no term_usage row for this segment/sense — proposal is stale"
        )
    statuses = SegmentRepository(conn).reset_segments(
        [segment_id], "editor rejected term detection — verify"
    )
    return {"rejected": True, "segment_reset": statuses[segment_id]}


def apply_retire_sense(conn, sense_id: int) -> dict:
    """Approve a 'retire_sense' proposal: glossary entry is overfit/wrong everywhere.

    Always bumps (D3) — removing a constraint must restage every segment that was
    translated under it, so the sense regenerates without it. term_usage rows are
    left in place; staleness needs them, and 'retired' already excludes the sense
    from constraints and future resolution.
    """
    glossary = GlossaryRepository(conn)
    glossary.update_sense_status(sense_id, "retired")
    new_version = glossary.bump_sense_version(sense_id)
    return {"retired": True, "new_version": new_version}


def apply_add_term(conn, latin_lemma: str, proposed_sk: str, note: str | None) -> dict:
    """Approve an 'add_term' proposal: term missing from the glossary.

    Creates the term + an approved sense + its human rendering. Never touches
    segments (D7) — corpus application is the explicit Stage 6 CLI step
    (re-resolve → diff → cost preview → reset).

    `note` is intentionally unused here: the editor's rationale is already
    preserved verbatim on the glossary_proposal row (audit trail), and
    glossary_sense.context_label has different semantics (sense disambiguation,
    e.g. "as passion") — it must not be repurposed to hold free-text notes.
    """
    glossary = GlossaryRepository(conn)
    if glossary.find_term_by_lemma(latin_lemma) is not None:
        raise ValueError("term_exists")

    term_id = glossary.insert_glossary_term(latin_lemma, "term", None)
    sense_id = glossary.insert_glossary_sense(term_id, None, status="approved")
    glossary.write_human_rendering(sense_id, proposed_sk, source_id(conn, "human"))
    return {"term_id": term_id, "sense_id": sense_id, "existed": False}
