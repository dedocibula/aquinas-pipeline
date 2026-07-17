"""Unit tests for ingest.apply_new_terms — Stage 6 of the editor glossary
proposals plan (find unapplied add_term proposals, re-resolve, diff, gate).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import ingest.apply_new_terms as ant
from ingest.apply_new_terms import TermApplyResult


def _fake_conn():
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    return conn


# ── find_target_proposals ───────────────────────────────────────────────────


def test_find_target_proposals_filters_already_applied():
    proposals = [
        {"proposal_id": 1, "latin_lemma": "ens"},
        {"proposal_id": 2, "latin_lemma": "actus"},
    ]
    with (
        patch.object(ant, "ProposalRepository") as proposal_repo,
        patch.object(ant, "GlossaryRepository") as glossary_repo,
        patch.object(ant, "TermUsageRepository") as term_usage_repo,
    ):
        proposal_repo.return_value.list_approved_add_terms.return_value = proposals
        glossary_repo.return_value.find_term_by_lemma.side_effect = [10, 20]
        glossary_repo.return_value.sense_ids_for_term.side_effect = [[100], [200]]
        # ens: not yet applied; actus: already has usage rows -> excluded
        term_usage_repo.return_value.any_usage_for_senses.side_effect = [False, True]

        targets = ant.find_target_proposals(object())

    assert targets == [
        {"proposal_id": 1, "latin_lemma": "ens", "term_id": 10, "sense_ids": [100]},
    ]


def test_find_target_proposals_skips_missing_term():
    """apply_add_term always creates the term at approval, but guard defensively."""
    proposals = [{"proposal_id": 1, "latin_lemma": "ens"}]
    with (
        patch.object(ant, "ProposalRepository") as proposal_repo,
        patch.object(ant, "GlossaryRepository") as glossary_repo,
        patch.object(ant, "TermUsageRepository"),
    ):
        proposal_repo.return_value.list_approved_add_terms.return_value = proposals
        glossary_repo.return_value.find_term_by_lemma.return_value = None

        targets = ant.find_target_proposals(object())

    assert targets == []


def test_find_target_proposals_empty_when_no_approved_proposals():
    with patch.object(ant, "ProposalRepository") as proposal_repo:
        proposal_repo.return_value.list_approved_add_terms.return_value = []
        assert ant.find_target_proposals(object()) == []


# ── resolve_and_diff ─────────────────────────────────────────────────────────


def test_resolve_and_diff_intersects_gained_with_already_translated():
    targets = [{"proposal_id": 1, "latin_lemma": "ens", "term_id": 10, "sense_ids": [100]}]
    conn = _fake_conn()
    with (
        patch.object(ant, "get_conn", return_value=conn),
        patch.object(ant, "SegmentRepository") as seg_repo,
        patch.object(ant, "TermUsageRepository") as term_usage_repo,
        patch("ingest.resolver.run") as resolver_run,
    ):
        seg_repo.return_value.get_translated_body_segment_ids.return_value = [1, 2, 3]
        term_usage_repo.return_value.segments_for_senses.return_value = {100: [2, 3, 4]}

        results = ant.resolve_and_diff(targets)

    resolver_run.assert_called_once()
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, TermApplyResult)
    assert r.proposal_id == 1
    # segment 4 excluded: it gained the lock but was never translated
    assert r.gained_segment_ids == [2, 3]


def test_resolve_and_diff_no_gain_when_untranslated_only():
    targets = [{"proposal_id": 1, "latin_lemma": "ens", "term_id": 10, "sense_ids": [100]}]
    conn = _fake_conn()
    with (
        patch.object(ant, "get_conn", return_value=conn),
        patch.object(ant, "SegmentRepository") as seg_repo,
        patch.object(ant, "TermUsageRepository") as term_usage_repo,
        patch("ingest.resolver.run"),
    ):
        seg_repo.return_value.get_translated_body_segment_ids.return_value = []
        term_usage_repo.return_value.segments_for_senses.return_value = {100: [5, 6]}

        results = ant.resolve_and_diff(targets)

    assert results[0].gained_segment_ids == []


# ── preview_gained_cost ──────────────────────────────────────────────────────


def test_preview_gained_cost_excludes_human_edited():
    conn = _fake_conn()
    with (
        patch.object(ant, "get_conn", return_value=conn),
        patch.object(ant, "SegmentRepository") as seg_repo,
        patch.object(ant, "RunRepository") as run_repo,
        patch("translate.run._cost_per_segment", return_value=0.02),
    ):
        seg_repo.return_value.get_human_edited_segments.return_value = [3]
        run_repo.return_value.last_run.return_value = {
            "total_cost_usd": 1.0,
            "total_segments": 50,
        }

        n, cost = ant.preview_gained_cost([1, 2, 3])

    assert n == 2
    assert cost == 2 * 0.02


# ── apply_gained_segments ────────────────────────────────────────────────────


def test_apply_gained_segments_delegates_to_reset_segments():
    conn = _fake_conn()
    with (
        patch.object(ant, "get_conn", return_value=conn),
        patch.object(ant, "SegmentRepository") as seg_repo,
    ):
        seg_repo.return_value.reset_segments.return_value = {1: "pending"}

        result = ant.apply_gained_segments([1])

    assert result == {1: "pending"}
    args, _ = seg_repo.return_value.reset_segments.call_args
    assert args[0] == [1]


# ── sample_locators ──────────────────────────────────────────────────────────


def test_sample_locators_limits_and_preserves_order():
    conn = _fake_conn()
    with (
        patch.object(ant, "get_conn", return_value=conn),
        patch.object(ant, "SegmentRepository") as seg_repo,
    ):
        seg_repo.return_value.get_locators.return_value = {1: "I.q1.a1", 2: "I.q1.a2"}

        result = ant.sample_locators([1, 2, 3], limit=2)

    assert result == ["I.q1.a1", "I.q1.a2"]


def test_sample_locators_empty_ids():
    with patch.object(ant, "get_conn") as get_conn:
        assert ant.sample_locators([]) == []
    get_conn.assert_called_once()
