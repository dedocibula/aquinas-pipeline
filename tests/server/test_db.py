"""Unit tests for src/server/db.py query helpers (SQL shape via fake_conn)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from server.db import (
    PROPOSAL_KIND_ADD_TERM,
    PROPOSAL_KIND_CHANGE_EVERYWHERE,
    PROPOSAL_KIND_REMOVE_HERE,
    PROPOSAL_KIND_RETIRE_EVERYWHERE,
    PROPOSAL_KIND_WRONG_SENSE_HERE,
    add_comment,
    delete_comment,
    get_comment_counts,
    get_pending_proposal_counts,
    get_segment_constraints,
    get_term_senses,
    list_comments,
    mark_thread_read,
    propose_add_term,
    propose_sense_change,
    reopen_thread,
    resolve_thread,
    segment_has_locked_sense,
)

FAKE_TERM = {
    "term_id": 5,
    "latin_lemma": "ratio",
    "senses": [
        {"sense_id": 100, "context_label": None, "status": "approved", "slovak": "rozum"},
        {"sense_id": 101, "context_label": "reason", "status": "approved", "slovak": "dôvod"},
    ],
}


def _patched_propose(current_sk="rozum", locked=True):
    mock_glossary_cls = MagicMock()
    mock_glossary = mock_glossary_cls.return_value
    mock_glossary.get_current_sense.return_value = {"sense_id": 100, "status": "approved"}
    mock_glossary.get_sk_rendering_content.return_value = current_sk

    mock_proposal_cls = MagicMock()
    mock_proposal = mock_proposal_cls.return_value
    mock_proposal.create_or_update_pending.return_value = 55

    return (
        patch("server.db.GlossaryRepository", mock_glossary_cls),
        patch("server.db.ProposalRepository", mock_proposal_cls),
        patch("server.db.get_term_senses", return_value=FAKE_TERM),
        patch("server.db.segment_has_locked_sense", return_value=locked),
        mock_glossary,
        mock_proposal,
    )


def test_get_segment_constraints_excludes_rejected_usage(fake_conn):
    """D10: a rejected term_usage row must not resurface as a locked term."""
    conn = fake_conn(fetchall_rows=[])
    get_segment_constraints(conn, [1])
    sql, _ = conn.executed[-1]
    assert "tu.status <> 'rejected'" in sql


def test_get_segment_constraints_empty_short_circuits(fake_conn):
    conn = fake_conn()
    assert get_segment_constraints(conn, []) == {}
    assert conn.executed == []


def test_get_segment_constraints_emits_sense_id(fake_conn):
    """Stage 3: sense_id must be present so the propose UI can target a term."""
    conn = fake_conn(
        fetchall_rows=[
            {
                "segment_id": 1,
                "sense_id": 100,
                "latin_lemma": "ratio",
                "context_label": None,
                "slovak": "rozum",
            }
        ]
    )
    result = get_segment_constraints(conn, [1])
    assert result[1][0]["sense_id"] == 100
    sql, _ = conn.executed[-1]
    assert "gs.sense_id" in sql


def test_get_term_senses_returns_none_for_unknown_sense(fake_conn):
    conn = fake_conn(fetchone_results=[None])
    assert get_term_senses(conn, 999) is None


def test_get_term_senses_returns_term_and_senses(fake_conn):
    conn = fake_conn(
        fetchone_results=[{"term_id": 5, "latin_lemma": "ratio"}],
        fetchall_rows=[
            {"sense_id": 100, "context_label": None, "status": "approved", "slovak": "rozum"},
            {"sense_id": 101, "context_label": "reason", "status": "approved", "slovak": "dôvod"},
        ],
    )
    result = get_term_senses(conn, 100)
    assert result["term_id"] == 5
    assert result["latin_lemma"] == "ratio"
    assert [s["sense_id"] for s in result["senses"]] == [100, 101]
    _, second_params = conn.executed[-1]
    assert second_params == (5,)


def test_get_pending_proposal_counts_empty_short_circuits(fake_conn):
    conn = fake_conn()
    assert get_pending_proposal_counts(conn, []) == {}
    assert conn.executed == []


def test_get_pending_proposal_counts_delegates_to_repository(fake_conn):
    conn = fake_conn()
    with patch("server.db.ProposalRepository") as mock_repo_cls:
        mock_repo_cls.return_value.pending_by_sense.return_value = {100: 2}
        result = get_pending_proposal_counts(conn, [100])
    mock_repo_cls.return_value.pending_by_sense.assert_called_once_with([100])
    assert result == {100: 2}


def test_segment_has_locked_sense_true_when_row_found(fake_conn):
    conn = fake_conn(fetchone_results=[(1,)])
    assert segment_has_locked_sense(conn, 42, 100) is True
    sql, params = conn.executed[-1]
    assert params == (42, 100)
    assert "status <> 'rejected'" in sql


def test_segment_has_locked_sense_false_when_no_row(fake_conn):
    conn = fake_conn(fetchone_results=[None])
    assert segment_has_locked_sense(conn, 42, 100) is False


def test_propose_sense_change_not_found(fake_conn):
    p1, p2, p3, p4, mock_glossary, _ = _patched_propose()
    mock_glossary.get_current_sense.return_value = None
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, proposal_id = propose_sense_change(
            conn, 999, PROPOSAL_KIND_CHANGE_EVERYWHERE,
            proposed_sk="myseľ", proposed_sense_id=None, note=None,
            origin_segment_id=None, proposed_by="editor@example.com",
        )
    assert status == "not_found"
    assert proposal_id is None


def test_propose_sense_change_rendering_valid(fake_conn):
    p1, p2, p3, p4, _, mock_proposal = _patched_propose(current_sk="rozum")
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, proposal_id = propose_sense_change(
            conn, 100, PROPOSAL_KIND_CHANGE_EVERYWHERE,
            proposed_sk="myseľ", proposed_sense_id=None, note=None,
            origin_segment_id=None, proposed_by="editor@example.com",
        )
    assert status == "ok"
    assert proposal_id == 55
    _, kwargs = mock_proposal.create_or_update_pending.call_args
    assert kwargs["kind"] == PROPOSAL_KIND_CHANGE_EVERYWHERE
    assert kwargs["proposed_sk"] == "myseľ"
    assert kwargs["latin_lemma"] == "ratio"


def test_propose_sense_change_rendering_missing_sk_returns_proposed_sk_required(fake_conn):
    p1, p2, p3, p4, _, mock_proposal = _patched_propose()
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, proposal_id = propose_sense_change(
            conn, 100, PROPOSAL_KIND_CHANGE_EVERYWHERE,
            proposed_sk=None, proposed_sense_id=None, note=None,
            origin_segment_id=None, proposed_by="editor@example.com",
        )
    assert status == "proposed_sk_required"
    mock_proposal.create_or_update_pending.assert_not_called()


def test_propose_sense_change_rendering_no_change(fake_conn):
    p1, p2, p3, p4, _, mock_proposal = _patched_propose(current_sk="rozum")
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, _ = propose_sense_change(
            conn, 100, PROPOSAL_KIND_CHANGE_EVERYWHERE,
            proposed_sk="rozum", proposed_sense_id=None, note=None,
            origin_segment_id=None, proposed_by="editor@example.com",
        )
    assert status == "no_change"
    mock_proposal.create_or_update_pending.assert_not_called()


def test_propose_sense_change_wrong_sense_here_not_locked(fake_conn):
    p1, p2, p3, p4, _, mock_proposal = _patched_propose(locked=False)
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, _ = propose_sense_change(
            conn, 100, PROPOSAL_KIND_WRONG_SENSE_HERE,
            proposed_sk=None, proposed_sense_id=101, note=None,
            origin_segment_id=42, proposed_by="editor@example.com",
        )
    assert status == "not_locked_here"
    mock_proposal.create_or_update_pending.assert_not_called()


def test_propose_sense_change_wrong_sense_here_wrong_term(fake_conn):
    p1, p2, p3, p4, _, mock_proposal = _patched_propose()
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, _ = propose_sense_change(
            conn, 100, PROPOSAL_KIND_WRONG_SENSE_HERE,
            proposed_sk=None, proposed_sense_id=999, note=None,
            origin_segment_id=42, proposed_by="editor@example.com",
        )
    assert status == "wrong_term"
    mock_proposal.create_or_update_pending.assert_not_called()


def test_propose_sense_change_wrong_sense_here_same_sense_no_change(fake_conn):
    p1, p2, p3, p4, _, mock_proposal = _patched_propose()
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, _ = propose_sense_change(
            conn, 100, PROPOSAL_KIND_WRONG_SENSE_HERE,
            proposed_sk=None, proposed_sense_id=100, note=None,
            origin_segment_id=42, proposed_by="editor@example.com",
        )
    assert status == "no_change"


def test_propose_sense_change_wrong_sense_here_valid_sense_id(fake_conn):
    p1, p2, p3, p4, _, mock_proposal = _patched_propose()
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, proposal_id = propose_sense_change(
            conn, 100, PROPOSAL_KIND_WRONG_SENSE_HERE,
            proposed_sk=None, proposed_sense_id=101, note=None,
            origin_segment_id=42, proposed_by="editor@example.com",
        )
    assert status == "ok"
    assert proposal_id == 55
    _, kwargs = mock_proposal.create_or_update_pending.call_args
    assert kwargs["proposed_sense_id"] == 101
    assert kwargs["proposed_sk"] is None


def test_propose_sense_change_wrong_sense_here_free_text(fake_conn):
    p1, p2, p3, p4, _, mock_proposal = _patched_propose(current_sk="rozum")
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, _ = propose_sense_change(
            conn, 100, PROPOSAL_KIND_WRONG_SENSE_HERE,
            proposed_sk="iný zmysel", proposed_sense_id=None, note=None,
            origin_segment_id=42, proposed_by="editor@example.com",
        )
    assert status == "ok"
    _, kwargs = mock_proposal.create_or_update_pending.call_args
    assert kwargs["proposed_sk"] == "iný zmysel"
    assert kwargs["proposed_sense_id"] is None


def test_propose_sense_change_wrong_sense_here_free_text_no_change(fake_conn):
    p1, p2, p3, p4, _, mock_proposal = _patched_propose(current_sk="rozum")
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, _ = propose_sense_change(
            conn, 100, PROPOSAL_KIND_WRONG_SENSE_HERE,
            proposed_sk="rozum", proposed_sense_id=None, note=None,
            origin_segment_id=42, proposed_by="editor@example.com",
        )
    assert status == "no_change"
    mock_proposal.create_or_update_pending.assert_not_called()


def test_propose_sense_change_wrong_sense_here_missing_target(fake_conn):
    p1, p2, p3, p4, _, mock_proposal = _patched_propose()
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, _ = propose_sense_change(
            conn, 100, PROPOSAL_KIND_WRONG_SENSE_HERE,
            proposed_sk=None, proposed_sense_id=None, note=None,
            origin_segment_id=42, proposed_by="editor@example.com",
        )
    assert status == "missing_target"
    mock_proposal.create_or_update_pending.assert_not_called()


def test_propose_sense_change_remove_here_not_locked(fake_conn):
    p1, p2, p3, p4, _, mock_proposal = _patched_propose(locked=False)
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, _ = propose_sense_change(
            conn, 100, PROPOSAL_KIND_REMOVE_HERE,
            proposed_sk="ignored", proposed_sense_id=None, note=None,
            origin_segment_id=42, proposed_by="editor@example.com",
        )
    assert status == "not_locked_here"
    mock_proposal.create_or_update_pending.assert_not_called()


def test_propose_sense_change_remove_here_ignores_proposed_sk(fake_conn):
    p1, p2, p3, p4, _, mock_proposal = _patched_propose()
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, _ = propose_sense_change(
            conn, 100, PROPOSAL_KIND_REMOVE_HERE,
            proposed_sk="should be ignored", proposed_sense_id=None, note=None,
            origin_segment_id=42, proposed_by="editor@example.com",
        )
    assert status == "ok"
    _, kwargs = mock_proposal.create_or_update_pending.call_args
    assert kwargs["kind"] == PROPOSAL_KIND_REMOVE_HERE
    assert kwargs["proposed_sk"] is None


def test_propose_sense_change_retire_everywhere(fake_conn):
    p1, p2, p3, p4, _, mock_proposal = _patched_propose()
    conn = fake_conn()
    with p1, p2, p3, p4:
        status, _ = propose_sense_change(
            conn, 100, PROPOSAL_KIND_RETIRE_EVERYWHERE,
            proposed_sk=None, proposed_sense_id=None, note="overfit",
            origin_segment_id=None, proposed_by="editor@example.com",
        )
    assert status == "ok"
    _, kwargs = mock_proposal.create_or_update_pending.call_args
    assert kwargs["kind"] == PROPOSAL_KIND_RETIRE_EVERYWHERE
    assert kwargs["proposed_sk"] is None
    assert kwargs["note"] == "overfit"


def test_propose_add_term_existing_lemma(fake_conn):
    conn = fake_conn()
    mock_glossary_cls = MagicMock()
    mock_glossary_cls.return_value.find_term_by_lemma.return_value = 7
    with patch("server.db.GlossaryRepository", mock_glossary_cls):
        status, proposal_id = propose_add_term(
            conn, latin_lemma="ratio", proposed_sk="rozum", note=None,
            origin_segment_id=None, proposed_by="editor@example.com",
        )
    assert status == "term_exists"
    assert proposal_id is None


def test_propose_add_term_valid(fake_conn):
    conn = fake_conn()
    mock_glossary_cls = MagicMock()
    mock_glossary_cls.return_value.find_term_by_lemma.return_value = None
    mock_proposal_cls = MagicMock()
    mock_proposal_cls.return_value.create_or_update_pending.return_value = 77
    with (
        patch("server.db.GlossaryRepository", mock_glossary_cls),
        patch("server.db.ProposalRepository", mock_proposal_cls),
    ):
        status, proposal_id = propose_add_term(
            conn, latin_lemma="novum", proposed_sk="nové", note="missing",
            origin_segment_id=None, proposed_by="editor@example.com",
        )
    assert status == "ok"
    assert proposal_id == 77
    _, kwargs = mock_proposal_cls.return_value.create_or_update_pending.call_args
    assert kwargs["kind"] == PROPOSAL_KIND_ADD_TERM
    assert kwargs["latin_lemma"] == "novum"
    assert kwargs["proposed_by"] == "editor@example.com"


# ---------------------------------------------------------------------------
# Comment threads
# ---------------------------------------------------------------------------

_COMMENT_ROW_1 = {
    "comment_id": 1,
    "segment_id": 42,
    "author": "alice@example.com",
    "body": "first",
    "created_at": "2026-07-01T10:00:00+00:00",
    "resolved": False,
    "resolved_by": None,
    "resolved_at": None,
}

_COMMENT_ROW_2 = {
    "comment_id": 2,
    "segment_id": 42,
    "author": "bob@example.com",
    "body": "second",
    "created_at": "2026-07-01T11:00:00+00:00",
    "resolved": False,
    "resolved_by": None,
    "resolved_at": None,
}


def test_list_comments_open_thread(fake_conn):
    conn = fake_conn(fetchall_rows=[_COMMENT_ROW_1, _COMMENT_ROW_2])
    thread = list_comments(conn, 42)
    assert [c.comment_id for c in thread.comments] == [1, 2]
    assert thread.open_count == 2
    assert thread.resolved is False


def test_list_comments_empty_thread_is_not_resolved(fake_conn):
    conn = fake_conn(fetchall_rows=[])
    thread = list_comments(conn, 42)
    assert thread.comments == []
    assert thread.open_count == 0
    assert thread.resolved is False


def test_list_comments_all_resolved_thread(fake_conn):
    resolved_row = dict(_COMMENT_ROW_1, resolved=True, resolved_by="alice@example.com",
                         resolved_at="2026-07-01T12:00:00+00:00")
    conn = fake_conn(fetchall_rows=[resolved_row])
    thread = list_comments(conn, 42)
    assert thread.open_count == 0
    assert thread.resolved is True


def test_add_comment_returns_new_comment(fake_conn):
    conn = fake_conn(fetchone_results=[_COMMENT_ROW_1])
    comment = add_comment(conn, 42, "alice@example.com", "first")
    assert comment.comment_id == 1
    assert comment.author == "alice@example.com"
    sql, params = conn.executed[-1]
    assert "INSERT INTO segment_comment" in sql
    assert params == (42, "alice@example.com", "first")


def test_resolve_thread_returns_rowcount(fake_conn):
    conn = fake_conn()
    conn._cursor.rowcount = 2
    result = resolve_thread(conn, 42, "admin@example.com")
    assert result == 2
    sql, params = conn.executed[-1]
    assert "resolved = true" in sql
    assert params == ("admin@example.com", 42)


def test_reopen_thread_returns_rowcount(fake_conn):
    conn = fake_conn()
    conn._cursor.rowcount = 1
    result = reopen_thread(conn, 42)
    assert result == 1
    sql, params = conn.executed[-1]
    assert "resolved = false" in sql
    assert params == (42,)


def test_delete_comment_notfound(fake_conn):
    conn = fake_conn(fetchone_results=[None])
    assert delete_comment(conn, 999, "alice@example.com") == "notfound"


def test_delete_comment_forbidden_for_non_author(fake_conn):
    conn = fake_conn(fetchone_results=[("alice@example.com",)])
    assert delete_comment(conn, 1, "bob@example.com") == "forbidden"


def test_delete_comment_ok_for_author(fake_conn):
    conn = fake_conn(fetchone_results=[("alice@example.com",)])
    assert delete_comment(conn, 1, "alice@example.com") == "ok"
    sql, params = conn.executed[-1]
    assert "DELETE FROM segment_comment" in sql
    assert params == (1,)


def test_mark_thread_read_upserts(fake_conn):
    conn = fake_conn()
    mark_thread_read(conn, 42, "alice@example.com")
    sql, params = conn.executed[-1]
    assert "ON CONFLICT (segment_id, user_email)" in sql
    assert params == (42, "alice@example.com")


def test_get_comment_counts_empty_short_circuits(fake_conn):
    conn = fake_conn()
    assert get_comment_counts(conn, [], "alice@example.com") == {}
    assert conn.executed == []


def test_get_comment_counts_shapes_result(fake_conn):
    conn = fake_conn(fetchall_rows=[{"segment_id": 42, "total": 3, "open_count": 2, "unread": 1}])
    result = get_comment_counts(conn, [42], "alice@example.com")
    assert result[42].total == 3
    assert result[42].open_count == 2
    assert result[42].unread == 1
    _, params = conn.executed[-1]
    assert params == ("alice@example.com", "alice@example.com", [42])
