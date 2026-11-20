"""Scan plans for each job and the budgeted scan loop around the POC engine.

Per §3.2/§3.3 of the architecture:
  quick   last 12 months, high-value families, ~40 header reads interleaved, no full reads
  full    the chosen years, every family, alerts for 12 months only, a tightened catch-all,
          full reads only when the header doesn't give the number or product
  sync    one search of bank senders since the last sync
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable

from carddiscovery import EXTRACTOR_VERSION
from carddiscovery.classify import analyze, needs_body
from carddiscovery.models import Evidence, EvidenceType as T
from carddiscovery.queries import (CATCH_ALL_TEXT, FAMILIES, SAMPLED_FAMILY_SUBJECTS, Query, is_full,
                                   split)
from carddiscovery.registry import Registry

from . import config
from .gmailx import BudgetExhausted, MeteredGmail

QUICK_FAMILIES = ("statement", "onboarding", "lifecycle")
NO_PROMOS = ("-category:promotions",)
# Subject words that mark mail we never need to read: filtering them in the search
# itself is free, reading them costs 20 units each.
# OTP mail is searched: it often names the card ("OTP for transaction on your card XX5630"),
# which shows the card in use. Only the card number and date are kept, never the code.
NOISE_SUBJECT = ("debit", "savings", "pre-approved", "preapproved", "apply now",
                 "lifetime free", "introducing", "last chance", "instant approval")
# Offers in the Promotions tab that name "card" in the subject, last 12 months only: offers
# to existing cardholders ("EMI on your Credit Card XX7776") can be the only mail naming a
# card. Measured on a real inbox: about 2.5% of promotional mail names a card number, so
# reading all of it (about 14x a normal scan) isn't worth it; this slice costs about 4,000 units.
CARD_OFFER_SUBJECT = ("card",)
# Families whose keywords ("upgrade", "reward", "points", "welcome") also appear in offers.
OFFER_PRONE = {"onboarding", "lifecycle", "application"}
PAYMENTS_SAMPLE = 40  # payment emails only confirm a card we already know; a sample is enough


def _chunks(items: list[str], n: int = 25) -> list[tuple[str, ...]]:
    return [tuple(items[i:i + n]) for i in range(0, len(items), n)]


def _sender_groups(registry: Registry) -> list[tuple[str, ...]]:
    """`from:` groups for every search: the listed bank and app domains, plus one group for
    all of .bank.in, so a bank's new or unlisted .bank.in name (hsbc.bank.in, icici.bank.in)
    is searched too. Gmail matches subdomains, so "bank.in" covers every bank's mail there;
    which bank it is, and whether to trust it, is decided by Registry.bank_for_domain. Its own
    group, so it can't crowd the sampled alerts of the other groups."""
    return _chunks(registry.all_domains()) + [("bank.in",)]


def _years_ago(today: date, years: int) -> date:
    return date(today.year - years, today.month, 1)


def quick_plan(registry: Registry, today: date) -> list[Query]:
    start = _years_ago(today, config.QUICK_LOOKBACK_YEARS)
    return [Query(family=f, senders=c, subject_any=FAMILIES[f], exclude_subject=NOISE_SUBJECT, after=start,
                  max_results=config.QUICK_MAX_READS, extra=NO_PROMOS)
            for f in QUICK_FAMILIES for c in _sender_groups(registry)]


def full_plan(registry: Registry, today: date, years: int) -> list[Query]:
    start = _years_ago(today, years)
    end = today + timedelta(days=1)  # Gmail's before: is exclusive
    chunks = _sender_groups(registry)
    out = [Query(family=f, senders=c, subject_any=words, exclude_subject=NOISE_SUBJECT, after=start,
                 extra=NO_PROMOS if f in OFFER_PRONE else (),
                 max_results=PAYMENTS_SAMPLE if f == "payment" else None)
           for f, words in FAMILIES.items() for c in chunks]
    alert_start = _years_ago(today, config.FULL_ALERT_YEARS)
    out += [Query(family="alerts", senders=c, subject_any=SAMPLED_FAMILY_SUBJECTS, exclude_subject=NOISE_SUBJECT,
                  after=alert_start, before=end, max_results=40) for c in chunks]
    covered = tuple(w for f in FAMILIES.values() for w in f) + SAMPLED_FAMILY_SUBJECTS
    out += [Query(family="catch_all", senders=c, text_any=CATCH_ALL_TEXT, exclude_subject=NOISE_SUBJECT + covered,
                  after=start, max_results=config.FULL_CATCH_ALL_CAP, extra=NO_PROMOS) for c in chunks]
    return out


# Statement first: statement emails with their PDF, searched directly right after the quick scan.
# A statement proves a credit card exists, and its PDF names the bank, number and product at once.
# Gmail matches whole words, and "E-Statement" is the words "e statement": Yes Bank writes
# "YES_BANK_ACE Rupay Credit Card E-Statement", which the first two phrases miss.
STATEMENT_SUBJECTS = ("credit card statement", "card statement", "card e-statement", "card estatement",
                      "card monthly statement")  # SBI Card: "IRCTC SBI Platinum Card Monthly Statement"
STATEMENT_MONTHS = 12  # the cards in use; older and closed cards are left to the full scan


