"""Unit tests for src/storage/models.py — construction, from_row, prompt/dict adapters."""

from __future__ import annotations

from storage.models import Constraint, Segment, Sense, Term

# Column shapes mirror the live repository SQL (GlossaryRepository.load_glossary,
# SegmentRepository.load_body_segments / get_segment, locked_terms).

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


def test_sense_from_row():
    sense = Sense.from_row(SENSE_ROW)
    assert sense.sense_id == 7
    assert sense.cs_content == "rozumová schopnosť"
    assert sense.la_surface == "intellectus"


def test_term_from_row_with_senses():
    sense = Sense.from_row(SENSE_ROW)
    term = Term.from_row(TERM_ROW, (sense,))
    assert term.term_id == 3
    assert term.is_multiword is False
    assert term.senses == (sense,)
    assert term.la_surface is None
    # senses tuple makes the term hashable
    assert hash(term)


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


def test_constraint_null_category_preserved_raw_defaults_in_prompt():
    row = {**LOCKED_TERM_ROW, "category": None, "latin_surface": None}
    c = Constraint.from_row(row)
    # the raw NULL category is preserved on the model …
    assert c.category is None
    # … and only defaulted to "term" at the prompt boundary.
    assert c.to_prompt_dict()["latin_lemma"] == "intellectus"
    assert c.to_prompt_dict()["category"] == "term"
