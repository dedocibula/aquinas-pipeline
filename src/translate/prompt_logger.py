"""Prompt logger — writes per-iteration JSONL records for post-run analysis."""

from __future__ import annotations

import json
import logging
import queue
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_SENTINEL = object()


class PromptLogger:
    """Append-mode JSONL writer for translation loop debug output.

    Thread-safe: callers enqueue records via a queue.Queue; a single dedicated
    writer thread drains the queue and writes to disk, so worker threads never
    block on file I/O or synchronise with each other.

    Each segment produces N iteration records (one per translate+review cycle)
    followed by one final record marking the chosen draft and outcome status.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._drain, daemon=True, name="prompt-logger")
        self._thread.start()

    def log_iteration(
        self,
        *,
        segment_id: int,
        locator_path: str,
        iteration: int,
        system_prompt: str,
        user_turn: str,
        draft: str,
        precheck_ok: bool,
        precheck_failures: list[str],
        reviewer_turn: str | None,
        verdict: str | None,
        notes: dict | None,
        feedback: str | None,
    ) -> None:
        self._write(
            {
                "type": "iteration",
                "segment_id": segment_id,
                "locator_path": locator_path,
                "iteration": iteration,
                "system_prompt": system_prompt,
                "user_turn": user_turn,
                "draft": draft,
                "precheck_ok": precheck_ok,
                "precheck_failures": precheck_failures,
                "reviewer_turn": reviewer_turn,
                "verdict": verdict,
                "notes": notes,
                "feedback": feedback,
            }
        )

    def log_final(
        self,
        *,
        segment_id: int,
        locator_path: str,
        status: str,
        chosen_iteration: int | None,
        chosen_draft: str | None,
    ) -> None:
        self._write(
            {
                "type": "final",
                "segment_id": segment_id,
                "locator_path": locator_path,
                "status": status,
                "chosen_iteration": chosen_iteration,
                "chosen_draft": chosen_draft,
            }
        )

    def log_polish(
        self,
        *,
        segment_id: int,
        locator_path: str,
        status: str,
        guard_flags: dict,
        cost_usd: float,
    ) -> None:
        self._write(
            {
                "type": "polish",
                "segment_id": segment_id,
                "locator_path": locator_path,
                "status": status,
                "guard_flags": guard_flags,
                "cost_usd": cost_usd,
            }
        )

    def _write(self, record: dict) -> None:
        self._queue.put(record)

    def _drain(self) -> None:
        while True:
            record = self._queue.get()
            if record is _SENTINEL:
                break
            try:
                self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._fh.flush()
            except Exception as exc:
                log.warning("PromptLogger write failed: %s", exc)

    def close(self) -> None:
        self._queue.put(_SENTINEL)
        self._thread.join()
        try:
            self._fh.close()
        except Exception as exc:
            log.warning("PromptLogger close failed (records may be incomplete): %s", exc)

    def __enter__(self) -> PromptLogger:
        return self

    def __exit__(self, *_) -> None:
        self.close()