def statement_plan(registry: Registry, today: date, months: int = STATEMENT_MONTHS) -> list[Query]:
    after = today - timedelta(days=30 * months)
    return [Query(family="statements", senders=g, subject_any=STATEMENT_SUBJECTS, exclude_subject=NOISE_SUBJECT,
                  after=after, extra=("has:attachment",)) for g in _sender_groups(registry)]


def sync_plan(registry: Registry, since: date) -> list[Query]:
    chunks = _sender_groups(registry)
    return ([Query(family="sync", senders=c, exclude_subject=NOISE_SUBJECT, after=since - timedelta(days=1),
                   extra=NO_PROMOS) for c in chunks]
            + [Query(family="sync_offers", senders=c, subject_any=CARD_OFFER_SUBJECT, exclude_subject=NOISE_SUBJECT,
                     after=since - timedelta(days=1), extra=("category:promotions",)) for c in chunks])


# ---------------------------------------------------------------- evidence store

class EvidenceStore:
    """Postgres `evidence` rows for one account."""

    def __init__(self, conn, account_id: int):
        self.c = conn
        self.account_id = account_id

    def known(self) -> dict[str, Evidence]:
        rows = self.c.execute("SELECT message_id, data FROM evidence WHERE account_id = %s AND extractor_version = %s",
                              (self.account_id, EXTRACTOR_VERSION))
        return {r["message_id"]: Evidence.from_dict(r["data"]) for r in rows}

    def number_tokens(self) -> dict[tuple[str, str], set[str]]:
        rows = self.c.execute("SELECT bank_id, last4, number_tokens FROM card_index WHERE account_id = %s",
                              (self.account_id,))
        return {(r["bank_id"], r["last4"]): set(r["number_tokens"]) for r in rows}

    def save_card_index(self, cards: dict[tuple[str, str], dict]) -> None:
        with self.c.transaction(), self.c.cursor() as cur:
            cur.executemany(
                "INSERT INTO card_index (account_id, bank_id, last4, email_count, number_tokens, pdf_query, "
                "pdf_message_ids) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (account_id, bank_id, last4) "
                "DO UPDATE SET email_count = EXCLUDED.email_count, number_tokens = EXCLUDED.number_tokens, "
                "pdf_query = EXCLUDED.pdf_query, pdf_message_ids = EXCLUDED.pdf_message_ids, indexed_at = now()",
                [(self.account_id, b, l4, v["email_count"], v["number_tokens"], v["pdf_query"], v["pdf_message_ids"])
                 for (b, l4), v in cards.items()])

    def save(self, evs: Iterable[Evidence]) -> None:
        import json
        with self.c.transaction(), self.c.cursor() as cur:
            cur.executemany(
                "INSERT INTO evidence (account_id, message_id, extractor_version, data) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (account_id, message_id) DO UPDATE SET data = EXCLUDED.data, "
                "extractor_version = EXCLUDED.extractor_version",
                [(self.account_id, e.message_id, EXTRACTOR_VERSION, json.dumps(e.to_dict())) for e in evs])


# ---------------------------------------------------------------- the scan loop

@dataclass
class ScanStats:
    searches: int = 0
    listed: int = 0
    skipped_known: int = 0
    header_reads: int = 0
    full_reads: int = 0
    seconds: float = 0.0
    first_card_s: float | None = None
    left_unread: int = 0
    reopened: int = 0
    per_family: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return dict(vars(self))


