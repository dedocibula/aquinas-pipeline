"""Unit tests for src/polish/batch.py.

All DB and Anthropic SDK calls are mocked — no real I/O.  The test seam is
the _client parameter on run_batch() and direct injection into the sub-functions.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from polish.batch import (
    _BatchStats,
    _build_payload,
    _build_request,
    _process_results,
    _SegmentPayload,
    fetch_batch_candidates,
    run_batch,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_succeeded_result(custom_id: str, text: str) -> SimpleNamespace:
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=200,
    )
    content_block = SimpleNamespace(type="text", text=text)
    message = SimpleNamespace(content=[content_block], usage=usage)
    result = SimpleNamespace(type="succeeded", message=message)
    return SimpleNamespace(custom_id=custom_id, result=result)


def _make_errored_result(custom_id: str) -> SimpleNamespace:
    error = SimpleNamespace(type="api_error", message="server error")
    result = SimpleNamespace(type="errored", error=error)
    return SimpleNamespace(custom_id=custom_id, result=result)


def _fake_client(batch_results: list | None = None) -> MagicMock:
    """Return a minimal fake anthropic.Anthropic() client for batch ops."""
    client = MagicMock()
    batch = SimpleNamespace(id="msgbatch_test001", processing_status="ended")
    client.messages.batches.create.return_value = batch
    client.messages.batches.retrieve.return_value = batch
    client.messages.batches.results.return_value = iter(batch_results or [])
    return client


# ── fetch_batch_candidates ────────────────────────────────────────────────────


def test_fetch_batch_candidates_builds_correct_sql():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = [(1,), (2,), (3,)]
    cursor.__enter__ = lambda s: s
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor

    result = fetch_batch_candidates(conn, limit=3)

    assert result == [1, 2, 3]
    cursor.execute.assert_called_once()
    sql, params = cursor.execute.call_args[0]
    assert "translation_status = 'translated'" in sql
    assert "code = 'human'" in sql
    assert "code = 'polish'" in sql
    assert params[-1] == 3  # limit


def test_fetch_batch_candidates_element_type_filter():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = [(10,)]
    cursor.__enter__ = lambda s: s
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor

    fetch_batch_candidates(conn, element_types=["body", "response"], limit=10)

    _, params = cursor.execute.call_args[0]
    assert "body" in params
    assert "response" in params


def test_fetch_batch_candidates_no_limit():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    cursor.__enter__ = lambda s: s
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor

    result = fetch_batch_candidates(conn)

    assert result == []
    sql, params = cursor.execute.call_args[0]
    assert "LIMIT" not in sql


# ── _build_payload ────────────────────────────────────────────────────────────


def test_build_payload_returns_none_when_no_model_text():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.__enter__ = lambda s: s
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor

    result = _build_payload(conn, 99)
    assert result is None


def test_build_payload_returns_payload():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = ("Boh je dobrý.",)
    cursor.__enter__ = lambda s: s
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor

    with patch("polish.batch.GlossaryRepository") as mock_gloss:
        mock_gloss.return_value.locked_terms.return_value = []
        result = _build_payload(conn, 42)

    assert result is not None
    assert result.segment_id == 42
    assert result.model_text == "Boh je dobrý."
    assert result.constraints == []


# ── _build_request ────────────────────────────────────────────────────────────


def test_build_request_sets_custom_id_and_model():
    payload = _SegmentPayload(segment_id=77, model_text="Teda Boh existuje.", constraints=[])
    system_text = "You are a polish assistant."

    with patch("polish.batch._load_system", return_value=system_text):
        req = _build_request(payload, system_text)

    assert req["custom_id"] == "77"
    assert req["params"]["model"] == "claude-haiku-4-5-20251001"
    assert req["params"]["max_tokens"] == 2048


def test_build_request_user_content_contains_source_draft():
    payload = _SegmentPayload(
        segment_id=5,
        model_text="Teda Boh je prvý pohybovateľ.",
        constraints=[],
    )

    req = _build_request(payload, "system")
    user_content = req["params"]["messages"][0]["content"]
    assert "<source_draft>" in user_content
    assert "Teda Boh je prvý pohybovateľ." in user_content


def test_build_request_system_has_cache_control():
    payload = _SegmentPayload(segment_id=1, model_text="text", constraints=[])
    req = _build_request(payload, "system prompt")
    system_blocks = req["params"]["system"]
    assert len(system_blocks) == 1
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}


# ── _process_results ──────────────────────────────────────────────────────────


def test_process_results_writes_guard_passing_segment():
    model_text = "Teda Boh je dokonalý."
    polished_text = "Teda Boh je dokonalý a večný."
    payload = _SegmentPayload(segment_id=1, model_text=model_text, constraints=[])

    succeeded = _make_succeeded_result("1", polished_text)
    client = MagicMock()
    client.messages.batches.results.return_value = iter([succeeded])

    conn = MagicMock()
    seg_repo = MagicMock()
    stats = _BatchStats()

    guard_ok = {
        "ok": True,
        "sentence_delta": 0,
        "term_retention_ok": True,
        "missing_terms": [],
        "particle_retention_ok": True,
        "missing_particles": [],
        "length_ratio": 1.05,
    }

    with patch("polish.batch.SegmentRepository", return_value=seg_repo):
        with patch("polish.batch.run_guards", return_value=guard_ok):
            _process_results(client, "batch_id", {1: payload}, conn, src_polish_id=8, stats=stats)

    seg_repo.write_segment_text.assert_called_once_with(1, "sk", 8, polished_text)
    conn.commit.assert_called_once()
    assert stats.polished == 1
    assert stats.guard_failed == 0
    assert stats.errored == 0


def test_process_results_skips_guard_failing_segment():
    model_text = "Boh je dobrý."
    polished_text = "Boh je dobrý. A veľký."  # sentence added
    payload = _SegmentPayload(segment_id=2, model_text=model_text, constraints=[])

    succeeded = _make_succeeded_result("2", polished_text)
    client = MagicMock()
    client.messages.batches.results.return_value = iter([succeeded])

    conn = MagicMock()
    seg_repo = MagicMock()
    stats = _BatchStats()

    guard_fail = {
        "ok": False,
        "sentence_delta": 1,
        "term_retention_ok": True,
        "missing_terms": [],
        "particle_retention_ok": True,
        "missing_particles": [],
        "length_ratio": 1.3,
    }

    with patch("polish.batch.SegmentRepository", return_value=seg_repo):
        with patch("polish.batch.run_guards", return_value=guard_fail):
            _process_results(client, "batch_id", {2: payload}, conn, src_polish_id=8, stats=stats)

    seg_repo.write_segment_text.assert_not_called()
    conn.commit.assert_not_called()
    assert stats.guard_failed == 1
    assert stats.polished == 0


def test_process_results_counts_errored_results():
    errored = _make_errored_result("3")
    client = MagicMock()
    client.messages.batches.results.return_value = iter([errored])

    conn = MagicMock()
    seg_repo = MagicMock()
    stats = _BatchStats()

    with patch("polish.batch.SegmentRepository", return_value=seg_repo):
        _process_results(client, "batch_id", {}, conn, src_polish_id=8, stats=stats)

    seg_repo.write_segment_text.assert_not_called()
    assert stats.errored == 1


def test_process_results_accumulates_cost():
    """Batch cost uses three separate billing buckets (input, cache-write, output, cache-read)."""
    payload = _SegmentPayload(segment_id=10, model_text="text", constraints=[])
    # Give cache_creation_input_tokens a non-zero value to exercise the cache-write bucket
    result_ns = _make_succeeded_result("10", "polished text")
    result_ns.result.message.usage.cache_creation_input_tokens = 80
    client = MagicMock()
    client.messages.batches.results.return_value = iter([result_ns])

    conn = MagicMock()
    stats = _BatchStats()
    guard_ok = {
        "ok": True, "sentence_delta": 0, "term_retention_ok": True,
        "missing_terms": [], "particle_retention_ok": True,
        "missing_particles": [], "length_ratio": 1.0,
    }

    with patch("polish.batch.SegmentRepository", return_value=MagicMock()):
        with patch("polish.batch.run_guards", return_value=guard_ok):
            _process_results(client, "batch_id", {10: payload}, conn, src_polish_id=8, stats=stats)

    # Fixture: input=100, cache_write=80, output=50, cache_read=200
    # Haiku batch pricing per 1M: input $0.40, cache-write $0.50, output $2.00, cache-read $0.04
    expected = (
        100 * 0.400 / 1_000_000
        + 80 * 0.500 / 1_000_000
        + 50 * 2.000 / 1_000_000
        + 200 * 0.040 / 1_000_000
    )
    assert stats.cost_usd == pytest.approx(expected)


def test_process_results_db_write_error_continues_iteration():
    """A write failure logs an error and increments errored, but does not abort remaining results."""
    payload_1 = _SegmentPayload(segment_id=1, model_text="text", constraints=[])
    payload_2 = _SegmentPayload(segment_id=2, model_text="text2", constraints=[])

    r1 = _make_succeeded_result("1", "polished 1")
    r2 = _make_succeeded_result("2", "polished 2")
    client = MagicMock()
    client.messages.batches.results.return_value = iter([r1, r2])

    conn = MagicMock()
    seg_repo = MagicMock()
    # First write raises; second succeeds
    seg_repo.write_segment_text.side_effect = [RuntimeError("disk full"), None]
    stats = _BatchStats()
    guard_ok = {
        "ok": True, "sentence_delta": 0, "term_retention_ok": True,
        "missing_terms": [], "particle_retention_ok": True,
        "missing_particles": [], "length_ratio": 1.0,
    }

    with patch("polish.batch.SegmentRepository", return_value=seg_repo):
        with patch("polish.batch.run_guards", return_value=guard_ok):
            _process_results(client, "batch_id", {1: payload_1, 2: payload_2},
                             conn, src_polish_id=8, stats=stats)

    assert stats.errored == 1
    assert stats.polished == 1
    assert seg_repo.write_segment_text.call_count == 2


def test_process_results_unordered_results_matched_by_custom_id():
    """Results arrive out of order — each must be matched to its payload by custom_id."""
    payload_10 = _SegmentPayload(segment_id=10, model_text="text A", constraints=[])
    payload_20 = _SegmentPayload(segment_id=20, model_text="text B", constraints=[])

    # Results arrive in reverse order
    r20 = _make_succeeded_result("20", "polished B")
    r10 = _make_succeeded_result("10", "polished A")
    client = MagicMock()
    client.messages.batches.results.return_value = iter([r20, r10])

    conn = MagicMock()
    seg_repo = MagicMock()
    stats = _BatchStats()
    guard_ok = {
        "ok": True, "sentence_delta": 0, "term_retention_ok": True,
        "missing_terms": [], "particle_retention_ok": True,
        "missing_particles": [], "length_ratio": 1.0,
    }

    with patch("polish.batch.SegmentRepository", return_value=seg_repo):
        with patch("polish.batch.run_guards", return_value=guard_ok):
            _process_results(client, "batch_id", {10: payload_10, 20: payload_20},
                             conn, src_polish_id=8, stats=stats)

    assert stats.polished == 2
    calls = {c[0][0]: c[0][3] for c in seg_repo.write_segment_text.call_args_list}
    assert calls[10] == "polished A"
    assert calls[20] == "polished B"


# ── run_batch (end-to-end with mocked get_conn) ───────────────────────────────


def test_run_batch_no_candidates_writes_report():
    conn = MagicMock()
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = lambda s: s
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.fetchall.return_value = []

    with patch("polish.batch.get_conn") as mock_get_conn:
        mock_get_conn.return_value.__enter__ = lambda s: conn
        mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)
        with patch("polish.batch._load_system", return_value="system"):
            with patch("polish.batch._write_report") as mock_report:
                stats = run_batch(_client=_fake_client())

    assert stats.total == 0
    assert stats.polished == 0
    mock_report.assert_called_once()
    report_stats = mock_report.call_args[0][0]
    assert report_stats.total == 0


def test_run_batch_no_source_count():
    """Segments whose (sk,model) text is missing are tallied in stats.no_source."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = [(1,), (2,)]
    # Both fetchone calls (for model text) return None → no payloads built
    cur.fetchone.return_value = None
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s: conn
    conn.__exit__ = MagicMock(return_value=False)

    with patch("polish.batch.get_conn", return_value=conn):
        with patch("polish.batch._load_system", return_value="system"):
            with patch("polish.batch.GlossaryRepository"):
                with patch("polish.batch._write_report") as mock_report:
                    stats = run_batch(_client=_fake_client())

    assert stats.no_source == 2
    mock_report.assert_called_once()


