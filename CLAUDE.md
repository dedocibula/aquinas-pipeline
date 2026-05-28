Before doing anything else, read `docs/claude-corrections.md`.

# Aquinas → Slovak Translation Pipeline

## What this is
A reproducible, cost-controlled pipeline that translates Thomas Aquinas's *Summa Theologiae*
from Scholastic Latin into Slovak. Built Summa-specific to ship fast; generality preserved only
where it costs nothing (opaque locators; data-driven source precedence).

## Three principles — never deviate from these

**1. Quality through constraints, not model freedom.**
The model translates *prose*. It does not decide terminology. Term choices come from authoritative
human sources in a fixed precedence order. The model receives locked Slovak terms as hard
constraints and generates the prose around them.

**2. Krystal is the authority.**
Krystal glossary beats everything. Where Krystal has a term entry, that entry is the law —
regardless of what Bahounek, English, or the model would suggest. Bahounek fills gaps only.
English disambiguates and anchors only.

**3. Re-runs are segment-scoped, never corpus-wide.**
When a reviewer changes a term, only the segments that used that specific term version are
re-translated. `term_usage.sense_version_used` compared against `glossary_sense.version` is
the mechanism. This keeps correction cost in the range of cents, not dollars. Never
re-translate more than the stale set.

## Context Routing: How to read this project

**Before any task**, read:
1. `.claude/database.md` — the full annotated schema; every column has a stated consumer.
2. `.claude/decisions.md` — why things are shaped the way they are; read this before "improving" anything.

**Then read ONLY the milestone file for the task you are building:**
- `.claude/m0_setup.md`
- `.claude/m1_resolution.md`
- `.claude/m2_scale.md`
- `.claude/m3_review.md` *(design-intent only, not build-locked)*
- `.claude/m4_translation.md` *(design-intent only, not build-locked)*
- `.claude/m5_hardening.md` *(design-intent only, not build-locked)*

**When a task involves reading or writing source data**, also read:
- `.claude/sources.md`

Do not load milestone files you are not currently building. Do not implement from
design-intent files (M3–M5) — they contain open decisions and will change.

## Tech Stack

| Milestone | Dependencies |
|---|---|
| M0–M2 | `lxml`, `python-docx`, `PyYAML`, `CLTK`, `MorphoDiTa`, `psycopg2-binary`, `requests`, `beautifulsoup4` |
| M3+ | `gspread` (Sheets sync) — not before |
| M4+ | `anthropic` (Batch API), DeepSeek client — not before |
| M4+ (maybe) | LangGraph — decision deferred, do not install earlier |

- Python 3.12 + `uv`
- PostgreSQL 16 — **must** have `ltree` and `pgvector` extensions loaded
- Docker Compose

**Strict Constraint:** No LangGraph or vectors in M0–M2.

## Conventions & Rules

- **Commit often** using Conventional Commits (`feat:`, `fix:`, `refactor:`).
- **Show diffs:** Do NOT commit without showing the diff for approval first.
- **Stop for DDL:** Pause and request human review before executing any database migrations
  or schema creation scripts.
- **Fail Loudly:** Do not write silent `try/except` blocks in parsers. If source HTML/XML
  deviates from expected structure, the parser must crash and log the exact locator and anomaly.
- **Boring Code Wins:** Prefer plain, debuggable Python. Do not introduce new dependencies
  unless explicitly authorised. Respect existing file structure.
- **Plan first:** If a task is complex, outline the plan before implementing.
- **Session state:** At the end of every session, update `docs/session_state.md` with:
  current milestone, key decisions made this session, files modified, exact next step.
  Read `docs/session_state.md` at the start of every session after corrections.

## Before building anything, answer:
- What happens if this crashes halfway through?
- What external identifiers (models, APIs, versions) might I hardcode?
- What are the system dependencies and how do I verify them at startup?

## Milestone Status

| Milestone | Status | Deliverable |
|---|---|---|
| M0 | build-locked | env + sources on disk, verified |
| M1 | build-locked | schema locked + resolver proven on 10 articles |
| M2 | build-locked | full corpus ingested, coverage report |
| M3 | design-intent | glossary review surface + re-run trigger |
| M4 | design-intent | translation loop |
| M5 | design-intent | polish + orchestration + consistency |

## Verification
*(To be added: test/build commands as project evolves, e.g., `uv run pytest`,
schema verification scripts.)*

## Correction Log
When corrected, append to `docs/claude-corrections.md`:
```
### [Short description of mistake]
- **Mistake:** What you did wrong
- **Correction:** The right approach
- **Rule:** General rule going forward
```
