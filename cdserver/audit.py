"""Inbox audit (development only): what a real inbox's bank mail looks like to the engine.

    CARDDISCOVERY_DEV=1 python -m cdserver audit --account 1 [--every 4] [--rate 2000]

Reads headers and the preview text (format=metadata, 20 units each) of every Nth email from
the known bank and app senders, with nothing filtered out, plus card-related mail from other
senders. Then reports:
  - what the server's searches would do with each email (fetched, or dropped and why)
  - every card number seen, and whether the server already has that card
  - card-numbered mail no search fetches, account mail carrying number shapes,
    unclassified subject patterns, 2-3 digit endings, mail not signed by its bank
  - sender addresses in use, and card-related senders missing from banks.toml with their
    signature status (whether to add them as a bank domain or a bill-payment app)

Only for inboxes whose owner agreed to this review (Google's Limited Use rules on people
reading mail). Uses the account's stored token, which must be read-only; only list/get are
called. Raw headers are kept in a temporary folder deleted at the end; the report masks digits
in subjects and shows no previews.
"""

from __future__ import annotations

import json
import re
import socket
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from carddiscovery import extract
from carddiscovery.classify import analyze
from carddiscovery.models import EvidenceType as T, MessageMeta
from carddiscovery.queries import CATCH_ALL_TEXT, FAMILIES, SAMPLED_FAMILY_SUBJECTS
from carddiscovery.registry import Registry, dev_mode, sender_domain

from . import db
from .engine import CARD_OFFER_SUBJECT, NOISE_SUBJECT

READONLY = "https://www.googleapis.com/auth/gmail.readonly"
HEADERS = ["From", "Subject", "Date", "To", "Authentication-Results", "List-Unsubscribe"]
OTHER_TEXT = ('"credit card"', '"card ending"', '"card no"', "BBPS", '"bill payment"', '"card bill"')


# ---------------------------------------------------------------- reading (read-only)

def _service(c, account_id: int):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    from .jobs import _token_info
    row = c.execute("SELECT token_blob, scopes FROM gmail_connections WHERE account_id = %s",
                    (account_id,)).fetchone()
    if not row:
        raise SystemExit(f"account {account_id} has no Gmail connection")
    if set(row["scopes"] or []) != {READONLY}:
        raise SystemExit(f"refusing: the token's scopes are {row['scopes']}, not read-only")
    c.execute("INSERT INTO audit_log (account_id, actor, action) VALUES (%s, 'audit', 'read_token:audit')",
              (account_id,))
    creds = Credentials.from_authorized_user_info(_token_info(row["token_blob"]), [READONLY])
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _call(req):
    """Wait out rate limits (shared with any running scan) and network timeouts."""
    from googleapiclient.errors import HttpError
    for attempt in range(8):
        try:
            return req.execute()
        except HttpError as e:
            if e.resp.status not in (403, 429, 500, 503):
                raise
        except (TimeoutError, socket.timeout, ConnectionError):
            pass
        time.sleep(15 * (attempt + 1))
    raise RuntimeError("gave up after repeated rate limits or timeouts")


def _list(svc, q: str) -> list[str]:
    ids, token = [], None
    while True:
        r = _call(svc.users().messages().list(userId="me", q=q, maxResults=500, pageToken=token))
        ids += [m["id"] for m in r.get("messages", [])]
        token = r.get("nextPageToken")
        if not token:
            return ids
        time.sleep(1)


