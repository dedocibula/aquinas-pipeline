"""Tests for the --polish mode added to optimize.run_compare."""

from __future__ import annotations

import json
from pathlib import Path

from optimize.run_compare import (
    _guard_line,
    _PolishRecord,
    _render_polish_pair,
    build_polish_report,
    parse_polish_jsonl,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
        encoding="utf-8",
    )


def _final(sid: int, locator: str, draft: str) -> dict:
    return {
        "type": "final",
        "segment_id": sid,
        "locator_path": locator,
        "status": "translated",
        "chosen_iteration": 1,
        "chosen_draft": draft,
    }


def _polish(
    sid: int,
    locator: str,
    polished: str,
    *,
    status: str = "polished",
    ok: bool = True,
    ratio: float = 1.0,
) -> dict:
    return {
        "type": "polish",
        "segment_id": sid,
        "locator_path": locator,
        "status": status,
        "polished_text": polished,
        "guard_flags": {
            "ok": ok,
            "length_ratio": ratio,
            "sentence_delta": 0,
            "term_retention_ok": True,
            "missing_terms": [],
            "particle_retention_ok": True,
            "missing_particles": [],
        },
        "cost_usd": 0.002,
    }


def _make_record(
    sid: int,
    locator: str = "I.q1.a1.arg1",
    model_text: str = "Model text.",
    polished_text: str = "Polished text.",
    ok: bool = True,
) -> _PolishRecord:
    return _PolishRecord(
        segment_id=sid,
        locator_path=locator,
        model_text=model_text,
        polished_text=polished_text,
        guard_flags={"ok": ok, "length_ratio": 1.0, "sentence_delta": 0,
                     "term_retention_ok": True, "missing_terms": [],
                     "particle_retention_ok": True, "missing_particles": []},
    )


# ── parse_polish_jsonl ────────────────────────────────────────────────────────


def test_parse_polish_jsonl_basic(tmp_path):
    p = tmp_path / "debug.jsonl"
    _write_jsonl(p, [
        _final(1, "I.q1.a1.arg1", "Model SK."),
        _polish(1, "I.q1.a1.arg1", "Polished SK."),
    ])
    result = parse_polish_jsonl(p)
    assert 1 in result
    assert result[1].model_text == "Model SK."
    assert result[1].polished_text == "Polished SK."
    assert result[1].locator_path == "I.q1.a1.arg1"


def test_parse_polish_jsonl_skips_non_polished_status(tmp_path):
    p = tmp_path / "debug.jsonl"
    _write_jsonl(p, [
        _polish(2, "I.q1.a1.arg2", "", status="skipped"),
        _polish(3, "I.q1.a1.arg3", "", status="error"),
        _polish(4, "I.q1.a1.arg4", "", status="no_source"),
    ])
    result = parse_polish_jsonl(p)
    assert result == {}


def test_parse_polish_jsonl_skips_missing_polished_text(tmp_path):
    p = tmp_path / "debug.jsonl"
    rec = _polish(5, "I.q1.a1.arg5", "Polished.")
    rec["polished_text"] = None
    _write_jsonl(p, [rec])
    result = parse_polish_jsonl(p)
    assert 5 not in result


def test_parse_polish_jsonl_no_final_record(tmp_path):
    """Polish record without a matching final record: model_text is None."""
    p = tmp_path / "debug.jsonl"
    _write_jsonl(p, [_polish(6, "I.q1.a1.arg6", "Polished.")])
    result = parse_polish_jsonl(p)
    assert 6 in result
    assert result[6].model_text is None


def test_parse_polish_jsonl_empty_file(tmp_path):
    p = tmp_path / "debug.jsonl"
    p.write_text("", encoding="utf-8")
    assert parse_polish_jsonl(p) == {}


def test_parse_polish_jsonl_raises_on_malformed_json(tmp_path):
    """Fail loudly: malformed JSONL raises ValueError with file + line context."""
    p = tmp_path / "debug.jsonl"
    p.write_text("{not valid json}\n", encoding="utf-8")
    try:
        parse_polish_jsonl(p)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert str(p) in str(exc)
        assert "line 1" in str(exc)


