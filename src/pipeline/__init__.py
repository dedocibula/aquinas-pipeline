"""Whole-pipeline step abstraction.

A `PipelineStep` is one named, runnable unit of work (parse Latin, resolve
terms, translate, ...). A `Runner` executes an ordered sequence of steps with
uniform logging, timing, and fail-loud semantics, threading a shared
`PipelineContext` (work id, report destination, env-derived knobs, DB access)
through each one.

This package holds only the abstraction. Concrete steps wrapping the existing
modules live alongside their stage (ingest/, translate/, acquire/).
"""

from __future__ import annotations

from pipeline.context import PipelineContext
from pipeline.runner import Runner
from pipeline.step import PipelineStep, StepResult

__all__ = ["PipelineContext", "PipelineStep", "StepResult", "Runner"]
