"""Cases from real mail: patterns that hung the workers, HSBC from mail.hsbc.co.in,
Axis statements that show only 2 digits, a card listed twice (Live+), and bill-payment
apps (CRED)."""

from __future__ import annotations

import time

import pytest

from carddiscovery import extract
from carddiscovery.explain import explain
from carddiscovery.models import EvidenceType as T
from carddiscovery.resolve import resolve_full
from carddiscovery.score import group

from .test_pipeline import REG, mail, monthly_statements, run

HSBC = dict(sender="HSBC India <hsbc@mail.hsbc.co.in>", auth="mx.google.com; dkim=pass header.i=@mail.hsbc.co.in")
AXIS = dict(sender="Axis Bank <cc.statements@axis.bank.in>", auth="mx.google.com; dkim=pass header.i=@axis.bank.in")
CRED = dict(sender="CRED <protect@cred.club>", auth="mx.google.com; dkim=pass header.i=@cred.club")


def ids(card):
    return {e.message_id for e in card.evidence}


# ---------------------------------------------------------------- patterns must not hang

@pytest.mark.parametrize("text", [
    "*" * 67,                              # HSBC's divider line: hung both workers
    "1" + "*" * 5000 + " end",
    "card ending " + " " * 5000 + "x",     # long blank run after "ending"
    "last 4 digits " + "\n" * 5000 + "x",
    "ending XX" + "X" * 5000,
])
def test_number_patterns_stay_fast_on_long_runs(text):
    t0 = time.monotonic()
    extract.last4s(text), extract.number_prefixes(text), extract.partial_last_digits(text)
    assert time.monotonic() - t0 < 0.5


# ---------------------------------------------------------------- HSBC alert

def test_hsbc_alert_from_mail_subdomain_is_read():
    m = mail("hdfc", "You have used your HSBC Credit Card ending with 9719 for a purchase transaction",
             "Your Credit card no ending with 9719,has been used for INR 2624.40.\n" + "*" * 67, **HSBC)
    _, _, [ev] = run([m])
    assert (ev.bank_id, ev.type.value, ev.last4s, ev.dkim_pass) == ("hsbc", "transaction_alert", ["9719"], True)


# ---------------------------------------------------------------- Axis "ending XX87"

def airtel_statement(days_ago=10):
    return mail("hdfc", "Airtel Axis Bank Mastercard Credit Card Statement ending XX87 - September 2026",
                "Please find enclosed your credit card statement. Total Amount Due 502 Dr", days_ago=days_ago, **AXIS)


def test_two_digit_ending_is_kept_and_rules_out_other_cards():
    magnus = monthly_statements("axis", "Axis Bank Magnus", "6190", 15, 3)
    stmt = airtel_statement()
    cards, _, ev = run(magnus + [stmt])
    assert ev[-1].last_digits == "87" and ev[-1].product == "Airtel" and not ev[-1].last4s
    airtel = next(c for c in cards if c.last4 is None)
    assert airtel.partial == "87" and airtel.product == "Airtel" and stmt.message_id in ids(airtel)
    assert explain(airtel, REG).title == "Axis Bank Airtel ••87"
    assert stmt.message_id not in ids(next(c for c in cards if c.last4 == "6190"))


def test_two_digit_ending_joins_the_card_it_matches():
    msgs = [mail("axis", "Your Axis Bank credit card ending 4587 was used", "Card ending 4587 used for Rs 500.",
                 days_ago=20), airtel_statement()]
    cards, _, _ = run(msgs)
    assert [c.last4 for c in cards] == ["4587"] and cards[0].product == "Airtel"


# ---------------------------------------------------------------- "Live+" listed twice

def hsbc_alert(l4, days_ago):
    return mail("hdfc", f"You have used your HSBC Credit Card ending with {l4}",
                f"Your Credit card no ending with {l4} has been used for INR 500.", days_ago=days_ago, **HSBC)


def live_statement(days_ago):
    return mail("hdfc", "Your HSBC Live+ Credit Card statement", "Your Live+ Credit Card statement is ready.",
                days_ago=days_ago, **HSBC)


