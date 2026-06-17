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

This starts PostgreSQL 16 with the `pgvector` extension available.

**4. Create the schema**

```bash
docker exec -i aquinas-pipeline-db-1 psql -U aquinas -d aquinas < db/schema.sql
```

`db/schema.sql` is the single source of truth for the database shape (extensions,
tables, views, and the source/work seed data). The incremental migrations that
originally built it live in `migrations/archive/` for provenance only — do not
replay them on a fresh database.

```bash
bash scripts/install-hooks.sh
```

This symlinks `scripts/pre-commit` into `.git/hooks/`. On every `git commit`, ruff runs against
staged `.py` files only — the commit is blocked if any lint errors are found.
