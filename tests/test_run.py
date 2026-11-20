"""End-to-end run against a fake mailbox that mimics Gmail's search filters."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr

from carddiscovery.ledger import Ledger
from carddiscovery.models import MessageMeta
from carddiscovery.pipeline import discover
from carddiscovery.registry import Registry, sender_domain

ME = "me@example.com"
AUTH = "mx.google.com; dkim=pass header.i=@icicibank.com"


class FakeGmail:
    def __init__(self, msgs: list[MessageMeta]):
        self.msgs = {m.message_id: m for m in msgs}
        self.searches = []

    def mailbox(self):
        return ME

    def user_addresses(self):
        return [ME]

    def search(self, q):
        self.searches.append(q)
        out = []
        for m in sorted(self.msgs.values(), key=lambda m: m.date, reverse=True):
            dom = sender_domain(m.sender) or ""
            addr = parseaddr(m.sender)[1].lower()
            if not any(s == addr or dom == s or dom.endswith("." + s) for s in q.senders):
                continue
            d = m.date.date()
            if q.after and d < q.after or q.before and d >= q.before:  # Gmail: before: is exclusive
                continue
            if q.subject_any and not any(t.lower() in m.subject.lower() for t in q.subject_any):
                continue
            if q.text_any and not any(t.lower() in m.text().lower() for t in q.text_any):
                continue
            # Gmail's -subject: matches whole words: -subject:debit doesn't drop "Rs.500 debited".
            if any(re.search(rf"(?<!\w){re.escape(t.lower())}(?!\w)", m.subject.lower()) for t in q.exclude_subject):
                continue
            out.append(m.message_id)
        return out[: q.max_results] if q.max_results else out

    def get_metadata(self, ids):
        return [MessageMeta(**{**vars(self.msgs[i]), "body": None}) for i in ids]

    def fill_body(self, m):
        m.body = self.msgs[m.message_id].body
        return m


def msg(i, subject, body, when):
    return MessageMeta(message_id=f"r{i}", date=when, sender="ICICI <cc@icicibank.com>", subject=subject,
                       snippet=body[:100], body=body, to=ME, auth_results=AUTH)


def test_alert_sent_today_is_found_by_alert_search(tmp_path):
    now = datetime.now(timezone.utc)
    box = FakeGmail([msg(1, "Transaction alert", "INR 99 spent on Card XX4242", now)])
    res = discover(box, Registry.load(), Ledger(tmp_path / "l.db"))
    assert res.stats.per_family["alerts"] == 1
    assert [c.last4 for c in res.cards] == ["4242"]


def test_sparse_alerts_cost_one_search_per_sender_group(tmp_path):
    now = datetime.now(timezone.utc)
    box = FakeGmail([msg(1, "Your ICICI Bank Coral Credit Card Statement", "card ending 1111", now)])
    res = discover(box, Registry.load(), Ledger(tmp_path / "l.db"))
    alert_searches = [q for q in box.searches if q.family == "alerts"]
    groups = -(-len(Registry.load().all_domains()) // 25)
    assert len(alert_searches) == groups  # one per sender group, no splitting
    assert res.stats.queries == len(box.searches) < 8 * groups


def test_dense_alerts_are_split_until_every_card_is_found(tmp_path):
    # 5 years of daily alerts on one card, plus a second card used only twice, years ago.
    now = datetime.now(timezone.utc)
    msgs = [msg(i, "Transaction alert", "INR 50 spent on Card XX1111", now - timedelta(days=i))
            for i in range(1, 1800)]
    msgs += [msg(5000, "Transaction alert", "INR 10 spent on Card XX2222", now - timedelta(days=2500)),
             msg(5001, "Transaction alert", "INR 10 spent on Card XX2222", now - timedelta(days=2400))]
    box = FakeGmail(msgs)
    res = discover(box, Registry.load(), Ledger(tmp_path / "l.db"), alerts_per_window=40)
    assert sorted(c.last4 for c in res.cards) == ["1111", "2222"]
    assert res.stats.metadata_fetched < 600  # sampled, not all 1,800 alerts
    assert res.stats.bodies_fetched == 0  # alerts with a card number need no full fetch


def test_second_run_uses_cache(tmp_path):
    now = datetime.now(timezone.utc)
    box = FakeGmail([msg(1, "Your ICICI Bank Coral Credit Card Statement", "card ending 1111", now)])
    ledger = Ledger(tmp_path / "l.db")
    discover(box, Registry.load(), ledger)
    res = discover(box, Registry.load(), ledger)
    assert res.stats.metadata_fetched == 0 and len(res.cards) == 1


def test_scan_depth_limits_every_search(tmp_path):
    now = datetime.now(timezone.utc)
    box = FakeGmail([
        msg(1, "Your ICICI Bank Coral Credit Card Statement", "card ending 1111", now - timedelta(days=30)),
        msg(2, "Your ICICI Bank Coral Credit Card Statement", "card ending 2222", now - timedelta(days=365 * 4)),
    ])
    res = discover(box, Registry.load(), Ledger(tmp_path / "l.db"), history_years=2)
    assert [c.last4 for c in res.cards] == ["1111"]
    assert all(q.after is not None for q in box.searches)
