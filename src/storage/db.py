"""
Shared database connection helper.

Usage:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(...)

Connection parameters are read from the DATABASE_URL environment variable,
which must be set before any script runs (loaded automatically via python-dotenv).
"""

import os
from contextlib import contextmanager
from typing import Generator

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Copy .env.example to .env and fill in the value."
        )
    return url


@contextmanager
def get_conn() -> Generator[psycopg2.extensions.connection, None, None]:
    """Yield a psycopg2 connection; commit on clean exit, rollback on exception."""
    conn = psycopg2.connect(_database_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def source_id(conn: psycopg2.extensions.connection, code: str) -> int:
    """Return source_id for a given source code. Raises if not found."""
    with conn.cursor() as cur:
        cur.execute("SELECT source_id FROM source WHERE code = %s", (code,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"Source '{code}' not found in DB. Was the migration run?")
    return row[0]


def work_id(conn: psycopg2.extensions.connection, structure_type: str) -> int:
    """Return work_id for a given structure_type. Raises if not found."""
    with conn.cursor() as cur:
        cur.execute("SELECT work_id FROM work WHERE structure_type = %s", (structure_type,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            f"Work with structure_type='{structure_type}' not found in DB. "
            "Was the migration run?"
        )
    return row[0]


def verify_connection() -> None:
    """Raise if the DB is not reachable or ltree extension is missing."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.execute("SELECT extname FROM pg_extension WHERE extname = 'ltree'")
            if cur.fetchone() is None:
                raise RuntimeError("ltree extension not found in DB.")
