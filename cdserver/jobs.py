"""Job handlers. Each runs inside a worker with one Gmail connection's quota."""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta, timezone

import httpx
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from carddiscovery import extract
from carddiscovery.classify import analyze
from carddiscovery.models import Evidence, EvidenceType as T
from carddiscovery.providers.gmail import SCOPES
from carddiscovery.registry import _extends
from carddiscovery.resolve import resolve_full
from carddiscovery.rules import active_version
from carddiscovery.queries import Query
from carddiscovery.registry import Registry

from . import cards, config, pdf, queue
from .engine import (EvidenceStore, card_aware_scan, half_card_lookup, half_cards, _years_ago, subject_scan, windowed_subject_scan, month_windows,
                     year_windows, MONTHLY_SUBJECTS, LIFECYCLE_SUBJECTS, full_plan, number_terms, quick_plan, scan, since_date,
                     statement_plan,
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
        # For the developer panel: step, month being checked, PDF counts, and units and seconds per
        # step. Carried over from the last run, so a scan that paused and resumed keeps its totals.
        self.dev: dict = dict((row.get("progress") or {}).get("dev") or {})
        self._mark = (0, time.monotonic())  # units and clock when the current step's tally last moved
        self.conn = c.execute("SELECT * FROM gmail_connections WHERE account_id = %s",
                              (self.account_id,)).fetchone()
        self.meter = Meter(config.BUDGETS[self.kind], on_flush=self._flush)
        self._gmail: MeteredGmail | None = None

    # progress, heartbeat and the cancel check, about once a second
    def progress(self) -> dict:
        return {"messages": self.messages[-12:], "stats": self.stats, "meter": self.meter.summary(), "dev": self.dev}

    def tally(self) -> None:
        """Add the units and seconds spent since the last tally to the current step's totals."""
        units, t = self.meter.units, time.monotonic()
        step = self.dev.get("step")
        if step:
            per = self.dev.setdefault("steps_cost", {}).setdefault(step, {"units": 0, "secs": 0.0})
            per["units"] += units - self._mark[0]
            per["secs"] = round(per["secs"] + t - self._mark[1], 1)
        self._mark = (units, t)

    def _flush(self, meter: Meter) -> None:
        if self._gmail is not None:
            meter.throttled, meter.backoff_s = self._gmail.throttled, self._gmail.backoff_s
        self.tally()
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
        info = _token_info(self.conn["token_blob"])
        self.c.execute("INSERT INTO audit_log (account_id, actor, action) VALUES (%s, %s, %s)",
                       (self.account_id, self.worker, f"read_token:{self.kind}"))
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


def _token_info(blob) -> dict:
    """The stored Google token (plain JSON). A token saved encrypted by an older version can't
    be read any more: the user has to connect Gmail again."""
    try:
        return json.loads(bytes(blob))
    except (ValueError, TypeError):
        raise NeedsReconnect("stored Gmail token can't be read: connect Gmail again") from None


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
              # No name checks in the quick scan: banks often greet "Dear Cardholder", and a card
              # must never be hidden in the first seconds over a greeting. The full scan checks names.
              names=[], max_reads=config.QUICK_MAX_READS, allow_full_reads=False, interleave=True,
              on_batch=on_batch)
    j.stats = st.as_dict()
    r = j.rebuild()
    j.say(f"Found {r['count']} cards in {st.seconds}s. Checking older emails in the background")
    queue.enqueue(j.c, "statements", j.account_id)  # statement first, before the full scan
    queue.enqueue(j.c, "discover_full", j.account_id)


PDF_MONTHS_BACK = 6  # the monthly mode reads incomplete cards' statements this far back


