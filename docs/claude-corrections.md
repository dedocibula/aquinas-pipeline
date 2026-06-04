# Claude Corrections Log
Read this file at the start of every session before doing any work.

### [Parallel subagent dispatch for independent steps]
- **Approach:** When a milestone has multiple independent steps (e.g., Steps 2/3/6 after schema migration, or Steps 4/5 after the Latin parser), dispatch them as concurrent subagents rather than running sequentially.
- **Why:** Independent steps share no data dependencies — running them in parallel saves wall-clock time and keeps context windows focused.
- **Rule:** Before implementing any step, check whether it depends on a prior step's DB output. If not, it can be delegated to a parallel subagent. Always dispatch parallel work in a single message with multiple Agent tool calls.

### [LLM batch translation: skip already-done work, write immediately]
- **Mistake:** Re-sent all ~3,496 gap lemmas to DeepSeek on every retry run, even the ~3,442 that were successfully translated in a prior run. Also collected all batch results before writing to DB — a crash after all API calls but before commit would lose everything.
- **Correction:** (1) Before calling the LLM, load existing proposed terms from DB and filter them out of the pending set. (2) Write each successful batch to DB and commit immediately after it completes — not in a single commit at the end.
- **Rule:** Any pipeline that calls an LLM in batches and writes results to a store must (a) check what is already stored before sending requests, and (b) persist each batch immediately on success. This applies to gap-term translation in M2 and to any future full-corpus translation loop in M4+.

### [Reading the database: use hardcoded connection string, not env var]
- **Mistake:** Tried to read the DB via `psql "$DATABASE_URL"` (psql not on PATH) and then via `os.environ['DATABASE_URL']` without loading `.env` first — both fail in this shell environment.
- **Correction:** Use `psycopg2.connect('postgresql://aquinas:aquinas@localhost:5432/aquinas')` directly. The connection string is in `.env` (`DATABASE_URL=postgresql://aquinas:aquinas@localhost:5432/aquinas`) and in `docker-compose.yml`. Hardcode it for one-off DB queries; load `.env` via `python-dotenv` only in production scripts.
- **Rule:** When issuing ad-hoc DB queries, always use `uv run python -c "import psycopg2; conn = psycopg2.connect('postgresql://aquinas:aquinas@localhost:5432/aquinas'); ..."`. Do not rely on `psql` being in PATH or on environment variables being pre-loaded.

### [Source layout: code in src/, tests in tests/]
- **Mistake:** Placed `verify.py` at `src/verify.py` (project root of src/) rather than inside the `src/acquire/` package where all production code lives. Tests were added correctly in `tests/` but the production module was outside the package.
- **Correction:** All production Python modules belong inside `src/acquire/` (the package exposed by hatchling). Thin entry-point wrappers (like `verify_sources.py`) may live at the project root only if they do nothing but delegate to a module inside `src/acquire/`.
- **Rule:** When creating a new production module, always place it under `src/acquire/`. Never add `.py` files directly under `src/` that are not part of the `acquire` package.