def scan(gmail: MeteredGmail, registry: Registry, store: EvidenceStore, queries: list[Query], *,
         names: list[str], max_reads: int | None = None, allow_full_reads: bool = True,
         interleave: bool = False, on_batch: Callable[[ScanStats], None] = lambda s: None) -> ScanStats:
    """Search, then read headers (and full emails only when needed) within the meter's budget.

    Raises BudgetExhausted after saving everything read so far, when unread mail remains;
    a rerun skips what is saved, so the job resumes where it stopped.
    """
    t0 = time.monotonic()
    st = ScanStats()
    known = store.known()
    addresses = gmail.user_addresses()

    # 1. searches (alert windows that come back full are split, as in the POC)
    results: list[tuple[str, list[str]]] = []
    level = queries
    while level:
        st.searches += len(level)
        found = gmail.search_many(level)
        nxt: list[Query] = []
        for q, ids in zip(level, found):
            halves = split(q) if q.family == "alerts" and is_full(q, ids) else []
            if halves:
                nxt += halves
            else:
                results.append((q.family, ids))
        level = nxt

    # 2. order: plan order (high-value first), or round-robin across searches for a fast preview
    seen: set[str] = set()
    order: list[str] = []
    lists = [ids for _, ids in results]
    if interleave:
        for i in range(max((len(x) for x in lists), default=0)):
            for ids in lists:
                if i < len(ids) and ids[i] not in seen:
                    seen.add(ids[i]); order.append(ids[i])
    else:
        for ids in lists:
            for i in ids:
                if i not in seen:
                    seen.add(i); order.append(i)
    for fam, ids in results:
        st.per_family[fam] = st.per_family.get(fam, 0) + len(ids)
    st.listed = len(order)
    todo = [i for i in order if i not in known]
    st.skipped_known = len(order) - len(todo)
    if max_reads is not None:
        todo = todo[:max_reads]

    # 2b. emails an earlier job read only as headers (the quick scan never opens emails)
    # but whose header lacked the card number or product: open them now, highest value first.
    if allow_full_reads:
        upgrade = [i for i in order if i in known and not known[i].used_body
                   and needs_body(known[i], full_statements=False)]
        upgrade = upgrade[:gmail.meter.affordable("full")]
        for start in range(0, len(upgrade), 50):
            metas = gmail.get_full(upgrade[start:start + 50])
            st.full_reads += len(metas)
            batch = []
            for m in metas:
                batch.append(analyze(m, registry, addresses, names))
                m.body = None
            store.save(batch)
            st.reopened = st.reopened + len(metas)
            on_batch(st)

    # 3. reads, 50 per batch, within budget
    for start in range(0, len(todo), 50):
        can = gmail.meter.affordable("metadata")
        chunk = todo[start:start + min(50, can)]
        if not chunk:
            st.left_unread = len(todo) - start
            break
        metas = gmail.get_metadata(chunk)
        st.header_reads += len(metas)
        first = [(m, analyze(m, registry, addresses, names)) for m in metas]
        want = []
        if allow_full_reads:
            want = [m for m, ev in first if needs_body(ev, full_statements=False)]
            want = want[:gmail.meter.affordable("full")]
            if want:
                gmail.fill_bodies(want)
                st.full_reads += len(want)
        wanted = {m.message_id for m in want}
        batch = []
        for m, ev in first:
            if m.message_id in wanted:
                ev = analyze(m, registry, addresses, names)
            m.body = None  # email text never outlives this loop
            batch.append(ev)
            if st.first_card_s is None and ev.last4s and ev.type not in (T.PROMOTIONAL, T.OTP, T.NOT_CREDIT_CARD):
                st.first_card_s = round(time.monotonic() - t0, 2)
        store.save(batch)
        st.seconds = round(time.monotonic() - t0, 2)
        on_batch(st)
        if len(chunk) < min(50, len(todo) - start):
            st.left_unread = len(todo) - start - len(chunk)
            break
    st.seconds = round(time.monotonic() - t0, 2)
    if st.left_unread:
        raise BudgetExhausted(f"{st.left_unread} emails left to read")
    return st


def since_date(last: datetime | None) -> date:
    return (last or datetime.now(timezone.utc) - timedelta(days=1)).date()


# ---------------------------------------------------------------- card-aware full scan
#
# Searches cost 5 units and reads 20, so once a card is known, its history is found
# with searches instead of reading every email about it.
#
#   1. Windows of 6 months, newest first. In each, the usual searches run with every
#      known card's number excluded, so only mail about cards not seen yet comes back;
#      it is read, new cards join the exclusion list, and the window is searched again
#      until nothing new is left. Recent windows find the cards in use; older windows
#      only surface cards that went quiet (dormant, closed, replaced).
#   2. Per card, a few searches by its number: how many emails mention it, its newest
#      and oldest email, newest usage email, lifecycle emails, newest statement (opened
#      in full, which is where add-on cards are named) and its statement-PDF emails,
#      which are indexed for later but not downloaded.
#
# Gmail matches whole words, case-insensitively: "2846" matches "ending 2846" and
# "XXXX XXXX XXXX 2846" but not "XX2846", so each number is searched in its masked
# forms too, plus every form actually seen in that card's mail ("4375XXXXXXXX2846").
# A form we miss costs extra reads, never a missed card.

MASK_PREFIXES = ("", "XX", "XXXX", "XXXXXXXX", "XXXXXXXXXXXX")
LIFECYCLE_WORDS = ("annual fee", "upgraded", "upgrade", "replacement", "replaced", "closure", "closed",
                   "inactive", "not been used", "reissue", "renewal", "welcome", "dispatched", "activated")
USAGE_WORDS = ("statement", "spent", "transaction", "payment", "alert")
# Window sizes in months, newest first; the last size repeats. "6" = fixed 6-month windows,
# "1,2,3,6,12" = start small (cards in use show up fastest) and grow as older mail thins out.
WINDOW_SCHEDULE = tuple(int(x) for x in os.environ.get("CD_WINDOWS", "3").split(","))
ROUNDS_PER_WINDOW = int(os.environ.get("CD_ROUNDS", "3"))
READS_PER_ROUND = int(os.environ.get("CD_READS_PER_ROUND", "50"))
WINDOW_CATCH_ALL_CAP = 100
_WORD = re.compile(r"[A-Za-z0-9]+")


def number_terms(bank_id: str, l4: str, learned: Iterable[str] = ()) -> list[str]:
    terms = [p + l4 for p in MASK_PREFIXES]
    if bank_id == "amex":  # Amex prints 5 digits ("ending 41005"); we only know the last 4
        terms += [f"{d}{l4}" for d in range(10)]
    seen = {t.lower() for t in terms}
    return terms + [t for t in learned if t.lower() not in seen]


def _number_group(bank_id: str, l4: str, learned: Iterable[str] = ()) -> str:
    return "(" + " OR ".join(number_terms(bank_id, l4, learned)) + ")"