def _cards_by_bank(j: Job) -> str:
    rows = j.c.execute("SELECT bank_id, count(*) AS n FROM cards WHERE account_id = %s AND source = 'gmail' "
                       "AND grp IN ('owned', 'review') GROUP BY bank_id ORDER BY count(*) DESC, bank_id",
                       (j.account_id,)).fetchall()
    return ", ".join(f"{j.registry.banks[r['bank_id']].name if r['bank_id'] in j.registry.banks else r['bank_id']} "
                     f"{r['n']}" for r in rows)


def _monthly_full_scan(j: Job, years: int, consent: bool, on_batch):
    """CD_SCAN_MODE=monthly, in three steps:
    1. month by month, newest first, over every year chosen: subjects only, for mail saying
       "credit card" or a card statement, with the cards found so far excluded; the app shows
       the cards per bank after each month;
    2. cards still missing their number or product: their statement emails over the last 6
       months, one month at a time, newest first: the email in full, then the PDF only if the
       card is still incomplete (PDFs only with the user's consent and secrets);
    3. a lifecycle pass (welcome, dispatch, fee, renewal, closure) over every year, for dormant
       and closed cards that rarely send anything else."""
    store = EvidenceStore(j.c, j.account_id)
    today = date.today()

    months = month_windows(today, years)
    lifecycle = year_windows(today, years)
    plan = [("subjects", len(months)), ("half cards", 0), ("statements", PDF_MONTHS_BACK),
            ("lifecycle", len(lifecycle))]

    def step(name, i, after=None, before=None, total=None, label=None):
        if j.dev.get("step") != name:
            j.tally()  # close the previous step's units and time before this one starts
        j.dev.update(step=name, steps=plan, window_index=i, window_total=total or dict(plan)[name],
                     window=[after.isoformat(), before.isoformat()] if after else None, label=label,
                     pdfs_consent=consent)

    done = iter(range(1, len(months) + 1))
    step("subjects", 0, *months[0])

    def shown(after, before):
        j.rebuild()
        i = next(done)
        if i < len(months):
            step("subjects", i, *months[i])
        j.say(f"Checked {after:%b %Y}: {_cards_by_bank(j)}")

    st = windowed_subject_scan(j.gmail, j.registry, store, months, MONTHLY_SUBJECTS,
                               names=j.names(), on_batch=on_batch, on_window=shown)
    # Cards known by number only or by name only: look for the other half, offers included.
    half = half_cards(store.known().values())
    tally = j.dev.setdefault("half_cards", {"total": len(half), "completed": 0})
    if half:
        step("half cards", 0, total=len(half), label="looking up")

        def card_done(i, n, bank, want, value, found):
            tally["completed"] += int(found)
            what = f"••{value}" if want == "name" else value
            res = "found" if found else "not found"
            step("half cards", i + 1, total=n, label=f"{bank} {what}: {want} {res}")
            j.rebuild()
            j.say(f"Looked up the {want} of {bank} {what}: {res}")

        hs = half_card_lookup(j.gmail, j.registry, store, _years_ago(today, years), names=j.names(),
                              on_batch=on_batch, on_card=card_done)
        st.header_reads += hs.header_reads
        st.full_reads += hs.full_reads
        st.searches += hs.searches
    secrets = _pdf_secrets(j)
    for i, (after, before) in enumerate(month_windows(today, 1)[:PDF_MONTHS_BACK]):
        step("statements", i, after, before)
        read_statement_gaps(j, *secrets, pdfs=consent, window=(after, before))
        j.say(f"Statements of {after:%b %Y} checked for incomplete cards: {_cards_by_bank(j)}")
    done_life = iter(range(1, len(lifecycle) + 1))
    step("lifecycle", 0, *lifecycle[0])

    def life_shown(after, before):
        i = next(done_life)
        if i < len(lifecycle):
            step("lifecycle", i, *lifecycle[i])
        j.say(f"Lifecycle mail before {before:%b %Y} checked")

    life = windowed_subject_scan(j.gmail, j.registry, store, lifecycle, LIFECYCLE_SUBJECTS,
                                 names=j.names(), on_batch=on_batch, family="lifecycle", on_window=life_shown)
    j.tally()
    j.dev.update(step="done", window_index=len(lifecycle), label=None)
    st.header_reads += life.header_reads
    st.searches += life.searches
    return st


