"""Tests for translate.prompt_logger — thread-safe JSONL writer."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from translate.prompt_logger import PromptLogger


def _read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ── Basic write behaviour ─────────────────────────────────────────────────────


def test_iteration_record_written(tmp_path):
    path = tmp_path / "debug.jsonl"
    with PromptLogger(path) as pl:
        pl.log_iteration(
            segment_id=1,
            locator_path="I.q1.a1.arg1",
            iteration=1,
            system_prompt="sys",
            user_turn="user",
            draft="draft",
            precheck_ok=True,
            precheck_failures=[],
            reviewer_turn=None,
            verdict=None,
            notes=None,
            feedback=None,
        )
    records = _read_records(path)
    assert len(records) == 1
    r = records[0]
    assert r["type"] == "iteration"
    assert r["segment_id"] == 1
    assert r["locator_path"] == "I.q1.a1.arg1"
    assert r["draft"] == "draft"
    assert r["precheck_ok"] is True


def test_final_record_written(tmp_path):
    path = tmp_path / "debug.jsonl"
    with PromptLogger(path) as pl:
        pl.log_final(
            segment_id=7,
            locator_path="I.q2.a3.body",
            status="translated",
            chosen_iteration=2,
            chosen_draft="Final Slovak text.",
        )
    records = _read_records(path)
    assert len(records) == 1
    r = records[0]
    assert r["type"] == "final"
    assert r["segment_id"] == 7
    assert r["status"] == "translated"
    assert r["chosen_iteration"] == 2


def test_multiple_records_all_written(tmp_path):
    path = tmp_path / "debug.jsonl"
    with PromptLogger(path) as pl:
        for i in range(5):
            pl.log_final(
                segment_id=i,
                locator_path=f"I.q1.a{i}",
                status="translated",
                chosen_iteration=1,
                chosen_draft=f"draft {i}",
            )
    records = _read_records(path)
    assert len(records) == 5
    assert [r["segment_id"] for r in records] == list(range(5))


def test_non_ascii_preserved(tmp_path):
    path = tmp_path / "debug.jsonl"
    with PromptLogger(path) as pl:
        pl.log_final(
            segment_id=1,
            locator_path="I.q1.a1",
            status="translated",
            chosen_iteration=1,
            chosen_draft="Zdá sa, že Boh nejestvuje.",
        )
    records = _read_records(path)
    assert records[0]["chosen_draft"] == "Zdá sa, že Boh nejestvuje."


# ── Context manager ────────────────────────────────────────────────────────────


def test_context_manager_closes_and_flushes(tmp_path):
    path = tmp_path / "debug.jsonl"
    with PromptLogger(path) as pl:
        pl.log_final(
            segment_id=99,
            locator_path="I.q6.a1",
            status="needs_human",
            chosen_iteration=None,
            chosen_draft=None,
        )
    assert path.exists()
    records = _read_records(path)
    assert len(records) == 1


def test_close_drains_all_queued_records(tmp_path):
    path = tmp_path / "debug.jsonl"
    pl = PromptLogger(path)
    for i in range(20):
        pl.log_final(
            segment_id=i,
            locator_path=f"I.q1.a{i}",
            status="translated",
            chosen_iteration=1,
            chosen_draft=f"d{i}",
        )
    pl.close()
    records = _read_records(path)
    assert len(records) == 20


def test_double_close_does_not_raise(tmp_path):
    path = tmp_path / "debug.jsonl"
    pl = PromptLogger(path)
    pl.log_final(
        segment_id=1,
        locator_path="I.q1.a1",
        status="translated",
        chosen_iteration=1,
        chosen_draft="text",
    )
    pl.close()
    pl.close()  # second close must not raise and must not lose the first record
    records = _read_records(path)
    assert len(records) == 1


# ── Concurrent writes ─────────────────────────────────────────────────────────


def test_concurrent_writes_all_present(tmp_path):
    path = tmp_path / "debug.jsonl"
    n_threads = 8
    records_per_thread = 25

    with PromptLogger(path) as pl:
        def worker(tid: int) -> None:
            for i in range(records_per_thread):
                pl.log_final(
                    segment_id=tid * 1000 + i,
                    locator_path=f"I.q{tid}.a{i}",
                    status="translated",
                    chosen_iteration=1,
                    chosen_draft=f"t{tid}-r{i}",
                )

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    records = _read_records(path)
    assert len(records) == n_threads * records_per_thread
    segment_ids = {r["segment_id"] for r in records}
    assert len(segment_ids) == n_threads * records_per_thread


def test_concurrent_writes_no_interleaving(tmp_path):
    """Each line must be valid JSON — no partial writes from concurrent threads."""
    path = tmp_path / "debug.jsonl"
    with PromptLogger(path) as pl:
        def worker(tid: int) -> None:
            for i in range(50):
                pl.log_iteration(
                    segment_id=tid * 100 + i,
                    locator_path=f"I.q{tid}.a{i}",
                    iteration=i,
                    system_prompt="s" * 200,
                    user_turn="u" * 200,
                    draft="d" * 100,
                    precheck_ok=True,
                    precheck_failures=[],
                    reviewer_turn="r" * 100,
                    verdict="APPROVED",
                    notes={"iteration": i},
                    feedback=None,
                )

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 500
    for line in lines:
        parsed = json.loads(line)  # raises if interleaved/corrupt
        assert parsed["type"] == "iteration"


# ── Parent directory creation ─────────────────────────────────────────────────


def test_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "deep" / "debug.jsonl"
    with PromptLogger(path) as pl:
        pl.log_final(
            segment_id=1,
            locator_path="I.q1.a1",
            status="translated",
            chosen_iteration=1,
            chosen_draft="text",
        )
    assert path.exists()


# ── log_polish ────────────────────────────────────────────────────────────────


def test_polish_record_written(tmp_path):
    path = tmp_path / "debug.jsonl"
    with PromptLogger(path) as pl:
        pl.log_polish(
            segment_id=42,
            locator_path="I.q1.a1.arg1",
            status="polished",
            guard_flags={"ok": True, "sentence_delta": 0, "length_ratio": 1.02},
            cost_usd=0.0015,
        )
    records = _read_records(path)
    assert len(records) == 1
    r = records[0]
    assert r["type"] == "polish"
    assert r["segment_id"] == 42
    assert r["locator_path"] == "I.q1.a1.arg1"
    assert r["status"] == "polished"
    assert r["guard_flags"]["ok"] is True
    assert abs(r["cost_usd"] - 0.0015) < 1e-9


def test_polish_record_after_final(tmp_path):
    """Each translated segment emits a final then a polish record in sequence."""
    path = tmp_path / "debug.jsonl"
    with PromptLogger(path) as pl:
        pl.log_final(
            segment_id=1,
            locator_path="I.q1.a1.arg1",
            status="translated",
            chosen_iteration=1,
            chosen_draft="draft text",
        )
        pl.log_polish(
            segment_id=1,
            locator_path="I.q1.a1.arg1",
            status="polished",
            guard_flags={"ok": False, "missing_particles": ["totiž"]},
            cost_usd=0.002,
        )
    records = _read_records(path)
    assert len(records) == 2
    assert records[0]["type"] == "final"
    assert records[1]["type"] == "polish"
    assert records[1]["guard_flags"]["missing_particles"] == ["totiž"]
