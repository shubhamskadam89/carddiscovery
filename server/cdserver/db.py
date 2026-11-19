"""Postgres access: one connection pool per process, and numbered SQL migrations."""

from __future__ import annotations

import atexit
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=10,
                               kwargs={"row_factory": dict_row, "autocommit": True}, open=True)
        atexit.register(_pool.close)
    return _pool


def connect() -> psycopg.Connection:
    """A dedicated autocommit connection (workers keep one for their lifetime)."""
    return psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row)


def migrate() -> list[str]:
    """Apply migrations/NNN_*.sql that haven't run yet. Returns the names applied."""
    applied: list[str] = []
    with pool().connection() as c:
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                  "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r["name"] for r in c.execute("SELECT name FROM schema_migrations")}
        for f in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
            if f.name in done:
                continue
            with c.transaction():
                c.execute(f.read_text())
                c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
            applied.append(f.name)
    return applied
