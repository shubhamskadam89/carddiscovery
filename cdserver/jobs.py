"""Job handlers. Each runs inside a worker with one Gmail connection's quota."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import httpx
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from carddiscovery.providers.gmail import SCOPES
from carddiscovery.queries import Query
from carddiscovery.registry import Registry

from . import cards, config, pdf, queue
from .engine import (EvidenceStore, card_aware_scan, full_plan, number_terms, quick_plan, scan, since_date,
                     sync_plan)
from .gmailx import Cancelled, Meter, MeteredGmail
from .vault import Vault


class NeedsReconnect(Exception):
    pass


class Job:
    """What a handler gets: the job row, its user's connection, Gmail behind a meter."""

    def __init__(self, c, row: dict, registry: Registry, vault: Vault, worker: str):
        self.c, self.row, self.registry, self.vault, self.worker = c, row, registry, vault, worker
        self.id, self.account_id, self.kind = row["id"], row["account_id"], row["kind"]
        self.messages: list[str] = list((row.get("progress") or {}).get("messages", []))
        self.stats: dict = {}
        self.conn = c.execute("SELECT * FROM gmail_connections WHERE account_id = %s",
                              (self.account_id,)).fetchone()
        self.meter = Meter(config.BUDGETS[self.kind], on_flush=self._flush)
        self._gmail: MeteredGmail | None = None

    # progress, heartbeat and the cancel check, about once a second
    def progress(self) -> dict:
        return {"messages": self.messages[-12:], "stats": self.stats, "meter": self.meter.summary()}

    def _flush(self, meter: Meter) -> None:
        queue.heartbeat(self.c, self.id, meter.unflushed, self.progress())
        r = self.c.execute("SELECT cancel_requested, status FROM gmail_connections WHERE account_id = %s",
                           (self.account_id,)).fetchone()
        if not r or r["cancel_requested"] or r["status"] == "disconnecting":
            raise Cancelled()

    def say(self, msg: str) -> None:
        self.messages.append(msg)
        self.meter.flush()

    @property
    def gmail(self) -> MeteredGmail:
        if self._gmail is None:
            self._gmail = MeteredGmail(self._creds(), self.meter)
        return self._gmail

    def _creds(self) -> Credentials:
        info = json.loads(self.vault.open(bytes(self.conn["token_blob"]), f"gmail:{self.account_id}"))
        self.c.execute("INSERT INTO audit_log (account_id, actor, action) VALUES (%s, %s, %s)",
                       (self.account_id, self.worker, f"decrypt_token:{self.kind}"))
        creds = Credentials.from_authorized_user_info(info, SCOPES)
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise NeedsReconnect(str(e)) from None
        return creds

    def names(self) -> list[str]:
        return list(self.conn["names"] or [])

    def rebuild(self) -> dict:
        return cards.rebuild(self.c, self.account_id, self.registry)


def run(j: Job) -> None:
    if j.kind == "disconnect":
        return disconnect(j)
    if not j.conn or j.conn["status"] != "active" or j.conn["cancel_requested"]:
        raise Cancelled()
    try:
        HANDLERS[j.kind](j)
    except NeedsReconnect:
        j.c.execute("UPDATE gmail_connections SET status = 'needs_reconnect' WHERE account_id = %s", (j.account_id,))
        j.c.execute("INSERT INTO notifications (account_id, kind) VALUES (%s, 'reconnect')", (j.account_id,))
        raise


# ---------------------------------------------------------------- discovery

def discover_quick(j: Job) -> None:
    j.say(f"Connected to {j.gmail.mailbox()}")
    j.say("Checking the last 12 months: statements, welcome and card-update emails")

    def on_batch(st):
        j.stats = st.as_dict()
        r = j.rebuild()
        j.say(f"Read {st.header_reads} emails, {r['count']} cards so far")

    st = scan(j.gmail, j.registry, EvidenceStore(j.c, j.account_id), quick_plan(j.registry, date.today()),
              names=j.names(), max_reads=config.QUICK_MAX_READS, allow_full_reads=False, interleave=True,
              on_batch=on_batch)
    j.stats = st.as_dict()
    r = j.rebuild()
    j.say(f"Found {r['count']} cards in {st.seconds}s. Checking older emails in the background")
    queue.enqueue(j.c, "discover_full", j.account_id)