def test_unnamed_card_takes_the_only_product_its_bank_mail_names():
    # Before: "HSBC Live+" (no number) and "HSBC credit card ••3408" both shown as owned.
    cards, _, _ = run([hsbc_alert("3408", 40), live_statement(30), live_statement(60)])
    assert [(c.last4, c.product) for c in cards] == [("3408", "Live+")]


def test_two_unnamed_cards_leave_the_product_mail_set_aside():
    _, _, ev = run([hsbc_alert("3408", 40), hsbc_alert("9719", 45), live_statement(30)])
    r = resolve_full(ev)
    assert sorted((c.last4, c.product) for c in r.cards) == [("3408", None), ("9719", None)]
    assert [s.reason for s in r.set_aside] == ["several_cards"]


def test_two_unknown_products_are_not_given_to_the_unnamed_card():
    premier = mail("hdfc", "Your HSBC Premier Credit Card statement", "Your Premier Credit Card statement.",
                   days_ago=35, **HSBC)
    cards, _, _ = run([hsbc_alert("3408", 40), live_statement(30), premier])
    assert sorted((c.last4 or "-", c.product or "-") for c in cards) == \
        [("-", "Live+"), ("-", "Premier"), ("3408", "-")]


# ---------------------------------------------------------------- bill-payment apps

def cred_bill(l4, bank_phrase="HDFC Bank", days_ago=5, **kw):
    return mail("hdfc", f"Your {bank_phrase} credit card bill is due",
                f"Your {bank_phrase} credit card ending {l4} has a bill of Rs. 4,210 due soon.",
                days_ago=days_ago, **{**CRED, **kw})


def test_app_mail_joins_the_card_the_bank_mail_found():
    msgs = monthly_statements("hdfc", "HDFC Bank Millennia", "2846", 10, 3) + [cred_bill("2846")]
    cards, _, ev = run(msgs)
    assert ev[-1].via == "cred" and ev[-1].bank_id == "hdfc" and ev[-1].dkim_pass
    assert [c.last4 for c in cards] == ["2846"] and group(cards[0]) == "owned"


def test_one_app_bill_puts_the_card_in_the_wallet():
    # CRED sends a bill only for a card linked to the user's own CRED account.
    cards, _, ev = run([cred_bill("5511")])
    [c] = cards
    assert ev[0].type == T.STATEMENT and c.bank_id == "hdfc" and c.last4 == "5511"
    assert "app_only" not in c.flags and group(c) == "owned"


def test_card_seen_only_in_app_payments_goes_to_review():
    # "You paid ... on CRED": people pay family members' cards through these apps too.
    paid = [mail("hdfc", "Payment successful", f"You paid Rs 4,210 to HDFC Bank credit card ending 5511 on CRED.",
                 days_ago=d, **CRED) for d in (5, 35, 65)]
    cards, _, ev = run(paid)
    [c] = cards
    assert all(e.type == T.PAYMENT for e in ev)
    assert "app_only" in c.flags and group(c) == "review"
    assert any("bill-payment app" in s for s in explain(c, REG).concerns)


def test_app_mail_without_a_card_number_is_ignored():
    promo = mail("hdfc", "Pay your HDFC Bank credit card bill on CRED", "Pay any credit card bill and win.",
                 **CRED)
    cards, _, ev = run([promo])
    assert ev[0].bank_id is None and cards == []


def test_app_mail_not_signed_by_the_app_counts_as_unverified():
    _, _, [ev] = run([cred_bill("2846", auth="mx.google.com; dkim=fail")])
    assert ev.via == "cred" and ev.dkim_pass is False


# ---------------------------------------------------------------- bank accounts are not cards

KOTAK = dict(sender="Kotak Mahindra Bank <bankalerts@kotak.com>", auth="mx.google.com; dkim=pass header.i=@kotak.com")


