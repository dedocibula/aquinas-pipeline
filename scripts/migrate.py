#!/usr/bin/env python3
"""Apply pending SQL migrations from migrations/*.sql in numeric order.

Tracks applied migrations in schema_migrations (created on first run).
Only scans the migrations/ root — archive/ is intentionally excluded.
Each migration runs inside a transaction; failure rolls back and aborts.
"""

import os
import re
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv()

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _migration_files() -> list[Path]:
    files = [f for f in MIGRATIONS_DIR.glob("*.sql") if f.is_file()]
    files.sort(key=lambda f: int(re.match(r"(\d+)", f.name).group(1)))
    return files


def _ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   text        PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
        """)
    conn.commit()


def _applied(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def _apply(conn, path: Path) -> None:
    sql = path.read_text()
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
    conn.commit()


def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(db_url)
    try:
        _ensure_table(conn)
        applied = _applied(conn)
        pending = [f for f in _migration_files() if f.name not in applied]

        if not pending:
            print("migrate: nothing to apply")
            return

        for path in pending:
            print(f"migrate: applying {path.name} ...", flush=True)
            try:
                _apply(conn, path)
            except Exception as exc:
                conn.rollback()
                print(f"migrate: FAILED on {path.name}: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"migrate: {path.name} ok")

        print(f"migrate: {len(pending)} migration(s) applied")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
