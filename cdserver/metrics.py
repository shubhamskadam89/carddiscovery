"""Resource use: per job (inside the worker) and for the whole stack (monitor command)."""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path

import psutil

MB = 1024 * 1024


class JobProbe:
    """Wall time, CPU time and memory (RSS) of this process while one job runs.
    Memory is sampled every 100 ms in a background thread to catch the peak."""

    def __init__(self, every_s: float = 0.1):
        self.p = psutil.Process()
        self.every = every_s
        self._stop = threading.Event()

    def __enter__(self) -> "JobProbe":
        self.t0 = time.monotonic()
        self.cpu0 = sum(self.p.cpu_times()[:2])
        self.rss0 = self.peak = self.p.memory_info().rss
        self.thread = threading.Thread(target=self._sample, daemon=True)
        self.thread.start()
        return self

    def _sample(self) -> None:
        while not self._stop.wait(self.every):
            self.peak = max(self.peak, self.p.memory_info().rss)

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self.thread.join()
        self.rss1 = self.p.memory_info().rss
        self.peak = max(self.peak, self.rss1)
        self.wall = time.monotonic() - self.t0
        self.cpu = sum(self.p.cpu_times()[:2]) - self.cpu0

    def as_dict(self) -> dict:
        return {"wall_s": round(self.wall, 2), "cpu_s": round(self.cpu, 2),
                "cpu_pct": round(100 * self.cpu / self.wall, 1) if self.wall else 0.0,
                "rss_start_mb": round(self.rss0 / MB, 1), "rss_peak_mb": round(self.peak / MB, 1),
                "rss_end_mb": round(self.rss1 / MB, 1), "rss_growth_mb": round((self.peak - self.rss0) / MB, 1)}


def _role(p: psutil.Process) -> str | None:
    try:
        cmd = p.cmdline()
    except (psutil.Error, OSError):
        return None
    if "cdserver" not in cmd:
        return None
    i = cmd.index("cdserver")
    role = cmd[i + 1] if len(cmd) > i + 1 else "?"
    return None if role in ("monitor", "dev") else f"{role}:{p.pid}"


def monitor(out: Path, every_s: float = 1.0, db_url: str | None = None) -> None:
    """Sample every cdserver process (and the database size) until Ctrl+C, then summarise."""
    import psycopg
    out.parent.mkdir(parents=True, exist_ok=True)
    procs: dict[int, psutil.Process] = {}
    peak: dict[str, float] = {}
    cpu_total: dict[str, float] = {}
    samples = 0
    db0 = None
    conn = psycopg.connect(db_url, autocommit=True) if db_url else None
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "process", "rss_mb", "cpu_pct", "db_mb"])
        print(f"Monitoring cdserver processes every {every_s}s → {out}  (Ctrl+C to stop)", flush=True)
        t0 = time.monotonic()
        try:
            while True:
                for p in psutil.process_iter():
                    if p.pid not in procs and _role(p):
                        p.cpu_percent(None)  # prime
                        procs[p.pid] = p
                time.sleep(every_s)
                db_mb = None
                if conn:
                    db_mb = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0] / MB
                    db0 = db_mb if db0 is None else db0
                line = []
                for pid, p in list(procs.items()):
                    try:
                        role = _role(p)
                        rss = p.memory_info().rss / MB
                        cpu = p.cpu_percent(None)
                    except (psutil.Error, OSError):
                        procs.pop(pid)
                        continue
                    if not role:
                        continue
                    peak[role] = max(peak.get(role, 0), rss)
                    cpu_total[role] = cpu_total.get(role, 0) + cpu * every_s / 100
                    w.writerow([round(time.monotonic() - t0, 1), role, round(rss, 1), cpu, db_mb and round(db_mb, 2)])
                    line.append(f"{role.split(':')[0]} {rss:.0f}MB {cpu:.0f}%")
                samples += 1
                f.flush()
                if samples % 5 == 0:
                    print(f"[{time.monotonic() - t0:6.0f}s] " + " | ".join(line)
                          + (f" | db {db_mb:.1f}MB" if db_mb else ""), flush=True)
        except KeyboardInterrupt:
            pass
    print("\nPeak memory and total CPU time per process:")
    for role in sorted(peak):
        print(f"  {role:<20} peak {peak[role]:7.1f} MB   cpu {cpu_total[role]:7.1f} s")
    print(f"  total peak (sum)     {sum(peak.values()):7.1f} MB")
    if db0 is not None and conn:
        db1 = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0] / MB
        print(f"  database             {db0:.1f} MB → {db1:.1f} MB")
