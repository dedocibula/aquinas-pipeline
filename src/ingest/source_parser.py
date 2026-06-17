"""Shared text-overlay writer for the Czech (Bahounek) and English (Dominican)
sources.

Both overlay an existing Latin-built segment graph: a source-specific function
parses the source into ``OverlayElement``s, then ``TextOverlayWriter.store()``
looks each one up by its ltree locator and upserts a ``segment_text`` row in the
writer's language. They never create segments — that is the job of
``parser_latin`` (the structural parser), which is intentionally not part of this
hierarchy.

Parsing is *not* shared (the two sources have unrelated HTML shapes), so it stays
in per-source module functions (``parse_bahounek_for_articles`` /
``parse_english_for_articles``); only the lookup-and-upsert loop is common. The
one behaviour that differs between the two sources is the policy for a locator
with no matching segment (fail-loud vs gap-log vs silent skip) and the exact gap-
log sink. That decision is injected into ``store()`` via an ``on_missing``
callback so the shared loop lives in one place.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from storage.repositories import SegmentRepository


@dataclass
class OverlayElement:
    """A locator paired with the text to overlay onto its existing segment."""

    locator: str
    text: str


class TextOverlayWriter:
    """Writes a language's text onto existing segments. Subclass to set ``lang``."""

    lang: str  # segment_text.lang this writer targets ('cs' | 'en')

    def store(
        self,
        conn,
        elements: list[OverlayElement],
        src_id: int,
        on_missing: Callable[[str], None],
    ) -> int:
        """Upsert segment_text rows for every element whose locator has a segment.

        ``on_missing(locator)`` is invoked for an element with no matching
        segment; it logs and returns to skip, or raises to fail loud. Returns the
        number of rows upserted.
        """
        repo = SegmentRepository(conn)
        inserted = 0
        for elem in elements:
            seg_id = repo.get_segment_id_by_locator(elem.locator)
            if seg_id is None:
                on_missing(elem.locator)
                continue
            repo.write_segment_text(seg_id, self.lang, src_id, elem.text)
            inserted += 1
        return inserted