def test_run_batch_happy_path_writes_polish():
    """Integration-level: one candidate → one batch → write_segment_text + commit called."""
    model_text = "Teda Boh je dokonalý."
    polished_text = "Teda Boh je dokonalý a mocný."
    guard_ok = {
        "ok": True, "sentence_delta": 0, "term_retention_ok": True,
        "missing_terms": [], "particle_retention_ok": True,
        "missing_particles": [], "length_ratio": 1.05,
    }

    succeeded = _make_succeeded_result("1", polished_text)
    client = _fake_client([succeeded])

    call_count = {"n": 0}

    def _make_conn(seg_ids=None, model_row=None):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        if seg_ids is not None:
            cur.fetchall.return_value = [(i,) for i in seg_ids]
        if model_row is not None:
            cur.fetchone.return_value = model_row
        conn.cursor.return_value = cur
        conn.__enter__ = lambda s: conn
        conn.__exit__ = MagicMock(return_value=False)
        return conn

    # Three get_conn calls: candidates, payload build, result processing
    conns = [
        _make_conn(seg_ids=[1]),
        _make_conn(model_row=(model_text,)),
        _make_conn(),
    ]

    def _side_effect():
        c = conns[call_count["n"]]
        call_count["n"] += 1
        return c

    with patch("polish.batch.get_conn") as mock_get_conn:
        mock_get_conn.side_effect = _side_effect
        with patch("polish.batch._load_system", return_value="system"):
            with patch("polish.batch.GlossaryRepository") as mock_gloss:
                mock_gloss.return_value.locked_terms.return_value = []
                with patch("polish.batch.SegmentRepository") as mock_seg:
                    seg_repo = mock_seg.return_value
                    with patch("polish.batch.source_id", return_value=8):
                        with patch("polish.batch.run_guards", return_value=guard_ok):
                            with patch("polish.batch._write_report"):
                                stats = run_batch(_client=client, limit=1)

    assert stats.polished == 1
    assert stats.guard_failed == 0
    assert stats.errored == 0
    # write_segment_text was called with correct args
    seg_repo.write_segment_text.assert_called_once_with(1, "sk", 8, polished_text)
    # commit was called on the result-processing connection
    process_conn = conns[2]
    process_conn.commit.assert_called_once()