def discover_full(j: Job) -> None:
    years = j.conn["years"]
    j.say(f"Checking {years} years of bank email")

    def on_batch(st):
        j.stats = st.as_dict()
        j.rebuild()
        j.say(f"Read {st.header_reads} emails ({st.full_reads} in full)")

    consent = bool(j.conn["pdf_opt_in"] and config.READ_STATEMENT_PDFS)
    if config.SCAN_MODE == "monthly":
        st = _monthly_full_scan(j, years, consent, on_batch)
    elif config.SCAN_MODE == "subjects":
        st = subject_scan(j.gmail, j.registry, EvidenceStore(j.c, j.account_id), date.today(), years,
                          names=j.names(), on_batch=on_batch,
                          on_window=lambda d, n: j.say(f"Checked back to {d:%b %Y}: {n} cards so far"),
                          after_first_year=lambda: read_statement_gaps(j, *_pdf_secrets(j), pdfs=consent))
        read_statement_gaps(j, *_pdf_secrets(j), pdfs=consent)  # cards still incomplete after all years
    elif config.CARD_AWARE_FULL_SCAN:
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

def statement_gaps(known: dict, tried: dict[str, str], complete: list | None = None,
                   window: tuple[date, date] | None = None) -> list:
    """Statement emails to open first: per group of statements missing the card number or the
    product ("HSBC Live+", no number; "Axis MY Zone", ending XX30; "HDFC ••3254", no product),
    the newest one not tried yet. A group is skipped when its subject or body already ties it
    to a card known with both number and product (`complete`, from the resolver), and settled,
    never opened again, once a statement PDF has given a number and product that fit it, or any
    of its emails was already read."""
    statements = [e for e in known.values() if e.type == T.STATEMENT and e.bank_id
                  and not e.message_id.endswith("#pdf") and not (e.last4s and e.product)]
    if window:  # one month at a time: only that month's statement emails
        statements = [e for e in statements if window[0] <= e.date.date() < window[1]]
    settled = [e for e in known.values() if e.message_id.endswith("#pdf") and e.last4s and e.product]
    complete = complete or []

    def known_card(group_key) -> bool:
        bank, product, digits, number = group_key
        cards = [c for c in complete if c.bank_id == bank]
        if number:
            return any(c.last4 == number for c in cards)
        same = [c for c in cards if product is None or product in {p for _, p in c.product_history}]
        if digits:
            return len([c for c in same if c.last4.endswith(digits)]) == 1
        return product is not None and len(same) == 1

    def fits(group_key, s) -> bool:
        bank, product, digits, number = group_key
        return (s.bank_id == bank and (product is None or s.product == product
                                       or _extends(s.product, product) or _extends(product, s.product))
                and (digits is None or s.last4s[0].endswith(digits))
                and (number is None or s.last4s[0] == number))

    groups: dict[tuple, list] = {}
    for e in statements:
        key = (e.bank_id, e.product, e.last_digits, e.last4s[0] if e.last4s else None)
        groups.setdefault(key, []).append(e)
    out = []
    for key, evs in groups.items():
        if (known_card(key) or any(fits(key, s) for s in settled)
                or any(tried.get(e.message_id) in ("read", "email") for e in evs)):
            continue
        todo = sorted((e for e in evs if e.message_id not in tried), key=lambda e: e.date, reverse=True)
        tries = STATEMENT_TRIES_PER_GROUP if any(key[1:]) else UNKNOWN_GROUP_TRIES
        out += [(key, e) for e in todo[:tries]]
    return out


STATEMENT_TRIES_PER_GROUP = 2  # newest first; a second try covers a statement email sent without its PDF
# A group with no product, ending or number ("HDFC statement") can hide several cards, so more
# of its emails are read; each read refines its email's evidence, regrouping it on the next run.
UNKNOWN_GROUP_TRIES = 6


