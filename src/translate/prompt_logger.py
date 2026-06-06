"""Prompt logger — writes per-iteration JSONL records for post-run analysis."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class PromptLogger:
    """Append-mode JSONL writer for translation loop debug output.

    Each segment produces N iteration records (one per translate+review cycle)
    followed by one final record marking the chosen draft and outcome status.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")

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

    def _write(self, record: dict) -> None:
        try:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception as exc:
            log.warning("PromptLogger write failed: %s", exc)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception as exc:
            log.warning("PromptLogger close failed (records may be incomplete): %s", exc)

    def __enter__(self) -> PromptLogger:
        return self

    def __exit__(self, *_) -> None:
        self.close()