def pull(svc, registry: Registry, out: Path, every: int, rate: int, log=print) -> None:
    known = " OR ".join(registry.all_domains())
    plan = {"known": f"from:({known})", "other": f"({' OR '.join(OTHER_TEXT)}) -from:({known})"}
    todo = []
    for name, q in plan.items():
        ids = _list(svc, q)
        log(f"{name}: {len(ids)} emails" + (f", reading every {every}th" if name == "known" else ""))
        todo += [(name, i) for i in (ids[::every] if name == "known" else ids)]
    per_batch, pause = 25, 60 * 25 * 20 / rate
    with out.open("w") as f:
        for start in range(0, len(todo), per_batch):
            chunk = dict((i, n) for n, i in todo[start:start + per_batch])
            rows, pending = [], list(chunk)
            for attempt in range(8):
                failed = []

                def cb(rid, resp, exc):
                    (failed.append(rid) if exc else rows.append(resp))

                batch = svc.new_batch_http_request(callback=cb)
                for i in pending:
                    batch.add(svc.users().messages().get(userId="me", id=i, format="metadata",
                                                         metadataHeaders=HEADERS), request_id=i)
                _call(batch)
                pending = failed
                if not pending:
                    break
                time.sleep(15 * (attempt + 1))
            for m in rows:
                h = {x["name"]: x["value"] for x in m.get("payload", {}).get("headers", [])}
                f.write(json.dumps({"id": m["id"], "query": chunk.get(m["id"]), "labels": m.get("labelIds", []),
                                    "date": int(m.get("internalDate", 0)), "snippet": m.get("snippet", ""),
                                    "headers": h}) + "\n")
            if (start // per_batch) % 20 == 0:
                log(f"  {start + len(chunk)}/{len(todo)}")
            time.sleep(pause)


# ---------------------------------------------------------------- report

def _words(text: str, words) -> bool:
    """Gmail-style whole-word match ("debit" doesn't match "debited")."""
    joined = " " + " ".join(re.findall(r"[a-z0-9]+", text.lower())) + " "
    return any(" " + " ".join(re.findall(r"[a-z0-9]+", w.lower())) + " " in joined for w in words)


def search_fate(subject: str, snippet: str, labels: list[str], recent: bool) -> str:
    """Which of the server's card-aware searches would return this email (from the subject
    and preview; Gmail also searches the body, so "not matched" can be an overstatement)."""
    promo = "CATEGORY_PROMOTIONS" in labels
    if _words(subject, NOISE_SUBJECT):
        return "dropped: noise word in subject"
    if _words(subject, FAMILIES["statement"] + FAMILIES["payment"] + FAMILIES["application"]):
        return "searched (core)"
    if recent and _words(subject, SAMPLED_FAMILY_SUBJECTS):
        return "searched (alerts, last 12 months)"
    if recent and promo and _words(subject, CARD_OFFER_SUBJECT):
        return "searched (card offers, last 12 months)"
    if _words(subject, FAMILIES["onboarding"] + FAMILIES["lifecycle"]) or _words(f"{subject} {snippet}",
                                                                              CATCH_ALL_TEXT):
        return "dropped: Promotions tab" if promo else "searched (other)"
    return "not matched by any search"


def _tpl(s: str) -> str:
    return re.sub(r"9[9,.]*", "9", re.sub(r"\d", "9", s)).strip()[:90]


def _signers(auth: str) -> str:
    d = sorted(set(re.findall(r"dkim=pass[^;]*?header\.(?:i=@?|d=)([\w.-]+)", auth)))
    dm = re.findall(r"dmarc=(\w+)", auth)
    return f"dkim={','.join(d) or 'none'} dmarc={dm[0] if dm else 'none'}"


def report(rows: list[dict], registry: Registry, have: set[str], now: datetime) -> str:
    out: list[str] = []
    p = out.append
    fate, fate_num, types = Counter(), Counter(), defaultdict(Counter)
    nums = defaultdict(lambda: {"n": 0, "types": Counter(), "first": None, "last": None})
    unreached, notcc, unknown, partials, unsigned, addrs = (Counter() for _ in range(6))
    others, other_banks, other_sig = Counter(), Counter(), defaultdict(Counter)
    other_nums = defaultdict(set)
    year_ago = now.timestamp() - 365 * 86400
    for r in rows:
        h = r["headers"]
        subj, frm = h.get("Subject", ""), h.get("From", "")
        dt = datetime.fromtimestamp(r["date"] / 1000, tz=timezone.utc)
        dom = sender_domain(frm) or "?"
        if r["query"] == "other":
            others[dom] += 1
            other_sig[dom][_signers(h.get("Authentication-Results", ""))] += 1
            text = f"{subj}\n{r['snippet']}"
            bank, l4 = registry.bank_from_text(text), extract.last4s(text)
            if bank:
                other_banks[f"{dom:30} names {bank:9} {'WITH a card number' if l4 else 'no number'}"] += 1
                for x in l4:
                    other_nums[f"{bank}:{x}"].add(dom)
            continue
        m = MessageMeta(r["id"], dt, frm, subj, r["snippet"], to=h.get("To", ""),
                        auth_results=h.get("Authentication-Results", ""), labels=tuple(r["labels"]))
        ev = analyze(m, registry, [], [])
        addrs[frm.split("<")[-1].rstrip(">").lower()] += 1
        f = search_fate(subj, r["snippet"], r["labels"], r["date"] / 1000 >= year_ago)
        fate[f] += 1
        types[ev.bank_id][ev.type.value] += 1
        if ev.bank_id and not ev.dkim_pass:
            unsigned[dom] += 1
        if ev.last_digits:
            partials[f"{ev.bank_id} ••{ev.last_digits} ({ev.type.value})"] += 1
        if ev.type == T.UNKNOWN:
            unknown[f"{ev.bank_id} | {_tpl(subj)}"] += 1
        if not ev.last4s:
            continue
        fate_num[f] += 1
        if ev.type == T.NOT_CREDIT_CARD:
            notcc[f"{ev.bank_id} | {_tpl(subj)}"] += 1
        elif not f.startswith("searched"):
            unreached[f"{ev.bank_id} | {ev.type.value} | {_tpl(subj)}"] += 1
        if ev.type in (T.NOT_CREDIT_CARD, T.PROMOTIONAL):
            continue
        for x in ev.last4s:
            n = nums[f"{ev.bank_id}:{x}"]
            n["n"] += 1
            n["types"][ev.type.value] += 1
            n["first"] = min(n["first"] or dt, dt)
            n["last"] = max(n["last"] or dt, dt)

    def section(title, counter, limit=30):
        p(f"\n== {title}")
        for k, v in counter.most_common(limit):
            p(f"  {v:5}  {k}")

    p(f"emails read: {len(rows)} ({sum(r['query'] == 'known' for r in rows)} from known senders, "
      f"{sum(r['query'] == 'other' for r in rows)} card-related from other senders)")
    p("\n== What the server's searches would do (known senders)")
    for k, v in fate.most_common():
        p(f"  {v:5}  {k}   (with a card number: {fate_num[k]})")
    p("\n== Card numbers in card mail (account and promotional mail left out)")
    for k, n in sorted(nums.items(), key=lambda x: -x[1]["n"]):
        p(f"  {'HAVE' if k in have else 'MISSING':7} {k:16} {n['n']:4} emails  {n['first']:%Y-%m}..{n['last']:%Y-%m}"
          f"  {dict(n['types'].most_common(4))}")
    section("Card-numbered mail that no search fetches", unreached)
    section("Classified 'not credit card' but carrying a number (check these are accounts)", notcc, 20)
    section("Unclassified ('unknown') subject patterns", unknown)
    section("2-3 digit endings", partials, 20)
    section("Bank mail not signed by the bank's domain", unsigned, 20)
    p("\n== Type mix per bank")
    for b, c in sorted(types.items(), key=lambda x: str(x[0])):
        p(f"  {b}: {dict(c.most_common())}")
    section("Sender addresses of known banks and apps", addrs, 60)
    p("\n== Card-related mail from senders NOT in banks.toml (domain: count, signatures)")
    for d, v in others.most_common(50):
        p(f"  {v:5}  {d:32} {dict(other_sig[d].most_common(2))}")
    section("  ...of which name a bank (read as app mail would be)", other_banks, 40)
    p("\n  card numbers in those:")
    for k, ds in sorted(other_nums.items()):
        p(f"  {'HAVE' if k in have else 'MISSING':7} {k:16} via {sorted(ds)}")
    return "\n".join(out)


def run(account_id: int, every: int, rate: int) -> str:
    if not dev_mode():
        raise SystemExit("the inbox audit is for development only: set CARDDISCOVERY_DEV=1")
    registry = Registry.load()
    c = db.connect()
    have = {r["k"] for r in c.execute("SELECT bank_id || ':' || last4 AS k FROM cards "
                                      "WHERE account_id = %s AND last4 IS NOT NULL", (account_id,))}
    svc = _service(c, account_id)
    with tempfile.TemporaryDirectory(prefix="cd-audit-") as tmp:
        path = Path(tmp) / "headers.jsonl"
        pull(svc, registry, path, every, rate)
        rows = [json.loads(line) for line in path.open()]
    return report(rows, registry, have, datetime.now(timezone.utc))
