"""Typed data structures for shared pipeline concepts.

Frozen dataclasses replacing the ad-hoc dicts that flow between the glossary
loader, resolver, and translation loop. This module is *additive*: the dicts are
still produced by the current SQL helpers. Repositories (next phase) will return
these models directly; until then, ``from_row``/``as_dict`` bridge the two
representations so callers can migrate incrementally.

Existing dataclasses defined next to their behavior (Resolution, CheckResult,
ReviewResult, UsageInfo, SegmentOutcome, ArticleResult) are re-exported here so
there is a single import surface for shared shapes.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Re-exported dataclasses (defined alongside their behavior) ─────────────────
# Imported here to give a single import surface; the canonical definitions stay
# in their owning modules. Retyping their dict fields to the models below is
# deferred to the repository phase, where the dict→model boundary flips.
from common.pricing import UsageInfo
from ingest.resolution import Resolution
from translate.loop import SegmentOutcome
from translate.prechecks import CheckResult
from translate.reviewer import ReviewResult
from translate.run import ArticleResult

__all__ = [
    "Sense",
    "Term",
    "Segment",
    "Constraint",
    "Resolution",
    "CheckResult",
    "ReviewResult",
    "UsageInfo",
    "SegmentOutcome",
    "ArticleResult",
]


@dataclass(frozen=True)
class Sense:
    """One sense of a glossary term, with its highest-authority renderings.

    Mirrors the per-sense dict built by ``glossary_repo._load_glossary``: each
    rendering (cs/en/sk) is already collapsed to the top-authority source by the
    LATERAL ORDER BY in that query.
    """

    sense_id: int
    context_label: str | None
    version: int
    cs_lemma: str | None
    cs_content: str | None
    en_cue: str | None
    sk_content: str | None
    la_surface: str | None

    @classmethod
    def from_row(cls, row) -> Sense:
        """Build from a mapping (RealDictRow or dict) keyed by the sense columns."""
        return cls(
            sense_id=row["sense_id"],
            context_label=row["context_label"],
            version=row["version"],
            cs_lemma=row["cs_lemma"],
            cs_content=row["cs_content"],
            en_cue=row["en_cue"],
            sk_content=row["sk_content"],
            la_surface=row["la_surface"],
        )

    def as_dict(self) -> dict:
        """Return the legacy sense-dict shape used by current consumers."""
        return {
            "sense_id": self.sense_id,
            "context_label": self.context_label,
            "version": self.version,
            "cs_lemma": self.cs_lemma,
            "cs_content": self.cs_content,
            "en_cue": self.en_cue,
            "sk_content": self.sk_content,
            "la_surface": self.la_surface,
        }


@dataclass(frozen=True)
class Term:
    """A glossary term and its approved senses.

    Mirrors the per-term dict from ``glossary_repo._load_glossary``; ``senses`` is
    a tuple (not a list) so the whole structure is hashable.
    """

    term_id: int
    latin_lemma: str
    is_multiword: bool
    category: str | None
    la_surface: str | None
    senses: tuple[Sense, ...]

    @classmethod
    def from_row(cls, row, senses: tuple[Sense, ...]) -> Term:
        """Build the term-level fields from a row plus its already-built senses."""
        return cls(
            term_id=row["term_id"],
            latin_lemma=row["latin_lemma"],
            is_multiword=row["is_multiword"],
            category=row["category"],
            la_surface=row["la_surface"],
            senses=tuple(senses),
        )

    def as_dict(self) -> dict:
        """Return the legacy term-dict shape (senses as a list of sense-dicts)."""
        return {
            "term_id": self.term_id,
            "latin_lemma": self.latin_lemma,
            "is_multiword": self.is_multiword,
            "category": self.category,
            "la_surface": self.la_surface,
            "senses": [s.as_dict() for s in self.senses],
        }


@dataclass(frozen=True)
class Segment:
    """A corpus segment with its source-language texts.

    ``_load_segments`` (bulk body load) populates the first six fields only.
    ``get_segment_with_texts`` (single-segment load in the translation loop) also
    carries ``reply_to`` and ``translation_status`` — both optional here so the
    one model covers both producers. (Verified against the loop query and the
    ``v_segment`` view, which additionally exposes slovak_draft/slovak_final.)
    """

    segment_id: int
    locator_path: str
    element_type: str
    latin: str | None
    czech: str | None
    english: str | None
    reply_to: int | None = None
    translation_status: str | None = None

    @classmethod
    def from_row(cls, row) -> Segment:
        """Build from a mapping; reply_to/translation_status default when absent."""
        return cls(
            segment_id=row["segment_id"],
            locator_path=row["locator_path"],
            element_type=row["element_type"],
            latin=row["latin"],
            czech=row["czech"],
            english=row["english"],
            reply_to=_get(row, "reply_to"),
            translation_status=_get(row, "translation_status"),
        )

    def as_dict(self) -> dict:
        """Return the legacy segment-dict shape.

        Optional fields are included only when set, so the result matches
        ``_load_segments`` rows (six keys) or ``get_segment_with_texts`` rows
        (eight keys) depending on how the segment was loaded.
        """
        out = {
            "segment_id": self.segment_id,
            "locator_path": self.locator_path,
            "element_type": self.element_type,
            "latin": self.latin,
            "czech": self.czech,
            "english": self.english,
        }
        if self.reply_to is not None:
            out["reply_to"] = self.reply_to
        if self.translation_status is not None:
            out["translation_status"] = self.translation_status
        return out


@dataclass(frozen=True)
class Constraint:
    """A locked Slovak term the translator must use for a Latin lemma.

    Sourced from ``loop.get_locked_terms``. ``category`` defaults to "term" when
    the glossary row has NULL, matching the prompt-building behavior in
    ``translate_segment``.
    """

    latin_lemma: str
    required_slovak: str
    context_label: str | None
    category: str = "term"
    sense_id: int | None = None
    version: int | None = None
    latin_surface: str | None = None

    @classmethod
    def from_row(cls, row) -> Constraint:
        """Build from a ``get_locked_terms`` row."""
        return cls(
            latin_lemma=row["latin_lemma"],
            required_slovak=row["required_slovak"],
            context_label=row["context_label"],
            category=row["category"] or "term",
            sense_id=_get(row, "sense_id"),
            version=_get(row, "version"),
            latin_surface=_get(row, "latin_surface"),
        )

    def to_prompt_dict(self) -> dict:
        """Return the constraint dict the translator prompt consumes.

        Matches ``translate_segment``: the lemma shown to the model is the Latin
        surface form when one exists, else the lemma.
        """
        return {
            "latin_lemma": self.latin_surface or self.latin_lemma,
            "required_slovak": self.required_slovak,
            "context_label": self.context_label,
            "category": self.category,
        }


def _get(row, key, default=None):
    """Mapping ``.get`` that also works for RealDictRow lacking ``.get``."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default
