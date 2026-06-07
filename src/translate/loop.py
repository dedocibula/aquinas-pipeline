"""Translation loop — translate_segment() orchestrator.

MAX_ITERATIONS = 3 hard cap. Pre-checks run before every R1 call; a
pre-check failure sends the draft back to the translator without calling R1.
The loop writes the best qualifying draft and updates DB state, then commits.
"""

from __future__ import annotations

import logging
import re

import psycopg2.extras
from dotenv import load_dotenv

from common.db import source_id
from common.lemmatize import lemmatize_latin
from common.pricing import UsageInfo
from translate.prechecks import check_structure, check_terminology_lemma
from translate.prompt_logger import PromptLogger
from translate.reviewer import build_reviewer_turn, call_reviewer_r1
from translate.translator import (
    build_user_turn,
    call_translator_v3,
    load_translator_system_prompt,
)

load_dotenv()

MAX_ITERATIONS = 3
log = logging.getLogger(__name__)


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
            SELECT DISTINCT ON (gs.sense_id)
                gt.latin_lemma,
                sr.content  AS required_slovak,
                gs.sense_id,
                gs.version
            FROM term_usage tu
            JOIN glossary_sense gs  ON gs.sense_id = tu.sense_id AND gs.status = 'approved'
            JOIN glossary_term  gt  ON gt.term_id  = gs.term_id
            JOIN sense_rendering sr ON sr.sense_id = gs.sense_id AND sr.lang = 'sk'
            JOIN source          s  ON s.source_id  = sr.source_id
            WHERE tu.segment_id = %s
              AND sr.content IS NOT NULL
            ORDER BY gs.sense_id, s.authority_rank
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


# ── Term lookup ───────────────────────────────────────────────────────────────

_SUFFIX_RE = re.compile(r"\d+$")


def _build_surface_constraints(latin: str, constraints: list[dict]) -> list[dict]:
    """Replace lemma-form constraints with the inflected surface forms from the Latin text.

    For each approved single-word term, finds all tokens in `latin` whose CLTK lemma
    matches and substitutes those surface forms so the translator sees the exact inflected
    words (e.g. 'rationem → rozum', 'rationi → rozum' rather than 'ratio → rozum').

    Multiword terms (space in lemma) are kept as-is — phrase matching, not lemmatization.
    Falls back to the original lemma form for any term CLTK does not find in this text.
    Each constraint is processed independently, so two approved senses sharing the same
    lemma (different Slovak renderings) are both emitted correctly.
    """
    if not constraints or not latin:
        return constraints

    tokens = set(re.findall(r"[a-zA-Z]+", latin))

    # Pre-compute token → set of stripped lemmas once to avoid re-calling CLTK per constraint.
    token_lemmas: dict[str, set[str]] = {
        token: {_SUFFIX_RE.sub("", cand) for cand in lemmatize_latin(token)}
        for token in tokens
    }

    result: list[dict] = []
    for c in constraints:
        lemma = c["latin_lemma"]
        if " " in lemma:
            result.append(c)
            continue

        stripped = _SUFFIX_RE.sub("", lemma)
        surfaces = sorted(t for t, ls in token_lemmas.items() if stripped in ls)
        if surfaces:
            for surface in surfaces:
                result.append({"latin_lemma": surface, "required_slovak": c["required_slovak"]})
        else:
            result.append(c)

    return result


# ── Main loop ─────────────────────────────────────────────────────────────────


