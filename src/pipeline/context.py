"""Shared state threaded through every pipeline step.

A step receives one `PipelineContext`. It carries the things every stage needs
but that no single stage owns: the work being processed, where reports go, the
env-derived tuning knobs, and a way to open a DB connection. Steps still open
their own connection (one per unit of work, preserving existing transaction
boundaries) via `ctx.connect()`; the context just centralises the factory so
tests can inject a fake.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import psycopg2.extensions

from storage.db import get_conn

_MISSING = object()

# A factory yielding a DB connection as a context manager (commit on clean exit).
ConnectFactory = Callable[[], "Any"]


@dataclass
class PipelineContext:
    """Read-mostly state passed to each step's ``run``.

    reports_dir: root for per-stage report folders (``reports/<stage>/``).
    work_id:     the work being processed; ``None`` until resolved from the DB.
    knobs:       env-derived tuning values (e.g. ``GAP_BATCH_SIZE``), as strings.
    connect:     factory returning a DB connection context manager.
    """

    reports_dir: Path
    work_id: int | None = None
    knobs: Mapping[str, str] = field(default_factory=dict)
    connect: ConnectFactory = get_conn

    @contextmanager
    def connection(self) -> Iterator[psycopg2.extensions.connection]:
        """Open a DB connection via the configured factory.

        Thin pass-through so steps depend on the context, not on ``storage.db``
        directly, which keeps them trivially fakeable in tests.
        """
        with self.connect() as conn:
            yield conn

    def stage_reports_dir(self, stage: str) -> Path:
        """Return (creating) the report folder for a stage: ``reports/<stage>/``."""
        path = self.reports_dir / stage
        path.mkdir(parents=True, exist_ok=True)
        return path

    # --- typed knob accessors -------------------------------------------------
    # Knobs come from the environment as strings; these cast with a default and
    # fail loudly on a malformed value rather than silently falling back.

    def knob(self, key: str, default: str | None = None) -> str | None:
        value = self.knobs.get(key, _MISSING)
        return default if value is _MISSING else value  # type: ignore[return-value]

    def knob_int(self, key: str, default: int) -> int:
        return self._cast(key, default, int)

    def knob_float(self, key: str, default: float) -> float:
        return self._cast(key, default, float)

    def _cast(self, key: str, default: Any, cast: Callable[[str], Any]) -> Any:
        raw = self.knobs.get(key, _MISSING)
        if raw is _MISSING:
            return default
        try:
            return cast(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"knob {key!r}={raw!r} is not a valid {cast.__name__}"
            ) from exc
