"""Unit tests for RunRepository — run provenance + analytics SQL."""

from __future__ import annotations

from unittest.mock import patch

from storage.repositories import RunRepository


def test_glossary_snapshot(fake_conn):
    conn = fake_conn(fetchone_results=[(10, 4)])
    assert RunRepository(conn).glossary_snapshot() == {
        "approved_senses": 10,
        "max_version": 4,
    }


def test_open_run_inserts_and_returns_id(fake_conn):
    conn = fake_conn(fetchone_results=[(7,)])
    run_id = RunRepository(conn).open_run(
        flow_name="translate_corpus",
        git_sha="abc1234",
        prompt_hash="deadbeef",
        snapshot={"approved_senses": 10},
        translator_model="deepseek-chat",
        reviewer_model="deepseek-reasoner",
        temperature=0.3,
        filters=None,
        max_workers=5,
    )
    assert run_id == 7
    sql, params = conn.executed[-1]
    assert "INSERT INTO translation_run" in sql
    assert params[0] == "translate_corpus"
    assert params[1] == "abc1234"
    assert params[-1] == 5  # max_workers
    assert params[-2] is None  # filters → None when absent


def test_finalize_run_updates_totals(fake_conn):
    conn = fake_conn()
    RunRepository(conn).finalize_run(
        7,
        total_segments=12,
        total_translated=10,
        total_needs_human=2,
        total_cost=0.42,
    )
    sql, params = conn.executed[-1]
    assert "UPDATE translation_run" in sql and "finished_at = now()" in sql
    assert params == (12, 10, 2, 0.42, 7)


def test_insert_run_segments_uses_execute_values(fake_conn):
    records = [
        {
            "segment_id": 1,
            "final_status": "translated",
            "iterations_used": 1,
            "chosen_iteration": 1,
            "cost_usd": 0.01,
            "failure_classes": None,
            "last_feedback": None,
        }
    ]
    conn = fake_conn()
    with patch("storage.repositories.psycopg2") as mock_psycopg2:
        RunRepository(conn).insert_run_segments(7, records)
    assert mock_psycopg2.extras.execute_values.called
    rows = mock_psycopg2.extras.execute_values.call_args.args[2]
    assert rows[0][0] == 7 and rows[0][1] == 1  # run_id, segment_id


def test_last_run_returns_most_recent(fake_conn):
    row = {
        "run_id": 9,
        "flow_name": "translate_corpus",
        "started_at": "2026-06-17T10:00:00",
        "finished_at": "2026-06-17T10:30:00",
        "total_segments": 100,
        "total_translated": 95,
        "total_needs_human": 5,
        "total_cost_usd": 1.23,
    }
    conn = fake_conn(fetchone_results=[row])
    result = RunRepository(conn).last_run()
    assert result == row
    sql, _ = conn.executed[-1]
    assert "FROM translation_run ORDER BY run_id DESC LIMIT 1" in sql


def test_last_run_returns_none_when_no_runs(fake_conn):
    conn = fake_conn(fetchone_results=[])
    assert RunRepository(conn).last_run() is None
