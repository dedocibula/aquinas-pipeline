"""Shared base for the text-overlay parsers (Czech Bahounek, English Dominican).

Both overlay an existing Latin-built segment graph: they parse a source into
``OverlayElement``s, look each one up by its ltree locator, and upsert a
``segment_text`` row in their language. They never create segments — that is the
job of ``parser_latin`` (the structural parser), which is intentionally not part
of this hierarchy.

The only behaviour that differs between the two overlay sources is the policy for
a locator with no matching segment (fail-loud vs gap-log vs silent skip) and the
exact gap-log sink. That decision is injected into ``store()`` via an
``on_missing`` callback so the shared lookup-and-upsert loop lives in one place.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from storage.repositories import SegmentRepository


@dataclass
class OverlayElement:
    """A locator paired with the text to overlay onto its existing segment."""

    locator: str
    text: str


class TextOverlayParser(ABC):
    """Base class for parsers that attach a language's text to existing segments."""

    lang: str  # segment_text.lang this parser writes ('cs' | 'en')

    @abstractmethod
    def parse(self, article_locators: list[str]) -> list[OverlayElement]:
        """Parse the source files into overlay elements for the given articles."""

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