def discover_full(j: Job) -> None:
    years = j.conn["years"]
    j.say(f"Checking {years} years of bank email")

    def on_batch(st):
        j.stats = st.as_dict()
        j.rebuild()
        j.say(f"Read {st.header_reads} emails ({st.full_reads} in full)")

    if config.CARD_AWARE_FULL_SCAN:
        st = card_aware_scan(j.gmail, j.registry, EvidenceStore(j.c, j.account_id), date.today(), years,
                             names=j.names(), on_batch=on_batch,
                             on_window=lambda d, n: j.say(f"Checked back to {d:%b %Y}: {n} cards so far"))
    else:
        st = scan(j.gmail, j.registry, EvidenceStore(j.c, j.account_id), full_plan(j.registry, date.today(), years),
                  names=j.names(), on_batch=on_batch)
    j.stats = st.as_dict()
    r = j.rebuild()
    j.c.execute("UPDATE gmail_connections SET last_synced_at = %s WHERE account_id = %s",
                (j.row["started_at"], j.account_id))
    if r.get("rejected"):
        j.say(f"Ignored {r['rejected']} emails that claimed to be from a bank but failed Gmail's sender check")
    j.stats["rejected_by_bank"] = r.get("rejected_by_bank", {})
    j.say(f"Full check done: {r['count']} cards")
    if j.conn["pdf_opt_in"] and config.READ_STATEMENT_PDFS:
        queue.enqueue(j.c, "pdf_enrich", j.account_id)


def daily_sync(j: Job) -> None:
    if j.conn["last_synced_at"] is None:
        return  # the full scan hasn't finished; it sets the starting point
    started = datetime.now(timezone.utc)
    st = scan(j.gmail, j.registry, EvidenceStore(j.c, j.account_id),
              sync_plan(j.registry, since_date(j.conn["last_synced_at"])), names=j.names())
    j.stats = st.as_dict()
    r = j.rebuild()
    for key in r["new"]:
        j.c.execute("INSERT INTO notifications (account_id, kind, detail) VALUES (%s, 'new_card', %s)",
                    (j.account_id, json.dumps({"card_key": key})))
    j.c.execute("UPDATE gmail_connections SET last_synced_at = %s WHERE account_id = %s", (started, j.account_id))
    j.say(f"Synced: {st.header_reads} new bank emails, {len(r['new'])} new cards")


def token_check(j: Job) -> None:
    j._creds()


# ---------------------------------------------------------------- statement PDFs

