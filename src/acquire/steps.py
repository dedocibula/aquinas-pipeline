"""Source verification as a pipeline step.

Wraps the source acceptance checks (`acquire.verify`) as `VerifySourcesStep` so a
run can gate on it: if the source tree, DB, or env is broken, the runner stops
before any downstream step touches the database.

Entry point: ``python -m acquire.steps`` (drives `main`).
"""

from __future__ import annotations

import sys

from acquire.verify import CHECKS, ROOT
from pipeline import BaseStep, PipelineContext, Runner, StepResult


class VerifySourcesStep(BaseStep):
    """Run every source check; ok only if all of them pass.

    Each check prints its own status line (``[OK]``/``[FAIL]``) and returns a
    bool. An unexpected exception in a check is reported and counts as a
    failure — verification never crashes the runner, it just reports ``ok=False``
    so downstream ingest steps refuse to run.
    """

    name = "verify-sources"
    stage = "acquire"

    def __init__(self, checks: list[tuple[str, object]] | None = None) -> None:
        self._checks = list(CHECKS if checks is None else checks)

    def run(self, ctx: PipelineContext) -> StepResult:
        results: dict[str, bool] = {}
        for label, fn in self._checks:
            try:
                results[label] = bool(fn())
            except Exception as exc:  # fail loud, but report every check
                print(f"  [FAIL] {label} — unexpected error: {exc}", file=sys.stderr)
                results[label] = False

        passed = sum(1 for ok in results.values() if ok)
        total = len(results)
        return StepResult(
            name=self.name,
            ok=passed == total,
            summary=f"{passed}/{total} source checks passed",
            details={"checks": results},
        )


def main(argv: list[str] | None = None) -> int:
    """Verify all sources via the runner; exit 0 iff every check passes."""
    ctx = PipelineContext(reports_dir=ROOT / "reports")
    results = Runner(ctx).run([VerifySourcesStep()])
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