def read_statement_gaps(j: Job, name: str | None, dob: date | None, pdfs: bool = True,
                        window: tuple[date, date] | None = None) -> int:
    """Statement first: a statement PDF names the bank, the card number and the product at
    once, which settles a card that other mail only half-describes. The newest PDF of each
    group in statement_gaps() is opened in memory; its card number(s) and product become an
    evidence row ("<message id>#pdf") like any email's, and its totals go to statement_facts.
    About 80 units per PDF (full read to list the attachment, then the attachment)."""
    store = EvidenceStore(j.c, j.account_id)
    # PDFs tried by an older PDF reader are tried once more, and what that reader took from
    # them is dropped first (it may hold numbers the new rules reject).
    with j.c.transaction():
        j.c.execute("DELETE FROM evidence WHERE account_id = %s AND message_id IN (SELECT message_id || '#pdf' "
                    "FROM statement_pdf_reads WHERE account_id = %s AND pdf_version < %s)",
                    (j.account_id, j.account_id, pdf.PDF_READER_VERSION))
        j.c.execute("DELETE FROM statement_pdf_reads WHERE account_id = %s AND pdf_version < %s",
                    (j.account_id, pdf.PDF_READER_VERSION))
    known = store.known()
    tried = {r["message_id"]: r["outcome"] for r in j.c.execute(
        "SELECT message_id, outcome FROM statement_pdf_reads WHERE account_id = %s", (j.account_id,))}
    if pdfs:  # emails checked before the user consented to PDFs: try their PDFs now
        tried = {m: o for m, o in tried.items() if o != "checked"}
    # Cards already known with number and product: their statements need no PDF.
    complete = [c for c in resolve_full(list(known.values())).cards if c.last4 and c.product_history]
    gaps = statement_gaps(known, tried, complete, window)
    if not gaps:
        j.rebuild()
        return 0
    j.say(f"Reading {len(gaps)} statement PDFs for cards missing their number or product")
    bank_l4s: dict[str, set[str]] = {}
    for e in known.values():
        if e.bank_id and e.type not in (T.NOT_CREDIT_CARD, T.PROMOTIONAL):
            bank_l4s.setdefault(e.bank_id, set()).update(e.last4s)

    pdf_dev = j.dev.setdefault("statement_reads", {})

    def record(mid: str, bank: str, outcome: str) -> None:
        pdf_dev[outcome] = pdf_dev.get(outcome, 0) + 1
        j.c.execute("INSERT INTO statement_pdf_reads (account_id, message_id, bank_id, outcome, pdf_version) VALUES "
                    "(%s, %s, %s, %s, %s) ON CONFLICT (account_id, message_id) DO UPDATE SET outcome = EXCLUDED.outcome, "
                    "pdf_version = EXCLUDED.pdf_version, read_at = now()",
                    (j.account_id, mid, bank, outcome, pdf.PDF_READER_VERSION))

    read = 0
    done_groups: set[tuple] = set()
    for key, e in gaps:
        if key in done_groups:
            continue  # read already, or locked: an older PDF of the group has the same password
        if j.meter.affordable("full") < 1 or j.meter.affordable("attachment") < 1:
            break
        # One full read gives the email with its attachment names and its PDF ids. Read under
        # today's rules first (HDFC names the PDF after the card, "0036XXXXXXXXXX13_...pdf",
        # and Yes writes "YES_BANK_ACE" in the subject): when the email alone now gives the
        # product and the number or its ending, the group is settled without the PDF.
        meta, attachments = j.gmail.full_with_pdfs(e.message_id)
        fresh = analyze(meta, j.registry, [], j.names())
        if fresh.bank_id == e.bank_id and fresh.type == T.STATEMENT:
            fresh.to_user, fresh.date = e.to_user, e.date
            store.save([fresh])
            if fresh.product and (fresh.last4s or fresh.last_digits):
                record(e.message_id, e.bank_id, "email")
                if any(key[1:]):  # a group that knew nothing may still hide other cards
                    done_groups.add(key)
                continue
        if not pdfs:  # no consent to open statement PDFs: the email was all we could use
            record(e.message_id, e.bank_id, "checked")
            continue
        if not attachments:
            record(e.message_id, e.bank_id, "no_pdf")
            continue
        # How the PDF is locked: what the email says, else what this bank is known to use. A PDF
        # that needs what we don't have (HSBC: 6 digits of the card number) isn't downloaded.
        l4s = sorted(bank_l4s.get(e.bank_id, ()))
        rule = pdf.password_rule(meta.text())
        rules = [rule] if rule else pdf.PATTERNS.get(e.bank_id, [])
        if rules and not any(pdf.can_open(r, bool(name), bool(dob), l4s) for r in rules):
            record(e.message_id, e.bank_id, "password")
            locked = j.dev.setdefault("pdfs_not_downloaded", {})
            locked[e.bank_id] = locked.get(e.bank_id, 0) + 1
            continue
        outcome = "no_number"
        for att_name, att in [a for a in attachments if not re.search(r"terms|mitc|conditions", a[0], re.I)][:2]:
            if j.meter.affordable("attachment") < 1:
                break
            data = j.gmail.attachment(e.message_id, att)
            keyed = pdf.keyed_candidates(e.bank_id, name, dob, l4s, rule)
            try:
                text, used = pdf.open_pdf_with(data, [pw for pw, _ in keyed])
            except pdf.PasswordNeeded:
                outcome = "password"
                data = None
                j.c.execute("INSERT INTO notifications (account_id, kind, detail) VALUES (%s, 'pdf_password', %s)",
                            (j.account_id, json.dumps({"bank": e.bank_id})))
                continue
            except Exception:  # a PDF the library can't parse: record it, never download it again
                outcome = "unreadable"
                data = None
                continue
            unlocked_by = dict(keyed).get(used) if used else None  # SBI: the last 4 in the password
            card = pdf.card_identity(text, j.registry, e.bank_id, known=bank_l4s.get(e.bank_id, set()),
                                     unlocked_by=unlocked_by, partial_hint=e.last_digits)
            facts = pdf.parse(text)
            text = None
            if card.last4s and not card.product and not fresh.product:
                # The text doesn't name the card (ICICI, RBL print it only as a logo): read the
                # bank's logo spot on page 1. Only a product name is taken, and only a clear match.
                catalog = sorted({p for _, p in j.registry.banks[e.bank_id].product_patterns})
                named = pdf.product_by_ocr(data, used, e.bank_id, catalog)
                if named:
                    card.product, card.products = named, [named] + [p for p in card.products if p != named]
                    j.dev["ocr_named"] = j.dev.get("ocr_named", 0) + 1
            data = None  # the PDF lives only in this loop
            if not card.last4s:
                continue
            store.save([Evidence(
                message_id=f"{e.message_id}#pdf", date=e.date, bank_id=e.bank_id, type=T.STATEMENT,
                last4s=card.last4s, products=card.products, product=card.product,
                dkim_pass=e.dkim_pass, forwarded=e.forwarded, to_user=e.to_user,
                name_match=extract.name_matches(card.holder, j.names()), used_body=True,
                rules_version=active_version())])
            j.c.execute(
                "INSERT INTO statement_facts (account_id, message_id, bank_id, last4, statement_date, total_due, "
                "minimum_due, credit_limit, available_limit, reward_points, reconciled) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (account_id, message_id) DO NOTHING",
                (j.account_id, e.message_id, e.bank_id, card.last4s[0], facts.statement_date, facts.total_due,
                 facts.minimum_due, facts.credit_limit, facts.available_limit, facts.reward_points, facts.reconciled))
            outcome = "read"
            read += 1
            break
        record(e.message_id, e.bank_id, outcome)
        if outcome in ("read", "password", "unreadable"):
            done_groups.add(key)
    j.rebuild()
    return read


