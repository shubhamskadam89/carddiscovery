"""Offline dry run: the real scan code over the generated emails held in memory.

Only for checking the generator and the engine before writing 10,000 emails into a
real inbox. Search here is a simple approximation of Gmail's (sender, subject words,
dates, Promotions tab); the real test is the import into Gmail.
"""

from __future__ import annotations

import re
from datetime import date
from email.utils import parseaddr

from carddiscovery.models import MessageMeta
from carddiscovery.providers.gmail import html_to_text, tidy
from carddiscovery.queries import Query

from ..gmailx import BudgetExhausted, Meter
from .generate import Mail


def _meta(m: Mail, to: str, with_body: bool) -> MessageMeta:
    body = tidy(html_to_text(m.html)) if m.html else tidy(m.text)
    auth = ""
    if m.auth:
        verdict, dom = m.auth.split(":", 1)
        auth = f"mx.google.com; dkim={verdict} header.i=@{dom}; dmarc={verdict} header.from={dom}"
    return MessageMeta(message_id=m.key, date=m.when, sender=m.sender, subject=m.subject, snippet=body[:200],
                       to=to, auth_results=auth, list_unsubscribe=m.unsubscribe, labels=tuple(m.labels),
                       body=body if with_body else None,
                       attachment_names=tuple(n for n, _ in m.attachments) if with_body else ())


class LocalBox:
    def __init__(self, mails: list[Mail], to: str, meter: Meter):
        self.mails = sorted(mails, key=lambda m: m.when, reverse=True)
        self.by_key = {m.key: m for m in mails}
        self.to = to
        self.meter = meter

    def mailbox(self) -> str:
        return self.to

    def user_addresses(self) -> list[str]:
        self.meter.charge("profile", 2)
        return [self.to]

    def _match(self, m: Mail, q: Query) -> bool:
        addr = parseaddr(m.sender)[1].lower()
        dom = addr.split("@")[-1]
        if not any(s.lower() == addr or dom == s.lower() or dom.endswith("." + s.lower()) for s in q.senders):
            return False
        d = m.when.date()
        if (q.after and d < q.after) or (q.before and d >= q.before):
            return False
        subj = m.subject.lower()
        words = lambda t, hay: re.search(r"\b" + re.escape(t.lower()) + r"\b", hay) is not None
        if q.subject_any and not any(words(t, subj) for t in q.subject_any):
            return False
        if q.text_any and not any(words(t, (m.subject + " " + m.text).lower()) for t in q.text_any):
            return False
        if any(words(t, subj) for t in q.exclude_subject):
            return False
        for e in q.extra:
            if e == "-category:promotions" and "CATEGORY_PROMOTIONS" in m.labels:
                return False
            if e in ("has:attachment", "filename:pdf") and not m.attachments:
                return False
        return True

    def search(self, q: Query) -> list[str]:
        ids = [m.key for m in self.mails if self._match(m, q)]
        pages = max(1, -(-len(ids) // 500))
        if q.max_results:
            ids, pages = ids[:q.max_results], 1
        self.meter.charge("search", pages)
        return ids

    def search_many(self, queries: list[Query]) -> list[list[str]]:
        return [self.search(q) for q in queries]

    def get_metadata(self, ids):
        ids = list(ids)
        self.meter.charge("metadata", len(ids))
        return [_meta(self.by_key[i], self.to, False) for i in ids]

    def fill_bodies(self, msgs):
        self.meter.charge("full", len(msgs))
        for m in msgs:
            full = _meta(self.by_key[m.message_id], self.to, True)
            m.body, m.attachment_names = full.body, full.attachment_names

    def get_full(self, ids):
        self.meter.charge("full", len(ids))
        return [_meta(self.by_key[i], self.to, True) for i in ids]


def run(c, account_id: int, mails: list[Mail], to: str, registry, years: int, names: list[str]) -> list[dict]:
    """Quick scan, then the full scan (resuming as the real worker would), into Postgres."""
    from .. import cards, config
    from ..engine import EvidenceStore, full_plan, quick_plan, scan
    out = []
    today = date.fromisoformat("2026-11-16")
    m = Meter(config.BUDGETS["discover_quick"], per_minute=10**9)
    st = scan(LocalBox(mails, to, m), registry, EvidenceStore(c, account_id), quick_plan(registry, today),
              names=names, max_reads=config.QUICK_MAX_READS, allow_full_reads=False, interleave=True)
    cards.rebuild(c, account_id, registry)
    out.append({"kind": "discover_quick", **st.as_dict(), **m.summary()})
    rounds = 0
    while True:
        rounds += 1
        m = Meter(config.BUDGETS["discover_full"], per_minute=10**9)
        try:
            st = scan(LocalBox(mails, to, m), registry, EvidenceStore(c, account_id), full_plan(registry, today, years),
                      names=names)
            out.append({"kind": f"discover_full #{rounds}", **st.as_dict(), **m.summary()})
            break
        except BudgetExhausted as e:
            out.append({"kind": f"discover_full #{rounds} (paused)", "note": str(e), **m.summary()})
            cards.rebuild(c, account_id, registry)
            if rounds > 10:
                break
    cards.rebuild(c, account_id, registry)
    return out
