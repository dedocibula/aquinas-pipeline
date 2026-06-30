"""
Tests for src/review/import_approvals.py — pure logic, no real gspread, no DB.
"""

from __future__ import annotations

from review.import_approvals import (
    COLS,
    load_approved_rows,
    process_approval,
    process_new_term,
)
from review.sheets import HEADER

# DB and worksheet fakes come from the shared fixtures in tests/conftest.py
# (fake_conn / fake_worksheet). FakeConn shares one cursor across cursor()
# calls, so executed/fetchone sequencing accumulates exactly as the real code
# threads multiple cursors over one connection.

# ── Sheet row helper ──────────────────────────────────────────────────────────


def _make_sheet_row(
    approved: str = "TRUE",
    latin: str = "ratio",
    latin_text: str = "ratio",
    context_label: str = "",
    proposed_slovak: str = "rozum",
    sense_id: int = 101,
    db_version: int = 1,
    category: str = "",
) -> list:
    row = [""] * 15
    row[COLS["approved"]] = approved
    row[COLS["category"]] = category
    row[COLS["latin_lemma"]] = latin
    row[COLS["latin_text"]] = latin_text
    row[COLS["context_label"]] = context_label
    row[COLS["proposed_slovak"]] = proposed_slovak
    row[COLS["sense_id"]] = str(sense_id)
    row[COLS["db_version"]] = str(db_version)
    return row


def _new_term_row(
    approved: str = "TRUE",
    latin: str = "circe",
    latin_text: str = "",
    context_label: str = "",
    proposed_slovak: str = "",
    category: str = "name",
) -> list:
    """Sheet row for the new-term creation path: sense_id and db_version are blank."""
    row = [""] * 15
    row[COLS["approved"]] = approved
    row[COLS["category"]] = category
    row[COLS["latin_lemma"]] = latin
    row[COLS["latin_text"]] = latin_text
    row[COLS["context_label"]] = context_label
    row[COLS["proposed_slovak"]] = proposed_slovak
    # sense_id and db_version intentionally left blank
    return row


# ── load_approved_rows ────────────────────────────────────────────────────────


def test_load_approved_rows_skips_header(fake_worksheet):
    ws = fake_worksheet(rows=[HEADER, _make_sheet_row(approved="TRUE")])
    rows = load_approved_rows(ws)
    assert len(rows) == 1


def test_load_approved_rows_filters_unticked(fake_worksheet):
    ws = fake_worksheet(rows=[
        HEADER,
        _make_sheet_row(approved="TRUE", sense_id=1),
        _make_sheet_row(approved="FALSE", sense_id=2),
        _make_sheet_row(approved="", sense_id=3),
    ])
    rows = load_approved_rows(ws)
    assert len(rows) == 1
    assert rows[0]["sense_id"] == 1


def test_load_approved_rows_truthy_variants(fake_worksheet):
    for val in ("TRUE", "True", "true", "1", "YES", "yes"):
        ws = fake_worksheet(rows=[HEADER, _make_sheet_row(approved=val, sense_id=99)])
        rows = load_approved_rows(ws)
        assert len(rows) == 1, f"Expected 1 row for approved={val!r}"


def test_load_approved_rows_no_dead_is_not_true_branch(fake_worksheet):
    """Approved filtering uses only _TRUTHY set — no identity check on True."""
    ws = fake_worksheet(rows=[HEADER, _make_sheet_row(approved="FALSE", sense_id=5)])
    rows = load_approved_rows(ws)
    assert rows == []


def test_load_approved_rows_blank_sense_id_with_latin_lemma_is_new_term_row(fake_worksheet):
    """Blank sense_id + filled latin_lemma → new-term row included with sense_id=None."""
    ws = fake_worksheet(rows=[HEADER, _new_term_row(latin="circe")])
    rows = load_approved_rows(ws)
    assert len(rows) == 1
    assert rows[0]["sense_id"] is None
    assert rows[0]["latin_lemma"] == "circe"


