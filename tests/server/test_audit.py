"""The development-only inbox audit: guard, search matching, and the report."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from carddiscovery.registry import Registry
from cdserver import audit


def test_audit_refuses_outside_development(monkeypatch):
    monkeypatch.delenv("CARDDISCOVERY_DEV", raising=False)
    with pytest.raises(SystemExit, match="development only"):
        audit.run(1, 4, 2000)


def test_search_matching_uses_whole_words():
    # "debit" (a noise word) must not drop "debited" alerts, as Gmail's whole-word search doesn't.
    assert audit.search_fate("Rs.500 debited via Credit Card **6113", "", [], recent=True) == \
        "searched (alerts, last 12 months)"
    assert audit.search_fate("Debit notification from Axis Bank", "", [], recent=True) == \
        "dropped: noise word in subject"
    assert audit.search_fate("Get 5% off with Card xx7776", "", ["CATEGORY_PROMOTIONS"], recent=True) == \
        "searched (card offers, last 12 months)"


def _row(i, query, frm, subject, snippet="", labels=(), auth="mx.google.com; dkim=pass header.i=@hdfcbank.net"):
    return {"id": f"m{i}", "query": query, "labels": list(labels), "date": 1_790_000_000_000 + i,
            "snippet": snippet, "headers": {"From": frm, "Subject": subject, "Authentication-Results": auth}}


def test_report_flags_missing_cards_and_unknown_senders():
    rows = [
        _row(1, "known", "HDFC <alerts@hdfcbank.net>", "Rs.500 debited via Credit Card **6113"),
        _row(2, "known", "HDFC <alerts@hdfcbank.net>", "Rs.900 debited via Credit Card **7091"),
        _row(3, "other", "X <a@newbank.example>", "Your HDFC Bank credit card ending 2222 bill",
             auth="mx.google.com; dkim=pass header.d=newbank.example; dmarc=pass"),
    ]
    text = audit.report(rows, Registry.load(), {"hdfc:6113"}, datetime(2026, 11, 19, tzinfo=timezone.utc))
    assert "HAVE    hdfc:6113" in text and "MISSING hdfc:7091" in text
    assert "newbank.example" in text and "dkim=newbank.example dmarc=pass" in text
    assert "MISSING hdfc:2222" in text
