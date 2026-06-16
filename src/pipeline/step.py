"""The unit of pipeline work: a named step that runs against a context.

A step is anything with a ``name`` and a ``run(ctx) -> StepResult``. It may also
expose ``verify(ctx) -> bool`` to declare a precondition (source verification
uses this so downstream steps refuse to run on a broken source tree).

`PipelineStep` is a runtime-checkable Protocol so existing callables can be
adapted without inheritance; `BaseStep` is a small convenience base for the
common case (set ``name``, implement ``run``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pipeline.context import PipelineContext


@dataclass
class StepResult:
    """Outcome of a single step.

    ok:      did the step succeed (False short-circuits a stop-on-failure run).
    summary: one-line human-readable result, surfaced by the runner.
    details: structured extras for reporting (counts, paths, anomalies).
    """

    name: str
    ok: bool
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PipelineStep(Protocol):
    name: str

    def run(self, ctx: "PipelineContext") -> StepResult: ...


class BaseStep:
    """Convenience base for concrete steps.

    Subclasses set ``name`` (class attr) and implement ``run``. ``verify``
    defaults to a no-op pass so steps without a precondition need not override it.
    """

    name: str = ""

    def run(self, ctx: "PipelineContext") -> StepResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def verify(self, ctx: "PipelineContext") -> bool:
        return True