def test_load_approved_rows_skips_blank_sense_id_and_blank_latin_lemma(fake_worksheet):
    """Both sense_id and latin_lemma blank → nothing useful in the row; skip it."""
    row = _new_term_row()
    row[COLS["latin_lemma"]] = ""
    ws = fake_worksheet(rows=[HEADER, row])
    assert load_approved_rows(ws) == []


def test_load_approved_rows_parses_ints(fake_worksheet):
    ws = fake_worksheet(rows=[HEADER, _make_sheet_row(sense_id=42, db_version=3)])
    rows = load_approved_rows(ws)
    assert rows[0]["sense_id"] == 42
    assert rows[0]["db_version"] == 3


def test_load_approved_rows_blank_db_version_parses_as_none(fake_worksheet):
    row = _make_sheet_row(approved="TRUE")
    row[COLS["db_version"]] = ""
    ws = fake_worksheet(rows=[HEADER, row])
    rows = load_approved_rows(ws)
    assert rows[0]["db_version"] is None


def test_load_approved_rows_returns_proposed_slovak(fake_worksheet):
    ws = fake_worksheet(rows=[HEADER, _make_sheet_row(proposed_slovak="milosť")])
    rows = load_approved_rows(ws)
    assert rows[0]["proposed_slovak"] == "milosť"


def test_load_approved_rows_returns_context_label(fake_worksheet):
    ws = fake_worksheet(rows=[HEADER, _make_sheet_row(context_label="sanctifying grace")])
    rows = load_approved_rows(ws)
    assert rows[0]["context_label"] == "sanctifying grace"


def test_load_approved_rows_empty_context_label(fake_worksheet):
    ws = fake_worksheet(rows=[HEADER, _make_sheet_row(context_label="")])
    rows = load_approved_rows(ws)
    assert rows[0]["context_label"] == ""


def test_load_approved_rows_empty_sheet(fake_worksheet):
    ws = fake_worksheet(rows=[HEADER])
    assert load_approved_rows(ws) == []


# ── process_approval ─────────────────────────────────────────────────────────


def _row(sense_id=101, proposed_slovak="rozum", db_version=1, latin_lemma="ratio", context_label=""):
    return {
        "sense_id": sense_id,
        "proposed_slovak": proposed_slovak,
        "context_label": context_label,
        "db_version": db_version,
        "latin_lemma": latin_lemma,
    }


def test_process_approval_ok_proposed_sense_bumps(fake_conn):
    """Approving a proposed sense returns OK and always bumps the version."""
    conn = fake_conn(fetchone_results=[
        (101, 1, "proposed"),   # get_current_sense
        (2,),                   # bump_sense_version RETURNING version
    ])
    status, bumped = process_approval(conn, _row(proposed_slovak="nový_rozum"), human_src_id=6)
    assert status == "OK"
    assert bumped is True


def test_process_approval_version_mismatch_proceeds(fake_conn):
    """A version mismatch on a proposed sense is no longer a conflict — approval proceeds."""
    conn = fake_conn(fetchone_results=[
        (101, 3, "proposed"),   # DB version 3, sheet has 1 — mismatch, but still proposed
        (4,),                   # bump_sense_version RETURNING version
    ])
    status, bumped = process_approval(conn, _row(db_version=1), human_src_id=6)
    assert status == "OK"
    assert bumped is True


def test_process_approval_blank_db_version_is_conflict(fake_conn):
    """Blank db_version (None) must be treated as CONFLICT, not bypass."""
    conn = fake_conn(fetchone_results=[
        (101, 1, "proposed"),
    ])
    status, bumped = process_approval(conn, _row(db_version=None), human_src_id=6)
    assert status == "CONFLICT"
    assert bumped is False
    assert len(conn.executed) == 1