def _pdf_secrets(j: Job) -> tuple[str | None, date | None]:
    """Name and date of birth for statement PDF passwords (the date of birth is stored sealed)."""
    dob = None
    if j.conn["dob_blob"]:
        dob = date.fromisoformat(j.vault.open(bytes(j.conn["dob_blob"]), f"dob:{j.account_id}").decode())
    return (j.names() or [None])[0], dob


def statements(j: Job) -> None:
    """Statement first, right after the quick scan: search statement emails with their PDF
    (last 12 months), read their headers only (statement bodies almost never carry the card
    number), skip those whose subject already ties them to a fully known card, and open the
    newest PDF of each remaining group (only with the user's consent to read statement PDFs)."""
    j.say("Looking for credit card statements")
    st = scan(j.gmail, j.registry, EvidenceStore(j.c, j.account_id), statement_plan(j.registry, date.today()),
              names=j.names(), allow_full_reads=False)
    read = 0
    # Statement emails are always re-read for what they show (product, file-name digits);
    # their PDFs are opened only with the user's consent.
    read = read_statement_gaps(j, *_pdf_secrets(j), pdfs=bool(j.conn["pdf_opt_in"] and config.READ_STATEMENT_PDFS))
    j.stats = {**st.as_dict(), "statement_pdfs_read": read}
    j.say(f"Checked {st.header_reads} statement emails; read {read} statement PDFs")


