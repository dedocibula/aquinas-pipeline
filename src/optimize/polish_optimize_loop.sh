#!/usr/bin/env bash
# Polish prompt optimization loop.
# Usage: ./src/optimize/polish_optimize_loop.sh [EPOCHS]   (default: 5)
# Must be run from the repository root (relative paths: reports/, prompts/, src/optimize/).
#
# Each epoch:
#   1. Reset the golden set to pending (deletes sk/model + sk/polish rows; sk/human untouched)
#   2. Run a full pilot (translate + polish per segment)
#   3. Compare this epoch's polish against the prior epoch's, side by side
#   4. Block until the user enters 1/2 preferences for each segment
#   5. Ask Claude to improve prompts/polish_system.txt based on the preference summary
#   6. Append a row to src/optimize/polish_prompt_changelog.md and commit
set -euo pipefail

EPOCHS=${1:-5}
export PILOT_SAMPLE_FILE="src/optimize/samples/pilot_sample_100.json"

for epoch in $(seq 1 "$EPOCHS"); do
    echo ""
    echo "=== Polish Epoch $epoch / $EPOCHS ==="

    # 1. Reset golden set (model + polish rows deleted; human rows preserved)
    uv run python -m optimize.reset_golden

    # 2. Full pilot: translate every segment, then polish each translated segment
    PILOT_WORKERS=8 uv run python -m optimize.pilot

    # 3. Collect the two most recent pilot JSONL files (oldest first).
    # mapfile is bash 4+; macOS ships bash 3.2, so use a plain assignment instead.
    # shellcheck disable=SC2207
    SORTED=( $(ls -t reports/translate/debug/debug_*.jsonl 2>/dev/null | head -2 | tac) )

    if [[ ${#SORTED[@]} -lt 2 ]]; then
        echo "Only one run recorded — skipping compare on first epoch."
        continue
    fi

    PRIOR_JSONL="${SORTED[0]}"
    CURRENT_JSONL="${SORTED[1]}"
    echo "Comparing:"
    echo "  Prior:   $PRIOR_JSONL"
    echo "  Current: $CURRENT_JSONL"

    # 4. Side-by-side polish comparison — blocks on user 1/2/s input per segment
    uv run python -m optimize.run_compare --polish "$PRIOR_JSONL" "$CURRENT_JSONL"

    # 5. Find the decisions file written by the comparison (run_compare writes it to reports/)
    DECISIONS=$(ls -t reports/polish_decisions_*.txt 2>/dev/null | head -1 || true)
    if [[ -z "$DECISIONS" ]]; then
        echo "No decisions file found — skipping prompt update."
        continue
    fi

    GUARD_STATS=""
    if [[ -f reports/m5_polish_sample.txt ]]; then
        GUARD_STATS=$(cat reports/m5_polish_sample.txt)
    fi

    # 6. Let Claude improve the polish prompt and commit
    claude --dangerously-skip-permissions -p "Read prompts/polish_system.txt and src/optimize/polish_prompt_changelog.md.

You just completed polish epoch $epoch. The user compared prior vs current polish outputs
for each segment and recorded 1/2/s preferences in the decisions file below.

$(cat "$DECISIONS")

Guard pass-rates from the current pilot run:
$GUARD_STATS

Analyze what the 1/2 preferences reveal about prompts/polish_system.txt. A majority of
'1' (prior) means the current prompt degraded quality; '2' means improvement. Look for
patterns in the notes and guard deltas.

Improve prompts/polish_system.txt based on this evidence.

STRICT CONSTRAINT — NO OVERFITTING:
The golden set is a small sample; per-term observations are noise, not general rules.
You MUST NOT:
  - Add forbidden-synonym or NEVER-USE lists for specific Slovak terms
  - Enumerate term-specific inflection tables
  - Add any rule that names a specific glossary term (rozum, princíp, etc.)
  - Enumerate specific constraint values from the decisions file
The <hard_constraints> XML block already delivers per-term requirements at inference time.

ALLOWED changes:
  - Structural/format guidance that applies to ALL polished segments
  - General principles for retaining scholastic particles and sentence boundaries
  - Guidance on what makes prose flow better vs worse in this theological register
  - Removing or softening instructions shown to hurt quality

Append a one-row entry to the markdown table in src/optimize/polish_prompt_changelog.md:
  Epoch | Git SHA | Prompt Hypothesis | Preference (1s/2s/skip) | Guard Pass Rate | Notes

Commit all modified files with: 'prompt(polish): epoch $epoch'"

done

echo ""
echo "=== Polish optimization complete after $EPOCHS epochs. ==="