def test_process_approval_already_confirmed_on_rerun(fake_conn):
    """Re-run: already approved and SK unchanged → ALREADY_CONFIRMED, no version bump."""
    conn = fake_conn(fetchone_results=[
        (101, 2, "approved"),   # get_current_sense
        ("rozum",),             # get_sk_rendering_content — same as _row default → no bump
    ])
    status, bumped = process_approval(conn, _row(db_version=1), human_src_id=6)
    assert status == "ALREADY_CONFIRMED"
    assert bumped is False


def test_process_approval_not_found(fake_conn):
    conn = fake_conn(fetchone_results=[None])
    status, bumped = process_approval(conn, _row(sense_id=999), human_src_id=6)
    assert status == "NOT_FOUND"
    assert bumped is False
    assert len(conn.executed) == 1


def test_process_approval_writes_human_rendering(fake_conn):
    conn = fake_conn(fetchone_results=[
        (101, 1, "proposed"),   # get_current_sense
        ("rozum",),             # get_model_rendering (same → no bump)
    ])
    process_approval(conn, _row(proposed_slovak="rozum"), human_src_id=6)
    sqls = [e[0] for e in conn.executed]
    insert_sqls = [s for s in sqls if "INSERT INTO sense_rendering" in s]
    assert len(insert_sqls) == 1
    params = conn.executed[1][1]  # second execute = write_human_rendering
    assert "rozum" in params
    assert 6 in params


def test_process_approval_updates_status_to_approved(fake_conn):
    conn = fake_conn(fetchone_results=[
        (101, 1, "proposed"),
        ("rozum",),
    ])
    process_approval(conn, _row(), human_src_id=6)
    sqls = [e[0] for e in conn.executed]
    update_sqls = [s for s in sqls if "UPDATE glossary_sense" in s and "status" in s]
    assert len(update_sqls) == 1
    status_params = [e[1] for e in conn.executed if "status" in e[0] and "UPDATE" in e[0]]
    assert any("approved" in str(p) for p in status_params)


def test_process_approval_krystal_term_bumps_when_reviewer_edits(fake_conn):
    """Krystal term: if reviewer changes proposed_slovak → version bumps."""
    conn = fake_conn(fetchone_results=[
        (101, 1, "proposed"),     # version matches
        ("bytie",),               # Krystal rendering
        (2,),                     # bump_sense_version RETURNING
    ])
    status, bumped = process_approval(conn, _row(proposed_slovak="súcno", db_version=1), human_src_id=6)
    assert status == "OK"
    assert bumped is True


# ── process_approval — context_label write-back ───────────────────────────────


def test_process_approval_writes_context_label(fake_conn):
    """A non-empty context_label in the row is written to glossary_sense."""
    conn = fake_conn(fetchone_results=[
        (101, 1, "proposed"),
        ("rozum",),
    ])
    process_approval(conn, _row(context_label="sanctifying grace"), human_src_id=6)
    label_updates = [
        e for e in conn.executed
        if "UPDATE glossary_sense" in e[0] and "context_label" in e[0]
    ]
    assert len(label_updates) == 1
    sql, params = label_updates[0]
    assert params == ("sanctifying grace", 101)


def test_process_approval_empty_context_label_writes_null(fake_conn):
    """Empty string context_label writes NULL, not empty string."""
    conn = fake_conn(fetchone_results=[
        (101, 1, "proposed"),
        ("rozum",),
    ])
    process_approval(conn, _row(context_label=""), human_src_id=6)
    label_updates = [
        e for e in conn.executed
        if "UPDATE glossary_sense" in e[0] and "context_label" in e[0]
    ]
    assert len(label_updates) == 1
    _, params = label_updates[0]
    assert params[0] is None  # NULL, not ""


# ── process_approval — latin_text import semantics ───────────────────────────


def _row_with_surface(**overrides):
    base = {
        "sense_id": 101,
        "proposed_slovak": "rozum",
        "context_label": "",
        "db_version": 1,
        "latin_lemma": "ratio",
        "latin_text": "ratio",
    }
    base.update(overrides)
    return base


