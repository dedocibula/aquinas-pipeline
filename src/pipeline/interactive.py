"""Interactive pipeline driver — ``python -m pipeline``.

Shows where the corpus stands in the flow (DB status + the last command you ran)
and a numbered menu whose every item invokes a `PipelineStep` through the
`Runner`. No operation logic lives here: each menu entry delegates to the Step
that owns the work, so the driver and the per-stage CLIs share one
implementation and every action gets the runner's timing + per-stage report for
free.

The "where am I" view combines:
  - live DB status — segment translation_status counts, glossary sense status
    counts, and the most recent translation_run (via the repositories);
  - the last command — persisted to ``.pipeline_state.json`` (no DDL) so the
    driver remembers across invocations what you last did.

Usage:
  uv run python -m pipeline            # interactive menu loop
  uv run python -m pipeline --status   # print status once and exit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from pipeline.context import PipelineContext
from pipeline.runner import Runner
from pipeline.step import PipelineStep

ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = ROOT / "reports"
STATE_FILE = ROOT / ".pipeline_state.json"

# Env knobs the resolve step reads; threaded through the context like the
# non-interactive pipeline does.
_KNOB_KEYS = (
    "GAP_FREQ_FLOOR",
    "GAP_BATCH_SIZE",
    "GAP_MAX_WORKERS",
    "GAP_FREQ_CEILING_PCT",
)


# ── Flow position ───────────────────────────────────────────────────────────


@dataclass
class StatusSnapshot:
    """Where the corpus stands, assembled from DB reads."""

    segment_counts: dict[str, int]
    sense_counts: dict[str, int]
    last_run: dict | None


def gather_status(ctx: PipelineContext) -> StatusSnapshot:
    """Read the live flow position from the DB via the repositories."""
    from storage.db import work_id
    from storage.repositories import (
        GlossaryRepository,
        RunRepository,
        SegmentRepository,
    )

    with ctx.connection() as conn:
        wid = ctx.work_id if ctx.work_id is not None else work_id(conn, "summa_articulus")
        segment_counts = SegmentRepository(conn).translation_status_counts(wid)
        sense_counts = GlossaryRepository(conn).sense_status_counts()
        last_run = RunRepository(conn).last_run()
    return StatusSnapshot(segment_counts, sense_counts, last_run)


def render_status(snapshot: StatusSnapshot, state: dict) -> str:
    """Render the flow-position banner."""
    lines = ["", "=" * 60, "PIPELINE STATUS", "=" * 60]

    seg = snapshot.segment_counts
    seg_total = sum(seg.values())
    lines.append(f"Segments ({seg_total} total):")
    if seg:
        for status in ("pending", "translated", "needs_human"):
            if status in seg:
                lines.append(f"  {status:<14} {seg[status]:>6}")
        for status in sorted(set(seg) - {"pending", "translated", "needs_human"}):
            lines.append(f"  {status:<14} {seg[status]:>6}")
    else:
        lines.append("  (none — corpus not yet ingested)")

    sense = snapshot.sense_counts
    sense_total = sum(sense.values())
    lines.append(f"Glossary senses ({sense_total} total):")
    if sense:
        for status in ("proposed", "flagged", "approved"):
            if status in sense:
                lines.append(f"  {status:<14} {sense[status]:>6}")
    else:
        lines.append("  (none)")

    run = snapshot.last_run
    if run:
        when = run.get("finished_at") or run.get("started_at")
        state_word = "finished" if run.get("finished_at") else "RUNNING/crashed"
        lines.append(
            f"Last run: #{run['run_id']} {run['flow_name']} ({state_word}, {when})"
        )
        if run.get("finished_at"):
            lines.append(
                f"  translated={run.get('total_translated')} "
                f"needs_human={run.get('total_needs_human')} "
                f"cost=${run.get('total_cost_usd')}"
            )
    else:
        lines.append("Last run: (none)")

    last_cmd = state.get("last_command")
    if last_cmd:
        lines.append(f"Last command: {last_cmd} (at {state.get('at', '?')})")

    lines.append("")
    return "\n".join(lines)


# ── State persistence (no DDL — a small JSON file) ──────────────────────────


def load_state(path: Path | None = None) -> dict:
    path = path if path is not None else STATE_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt state file is non-fatal context, not pipeline data.
        return {}


def save_state(token: str, path: Path | None = None) -> None:
    path = path if path is not None else STATE_FILE
    path.write_text(
        json.dumps({"last_command": token, "at": datetime.now().isoformat(timespec="seconds")}),
        encoding="utf-8",
    )


# ── Menu (every item is a Step) ─────────────────────────────────────────────


@dataclass
class MenuItem:
    token: str
    label: str
    factory: Callable[[], PipelineStep]


def build_menu() -> list[MenuItem]:
    """The ordered menu. Each factory builds the Step that does the work.

    Imports are local so the driver imports cheaply and a missing optional
    dependency in one stage doesn't break the whole menu at import time.
    """

    def _verify() -> PipelineStep:
        from acquire.steps import VerifySourcesStep

        return VerifySourcesStep()

    def _latin() -> PipelineStep:
        from ingest.steps import LatinStep

        return LatinStep()

    def _bahounek() -> PipelineStep:
        from ingest.steps import BahounekStep

        return BahounekStep()

    def _english() -> PipelineStep:
        from ingest.steps import EnglishStep

        return EnglishStep()

    def _resolve() -> PipelineStep:
        from ingest.steps import ResolveStep

        return ResolveStep()

    def _mine() -> PipelineStep:
        from ingest.steps import MineSensesStep

        return MineSensesStep()

    def _export() -> PipelineStep:
        from review.steps import ExportReviewStep

        return ExportReviewStep()

    def _import() -> PipelineStep:
        from review.steps import ImportApprovalsStep

        return ImportApprovalsStep()

    def _translate() -> PipelineStep:
        from translate.steps import TranslateCorpusStep

        return TranslateCorpusStep()

    def _rerun() -> PipelineStep:
        from translate.steps import RerunStaleStep

        return RerunStaleStep()

    def _retranslate() -> PipelineStep:
        from translate.steps import RetranslateBodyStep

        return RetranslateBodyStep()

    def _report() -> PipelineStep:
        from ingest.steps import ReportStep

        return ReportStep()

    return [
        MenuItem("verify", "Verify sources", _verify),
        MenuItem("latin", "Ingest Latin corpus", _latin),
        MenuItem("bahounek", "Ingest Czech (Bahounek) overlay", _bahounek),
        MenuItem("english", "Ingest English overlay", _english),
        MenuItem("resolve", "Recollect terms (run resolver)", _resolve),
        MenuItem("mine-senses", "Mine senses + label + write proposed", _mine),
        MenuItem("export-review", "Export glossary to review sheet", _export),
        MenuItem("import-approvals", "Import approvals + flag stale", _import),
        MenuItem("translate", "Translate corpus", _translate),
        MenuItem("rerun-stale", "Re-translate stale segments", _rerun),
        MenuItem("retranslate-body", "Re-translate all body segments", _retranslate),
        MenuItem("report", "Coverage / provenance report", _report),
    ]


def render_menu(menu: list[MenuItem]) -> str:
    lines = ["Choose a step (number), 'r' to refresh status, 'q' to quit:"]
    for i, item in enumerate(menu, 1):
        lines.append(f"  {i:>2}. {item.label}")
    return "\n".join(lines)


# ── Loop ────────────────────────────────────────────────────────────────────


def _context() -> PipelineContext:
    knobs = {k: os.environ[k] for k in _KNOB_KEYS if k in os.environ}
    return PipelineContext(reports_dir=REPORTS_DIR, knobs=knobs)


def run_loop(
    ctx: PipelineContext,
    *,
    read: Callable[[str], str] = input,
    out=None,
    gather: Callable[[PipelineContext], StatusSnapshot] = gather_status,
    make_runner: Callable[[], Runner] | None = None,
) -> int:
    """Drive the interactive menu until the user quits. Returns an exit code."""
    out = out if out is not None else sys.stdout
    menu = build_menu()
    if make_runner is None:
        make_runner = lambda: Runner(ctx, out=out, err=out)  # noqa: E731

    while True:
        state = load_state()
        try:
            snapshot = gather(ctx)
            print(render_status(snapshot, state), file=out)
        except Exception as exc:  # DB unreachable etc. — surface it, keep going
            print(f"\n[status unavailable: {exc}]\n", file=out)
        print(render_menu(menu), file=out)

        try:
            choice = read("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("", file=out)
            return 0

        if choice in ("q", "quit", "0", ""):
            return 0
        if choice in ("r", "refresh"):
            continue
        if not choice.isdigit() or not (1 <= int(choice) <= len(menu)):
            print(f"Unrecognized choice: {choice!r}", file=out)
            continue

        item = menu[int(choice) - 1]
        try:
            step = item.factory()
        except Exception as exc:  # broken optional dep in this stage — surface, don't crash the loop
            print(f"[{item.token}] unavailable: {exc}", file=out)
            continue
        results = make_runner().run([step])
        save_state(item.token)
        if not all(r.ok for r in results):
            print(f"[{item.token}] reported a failure — see the report above.", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Interactive pipeline driver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the flow-position status once and exit (non-interactive).",
    )
    args = parser.parse_args(argv)

    ctx = _context()

    if args.status:
        try:
            snapshot = gather_status(ctx)
        except Exception as exc:
            print(f"status unavailable: {exc}", file=sys.stderr)
            return 1
        print(render_status(snapshot, load_state()))
        return 0

    return run_loop(ctx)


if __name__ == "__main__":
    sys.exit(main())