def test_parse_polish_jsonl_ignores_blank_lines(tmp_path):
    p = tmp_path / "debug.jsonl"
    p.write_text(
        "\n" + json.dumps(_final(7, "I.q1.a1.arg7", "M")) + "\n\n"
        + json.dumps(_polish(7, "I.q1.a1.arg7", "P")) + "\n",
        encoding="utf-8",
    )
    result = parse_polish_jsonl(p)
    assert 7 in result


# ── _guard_line ───────────────────────────────────────────────────────────────


def test_guard_line_ok():
    flags = {"ok": True, "length_ratio": 0.98, "sentence_delta": 0,
             "term_retention_ok": True, "missing_terms": [],
             "particle_retention_ok": True, "missing_particles": []}
    line = _guard_line(flags)
    assert "ok=True" in line
    assert "0.98" in line
    assert "sentence_delta" not in line


def test_guard_line_failures():
    flags = {"ok": False, "length_ratio": 0.3, "sentence_delta": 2,
             "term_retention_ok": False, "missing_terms": ["rozum"],
             "particle_retention_ok": False, "missing_particles": ["totiž"]}
    line = _guard_line(flags)
    assert "ok=False" in line
    assert "sentence_delta=2" in line
    assert "missing_terms" in line
    assert "rozum" in line
    assert "missing_particles" in line
    assert "totiž" in line


# ── _render_polish_pair ───────────────────────────────────────────────────────


def test_render_polish_pair_contains_both_texts():
    prior = _make_record(1, polished_text="Prior polish.")
    current = _make_record(1, polished_text="Current polish.")
    rendered = _render_polish_pair(prior, current)
    assert "Prior polish." in rendered
    assert "Current polish." in rendered
    assert "[1] PRIOR POLISH" in rendered
    assert "[2] CURRENT POLISH" in rendered


def test_render_polish_pair_shows_model_text():
    prior = _make_record(1, model_text="Model draft text.")
    current = _make_record(1)
    rendered = _render_polish_pair(prior, current)
    assert "Model draft text." in rendered


def test_render_polish_pair_shows_locator():
    prior = _make_record(1, locator="I.q3.a2.respondeo")
    current = _make_record(1, locator="I.q3.a2.respondeo")
    rendered = _render_polish_pair(prior, current)
    assert "I.q3.a2.respondeo" in rendered


# ── build_polish_report ───────────────────────────────────────────────────────


def test_build_polish_report_empty_overlap(tmp_path):
    """Two runs with no shared segment IDs — no comparisons, no decisions prompt."""
    prior_p = tmp_path / "prior.jsonl"
    current_p = tmp_path / "current.jsonl"
    _write_jsonl(prior_p, [_final(1, "a", "M"), _polish(1, "a", "P")])
    _write_jsonl(current_p, [_final(2, "b", "M"), _polish(2, "b", "P")])

    inputs = iter([])
    report, dec_path = build_polish_report(
        prior_p, current_p, _input_fn=lambda _: next(inputs), output_dir=tmp_path
    )
    assert "Comparable pairs:   0" in report
    assert dec_path.exists()


def test_build_polish_report_collects_decisions(tmp_path):
    prior_p = tmp_path / "prior.jsonl"
    current_p = tmp_path / "current.jsonl"
    _write_jsonl(prior_p, [_final(1, "I.q1.a1.arg1", "M1"), _polish(1, "I.q1.a1.arg1", "PriorP")])
    _write_jsonl(current_p, [_final(1, "I.q1.a1.arg1", "M1"), _polish(1, "I.q1.a1.arg1", "CurrP")])

    inputs = iter(["2 flows better"])
    report, dec_path = build_polish_report(
        prior_p, current_p, _input_fn=lambda _: next(inputs), output_dir=tmp_path
    )
    assert "Prefer current (2): 1" in report
    content = dec_path.read_text()
    assert "preference=2" in content
    assert "flows better" in content


def test_build_polish_report_skip_decision(tmp_path):
    prior_p = tmp_path / "prior.jsonl"
    current_p = tmp_path / "current.jsonl"
    _write_jsonl(prior_p, [_final(1, "I.q1.a1.arg1", "M"), _polish(1, "I.q1.a1.arg1", "P1")])
    _write_jsonl(current_p, [_final(1, "I.q1.a1.arg1", "M"), _polish(1, "I.q1.a1.arg1", "P2")])

    inputs = iter(["s"])
    report, _ = build_polish_report(
        prior_p, current_p, _input_fn=lambda _: next(inputs), output_dir=tmp_path
    )
    assert "Skipped (s):        1" in report


