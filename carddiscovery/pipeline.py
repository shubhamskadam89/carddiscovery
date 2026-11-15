"""Discovery run: plan queries, fetch, analyse, resolve, score."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

from .classify import analyze, needs_body
from .ledger import Ledger
from .models import Application, Card, Evidence, EvidenceType as T
from .providers import MailProvider
from .queries import Query, is_full, plan, split
from .registry import Registry
from .resolve import resolve
from .score import score_all


@dataclass
class RunStats:
    queries: int = 0
    alert_windows: int = 0
    listed: int = 0
    from_cache: int = 0
    metadata_fetched: int = 0
    bodies_fetched: int = 0
    seconds: float = 0.0
    first_card_seconds: float | None = None
    per_family: Counter = field(default_factory=Counter)
    per_type: Counter = field(default_factory=Counter)
    dkim_pass: int = 0
    bank_messages: int = 0


@dataclass
class RunResult:
    mailbox: str
    cards: list[Card]
    applications: list[Application]
    stats: RunStats
    unknown_subjects: list[tuple[str, str]]  # (bank, subject) — shown on screen only, never stored


def discover(
    provider: MailProvider,
    registry: Registry,
    ledger: Ledger,
    user_names: list[str] | None = None,
    history_years: int = 15,
    max_bodies: int = 400,
    alerts_per_window: int = 40,
    progress: Callable[[str], None] = lambda s: None,
    collect_unknown: bool = False,
) -> RunResult:
    t0 = time.monotonic()
    stats = RunStats()
    mailbox = provider.mailbox()
    addresses = provider.user_addresses()
    known = ledger.known(mailbox)
    if not registry.test_senders:
        # Results from earlier test runs must not leak into a real run.
        known = {k: v for k, v in known.items() if not v.test}
    evidence: dict[str, Evidence] = dict(known)
    unknown_subjects: list[tuple[str, str]] = []

    def search_all(qs: list[Query]) -> list[list[str]]:
        stats.queries += len(qs)
        many = getattr(provider, "search_many", None)
        return many(qs) if many else [provider.search(q) for q in qs]

    # 1. List everything in as few round trips as possible.
    progress("searching")
    queries = plan(registry.all_domains(), date.today(), history_years=history_years,
                   alerts_per_window=alerts_per_window)
    found: list[tuple[str, list[str]]] = []
    level = list(zip(queries, search_all(queries)))
    while level:
        # A full alert window may be hiding more cards: split it and search the halves.
        to_split: list[Query] = []
        for q, ids in level:
            halves = split(q) if q.family == "alerts" and is_full(q, ids) else []
            if halves:
                to_split += halves
            else:
                found.append((q.family, ids))
                if q.family == "alerts":
                    stats.alert_windows += 1
        level = list(zip(to_split, search_all(to_split))) if to_split else []

    order: dict[str, str] = {}
    for family, ids in found:
        stats.listed += len(ids)
        stats.per_family[family] += len(ids)
        for i in ids:
            order.setdefault(i, family)
    stats.from_cache = sum(1 for i in order if i in known)
    new_ids = [i for i in order if i not in evidence]

    # 2. Fetch headers, then bodies only where needed, in batches.
    progress(f"reading {len(new_ids)} emails")
    getter = getattr(provider, "fill_bodies", None)
    for start in range(0, len(new_ids), 200):
        metas = provider.get_metadata(new_ids[start:start + 200])
        stats.metadata_fetched += len(metas)
        first = [(m, analyze(m, registry, addresses, user_names)) for m in metas]
        want = [m for m, ev in first if needs_body(ev)][: max(0, max_bodies - stats.bodies_fetched)]
        if want:
            if getter:
                getter(want)
            else:
                for m in want:
                    provider.fill_body(m)
            stats.bodies_fetched += len(want)
        wanted = {m.message_id for m in want}
        batch: list[Evidence] = []
        for m, ev in first:
            if m.message_id in wanted:
                ev = analyze(m, registry, addresses, user_names)
            if collect_unknown and ev.type == T.UNKNOWN and ev.bank_id:
                unknown_subjects.append((ev.bank_id, m.subject))
            m.body = None  # drop email text as soon as we're done with it
            batch.append(ev)
            evidence[ev.message_id] = ev
            if stats.first_card_seconds is None and ev.last4s and ev.type not in (T.PROMOTIONAL, T.OTP):
                stats.first_card_seconds = round(time.monotonic() - t0, 1)
        ledger.save(mailbox, batch)

    all_ev = list(evidence.values())
    for ev in all_ev:
        stats.per_type[ev.type.value] += 1
        if ev.bank_id:
            stats.bank_messages += 1
            stats.dkim_pass += ev.dkim_pass
    cards, apps = resolve(all_ev)
    score_all(cards, datetime.now(timezone.utc))
    answers = ledger.decisions(mailbox)
    for c in cards:
        c.decision = answers.get(c.decision_key)
    stats.seconds = round(time.monotonic() - t0, 1)
    return RunResult(mailbox, cards, apps, stats, unknown_subjects)
