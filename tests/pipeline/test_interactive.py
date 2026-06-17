"""Unit tests for the interactive pipeline driver.

No DB, no real Steps run: status is injected via a fake `gather`, and the
runner is replaced with a recorder so we test the loop/menu/state machinery in
isolation.
"""

from __future__ import annotations

from pipeline import interactive as ix
from pipeline.interactive import (
    StatusSnapshot,
    build_menu,
    load_state,
    render_menu,
    render_status,
    run_loop,
    save_state,
)

# ── State persistence ────────────────────────────────────────────────────────


def test_load_state_missing_returns_empty(tmp_path):
    assert load_state(tmp_path / "nope.json") == {}


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    save_state("resolve", path)
    state = load_state(path)
    assert state["last_command"] == "resolve"
    assert "at" in state


def test_load_state_corrupt_returns_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_state(path) == {}


# ── Rendering ────────────────────────────────────────────────────────────────


def test_render_status_shows_counts_and_last_command():
    snap = StatusSnapshot(
        segment_counts={"pending": 40, "translated": 55, "needs_human": 5},
        sense_counts={"proposed": 12, "approved": 88},
        last_run={
            "run_id": 9,
            "flow_name": "translate_corpus",
            "started_at": "2026-06-17T10:00",
            "finished_at": "2026-06-17T10:30",
            "total_translated": 95,
            "total_needs_human": 5,
            "total_cost_usd": 1.23,
        },
    )
    text = render_status(snap, {"last_command": "resolve", "at": "2026-06-17T09:00"})
    assert "pending" in text and "40" in text
    assert "100 total" in text  # segment total
    assert "proposed" in text and "approved" in text
    assert "#9 translate_corpus" in text
    assert "Last command: resolve" in text


def test_render_status_handles_empty_corpus():
    snap = StatusSnapshot(segment_counts={}, sense_counts={}, last_run=None)
    text = render_status(snap, {})
    assert "not yet ingested" in text
    assert "Last run: (none)" in text


def test_render_status_flags_unfinished_run():
    snap = StatusSnapshot(
        segment_counts={},
        sense_counts={},
        last_run={"run_id": 1, "flow_name": "translate_corpus",
                  "started_at": "x", "finished_at": None},
    )
    text = render_status(snap, {})
    assert "RUNNING/crashed" in text


def test_render_menu_numbers_items():
    menu = build_menu()
    text = render_menu(menu)
    assert "1. " in text and "q' to quit" in text
    assert len(menu) >= 6  # the documented menu set


# ── Loop ─────────────────────────────────────────────────────────────────────


class _Reader:
    """Yields queued inputs, then raises EOFError (as a real EOF would)."""

    def __init__(self, *inputs):
        self._inputs = list(inputs)

    def __call__(self, _prompt=""):
        if not self._inputs:
            raise EOFError
        return self._inputs.pop(0)


class _RecordingRunner:
    last = None

    def __init__(self, results):
        self._results = results
        self.ran = []
        _RecordingRunner.last = self

    def run(self, steps):
        self.ran.extend(s.name for s in steps)
        return self._results


def _fake_gather(_ctx):
    return StatusSnapshot(segment_counts={}, sense_counts={}, last_run=None)


def _run(monkeypatch, tmp_path, reader, results=None, capsys=None):
    from pipeline import StepResult
    from pipeline.context import PipelineContext

    monkeypatch.setattr(ix, "STATE_FILE", tmp_path / "state.json")
    results = results if results is not None else [StepResult(name="x", ok=True)]
    runner = _RecordingRunner(results)
    ctx = PipelineContext(reports_dir=tmp_path)
    out = []

    class _Out:
        def write(self, s):
            out.append(s)

        def flush(self):
            pass

    code = run_loop(
        ctx,
        read=reader,
        out=_Out(),
        gather=_fake_gather,
        make_runner=lambda: runner,
    )
    return code, runner, "".join(out)


