# Claude Corrections Log
Read this file at the start of every session before doing any work.

### [Pilot translation: always run with parallel workers]
- **Mistake:** Ran `uv run python -m optimize.pilot` without setting `PILOT_WORKERS`, defaulting to 1 worker — extremely slow for batches of 40–80 segments.
- **Correction:** Always set `PILOT_WORKERS=10` (or higher) when running the pilot. Example: `PILOT_WORKERS=10 uv run python -m optimize.pilot`
- **Rule:** Never run the pilot with the default single worker. Always pass `PILOT_WORKERS=8` at minimum.

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

### [Env vars: always load .env via python-dotenv in production modules]
- **Mistake:** Scripts that call `os.environ.get("GSHEETS_SPREADSHEET_ID")` or `os.environ.get("DATABASE_URL")` fail when run via `uv run` because `.env` is not automatically loaded — the shell does not export those vars.
- **Correction:** Add `from dotenv import load_dotenv; load_dotenv()` at module level in every shared env-reading helper (`src/ingest/db.py`, `src/review/sheets.py`). All callers inherit the load automatically. Add `python-dotenv>=1.0` to `pyproject.toml`.
- **Rule:** Any new module that reads env vars via `os.environ` must call `load_dotenv()` at import time. Do not rely on the shell pre-exporting vars, and do not inline env vars on the command line.

### [Source layout: code in src/, tests in tests/]
- **Mistake:** Placed `verify.py` at `src/verify.py` (project root of src/) rather than inside the `src/acquire/` package where all production code lives. Tests were added correctly in `tests/` but the production module was outside the package.
- **Correction:** All production Python modules belong inside `src/acquire/` (the package exposed by hatchling). Thin entry-point wrappers (like `verify_sources.py`) may live at the project root only if they do nothing but delegate to a module inside `src/acquire/`.
- **Rule:** When creating a new production module, always place it under `src/acquire/`. Never add `.py` files directly under `src/` that are not part of the `acquire` package.

### [Debug pilot: reset translations before re-running for comparison]
- **Mistake:** Tried to re-run the debug pilot after I.q1 segments were already translated. The pilot only fetches `translation_status = 'pending'` segments, so a previously-translated question produces 0 segments and an empty JSONL — nothing to compare.
- **Correction:** The correct sequence when re-running to compare output: (1) identify the segment_ids processed in the prior JSONL run, (2) reset them to `pending` in the DB (`UPDATE segment SET translation_status='pending' WHERE segment_id IN (...)`), (3) run `uv run python -m optimize.pilot`, (4) compare the new JSONL in `reports/` against the prior one for qualitative analysis.
- **Rule:** The pilot entry point is `uv run python -m optimize.pilot`, not `translate.loop`. Any time you want to re-run the pilot to verify a code or data change, always reset the target segments first. Never compare an empty JSONL against a prior run.
