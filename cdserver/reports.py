"""Users telling us we got a card wrong, and the team keeping track of every such report.

Two ways in, one log (user_reports):

  In the app   On a card: "This card isn't mine" (the card's decision becomes "no"). On each email under a
               card: "Not about this card" (that email is left out when the cards are rebuilt). Both undo.

  By email     The user forwards the bank email we showed wrongly to the team's report inbox
               (CD_REPORT_INBOX), with a line saying what's wrong. The inbox is connected like any Gmail
               account (read-only) but never scanned for cards; the `report_inbox` job reads it every few
               minutes. For each forward: who sent it (must be a connected user's own mailbox, signed by
               their mail domain), the original email taken out of the forward, matched to the email we
               used (same bank, number and date), then acted on like the app's buttons. A forward we can't
               match or act on stays "open" for the team.

Every report keeps: account, source, kind, status, the email and card it's about, the user's note and
the forwarded email's subject. The team sees them in the dev panel ("User reports").
"""

from __future__ import annotations

import re
from datetime import date, timedelta, timezone, datetime
from email.utils import parseaddr, parsedate_to_datetime

from carddiscovery.models import MessageMeta

from . import config

LOOKBACK_DAYS = 30   # the inbox is read every few minutes; a month back covers any outage
_NOT_MINE = re.compile(r"not\s+(my|mine)|isn'?t\s+(my|mine)|don'?t\s+(have|own)|never\s+(had|applied|owned)|"
                       r"(friend|wife|husband|father|mother|brother|sister|someone\s+else)'?s?\b", re.I)


# ---------------------------------------------------------------- the log

def log(c, account_id: int | None, source: str, kind: str, status: str, *, message_id=None, card_key=None,
        bank_id=None, last4=None, note=None, subject=None, from_address=None, inbox_message_id=None,
        evidence_ids=None, correct: dict | None = None) -> int:
    import json
    ids = list(evidence_ids or ([message_id] if message_id else []))
    r = c.execute(
        "INSERT INTO user_reports (account_id, source, kind, status, message_id, card_key, bank_id, last4, note, "
        "subject, from_address, inbox_message_id, evidence_ids, correct) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s, %s, %s, %s, %s) ON CONFLICT (inbox_message_id) DO NOTHING RETURNING id",
        (account_id, source, kind, status, message_id, card_key, bank_id, last4, (note or "")[:500] or None,
         (subject or "")[:200] or None, from_address, inbox_message_id, ids,
         json.dumps(correct) if correct else None)).fetchone()
    return r["id"] if r else 0


def undo(c, account_id: int, *, message_id=None, card_key=None) -> None:
    """The user took back an app report: kept in the log, marked as undone."""
    c.execute("UPDATE user_reports SET status = 'fixed', note = coalesce(note || ' · ', '') || 'undone by the user' "
              "WHERE account_id = %s AND source = 'app' AND status = 'applied' AND "
              "(message_id = %s OR card_key = %s)", (account_id, message_id, card_key))


def recent(c, limit: int = 50, account_id: int | None = None) -> list[dict]:
    q = ("SELECT r.id, r.account_id, a.external_id AS user, r.source, r.kind, r.status, r.bank_id, r.last4, "
         "r.note, r.subject, r.correct, cardinality(r.evidence_ids) AS emails, r.created_at "
         "FROM user_reports r LEFT JOIN accounts a ON a.id = r.account_id ")
    args: tuple = ()
    if account_id:
        q += "WHERE r.account_id = %s "
        args = (account_id,)
    return [dict(r, created_at=r["created_at"].isoformat()) for r in
            c.execute(q + "ORDER BY r.id DESC LIMIT %s", args + (limit,))]


# ---------------------------------------------------------------- "not about this card" (app)

def wrong_ids(c, account_id: int) -> set[str]:
    return {r["message_id"] for r in c.execute("SELECT message_id FROM email_reports WHERE account_id = %s",
                                               (account_id,))}


