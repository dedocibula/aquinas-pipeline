# Aquinas → Slovak Translation Pipeline

A reproducible, cost-controlled pipeline that translates Thomas Aquinas's *Summa Theologiae*
from Scholastic Latin into Slovak. Built Summa-specific to ship fast.

The model translates prose. It does not decide terminology. Term choices come from authoritative
human sources in a fixed precedence order — Krystal glossary beats everything; Bahounek fills
gaps; Dominican/Freddoso English anchors disambiguation. The model receives locked Slovak terms
as hard constraints and generates prose around them.

## Sources

| Source | Language | Role |
|---|---|---|
| Corpus Thomisticum | Latin | Primary text (87 HTML files, 2,663 articles) |
| Krystal glossary | Slovak | Authoritative term authority — overrides everything |
| Bahounek | Czech | Gap-filler for terms not in Krystal |
| Dominican Province | English | Disambiguation anchor + Freddoso fallback |
| Freddoso | English | Disambiguation anchor (partial — q79–q90 of Part III absent) |

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/) — package and environment management
- Docker + Docker Compose — for PostgreSQL 16 with `pgvector` and `ltree`
- `jq` — used by the pre-commit lint hook

## Installation

**1. Clone and install dependencies**

```bash
git clone <repo>
cd aquinas-pipeline
uv sync --extra dev
```

**2. Configure environment**

```bash
cp .env.example .env
# Edit .env — fill in DATABASE_URL, DEEPSEEK_API_KEY, ANTHROPIC_API_KEY
```

The `DATABASE_URL` for the local Docker database is:
```
postgresql://aquinas:aquinas@localhost:5432/aquinas
```

**3. Start the database**

```bash
docker compose up -d
```

This starts PostgreSQL 16 with the `pgvector` extension available. The `ltree` extension is enabled during M1 schema creation.

**4. Acquire source texts**

Run each acquisition script once. They download and save sources to `sources/` (gitignored).

```bash
uv run python src/acquire/latin.py       # Corpus Thomisticum — ~87 HTML files
uv run python src/acquire/bahounek.py    # Bahounek Czech — 4 HTML files
uv run python src/acquire/dominican.py   # Dominican Province English — 614 HTML files
uv run python src/acquire/freddoso.py    # Freddoso English — TOC pages + coverage map
```

Place the Krystal docx manually at:
```
sources/czech/krystal/Teologická Suma úzus - verze 4.docx
```

**5. Verify everything**

```bash
uv run python verify_sources.py
```

All 7 checks must pass (Latin, Bahounek, Krystal, Dominican, Freddoso, DB, .env).

## Development

```bash
uv run pytest              # run tests
uv run ruff check src/ tests/ verify_sources.py   # lint
```

**Install the pre-commit hook** (once, after cloning):

```bash
bash scripts/install-hooks.sh
```

This symlinks `scripts/pre-commit` into `.git/hooks/`. On every `git commit`, ruff runs against
staged `.py` files only — the commit is blocked if any lint errors are found.

## Milestone Status

| Milestone | Status | Deliverable |
|---|---|---|
| M0 | complete | env + sources on disk, verified |
| M1 | next | schema + resolver proven on 10 articles |
| M2 | — | full corpus ingested, coverage report |
| M3 | — | glossary review surface + re-run trigger |
| M4 | — | translation loop |
| M5 | — | polish + orchestration + consistency |
