# Aquinas → Slovak Translation Pipeline

A reproducible, cost-controlled pipeline that translates Thomas Aquinas's *Summa Theologiae*
from Scholastic Latin into Slovak.

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

## Running the review server

```bash
uv run flask --app server.app run --debug
```

The server reads `DATABASE_URL`, `FLASK_SECRET_KEY`, `GOOGLE_CLIENT_ID`, and
`GOOGLE_CLIENT_SECRET` from the environment (`.env` is loaded automatically).
Editors authenticate via Google OAuth; the callback URL must be registered in
Google Cloud Console → OAuth 2.0 → Authorized redirect URIs:
```
http://localhost:5000/auth/callback
```

## Testing

```bash
uv run pytest
```

All tests use fakes/mocks — no live DB or API keys required.

## Deployment (Railway)

The server is deployed to Railway via GitHub Actions.

- **CI** (`.github/workflows/ci.yml`): runs `pytest` on every push and pull request.
- **Deploy** (`.github/workflows/deploy.yml`): triggers automatically when CI passes on `main`,
  deploying to the `aquinas-pipeline` Railway service via `railway up --ci --service aquinas-pipeline`.

The `RAILWAY_TOKEN` secret must be set in the GitHub repository settings.

Railway environment variables required on the app service:

| Variable | Value |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `FLASK_SECRET_KEY` | (strong random secret) |
| `GOOGLE_CLIENT_ID` | from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | from Google Cloud Console |

The Railway callback URL (`https://<railway-domain>.up.railway.app/auth/callback`) must also
be added to Google Cloud Console → OAuth 2.0 → Authorized redirect URIs.

### Accessing the Railway database locally

The Postgres service has no public TCP proxy. Access is via the Railway CLI tunnel only:

```bash
railway connect Postgres
# → tunnels Railway Postgres to localhost:5432 for the duration of the session
```

Then point local tools at `postgresql://postgres:<password>@localhost:5432/railway`.
The Flask app connects over Railway's private internal network (`${{Postgres.DATABASE_URL}}`
resolves to the internal address) — the public internet is never involved.

### Automated backups

A dedicated Railway cron service (build: `Dockerfile.backup`, cron schedule `0 3 * * *`)
runs `scripts/backup.py` daily on the private network — it connects via
`${{Postgres.DATABASE_URL}}` (no public exposure), takes a full `pg_dump -Fc` (schema +
data), and uploads it to an S3 bucket under a dedicated IAM user scoped to that bucket
only. The last 14 dumps are kept; older ones are pruned after each successful upload
only — a run of failures never deletes existing backups, it just means no new one was
added that day.

Required service variables on the backup service:

| Variable | Value |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Dedicated IAM user's access key, scoped to only the backup bucket |
| `AWS_REGION` | The bucket's region |
| `S3_BACKUP_BUCKET` | Name of the backup bucket |

**Restoring from a backup:**

```bash
# 1. Download the relevant dump
aws s3 cp s3://<bucket>/backups/aquinas_backup_<timestamp>.dump .

# 2. Open a tunnel to Railway Postgres
railway connect Postgres

# 3. Restore — note --clean, since this is a full dump (unlike the data-only
#    delta procedure below)
pg_restore --clean --if-exists -d postgresql://postgres:<password>@localhost:5432/railway \
  aquinas_backup_<timestamp>.dump
```

Do a restore drill periodically (e.g. quarterly) into a scratch database to confirm
backups are actually restorable, not just produced.

### Pushing a DB delta to Railway

After a local data migration (e.g. re-translating a segment batch):

```bash
# 1. Open tunnel
railway connect Postgres

# 2. In another terminal — dump data-only from local, restore to Railway
docker exec aquinas-pipeline-db-1 pg_dump -U aquinas aquinas \
  -Fc --data-only --no-privileges --no-owner \
  --exclude-table=source --exclude-table=work \
  -f /tmp/delta.dump

docker exec aquinas-pipeline-db-1 pg_restore \
  --disable-triggers --superuser=postgres \
  -d "postgresql://postgres:<password>@host.docker.internal:5432/railway" \
  /tmp/delta.dump
```