def test_process_approval_latin_text_unchanged_no_surface_write(fake_conn):
    """When latin_text matches current LA surface, no write is performed."""
    conn = fake_conn(fetchone_results=[
        (101, 1, "proposed"),   # get_current_sense
        ("rozum",),             # get_model_rendering (same SK content)
        ("ratio",),             # get_la_surface → same as latin_text
    ])
    status, bumped = process_approval(conn, _row_with_surface(), human_src_id=6)
    assert status == "OK"
    updates_la = [e for e in conn.executed if "UPDATE glossary_term" in e[0] and "la_surface" in e[0]]
    assert len(updates_la) == 0


def test_process_approval_latin_text_changed_writes_la_surface(fake_conn):
    """When latin_text differs from DB LA surface, glossary_term.la_surface is updated."""
    conn = fake_conn(fetchone_results=[
        (101, 1, "proposed"),                   # get_current_sense
        ("rozum",),                             # get_model_rendering (same → no SK bump)
        ("old_ratio",),                         # get_la_surface → different
        (False, "term"),                        # get_term_flags → singleword term
    ])
    status, bumped = process_approval(conn, _row_with_surface(latin_text="ratio"), human_src_id=6)
    assert status == "OK"
    updates_la = [e for e in conn.executed if "UPDATE glossary_term" in e[0] and "la_surface" in e[0]]
    assert len(updates_la) == 1


def test_process_approval_latin_text_changed_multiword_bumps_version(fake_conn):
    """LA surface change for a multiword term triggers a version bump."""
    conn = fake_conn(fetchone_results=[
        (101, 1, "proposed"),
        ("actus essendi",), # same SK
        ("old surface",),   # different LA surface
        (True, "term"),     # is_multiword=True
        (2,),               # bump RETURNING
    ])
    status, bumped = process_approval(conn, _row_with_surface(latin_text="actus essendi"), human_src_id=6)
    assert status == "OK"
    assert bumped is True


def test_process_approval_latin_text_changed_formula_bumps_version(fake_conn):
    """LA surface change for a formula term triggers a version bump."""
    conn = fake_conn(fetchone_results=[
        (101, 1, "proposed"),
        ("Odpovedám",),         # same SK
        ("old formula",),       # different LA surface
        (True, "formula"),      # is_multiword=True + formula category
        (2,),                   # bump RETURNING
    ])
    status, bumped = process_approval(conn, _row_with_surface(latin_text="Respondeo dicendum quod"), human_src_id=6)
    assert status == "OK"
    assert bumped is True


def test_process_approval_sk_and_la_change_only_bumps_once(fake_conn):
    """If both SK and LA changed, version is bumped once (SK bump already covers it)."""
    conn = fake_conn(fetchone_results=[
        (101, 1, "proposed"),
        ("old_rozum",),         # different SK → SK bump
        (3,),                   # bump_sense_version RETURNING
        ("old surface",),       # different LA surface
        (True, "formula"),      # formula → LA would bump too, but already bumped
    ])
    row = _row_with_surface(proposed_slovak="new_rozum", latin_text="new surface")
    status, bumped = process_approval(conn, row, human_src_id=6)
    assert status == "OK"
    assert bumped is True
    bump_calls = [e for e in conn.executed if "version = version + 1" in e[0]]
    assert len(bump_calls) == 1


def test_process_approval_empty_latin_text_skips_la_processing(fake_conn):
    """Empty or blank latin_text is treated as None — LA processing is skipped."""
    conn = fake_conn(fetchone_results=[
        (101, 1, "proposed"),
        ("rozum",),
    ])
    row = _row_with_surface(latin_text="")
    status, bumped = process_approval(conn, row, human_src_id=6)
    assert status == "OK"
    assert bumped is True
    # No LA surface queries or writes should have been made
    la_queries = [e for e in conn.executed if "la_surface" in e[0]]
    assert len(la_queries) == 0
    # Approval always bumps once, regardless of LA processing being skipped.
    bump_calls = [e for e in conn.executed if "version = version + 1" in e[0]]
    assert len(bump_calls) == 1