def pdf_enrich(j: Job) -> None:
    dob = None
    if j.conn["dob_blob"]:
        dob = date.fromisoformat(j.vault.open(bytes(j.conn["dob_blob"]), f"dob:{j.account_id}").decode())
    name = (j.names() or [None])[0]
    # Only cards the user owns and still uses (never ones "to review": they may be someone else's;
    # not dormant or may-be-closed ones: they have no recent statements to read).
    rows = j.c.execute("SELECT bank_id, last4 FROM cards WHERE account_id = %s AND source = 'gmail' "
                       "AND grp = 'owned' AND last4 IS NOT NULL AND view->>'status' IN ('Active', 'New')",
                       (j.account_id,)).fetchall()
    by_bank: dict[str, set[str]] = {}
    for r in rows:
        by_bank.setdefault(r["bank_id"], set()).add(r["last4"])
    done_ids = {r["message_id"] for r in
                j.c.execute("SELECT message_id FROM statement_facts WHERE account_id = %s", (j.account_id,))}
    after = date.today() - timedelta(days=30 * config.PDF_MONTHS)
    tested = tuple(j.registry.test_senders)
    # One search per card, by its number: the newest statement email with a PDF for exactly that card.
    cards_ = [(r["bank_id"], r["last4"]) for r in rows]
    queries = [Query(family="pdf", senders=tuple(j.registry.banks[b].domains or []) + tested,
                     subject_any=("statement",), after=after, max_results=1,
                     extra=("has:attachment", "filename:pdf", "(" + " OR ".join(number_terms(b, l4)) + ")"))
               for b, l4 in cards_]
    j.say(f"Looking for the newest statement PDF of {len(cards_)} cards")
    # The full scan already indexed each card's statement-PDF emails: use them, search only when missing.
    indexed = {(r["bank_id"], r["last4"]): list(r["pdf_message_ids"]) for r in j.c.execute(
        "SELECT bank_id, last4, pdf_message_ids FROM card_index WHERE account_id = %s", (j.account_id,))}
    need = [i for i, card in enumerate(cards_) if not indexed.get(card)]
    searched = j.gmail.search_many([queries[i] for i in need]) if need else []
    found = [indexed.get(card, [])[:1] for card in cards_]
    for i, ids in zip(need, searched):
        found[i] = ids
    added = skipped = 0
    for (bank, l4), ids in zip(cards_, found):
        for mid in ids:
            if mid in done_ids:
                skipped += 1  # already have this statement
                continue
            if j.meter.affordable("full") < 1:
                break
            for _, att in j.gmail.pdf_attachments(mid):
                if j.meter.affordable("attachment") < 1:
                    break
                data = j.gmail.attachment(mid, att)
                try:
                    text = pdf.open_pdf(data, pdf.candidates(bank, name, dob, sorted(by_bank[bank])))
                except pdf.PasswordNeeded:
                    j.c.execute("INSERT INTO notifications (account_id, kind, detail) VALUES (%s, 'pdf_password', %s)",
                                (j.account_id, json.dumps({"bank": bank})))
                    continue
                finally:
                    data = None  # the PDF lives only in this loop
                f = pdf.parse(text)
                text = None
                if not f.last4 or f.last4 not in by_bank[bank]:
                    continue  # someone else's statement, or a card we don't know yet
                j.c.execute(
                    "INSERT INTO statement_facts (account_id, message_id, bank_id, last4, statement_date, total_due, "
                    "minimum_due, credit_limit, available_limit, reward_points, reconciled) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (account_id, message_id) DO NOTHING",
                    (j.account_id, mid, bank, f.last4, f.statement_date, f.total_due, f.minimum_due, f.credit_limit,
                     f.available_limit, f.reward_points, f.reconciled))
                added += 1
                break
    j.stats = {"pdfs_read": added, "older_statements_skipped": skipped}
    j.say(f"Added details from {added} statement PDFs")


# ---------------------------------------------------------------- disconnect / delete

def disconnect(j: Job) -> None:
    """Revoke at Google, then delete the token and everything derived from Gmail.
    Runs as a worker job because only workers can decrypt the token to revoke it."""
    revoked = False
    if j.conn:
        try:
            info = json.loads(j.vault.open(bytes(j.conn["token_blob"]), f"gmail:{j.account_id}"))
            j.c.execute("INSERT INTO audit_log (account_id, actor, action) VALUES (%s, %s, 'decrypt_token:revoke')",
                        (j.account_id, j.worker))
            r = httpx.post("https://oauth2.googleapis.com/revoke", data={"token": info.get("refresh_token")},
                           timeout=10)
            revoked = r.status_code == 200
        except Exception:
            revoked = False  # the token is deleted regardless
    with j.c.transaction():
        for t in ("evidence", "statement_facts", "notifications", "decisions", "card_index"):
            j.c.execute(f"DELETE FROM {t} WHERE account_id = %s", (j.account_id,))
        j.c.execute("DELETE FROM cards WHERE account_id = %s AND source = 'gmail'", (j.account_id,))
        j.c.execute("DELETE FROM gmail_connections WHERE account_id = %s", (j.account_id,))
        if j.row["payload"].get("delete_account"):
            j.c.execute("DELETE FROM accounts WHERE id = %s", (j.account_id,))  # cascades, this job row too
    j.stats = {"revoked": revoked}


HANDLERS = {"discover_quick": discover_quick, "discover_full": discover_full, "daily_sync": daily_sync,
            "pdf_enrich": pdf_enrich, "token_check": token_check}
