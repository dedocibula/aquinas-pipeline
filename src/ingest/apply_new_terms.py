"""New-term corpus application (Stage 6 of the editor glossary proposals plan).

An approved ``add_term`` proposal creates the glossary entry (Stage 2's
``apply_add_term``) but never touches segments (D7) — a newly inserted
term_usage row would carry the current version, not stale, so there is
nothing to restage yet. The resolver only *detects* the new term on a full
corpus re-resolve (``ingest.resolver.run``), which is free compute but slow
and corpus-wide. This module: finds add_term proposals not yet applied,
re-resolves, diffs which already-translated segments gained a lock, and
(owner-gated, cost-previewed) resets exactly those segments so the next
translate run regenerates them under the new constraint.

Known limitation: CLTK lemmatization misses some surface forms (e.g. `ente`
does not lemmatize to `ens`, `subiectum` lemmatizes to the verb `subicio`),
so a new term only applies where the resolver can actually detect it.
Lemmatization overrides are a separate, deferred workstream
(``.claude/m5_reviewer_corrections_plan.md`` §6.2), not this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from storage.db import get_conn
from storage.repositories import (
    GlossaryRepository,
    ProposalRepository,
    RunRepository,
    SegmentRepository,
    TermUsageRepository,
)


@dataclass
class TermApplyResult:
    proposal_id: int
    latin_lemma: str
    term_id: int
    sense_ids: list[int]
    gained_segment_ids: list[int] = field(default_factory=list)


def find_target_proposals(conn) -> list[dict]:
    """Approved add_term proposals whose term has zero term_usage rows yet.

    A proposal drops out of this list forever once its term is applied — no
    "already applied" bookkeeping is needed beyond term_usage itself.
    """
    glossary = GlossaryRepository(conn)
    tu = TermUsageRepository(conn)
    targets = []
    for p in ProposalRepository(conn).list_approved_add_terms():
        term_id = glossary.find_term_by_lemma(p["latin_lemma"])
        if term_id is None:
            # Should not happen — apply_add_term creates the term at approval.
            continue
        sense_ids = glossary.sense_ids_for_term(term_id)
        if tu.any_usage_for_senses(sense_ids):
            continue
        targets.append(
            {
                "proposal_id": p["proposal_id"],
                "latin_lemma": p["latin_lemma"],
                "term_id": term_id,
                "sense_ids": sense_ids,
            }
        )
    return targets


def resolve_and_diff(targets: list[dict], work_id: int = 1) -> list[TermApplyResult]:
    """Run the full resolver, then report which already-translated segments
    gained a lock for each target term's senses.

    Because ``find_target_proposals`` only returns terms with zero prior
    term_usage rows, every post-resolve row for a target sense is new by
    construction — no separate before/after snapshot is needed.
    """
    from ingest.resolver import run as resolver_run

    all_sense_ids = [sid for t in targets for sid in t["sense_ids"]]

    with get_conn() as conn:
        already_translated = set(SegmentRepository(conn).get_translated_body_segment_ids(work_id))

    resolver_run()

    with get_conn() as conn:
        gained_by_sense = TermUsageRepository(conn).segments_for_senses(all_sense_ids)

    results = []
    for t in targets:
        gained = set()
        for sense_id in t["sense_ids"]:
            gained |= set(gained_by_sense.get(sense_id, []))
        gained &= already_translated
        results.append(
            TermApplyResult(
                proposal_id=t["proposal_id"],
                latin_lemma=t["latin_lemma"],
                term_id=t["term_id"],
                sense_ids=t["sense_ids"],
                gained_segment_ids=sorted(gained),
            )
        )
    return results


def preview_gained_cost(gained_segment_ids: list[int]) -> tuple[int, float]:
    """Read-only cost estimate for resetting exactly the gained segments."""
    from translate.run import _cost_per_segment

    with get_conn() as conn:
        seg_repo = SegmentRepository(conn)
        human_edited = set(seg_repo.get_human_edited_segments(gained_segment_ids))
        payable = [s for s in gained_segment_ids if s not in human_edited]
        cost_per_segment = _cost_per_segment(RunRepository(conn).last_run())
    return len(payable), len(payable) * cost_per_segment


def apply_gained_segments(segment_ids: list[int]) -> dict[int, str]:
    """Reset exactly the gained segments (human-edited ones flagged instead)."""
    with get_conn() as conn:
        return SegmentRepository(conn).reset_segments(
            segment_ids, "new glossary term applied — verify under updated constraint"
        )


def sample_locators(segment_ids: list[int], limit: int = 5) -> list[str]:
    """A few locator_paths for a human-readable report line."""
    sample = segment_ids[:limit]
    with get_conn() as conn:
        locators = SegmentRepository(conn).get_locators(sample)
    return [locators[s] for s in sample if s in locators]
