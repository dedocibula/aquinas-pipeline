"""
Tests for src/ingest/pipeline.py — argument parsing and step dispatch.
No DB, no filesystem side effects.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from ingest.pipeline import _STEPS, main


def _noop_steps(*steps):
    """Context manager: replace the given step names with no-ops in _STEP_FNS."""
    import ingest.pipeline as pl
    mocks = {s: MagicMock() for s in steps}
    original = {s: pl._STEP_FNS[s] for s in steps}
    for s, m in mocks.items():
        pl._STEP_FNS[s] = m
    try:
        yield mocks
    finally:
        for s, fn in original.items():
            pl._STEP_FNS[s] = fn


_noop_steps = contextmanager(_noop_steps)


class TestArgumentParsing:
    def test_requires_step_or_all(self):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code != 0

    def test_step_latin_accepted(self):
        with _noop_steps("latin") as mocks:
            result = main(["--step", "latin"])
        assert result == 0
        mocks["latin"].assert_called_once()

    def test_step_report_accepted(self):
        with _noop_steps("report") as mocks:
            result = main(["--step", "report"])
        assert result == 0
        mocks["report"].assert_called_once()

    def test_invalid_step_rejected(self):
        with pytest.raises(SystemExit) as exc:
            main(["--step", "nonexistent"])
        assert exc.value.code != 0

    def test_step_and_all_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            main(["--step", "latin", "--all"])


class TestAllSteps:
    def test_all_runs_every_step_in_order(self):
        call_order = []

        with _noop_steps(*_STEPS) as mocks:
            for step, m in mocks.items():
                name = step
                m.side_effect = lambda s=name: call_order.append(s)
            result = main(["--all"])

        assert result == 0
        assert call_order == list(_STEPS)

    def test_all_stops_on_first_failure(self):
        call_order = []

        with _noop_steps(*_STEPS) as mocks:
            mocks["latin"].side_effect = RuntimeError("latin broke")
            mocks["bahounek"].side_effect = lambda: call_order.append("bahounek")
            result = main(["--all"])

        assert result == 1
        assert "bahounek" not in call_order


class TestStepFailure:
    def test_returns_1_on_exception(self):
        with _noop_steps("resolve") as mocks:
            mocks["resolve"].side_effect = RuntimeError("DB down")
            result = main(["--step", "resolve"])
        assert result == 1


class TestPilotMode:
    def test_pilot_accepted_with_n(self):
        import ingest.pipeline as pl
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
        import ingest.pipeline as pl
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
        import ingest.pipeline as pl
        original = pl._step_pilot
        pl._step_pilot = MagicMock(side_effect=RuntimeError("no DB"))
        try:
            result = main(["--pilot", "10"])
        finally:
            pl._step_pilot = original
        assert result == 1
