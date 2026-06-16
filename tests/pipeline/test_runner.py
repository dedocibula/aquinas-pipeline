"""Tests for the pipeline core: context, step, runner.

No DB, no real filesystem beyond tmp_path. Exercises the runner's ordering,
fail-loud, and stop-on-failure semantics plus the context knob/connection
helpers.
"""

from __future__ import annotations

import io
from contextlib import contextmanager

import pytest

from pipeline import PipelineContext, PipelineStep, Runner, StepResult
from pipeline.step import BaseStep


class _RecordingStep:
    """Bare step (no inheritance) — proves the Protocol is duck-typed."""

    def __init__(self, name, *, ok=True, raises=None, log=None):
        self.name = name
        self._ok = ok
        self._raises = raises
        self._log = log

    def run(self, ctx):
        if self._log is not None:
            self._log.append(self.name)
        if self._raises is not None:
            raise self._raises
        return StepResult(name=self.name, ok=self._ok, summary=f"{self.name} done")


def _ctx(tmp_path, **knobs):
    return PipelineContext(reports_dir=tmp_path, knobs=knobs)


def _runner(tmp_path):
    return Runner(_ctx(tmp_path), out=io.StringIO(), err=io.StringIO())


class TestRunnerOrdering:
    def test_runs_every_step_in_order(self, tmp_path):
        log: list[str] = []
        steps = [_RecordingStep(n, log=log) for n in ("a", "b", "c")]
        results = _runner(tmp_path).run(steps)
        assert log == ["a", "b", "c"]
        assert [r.name for r in results] == ["a", "b", "c"]
        assert all(r.ok for r in results)

    def test_stops_on_first_failure_by_default(self, tmp_path):
        log: list[str] = []
        steps = [
            _RecordingStep("a", log=log),
            _RecordingStep("b", ok=False, log=log),
            _RecordingStep("c", log=log),
        ]
        results = _runner(tmp_path).run(steps)
        assert log == ["a", "b"]  # c never ran
        assert [r.ok for r in results] == [True, False]

    def test_continue_past_failure_when_asked(self, tmp_path):
        log: list[str] = []
        steps = [
            _RecordingStep("a", ok=False, log=log),
            _RecordingStep("b", log=log),
        ]
        results = _runner(tmp_path).run(steps, stop_on_failure=False)
        assert log == ["a", "b"]
        assert [r.ok for r in results] == [False, True]


class TestRunnerFailLoud:
    def test_exception_becomes_failed_result_not_crash(self, tmp_path):
        err = io.StringIO()
        runner = Runner(_ctx(tmp_path), out=io.StringIO(), err=err)
        steps = [
            _RecordingStep("boom", raises=RuntimeError("DB down")),
            _RecordingStep("after"),
        ]
        results = runner.run(steps)
        assert results[0].ok is False
        assert "DB down" in results[0].summary
        assert "FAIL in step 'boom'" in err.getvalue()
        assert len(results) == 1  # stopped after the failure


class _VerifyStep(BaseStep):
    """Step exposing a verify() precondition, to exercise the runner gate."""

    def __init__(self, name, *, verify_ok=True, verify_raises=None, log=None):
        self.name = name
        self._verify_ok = verify_ok
        self._verify_raises = verify_raises
        self._log = log

    def verify(self, ctx):
        if self._verify_raises is not None:
            raise self._verify_raises
        return self._verify_ok

    def run(self, ctx):
        if self._log is not None:
            self._log.append(self.name)
        return StepResult(name=self.name, ok=True, summary="ran")


class TestRunnerVerifyHook:
    def test_failed_precondition_skips_run_and_stops(self, tmp_path):
        log: list[str] = []
        err = io.StringIO()
        runner = Runner(_ctx(tmp_path), out=io.StringIO(), err=err)
        steps = [
            _VerifyStep("gate", verify_ok=False, log=log),
            _RecordingStep("after", log=log),
        ]
        results = runner.run(steps)
        assert log == []  # neither gate.run nor after ran
        assert results[0].ok is False
        assert "precondition not met" in results[0].summary
        assert "precondition not met" in err.getvalue()
        assert len(results) == 1  # stopped after the failed gate

    def test_passing_precondition_runs_step(self, tmp_path):
        log: list[str] = []
        steps = [_VerifyStep("gate", verify_ok=True, log=log)]
        results = _runner(tmp_path).run(steps)
        assert log == ["gate"]
        assert results[0].ok is True

    def test_precondition_exception_becomes_failed_result(self, tmp_path):
        err = io.StringIO()
        runner = Runner(_ctx(tmp_path), out=io.StringIO(), err=err)
        steps = [_VerifyStep("gate", verify_raises=RuntimeError("DB down"))]
        results = runner.run(steps)
        assert results[0].ok is False
        assert "precondition error" in results[0].summary
        assert "DB down" in err.getvalue()


class TestProtocolConformance:
    def test_recording_step_is_a_pipeline_step(self):
        assert isinstance(_RecordingStep("x"), PipelineStep)

    def test_base_step_default_verify_passes(self, tmp_path):
        class S(BaseStep):
            name = "s"

            def run(self, ctx):
                return StepResult(name=self.name, ok=True)

        s = S()
        assert s.verify(_ctx(tmp_path)) is True
        assert isinstance(s, PipelineStep)


class TestContextKnobs:
    def test_int_and_float_casts(self, tmp_path):
        ctx = _ctx(tmp_path, BATCH="50", PCT="0.4")
        assert ctx.knob_int("BATCH", 10) == 50
        assert ctx.knob_float("PCT", 0.1) == 0.4

    def test_defaults_when_absent(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert ctx.knob_int("BATCH", 10) == 10
        assert ctx.knob("MISSING", "fallback") == "fallback"

    def test_malformed_knob_fails_loud(self, tmp_path):
        ctx = _ctx(tmp_path, BATCH="not-a-number")
        with pytest.raises(ValueError, match="BATCH"):
            ctx.knob_int("BATCH", 10)


class TestContextHelpers:
    def test_stage_reports_dir_is_created(self, tmp_path):
        ctx = _ctx(tmp_path)
        path = ctx.stage_reports_dir("translate")
        assert path == tmp_path / "translate"
        assert path.is_dir()

    def test_connection_uses_injected_factory(self, tmp_path):
        opened = []

        @contextmanager
        def fake_connect():
            opened.append("open")
            yield "CONN"

        ctx = PipelineContext(reports_dir=tmp_path, connect=fake_connect)
        with ctx.connection() as conn:
            assert conn == "CONN"
        assert opened == ["open"]
