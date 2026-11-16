"""Scan plans for each job and the budgeted scan loop around the POC engine.

Per §3.2/§3.3 of the architecture:
  quick   last 12 months, high-value families, ~40 header reads interleaved, no full reads
  full    the chosen years, every family, alerts for 12 months only, a tightened catch-all,
          full reads only when the header doesn't give the number or product
  sync    one search of bank senders since the last sync
"""

from __future__ import annotations

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
NOISE_SUBJECT = ("otp", "one time password", "debit", "savings", "pre-approved", "preapproved", "apply now",
                 "lifetime free", "introducing", "last chance", "instant approval")
# Families whose keywords ("upgrade", "reward", "points", "welcome") also appear in offers.
OFFER_PRONE = {"onboarding", "lifecycle", "application"}
PAYMENTS_SAMPLE = 40  # payment emails only confirm a card we already know; a sample is enough


def _chunks(items: list[str], n: int = 25) -> list[tuple[str, ...]]:
    return [tuple(items[i:i + n]) for i in range(0, len(items), n)]


def _years_ago(today: date, years: int) -> date:
    return date(today.year - years, today.month, 1)


def quick_plan(registry: Registry, today: date) -> list[Query]:
    start = _years_ago(today, config.QUICK_LOOKBACK_YEARS)
    return [Query(family=f, senders=c, subject_any=FAMILIES[f], exclude_subject=NOISE_SUBJECT, after=start,
                  max_results=config.QUICK_MAX_READS, extra=NO_PROMOS)
            for f in QUICK_FAMILIES for c in _chunks(registry.all_domains())]


def full_plan(registry: Registry, today: date, years: int) -> list[Query]:
    start = _years_ago(today, years)
    end = today + timedelta(days=1)  # Gmail's before: is exclusive
    chunks = _chunks(registry.all_domains())
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


def sync_plan(registry: Registry, since: date) -> list[Query]:
    return [Query(family="sync", senders=c, exclude_subject=NOISE_SUBJECT, after=since - timedelta(days=1),
                  extra=NO_PROMOS)
            for c in _chunks(registry.all_domains())]


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
