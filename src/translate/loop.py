"""Translation loop — translate_segment() orchestrator.

MAX_ITERATIONS = 3 hard cap. Pre-checks run before every R1 call; a
pre-check failure sends the draft back to the translator without calling R1.
The loop writes the best qualifying draft and updates DB state, then commits.
"""

from __future__ import annotations

import logging

import psycopg2.extras
from dotenv import load_dotenv

from common.db import source_id
from common.pricing import UsageInfo
from translate.prechecks import check_structure, check_terminology
from translate.reviewer import call_reviewer_r1
from translate.translator import call_translator_v3, load_style_profile

load_dotenv()

MAX_ITERATIONS = 3
log = logging.getLogger(__name__)

_style_profile: dict | None = None


def _get_style_profile() -> dict:
    global _style_profile
    if _style_profile is None:
        _style_profile = load_style_profile()
    return _style_profile


# ── DB helpers ────────────────────────────────────────────────────────────────


def get_segment_with_texts(conn, segment_id: int) -> dict | None:
    """Return v_segment row for the given segment_id, or None if not found."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                s.segment_id,
                s.locator_path::text AS locator_path,
                s.element_type,
                s.reply_to,
                s.translation_status,
                max(t.content) FILTER (WHERE t.lang = 'la') AS latin,
                max(t.content) FILTER (WHERE t.lang = 'cs') AS czech,
                max(t.content) FILTER (WHERE t.lang = 'en') AS english
            FROM segment s
            LEFT JOIN segment_text t USING (segment_id)
            WHERE s.segment_id = %s
            GROUP BY s.segment_id, s.locator_path, s.element_type,
                     s.reply_to, s.translation_status
            """,
            (segment_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_locked_terms(conn, segment_id: int) -> list[dict]:
    """Return approved term constraints for a segment.

    Each entry: {latin_lemma, required_slovak, sense_id, version}.
    Only approved senses with a SK rendering are included.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                gt.latin_lemma,
                sr.content  AS required_slovak,
                gs.sense_id,
                gs.version
            FROM term_usage tu
            JOIN glossary_sense gs  ON gs.sense_id = tu.sense_id AND gs.status = 'approved'
            JOIN glossary_term  gt  ON gt.term_id  = gs.term_id
            JOIN sense_rendering sr ON sr.sense_id = gs.sense_id AND sr.lang = 'sk'
            WHERE tu.segment_id = %s
              AND sr.content IS NOT NULL
            """,
            (segment_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def write_segment_text(conn, segment_id: int, lang: str, src_id: int, content: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO segment_text (segment_id, lang, content, source_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (segment_id, lang, source_id) DO UPDATE
                SET content = EXCLUDED.content
            """,
            (segment_id, lang, content, src_id),
        )


def update_translation_status(conn, segment_id: int, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE segment SET translation_status = %s WHERE segment_id = %s",
            (status, segment_id),
        )


def write_reviewer_notes(conn, segment_id: int, notes: dict, iteration: int) -> None:
    payload = {"iteration": iteration, **notes}
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE segment SET reviewer_notes = %s WHERE segment_id = %s",
            (psycopg2.extras.Json(payload), segment_id),
        )


def update_sense_version_used(conn, segment_id: int, sense_id: int, version: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE term_usage SET sense_version_used = %s "
            "WHERE segment_id = %s AND sense_id = %s",
            (version, segment_id, sense_id),
        )


# ── Main loop ─────────────────────────────────────────────────────────────────


def translate_segment(segment_id: int, conn) -> tuple[str, list[UsageInfo]]:
    """Translate one segment through the full draft → pre-check → R1 → revise loop.

    Returns (status, usages) where status is 'translated' or 'needs_human' and
    usages is the list of UsageInfo from every API call made.
    Always commits before returning.
    Raises RuntimeError only if the segment is not found in DB.
    """
    seg = get_segment_with_texts(conn, segment_id)
    if seg is None:
        raise RuntimeError(f"segment_id={segment_id} not found in DB")

    locked_terms = get_locked_terms(conn, segment_id)
    constraints = [
        {"latin_lemma": t["latin_lemma"], "required_slovak": t["required_slovak"]}
        for t in locked_terms
    ]

    src_model = source_id(conn, "model")
    style_profile = _get_style_profile()

    prior_draft: str | None = None
    prior_feedback: str | None = None
    best_draft: str | None = None   # last draft that cleared pre-checks
    last_draft: str | None = None   # most recent draft regardless of pre-checks
    usages: list[UsageInfo] = []

    for iteration in range(1, MAX_ITERATIONS + 1):
        try:
            draft, t_usage = call_translator_v3(
                seg, constraints, prior_draft, prior_feedback, style_profile
            )
            usages.append(t_usage)
        except RuntimeError as exc:
            log.error("segment_id=%d iteration=%d translator error: %s", segment_id, iteration, exc)
            break

        last_draft = draft

        structure_result = check_structure(seg, draft, conn)
        terminology_result = check_terminology(draft, constraints)

        if not (structure_result.ok and terminology_result.ok):
            failures = structure_result.failures + terminology_result.failures
            prior_feedback = "Pre-check failures — fix before R1 review:\n" + "\n".join(
                f"  - {f}" for f in failures
            )
            prior_draft = draft
            if best_draft is None:
                best_draft = draft
            continue  # back to translator; do NOT call R1

        best_draft = draft  # cleared pre-checks — candidate for best

        latin = seg.get("latin")
        if not latin:
            log.error("segment_id=%d iteration=%d: missing Latin text; skipping R1", segment_id, iteration)
            break

        try:
            review = call_reviewer_r1(latin, draft, constraints)
            if review.usage is not None:
                usages.append(review.usage)
        except RuntimeError as exc:
            log.error("segment_id=%d iteration=%d reviewer error: %s", segment_id, iteration, exc)
            break

        if review.verdict in ("APPROVED", "APPROVED_WITH_NOTES"):
            write_segment_text(conn, segment_id, "sk", src_model, draft)
            update_translation_status(conn, segment_id, "translated")
            if review.notes:
                write_reviewer_notes(conn, segment_id, review.notes, iteration)
            for term in locked_terms:
                update_sense_version_used(conn, segment_id, term["sense_id"], term["version"])
            conn.commit()
            log.info("segment_id=%d translated in %d iteration(s)", segment_id, iteration)
            return "translated", usages

        prior_feedback = review.feedback
        prior_draft = draft

    # Exhausted — write best_draft (last to clear pre-checks) or fall back to last_draft
    final_draft = best_draft or last_draft
    if final_draft is None:
        # No draft was ever produced (e.g., translator raised on iteration 1)
        log.error("segment_id=%d: no draft produced; skipping DB write", segment_id)
        return "needs_human", usages

    write_segment_text(conn, segment_id, "sk", src_model, final_draft)
    update_translation_status(conn, segment_id, "needs_human")
    for term in locked_terms:
        update_sense_version_used(conn, segment_id, term["sense_id"], term["version"])
    conn.commit()
    log.warning("segment_id=%d flagged needs_human after %d iterations", segment_id, MAX_ITERATIONS)
    return "needs_human", usages