def pdf_enrich(j: Job) -> None:
    name, dob = _pdf_secrets(j)
    gaps_read = read_statement_gaps(j, name, dob)  # statement first: cards missing number or product
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
        rules = pdf.PATTERNS.get(bank, [])
        if rules and not any(pdf.can_open(r, bool(name), bool(dob), sorted(by_bank[bank])) for r in rules):
            continue  # locked with what we can't have (HSBC: 6 digits of the card): don't download it
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
                except Exception:  # a PDF the library can't parse
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
    j.stats = {"statement_gaps_read": gaps_read, "pdfs_read": added, "older_statements_skipped": skipped}
    j.say(f"Added details from {added} statement PDFs")


# ---------------------------------------------------------------- disconnect / delete

def disconnect(j: Job) -> None:
    """Revoke at Google, then delete the token and everything derived from Gmail.
    Runs as a worker job so the API never waits on Google."""
    revoked = False
    if j.conn:
        try:
            info = _token_info(j.conn["token_blob"])
            j.c.execute("INSERT INTO audit_log (account_id, actor, action) VALUES (%s, %s, 'read_token:revoke')",
                        (j.account_id, j.worker))
            r = httpx.post("https://oauth2.googleapis.com/revoke", data={"token": info.get("refresh_token")},
                           timeout=10)
            revoked = r.status_code == 200
        except Exception:
            revoked = False  # the token is deleted regardless
    with j.c.transaction():
        for t in ("evidence", "statement_facts", "notifications", "decisions", "card_index", "set_aside",
                  "statement_pdf_reads"):
            j.c.execute(f"DELETE FROM {t} WHERE account_id = %s", (j.account_id,))
        j.c.execute("DELETE FROM cards WHERE account_id = %s AND source = 'gmail'", (j.account_id,))
        j.c.execute("DELETE FROM gmail_connections WHERE account_id = %s", (j.account_id,))
        if j.row["payload"].get("delete_account"):
            j.c.execute("DELETE FROM accounts WHERE id = %s", (j.account_id,))  # cascades, this job row too
    j.stats = {"revoked": revoked}


HANDLERS = {"discover_quick": discover_quick, "discover_full": discover_full, "daily_sync": daily_sync,
            "pdf_enrich": pdf_enrich, "token_check": token_check, "statements": statements}