@pytest.mark.parametrize("bank,subject,snippet,extra", [
    ("hdfc", "Email Account Statement of your HDFC Bank Account  ***7785 for the period 10-Aug-2026 TO 16-Sep-2026",
     "Here's your HDFC Bank Account SmartStatement. Apply for an HDFC Bank Credit Card today.", {}),
    ("axis", "Stay up-to-date with charges applicable on your Savings A/c XX8466",
     "Schedule of charges: Credit Card late payment fee, statement charges for account ending 8466", {}),
    ("hdfc", "Dear Nikhil, your account XX7785 is eligible for Preferred Banking at no cost",
     "Upgrade to Preferred Banking with your account XX7785.", {}),
    ("hdfc", "IMPS Transaction - Success", "Rs 500 sent via IMPS from account XX4585.", KOTAK),
    ("hdfc", "Account Balance - Weekly", "Balance in your account XX4585 is Rs 12,000.", KOTAK),
])
def test_bank_account_mail_never_becomes_a_card(bank, subject, snippet, extra):
    cards, _, [ev] = run([mail(bank, subject, snippet, **extra)])
    assert cards == [] and ev.type.value in ("not_credit_card", "promotional")


def test_credit_card_account_wording_is_still_a_card():
    cards, _, _ = run([mail("hdfc", "Your HDFC Bank Credit Card Statement",
                            "Statement for your credit card account ending 1234 is ready.")])
    assert [c.last4 for c in cards] == ["1234"]


# ---------------------------------------------------------------- OTP mail

def otp(text, days_ago=3):
    return mail("axis", "One Time Password for transaction on your Axis Bank Card", text, days_ago=days_ago)


def test_otp_adds_use_to_a_card_other_mail_shows():
    stmts = monthly_statements("axis", "Axis Bank Magnus", "5630", 400, 2)  # last statement over a year ago
    o = otp("OTP 482913 for Rs 999 at AMAZON on your card XX5630. Do not share.")
    cards, _, ev = run(stmts + [o])
    [c] = cards
    assert ev[-1].type.value == "otp" and o.message_id in {e.message_id for e in c.evidence}
    assert c.status.value == "active"  # a recent OTP is recent use


def test_otp_alone_never_creates_a_card():
    cards, _, ev = run([otp("OTP 482913 for Rs 999 on your card XX9911.")])
    assert ev[0].last4s == ["9911"] and cards == []


def test_debit_card_otp_is_not_a_credit_card():
    _, _, [ev] = run([mail("axis", "OTP for your Axis Bank Debit Card transaction", "OTP 1234 for card XX9911")])
    assert ev.type.value == "not_credit_card"


# ---------------------------------------------------------------- searches

def test_otp_is_searched_and_card_offers_are_searched_for_a_year():
    from datetime import date
    from cdserver import engine
    today = date(2026, 11, 19)
    plan = engine.combined_window_plan(REG, date(2026, 8, 1), date(2026, 11, 20), today)
    assert all("otp" not in q.to_gmail().lower() for q in plan)
    offers = [q for q in plan if q.family == "card_offers"]
    assert offers and all("category:promotions" in q.to_gmail() and "subject:(card)" in q.to_gmail() for q in offers)
    old = engine.combined_window_plan(REG, date(2023, 1, 1), date(2023, 4, 1), today)
    assert not [q for q in old if q.family == "card_offers"]  # older than 12 months: not searched
    sync = engine.sync_plan(REG, today)
    assert {q.family for q in sync} == {"sync", "sync_offers"}


def test_no_full_read_when_two_digits_match_a_known_card():
    from cdserver.engine import _Reader
    _, _, ev = run(monthly_statements("axis", "Axis Bank Airtel", "9487", 30, 1) + [airtel_statement()])
    r = _Reader.__new__(_Reader)
    r.known = {e.message_id: e for e in ev[:-1]}
    assert r._partial_known(ev[-1])
    r.known = {}
    assert not r._partial_known(ev[-1])  # no card to match: the full read may find the number


def test_otp_and_alert_subjects_never_name_a_card():
    # Before: "SBI Card One Time Password (unrecognised)", "HDFC Bank Activate (unrecognised)".
    msgs = monthly_statements("hdfc", "HDFC Bank", "3254", 20, 2) + [
        mail("hdfc", "Activate Your Card xx3254 in 5 Seconds", "Activate your card xx3254 now", days_ago=5)]
    cards, _, ev = run(msgs)
    assert not any(f.startswith("raw:Activate") for c in cards for f in c.flags)
    assert "Activate" not in (cards[0].product or "")