def test_build_polish_report_invalid_then_valid_input(tmp_path):
    """Invalid input ('x') is ignored; next valid input ('1') is accepted."""
    prior_p = tmp_path / "prior.jsonl"
    current_p = tmp_path / "current.jsonl"
    _write_jsonl(prior_p, [_final(1, "I.q1.a1.arg1", "M"), _polish(1, "I.q1.a1.arg1", "P1")])
    _write_jsonl(current_p, [_final(1, "I.q1.a1.arg1", "M"), _polish(1, "I.q1.a1.arg1", "P2")])

    inputs = iter(["x", "", "1"])
    report, _ = build_polish_report(
        prior_p, current_p, _input_fn=lambda _: next(inputs), output_dir=tmp_path
    )
    assert "Prefer prior  (1):  1" in report


def test_build_polish_report_guard_delta(tmp_path):
    prior_p = tmp_path / "prior.jsonl"
    current_p = tmp_path / "current.jsonl"
    # prior: ok=True, current: ok=False
    _write_jsonl(prior_p, [
        _final(1, "I.q1.a1.arg1", "M"),
        _polish(1, "I.q1.a1.arg1", "P1", ok=True),
    ])
    _write_jsonl(current_p, [
        _final(1, "I.q1.a1.arg1", "M"),
        _polish(1, "I.q1.a1.arg1", "P2", ok=False),
    ])

    inputs = iter(["s"])
    report, _ = build_polish_report(
        prior_p, current_p, _input_fn=lambda _: next(inputs), output_dir=tmp_path
    )
    assert "GUARD DELTA" in report
    # 1/1 (100%) prior ok → 0/1 (0.0%) current ok
    assert "100.0%" in report
    assert "0.0%" in report


def test_build_polish_report_writes_decisions_file(tmp_path):
    prior_p = tmp_path / "prior.jsonl"
    current_p = tmp_path / "current.jsonl"
    _write_jsonl(prior_p, [_final(1, "I.q1.a1.arg1", "M"), _polish(1, "I.q1.a1.arg1", "P1")])
    _write_jsonl(current_p, [_final(1, "I.q1.a1.arg1", "M"), _polish(1, "I.q1.a1.arg1", "P2")])

    inputs = iter(["1"])
    _, dec_path = build_polish_report(
        prior_p, current_p, _input_fn=lambda _: next(inputs), output_dir=tmp_path
    )
    assert dec_path.name.startswith("polish_decisions_")
    assert dec_path.suffix == ".txt"


# ── PolishOutcome.polished_text flows through pilot ───────────────────────────


def test_polish_outcome_has_polished_text_field():
    """PolishOutcome gained polished_text in Phase 4; ensure field exists."""
    from polish.polisher import PolishOutcome
    outcome = PolishOutcome(segment_id=1)
    assert hasattr(outcome, "polished_text")
    assert outcome.polished_text is None


def test_log_polish_writes_polished_text_to_file(tmp_path):
    """Full PromptLogger lifecycle: log_polish with polished_text reaches the JSONL file."""
    from translate.prompt_logger import PromptLogger

    log_path = tmp_path / "debug.jsonl"
    with PromptLogger(log_path) as pl:
        pl.log_polish(
            segment_id=42,
            locator_path="I.q1.a1.arg1",
            status="polished",
            guard_flags={"ok": True},
            cost_usd=0.002,
            polished_text="Polished Slovak text.",
        )

    records = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    rec = records[0]
    assert rec["type"] == "polish"
    assert rec["polished_text"] == "Polished Slovak text."
    assert rec["segment_id"] == 42


def test_log_polish_polished_text_none_is_omitted_from_filter(tmp_path):
    """parse_polish_jsonl skips records where polished_text is None (no polished_text key)."""
    from translate.prompt_logger import PromptLogger

    log_path = tmp_path / "debug.jsonl"
    with PromptLogger(log_path) as pl:
        pl.log_polish(
            segment_id=99,
            locator_path="I.q1.a1.arg9",
            status="polished",
            guard_flags={"ok": True},
            cost_usd=0.001,
            polished_text=None,
        )

    result = parse_polish_jsonl(log_path)
    assert 99 not in result
