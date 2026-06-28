"""Compare two translation or polish runs for regressions and improvements.

Translation mode (default):
    Reads translation_run + run_segment (migration 005) and reports:
      - per-run summary (totals, cost, avg iterations, code/prompt/glossary state)
      - segment status flips (improved: needs_human → translated; regressed: reverse)
      - failure-class deltas (e.g. did precheck_terminology failures go down?)

    Usage:
        uv run python -m optimize.run_compare <run_a> <run_b>

    run_a is the baseline (older), run_b the candidate (newer). Only segments
    present in BOTH runs are compared for flips — a subset run against a full run
    compares the intersection. Output goes to reports/run_compare_<a>_<b>.txt and
    stdout. Deep dives (full prompts/drafts) live in the PromptLogger JSONL
    referenced by translation_run.jsonl_path.

Polish mode (--polish):
    Reads two PromptLogger JSONL files (reports/translate/debug/debug_<ts>.jsonl)
    produced by optimize.pilot and, for each segment polished in both runs, shows
    the model draft + prior/current polished text side by side with guard deltas.
    The user enters "1" (prior better), "2" (current better), or "s" (skip) for
    each segment. Decisions are written to reports/polish_decisions_<ts>.txt for
    polish_optimize_loop.sh to consume.

    Usage:
        uv run python -m optimize.run_compare --polish <prior.jsonl> <current.jsonl>

    Note: polish rows are deleted by reset_golden between epochs, so the JSONL is
    the only source of prior-epoch polished text.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from storage.db import get_conn

_REPORTS_DIR = Path("reports")


def fetch_run_summary(conn, run_id: int) -> dict:
    """Return the translation_run row plus derived avg iterations, or raise."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.flow_name, r.started_at, r.finished_at, r.git_sha,
                   r.prompt_hash, r.glossary_snapshot, r.translator_model,
                   r.reviewer_model, r.filters, r.total_segments,
                   r.total_translated, r.total_needs_human, r.total_cost_usd,
                   avg(s.iterations_used) AS avg_iterations
            FROM translation_run r
            LEFT JOIN run_segment s USING (run_id)
            WHERE r.run_id = %s
            GROUP BY r.run_id
            """,
            (run_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"run_id={run_id} not found in translation_run")
    keys = (
        "flow_name", "started_at", "finished_at", "git_sha", "prompt_hash",
        "glossary_snapshot", "translator_model", "reviewer_model", "filters",
        "total_segments", "total_translated", "total_needs_human",
        "total_cost_usd", "avg_iterations",
    )
    return dict(zip(keys, row))


def fetch_status_flips(conn, run_a: int, run_b: int) -> list[tuple]:
    """Return (segment_id, locator_path, status_a, status_b) where status changed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.segment_id, seg.locator_path::text,
                   a.final_status, b.final_status
            FROM run_segment a
            JOIN run_segment b USING (segment_id)
            JOIN segment seg USING (segment_id)
            WHERE a.run_id = %s
              AND b.run_id = %s
              AND a.final_status <> b.final_status
            ORDER BY seg.locator_path
            """,
            (run_a, run_b),
        )
        return cur.fetchall()


def fetch_failure_class_counts(conn, run_id: int) -> Counter:
    """Count failure_classes entries by class (terminology keyed by term too)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fc->>'class' AS cls, fc->>'term' AS term, count(*)
            FROM run_segment, jsonb_array_elements(failure_classes) AS fc
            WHERE run_id = %s
            GROUP BY 1, 2
            """,
            (run_id,),
        )
        counts: Counter = Counter()
        for cls, term, n in cur.fetchall():
            key = f"{cls}({term})" if term else cls
            counts[key] = n
        return counts


def _format_summary(label: str, run_id: int, s: dict) -> list[str]:
    snapshot = s["glossary_snapshot"] or {}
    cost = s["total_cost_usd"]
    avg_iters = s["avg_iterations"]
    return [
        f"RUN {label} (run_id={run_id}, {s['flow_name']})",
        f"  started:      {s['started_at']}",
        f"  finished:     {s['finished_at'] or 'NOT FINISHED (crashed?)'}",
        f"  git sha:      {s['git_sha']}",
        f"  prompt hash:  {(s['prompt_hash'] or '')[:12]}",
        f"  glossary:     {snapshot.get('approved_senses')} approved senses"
        f" (max version {snapshot.get('max_version')})",
        f"  filters:      {s['filters']}",
        f"  segments:     {s['total_segments']}"
        f" ({s['total_translated']} translated, {s['total_needs_human']} needs_human)",
        f"  avg iters:    {float(avg_iters):.2f}" if avg_iters is not None else "  avg iters:    n/a",
        f"  cost:         ${float(cost):.2f}" if cost is not None else "  cost:         n/a",
    ]


def build_report(conn, run_a: int, run_b: int) -> str:
    summary_a = fetch_run_summary(conn, run_a)
    summary_b = fetch_run_summary(conn, run_b)
    flips = fetch_status_flips(conn, run_a, run_b)
    fails_a = fetch_failure_class_counts(conn, run_a)
    fails_b = fetch_failure_class_counts(conn, run_b)

    improved = [(sid, loc) for sid, loc, sa, sb in flips if sb == "translated"]
    regressed = [(sid, loc) for sid, loc, sa, sb in flips if sb == "needs_human"]

    lines: list[str] = [f"RUN COMPARISON: {run_a} (baseline) → {run_b} (candidate)", ""]
    lines += _format_summary("A", run_a, summary_a)
    lines.append("")
    lines += _format_summary("B", run_b, summary_b)

    if summary_a["prompt_hash"] != summary_b["prompt_hash"]:
        lines.append("\n  NOTE: prompt hash differs — prompt change between runs.")
    if summary_a["git_sha"] != summary_b["git_sha"]:
        lines.append("  NOTE: git sha differs — code change between runs.")

    lines += ["", f"STATUS FLIPS (segments in both runs): {len(flips)}"]
    lines.append(f"  improved (needs_human → translated): {len(improved)}")
    for _, loc in improved:
        lines.append(f"    + {loc}")
    lines.append(f"  regressed (translated → needs_human): {len(regressed)}")
    for _, loc in regressed:
        lines.append(f"    - {loc}")

    lines += ["", "FAILURE CLASS DELTAS (count in A → count in B):"]
    for key in sorted(set(fails_a) | set(fails_b)):
        a, b = fails_a.get(key, 0), fails_b.get(key, 0)
        marker = "▼" if b < a else ("▲" if b > a else " ")
        lines.append(f"  {marker} {key:<45} {a:>4} → {b:<4}")
    if not fails_a and not fails_b:
        lines.append("  (no failures recorded in either run)")

    return "\n".join(lines) + "\n"


# ── Polish comparison (JSONL-based) ───────────────────────────────────────────


@dataclass
class _PolishRecord:
    segment_id: int
    locator_path: str
    model_text: str | None    # from type='final', chosen_draft
    polished_text: str | None # from type='polish', polished_text
    guard_flags: dict


def parse_polish_jsonl(path: Path) -> dict[int, _PolishRecord]:
    """Parse a pilot JSONL; return polished records keyed by segment_id.

    Each segment combines:
      type='final'  → model_text (chosen_draft)
      type='polish' → polished_text, guard_flags, locator_path

    Only segments where type='polish' has status='polished' and a non-empty
    polished_text field are included (polished_text was added to log_polish in
    Phase 4; older JSONL files without it produce an empty dict).
    """
    finals: dict[int, dict] = {}
    polishes: dict[int, dict] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Malformed JSON in {path} at line {lineno}: {exc}\n  raw: {line[:200]!r}"
            ) from exc
        sid = rec.get("segment_id")
        if sid is None:
            continue
        t = rec.get("type")
        if t == "final":
            finals[sid] = rec
        elif t == "polish":
            polishes[sid] = rec

    result: dict[int, _PolishRecord] = {}
    for sid, p in polishes.items():
        if p.get("status") != "polished":
            continue
        polished_text = p.get("polished_text") or ""
        if not polished_text:
            continue
        final = finals.get(sid, {})
        result[sid] = _PolishRecord(
            segment_id=sid,
            locator_path=p.get("locator_path", ""),
            model_text=final.get("chosen_draft"),
            polished_text=polished_text,
            guard_flags=p.get("guard_flags", {}),
        )
    return result


def _guard_line(flags: dict) -> str:
    """One-line guard summary for display."""
    parts = [f"ok={flags.get('ok', '?')}"]
    ratio = flags.get("length_ratio")
    if isinstance(ratio, (int, float)):
        parts.append(f"ratio={ratio:.2f}")
    delta = flags.get("sentence_delta", 0)
    if delta:
        parts.append(f"sentence_delta={delta}")
    if not flags.get("term_retention_ok", True):
        parts.append(f"missing_terms={flags.get('missing_terms', [])}")
    if not flags.get("particle_retention_ok", True):
        parts.append(f"missing_particles={flags.get('missing_particles', [])}")
    return "  ".join(parts)


def _render_polish_pair(prior: _PolishRecord, current: _PolishRecord) -> str:
    sep = "=" * 70
    lines = [
        f"\n{sep}",
        f"  {prior.locator_path}  (segment_id={prior.segment_id})",
        sep,
    ]
    if prior.model_text:
        lines += ["TRANSLATION:", f"  {prior.model_text[:400]}"]
    lines += [
        "",
        "--- [1] PRIOR POLISH ---",
        f"  {prior.polished_text}",
        f"  guards: {_guard_line(prior.guard_flags)}",
        "",
        "--- [2] CURRENT POLISH ---",
        f"  {current.polished_text}",
        f"  guards: {_guard_line(current.guard_flags)}",
    ]
    return "\n".join(lines)


def build_polish_report(
    prior_path: Path,
    current_path: Path,
    *,
    _input_fn=None,
    output_dir: Path | None = None,
) -> tuple[str, Path]:
    """Run the interactive side-by-side polish comparison.

    Prints each pair to stdout, prompts for "1", "2", or "s" (skip) per segment,
    writes decisions to reports/polish_decisions_<ts>.txt.
    Returns (summary_report_str, decisions_path).

    _input_fn is a test seam replacing the built-in input().
    """
    _input = _input_fn or input
    out_dir = output_dir if output_dir is not None else _REPORTS_DIR

    prior = parse_polish_jsonl(prior_path)
    current = parse_polish_jsonl(current_path)
    shared_ids = sorted(set(prior) & set(current))

    print(
        f"\nPolish comparison: {prior_path.name} vs {current_path.name}"
        f"\n  Prior polished:   {len(prior)}"
        f"\n  Current polished: {len(current)}"
        f"\n  Comparable pairs: {len(shared_ids)}\n"
    )

    decisions: list[dict] = []
    counts: Counter = Counter()
    for sid in shared_ids:
        print(_render_polish_pair(prior[sid], current[sid]))
        while True:
            raw = _input(
                "\nPreference? 1 (prior), 2 (current), s (skip) [+note after space]: "
            ).strip()
            if not raw:
                continue
            parts = raw.split(" ", 1)
            choice = parts[0].lower()
            note = parts[1].strip() if len(parts) > 1 else ""
            if choice in ("1", "2", "s"):
                break
            print("  Enter 1, 2, or s (skip).")
        decisions.append({
            "segment_id": sid,
            "locator_path": prior[sid].locator_path,
            "preference": choice,
            "note": note,
        })
        counts[choice] += 1

    # Build summary report
    report_lines = [
        f"POLISH COMPARISON: {prior_path.name} (prior) → {current_path.name} (current)",
        f"  Prior polished:     {len(prior)}",
        f"  Current polished:   {len(current)}",
        f"  Comparable pairs:   {len(shared_ids)}",
        f"  Prefer prior  (1):  {counts['1']}",
        f"  Prefer current (2): {counts['2']}",
        f"  Skipped (s):        {counts['s']}",
        "",
        "GUARD DELTA (ok rate: prior → current)",
    ]
    prior_ok = sum(1 for sid in shared_ids if prior[sid].guard_flags.get("ok"))
    current_ok = sum(1 for sid in shared_ids if current[sid].guard_flags.get("ok"))
    n = len(shared_ids) or 1
    report_lines.append(
        f"  {prior_ok}/{len(shared_ids)} ({prior_ok/n*100:.1f}%) → "
        f"{current_ok}/{len(shared_ids)} ({current_ok/n*100:.1f}%)"
    )
    report_lines += ["", "SEGMENT DECISIONS"]
    for d in decisions:
        note_str = f"  // {d['note']}" if d["note"] else ""
        report_lines.append(
            f"  {d['locator_path']}  seg={d['segment_id']}"
            f"  preference={d['preference']}{note_str}"
        )

    report = "\n".join(report_lines) + "\n"

    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    decisions_path = out_dir / f"polish_decisions_{ts}.txt"
    decisions_path.write_text(report, encoding="utf-8")
    print(f"\nDecisions written to {decisions_path}")
    return report, decisions_path


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare two translation runs (default) or two polish JSONL runs (--polish)"
    )
    parser.add_argument(
        "--polish",
        action="store_true",
        help="Polish mode: compare two pilot JSONL files instead of DB run IDs",
    )
    parser.add_argument(
        "arg_a",
        help="baseline run_id (translation mode) or path to prior JSONL (--polish)",
    )
    parser.add_argument(
        "arg_b",
        help="candidate run_id (translation mode) or path to current JSONL (--polish)",
    )
    args = parser.parse_args()

    if args.polish:
        prior_path = Path(args.arg_a)
        current_path = Path(args.arg_b)
        if not prior_path.exists():
            parser.error(f"prior JSONL not found: {prior_path}")
        if not current_path.exists():
            parser.error(f"current JSONL not found: {current_path}")
        report, decisions_path = build_polish_report(prior_path, current_path)
        print(report)
    else:
        try:
            run_a, run_b = int(args.arg_a), int(args.arg_b)
        except ValueError:
            parser.error("Translation mode expects integer run_ids; use --polish for JSONL paths")
        with get_conn() as conn:
            report = build_report(conn, run_a, run_b)
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = _REPORTS_DIR / f"run_compare_{run_a}_{run_b}.txt"
        path.write_text(report, encoding="utf-8")
        print(report)
        print(f"Report written to {path}")


if __name__ == "__main__":
    main()