def test_merchant_in_an_alert_does_not_name_the_card():
    # Before: "Axis Flipkart ••5630" (from "spent on Flipkart" in one alert) and "Axis MY Zone ••30"
    # (statements ending XX30) were shown as two cards.
    alert = mail("axis", "Transaction alert on your Axis Bank Credit Card",
                 "Rs 799 spent on Flipkart Axis Bank Credit Card ending 5630.", days_ago=40)
    stmts = [mail("axis", "Your Axis Bank MY Zone Credit Card Statement ending XX30", "Statement is ready.",
                  days_ago=d, **AXIS) for d in (10, 40, 70)]
    other = mail("axis", "Your Axis Bank Select Credit Card Statement", "Statement is ready.", days_ago=12)
    cards, _, ev = run([alert] + stmts + [other])
    assert ev[0].product == "Flipkart"  # what the alert alone says
    c5630 = next(c for c in cards if c.last4 == "5630")
    assert c5630.product == "MY Zone" and all(s.message_id in {e.message_id for e in c5630.evidence} for s in stmts)
    assert not any(c.partial == "30" for c in cards)


def test_alert_name_is_used_when_nothing_else_names_the_card():
    cards, _, _ = run([mail("hdfc", "Alert: HDFC Bank Millennia Credit Card used",
                            "Your Millennia Credit Card ending 4455 was used for Rs 500.")])
    assert cards[0].product == "Millennia"


# ---------------------------------------------------------------- any .bank.in name of a bank

@pytest.mark.parametrize("domain,bank", [
    ("hsbc.bank.in", "hsbc"), ("informationservices.hsbc.bank.in", "hsbc"), ("hsbcindia.bank.in", "hsbc"),
    ("icici.bank.in", "icici"), ("customer.icici.bank.in", "icici"), ("hdfc.bank.in", "hdfc"),
    ("mailers.hdfcbank.bank.in", "hdfc"), ("aubank.bank.in", "au"), ("sbicard.bank.in", "sbi"),
    ("alerts.sbi.bank.in", None),          # State Bank of India, not SBI Card
    ("hsbc.bank.in.evil.com", None),       # not under .bank.in
    ("hsbc-alerts.com", None),             # anyone can register this
    ("bank.in", None),
])
def test_any_bank_in_name_of_a_bank_is_that_bank(domain, bank):
    assert REG.bank_for_domain(domain) == bank


def test_unlisted_bank_in_sender_is_read_and_trusted():
    m = mail("hdfc", "You have used your HSBC Credit Card ending with 4242", "Card ending with 4242 used.",
             sender="HSBC <alerts@new.hsbcindia.bank.in>", auth="mx.google.com; dkim=pass header.d=hsbcindia.bank.in")
    _, _, [ev] = run([m])
    assert (ev.bank_id, ev.last4s, ev.dkim_pass) == ("hsbc", ["4242"], True)


def test_bank_in_keys_that_could_mean_two_banks_are_refused():
    from carddiscovery.registry import Registry
    with pytest.raises(ValueError, match="clash"):
        Registry.from_banks({"a": {"name": "A", "domains": ["abc.bank.in"]},
                             "b": {"name": "B", "domains": ["abcd.bank.in"]}})


def test_every_search_also_covers_all_of_bank_in():
    from datetime import date
    from cdserver import engine
    today = date(2026, 11, 19)
    for plan in (engine.quick_plan(REG, today), engine.full_plan(REG, today, 3), engine.sync_plan(REG, today),
                 engine.combined_window_plan(REG, date(2026, 8, 1), date(2026, 11, 20), today)):
        assert any(q.senders == ("bank.in",) for q in plan)


# ---------------------------------------------------------------- one card, not three