def report_wrong(c, account_id: int, message_id: str, wrong: bool, note: str | None = None) -> None:
    if wrong:
        c.execute("INSERT INTO email_reports (account_id, message_id, note) VALUES (%s, %s, %s) "
                  "ON CONFLICT (account_id, message_id) DO UPDATE SET note = EXCLUDED.note, created_at = now()",
                  (account_id, message_id, (note or "")[:300] or None))
    else:
        c.execute("DELETE FROM email_reports WHERE account_id = %s AND message_id = %s", (account_id, message_id))


# ---------------------------------------------------------------- reading a forward

_MARKER = re.compile(r"-{3,}\s*forwarded message\s*-{3,}|begin forwarded message:?", re.I)
_HEADER = re.compile(r"^\s*(from|date|sent|subject|to|cc)\s*:\s*(.*)$", re.I)


def _parse_date(raw: str) -> datetime | None:
    raw = re.sub(r"\s+at\s+", " ", raw.strip())  # Gmail: "Mon, 12 Oct 2026 at 10:15"
    try:
        d = parsedate_to_datetime(raw)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in ("%a, %d %b %Y %H:%M", "%d %b %Y %H:%M", "%a, %b %d, %Y %I:%M %p", "%d %B %Y %H:%M:%S %p GMT%z"):
        try:
            d = datetime.strptime(raw, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    m = re.match(r"(\d{1,2} \w+ \d{4})", raw)  # "3 November 2026 9:02:11 AM GMT+5:30" -> the day at least
    if m:
        try:
            return datetime.strptime(m.group(1), "%d %B %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def unwrap(msg: MessageMeta) -> tuple[MessageMeta, str] | None:
    """(the original email inside a forward, the user's note above it), for Gmail's and Apple Mail's
    layouts. None when there's no forwarded block."""
    body = msg.body or ""
    m = _MARKER.search(body)
    if not m:
        return None
    note = " ".join(body[:m.start()].split())
    lines = body[m.end():].splitlines()
    heads, i = {}, 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    while i < len(lines):
        h = _HEADER.match(lines[i])
        if not h:
            break
        heads.setdefault(h.group(1).lower(), h.group(2).strip())
        i += 1
    if "from" not in heads:
        return None
    inner = "\n".join(lines[i:]).strip()
    when = _parse_date(heads.get("date") or heads.get("sent") or "") or msg.date
    orig = MessageMeta(message_id=msg.message_id, date=when, sender=heads["from"], subject=heads.get("subject", ""),
                       snippet=" ".join(inner.split())[:200], to="", auth_results="", body=inner)
    return orig, note


def signed_by_sender(msg: MessageMeta) -> bool:
    """Gmail's own check says the From address's mail domain signed it: the forward really came from
    that person, so a stranger can't report on someone else's cards."""
    from carddiscovery.classify import sender_signed
    return sender_signed(msg)


def match(evs: dict, ev, when: datetime):
    """The email we used that this forwarded original is: same bank, a shared card number when the
    original shows one, and the closest date within a day and a half."""
    best, best_key = None, None
    for e in evs.values():
        if e.bank_id != ev.bank_id:
            continue
        gap = abs((e.date - when).total_seconds())
        if gap > 36 * 3600:
            continue
        shared = bool(set(e.last4s) & set(ev.last4s))
        if ev.last4s and not shared:
            continue
        key = (not shared, e.type != ev.type, gap)
        if best_key is None or key < best_key:
            best, best_key = e, key
    return best


def read_inbox(gmail, registry, c, inbox: str, log_fn=None) -> dict:
    """New forwards in the report inbox -> user_reports, acted on where they can be."""
    from carddiscovery.classify import analyze
    from carddiscovery.queries import Query
    from . import cards
    from .engine import EvidenceStore
    users = {r["mailbox"].lower(): r["account_id"] for r in
             c.execute("SELECT account_id, mailbox FROM gmail_connections WHERE mailbox IS NOT NULL")}
    users.pop(inbox, None)
    q = Query(family="reports", senders=tuple(sorted(users)) or ("nobody@invalid",),
              after=date.today() - timedelta(days=LOOKBACK_DAYS), extra=(f"to:{inbox}",))
    seen = {r["inbox_message_id"] for r in c.execute(
        "SELECT inbox_message_id FROM user_reports WHERE inbox_message_id IS NOT NULL")}
    ids = [i for i in gmail.search(q) if i not in seen]
    out = {"found": len(ids), "applied": 0, "open": 0, "ignored": 0}
    rebuild = set()
    for msg in gmail.get_full(ids) if ids else []:
        sender = parseaddr(msg.sender or "")[1].lower()
        account = users.get(sender)
        if not account or not signed_by_sender(msg):
            out["ignored"] += 1  # not a connected user's own signed mail: no record of it
            continue
        got = unwrap(msg)
        msg.body = None
        if not got:
            log(c, account, "forward", "other", "open", note=f"not a forward: {msg.subject}"[:200],
                from_address=sender, inbox_message_id=msg.message_id)
            out["open"] += 1
            continue
        orig, note = got
        ev = analyze(orig, registry, [], [])
        orig.body = None  # email text never outlives this step
        kind = "not_my_card" if _NOT_MINE.search(note or "") else "wrong_email"
        hit = match(EvidenceStore(c, account).known(), ev, orig.date) if ev.bank_id else None
        card_key, status = None, "open"
        last4 = (ev.last4s or [None])[0]
        if hit:
            if kind == "not_my_card":
                row = c.execute("SELECT card_key FROM cards WHERE account_id = %s AND source = 'gmail' AND bank_id = %s "
                                "AND last4 = ANY(%s) LIMIT 1", (account, hit.bank_id, hit.last4s or [""])).fetchone()
                if row:
                    card_key = row["card_key"]
                    c.execute("INSERT INTO decisions (account_id, card_key, decision) VALUES (%s, %s, 'no') "
                              "ON CONFLICT (account_id, card_key) DO UPDATE SET decision = 'no', decided_at = now()",
                              (account, card_key))
                    status = "applied"
            else:
                report_wrong(c, account, hit.message_id.split("#")[0], True, note)
                status = "applied"
            rebuild.add(account)
        log(c, account, "forward", kind, status, message_id=hit and hit.message_id, card_key=card_key,
            bank_id=ev.bank_id, last4=last4, note=note, subject=orig.subject, from_address=sender,
            inbox_message_id=msg.message_id)
        out[status] += 1
    for a in rebuild:
        cards.rebuild(c, a, registry)
    if log_fn:
        log_fn(f"report inbox: {out}")
    return out


# ---------------------------------------------------------------- the emails behind a report

REVIEW_MAX = 15  # emails fetched per report review (60 units each)


def card_evidence_ids(c, account_id: int, card_key: str) -> list[str]:
    """Every evidence email (and statement-PDF row) of one of the user's cards, by Gmail id."""
    from carddiscovery.resolve import resolve_full
    from .engine import EvidenceStore
    wrong = wrong_ids(c, account_id)
    evs = [e for e in EvidenceStore(c, account_id).known().values() if e.message_id.split("#")[0] not in wrong]
    for card in resolve_full(evs).cards:
        if card.decision_key == card_key:
            return [e.message_id for e in sorted(card.evidence, key=lambda e: e.date, reverse=True)]
    return []


def review_emails(c, report_id: int, gmail_for=None) -> dict:
    """Fetch the emails a report is about, live, with the reporting user's read-only Gmail access, for the
    team to look at. Nothing fetched is stored; each review is written to the audit log."""
    from carddiscovery import extract
    r = c.execute("SELECT id, account_id, kind, evidence_ids, message_id FROM user_reports WHERE id = %s",
                  (report_id,)).fetchone()
    if not r:
        raise ValueError("no such report")
    ids = list(dict.fromkeys(i.split("#")[0] for i in (r["evidence_ids"] or ([r["message_id"]] if r["message_id"] else []))))
    pdf_rows = {i.split("#")[0] for i in (r["evidence_ids"] or []) if i.endswith("#pdf")}
    out = {"report": r["id"], "kind": r["kind"], "total": len(ids), "emails": []}
    if not ids:
        return out
    gmail = (gmail_for or _gmail_for)(c, r["account_id"])
    c.execute("INSERT INTO audit_log (account_id, actor, action) VALUES (%s, %s, %s)",
              (r["account_id"], "team_review", f"report:{r['id']}"))
    for m in gmail.get_full(ids[:REVIEW_MAX]):
        body, _ = extract.redact_pans(m.body or "")
        out["emails"].append({"id": m.message_id, "date": m.date.isoformat() if m.date else None, "from": m.sender,
                              "subject": m.subject, "text": " ".join(body.split())[:1500],
                              "attachments": list(m.attachment_names or ()), "pdf_evidence": m.message_id in pdf_rows})
        m.body = None
    return out


def _gmail_for(c, account_id: int):
    """The user's Gmail with their read-only token, metered like a small job."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from carddiscovery.providers.gmail import SCOPES
    from .gmailx import Meter, MeteredGmail
    from .jobs import _token_info
    conn = c.execute("SELECT token_blob FROM gmail_connections WHERE account_id = %s AND status = 'active'",
                     (account_id,)).fetchone()
    if not conn:
        raise ValueError("this user's Gmail isn't connected any more")
    creds = Credentials.from_authorized_user_info(_token_info(conn["token_blob"]), SCOPES)
    creds.refresh(Request())
    return MeteredGmail(creds, Meter(REVIEW_MAX * 60 + 100))


# ---------------------------------------------------------------- "Report a mistake" (a correction)

def catalog(registry) -> list[dict]:
    """Banks and their card products, for the correction form's two dropdowns."""
    out = []
    for bank_id, bank in sorted(registry.banks.items(), key=lambda kv: kv[1].name.lower()):
        products = sorted({p for _, p in bank.product_patterns}, key=str.lower)
        out.append({"id": bank_id, "name": bank.name, "products": products})
    return out


def correction(c, registry, account_id: int, card_key: str, bank: str, product: str | None, last4: str | None,
               note: str | None) -> int:
    """The user says this card is really <bank> <product> ending <last4>: logged with the card's evidence,
    status open (the team decides; the card shows it as reported meanwhile)."""
    if bank not in registry.banks:
        raise ValueError("unknown bank")
    products = {p for _, p in registry.banks[bank].product_patterns}
    if product and product not in products:
        raise ValueError("unknown card for that bank")
    if last4 and not re.fullmatch(r"\d{4}", last4):
        raise ValueError("last 4 digits must be 4 digits")
    card = c.execute("SELECT bank_id, last4 FROM cards WHERE account_id = %s AND card_key = %s",
                     (account_id, card_key)).fetchone()
    if not card:
        raise ValueError("no such card")
    return log(c, account_id, "app", "correction", "open", card_key=card_key, bank_id=card["bank_id"],
               last4=card["last4"], note=note, evidence_ids=card_evidence_ids(c, account_id, card_key),
               correct={"bank": bank, "product": product or None, "last4": last4 or None})


def open_corrections(c, account_id: int) -> dict[str, dict]:
    """card_key -> the latest open correction the user sent for it."""
    out = {}
    for r in c.execute("SELECT card_key, correct, created_at FROM user_reports WHERE account_id = %s AND "
                       "kind = 'correction' AND status = 'open' ORDER BY id", (account_id,)):
        out[r["card_key"]] = dict(r["correct"] or {}, at=r["created_at"].isoformat())
    return out