def learn_number_tokens(text: str, l4s: list[str]) -> dict[str, set[str]]:
    """The exact words a bank used for each card number, e.g. "XX2846" or "4375XXXXXXXX2846".
    Masked words only: an all-digit word longer than 5 would be a full card number."""
    out: dict[str, set[str]] = {}
    for w in _WORD.findall(text or ""):
        for l4 in l4s:
            if w.endswith(l4) and len(w) > len(l4) and not (w.isdigit() and len(w) > 5):
                out.setdefault(l4, set()).add(w.upper())
    return out


def _known_cards(evs) -> dict[tuple[str, str], None]:
    from carddiscovery.models import CARD_EVIDENCE_TYPES
    out: dict[tuple[str, str], None] = {}
    for ev in evs:
        if ev.bank_id and ev.type in CARD_EVIDENCE_TYPES:
            for l4 in ev.last4s:
                out[(ev.bank_id, l4)] = None
    return out


def _months_back(d: date, months: int) -> date:
    m = d.year * 12 + d.month - 1 - months
    return date(m // 12, m % 12 + 1, min(d.day, 28))


def windows(today: date, years: int, schedule: tuple[int, ...] | None = None) -> list[tuple[date, date]]:
    """[after, before) ranges, newest first, covering the lookback, sized by the schedule."""
    schedule = schedule or WINDOW_SCHEDULE
    start, end, out = _years_ago(today, years), today + timedelta(days=1), []
    while end > start:
        months = schedule[min(len(out), len(schedule) - 1)]
        w = max(start, _months_back(end, months))
        out.append((w, end))
        end = w
    return out


def window_plan(registry: Registry, after: date, before: date, today: date) -> list[Query]:
    chunks = _sender_groups(registry)
    out = [Query(family=f, senders=c, subject_any=words, exclude_subject=NOISE_SUBJECT, after=after, before=before,
                 extra=NO_PROMOS if f in OFFER_PRONE else (),
                 max_results=PAYMENTS_SAMPLE if f == "payment" else None)
           for f, words in FAMILIES.items() for c in chunks]
    alert_start = _years_ago(today, config.FULL_ALERT_YEARS)
    if before > alert_start:
        out += [Query(family="alerts", senders=c, subject_any=SAMPLED_FAMILY_SUBJECTS, exclude_subject=NOISE_SUBJECT,
                      after=max(after, alert_start), before=before, max_results=40) for c in chunks]
    covered = tuple(w for f in FAMILIES.values() for w in f) + SAMPLED_FAMILY_SUBJECTS
    out += [Query(family="catch_all", senders=c, text_any=CATCH_ALL_TEXT, exclude_subject=NOISE_SUBJECT + covered,
                  after=after, before=before, max_results=WINDOW_CATCH_ALL_CAP, extra=NO_PROMOS) for c in chunks]
    return out


COMBINED_WINDOWS = os.environ.get("CD_COMBINED", "1") != "0"


def _q(term: str) -> str:
    return f'"{term}"' if " " in term else term


def combined_window_plan(registry: Registry, after: date, before: date, today: date) -> list[Query]:
    """The window's searches merged into 2 per sender group (plus alerts in the last 12 months).
    With every known card excluded, little comes back, so per-type searches aren't needed:
      - statements, payments, applications (not filtered by the Promotions tab)
      - onboarding, lifecycle and the catch-all text, outside the Promotions tab"""
    chunks = _sender_groups(registry)
    core = FAMILIES["statement"] + FAMILIES["payment"] + FAMILIES["application"]
    offer_prone = FAMILIES["onboarding"] + FAMILIES["lifecycle"]
    other = ("(subject:(" + " OR ".join(_q(w) for w in offer_prone) + ") OR "
             + " OR ".join(_q(t) for t in CATCH_ALL_TEXT) + ")")
    out = []
    for c in chunks:
        out.append(Query(family="core", senders=c, subject_any=core, exclude_subject=NOISE_SUBJECT,
                         after=after, before=before))
        out.append(Query(family="other", senders=c, exclude_subject=NOISE_SUBJECT, after=after, before=before,
                         extra=NO_PROMOS + (other,)))
    alert_start = _years_ago(today, config.FULL_ALERT_YEARS)
    if before > alert_start:
        out += [Query(family="alerts", senders=c, subject_any=SAMPLED_FAMILY_SUBJECTS, exclude_subject=NOISE_SUBJECT,
                      after=max(after, alert_start), before=before, max_results=40) for c in chunks]
        out += [Query(family="card_offers", senders=c, subject_any=CARD_OFFER_SUBJECT, exclude_subject=NOISE_SUBJECT,
                      after=max(after, alert_start), before=before, extra=("category:promotions",)) for c in chunks]
    return out


class _Reader:
    """Header reads (full reads only when needed), saved per batch, within the budget.
    Also learns how each card's number is written in the mail it reads."""

    def __init__(self, gmail, registry, store, names, st: ScanStats, on_batch, t0):
        self.g, self.reg, self.store, self.names, self.st, self.on_batch, self.t0 = \
            gmail, registry, store, names, st, on_batch, t0
        self.addresses = gmail.user_addresses()
        self.known = store.known()
        self.tokens: dict[tuple[str, str], set[str]] = store.number_tokens()
        # Subjects read so far (lower case, digit-free ones only), for skipping repeats.
        self.subjects: dict[str, int] = {}

    def _learn(self, m, ev) -> None:
        if ev.bank_id and ev.last4s:
            for l4, words in learn_number_tokens(m.text(), ev.last4s).items():
                self.tokens.setdefault((ev.bank_id, l4), set()).update(words)

    def _finish(self, metas_evs) -> None:
        batch = []
        for m, ev in metas_evs:
            self._learn(m, ev)
            subj = " ".join((m.subject or "").lower().split())
            if ev.bank_id and subj and not re.search(r"[\d\"]", subj):
                self.subjects[subj] = self.subjects.get(subj, 0) + 1
            m.body = None  # email text never outlives this step
            batch.append(ev)
            self.known[ev.message_id] = ev
            if self.st.first_card_s is None and ev.last4s and ev.type not in (T.PROMOTIONAL, T.OTP,
                                                                               T.NOT_CREDIT_CARD):
                self.st.first_card_s = round(time.monotonic() - self.t0, 2)
        self.store.save(batch)
        self.st.seconds = round(time.monotonic() - self.t0, 2)
        self.on_batch(self.st)

    def read(self, ids: list[str], full: bool = True) -> None:
        """Header reads; with full=True, also full reads of emails whose header lacks the
        number or product. full=False reads subjects and previews only."""
        ids = [i for i in dict.fromkeys(ids) if i not in self.known]
        for start in range(0, len(ids), 50):
            chunk = ids[start:start + 50][:self.g.meter.affordable("metadata")]
            if not chunk:
                self.st.left_unread = len(ids) - start
                raise BudgetExhausted(f"{self.st.left_unread} emails left to read")
            metas = self.g.get_metadata(chunk)
            self.st.header_reads += len(metas)
            first = [(m, analyze(m, self.reg, self.addresses, self.names)) for m in metas]
            want = [m for m, ev in first if full and needs_body(ev, full_statements=False)
                    and not self._partial_known(ev)]
            want = want[:self.g.meter.affordable("full")]
            if want:
                self.g.fill_bodies(want)
                self.st.full_reads += len(want)
            wanted = {m.message_id for m in want}
            self._finish([(m, analyze(m, self.reg, self.addresses, self.names) if m.message_id in wanted else ev)
                          for m, ev in first])

    def _partial_known(self, ev) -> bool:
        """The header shows only the last 2-3 digits ("ending XX87") and exactly one card
        already found fits them. Axis statements carry no fuller number in the body either,
        so a 60-unit full read would learn nothing."""
        if not ev.last_digits or not ev.bank_id:
            return False
        fits = {l4 for e in self.known.values() if e.bank_id == ev.bank_id
                and e.type not in (T.PROMOTIONAL, T.OTP, T.NOT_CREDIT_CARD) for l4 in e.last4s
                if l4.endswith(ev.last_digits)}
        return len(fits) == 1

    def read_full(self, ids: list[str]) -> None:
        """Open in full (60 units): for emails whose body matters, e.g. the newest statement."""
        ids = [i for i in dict.fromkeys(ids) if not (i in self.known and self.known[i].used_body)]
        ids = ids[:self.g.meter.affordable("full")]
        for start in range(0, len(ids), 50):
            metas = self.g.get_full(ids[start:start + 50])
            self.st.full_reads += len(metas)
            self._finish([(m, analyze(m, self.reg, self.addresses, self.names)) for m in metas])

    def reopen_skimmed(self) -> None:
        """Emails the quick scan read as headers only, whose header lacked the number or product."""
        ids = [i for i, ev in self.known.items()
               if not ev.used_body and needs_body(ev, full_statements=False) and not self._partial_known(ev)]
        n = len(ids)
        self.read_full(ids)
        self.st.reopened += n


def _search_all(gmail, st: ScanStats, queries: list[Query]) -> list[tuple[str, list[str]]]:
    results, level = [], queries
    while level:
        st.searches += len(level)
        nxt = []
        for q, ids in zip(level, gmail.search_many(level)):
            halves = split(q) if q.family == "alerts" and is_full(q, ids) else []
            if halves:
                nxt += halves
            else:
                results.append((q.family, ids))
        level = nxt
    return results


def _probe_cards(gmail, registry: Registry, st: ScanStats, r: "_Reader", cards: list[tuple[str, str]],
                 after: date, before: date, index: dict) -> None:
    """For each card, a few searches by its number over [after, before): count, newest and oldest
    email, newest usage, lifecycle emails, newest statement (opened in full: add-on cards are
    named there) and its statement-PDF emails (indexed, not downloaded)."""
    tested = tuple(registry.test_senders)
    probes: list[tuple[tuple[str, str], Query]] = []
    for bank, l4 in cards:
        senders = tuple(registry.banks[bank].domains if bank in registry.banks else ()) + tested
        if not senders:
            continue
        num = (_number_group(bank, l4, r.tokens.get((bank, l4), ())),)
        rng = dict(after=after, before=before)
        probes += [((bank, l4), Query(family="card_all", senders=senders, extra=num, **rng)),
                   ((bank, l4), Query(family="card_usage", senders=senders, subject_any=USAGE_WORDS, max_results=1,
                                      extra=num, **rng)),
                   ((bank, l4), Query(family="card_life", senders=senders, subject_any=LIFECYCLE_WORDS,
                                      max_results=20, extra=num, **rng)),
                   ((bank, l4), Query(family="card_statement", senders=senders, subject_any=("statement",),
                                      max_results=1, extra=num, **rng)),
                   ((bank, l4), Query(family="card_pdfs", senders=senders, subject_any=("statement",),
                                      extra=num + ("has:attachment", "filename:pdf"), **rng))]
    if not probes:
        return
    heads: list[str] = []
    statements: list[str] = []
    for ((card, q), (fam, ids)) in zip(probes, _search_all(gmail, st, [q for _, q in probes])):
        st.per_family[fam] = st.per_family.get(fam, 0) + len(ids)
        row = index.setdefault(card, {"email_count": 0, "pdf_query": None, "pdf_message_ids": []})
        if fam == "card_all":
            row["email_count"] = len(ids)
            heads += ids[:1] + ids[-1:]
        elif fam == "card_statement":
            statements += ids
        elif fam == "card_pdfs":
            row["pdf_query"], row["pdf_message_ids"] = q.to_gmail(), ids
        else:
            heads += ids
    r.read(heads)
    r.read_full(statements)


# ---------------------------------------------------------------- subjects first (CD_SCAN_MODE=subjects)

SUBJECT_SCAN_TEXT = ("credit card", "card statement", "card e-statement", "card estatement")
SUBJECT_ROUNDS = 6  # rounds per year: each re-searches with the cards found so far excluded


def year_windows(today: date, years: int) -> list[tuple[date, date]]:
    """The last 12 months first, then each year before it, newest first."""
    end = today + timedelta(days=1)  # Gmail's before: is exclusive
    out = [(today - timedelta(days=365), end)]
    for k in range(1, years):
        out.append((today - timedelta(days=365 * (k + 1)), today - timedelta(days=365 * k)))
    return out


def subject_scan(gmail, registry: Registry, store: EvidenceStore, today: date, years: int, *,
                 names: list[str], on_batch: Callable[[ScanStats], None] = lambda s: None,
                 on_window: Callable[[date, int], None] = lambda d, n: None,
                 after_first_year: Callable[[], None] = lambda: None) -> ScanStats:
    """Subjects first, one year at a time, newest first: search every bank and app sender for
    mail mentioning a credit card or a card statement (with or without an attachment, outside
    the Promotions tab), with every card already found excluded by its number, and read the
    subjects and previews only (20 units each; no full reads). After the first year,
    `after_first_year` runs (the statement step: PDFs only for cards found but incomplete);
    older years are subjects only, and the caller runs the statement step again at the end."""
    t0 = time.monotonic()
    st = ScanStats()
    r = _Reader(gmail, registry, store, names, st, on_batch, t0)
    for n, (after, before) in enumerate(year_windows(today, years)):
        for _ in range(SUBJECT_ROUNDS):
            cards = _known_cards(r.known.values())
            excl = tuple(f"-{t}" for b, l4 in cards for t in number_terms(b, l4, r.tokens.get((b, l4), ())))
            plan = [Query(family="credit_card", senders=g, text_any=SUBJECT_SCAN_TEXT, exclude_subject=NOISE_SUBJECT,
                          after=after, before=before, extra=NO_PROMOS + excl) for g in _sender_groups(registry)]
            results = _search_all(gmail, st, plan)
            for fam, ids in results:
                st.per_family[fam] = st.per_family.get(fam, 0) + len(ids)
            todo = list(dict.fromkeys(i for _, ids in results for i in ids if i not in r.known))
            st.listed += len(todo)
            if not todo:
                break
            r.read(todo[:READS_PER_ROUND], full=False)
            if len(todo) <= READS_PER_ROUND:
                break
        on_window(after, len(_known_cards(r.known.values())))
        if n == 0:
            after_first_year()
    st.seconds = round(time.monotonic() - t0, 2)
    return st


# ---------------------------------------------------------------- monthly (CD_SCAN_MODE=monthly)

# Subjects with the word "card" (Gmail matches whole words): "credit card" alone missed SBI's
# "Transaction Alert from BPCL Octane Card", CRED's "statement for your IDFC First Bank card
# XXXX-7846" and "Your SBI Card Account Deactivation". Debit and savings mail stay out
# (NOISE_SUBJECT), and so do the Promotions tab and every card already found by its number.
MONTHLY_SUBJECTS = ("card",)
# A subject read this many times (an alert template: "Transaction Alert from BPCL Octane Card")
# is left out of later months' searches, which is free, instead of read every month at 20
# units each. Every RECHECK_EVERY months it's searched again, so a new card on the same
# template (a replacement, an add-on) still shows up.
REPEAT_SUBJECT_LIMIT = 2
# OTP emails only ever confirm a card other mail already shows (SBI sends one per purchase), so
# the month-by-month search leaves them out: about a fifth of a heavy card user's card mail.
MONTHLY_EXCLUDE = ("otp", "one time password")
RECHECK_EVERY = 6
MAX_SUBJECT_EXCLUSIONS = 15
# The lifecycle pass after the main scan: dormant and closed cards often send only these.
LIFECYCLE_SUBJECTS = FAMILIES["onboarding"] + FAMILIES["lifecycle"]
MONTH_DAYS = 30


RECENT_MONTHS = 12   # one month at a time this far back: cards in use show up month by month
OLDER_WINDOW_DAYS = 90  # older mail is sparse: three months per window, a third of the searches


def month_windows(today: date, years: int, recent: int | None = None) -> list[tuple[date, date]]:
    """Windows newest first, back `years` years, ends exclusive: 30 days each for the last
    `recent` months, then 90 days each (a window that would cross the start is cut there)."""
    recent = RECENT_MONTHS if recent is None else recent
    start = today - timedelta(days=365 * years)
    out, before = [], today + timedelta(days=1)
    while before > start:
        size = MONTH_DAYS if len(out) < recent else OLDER_WINDOW_DAYS
        after = max(start, (today if not out else before) - timedelta(days=size))
        out.append((after, before))
        before = after
    return out


def windowed_subject_scan(gmail, registry: Registry, store: EvidenceStore,
                          windows: list[tuple[date, date]], subjects: tuple[str, ...], *, names: list[str],
                          on_batch: Callable[[ScanStats], None] = lambda s: None,
                          on_window: Callable[[date, date], None] = lambda a, b: None,
                          family: str = "monthly") -> ScanStats:
    """Window by window, newest first: search every bank and app sender for these subjects,
    outside the Promotions tab, with every card already found excluded by its number, and read
    subjects and previews only (20 units each, no full reads). After each window `on_window`
    runs, so the cards found so far can be shown and acted on."""
    t0 = time.monotonic()
    st = ScanStats()
    r = _Reader(gmail, registry, store, names, st, on_batch, t0)
    for w, (after, before) in enumerate(windows):
        for _ in range(SUBJECT_ROUNDS):
            cards = _known_cards(r.known.values())
            excl = tuple(f"-{t}" for b, l4 in cards for t in number_terms(b, l4, r.tokens.get((b, l4), ())))
            repeats = () if w % RECHECK_EVERY == RECHECK_EVERY - 1 else tuple(
                s for s, n in sorted(r.subjects.items(), key=lambda x: -x[1])
                if n >= REPEAT_SUBJECT_LIMIT)[:MAX_SUBJECT_EXCLUSIONS]
            st.per_family["subjects_skipped"] = len(repeats)
            skip = NOISE_SUBJECT + (MONTHLY_EXCLUDE if family == "monthly" else ()) + repeats
            plan = [Query(family=family, senders=g, subject_any=subjects, exclude_subject=skip,
                          after=after, before=before, extra=NO_PROMOS + excl) for g in _sender_groups(registry)]
            results = _search_all(gmail, st, plan)
            for fam, ids in results:
                st.per_family[fam] = st.per_family.get(fam, 0) + len(ids)
            todo = list(dict.fromkeys(i for _, ids in results for i in ids if i not in r.known))
            st.listed += len(todo)
            if not todo:
                break
            r.read(todo[:READS_PER_ROUND], full=False)
            if len(todo) <= READS_PER_ROUND:
                break
        on_window(after, before)
    st.seconds = round(time.monotonic() - t0, 2)
    return st


def card_aware_scan(gmail, registry: Registry, store: EvidenceStore, today: date, years: int, *,
                    names: list[str], on_batch: Callable[[ScanStats], None] = lambda s: None,
                    on_window: Callable[[date, int], None] = lambda d, n: None) -> ScanStats:
    """6-month windows, newest first. In each: search with every known card's number excluded and
    read what comes back (mail about cards not seen yet); then look up each card found in this
    window by its number over this window and everything older (its full history), at once.

    Window 1 (last 6 months) finds the cards in use and fetches their 4.5 years of history; window 2
    finds only cards silent for the last 6 months and fetches their 4 years; and so on. A card is
    complete (status, first and last seen, add-ons) as soon as it is found, so a scan that pauses
    for budget leaves finished cards, not half-read ones."""
    from dataclasses import replace as _replace
    t0 = time.monotonic()
    st = ScanStats()
    r = _Reader(gmail, registry, store, names, st, on_batch, t0)
    r.reopen_skimmed()
    start = _years_ago(today, years)
    index: dict[tuple[str, str], dict] = {}
    probed: set[tuple[str, str]] = set()
    n_windows = n_rounds = 0
    for after, before in windows(today, years):
        n_windows += 1
        plan = (combined_window_plan if COMBINED_WINDOWS else window_plan)(registry, after, before, today)
        for _ in range(ROUNDS_PER_WINDOW):
            cards = _known_cards(r.known.values())
            excl = tuple(f"-{t}" for b, l4 in cards for t in number_terms(b, l4, r.tokens.get((b, l4), ())))
            results = _search_all(gmail, st, [_replace(q, extra=q.extra + excl) for q in plan])
            n_rounds += 1
            lists = [[i for i in ids if i not in r.known] for _, ids in results]
            for fam, ids in results:
                st.per_family[fam] = st.per_family.get(fam, 0) + len(ids)
            todo: list[str] = []
            for i in range(max((len(x) for x in lists), default=0)):  # round-robin: every family gets a turn
                todo += [x[i] for x in lists if i < len(x)]
            todo = list(dict.fromkeys(todo))
            st.listed += len(todo)
            if not todo:
                break
            r.read(todo[:READS_PER_ROUND])
            if len(todo) <= READS_PER_ROUND:
                break  # everything this window returned is read
        # Cards first seen in this window (or found inside a statement just opened, like add-ons):
        # their whole history lies in this window and older ones.
        for _ in range(3):
            new = [c for c in _known_cards(r.known.values()) if c not in probed]
            if not new:
                break
            probed.update(new)
            _probe_cards(gmail, registry, st, r, new, start, before, index)
        store.save_card_index({c: {**v, "number_tokens": sorted(r.tokens.get(c, ()))} for c, v in index.items()})
        on_window(after, len(probed))
    st.per_family.update(windows=n_windows, rounds=n_rounds, cards_probed=len(probed))
    st.seconds = round(time.monotonic() - t0, 2)
    return st


# ---------------------------------------------------------------- half-known cards
# Cards known by only one half: a number with no name (alerts, payments: "card ending 9719")
# or a name with no number (statements: "HSBC LIVE+ CREDIT CARD", PDF locked). The other
# half is often only in mail the subject scan skips, offers in the Promotions tab included:
# "spends on your HSBC Visa Platinum Credit Card ending with 9719". One search per card, all
# tabs, then whole emails, newest first, until one names both; at most HALF_READS per card.
HALF_READS = int(os.environ.get("CD_HALF_READS", "3"))
HALF_UNTRUSTED = (T.PROMOTIONAL, T.OTP, T.NOT_CREDIT_CARD)


def half_cards(evs) -> list[tuple[str, str, str]]:
    """(bank, "name" | "number", last4 or product): the half each such card is missing."""
    from carddiscovery.resolve import resolve_full
    out = []
    for c in resolve_full(list(evs)).cards:
        if c.last4 and not c.product_history:
            out.append((c.bank_id, "name", c.last4))
        elif not c.last4 and c.product:
            out.append((c.bank_id, "number", c.product))
    return out


def _same_line(a: str, b: str) -> bool:
    from carddiscovery.registry import _extends
    return _extends(a, b) or _extends(b, a)


def _completes(ev: Evidence, want: str, value: str) -> bool:
    if ev.type in HALF_UNTRUSTED or (ev.type == T.OFFER_EXISTING_CARD and not ev.held):
        return False
    if want == "name":
        return value in ev.last4s and bool(ev.product)
    return bool(ev.last4s) and bool(ev.product) and (ev.product == value or _same_line(ev.product, value))


def half_card_lookup(gmail, registry: Registry, store: EvidenceStore, start: date, *, names: list[str],
                     on_batch: Callable[[ScanStats], None] = lambda s: None,
                     on_card: Callable[..., None] = lambda *a: None) -> ScanStats:
    t0 = time.monotonic()
    st = ScanStats()
    todo = half_cards(store.known().values())
    if not todo:
        return st
    r = _Reader(gmail, registry, store, names, st, on_batch, t0)
    for i, (bank_id, want, value) in enumerate(todo):
        bank = registry.banks.get(bank_id)
        # What one find settles can settle others too: once ••9719 is the Visa Platinum, the
        # bank's one other number and one other name ("Live+", ••3408) pair up by themselves.
        # Re-resolving costs no quota; a search and reads do.
        found = (bank_id, want, value) not in half_cards(r.known.values())
        if not found and bank and bank.domains:
            if want == "name":  # the number is known: mail naming it, in any tab
                q = Query(family="half_card", senders=tuple(bank.domains), after=start,
                          text_any=tuple(number_terms(bank_id, value, r.tokens.get((bank_id, value), ()))),
                          extra=('"credit card"',))
            else:  # the name is known: mail naming it next to a card ending
                # Its words, not the phrase: SBI writes "IRCTC SBI Platinum Card" for "IRCTC Platinum".
                # No "ending" required: HDFC statements show the number only as the PDF's name
                # ("4893XXXXXXXXXX90_...pdf"), which a full read brings in.
                words = tuple(w for w in re.findall(r"[A-Za-z0-9]+", value) if len(w) > 1) or (value,)
                q = Query(family="half_card", senders=tuple(bank.domains), after=start, extra=words + ("card",))
            ids = [m for _, got in _search_all(gmail, st, [q]) for m in got]
            # Not yet read first (offers, mostly), then mail read by its header only.
            fresh = [m for m in ids if m not in r.known]
            skimmed = [m for m in ids if m in r.known and not r.known[m].used_body]
            for mid in (fresh + skimmed)[:HALF_READS]:
                if r.g.meter.affordable("full") < 1:
                    break
                r.read_full([mid])  # 60 units: the whole email
                if mid in r.known and _completes(r.known[mid], want, value):
                    found = True
                    if want == "name":
                        break
                    # A name can stand for two numbers (a card and its add-on: SBI sends
                    # "Transaction Alert from BPCL Octane Card" for ••8022 and ••8030 alike),
                    # so a name lookup reads all its sample.
        on_card(i, len(todo), bank_id, want, value, found)
    st.seconds = round(time.monotonic() - t0, 2)
    return st
