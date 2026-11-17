"""Forged, spoofed and one-off mail must not create cards or change real ones."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from carddiscovery.models import Evidence, EvidenceType as ET
from carddiscovery.registry import Registry
from cdserver import cards, engine, trust

NOW = datetime.now(timezone.utc)


def ev(i, typ, l4s, days_ago, ok=True, sender="alerts@hdfcbank.net", product="Millennia", bank="hdfc"):
    return Evidence(message_id=f"e{i}", date=NOW - timedelta(days=days_ago), bank_id=bank, type=typ,
                    last4s=list(l4s), product=product, products=[product] if product else [], dkim_pass=ok,
                    sender=sender)


def shown(c, a):
    return {r["last4"]: (r["grp"], r["view"]["status"]) for r in
            c.execute("SELECT last4, grp, view FROM cards WHERE account_id = %s", (a,))}


def test_mail_failing_gmails_check_creates_nothing_and_changes_nothing(c, account):
    a = account()
    engine.EvidenceStore(c, a).save([
        ev(1, ET.STATEMENT, ["2846"], 70), ev(2, ET.STATEMENT, ["2846"], 40), ev(3, ET.TRANSACTION_ALERT, ["2846"], 5),
        ev(4, ET.UPGRADE, ["2846", "9999"], 10, ok=False),        # forged upgrade of a real card
        ev(5, ET.CLOSURE, ["2846"], 3, ok=False),                 # forged closure
        ev(6, ET.STATEMENT, ["8812"], 20, ok=False),              # forged statement for a fake card
    ])
    out = cards.rebuild(c, a, Registry.load())
    assert shown(c, a) == {"2846": ("owned", "Active")}
    assert out["rejected"] == 3 and out["rejected_by_bank"] == {"hdfc": 3}


def test_upgrade_counts_only_with_follow_up_mail_and_closure_only_without_later_spending(c, account):
    a = account()
    engine.EvidenceStore(c, a).save([
        ev(1, ET.STATEMENT, ["6666"], 400), ev(2, ET.STATEMENT, ["6666"], 370),
        ev(3, ET.UPGRADE, ["6666", "7777"], 300),                 # real: 7777 statements follow
        ev(4, ET.STATEMENT, ["7777"], 270), ev(5, ET.STATEMENT, ["7777"], 30),
        ev(6, ET.STATEMENT, ["2222"], 200, product=None), ev(7, ET.STATEMENT, ["2222"], 100, product=None),
        ev(8, ET.REPLACEMENT, ["2222", "7310"], 90, product=None),  # verified but never followed up
        ev(9, ET.TRANSACTION_ALERT, ["2222"], 10, product=None),
        ev(10, ET.CLOSURE, ["2222"], 60, product=None),             # closure, then more spending
        ev(11, ET.TRANSACTION_ALERT, ["2222"], 20, product=None),
    ])
    out = cards.rebuild(c, a, Registry.load())
    got = shown(c, a)
    assert got["6666"][0] == "history" and got["7777"] == ("owned", "Active")
    assert got["2222"] == ("owned", "Active") and out["uncorroborated"] == 2


def test_a_single_weak_email_from_a_new_sender_waits_to_be_confirmed(c, account):
    a = account()
    engine.EvidenceStore(c, a).save([
        ev(1, ET.TRANSACTION_ALERT, ["4455"], 3, sender="new-alerts@hdfcbank.net"),
        ev(2, ET.WELCOME, ["5566"], 3, sender="new-cards@hdfcbank.net"),       # strong: owned at once
    ])
    cards.rebuild(c, a, Registry.load())
    got = shown(c, a)
    assert got["4455"] == ("review", "New, waiting to confirm")
    assert got["5566"][0] == "owned"


def test_known_senders_need_verified_mail_in_two_months():
    evs = [ev(1, ET.STATEMENT, ["1"], 70), ev(2, ET.STATEMENT, ["1"], 40, sender="alerts@hdfcbank.net"),
           ev(3, ET.STATEMENT, ["1"], 5, sender="once@hdfcbank.net"),
           ev(4, ET.STATEMENT, ["1"], 100, ok=False, sender="spoof@hdfcbank.net"),
           ev(5, ET.STATEMENT, ["1"], 10, ok=False, sender="spoof@hdfcbank.net")]
    assert trust.known_senders(evs) == {"alerts@hdfcbank.net"}


def test_exclusion_list_stays_within_gmails_request_size():
    many = {("hdfc", f"{n:04d}"): 1 for n in range(500)}
    terms = engine.exclusion_terms(many, {})
    assert len(" ".join(terms)) <= engine.MAX_EXCLUSION_CHARS
    assert terms[:2] == ("-0000", "-XX0000")  # most-mentioned cards first
