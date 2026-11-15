"""Command-line entry point.

    carddiscovery auth                     # one-time Google sign-in (read-only)
    carddiscovery discover --name "..."    # find cards and print them
    carddiscovery senders                  # card-related senders not in the registry yet
    carddiscovery forget                   # delete local token and ledger
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .ledger import Ledger
from .models import Card
from .registry import Registry, TestModeLocked, sender_domain
from .rules import BundleError
from .score import group

DEFAULT_CREDENTIALS = Path("credentials.json")


def _state_dir() -> Path:
    from .providers.gmail import STATE_DIR
    return STATE_DIR


def _provider(args):
    from .providers.gmail import GmailProvider, authorize
    return GmailProvider(authorize(Path(args.credentials)))


def mask_text(text: str) -> str:
    """Hide numbers before printing email text to a terminal (which may be logged)."""
    import re
    return re.sub(r"\d", "#", text)


def _registry(args) -> Registry:
    """Bank list and rules: from a signed bundle if --bundle is given (verified,
    refused if tampered), otherwise from the source files in data/."""
    from . import rules
    catalog = Path(args.catalog) if getattr(args, "catalog", None) else None
    test_senders = getattr(args, "test_sender", None) or []
    if getattr(args, "bundle", None):
        bundle = rules.load_bundle(Path(args.bundle))
        rules.activate(bundle)
        print(f"Rules bundle {bundle.get('version')} (signature verified)", file=sys.stderr)
        return Registry.from_banks(bundle["banks"], catalog_csv=catalog, test_senders=test_senders)
    return Registry.load(catalog_csv=catalog, test_senders=test_senders)


def cmd_bundle(args) -> int:
    from . import rules
    if args.action == "keygen":
        pub = rules.keygen()
        print(f"Signing key: {rules.DEFAULT_KEY} (keep it private)\nPublic key:  {pub} (ships with the app)")
    elif args.action == "build":
        b, sig = rules.build(Path(args.out))
        print(f"Wrote {b} and {sig}")
    elif args.action == "verify":
        bundle = rules.load_bundle(Path(args.path))
        print(f"OK: bundle {bundle.get('version')}, {len(bundle['banks'])} banks, "
              f"{len(bundle['classify']['rules'])} rules")
    return 0


def cmd_auth(args) -> int:
    p = _provider(args)
    print(f"Signed in as {p.mailbox()} (read-only). Token saved in {_state_dir()}.")
    return 0


def _fmt_date(d) -> str:
    return d.strftime("%Y-%m") if d else "-"


def _card_row(c: Card, registry: Registry) -> str:
    bank = registry.banks[c.bank_id].name if c.bank_id in registry.banks else c.bank_id
    product = c.product or "variant unknown"
    num = f"••{c.last4}" if c.last4 else "no number"
    types = Counter(e.type.value for e in c.evidence)
    ev = ", ".join(f"{t}×{n}" for t, n in types.most_common(4))
    flags = ", ".join(sorted(f for f in c.flags if not f.startswith("raw:")))
    extra = f"  [{flags}]" if flags else ""
    replaced = f" → ••{c.replaced_by.last4}" if c.replaced_by and c.replaced_by.last4 else ""
    return (f"  {bank:<22} {product:<28} {num:<10} own {c.ownership:.2f}  "
            f"{c.status.value}{replaced} ({c.status_confidence:.1f})  "
            f"{_fmt_date(c.first_seen)}…{_fmt_date(c.last_seen)}  {ev}{extra}")


def cmd_discover(args) -> int:
    from .pipeline import discover

    registry = _registry(args)
    provider = _provider(args)
    ledger = Ledger(_state_dir() / "ledger.db")
    if args.test_sender:
        print("TEST MODE: treating mail from " + ", ".join(args.test_sender)
              + " as bank mail (bank read from the email text).", file=sys.stderr)
    if args.fresh:
        ledger.forget(provider.mailbox())

    def progress(msg: str):
        print(f"… {msg}", file=sys.stderr, flush=True)

    res = discover(
        provider, registry, ledger,
        user_names=args.name or [],
        history_years=args.years,
        max_bodies=args.max_bodies,
        alerts_per_window=args.alerts_per_window,
        progress=progress,
        collect_unknown=args.show_unknown,
    )

    from .explain import explain

    groups: dict[str, list[Card]] = {"owned": [], "review": [], "history": [], "rejected": []}
    for c in res.cards:
        groups[group(c)].append(c)
    titles = {"owned": "YOUR CARDS", "review": "PLEASE CONFIRM", "history": "OLD CARDS (closed / replaced)",
              "rejected": "NOT YOURS (your answer)"}
    print(f"\nMailbox: {res.mailbox}")
    for g in ("owned", "review", "history", "rejected"):
        if g == "rejected" and not groups[g]:
            continue
        print(f"\n{titles[g]} ({len(groups[g])})")
        for c in sorted(groups[g], key=lambda c: (c.bank_id, c.last4 or "")):
            if args.details:
                print(_card_row(c, registry))
                continue
            e = explain(c, registry)
            print(f"  • {e.title}  [{e.status}]")
            print(f"      {e.headline}")
            print(f"      Seen in: {'; '.join(e.evidence)}")
            for concern in e.concerns:
                print(f"      ⚠ {concern}")
            if e.question:
                print(f"      {e.question}  →  carddiscovery decide {c.decision_key} yes|no")
    if res.applications:
        print(f"\nAPPLICATIONS (not counted as cards) ({len(res.applications)})")
        for a in res.applications:
            bank = registry.banks[a.bank_id].name if a.bank_id in registry.banks else a.bank_id
            print(f"  {bank:<22} {a.product or 'product unknown':<28} {a.status:<12} "
                  f"{_fmt_date(a.first_seen)}…{_fmt_date(a.last_seen)}")

    s = res.stats
    dkim_rate = (s.dkim_pass / s.bank_messages * 100) if s.bank_messages else 0
    print("\nRUN STATS")
    first = f"{s.first_card_seconds}s" if s.first_card_seconds is not None else "none found"
    print(f"  time {s.seconds}s (first card: {first}) · searches run {s.queries} "
          f"({s.alert_windows} alert windows) · "
          f"ids listed {s.listed} · metadata fetched {s.metadata_fetched} · bodies fetched {s.bodies_fetched} · "
          f"reused from cache {s.from_cache} · API calls {getattr(provider, 'calls', '?')}")
    print(f"  bank messages {s.bank_messages} · DKIM/DMARC pass {dkim_rate:.0f}%")
    print("  by family: " + ", ".join(f"{k} {v}" for k, v in s.per_family.most_common()))
    print("  by type:   " + ", ".join(f"{k} {v}" for k, v in s.per_type.most_common()))

    if args.show_unknown and res.unknown_subjects:
        print(f"\nUNCLASSIFIED BANK EMAILS ({len(res.unknown_subjects)}) — subjects shown here only, not saved")
        for bank, subj in Counter(res.unknown_subjects).most_common(40):
            print(f"  {bank:<10} {mask_text(subj)[:100]}")

    if args.json:
        out = {
            "mailbox": res.mailbox,
            "cards": [
                {
                    "group": group(c), "bank": c.bank_id, "product": c.product, "last4": c.last4,
                    "ownership": c.ownership, "status": c.status.value,
                    "status_confidence": c.status_confidence,
                    "first_seen": c.first_seen.isoformat() if c.first_seen else None,
                    "last_seen": c.last_seen.isoformat() if c.last_seen else None,
                    "replaced_by": c.replaced_by.last4 if c.replaced_by else None,
                    "flags": sorted(c.flags),
                    "decision": c.decision,
                    "explanation": vars(explain(c, registry)),
                    "evidence": [e.to_dict() for e in c.evidence],
                }
                for c in res.cards
            ],
            "applications": [
                {"bank": a.bank_id, "product": a.product, "status": a.status,
                 "first_seen": a.first_seen.isoformat(), "last_seen": a.last_seen.isoformat()}
                for a in res.applications
            ],
        }
        Path(args.json).write_text(json.dumps(out, indent=2))
        print(f"\nWrote {args.json}")
    return 0


def cmd_senders(args) -> int:
    """Show card-related senders that are not in the registry, to grow it."""
    registry = Registry.load()
    provider = _provider(args)
    gmail_q = ('"credit card" subject:(statement OR card OR welcome OR approved OR dispatched OR transaction) '
               + " ".join(f"-from:{d}" for d in registry.all_domains()))
    ids = []
    token = None
    while len(ids) < args.limit:
        resp = provider._exec(provider.svc.users().messages().list(
            userId="me", q=gmail_q, maxResults=min(500, args.limit - len(ids)), pageToken=token))
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    metas = provider.get_metadata(ids)
    counts = Counter(sender_domain(m.sender) for m in metas)
    print(f"Card-related mail from senders not in the registry (sample of {len(metas)}):")
    for d, n in counts.most_common(40):
        print(f"  {n:>5}  {d}")
    return 0


def cmd_serve(args) -> int:
    from .web import App, serve
    app = App(_registry(args), Path(args.credentials), _state_dir(), args.name or [], args.test_sender or [])
    serve(app, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_decide(args) -> int:
    """Record the user's answer for a card: yes (mine), no (not mine), or clear."""
    provider = _provider(args)  # the mailbox the answer belongs to comes from the sign-in
    ledger = Ledger(_state_dir() / "ledger.db")
    ledger.decide(provider.mailbox(), args.card, None if args.answer == "clear" else args.answer)
    print(f"Saved: {args.card} → {args.answer}")
    return 0


