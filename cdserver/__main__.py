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
    sim.add_argument("action", choices=["generate", "dryrun", "import", "clean"])
    sim.add_argument("--to", default="shubhamtester183@gmail.com", help="the test inbox")
    sim.add_argument("--total", type=int, default=10_000)
    sim.add_argument("--seed", type=int, default=7)
    sim.add_argument("--years", type=int, default=3, choices=[1, 3, 5])
    sim.add_argument("--mix", default="synthetic", choices=["synthetic", "real"],
                     help="real: one year, ~20 cards, shaped like real bank mail (sim/realmix.py)")
    au = sub.add_parser("audit", help="development only: how a consenting inbox's bank mail looks to the engine")
    au.add_argument("--account", type=int, required=True)
    au.add_argument("--every", type=int, default=4, help="read every Nth email from known senders")
    au.add_argument("--rate", type=int, default=2000, help="Gmail units per minute (Google allows 6,000)")
    co = sub.add_parser("corpus", help="development only: study set of an owner's own emails (see cdserver/corpus.py)")
    co.add_argument("action", choices=["fetch", "label", "review", "report", "model", "blind", "blind-apply",
                                       "evaluate", "export", "llm", "relabel", "delete"])
    co.add_argument("--account", type=int, required=True)
    co.add_argument("--labels", default="auto", choices=["auto", "haiku"],
                    help="evaluate: train on the rule labels (auto) or on Haiku's relabelling")
    co.add_argument("--provider", default="ollama", choices=["ollama", "claude", "claude-cli", "claude-cli-haiku"],
                    help="llm: local Ollama model, or Claude via the Anthropic API (owner's agreement needed)")
    co.add_argument("--truth", help='JSON: {"cards": {"sbi": {"last4": ["8022"], "products": ["BPCL Octane"]}}}')
    ev = sub.add_parser("evaluate", help="compare a user's cards with the simulated inbox's answer file")
    ev.add_argument("--mailbox", default="shubhamtester183@gmail.com")
    ev.add_argument("--mix", default="synthetic", choices=["synthetic", "real"])
    args = p.parse_args()

    from . import config, db
    if args.cmd == "corpus":
        from . import corpus
        if args.action == "fetch":
            print(corpus.fetch(args.account))
        elif args.action == "delete":
            print(f"deleted {corpus.Store(args.account).delete()} files")
        elif args.action == "label":
            print(corpus.label_all(args.account, args.truth))
        elif args.action == "review":
            print(f"{corpus.apply_review(args.account)} of your answers applied")
        elif args.action == "model":
            from . import corpus_model
            corpus.Store(args.account)  # same access check
            print(corpus_model.study(args.account))
        elif args.action == "relabel":
            from . import corpus_llm
            corpus.Store(args.account)  # same access check
            print(corpus_llm.relabel(args.account, corpus._truth(args.truth)))
        elif args.action == "llm":
            from . import corpus_llm
            corpus.Store(args.account)  # same access check
            print(corpus_llm.run(args.account, corpus._truth(args.truth), args.provider))
        elif args.action in ("blind", "blind-apply", "evaluate", "export"):
            from . import corpus_eval
            corpus.Store(args.account)  # same access check
            if args.action == "blind-apply":
                print(corpus_eval.blind_apply(args.account))
            elif args.action == "export":
                print(corpus_eval.export(args.account, labels=args.labels))
            else:
                if args.action == "blind":
                    print(corpus_eval.blind(args.account, corpus._truth(args.truth)))
                else:
                    print(corpus_eval.evaluate(args.account, corpus._truth(args.truth), args.labels))
        else:
            print(corpus.report(args.account))
        return
    if args.cmd == "audit":
        from .audit import run as audit
        print(audit(args.account, args.every, args.rate))
        return
    if args.cmd == "migrate":
        print("applied:", db.migrate() or "nothing new")
    elif args.cmd == "api":
        import uvicorn
        from .api import create_app
        from .worker import registry
        db.migrate()
        app, api = create_app(registry(), [n.strip() for n in args.name.split(",") if n.strip()])
        print(f"API: {config.PUBLIC_URL}/?t={api.token}", flush=True)
        uvicorn.run(app, host=config.API_BIND, port=config.API_PORT, log_level="warning")
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
            name = "truth_real.json" if args.mix == "real" else "truth.json"
            print_report(evaluate(c, row["account_id"], load_truth(config.HOME / "sim" / name), row["years"],
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
    if args.mix == "real":
        from .sim import realmix
        truth_file = config.HOME / "sim" / "truth_real.json"
        mails, truth = realmix.build(args.to)
    else:
        truth_file = config.HOME / "sim" / "truth.json"
        mails, truth = gen.build(args.to, args.total, args.seed)
    gen.write_truth(truth, truth_file)
    if args.action == "dryrun" and args.mix == "real":
        from .sim.dryrun import run_monthly
        from .sim.evaluate import evaluate, print_report
        from .worker import registry
        reg = registry()
        db.migrate()
        with db.pool().connection() as c:
            c.execute("DELETE FROM accounts WHERE external_id = 'sim-dryrun-real'")
            acc = c.execute("INSERT INTO accounts (external_id) VALUES ('sim-dryrun-real') RETURNING id").fetchone()["id"]
            for s in run_monthly(c, acc, mails, args.to, reg, realmix.TODAY, [gen.PERSONA["name"]]):
                print(f"  {s['kind']:<12} units {s['units']:>6}  searches {s.get('searches', '-')}  headers "
                      f"{s.get('header_reads', '-')}  full {s.get('full_reads', '-')}  by kind {s['units_by_kind']}")
            print_report(evaluate(c, acc, truth, 1, reg))
        return
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
    elif args.action in ("import", "clean"):
        from .sim import importer
        if args.action == "import":
            importer.import_all(mails, args.to)
        else:
            importer.clean(args.to)


if __name__ == "__main__":
    main()
