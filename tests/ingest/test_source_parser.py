"""Unit tests for the shared TextOverlayParser.store() loop."""

from __future__ import annotations

import pytest

from ingest.source_parser import OverlayElement, TextOverlayParser


class _StubParser(TextOverlayParser):
    lang = "cs"

    def parse(self, article_locators):  # pragma: no cover - not exercised here
        return []


def test_store_upserts_found_and_invokes_on_missing(fake_conn):
    # First lookup hits (segment 5), second misses (None).
    conn = fake_conn(fetchone_results=[(5,)])
    elements = [
        OverlayElement("I.q1.a1.respondeo", "found"),
        OverlayElement("I.q9.a9.respondeo", "missing"),
    ]
    missed: list[str] = []

    inserted = _StubParser().store(conn, elements, src_id=3, on_missing=missed.append)

    assert inserted == 1
    assert missed == ["I.q9.a9.respondeo"]
    # The single insert carries the parser's lang and the resolved segment id.
    inserts = [(sql, p) for sql, p in conn.executed if "INSERT INTO segment_text" in sql]
    assert len(inserts) == 1
    assert inserts[0][1] == (5, "cs", "found", 3)


def test_store_propagates_on_missing_raise(fake_conn):
    conn = fake_conn(fetchone_results=[])

    def boom(locator):
        raise RuntimeError(f"no segment for {locator}")

    with pytest.raises(RuntimeError, match="no segment for"):
        _StubParser().store(
            conn, [OverlayElement("I.q9.a9.respondeo", "x")], src_id=1, on_missing=boom
        )