# ── load_approved_rows — new-term rows ───────────────────────────────────────


def test_load_approved_rows_new_term_row_parses_category(fake_worksheet):
    ws = fake_worksheet(rows=[HEADER, _new_term_row(latin="circe", category="name")])
    rows = load_approved_rows(ws)
    assert rows[0]["category"] == "name"


def test_load_approved_rows_new_term_row_blank_category_is_empty_string(fake_worksheet):
    ws = fake_worksheet(rows=[HEADER, _new_term_row(latin="circe", category="")])
    rows = load_approved_rows(ws)
    assert rows[0]["category"] == ""


def test_load_approved_rows_existing_row_parses_category(fake_worksheet):
    ws = fake_worksheet(rows=[HEADER, _make_sheet_row(category="term")])
    rows = load_approved_rows(ws)
    assert rows[0]["category"] == "term"


def test_load_approved_rows_mixed_new_and_existing(fake_worksheet):
    """Sheet may contain both existing-sense rows and new-term rows."""
    ws = fake_worksheet(rows=[
        HEADER,
        _make_sheet_row(sense_id=101),
        _new_term_row(latin="circe"),
    ])
    rows = load_approved_rows(ws)
    assert len(rows) == 2
    existing = [r for r in rows if r["sense_id"] is not None]
    new_term = [r for r in rows if r["sense_id"] is None]
    assert len(existing) == 1 and existing[0]["sense_id"] == 101
    assert len(new_term) == 1 and new_term[0]["latin_lemma"] == "circe"


# ── process_new_term ─────────────────────────────────────────────────────────


def _new_row(**overrides):
    base = {
        "latin_lemma": "circe",
        "context_label": "",
        "proposed_slovak": "Kirke",
        "category": "name",
        "latin_text": "",
    }
    base.update(overrides)
    return base


def test_process_new_term_created_inserts_term_sense_and_rendering(fake_conn):
    """Brand-new latin_lemma: INSERT term, INSERT sense, INSERT rendering."""
    conn = fake_conn(fetchone_results=[
        None,   # find_term_by_lemma → not found
        (1,),   # insert_glossary_term RETURNING term_id
        (10,),  # insert_glossary_sense RETURNING sense_id
    ])
    status = process_new_term(conn, _new_row(), human_src_id=6)
    assert status == "CREATED"
    sqls = [e[0] for e in conn.executed]
    assert any("INSERT INTO glossary_term" in s for s in sqls)
    assert any("INSERT INTO glossary_sense" in s for s in sqls)
    assert any("INSERT INTO sense_rendering" in s for s in sqls)


def test_process_new_term_created_no_rendering_when_sk_blank(fake_conn):
    """New term with blank proposed_slovak: no sense_rendering INSERT."""
    conn = fake_conn(fetchone_results=[
        None,
        (1,),
        (10,),
    ])
    status = process_new_term(conn, _new_row(proposed_slovak=""), human_src_id=6)
    assert status == "CREATED"
    sqls = [e[0] for e in conn.executed]
    assert not any("INSERT INTO sense_rendering" in s for s in sqls)


def test_process_new_term_created_passes_category_and_la_surface(fake_conn):
    """Category and la_surface from the sheet row are passed to INSERT."""
    conn = fake_conn(fetchone_results=[None, (1,), (10,)])
    process_new_term(conn, _new_row(category="name", latin_text="Circe, Circes"), human_src_id=6)
    term_insert = next(e for e in conn.executed if "INSERT INTO glossary_term" in e[0])
    _, params = term_insert
    assert "name" in params
    assert "Circe, Circes" in params


def test_process_new_term_created_context_label_null_when_blank(fake_conn):
    """Blank context_label in row → NULL stored in glossary_sense."""
    conn = fake_conn(fetchone_results=[None, (1,), (10,)])
    process_new_term(conn, _new_row(context_label=""), human_src_id=6)
    sense_insert = next(e for e in conn.executed if "INSERT INTO glossary_sense" in e[0])
    _, params = sense_insert
    assert params[1] is None  # context_label position