def _hdfc_statement(subject, attachment, days_ago):
    from carddiscovery.models import MessageMeta
    from datetime import timedelta
    from .test_pipeline import NOW
    return MessageMeta(f"st{days_ago}{(attachment or 'x')[:4]}", NOW - timedelta(days=days_ago),
                       "HDFC Bank Cards <Emailstatements.cards@hdfcbank.bank.in>", subject,
                       "Your latest eStatement is attached.",
                       auth_results="mx.google.com; dkim=pass header.i=@hdfcbank.bank.in",
                       body="Your latest eStatement is attached with this mail.",
                       attachment_names=(attachment,) if attachment else ())


def test_statements_with_and_without_the_ending_name_one_card():
    # Before: "PhonePe Ultimo" (no number) shown next to ••3254, and "Diners Club Black" and
    # "Diners Club Black Metal" (no number) twice each, beside the cards they describe.
    alert = lambda l4, d: mail("hdfc", f"Rs.500 debited via Credit Card **{l4}",
                               f"Your HDFC Bank Credit Card ending {l4} was used.", days_ago=d)
    msgs = [alert("3254", 5), alert("6113", 6),
            _hdfc_statement("Your HDFC Bank - PhonePe Ultimo HDFC Bank Credit Card Statement - Oct-2026", None, 40),
            _hdfc_statement("Your HDFC Bank - PhonePe Ultimo HDFC Bank Credit Card Statement - November-2026",
                            "6530XXXXXXXXXX54_17-11-2026_5.pdf", 2),
            _hdfc_statement("Your HDFC Bank - Diners Black Credit Card Statement - Sep-2026", None, 70),
            _hdfc_statement("Your HDFC Bank - Diners Black Metal Credit Card Statement - November-2026",
                            "0036XXXXXXXXXX13_17-11-2026_544.pdf", 2)]
    cards, _, _ = run(msgs)
    assert sorted((c.last4, c.product) for c in cards) == [("3254", "PhonePe Ultimo"),
                                                         ("6113", "Diners Club Black Metal")]


def test_sbi_ending_written_in_spaced_groups_is_read():
    assert extract.partial_last_digits("your SBI Credit Card ending with XXXX XXXX XXXX XX72. Please") == "72"


# ---------------------------------------------------------------- example numbers, roles, "ending 16"

SBI_EPIN = ("Dear SBI Cardholder,\nWelcome to the SBI Card family. To use your SBI Credit Card, you require a PIN.\n"
            "For example, if * Your Card number is 'XXXX-XXXX-XXXX-4321' * Your Date-of-Birth is 'XX-XX-1989' * "
            "Your Registered Mobile Number is 'XXXXXX3245' Then, your password will be - '432119893245'.")


def test_example_numbers_in_instructions_are_not_cards():
    # Before: SBI's e-PIN email made cards ••4321, ••1989 and ••3245 out of its password example.
    m = mail("sbi", "Important Information:e-PIN for your card no. ending 16", "Dear SBI Cardholder,", body=SBI_EPIN)
    _, _, [ev] = run([m])
    assert ev.last4s == [] and ev.last_digits == "16" and ev.name_match is None


def test_epin_email_joins_the_statement_card_with_the_same_ending():
    stmt = mail("sbi", "Your IRCTC SBI Platinum Card Monthly Statement -Oct 2026",
                "Attached is the monthly statement of your SBI Credit Card ending with XXXX XXXX XXXX XX16.", days_ago=5)
    epin = mail("sbi", "Important Information:e-PIN for your card no. ending 16", "Dear SBI Cardholder,",
                body=SBI_EPIN, days_ago=30)
    cards, _, _ = run([stmt, epin])
    assert [(c.partial, c.product, len(c.evidence)) for c in cards] == [("16", "IRCTC Platinum", 2)]


@pytest.mark.parametrize("greeting", ["Dear Cardholder,", "Dear SBI Cardholder,", "Dear Credit Cardholder,",
                                      "Dear Applicant,", "Dear Valued Customer,"])
def test_role_greetings_are_not_names(greeting):
    assert extract.holder_name(greeting + "\nYour statement is attached.") is None


