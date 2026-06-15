"""Unit tests for src/common/models.py — construction, from_row, as_dict round-trip."""

from __future__ import annotations

from common.models import (
    ArticleResult,
    CheckResult,
    Constraint,
    Resolution,
    ReviewResult,
    Segment,
    SegmentOutcome,
    Sense,
    Term,
    UsageInfo,
)

# Column shapes mirror the live SQL helpers (glossary_repo._load_glossary,
# _load_segments, loop.get_segment_with_texts, loop.get_locked_terms).

SENSE_ROW = {
    "sense_id": 7,
    "context_label": "intellectus",
    "version": 2,
    "cs_lemma": "rozum",
    "cs_content": "rozumová schopnosť",
    "en_cue": "intellect",
    "sk_content": "rozum",
    "la_surface": "intellectus",
}

TERM_ROW = {
    "term_id": 3,
    "latin_lemma": "intellectus",
    "is_multiword": False,
    "category": "faculty",
    "la_surface": None,
}

SEGMENT_BULK_ROW = {
    "segment_id": 42,
    "locator_path": "I.q1.a1.resp",
    "element_type": "respondeo",
    "latin": "Respondeo dicendum...",
    "czech": "Odpovídám...",
    "english": "I answer that...",
}

SEGMENT_LOOP_ROW = {
    **SEGMENT_BULK_ROW,
    "reply_to": 41,
    "translation_status": "pending",
}

LOCKED_TERM_ROW = {
    "latin_lemma": "intellectus",
    "category": "faculty",
    "latin_surface": "intellectu",
    "required_slovak": "rozum",
    "sense_id": 7,
    "version": 2,
    "context_label": "intellectus",
}


def test_sense_from_row_and_roundtrip():
    sense = Sense.from_row(SENSE_ROW)
    assert sense.sense_id == 7
    assert sense.cs_content == "rozumová schopnosť"
    assert sense.as_dict() == SENSE_ROW


def test_term_from_row_with_senses():
    sense = Sense.from_row(SENSE_ROW)
    term = Term.from_row(TERM_ROW, (sense,))
    assert term.term_id == 3
    assert term.is_multiword is False
    assert term.senses == (sense,)
    # senses tuple makes the term hashable
    assert hash(term)
    assert term.as_dict() == {**TERM_ROW, "senses": [SENSE_ROW]}


def test_segment_bulk_row_omits_optional_fields():
    seg = Segment.from_row(SEGMENT_BULK_ROW)
    assert seg.reply_to is None
    assert seg.translation_status is None
    assert seg.as_dict() == SEGMENT_BULK_ROW


def test_segment_loop_row_carries_optional_fields():
    seg = Segment.from_row(SEGMENT_LOOP_ROW)
    assert seg.reply_to == 41
    assert seg.translation_status == "pending"
    assert seg.as_dict() == SEGMENT_LOOP_ROW


def test_constraint_from_row_and_prompt_dict():
    c = Constraint.from_row(LOCKED_TERM_ROW)
    assert c.required_slovak == "rozum"
    assert c.sense_id == 7
    # prompt uses the Latin surface form when present
    assert c.to_prompt_dict() == {
        "latin_lemma": "intellectu",
        "required_slovak": "rozum",
        "context_label": "intellectus",
        "category": "faculty",
    }


def test_constraint_null_category_defaults_to_term():
    row = {**LOCKED_TERM_ROW, "category": None, "latin_surface": None}
    c = Constraint.from_row(row)
    assert c.category == "term"
    # with no surface, the prompt lemma falls back to the lemma itself
    assert c.to_prompt_dict()["latin_lemma"] == "intellectus"
    assert c.to_prompt_dict()["category"] == "term"


def test_reexports_are_the_canonical_types():
    # Re-exported dataclasses are the same objects, not copies.
    from common.pricing import UsageInfo as CanonicalUsageInfo
    from ingest.resolution import Resolution as CanonicalResolution
    from translate.loop import SegmentOutcome as CanonicalSegmentOutcome
    from translate.prechecks import CheckResult as CanonicalCheckResult
    from translate.reviewer import ReviewResult as CanonicalReviewResult
    from translate.run import ArticleResult as CanonicalArticleResult

    assert UsageInfo is CanonicalUsageInfo
    assert Resolution is CanonicalResolution
    assert SegmentOutcome is CanonicalSegmentOutcome
    assert CheckResult is CanonicalCheckResult
    assert ReviewResult is CanonicalReviewResult
    assert ArticleResult is CanonicalArticleResult
