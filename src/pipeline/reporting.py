"""Per-step report writer → ``reports/<stage>/<step>.md``.

Every step the `Runner` executes produces a `StepResult`; this module renders
that result into one concise, actionable Markdown file under the step's stage
folder. The goal is a uniform "what happened" surface across stages: status,
timing, the one-line summary, the structured details the step chose to expose,
and — when a step failed — an explicit pointer that human action is required.

The runner owns the timing and the stage destination; steps only decide what to
put in `StepResult.details`. Rendering stays deliberately dumb (no per-stage
special-casing) so a new step gets a usable report for free.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.step import StepResult


@dataclass(frozen=True)
class StepReport:
    """A renderable record of one step's outcome.

    stage:      the report folder the step belongs to (``acquire``, ``ingest``…).
    result:     the step's `StepResult` (status, summary, details).
    started_at: wall-clock start (UTC-naive local time, for the header).
    elapsed_s:  wall-clock duration in seconds.
    """

    stage: str
    result: StepResult
    started_at: datetime
    elapsed_s: float

    def render(self) -> str:
        r = self.result
        status = "ok" if r.ok else "FAILED"
        lines = [
            f"# {self.stage} · {r.name}",
            "",
            f"- status: {status}",
            f"- when: {self.started_at.isoformat(timespec='seconds')}",
            f"- elapsed: {self.elapsed_s:.1f}s",
        ]
        if r.summary:
            lines.append(f"- summary: {r.summary}")

        body = _render_details(r.details)
        if body:
            lines += ["", "## details", *body]

        action = _action_required(r)
        if action:
            lines += ["", "## action required", *action]

        return "\n".join(lines) + "\n"

    def write(self, stage_dir: Path) -> Path:
        """Write the rendered report to ``<stage_dir>/<step>.md`` and return its path."""
        stage_dir.mkdir(parents=True, exist_ok=True)
        path = stage_dir / f"{self.result.name}.md"
        path.write_text(self.render(), encoding="utf-8")
        return path


def _render_details(details: dict[str, Any]) -> list[str]:
    """Flatten the details dict into ``key: value`` lines.

    One level of nesting is expanded (dicts → indented sub-items, lists → a
    count plus a short preview) so a `checks` map or an `anomalies` list reads
    cleanly without a bespoke template per step.
    """
    lines: list[str] = []
    for key, value in details.items():
        if isinstance(value, dict):
            lines.append(f"- {key}:")
            for sub_key, sub_val in value.items():
                lines.append(f"    - {sub_key}: {_scalar(sub_val)}")
        elif isinstance(value, (list, tuple)):
            preview = ", ".join(_scalar(v) for v in list(value)[:5])
            more = "" if len(value) <= 5 else f" (+{len(value) - 5} more)"
            count = f" ({len(value)})" if value else " (0)"
            lines.append(f"- {key}{count}: {preview}{more}".rstrip())
        else:
            lines.append(f"- {key}: {_scalar(value)}")
    return lines


def _action_required(result: StepResult) -> list[str]:
    """Surface what a human must look at — failed checks, or a failed step."""
    lines: list[str] = []
    checks = result.details.get("checks")
    if isinstance(checks, dict):
        failing = [name for name, ok in checks.items() if not ok]
        if failing:
            lines.append(f"- failing checks: {', '.join(failing)}")
    if not result.ok and not lines:
        lines.append(f"- step failed: {result.summary or 'see details'}")
    return lines


def _scalar(value: Any) -> str:
    """Render a leaf value for a report line (bools as pass/fail, else str)."""
    if isinstance(value, bool):
        return "pass" if value else "FAIL"
    return str(value)