def translate_segment(
    segment_id: int, conn, prompt_log: PromptLogger | None = None
) -> tuple[str, list[UsageInfo]]:
    """Translate one segment through the full draft → pre-check → R1 → revise loop.

    Returns (status, usages) where status is 'translated' or 'needs_human' and
    usages is the list of UsageInfo from every API call made.
    Always commits before returning.
    Raises RuntimeError only if the segment is not found in DB.
    """
    seg = get_segment_with_texts(conn, segment_id)
    if seg is None:
        raise RuntimeError(f"segment_id={segment_id} not found in DB")
    if not seg.get("latin"):
        log.error("segment_id=%d: no Latin text in DB; flagging needs_human", segment_id)
        update_translation_status(conn, segment_id, "needs_human")
        conn.commit()
        return "needs_human", []

    locked_terms = get_locked_terms(conn, segment_id)
    constraints = [
        {"latin_lemma": t["latin_lemma"], "required_slovak": t["required_slovak"]}
        for t in locked_terms
    ]
    # Surface-form constraints for the translator: CLTK maps each approved lemma
    # to the inflected forms that actually appear in this segment's Latin text,
    # so the model sees e.g. 'rationem → rozum' rather than 'ratio → rozum'.
    # The reviewer still receives lemma-form constraints (more semantic for auditing).
    translator_constraints = _build_surface_constraints(seg.get("latin") or "", constraints)

    src_model = source_id(conn, "model")

    prior_draft: str | None = None
    prior_feedback: str | None = None
    precheck_passing_draft: str | None = None   # last draft that cleared ALL pre-checks
    precheck_passing_iter: int | None = None
    fallback_draft: str | None = None           # any draft produced; absolute last resort
    fallback_iter: int | None = None
    usages: list[UsageInfo] = []
    locator = seg.get("locator_path", "")

    for iteration in range(1, MAX_ITERATIONS + 1):
        # Build prompts for logging. load_translator_system_prompt returns a cached string;
        # build_user_turn is deterministic given the same arguments. call_translator_v3
        # calls them again internally, so the logged strings match what is sent.
        system_prompt = load_translator_system_prompt() if prompt_log else ""
        user_turn = build_user_turn(seg, translator_constraints, prior_draft, prior_feedback) if prompt_log else ""

        try:
            draft, t_usage = call_translator_v3(
                seg, translator_constraints, prior_draft, prior_feedback
            )
            usages.append(t_usage)
        except RuntimeError as exc:
            log.error("segment_id=%d iteration=%d translator error: %s", segment_id, iteration, exc)
            break

        fallback_draft = draft
        fallback_iter = iteration

        structure_result = check_structure(seg, draft, conn)
        terminology_result = check_terminology_lemma(draft, constraints)

        if not structure_result.ok or not terminology_result.ok:
            all_failures = structure_result.failures + terminology_result.failures
            prior_feedback = "Pre-check failures — fix before R1 review:\n" + "\n".join(
                f"  - {f}" for f in all_failures
            )
            if prompt_log:
                prompt_log.log_iteration(
                    segment_id=segment_id,
                    locator_path=locator,
                    iteration=iteration,
                    system_prompt=system_prompt,
                    user_turn=user_turn,
                    draft=draft,
                    precheck_ok=False,
                    precheck_failures=all_failures,
                    reviewer_turn=None,
                    verdict=None,
                    notes=None,
                    feedback=prior_feedback,
                )
            prior_draft = draft
            continue  # back to translator; do NOT call R1

        precheck_passing_draft = draft
        precheck_passing_iter = iteration

        latin = seg.get("latin")
        if not latin:
            log.error("segment_id=%d iteration=%d: missing Latin text; skipping R1", segment_id, iteration)
            break

        reviewer_turn = build_reviewer_turn(latin, draft, constraints) if prompt_log else ""

        try:
            review = call_reviewer_r1(latin, draft, constraints)
            if review.usage is not None:
                usages.append(review.usage)
        except RuntimeError as exc:
            log.error("segment_id=%d iteration=%d reviewer error: %s", segment_id, iteration, exc)
            break

        if prompt_log:
            prompt_log.log_iteration(
                segment_id=segment_id,
                locator_path=locator,
                iteration=iteration,
                system_prompt=system_prompt,
                user_turn=user_turn,
                draft=draft,
                precheck_ok=True,
                precheck_failures=[],
                reviewer_turn=reviewer_turn,
                verdict=review.verdict,
                notes=review.notes,
                feedback=review.feedback,
            )

        if review.verdict in ("APPROVED", "APPROVED_WITH_NOTES"):
            write_segment_text(conn, segment_id, "sk", src_model, draft)
            update_translation_status(conn, segment_id, "translated")
            if review.notes:
                write_reviewer_notes(conn, segment_id, review.notes, iteration)
            for term in locked_terms:
                update_sense_version_used(conn, segment_id, term["sense_id"], term["version"])
            conn.commit()
            log.info("segment_id=%d translated in %d iteration(s)", segment_id, iteration)
            if prompt_log:
                prompt_log.log_final(
                    segment_id=segment_id,
                    locator_path=locator,
                    status="translated",
                    chosen_iteration=iteration,
                    chosen_draft=draft,
                )
            return "translated", usages

        prior_feedback = review.feedback
        prior_draft = draft

    # Exhausted — write precheck_passing_draft (last to clear all pre-checks) or fall back
    final_draft = precheck_passing_draft if precheck_passing_draft is not None else fallback_draft
    chosen_iter = precheck_passing_iter if precheck_passing_iter is not None else fallback_iter
    if final_draft is None:
        # No draft was ever produced (e.g., translator raised on iteration 1)
        log.error("segment_id=%d: no draft produced; skipping DB write", segment_id)
        if prompt_log:
            prompt_log.log_final(
                segment_id=segment_id,
                locator_path=locator,
                status="needs_human",
                chosen_iteration=None,
                chosen_draft=None,
            )
        return "needs_human", usages

    write_segment_text(conn, segment_id, "sk", src_model, final_draft)
    update_translation_status(conn, segment_id, "needs_human")
    for term in locked_terms:
        update_sense_version_used(conn, segment_id, term["sense_id"], term["version"])
    conn.commit()
    log.warning("segment_id=%d flagged needs_human after %d iterations", segment_id, MAX_ITERATIONS)
    if prompt_log:
        prompt_log.log_final(
            segment_id=segment_id,
            locator_path=locator,
            status="needs_human",
            chosen_iteration=chosen_iter,
            chosen_draft=final_draft,
        )
    return "needs_human", usages