def cmd_forget(args) -> int:
    from .providers.gmail import revoke

    state = _state_dir()
    if revoke(state / "token.json"):
        print("Google access revoked.")
    else:
        print("Could not revoke with Google (no token, or offline). Revoke manually at "
              "https://myaccount.google.com/permissions")
    removed = []
    for f in ("token.json", "ledger.db"):
        p = state / f
        if p.exists():
            p.unlink()
            removed.append(str(p))
    print("Deleted: " + (", ".join(removed) if removed else "nothing"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="carddiscovery")
    ap.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS),
                    help="OAuth client JSON downloaded from Google Cloud (default: ./credentials.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth", help="sign in with Google (read-only)").set_defaults(fn=cmd_auth)

    d = sub.add_parser("discover", help="find the cards in this mailbox")
    d.add_argument("--name", action="append", help="your name as banks write it (repeatable)")
    d.add_argument("--years", type=int, default=5, choices=range(1, 16), metavar="1-15",
                   help="how far back to scan, in years (default 5). Older mail is never fetched")
    d.add_argument("--max-bodies", type=int, default=400, help="cap on full-email fetches (default 400)")
    d.add_argument("--alerts-per-window", type=int, default=40, help="alerts sampled per 6-month window")
    d.add_argument("--catalog", help="CSV with bank_id,product,aliases to extend product matching")
    d.add_argument("--bundle", help="signed rules bundle (bundle.json, with bundle.sig next to it)")
    d.add_argument("--details", action="store_true", help="show scores and evidence counts instead of sentences")
    d.add_argument("--json", help="also write results to this JSON file")
    d.add_argument("--show-unknown", action="store_true",
                   help="print subjects of bank emails the rules could not classify")
    d.add_argument("--fresh", action="store_true", help="ignore cached results and re-read everything")
    d.add_argument("--test-sender", action="append", default=[], metavar="ADDRESS",
                   help="treat mail from this address as bank mail, for testing (repeatable). "
                        "See docs/test-emails.md")
    d.set_defaults(fn=cmd_discover)

    s = sub.add_parser("senders", help="list card-related senders missing from the registry")
    s.add_argument("--limit", type=int, default=500)
    s.set_defaults(fn=cmd_senders)

    sub.add_parser("forget", help="revoke Google access and delete local token and cache").set_defaults(fn=cmd_forget)

    sv = sub.add_parser("serve", help="local web page that mimics the mobile flow")
    sv.add_argument("--port", type=int, default=0, help="port (default: any free port)")
    sv.add_argument("--no-browser", action="store_true", help="don't open the browser")
    sv.add_argument("--name", action="append", help="your name as banks write it (repeatable)")
    sv.add_argument("--test-sender", action="append", default=[], metavar="ADDRESS",
                    help="dev only (CARDDISCOVERY_DEV=1): treat mail from this address as bank mail")
    sv.add_argument("--bundle", help="signed rules bundle")
    sv.add_argument("--catalog", help="CSV with bank_id,product,aliases")
    sv.set_defaults(fn=cmd_serve)

    dc = sub.add_parser("decide", help="answer 'is this your card?' for a card")
    dc.add_argument("card", help="card id shown by discover, e.g. icici:9191")
    dc.add_argument("answer", choices=["yes", "no", "clear"])
    dc.set_defaults(fn=cmd_decide)

    b = sub.add_parser("bundle", help="build, sign and verify the rules bundle")
    b.add_argument("action", choices=["keygen", "build", "verify"])
    b.add_argument("--out", default="dist", help="output folder for build (default dist/)")
    b.add_argument("--path", default="dist/bundle.json", help="bundle to verify")
    b.set_defaults(fn=cmd_bundle)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except BundleError as e:
        print(f"Rules bundle error: {e}", file=sys.stderr)
        return 2
    except TestModeLocked as e:
        print(f"{e}. Test senders make personal mail count as bank mail, so they are "
              "for development only:\n  CARDDISCOVERY_DEV=1 carddiscovery discover --test-sender ...",
              file=sys.stderr)
        return 2
    except Exception as e:  # turn common Google setup errors into one readable line
        msg = _google_setup_hint(e)
        if msg is None:
            raise
        print(msg, file=sys.stderr)
        return 1


def _google_setup_hint(e: Exception) -> str | None:
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        return None
    if not isinstance(e, HttpError):
        return None
    text = str(e)
    if "accessNotConfigured" in text or "has not been used in project" in text:
        import re
        m = re.search(r"project[= ](\d+)", text)
        project = m.group(1) if m else "<your project>"
        return ("The Gmail API is not enabled for your Google Cloud project.\n"
                f"Enable it here, wait 2–3 minutes, then run the command again:\n"
                f"  https://console.developers.google.com/apis/api/gmail.googleapis.com/overview?project={project}")
    if e.resp.status in (401, 403) and ("insufficient" in text.lower() or "scope" in text.lower()):
        return ("Google refused access for this account. Check that it is listed under Test users on the "
                "OAuth consent screen, then run `carddiscovery forget` and `carddiscovery auth` again.")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
