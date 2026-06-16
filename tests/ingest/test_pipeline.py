"""
Tests for src/ingest/pipeline.py — argument parsing and step dispatch via Runner.
No DB, no filesystem side effects (steps are replaced with fakes).
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import ingest.pipeline as pl
from ingest.pipeline import _STEPS, main
from pipeline import StepResult


class _FakeStep:
    """Duck-typed PipelineStep recording its invocation."""

    def __init__(self, name, log=None, *, ok=True, raises=None):
        self.name = name
        self._log = log
        self._ok = ok
        self._raises = raises

    def run(self, ctx):
        if self._log is not None:
            self._log.append(self.name)
        if self._raises is not None:
            raise self._raises
        return StepResult(name=self.name, ok=self._ok, summary="")


@contextmanager
def _fake_registry(log=None, **overrides):
    """Replace _build_steps with fakes for every token in _STEPS.

    overrides maps a token to kwargs for its _FakeStep (e.g. ok=False, raises=...).
    """
    fakes = {name: _FakeStep(name, log, **overrides.get(name, {})) for name in _STEPS}
    original = pl._build_steps
    pl._build_steps = lambda: fakes
    try:
        yield fakes
    finally:
        pl._build_steps = original


class TestArgumentParsing:
    def test_requires_step_or_all(self):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code != 0

    def test_step_latin_runs_only_latin(self):
        log: list[str] = []
        with _fake_registry(log):
            result = main(["--step", "latin"])
        assert result == 0
        assert log == ["latin"]

    def test_step_report_accepted(self):
        log: list[str] = []
        with _fake_registry(log):
            result = main(["--step", "report"])
        assert result == 0
        assert log == ["report"]

    def test_step_verify_accepted(self):
        log: list[str] = []
        with _fake_registry(log):
            result = main(["--step", "verify"])
        assert result == 0
        assert log == ["verify"]

    def test_invalid_step_rejected(self):
        with pytest.raises(SystemExit) as exc:
            main(["--step", "nonexistent"])
        assert exc.value.code != 0

    def test_step_and_all_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            main(["--step", "latin", "--all"])


class TestAllSteps:
    def test_all_runs_every_step_in_order(self):
        log: list[str] = []
        with _fake_registry(log):
            result = main(["--all"])
        assert result == 0
        assert log == list(_STEPS)

    def test_verify_is_prerequisite_step_zero(self):
        assert _STEPS[0] == "verify"

    def test_all_stops_on_first_failure(self):
        log: list[str] = []
        with _fake_registry(log, latin={"raises": RuntimeError("latin broke")}):
            result = main(["--all"])
        assert result == 1
        assert "bahounek" not in log  # never reached after latin failed

    def test_failed_verify_blocks_ingest(self):
        log: list[str] = []
        with _fake_registry(log, verify={"ok": False}):
            result = main(["--all"])
        assert result == 1
        assert log == ["verify"]  # nothing downstream ran


class TestStepFailure:
    def test_returns_1_on_exception(self):
        with _fake_registry(resolve={"raises": RuntimeError("DB down")}):
            result = main(["--step", "resolve"])
        assert result == 1

    def test_returns_1_on_not_ok_result(self):
        with _fake_registry(resolve={"ok": False}):
            result = main(["--step", "resolve"])
        assert result == 1


class TestPilotMode:
    def test_pilot_accepted_with_n(self):
        mock_pilot = MagicMock()
        original = pl._step_pilot
        pl._step_pilot = mock_pilot
        try:
            result = main(["--pilot", "50"])
        finally:
            pl._step_pilot = original
        assert result == 0
        mock_pilot.assert_called_once_with(50, [10, 25, 50, 100])

    def test_pilot_with_custom_batch_sizes(self):
        mock_pilot = MagicMock()
        original = pl._step_pilot
        pl._step_pilot = mock_pilot
        try:
            result = main(["--pilot", "30", "--batch-sizes", "5,20,50"])
        finally:
            pl._step_pilot = original
        assert result == 0
        mock_pilot.assert_called_once_with(30, [5, 20, 50])

    def test_pilot_mutually_exclusive_with_step(self):
        with pytest.raises(SystemExit):
            main(["--pilot", "50", "--step", "resolve"])

    def test_pilot_mutually_exclusive_with_all(self):
        with pytest.raises(SystemExit):
            main(["--pilot", "50", "--all"])

    def test_pilot_returns_1_on_exception(self):
        original = pl._step_pilot
        pl._step_pilot = MagicMock(side_effect=RuntimeError("no DB"))
        try:
            result = main(["--pilot", "10"])
        finally:
            pl._step_pilot = original
        assert result == 1


class TestMineSensesStep:
    def test_delegates_to_sense_mining(self, tmp_path):
        from pipeline import PipelineContext

        ctx = PipelineContext(reports_dir=tmp_path)
        with patch("ingest.sense_mining.run") as run:
            result = pl.MineSensesStep().run(ctx)
        run.assert_called_once_with(terms_filter=None, do_label=True, do_write=True)
        assert result.ok and result.name == "mine-senses"

    def test_declares_resolve_stage(self):
        assert pl.MineSensesStep.stage == "resolve"
