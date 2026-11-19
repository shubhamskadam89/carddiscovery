"""Postgres job queue: enqueue, claim with FOR UPDATE SKIP LOCKED, finish, retry, resume."""

from __future__ import annotations

import json

import psycopg

from . import config

_CLAIM = """
UPDATE jobs SET status = 'running', locked_by = %(worker)s, locked_at = now(),
                started_at = coalesce(started_at, now()), attempts = attempts + 1
WHERE id = (
    SELECT j.id FROM jobs j
    WHERE j.status = 'queued' AND j.run_after <= now()
      AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.account_id = j.account_id AND r.status = 'running')
      AND (NOT %(defer)s OR j.kind NOT IN ('discover_full', 'pdf_enrich'))
    ORDER BY j.priority, j.run_after
    FOR UPDATE SKIP LOCKED
    LIMIT 1)
RETURNING *
"""


def enqueue(c: psycopg.Connection, kind: str, account_id: int, payload: dict | None = None,
            delay_s: float = 0) -> int | None:
    """Queue a job. A second queued job of the same kind for the same user is a no-op."""
    row = c.execute(
        "INSERT INTO jobs (kind, account_id, priority, payload, run_after) "
        "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s)) "
        "ON CONFLICT (account_id, kind) WHERE status = 'queued' DO NOTHING RETURNING id",
        (kind, account_id, config.PRIORITY[kind], json.dumps(payload or {}), delay_s)).fetchone()
    return row["id"] if row else None


def claim(c: psycopg.Connection, worker: str) -> dict | None:
    used = c.execute("SELECT units FROM quota_daily WHERE day = current_date").fetchone()
    defer = bool(used) and used["units"] > config.PROJECT_UNITS_PER_DAY * config.DEFER_BACKGROUND_ABOVE
    try:
        with c.transaction():
            return c.execute(_CLAIM, {"worker": worker, "defer": defer}).fetchone()
    except psycopg.errors.UniqueViolation:
        return None  # another worker took this user's job in the same instant; try again


def finish(c: psycopg.Connection, job_id: int, status: str = "done", error: str | None = None,
           progress: dict | None = None) -> None:
    c.execute("UPDATE jobs SET status = %s, last_error = %s, finished_at = now(), locked_by = NULL, "
              "progress = progress || %s WHERE id = %s",
              (status, error, json.dumps(progress or {}), job_id))


def resume_later(c: psycopg.Connection, job_id: int, progress: dict, delay_s: float) -> None:
    """Budget ran out: back to the queue, keeping progress; attempts aren't counted against it."""
    c.execute("UPDATE jobs SET status = 'queued', locked_by = NULL, attempts = attempts - 1, "
              "run_after = now() + make_interval(secs => %s), progress = progress || %s WHERE id = %s",
              (delay_s, json.dumps(progress), job_id))


def retry_or_fail(c: psycopg.Connection, job: dict, error: str) -> str:
    if job["attempts"] >= config.MAX_ATTEMPTS:
        finish(c, job["id"], "failed", error)
        return "failed"
    c.execute("UPDATE jobs SET status = 'queued', locked_by = NULL, last_error = %s, "
              "run_after = now() + make_interval(secs => %s) WHERE id = %s",
              (error, 2 ** job["attempts"] * 5, job["id"]))
    return "retry"


def heartbeat(c: psycopg.Connection, job_id: int, units: int, progress: dict) -> None:
    c.execute("UPDATE jobs SET locked_at = now(), units_used = units_used + %s, progress = progress || %s "
              "WHERE id = %s", (units, json.dumps(progress), job_id))
    if units:
        c.execute("INSERT INTO quota_daily (day, units) VALUES (current_date, %s) "
                  "ON CONFLICT (day) DO UPDATE SET units = quota_daily.units + EXCLUDED.units", (units,))


def reap_stale(c: psycopg.Connection) -> int:
    """Jobs whose worker died (no heartbeat) go back to the queue."""
    cur = c.execute("UPDATE jobs SET status = 'queued', locked_by = NULL "
                    "WHERE status = 'running' AND locked_at < now() - make_interval(mins => %s)",
                    (config.STALE_LOCK_MIN,))
    return cur.rowcount


def cancel_queued(c: psycopg.Connection, account_id: int, except_kind: str | None = None) -> None:
    c.execute("UPDATE jobs SET status = 'cancelled', finished_at = now() "
              "WHERE account_id = %s AND status = 'queued' AND kind <> coalesce(%s, '')",
              (account_id, except_kind))