def test_truncated_preview_does_not_make_the_bank_a_name():
    # Gmail cut the preview at "... HSBC Dear"; the body followed on the next line.
    assert extract.holder_name("Customer service hotline numbers HSBC Dear\n HSBC\n| Dear Cardholder, |") is None


def test_masked_phone_and_date_are_not_cards():
    assert extract.last4s("Your Registered Mobile Number is 'XXXXXX3245'") == []
    assert extract.last4s("Your Date-of-Birth is 'XX-XX-1989'") == []


def test_ending_decides_even_when_another_card_has_the_same_name():
    # SBI: ••7724 is an IRCTC Platinum; statements "IRCTC Platinum … ending XX72" are ••2872's.
    named = mail("sbi", "Your IRCTC SBI Platinum Card Monthly Statement -Jan 2023",
                 "Your SBI Credit Card ending 7724 statement.", days_ago=700)
    otp = mail("sbi", "Transaction alert on your SBI Card", "Card ending 2872 used for Rs 500.", days_ago=20)
    stmt = mail("sbi", "Your IRCTC SBI Platinum Card Monthly Statement -Oct 2026",
                "Your SBI Credit Card ending with XXXX XXXX XXXX XX72.", days_ago=25)
    cards, _, _ = run([named, otp, stmt])
    assert sorted((c.last4, c.product) for c in cards) == [("2872", "IRCTC Platinum"), ("7724", "IRCTC Platinum")]


def test_one_stray_advert_does_not_block_the_dominant_product():
    # Yes: 26 "ACE" statements and one stray "YES First" mention; ••6606 is the only Yes card.
    YES = dict(sender="YES BANK <alerts@yes.bank.in>", auth="mx.google.com; dkim=pass header.i=@yes.bank.in")
    alert = mail("hdfc", "Transaction alert", "Your YES BANK Credit Card ending 6606 was used.", days_ago=3, **YES)
    ace = [mail("hdfc", "Your YES_BANK_ACE Rupay Credit Card E-Statement", "Statement attached.", days_ago=30 * i, **YES)
           for i in range(1, 5)]
    stray = mail("hdfc", "Your YES First Credit Card statement", "Statement attached.", days_ago=200, **YES)
    cards, _, _ = run([alert] + ace + [stray])
    assert ("6606", "ACE") in {(c.last4, c.product) for c in cards}
    assert not any(c.last4 is None and c.product == "ACE" for c in cards)


def test_customer_number_in_an_app_activation_is_not_a_card():
    # Kotak: "Kotak Mobile Banking app has been activated ... CRN: xxxx3056" made a card ••3056.
    from carddiscovery import extract
    text = ("Dear Customer, Kotak Mobile Banking app has been activated after successful mobile number "
            "verification. Please verify below details: CRN: xxxx3056 Device Name: iPhone 12")
    assert extract.last4s(text) == []
    assert extract.last4s("Customer ID XXXX1234; your credit card XX5678") == ["5678"]


@pytest.mark.parametrize("fname,last4,product", [
    ("5241XXXXXXXX1014_332796_Retail_Sapphiro_NORM.pdf", "1014", "Sapphiro"),  # generic subject, logo-only PDF
    ("4315XXXXXXXX7008_793649_Retail_Amazon_NORM.pdf", "7008", "Amazon Pay"),   # code is the first word
    ("4315XXXXXXXX2222_1_Retail_Zzzunknown_NORM.pdf", "2222", None),            # unknown code: no guess
])
def test_icici_statement_file_name_gives_the_product(fname, last4, product):
    from datetime import datetime, timezone
    from carddiscovery.classify import analyze
    from carddiscovery.models import MessageMeta
    m = MessageMeta("i1", datetime.now(timezone.utc), "ICICI Bank <credit_cards@icici.bank.in>",
                    "ICICI Bank Credit Card Statement for the period October 16 2026 to November 15 2026",
                    "Credit Card Statement", auth_results="mx.google.com; dkim=pass header.i=@icici.bank.in",
                    body="Credit Card Statement. Payment due by November 30, 2026", attachment_names=(fname,))
    ev = analyze(m, REG, [], [])
    assert ev.type == T.STATEMENT and ev.last4s == [last4] and ev.product == product


