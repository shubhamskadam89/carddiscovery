"""python -m cdserver migrate | api | worker | scheduler | dev | jobs"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys


def main() -> None:
    p = argparse.ArgumentParser(prog="cdserver")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    a = sub.add_parser("api")
    a.add_argument("--name", default="", help="your name as on bank mail, comma-separated variants")
    w = sub.add_parser("worker")
    w.add_argument("--once", action="store_true")
    sub.add_parser("scheduler")
    d = sub.add_parser("dev", help="API + 2 workers + scheduler")
    d.add_argument("--name", default="")
    d.add_argument("--workers", type=int, default=2)
    sub.add_parser("jobs", help="recent jobs with units and time")
    mon = sub.add_parser("monitor", help="sample memory and CPU of every cdserver process until Ctrl+C")
    mon.add_argument("--every", type=float, default=1.0)
    sim = sub.add_parser("sim", help="simulated 10k-email inbox: generate, dryrun, import, clean")
    sim.add_argument("action", choices=["generate", "dryrun", "import", "import-forged", "import-flood", "clean"])
    sim.add_argument("--flood", action="store_true", help="dryrun: include the 19-fake-per-real flood")
    sim.add_argument("--forged", action="store_true", help="dryrun: include the forged last-12-months mail")
    sim.add_argument("--to", default="shubhamtester183@gmail.com", help="the test inbox")
    sim.add_argument("--total", type=int, default=10_000)
    sim.add_argument("--seed", type=int, default=7)
    sim.add_argument("--years", type=int, default=3, choices=[1, 3, 5])
    ev = sub.add_parser("evaluate", help="compare a user's cards with the simulated inbox's answer file")
    ev.add_argument("--mailbox", default="shubhamtester183@gmail.com")
    args = p.parse_args()

    from . import config, db
    if args.cmd == "migrate":
        print("applied:", db.migrate() or "nothing new")
    elif args.cmd == "api":
        import uvicorn
        from .api import create_app
        from .worker import registry
        db.migrate()
        app, api = create_app(registry(), [n.strip() for n in args.name.split(",") if n.strip()])
        print(f"API: http://127.0.0.1:{config.API_PORT}/?t={api.token}", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=config.API_PORT, log_level="warning")
    elif args.cmd == "worker":
        from .worker import work
        work(once=args.once)
    elif args.cmd == "scheduler":
        from .worker import schedule
        schedule()
    elif args.cmd == "dev":
        db.migrate()
        py = sys.executable
        import socket
        import time
        with socket.socket() as s:  # fail fast, before starting workers, if the port is taken
            if s.connect_ex(("127.0.0.1", config.API_PORT)) == 0:
                sys.exit(f"Port {config.API_PORT} is already in use: another cdserver is probably running.\n"
                         f"Stop it (pkill -f cdserver) or pick another port with CD_PORT=...")
        procs = [subprocess.Popen([py, "-m", "cdserver", "api", "--name", args.name])]
        procs += [subprocess.Popen([py, "-m", "cdserver", "worker"]) for _ in range(args.workers)]
        procs.append(subprocess.Popen([py, "-m", "cdserver", "scheduler"]))
        try:
            while all(pr.poll() is None for pr in procs):  # one piece dies: stop them all
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        for pr in procs:
            if pr.poll() is None:
                pr.send_signal(signal.SIGINT)
        for pr in procs:
            try:
                pr.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pr.kill()
    elif args.cmd == "monitor":
        from .metrics import monitor
        monitor(config.HOME / "metrics.csv", args.every, config.DATABASE_URL)
    elif args.cmd == "sim":
        _sim(args)
    elif args.cmd == "evaluate":
        from .sim.evaluate import evaluate, load_truth, print_report
        from .worker import registry
        with db.pool().connection() as c:
            row = c.execute("SELECT account_id, years FROM gmail_connections WHERE mailbox = %s", (args.mailbox,)).fetchone()
            if not row:
                sys.exit(f"No connected account for {args.mailbox}. Connect it in the app first.")
            print_report(evaluate(c, row["account_id"], load_truth(config.HOME / "sim" / "truth.json"), row["years"],
                                  registry()))
    elif args.cmd == "jobs":
        with db.pool().connection() as c:
            for r in c.execute("SELECT id, kind, account_id, status, units_used, last_error, progress->'stats' AS s, "
                               "progress->'meter' AS m, round(extract(epoch FROM finished_at - started_at)::numeric, 1) "
                               "AS secs FROM jobs ORDER BY id DESC LIMIT 20"):
                s = r["s"] or {}
                print(f"#{r['id']:<4} {r['kind']:<15} user {r['account_id']:<3} {r['status']:<9} "
                      f"{r['units_used']:>6} units {str(r['secs'] or '-'):>6}s  searches {s.get('searches', '-')} "
                      f"headers {s.get('header_reads', '-')} full {s.get('full_reads', '-')} "
                      f"first card {s.get('first_card_s', '-')}s {r['last_error'] or ''}")


def _sim(args) -> None:
    from . import config, db
    from .sim import generate as gen
    truth_file = config.HOME / "sim" / "truth.json"
    mails, truth = gen.build(args.to, args.total, args.seed)
    from datetime import timedelta
    from .sim.forged import build_forged
    last_year = sum(1 for m in mails if m.when.date() > gen.TODAY - timedelta(days=365))
    forged, forged_truth = build_forged(args.to, last_year)
    # Forged numbers must never become cards; listing them is harmless when they aren't imported.
    truth["must_not_find"] += [f for f in forged_truth if f["last4"] not in {x["last4"] for x in truth["must_not_find"]}]
    truth["forged"] = {"count": len(forged), "last_year_total": last_year + len(forged)}
    gen.write_truth(truth, truth_file)
    from .sim.forged import build_flood
    flood, flood_truth = build_flood(args.to, last_year, 19, already=len(forged))
    seen_l4 = {x["last4"] for x in truth["must_not_find"]}
    truth["must_not_find"] += [f for f in flood_truth if f["last4"] not in seen_l4]
    truth["flood"] = {"fake": len(forged) + len(flood), "real_last_year": last_year}
    gen.write_truth(truth, truth_file)
    if getattr(args, "forged", False) or getattr(args, "flood", False):
        mails = mails + forged
    if getattr(args, "flood", False):
        mails = mails + flood
    if args.action == "generate":
        print(f"{len(mails)} emails for {args.to}, {mails[0].when:%Y-%m-%d} to {mails[-1].when:%Y-%m-%d}; "
              f"answer file: {truth_file}")
        print(json.dumps(truth["counts"], indent=1))
    elif args.action == "dryrun":
        import time
        from .sim.dryrun import run
        from .sim.evaluate import evaluate, print_report
        from .worker import registry
        reg = registry()
        db.migrate()
        with db.pool().connection() as c:
            c.execute("DELETE FROM accounts WHERE external_id = 'sim-dryrun'")
            acc = c.execute("INSERT INTO accounts (external_id) VALUES ('sim-dryrun') RETURNING id").fetchone()["id"]
            t0 = time.monotonic()
            steps = run(c, acc, mails, args.to, reg, args.years, [gen.PERSONA["name"]])
            print(f"dry run took {time.monotonic() - t0:.1f}s (no network)")
            for s in steps:
                print(f"  {s['kind']:<26} units {s['units']:>6}  searches {s.get('searches', '-')}  headers "
                      f"{s.get('header_reads', '-')}  full {s.get('full_reads', '-')}  reopened {s.get('reopened', '-')}  "
                      f"listed {s.get('listed', '-')}  by kind {s['units_by_kind']}")
            print_report(evaluate(c, acc, truth, args.years, reg))
    elif args.action in ("import", "import-forged", "import-flood", "clean"):
        from .sim import importer
        if args.action == "import":
            importer.import_all(mails, args.to)
        elif args.action == "import-flood":
            print(f"{len(flood)} more fake emails: with the {len(forged)} forged ones, 19 fake for each of the "
                  f"{last_year} real emails of the last 12 months")
            importer.import_all(flood, args.to)
        elif args.action == "import-forged":
            print(f"{len(forged)} forged emails over the last 12 months (1 in 20 of {last_year + len(forged)})")
            importer.import_all(forged, args.to)
        else:
            importer.clean(args.to)


if __name__ == "__main__":
    main()
