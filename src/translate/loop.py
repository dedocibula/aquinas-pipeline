"""Translation loop — translate_segment() orchestrator.

MAX_ITERATIONS = 3 hard cap. Terminology pre-check runs before every R1 call;
a failure sends the draft back to the translator without calling R1.
The loop writes the best qualifying draft and updates DB state, then commits.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv

from common.lemmatize import lemmatize_latin
from common.pricing import UsageInfo
from storage.db import source_id
from storage.repositories import GlossaryRepository, SegmentRepository
from translate.prechecks import check_terminology_lemma
from translate.prompt_logger import PromptLogger
from translate.reviewer import build_reviewer_turn, call_reviewer_r1
from translate.translator import (
    build_initial_messages,
    call_translator_v3,
)

load_dotenv()

MAX_ITERATIONS = 3
log = logging.getLogger(__name__)


@dataclass
class SegmentOutcome:
    """Per-segment analytics record for the run_segment table (migration 005).

    failure_classes entries are recorded at failure time, one dict per event:
      {"iter": 1, "class": "precheck_terminology", "term": "rozum"}
      {"iter": 2, "class": "reviewer_revision"}
    Known classes: precheck_terminology, reviewer_revision,
    translator_error, reviewer_error, no_source_text.
    """

    segment_id: int
    iterations_used: int = 0
    chosen_iteration: int | None = None
    failure_classes: list[dict] = field(default_factory=list)
    last_feedback: str | None = None


# ── Term lookup ───────────────────────────────────────────────────────────────

_SUFFIX_RE = re.compile(r"\d+$")

# Perfect-passive habere: 'habitum est' / 'habita sunt' ("as was held/stated").
_HABERE_PPP_RE = re.compile(r"\b(habitum|habita)\s+(?:est|sunt)\b", re.IGNORECASE)


def _drop_habere_ppp_constraints(latin: str, constraints: list[dict]) -> list[dict]:
    """Drop 'habitus' constraints whose only evidence is perfect-passive habere.

    CLTK lemmatizes the participle in 'habitum est' / 'habita sunt' to the noun
    *habitus*, so the resolver writes a bogus term_usage row and the pipeline
    then demands 'habitus' in the Slovak draft of a segment that never mentions
    the concept. When every token supporting the constraint is part of such a
    construction, the constraint is false — remove it. Constraints also backed
    by a genuine habitus token elsewhere in the segment are kept.

    TEMPORARY read-time patch: the root cause is the resolver writing the bogus
    term_usage row. Part 1's re-resolution pass will fix resolution with
    pos_tag_latin (participle + esse form never maps to a noun term) and purge
    the existing bad rows — delete this function once the data is clean.
    """
    if not latin or not constraints:
        return constraints
    ppp_tokens = {m.group(1).lower() for m in _HABERE_PPP_RE.finditer(latin)}
    if not ppp_tokens:
        return constraints

    result: list[dict] = []
    for c in constraints:
        stripped = _SUFFIX_RE.sub("", c["latin_lemma"])
        if stripped != "habitus":
            result.append(c)
            continue
        other_evidence = any(
            stripped in {_SUFFIX_RE.sub("", cand) for cand in lemmatize_latin(token)}
            for token in set(re.findall(r"[a-zA-Z]+", latin))
            if token.lower() not in ppp_tokens
        )
        if other_evidence:
            result.append(c)
        else:
            log.info(
                "dropping false 'habitus' constraint — only evidence is "
                "perfect-passive habere ('habitum est'/'habita sunt')"
            )
    return result


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
        if " " in lemma or c.get("category") == "formula":
            result.append(c)
            continue

        stripped = _SUFFIX_RE.sub("", lemma)
        surfaces = sorted(t for t, ls in token_lemmas.items() if stripped in ls)
        if surfaces:
            for surface in surfaces:
                result.append({**c, "latin_lemma": surface})
        else:
            result.append(c)

    return result


def _build_terminology_microedit(failures: list[str]) -> str:
    """Feedback turn for a terminology-only precheck failure."""
    lines = "\n".join(f"  - {f}" for f in failures)
    return (
        "<system_rejection>\n"
        "MACHINE REJECTION — FORBIDDEN SYNONYM DETECTED.\n"
        "The following required Slovak terms are absent from your output:\n"
        f"{lines}\n\n"
        "These are HARD COMPILER LOCKS. There is no acceptable synonym, paraphrase, or omission.\n"
        "Find the concept in your draft and replace it with the exact required Slovak lemma, correctly inflected for grammar.\n"
        "Every listed term MUST appear in your output. No exceptions.\n"
        "</system_rejection>\n\n"
        "Re-output your translation with ONLY the locked terms corrected. "
        "Inflect each required term for Slovak grammar. Do not change any other word."
    )


# ── Main loop ─────────────────────────────────────────────────────────────────


def translate_segment(
    segment_id: int, conn, prompt_log: PromptLogger | None = None
) -> tuple[str, list[UsageInfo], SegmentOutcome]:
    """Translate one segment through the full draft → pre-check → R1 → revise loop.

    Returns (status, usages, outcome) where status is 'translated' or
    'needs_human', usages is the list of UsageInfo from every API call made,
    and outcome is the SegmentOutcome analytics record for run_segment.
    Always commits before returning.
    Raises RuntimeError only if the segment is not found in DB.
    """
    outcome = SegmentOutcome(segment_id=segment_id)
    seg_repo = SegmentRepository(conn)
    seg = seg_repo.get_segment(segment_id)
    if seg is None:
        raise RuntimeError(f"segment_id={segment_id} not found in DB")
    _title_types = ("article_title", "question_title")
    if not seg.latin and not (seg.element_type in _title_types and seg.english):
        log.error("segment_id=%d: no Latin text in DB; flagging needs_human", segment_id)
        seg_repo.update_translation_status(segment_id, "needs_human")
        conn.commit()
        outcome.failure_classes.append({"iter": 0, "class": "no_source_text"})
        return "needs_human", [], outcome

    locked_terms = GlossaryRepository(conn).locked_terms(segment_id)
    # For formula terms, to_prompt_dict shows the human-readable Latin surface
    # (e.g. "Ad nonum sic proceditur") rather than the slug key; NULL category → "term".
    constraints = [c.to_prompt_dict() for c in locked_terms]
    # 'habitum est' is a form of habere, not the noun habitus — drop constraints
    # that have no other textual evidence, for translator, precheck and reviewer alike.
    constraints = _drop_habere_ppp_constraints(seg.latin or "", constraints)
    # Surface-form constraints for the translator: CLTK maps each approved lemma
    # to the inflected forms that actually appear in this segment's Latin text,
    # so the model sees e.g. 'rationem → rozum' rather than 'ratio → rozum'.
    # The reviewer still receives lemma-form constraints (more semantic for auditing).
    translator_constraints = _build_surface_constraints(seg.latin or "", constraints)

    src_model = source_id(conn, "model")

    # Multi-turn message history.  Starts as [system, user]; on each retry the
    # caller appends [assistant: prior_draft, user: feedback] so the model sees
    # its own output as a real assistant turn and the correction as a real user turn.
    messages: list[dict] = build_initial_messages(seg.as_dict(), translator_constraints)

    last_feedback: str | None = None            # most-recent feedback; for reviewer_notes on exhausted path
    precheck_passing_draft: str | None = None   # last draft that cleared ALL pre-checks
    precheck_passing_iter: int | None = None
    fallback_draft: str | None = None           # any draft produced; absolute last resort
    fallback_iter: int | None = None
    usages: list[UsageInfo] = []
    locator = seg.locator_path

    for iteration in range(1, MAX_ITERATIONS + 1):
        outcome.iterations_used = iteration
        # For logging: the system prompt is always messages[0]; the user turn is
        # always the last message (initial request on iter 1, feedback on retries).
        system_prompt = messages[0]["content"] if prompt_log else ""
        user_turn = messages[-1]["content"] if prompt_log else ""

        try:
            draft, t_usage = call_translator_v3(messages)
            usages.append(t_usage)
        except RuntimeError as exc:
            log.error("segment_id=%d iteration=%d translator error: %s", segment_id, iteration, exc)
            outcome.failure_classes.append({"iter": iteration, "class": "translator_error"})
            break

        # Draft is eligible as fallback (written on exhausted path)
        fallback_draft = draft
        fallback_iter = iteration

        terminology_result = check_terminology_lemma(draft, constraints)

        if not terminology_result.ok:
            for term in terminology_result.failed_terms:
                outcome.failure_classes.append(
                    {"iter": iteration, "class": "precheck_terminology", "term": term}
                )
            feedback = _build_terminology_microedit(terminology_result.failures)
            if prompt_log:
                prompt_log.log_iteration(
                    segment_id=segment_id,
                    locator_path=locator,
                    iteration=iteration,
                    system_prompt=system_prompt,
                    user_turn=user_turn,
                    draft=draft,
                    precheck_ok=False,
                    precheck_failures=terminology_result.failures,
                    reviewer_turn=None,
                    verdict=None,
                    notes=None,
                    feedback=feedback,
                )
            last_feedback = feedback
            messages.append({"role": "assistant", "content": draft})
            messages.append({"role": "user", "content": feedback})
            continue  # back to translator; do NOT call R1

        precheck_passing_draft = draft
        precheck_passing_iter = iteration

        latin = seg.latin or ""
        if not latin:
            if seg.element_type in _title_types and precheck_passing_draft is not None:
                # Title segments have no Latin — accept the precheck-passing draft directly.
                seg_repo.write_segment_text(segment_id, "sk", src_model, precheck_passing_draft)
                seg_repo.update_translation_status(segment_id, "translated")
                for term in locked_terms:
                    seg_repo.update_sense_version_used(segment_id, term.sense_id, term.version)
                conn.commit()
                log.info("segment_id=%d title translated (reviewer skipped — no Latin)", segment_id)
                outcome.chosen_iteration = iteration
                return "translated", usages, outcome
            log.error("segment_id=%d iteration=%d: missing Latin text; skipping R1", segment_id, iteration)
            break

        czech = seg.czech
        english = seg.english
        reviewer_turn = build_reviewer_turn(latin, draft, constraints, czech=czech, english=english) if prompt_log else ""

        review = None
        for _attempt in range(2):
            try:
                review = call_reviewer_r1(latin, draft, constraints, czech=czech, english=english)
                if review.usage is not None:
                    usages.append(review.usage)
                break
            except RuntimeError as exc:
                log.error(
                    "segment_id=%d iteration=%d reviewer error (attempt %d): %s",
                    segment_id, iteration, _attempt + 1, exc,
                )
                if _attempt == 0:
                    outcome.failure_classes.append({"iter": iteration, "class": "reviewer_error"})
        if review is None:
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
            seg_repo.write_segment_text(segment_id, "sk", src_model, draft)
            seg_repo.update_translation_status(segment_id, "translated")
            if review.notes:
                seg_repo.write_reviewer_notes(segment_id, review.notes, iteration)
            for term in locked_terms:
                seg_repo.update_sense_version_used(segment_id, term.sense_id, term.version)
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
            outcome.chosen_iteration = iteration
            outcome.last_feedback = None
            return "translated", usages, outcome

        outcome.failure_classes.append({"iter": iteration, "class": "reviewer_revision"})
        last_feedback = review.feedback
        if not last_feedback:
            log.warning("segment_id=%d iter=%d: REVISION_NEEDED with empty feedback; breaking", segment_id, iteration)
            break
        messages.append({"role": "assistant", "content": draft})
        messages.append({"role": "user", "content": last_feedback})

    # Exhausted — write precheck_passing_draft (last to clear all pre-checks) or fall back
    final_draft = precheck_passing_draft if precheck_passing_draft is not None else fallback_draft
    chosen_iter = precheck_passing_iter if precheck_passing_iter is not None else fallback_iter
    outcome.chosen_iteration = chosen_iter
    outcome.last_feedback = last_feedback
    if final_draft is None:
        # No draft was ever produced (e.g., translator raised on every iteration).
        # Still mark needs_human so the segment doesn't stay stuck as 'pending'.
        log.error("segment_id=%d: no draft produced; marking needs_human", segment_id)
        seg_repo.update_translation_status(segment_id, "needs_human")
        conn.commit()
        if prompt_log:
            prompt_log.log_final(
                segment_id=segment_id,
                locator_path=locator,
                status="needs_human",
                chosen_iteration=None,
                chosen_draft=None,
            )
        return "needs_human", usages, outcome

    seg_repo.write_segment_text(segment_id, "sk", src_model, final_draft)
    seg_repo.update_translation_status(segment_id, "needs_human")
    notes_payload: dict = {}
    if last_feedback:
        notes_payload["last_feedback"] = last_feedback
    if notes_payload:
        seg_repo.write_reviewer_notes(segment_id, notes_payload, chosen_iter or MAX_ITERATIONS)
    for term in locked_terms:
        seg_repo.update_sense_version_used(segment_id, term.sense_id, term.version)
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
    return "needs_human", usages, outcome
