"""Scan plans for each job and the budgeted scan loop around the POC engine.

Per §3.2/§3.3 of the architecture:
  quick   last 12 months, high-value families, ~40 header reads interleaved, no full reads
  full    the chosen years, every family, alerts for 12 months only, a tightened catch-all,
          full reads only when the header doesn't give the number or product
  sync    one search of bank senders since the last sync
"""

from __future__ import annotations

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
WINDOW_MONTHS = 6
ROUNDS_PER_WINDOW = 3
READS_PER_ROUND = 50
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


def windows(today: date, years: int) -> list[tuple[date, date]]:
    """[after, before) ranges of WINDOW_MONTHS, newest first, covering the lookback."""
    start, end, out = _years_ago(today, years), today + timedelta(days=1), []
    while end > start:
        w = max(start, _months_back(end, WINDOW_MONTHS))
        out.append((w, end))
        end = w
    return out


def window_plan(registry: Registry, after: date, before: date, today: date) -> list[Query]:
    chunks = _chunks(registry.all_domains())
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


class _Reader:
    """Header reads (full reads only when needed), saved per batch, within the budget.
    Also learns how each card's number is written in the mail it reads."""

    def __init__(self, gmail, registry, store, names, st: ScanStats, on_batch, t0):
        self.g, self.reg, self.store, self.names, self.st, self.on_batch, self.t0 = \
            gmail, registry, store, names, st, on_batch, t0
        self.addresses = gmail.user_addresses()
        self.known = store.known()
        self.tokens: dict[tuple[str, str], set[str]] = store.number_tokens()

    def _learn(self, m, ev) -> None:
        if ev.bank_id and ev.last4s:
            for l4, words in learn_number_tokens(m.text(), ev.last4s).items():
                self.tokens.setdefault((ev.bank_id, l4), set()).update(words)

    def _finish(self, metas_evs) -> None:
        batch = []
        for m, ev in metas_evs:
            self._learn(m, ev)
            m.body = None  # email text never outlives this step
            batch.append(ev)
            self.known[ev.message_id] = ev
            if self.st.first_card_s is None and ev.last4s and ev.type not in (T.PROMOTIONAL, T.OTP,
                                                                               T.NOT_CREDIT_CARD):
                self.st.first_card_s = round(time.monotonic() - self.t0, 2)
        self.store.save(batch)
        self.st.seconds = round(time.monotonic() - self.t0, 2)
        self.on_batch(self.st)

    def read(self, ids: list[str]) -> None:
        ids = [i for i in dict.fromkeys(ids) if i not in self.known]
        for start in range(0, len(ids), 50):
            chunk = ids[start:start + 50][:self.g.meter.affordable("metadata")]
            if not chunk:
                self.st.left_unread = len(ids) - start
                raise BudgetExhausted(f"{self.st.left_unread} emails left to read")
            metas = self.g.get_metadata(chunk)
            self.st.header_reads += len(metas)
            first = [(m, analyze(m, self.reg, self.addresses, self.names)) for m in metas]
            want = [m for m, ev in first if needs_body(ev, full_statements=False)][:self.g.meter.affordable("full")]
            if want:
                self.g.fill_bodies(want)
                self.st.full_reads += len(want)
            wanted = {m.message_id for m in want}
            self._finish([(m, analyze(m, self.reg, self.addresses, self.names) if m.message_id in wanted else ev)
                          for m, ev in first])

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
        ids = [i for i, ev in self.known.items() if not ev.used_body and needs_body(ev, full_statements=False)]
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


def card_aware_scan(gmail, registry: Registry, store: EvidenceStore, today: date, years: int, *,
                    names: list[str], on_batch: Callable[[ScanStats], None] = lambda s: None,
                    on_window: Callable[[date, int], None] = lambda d, n: None) -> ScanStats:
    from dataclasses import replace as _replace
    t0 = time.monotonic()
    st = ScanStats()
    r = _Reader(gmail, registry, store, names, st, on_batch, t0)
    r.reopen_skimmed()

    # 1. 6-month windows, newest first; each searched until no mail about unknown cards is left
    n_windows = n_rounds = 0
    for after, before in windows(today, years):
        n_windows += 1
        plan = window_plan(registry, after, before, today)
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
                break  # everything this window returned is read; newly found cards are excluded from the next one
        on_window(after, len(_known_cards(r.known.values())))
    st.per_family["windows"], st.per_family["rounds"] = n_windows, n_rounds

    # 2. per card: count, newest, oldest, newest usage, lifecycle, newest statement, statement PDFs
    start = _years_ago(today, years)
    tested = tuple(registry.test_senders)
    cards = list(_known_cards(r.known.values()))
    probes: list[tuple[tuple[str, str], Query]] = []
    for bank, l4 in cards:
        senders = tuple(registry.banks[bank].domains if bank in registry.banks else ()) + tested
        if not senders:
            continue
        num = (_number_group(bank, l4, r.tokens.get((bank, l4), ())),)
        probes += [((bank, l4), Query(family="card_all", senders=senders, after=start, extra=num)),
                   ((bank, l4), Query(family="card_usage", senders=senders, subject_any=USAGE_WORDS, after=start,
                                      max_results=1, extra=num)),
                   ((bank, l4), Query(family="card_life", senders=senders, subject_any=LIFECYCLE_WORDS,
                                      after=start, max_results=20, extra=num)),
                   ((bank, l4), Query(family="card_statement", senders=senders, subject_any=("statement",),
                                      after=start, max_results=1, extra=num)),
                   ((bank, l4), Query(family="card_pdfs", senders=senders, subject_any=("statement",), after=start,
                                      extra=num + ("has:attachment", "filename:pdf")))]
    found = _search_all(gmail, st, [q for _, q in probes])
    heads: list[str] = []
    newest_statements: list[str] = []
    index: dict[tuple[str, str], dict] = {}
    for ((card, q), (fam, ids)) in zip(probes, found):
        st.per_family[fam] = st.per_family.get(fam, 0) + len(ids)
        row = index.setdefault(card, {"email_count": 0, "pdf_query": None, "pdf_message_ids": []})
        if fam == "card_all":
            row["email_count"] = len(ids)
            heads += ids[:1] + ids[-1:]
        elif fam == "card_statement":
            newest_statements += ids
        elif fam == "card_pdfs":
            row["pdf_query"], row["pdf_message_ids"] = q.to_gmail(), ids
        else:
            heads += ids
    r.read(heads)
    r.read_full(newest_statements)  # add-on cards are named inside statements
    store.save_card_index({c: {**v, "number_tokens": sorted(r.tokens.get(c, ()))} for c, v in index.items()})
    st.per_family["cards_probed"] = len(cards)
    st.seconds = round(time.monotonic() - t0, 2)
    return st