def test_process_new_term_sense_added_reuses_existing_term(fake_conn):
    """Term already in DB, no matching sense: INSERT only the sense."""
    conn = fake_conn(fetchone_results=[
        (5,),   # find_term_by_lemma → term_id=5
        None,   # find_sense_by_label → not found
        (20,),  # insert_glossary_sense
    ])
    status = process_new_term(conn, _new_row(context_label="mythological"), human_src_id=6)
    assert status == "SENSE_ADDED"
    sqls = [e[0] for e in conn.executed]
    assert not any("INSERT INTO glossary_term" in s for s in sqls)
    assert any("INSERT INTO glossary_sense" in s for s in sqls)


def test_process_new_term_sense_added_writes_sk_when_provided(fake_conn):
    conn = fake_conn(fetchone_results=[
        (5,),
        None,
        (20,),
    ])
    process_new_term(conn, _new_row(proposed_slovak="Kirke"), human_src_id=6)
    assert any("INSERT INTO sense_rendering" in e[0] for e in conn.executed)


def test_process_new_term_updated_bumps_version_when_sk_changes(fake_conn):
    """Existing sense with different SK: write new rendering and bump version."""
    conn = fake_conn(fetchone_results=[
        (5,),
        {"sense_id": 20, "version": 1, "status": "approved", "context_label": "mythological"},
        ("old_Kirke",),   # get_sk_rendering_content → different
        (2,),             # bump_sense_version
    ])
    status = process_new_term(
        conn, _new_row(proposed_slovak="Kirke", context_label="mythological"), human_src_id=6
    )
    assert status == "UPDATED"
    assert any("version = version + 1" in e[0] for e in conn.executed)
    assert any("INSERT INTO sense_rendering" in e[0] for e in conn.executed)


def test_process_new_term_updated_no_version_bump_when_sk_unchanged(fake_conn):
    """Existing sense with identical SK: no version bump."""
    conn = fake_conn(fetchone_results=[
        (5,),
        {"sense_id": 20, "version": 1, "status": "approved", "context_label": None},
        ("Kirke",),   # get_sk_rendering_content → same as proposed
    ])
    process_new_term(conn, _new_row(proposed_slovak="Kirke"), human_src_id=6)
    assert not any("version = version + 1" in e[0] for e in conn.executed)


def test_process_new_term_updated_approves_non_approved_sense(fake_conn):
    """Existing sense still 'proposed' gets approved on update."""
    conn = fake_conn(fetchone_results=[
        (5,),
        {"sense_id": 20, "version": 1, "status": "proposed", "context_label": None},
        ("Kirke",),   # get_sk_rendering_content → unchanged
    ])
    process_new_term(conn, _new_row(proposed_slovak="Kirke"), human_src_id=6)
    status_updates = [e for e in conn.executed if "UPDATE glossary_sense SET status" in e[0]]
    assert len(status_updates) == 1
    assert "approved" in status_updates[0][1]


def test_process_new_term_updated_la_surface_written_when_changed(fake_conn):
    """latin_text differs from stored la_surface → glossary_term updated."""
    conn = fake_conn(fetchone_results=[
        (5,),
        {"sense_id": 20, "version": 1, "status": "approved", "context_label": None},
        ("Kirke",),       # get_sk_rendering_content → unchanged
        ("old surface",), # get_la_surface → different
    ])
    process_new_term(conn, _new_row(proposed_slovak="Kirke", latin_text="Circe, Circes"), human_src_id=6)
    la_updates = [e for e in conn.executed if "UPDATE glossary_term" in e[0] and "la_surface" in e[0]]
    assert len(la_updates) == 1


def test_process_new_term_no_lemma_returns_no_lemma(fake_conn):
    conn = fake_conn()
    status = process_new_term(conn, _new_row(latin_lemma=""), human_src_id=6)
    assert status == "NO_LEMMA"
    assert conn.executed == []
