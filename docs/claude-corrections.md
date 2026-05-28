# Claude Corrections Log
Read this file at the start of every session before doing any work.

### [Source layout: code in src/, tests in tests/]
- **Mistake:** Placed `verify.py` at `src/verify.py` (project root of src/) rather than inside the `src/acquire/` package where all production code lives. Tests were added correctly in `tests/` but the production module was outside the package.
- **Correction:** All production Python modules belong inside `src/acquire/` (the package exposed by hatchling). Thin entry-point wrappers (like `verify_sources.py`) may live at the project root only if they do nothing but delegate to a module inside `src/acquire/`.
- **Rule:** When creating a new production module, always place it under `src/acquire/`. Never add `.py` files directly under `src/` that are not part of the `acquire` package.
