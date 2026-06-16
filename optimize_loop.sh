#!/usr/bin/env bash
# Automated prompt optimization loop for the Aquinas translation pipeline.
# Usage: ./optimize_loop.sh [EPOCHS]   (default: 5)
#
# Each epoch:
#   1. Reset the 200-segment golden set to pending
#   2. Run a full pilot (translator + R1 reviewer)
#   3. Compare this run against the previous one
#   4. Ask Claude Code to improve both system prompts and log changes
set -euo pipefail

EPOCHS=${1:-5}
export PILOT_SAMPLE_FILE="docs/pilot_sample_200.json"
DB_CMD="docker exec aquinas-pipeline-db-1 psql -U aquinas -d aquinas -t -A"

for epoch in $(seq 1 "$EPOCHS"); do
    echo ""
    echo "=== Epoch $epoch / $EPOCHS ==="

    # 1. Reset golden set to pending
    uv run python -m translate.reset_golden

    # 2. Pilot on golden set (sample file set above via PILOT_SAMPLE_FILE)
    PILOT_WORKERS=5 uv run python -m translate.pilot

    # 3. Get the last two run IDs (newest first)
    RUN_IDS=$($DB_CMD -c \
        "SELECT run_id FROM translation_run ORDER BY started_at DESC LIMIT 2;")
    RUN_B=$(echo "$RUN_IDS" | sed -n '1p' | tr -d '[:space:]')
    RUN_A=$(echo "$RUN_IDS" | sed -n '2p' | tr -d '[:space:]')

    if [[ -z "$RUN_A" ]]; then
        echo "Only one run recorded — skipping compare on first epoch."
        continue
    fi

    # 4. Compare runs; symlink latest for Claude
    uv run python -m translate.run_compare "$RUN_A" "$RUN_B"
    cp "reports/run_compare_${RUN_A}_${RUN_B}.txt" reports/latest_compare.txt
    echo "Compare written to reports/latest_compare.txt"

    # 5. Let Claude optimize both prompts and log code suggestions
    claude --dangerously-skip-permissions -p "Read reports/latest_compare.txt and prompt_changelog.md.
Analyze the failure class deltas between run $RUN_A (baseline) and run $RUN_B (candidate).
You may modify BOTH prompts/translator_system.txt and prompts/reviewer_system.txt
if either needs improvement; explain your reasoning for each file.

STRICT CONSTRAINT — NO OVERFITTING:
The golden set is a 100-segment sample, not the full corpus. Per-term lists derived
from sample failure counts are sample-specific noise, not general rules.
You MUST NOT:
  - Add forbidden-synonym or NEVER-USE lists for specific Slovak terms
  - Enumerate term-specific inflection tables
  - Add any rule that names a specific glossary term (čnosť, vášeň, pamäť, etc.)
The hard_constraints XML block already delivers per-term requirements at inference
time for every segment in the corpus. Prompt additions that duplicate or extend that
mechanism for a subset of terms are overfitting and will be reverted.

ALLOWED changes to prompts:
  - Structural/format guidance that applies to ALL translated segments
  - General rules about how to handle hard_constraints as a class (not specific terms)
  - Reviewer scoring criteria improvements
  - Example improvements that illustrate a general principle

If you identify failure patterns that require a code fix (e.g. in prechecks.py, loop.py, or translator.py),
document the suggested change clearly — what function, what change, and why.
Append a one-row entry to the markdown table in prompt_changelog.md using these columns:
  Epoch | Git SHA | Prompt Hypothesis | Pass Rate | Code Suggestions | Notes
where Pass Rate is (translated / total) from the latest pilot report (reports/m4_sample.txt).
Commit all modified files with a conventional-commit message starting 'prompt(opt): epoch $epoch'."

done

echo ""
echo "=== Optimization complete after $EPOCHS epochs. ==="