# Found in the second study pass (2026-11): numbers the engine took for card endings.
@pytest.mark.parametrize("text, card", [
    # HDFC RuPay credit card UPI alert: the masked number after "Paid to" is the payee.
    ("Rs.250.00 has been debited from your RuPay Credit Card (ending 3254) Paid to ******1763 on 12-10-26.", "3254"),
    ("Rs.99.00 debited from your RuPay Credit Card (ending 3254) Paid to: ************8444", "3254"),
    # HDFC payment advice: an account number written as "account number ending with".
    ("Payment of INR 5,000 on reference No FT2082 for account number ending with XXXXXXXX4585", None),
    # Kotak NPS contribution: "NPS Account ending with" is a pension account.
    ("This will reflect in your NPS Account ending with XXXXXXXX6163", None),
    # Debit cards are never credit cards, wherever they appear.
    ("Your Axis Bank Debit card for A/c no. XX8466 has been dispatched", None),
    ("Your renewed Kotak Bank Debit Card ending XX8913 is on its way", None),
    ("transaction is completed for your Kotak Bank Debit Card XX8263", None),
])
def test_payee_account_and_debit_numbers_are_not_cards(text, card):
    assert extract.last4s(text) == ([card] if card else [])


def test_credit_card_numbers_near_those_words_are_still_kept():
    assert extract.last4s("Payment received for your credit card XX1234, paid to HDFC Bank") == ["1234"]
    assert extract.last4s("Rs 500 debited from your credit card ending 5511") == ["5511"]
    assert extract.last4s("Your Card account ending 41005") == ["1005"]  # Amex calls cards accounts


def test_approval_emails_alone_do_not_put_a_card_in_the_wallet():
    # An approval says a card was granted, not that it reached the person: repeats don't add up.
    approved = [mail("icici", "Congratulations! Your Sapphiro Credit Card is approved",
                     "Dear Shubham, your ICICI Bank Sapphiro Credit Card has been approved.", days_ago=d)
                for d in (20, 18, 15)]
    cards, _, ev = run(approved)
    [c] = cards
    assert all(e.type == T.APPROVAL for e in ev) and c.ownership < 0.9
    assert group(c) == "review"
    assert any("approval" in s for s in explain(c, REG).concerns)


# Patterns the shadow Haiku fallback found in mail the rules left "unknown" (2026-11). Typed from
# the subject, so the scan also opens them for the card number. Each names *your* card.


@pytest.mark.parametrize("sender, subject, snippet, expected", [
    (KOTAK, "Merchant credit refund alert on your Kotak Credit Card", "Rs 1419 refunded to card xx8687", T.PAYMENT),
    (KOTAK, "Important Update: Delivery status of your Kotak Bank Credit Card", "Card xx8687", T.DISPATCH_ACTIVATION),
    (KOTAK, "Generate Your Kotak Credit Card PIN Anytime, Anywhere", "for card xx8687", T.DISPATCH_ACTIVATION),
    (HSBC, "PIN Reset done for your HSBC Credit Card ending with 3408", "", T.DISPATCH_ACTIVATION),
    (HSBC, "We have received credits on your HSBC credit card", "ending 3408", T.PAYMENT),
    (None, "Card Control Setting Confirmation", "your HDFC Bank Credit Card xxxx7091", T.DISPATCH_ACTIVATION),
])
def test_haiku_found_patterns_are_typed_by_the_rules(sender, subject, snippet, expected):
    _, _, [e] = run([mail("hdfc", subject, snippet, **(sender or {}))])
    assert e.type == expected


def test_a_pin_reset_promo_sent_to_everyone_is_still_not_a_card_event():
    # "Reset your Credit Card PIN through Mobile Banking": no card, no number; Haiku agreed.
    _, _, [e] = run([mail("hdfc", "Reset your Credit Card PIN through Mobile Banking", "Download the app",
                          **HSBC)])
    assert e.type != T.DISPATCH_ACTIVATION
