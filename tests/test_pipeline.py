"""Scenario tests mirroring the test-case matrix in docs/card-discovery-design.md §13.

Each test builds synthetic emails, runs analyze → resolve → score, and checks the
resulting cards. Numbers in test names refer to the matrix.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from carddiscovery import extract
from carddiscovery.classify import analyze
from carddiscovery.models import EvidenceType as T, MessageMeta, Status
from carddiscovery.registry import Registry
from carddiscovery.resolve import resolve
from carddiscovery.score import group, score_all

NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)
USER = "me@example.com"
REG = Registry.load()

AUTH = {
    "hdfc": "mx.google.com; dkim=pass header.i=@hdfcbank.net header.s=s1; spf=pass",
    "icici": "mx.google.com; dkim=pass header.i=@icicibank.com; spf=pass",
    "axis": "mx.google.com; dkim=pass header.d=axisbank.com; spf=pass",
    "sbi": "mx.google.com; dkim=pass header.i=@sbicard.com",
    "amex": "mx.google.com; dkim=pass header.i=@aexp.com",
}
FROM = {
    "hdfc": "HDFC Bank <alerts@hdfcbank.net>",
    "icici": "ICICI Bank <credit_cards@icicibank.com>",
    "axis": "Axis Bank <cc.statements@axisbank.com>",
    "sbi": "SBI Card <statements@sbicard.com>",
    "amex": "American Express <americanexpress@welcome.aexp.com>",
}

_n = 0


def mail(bank: str, subject: str, snippet: str = "", body: str | None = None, *, days_ago: int = 30,
         sender: str | None = None, to: str = USER, auth: str | None = None, unsub: bool = False,
         labels: tuple[str, ...] = ()) -> MessageMeta:
    global _n
    _n += 1
    return MessageMeta(
        message_id=f"m{_n}",
        date=NOW - timedelta(days=days_ago),
        sender=sender or FROM[bank],
        subject=subject,
        snippet=snippet,
        to=to,
        auth_results=AUTH.get(bank, "") if auth is None else auth,
        list_unsubscribe=unsub,
        labels=labels,
        body=body,
    )


def run(msgs, names=("Shubham Kadam",)):
    ev = [analyze(m, REG, [USER], list(names)) for m in msgs]
    cards, apps = resolve(ev)
    score_all(cards, NOW)
    return cards, apps, ev


def monthly_statements(bank, product_phrase, last4, start_days_ago, count):
    return [
        mail(bank, f"Your {product_phrase} Credit Card Statement",
             f"Dear Shubham Kadam, your statement for card ending {last4} is ready.",
             days_ago=start_days_ago + 30 * i)
        for i in range(count)
    ]


def by_last4(cards):
    return {c.last4: c for c in cards}


# ---------------------------------------------------------------- extraction

@pytest.mark.parametrize("text,expected", [
    ("Card XX1234 used", ["1234"]),
    ("card xxxx xxxx xxxx 1234", ["1234"]),
    ("**** 1234 statement", ["1234"]),
    ("card ending 1234", ["1234"]),
    ("Credit Card ending with 1234", ["1234"]),
    ("ending in XX1234", ["1234"]),
    ("4XXXXXXXXXXX1234", ["1234"]),
    ("5xxx-xxxx-xxxx-1234", ["1234"]),
    ("old card XX1111, new card XX2222", ["1111", "2222"]),
    ("Rs 1234 spent on 12/10/2025", []),
])
def test_last4_masks_25(text, expected):
    assert extract.last4s(text) == expected


def test_full_card_number_redacted_29():
    text, n = extract.redact_pans("your card 4111 1111 1111 1111 was used")
    assert n == 1 and "4111" not in text
    msg = mail("hdfc", "Transaction alert", "card 4111111111111111 used at Amazon")
    analyze(msg, REG, [USER])
    assert "4111111111111111" not in msg.snippet


def test_product_aliases_bank_scoped_24():
    assert REG.products_in("hdfc", "HDFC Bank Regalia Gold Credit Card") == ["Regalia Gold"]
    assert REG.products_in("hdfc", "your Regalia credit card") == ["Regalia"]
    assert REG.products_in("idfc", "FIRST Millennia Credit Card") == ["Millennia"]
    # Generic words don't match unless they sit next to "card".
    assert REG.products_in("axis", "Select your preferred plan today") == []
    assert REG.products_in("axis", "Axis Bank Select Credit Card") == ["Select"]


# ---------------------------------------------------------------- A. discovery

def test_active_card_monthly_statements_1_21():
    cards, _, _ = run(monthly_statements("hdfc", "HDFC Bank Regalia Gold", "1111", 10, 24))
    assert len(cards) == 1
    c = cards[0]
    assert c.last4 == "1111" and c.product == "Regalia Gold"
    assert c.status == Status.ACTIVE and group(c) == "owned" and c.ownership >= 0.95


def test_unused_three_years_but_fee_notices_2_8():
    msgs = monthly_statements("hdfc", "HDFC Bank Millennia", "2222", 1200, 12)
    msgs.append(mail("hdfc", "Annual fee charged on your Millennia Credit Card",
                     "Annual fee of Rs 1000 charged on card ending 2222", days_ago=200))
    cards, _, _ = run(msgs)
    c = by_last4(cards)["2222"]
    assert c.status == Status.DORMANT and c.status_confidence >= 0.8
    assert group(c) == "owned"


def test_old_approval_only_goes_to_review_3():
    cards, _, _ = run([mail("icici", "Congratulations! Your Sapphiro Credit Card is approved",
                            "Dear Shubham, your ICICI Bank Sapphiro Credit Card has been approved.",
                            days_ago=1500)])
    assert len(cards) == 1
    c = cards[0]
    assert c.last4 is None and c.product == "Sapphiro"
    assert group(c) == "review" and c.status == Status.APPROVED_NOT_SEEN


def test_two_cards_same_bank_same_product_4():
    msgs = monthly_statements("axis", "Axis Bank ACE", "1111", 10, 6)
    msgs += monthly_statements("axis", "Axis Bank ACE", "2222", 15, 6)
    cards, _, _ = run(msgs)
    assert sorted(c.last4 for c in cards) == ["1111", "2222"]
    assert all(group(c) == "owned" for c in cards)


def test_different_variants_same_bank_5():
    msgs = monthly_statements("hdfc", "HDFC Bank Regalia Gold", "1111", 10, 3)
    msgs += monthly_statements("hdfc", "HDFC Bank Millennia", "2222", 12, 3)
    cards, _, _ = run(msgs)
    assert {c.last4: c.product for c in cards} == {"1111": "Regalia Gold", "2222": "Millennia"}


def test_rarely_used_lifetime_free_6_and_occasional_alerts_7():
    msgs = [
        mail("icici", "Welcome to the world of ICICI Bank Amazon Pay Credit Card",
             "Your Amazon Pay Credit Card ending 3333 is on its way", days_ago=1800),
        mail("icici", "Transaction alert for your ICICI Bank Credit Card",
             "INR 499 spent on ICICI Bank Card XX3333 at Amazon", days_ago=900),
        mail("icici", "Transaction alert for your ICICI Bank Credit Card",
             "INR 99 spent on ICICI Bank Card XX3333", days_ago=400),
    ]
    cards, _, _ = run(msgs)
    c = by_last4(cards)["3333"]
    assert c.product == "Amazon Pay" and group(c) == "owned" and c.status == Status.DORMANT


# ---------------------------------------------------------------- B. history

def test_old_welcome_only_9():
    cards, _, _ = run([mail("sbi", "Welcome to SBI Card",
                            "Your SimplyCLICK SBI Card ending 4444 has been dispatched", days_ago=2000)])
    c = cards[0]
    assert c.last4 == "4444" and group(c) == "owned" and c.status == Status.DORMANT


def test_last_statement_years_ago_not_closed_10():
    cards, _, _ = run(monthly_statements("sbi", "SBI Card ELITE", "5555", 1500, 10))
    c = cards[0]
    assert c.status == Status.DORMANT and c.status != Status.CLOSED


def test_upgrade_new_number_11():
    msgs = monthly_statements("hdfc", "HDFC Bank Regalia", "1111", 400, 6)
    msgs.append(mail("hdfc", "Your card has been upgraded",
                     "Your Regalia Credit Card ending 1111 has been upgraded to Regalia Gold Credit Card. "
                     "Your new card ending 9999 will be dispatched.", days_ago=300))
    msgs += monthly_statements("hdfc", "HDFC Bank Regalia Gold", "9999", 20, 6)
    cards, _, _ = run(msgs)
    cs = by_last4(cards)
    assert cs["1111"].status == Status.REPLACED and group(cs["1111"]) == "history"
    assert cs["1111"].replaced_by is cs["9999"]
    assert cs["9999"].product == "Regalia Gold" and group(cs["9999"]) == "owned"


def test_upgrade_same_number_11b():
    msgs = monthly_statements("hdfc", "HDFC Bank Regalia", "1111", 400, 3)
    msgs.append(mail("hdfc", "Upgrade successful",
                     "Your card ending 1111 has been upgraded to Regalia Gold Credit Card", days_ago=300))
    msgs += monthly_statements("hdfc", "HDFC Bank Regalia Gold", "1111", 20, 3)
    cards, _, _ = run(msgs)
    assert len(cards) == 1
    assert cards[0].product == "Regalia Gold"
    assert [p for _, p in cards[0].product_history][0] == "Regalia"


def test_lost_card_replaced_12b():
    msgs = monthly_statements("axis", "Axis Bank Magnus", "1111", 200, 3)
    msgs.append(mail("axis", "Replacement card dispatched",
                     "Replacement card for card ending 1111 dispatched. New card ending 7777.", days_ago=100))
    msgs += monthly_statements("axis", "Axis Bank Magnus", "7777", 10, 2)
    cards, _, _ = run(msgs)
    cs = by_last4(cards)
    assert cs["1111"].status == Status.REPLACED and cs["7777"].status == Status.ACTIVE


def test_closed_card_13():
    msgs = monthly_statements("icici", "ICICI Bank Coral", "8888", 900, 5)
    msgs.append(mail("icici", "Your credit card has been closed",
                     "Your Coral Credit Card ending 8888 has been closed as per your request.", days_ago=700))
    cards, _, _ = run(msgs)
    c = cards[0]
    assert c.status == Status.CLOSED and group(c) == "history"


def test_inactivity_notice_then_usage_34_and_without_35():
    base = [mail("hdfc", "Your Credit Card is inactive",
                 "Your Millennia Credit Card ending 1212 has not been used for over a year", days_ago=200)]
    cards, _, _ = run(base)
    assert cards[0].status == Status.POSSIBLY_CLOSED
    cards, _, _ = run(base + [mail("hdfc", "Transaction alert", "Rs 100 spent on card XX1212", days_ago=50)])
    assert cards[0].status == Status.ACTIVE


# ---------------------------------------------------------------- C. false positives

def test_promo_for_unowned_card_creates_nothing_14_15():
    msgs = [
        mail("hdfc", "Apply now for HDFC Bank Infinia Credit Card",
             "Exclusive offer: lifetime free, apply now", unsub=True),
        mail("axis", "You are pre-approved for an Axis Bank Atlas Credit Card",
             "Get your card in minutes", unsub=True),
        mail("sbi", "Introducing the new SBI Card PRIME", "Limited period offer", unsub=True,
             labels=("CATEGORY_PROMOTIONS",)),
    ]
    cards, _, ev = run(msgs)
    assert cards == []
    assert {e.type for e in ev} == {T.PROMOTIONAL}


def test_offer_naming_existing_card_15b():
    msgs = monthly_statements("hdfc", "HDFC Bank Regalia", "1111", 10, 2)
    msgs.append(mail("hdfc", "Upgrade now to Regalia Gold",
                     "Your Regalia Credit Card ending 1111 is eligible for an upgrade to Regalia Gold"))
    cards, _, _ = run(msgs)
    assert len(cards) == 1 and cards[0].product == "Regalia"


def test_addon_card_on_primary_statement_16():
    msgs = [mail("hdfc", "Your HDFC Bank Regalia Credit Card Statement",
                 "Statement for card ending 1111. Add-on card ending 5656 transactions included.",
                 days_ago=20)]
    cards, _, _ = run(msgs)
    cs = by_last4(cards)
    assert group(cs["1111"]) == "owned"
    assert "addon" in cs["5656"].flags and group(cs["5656"]) == "review"


def test_someone_elses_card_17():
    msgs = [mail("icici", "Payment due reminder for your ICICI Bank Credit Card",
                 "Dear Priya Sharma, payment due for card ending 4545", to="priya@example.com")]
    cards, _, _ = run(msgs)
    assert group(cards[0]) == "review" and cards[0].ownership < 0.5


def test_forwarded_statement_18():
    msgs = [mail("hdfc", "Fwd: Your HDFC Bank Millennia Credit Card Statement",
                 "---------- Forwarded message --------- statement for card ending 3434",
                 sender="Dad <dad@example.com>", auth="mx.google.com; dkim=pass header.i=@example.com")]
    cards, _, ev = run(msgs)
    # Sender isn't a bank, so it never becomes card evidence.
    assert ev[0].bank_id is None and cards == []


def test_rejected_and_in_progress_applications_19_20():
    msgs = [
        mail("axis", "Update on your Credit Card application",
             "We regret to inform you that we are unable to approve your application", days_ago=100),
        mail("icici", "Your Credit Card application is in progress",
             "Thank you for applying for the ICICI Bank Sapphiro Credit Card", days_ago=10),
    ]
    cards, apps, _ = run(msgs)
    assert cards == []
    assert {(a.bank_id, a.status) for a in apps} == {("axis", "rejected"), ("icici", "in_progress")}


def test_otp_and_debit_card_mail_ignored_45():
    msgs = [
        mail("hdfc", "OTP for your transaction", "Your OTP is 123456 for card XX1111"),
        mail("hdfc", "Debit card transaction alert", "Rs 500 debited from your savings account XX9090 "
                                                      "using debit card"),
    ]
    cards, _, ev = run(msgs)
    assert cards == [] and [e.type for e in ev] == [T.OTP, T.NOT_CREDIT_CARD]


def test_spoofed_bank_mail_not_owned_27():
    msgs = [mail("hdfc", "Your HDFC Bank Regalia Credit Card Statement",
                 "statement for card ending 6060", auth="mx.google.com; dkim=fail; spf=fail")]
    cards, _, _ = run(msgs)
    assert group(cards[0]) == "review"


# ---------------------------------------------------------------- D. duplicates

def test_alerts_and_promos_same_card_22():
    msgs = monthly_statements("icici", "ICICI Bank Coral", "1111", 10, 2)
    msgs += [mail("icici", "Transaction alert", "INR 200 spent on Card XX1111", days_ago=5),
             mail("icici", "Exclusive offer on your Credit Card XX1111", "Avail 10% off", unsub=True)]
    cards, _, _ = run(msgs)
    assert len(cards) == 1


def test_bank_sender_domain_change_23():
    msgs = [
        mail("hdfc", "Your HDFC Bank Millennia Credit Card Statement", "card ending 1111", days_ago=2000,
             sender="HDFC <statements@hdfcbank.com>", auth="mx.google.com; dkim=pass header.i=@hdfcbank.com"),
        mail("hdfc", "Your HDFC Bank Millennia Credit Card Statement", "card ending 1111", days_ago=20),
    ]
    cards, _, _ = run(msgs)
    assert len(cards) == 1 and group(cards[0]) == "owned"


def test_masked_variants_same_card_25():
    msgs = [
        mail("axis", "Axis Bank ACE Credit Card Statement", "card no. XXXX XXXX XXXX 4321", days_ago=60),
        mail("axis", "Transaction alert", "Spent Rs 100 on card ending 4321", days_ago=40),
        mail("axis", "Payment received", "Payment received for card 5xxx-xxxx-xxxx-4321", days_ago=30),
    ]
    cards, _, _ = run(msgs)
    assert len(cards) == 1 and cards[0].last4 == "4321"


# ---------------------------------------------------------------- resolution edge cases

def test_approval_without_number_merges_into_unique_card():
    msgs = [mail("hdfc", "Your HDFC Bank Millennia Credit Card is approved",
                 "Congratulations, your Millennia Credit Card has been approved", days_ago=400)]
    msgs += monthly_statements("hdfc", "HDFC Bank Millennia", "1111", 10, 3)
    cards, _, _ = run(msgs)
    assert len(cards) == 1 and cards[0].last4 == "1111"


def test_approval_without_number_not_merged_when_ambiguous_30():
    msgs = [mail("axis", "Your Axis Bank ACE Credit Card is approved",
                 "Your ACE Credit Card has been approved", days_ago=400)]
    msgs += monthly_statements("axis", "Axis Bank ACE", "1111", 10, 2)
    msgs += monthly_statements("axis", "Axis Bank ACE", "2222", 12, 2)
    cards, _, _ = run(msgs)
    prov = [c for c in cards if c.last4 is None]
    assert len(prov) == 1 and "ambiguous" in prov[0].flags and group(prov[0]) == "review"


def test_same_last4_different_banks_31():
    msgs = monthly_statements("hdfc", "HDFC Bank Millennia", "1111", 10, 2)
    msgs += monthly_statements("icici", "ICICI Bank Coral", "1111", 12, 2)
    cards, _, _ = run(msgs)
    assert len(cards) == 2


def test_recently_issued_card_is_active_not_dormant():
    cards, _, _ = run([mail("sbi", "Welcome aboard!",
                            "Your SBI Card SimplySAVE **** **** **** 1029 has been activated", days_ago=3)])
    assert cards[0].status == Status.ACTIVE
