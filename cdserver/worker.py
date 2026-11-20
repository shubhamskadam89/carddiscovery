"""Worker and scheduler loops."""

from __future__ import annotations

import os
import socket
import time

from carddiscovery.registry import Registry

from . import config, db, queue
from .gmailx import BudgetExhausted, Cancelled
from .jobs import Job, NeedsReconnect, run
from .metrics import JobProbe
from .vault import Vault


def registry() -> Registry:
    return Registry.load(test_senders=config.TEST_SENDERS or None)


def work(name: str | None = None, once: bool = False, idle_s: float = 0.5) -> None:
    name = name or f"{socket.gethostname()}:{os.getpid()}"
    c = db.connect()
    reg, vault = registry(), Vault("worker")
    print(f"[worker {name}] ready", flush=True)
    while True:
        row = queue.claim(c, name)
        if not row:
            if once:
                return
            time.sleep(idle_s)
            continue
        try:
            j = Job(c, row, reg, vault, name)
        except Exception as e:  # a job that can't even start is retried, never left "running"
            outcome = queue.retry_or_fail(c, row, f"{type(e).__name__} at start: {str(e)[:300]}")
            print(f"[worker {name}] job {row['id']} {row['kind']} user {row['account_id']}: {outcome} "
                  f"(failed to start: {type(e).__name__})", flush=True)
            continue
        t0 = time.monotonic()
        outcome = "done"
        probe = JobProbe()
        try:
            with probe:
                run(j)
            queue.heartbeat(c, j.id, j.meter.unflushed, j.progress())
            queue.finish(c, j.id, "done", progress={"seconds": round(time.monotonic() - t0, 2),
                                                    "runs": runs(row, j, probe)})
        except BudgetExhausted as e:
            outcome = "paused"
            j.messages.append("Still checking older emails (paused to stay within Gmail limits)")
            queue.heartbeat(c, j.id, j.meter.unflushed, j.progress())
            queue.resume_later(c, j.id, {"paused": str(e), "runs": runs(row, j, probe)}, config.RESUME_DELAY_S)
        except Cancelled:
            outcome = "cancelled"
            queue.finish(c, j.id, "cancelled")
        except NeedsReconnect:
            outcome = "reconnect"
            queue.finish(c, j.id, "failed", "Gmail access expired or was revoked: reconnect needed")
        except Exception as e:  # message only, never email content
            outcome = queue.retry_or_fail(c, row, f"{type(e).__name__}: {str(e)[:300]}")
        res = probe.as_dict() if hasattr(probe, "wall") else {}
        print(f"[worker {name}] job {j.id} {j.kind} user {j.account_id}: {outcome} "
              f"({j.meter.units} units, {time.monotonic() - t0:.1f}s, cpu {res.get('cpu_s', '-')}s, "
              f"memory {res.get('rss_start_mb', '-')}→{res.get('rss_peak_mb', '-')} MB peak, "
              f"waited on Google's limit {j.meter.backoff_s:.0f}s)", flush=True)


def runs(row: dict, j: Job, probe: JobProbe) -> list[dict]:
    """One entry per time this job ran (a paused job runs again later): time, CPU, memory, units."""
    return list((row.get("progress") or {}).get("runs", [])) + [{**probe.as_dict(), "units": j.meter.units}]


def schedule(every_s: float = 30, once: bool = False) -> None:
    """Cron stand-in: daily syncs for connections not synced in 24 h, and stuck-job recovery."""
    c = db.connect()
    print("[scheduler] ready", flush=True)
    while True:
        reaped = queue.reap_stale(c)
        due = c.execute(
            "SELECT g.account_id FROM gmail_connections g WHERE g.status = 'active' AND g.last_synced_at IS NOT NULL "
            "AND g.last_synced_at < now() - make_interval(hours => %s) AND NOT EXISTS (SELECT 1 FROM jobs j "
            "WHERE j.account_id = g.account_id AND j.status IN ('queued', 'running'))",
            (config.SYNC_EVERY_HOURS,)).fetchall()
        for r in due:
            queue.enqueue(c, "daily_sync", r["account_id"])
        if reaped or due:
            print(f"[scheduler] requeued {reaped} stuck jobs, queued {len(due)} daily syncs", flush=True)
        if once:
            return
        time.sleep(every_s)
