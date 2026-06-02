# M0 — Environment & Source Acquisition

**Status:** build-locked
**Estimate:** half day
**Prerequisite for:** everything

---

## User story
*As the engineer, I need a confirmed local environment and all source texts
on disk in verified format before writing any pipeline code — so that M1
never discovers a missing input mid-build.*

## Objective
Get the environment working and every source file in hand, verified. No schema,
no parsing logic. This milestone is complete when `verify_sources.py` prints
all green.

---

## Steps

### 1. Project scaffold
```
project/
├── CLAUDE.md
├── .claude/
├── sources/
│   ├── latin/
│   ├── czech/
│   │   ├── bahounek/
│   │   └── krystal/
│   └── english/
│       ├── dominican/
│       └── freddoso/
├── src/
├── tests/
├── style_profile.yaml      ← stub, populated at M1
├── .env.example
├── docker-compose.yml
└── pyproject.toml
```

### 2. Python environment
- Python 3.12 via `uv`
- `pyproject.toml` with dependencies: `psycopg2-binary`, `lxml`, `python-docx`,
  `pyyaml`, `requests`, `beautifulsoup4`, `cltk`, `morphodita` (or `ufal.morphodita`)
- `.env.example` with keys: `DATABASE_URL`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`
- `.gitignore` covering `.env`, `sources/` (large files), `__pycache__`

### 3. Docker Compose
```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: aquinas
      POSTGRES_USER: aquinas
      POSTGRES_PASSWORD: aquinas
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata:
```
Confirm: `docker compose up -d` starts cleanly; `psql` connects; `CREATE EXTENSION vector;` succeeds.

### 4. Source acquisition

**Latin (Corpus Thomisticum XML)**
- Download from corpusthomisticum.org
- Save to `sources/latin/`
- Verify: XML is well-formed; article count ≥ 2,669; five structural elements
  (arg, sed_contra, respondeo, replies) are present in a sample article

**Bahounek Czech**
- Scrape cormierop.cz — confirm coordinate tags present on a sample page first
- Save raw HTML per section to `sources/czech/bahounek/`
- Verify: coordinate tags of the form `I ot. N čl. N arg. N` present;
  sample article has all structural parts; coverage spans all four Partes

**English (Dominican Province)**
- Download from newadvent.org or dhspriory.org
- Save to `sources/english/dominican/`
- Verify: complete coverage (Prima Pars through Supplementum); HTML structure
  consistent enough for a simple parser

**English (Freddoso)**
- Download from Freddoso's site (alfredfreddoso.nd.edu or equivalent)
- Save to `sources/english/freddoso/`
- Verify: note which questions/articles are actually available; record coverage
  gaps so the English ingest knows when to fall back to Dominican Province

**Krystal docx**
- Already at `sources/czech/krystal/Teologicka__Suma_u_zus_-_verze_4.docx`
- Verify: `python-docx` opens it cleanly; paragraph count reasonable

### 5. `verify_sources.py`
A script that checks every source and prints a status report. Run this at the
end of M0 and paste the output as the milestone deliverable.

```python
# Checks to implement:
# - sources/latin/   : XML well-formed, article count, sample element types
# - sources/czech/bahounek/ : files present, coordinate tags found in sample
# - sources/czech/krystal/  : docx opens, paragraph count
# - sources/english/dominican/ : files present, sample article parseable
# - sources/english/freddoso/  : files present, coverage map printed
# - DB connection   : connects, pgvector extension loaded
# - .env            : all required keys present (values not checked)
# - DeepSeek key    : liveness probe — a cheap auth/balance ping that fails on
#                     HTTP 401/402 (dead or UNFUNDED key). M2's gap-term proposal
#                     pass depends on a live, funded DeepSeek account; catching a
#                     402 here prevents the "missing input discovered mid-build"
#                     failure this milestone exists to prevent.
```

---

## Deliverables
- Working Docker Compose with Postgres 16 + pgvector
- All source files on disk in `sources/`
- `verify_sources.py` running and printing all green
- `.env.example` and `pyproject.toml` committed

## Acceptance criteria
- `docker compose up -d && psql $DATABASE_URL -c "SELECT 1"` succeeds
- `python verify_sources.py` prints no failures
- `DEEPSEEK_API_KEY` passes a liveness probe (not merely present): a dead or
  unfunded key (HTTP 401/402) fails at verify time, not mid-run
- Corpus Thomisticum article count confirmed ≥ 2,669
- Bahounek coordinate tags confirmed present in all four Partes
- Freddoso coverage gaps documented (so M1 English ingest knows where to fall back)
- No pipeline code written yet
