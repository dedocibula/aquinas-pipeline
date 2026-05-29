# Claude Corrections Log
Read this file at the start of every session before doing any work.

### [Parallel subagent dispatch for independent steps]
- **Approach:** When a milestone has multiple independent steps (e.g., Steps 2/3/6 after schema migration, or Steps 4/5 after the Latin parser), dispatch them as concurrent subagents rather than running sequentially.
- **Why:** Independent steps share no data dependencies — running them in parallel saves wall-clock time and keeps context windows focused.
- **Rule:** Before implementing any step, check whether it depends on a prior step's DB output. If not, it can be delegated to a parallel subagent. Always dispatch parallel work in a single message with multiple Agent tool calls.

### [Source layout: code in src/, tests in tests/]
- **Mistake:** Placed `verify.py` at `src/verify.py` (project root of src/) rather than inside the `src/acquire/` package where all production code lives. Tests were added correctly in `tests/` but the production module was outside the package.
- **Correction:** All production Python modules belong inside `src/acquire/` (the package exposed by hatchling). Thin entry-point wrappers (like `verify_sources.py`) may live at the project root only if they do nothing but delegate to a module inside `src/acquire/`.
- **Rule:** When creating a new production module, always place it under `src/acquire/`. Never add `.py` files directly under `src/` that are not part of the `acquire` package.
