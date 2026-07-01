"""Unit tests for src/export/xliff.py — no database required."""

from __future__ import annotations

from unittest.mock import patch

from lxml import etree

from export.xliff import (
    XLIFF_NS,
    _build_unit,
    _q,
    _unit_id,
    export_pars,
    export_pars_bytes,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BASE_ROW = {
    "segment_id": 1,
    "locator_path": "I.q1.a1.arg1",
    "element_type": "arg",
    "translation_status": "translated",
    "reviewer_notes": None,
    "source_text": "Videtur quod non.",
    "source_lang": "la",
    "target_text": "Zdá sa, že nie.",
}


def _make_file_el() -> etree._Element:
    """Return a bare <file> element as the parent for _build_unit."""
    root = etree.Element(_q("xliff"), nsmap={None: XLIFF_NS})
    return etree.SubElement(root, _q("file"), id="I")


def _notes_map(unit: etree._Element) -> dict[str, str]:
    """Return {category: text} for all <note> children of the unit's <notes>."""
    notes_el = unit.find(_q("notes"))
    if notes_el is None:
        return {}
    return {n.get("category"): n.text for n in notes_el}


# ---------------------------------------------------------------------------
# _unit_id
# ---------------------------------------------------------------------------


def test_unit_id():
    assert _unit_id("I.q1.a1.arg1") == "I_q1_a1_arg1"


def test_unit_id_no_dots():
    assert _unit_id("I") == "I"


# ---------------------------------------------------------------------------
# _build_unit
# ---------------------------------------------------------------------------


def test_build_unit_human_preferred():
    row = {**_BASE_ROW, "target_text": "human text"}
    file_el = _make_file_el()
    _build_unit(file_el, row)
    unit = file_el.find(_q("unit"))
    target = unit.find(_q("segment") + "/" + _q("target"))
    assert target.text == "human text"


def test_build_unit_model_fallback():
    row = {**_BASE_ROW, "target_text": "model draft"}
    file_el = _make_file_el()
    _build_unit(file_el, row)
    unit = file_el.find(_q("unit"))
    assert unit.find(_q("segment") + "/" + _q("target")).text == "model draft"


def test_build_unit_english_source():
    row = {**_BASE_ROW, "source_lang": "en", "source_text": "It seems that not."}
    file_el = _make_file_el()
    _build_unit(file_el, row)
    unit = file_el.find(_q("unit"))
    notes = _notes_map(unit)
    assert notes.get("source_lang") == "en"


def test_build_unit_reviewer_notes_null():
    row = {**_BASE_ROW, "reviewer_notes": None}
    file_el = _make_file_el()
    _build_unit(file_el, row)
    unit = file_el.find(_q("unit"))
    notes = _notes_map(unit)
    assert "reviewer_notes" not in notes


def test_build_unit_reviewer_notes_present():
    row = {**_BASE_ROW, "reviewer_notes": {"iteration": 2, "last_feedback": "bad"}}
    file_el = _make_file_el()
    _build_unit(file_el, row)
    unit = file_el.find(_q("unit"))
    notes = _notes_map(unit)
    assert "reviewer_notes" in notes
    assert "iteration" in notes["reviewer_notes"]


# ---------------------------------------------------------------------------
# export_pars (file write path)
# ---------------------------------------------------------------------------

_ROWS = [
    {**_BASE_ROW, "segment_id": 1, "locator_path": "I.q1.a1.arg1"},
    {**_BASE_ROW, "segment_id": 2, "locator_path": "I.q1.a1.respondeo",
     "element_type": "respondeo"},
]


def test_export_pars_xml_valid(tmp_path):
    with patch("export.xliff._fetch_rows", return_value=_ROWS):
        export_pars(None, 1, "I", tmp_path)
    tree = etree.parse(str(tmp_path / "I.xlf"))
    root = tree.getroot()
    assert root.tag == _q("xliff")
    assert root.get("version") == "2.0"
    assert root.get("srcLang") == "la"
    assert root.get("trgLang") == "sk"


def test_export_pars_unit_count(tmp_path):
    with patch("export.xliff._fetch_rows", return_value=_ROWS):
        export_pars(None, 1, "I", tmp_path)
    tree = etree.parse(str(tmp_path / "I.xlf"))
    units = tree.findall(f".//{_q('unit')}")
    assert len(units) == len(_ROWS)


def test_export_pars_filename(tmp_path):
    with patch("export.xliff._fetch_rows", return_value=_ROWS):
        path = export_pars(None, 1, "I", tmp_path)
    assert path == tmp_path / "I.xlf"
    assert path.exists()


# ---------------------------------------------------------------------------
# export_pars_bytes (server / in-memory path)
# ---------------------------------------------------------------------------


def test_export_pars_bytes_returns_valid_xml():
    with patch("export.xliff._fetch_rows", return_value=_ROWS):
        data = export_pars_bytes(None, 1, "I")
    assert isinstance(data, bytes)
    root = etree.fromstring(data)
    assert root.tag == _q("xliff")
    assert root.get("version") == "2.0"
    units = root.findall(f".//{_q('unit')}")
    assert len(units) == len(_ROWS)