def test_quit_immediately(monkeypatch, tmp_path):
    code, runner, _ = _run(monkeypatch, tmp_path, _Reader("q"))
    assert code == 0 and runner.ran == []


def test_eof_quits(monkeypatch, tmp_path):
    code, runner, _ = _run(monkeypatch, tmp_path, _Reader())
    assert code == 0 and runner.ran == []


def test_selecting_first_item_runs_step_and_saves_state(monkeypatch, tmp_path):
    code, runner, _ = _run(monkeypatch, tmp_path, _Reader("1", "q"))
    menu = build_menu()
    assert runner.ran == [menu[0].factory().name]
    state = load_state(tmp_path / "state.json")
    assert state["last_command"] == menu[0].token


def test_invalid_choice_does_not_run(monkeypatch, tmp_path):
    code, runner, out = _run(monkeypatch, tmp_path, _Reader("999", "q"))
    assert runner.ran == []
    assert "Unrecognized choice" in out


def test_refresh_loops_without_running(monkeypatch, tmp_path):
    code, runner, _ = _run(monkeypatch, tmp_path, _Reader("r", "q"))
    assert runner.ran == []


def test_failed_step_reports_but_continues(monkeypatch, tmp_path):
    from pipeline import StepResult

    code, runner, out = _run(
        monkeypatch, tmp_path, _Reader("1", "q"),
        results=[StepResult(name="x", ok=False, summary="boom")],
    )
    assert "reported a failure" in out


def test_broken_factory_is_surfaced_not_fatal(monkeypatch, tmp_path):
    """A stage whose factory raises (broken optional dep) is reported, loop survives."""
    from pipeline.interactive import MenuItem

    def _boom():
        raise ImportError("no module named widget")

    menu = [MenuItem("broken", "Broken stage", _boom)]
    monkeypatch.setattr(ix, "build_menu", lambda: menu)
    code, runner, out = _run(monkeypatch, tmp_path, _Reader("1", "q"))
    assert code == 0
    assert runner.ran == []  # step never reached the runner
    assert "[broken] unavailable" in out


def test_status_unavailable_is_surfaced_not_fatal(monkeypatch, tmp_path):
    from pipeline import StepResult
    from pipeline.context import PipelineContext

    monkeypatch.setattr(ix, "STATE_FILE", tmp_path / "state.json")
    runner = _RecordingRunner([StepResult(name="x", ok=True)])
    out = []

    class _Out:
        def write(self, s):
            out.append(s)

        def flush(self):
            pass

    def _boom(_ctx):
        raise RuntimeError("db down")

    code = run_loop(
        PipelineContext(reports_dir=tmp_path),
        read=_Reader("q"),
        out=_Out(),
        gather=_boom,
        make_runner=lambda: runner,
    )
    assert code == 0
    assert "status unavailable: db down" in "".join(out)


# ── gather_status wiring ──────────────────────────────────────────────────────


def test_gather_status_reads_three_repositories(monkeypatch, fake_conn, tmp_path):
    from pipeline.context import PipelineContext

    conn = fake_conn()

    class _Seg:
        def __init__(self, c):
            pass

        def translation_status_counts(self, wid):
            return {"pending": 3}

    class _Gloss:
        def __init__(self, c):
            pass

        def sense_status_counts(self):
            return {"approved": 7}

    class _Run:
        def __init__(self, c):
            pass

        def last_run(self):
            return None

    monkeypatch.setattr("storage.db.work_id", lambda conn, st: 1)
    monkeypatch.setattr("storage.repositories.SegmentRepository", _Seg)
    monkeypatch.setattr("storage.repositories.GlossaryRepository", _Gloss)
    monkeypatch.setattr("storage.repositories.RunRepository", _Run)

    ctx = PipelineContext(reports_dir=tmp_path, connect=lambda: conn)
    snap = ix.gather_status(ctx)
    assert snap.segment_counts == {"pending": 3}
    assert snap.sense_counts == {"approved": 7}
    assert snap.last_run is None
