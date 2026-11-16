"""Server tests run against the Mac's default Postgres (localhost:5432), in a
separate database, carddiscovery_test, that is recreated for each test session."""

from __future__ import annotations

import getpass
import os

import pytest

BASE = os.environ.get("CD_TEST_PG", f"postgresql://{getpass.getuser()}@localhost:5432")
os.environ["CD_DATABASE_URL"] = f"{BASE}/carddiscovery_test"


def _recreate() -> bool:
    import psycopg
    try:
        with psycopg.connect(f"{BASE}/postgres", autocommit=True, connect_timeout=2) as c:
            c.execute("DROP DATABASE IF EXISTS carddiscovery_test WITH (FORCE)")
            c.execute("CREATE DATABASE carddiscovery_test")
        return True
    except psycopg.OperationalError:
        return False


@pytest.fixture(scope="session")
def database():
    if not _recreate():
        pytest.skip("Postgres isn't running on localhost:5432 (brew services start postgresql@18)")
    from cdserver import db
    db.migrate()
    return db


@pytest.fixture
def c(database):
    conn = database.connect()
    for t in ("jobs", "evidence", "cards", "statement_facts", "notifications", "decisions", "gmail_connections",
              "sessions", "accounts", "quota_daily", "audit_log"):
        conn.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield conn
    conn.close()


@pytest.fixture
def account(c):
    def make(user: str = "user-1") -> int:
        return c.execute("INSERT INTO accounts (external_id) VALUES (%s) RETURNING id", (user,)).fetchone()["id"]
    return make
