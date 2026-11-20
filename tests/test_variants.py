"""Product families and reading the variant from the same email (layers 1 and 2).

When an email only matches a family's bare name ("Regalia"), the variant is taken from
that email if exactly one is written there loosely, outside offer/upgrade wording.
"""

from __future__ import annotations

import pytest

from carddiscovery.classify import analyze
from carddiscovery.models import Evidence
from carddiscovery.registry import Registry

from .test_pipeline import REG, USER, by_last4, mail, run


def ev(subject, snippet, bank="hdfc"):
    return analyze(mail(bank, subject, snippet), REG, [USER], ["Shubham Kadam"])


# ---------------------------------------------------------------- layer 1: families

def test_families_come_from_product_names():
    hdfc = REG.families("hdfc")
    assert sorted(hdfc["Regalia"]) == ["Regalia First", "Regalia Gold"]
    assert hdfc["MoneyBack"] == ["MoneyBack+"]
    assert "Millennia" not in hdfc  # no variants: not a family


def _bank(**families):
    cfg = {"name": "X", "domains": ["x.com"], "products": {"Alpha": [], "Alpha Gold": [], "Alpha Lite": []}}
    if families:
        cfg["families"] = families
    return Registry.from_banks({"x": cfg})


def test_family_can_be_overridden_or_switched_off():
    assert sorted(_bank().families("x")["Alpha"]) == ["Alpha Gold", "Alpha Lite"]
    assert _bank(Alpha=["Alpha Gold"]).families("x")["Alpha"] == ["Alpha Gold"]
    assert "Alpha" not in _bank(Alpha=[]).families("x")


def test_family_naming_an_unknown_product_is_refused():
    with pytest.raises(ValueError, match="unknown products"):
        _bank(Alpha=["Alpha Platinum"])


# ---------------------------------------------------------------- layer 2: reading the variant

@pytest.mark.parametrize("subject,snippet", [
    ("Regalia Gold - your statement is ready", "Your statement for your Regalia Credit Card ending 1234 is ready."),
    ("Your Regalia Credit Card statement", "REGALIA-GOLD statement for card ending 1234."),
    ("Your Regalia Credit Card statement", "Statement for card ending 1234. Benefits of Regalia (Gold) apply."),
])
def test_variant_written_loosely_is_read(subject, snippet):
    e = ev(subject, snippet)
    assert e.product == "Regalia Gold" and e.products == ["Regalia Gold"] and e.variant_loose


def test_variant_alias_is_read_too():
    e = ev("MoneyBack Plus: statement ready", "Your MoneyBack Credit Card ending 1234 statement.")
    assert e.product == "MoneyBack+" and e.variant_loose


def test_longest_variant_wins_over_the_one_inside_it():
    e = ev("Emeralde Private Metal - your statement", "Statement for your Emeralde Credit Card ending 1234.",
           bank="icici")
    assert e.product == "Emeralde Private Metal"


@pytest.mark.parametrize("snippet", [
    "Statement for card ending 1234 is ready.",                                   # no variant written
    "Card ending 1234. Your card will be migrated to Regalia Gold next month.",   # moving to, not held
    "Card ending 1234. You are eligible for Regalia Gold.",                       # offered
])
def test_bare_name_kept_when_the_variant_is_not_clearly_held(snippet):
    e = ev("Your Regalia Credit Card statement", snippet)
    assert e.product == "Regalia" and not e.variant_loose


def test_bare_name_kept_when_two_variants_are_written():
    e = ev("Regalia Gold and Regalia First: lounge update", "Your Regalia Credit Card ending 1234 statement.")
    assert e.product == "Regalia" and not e.variant_loose


def test_upgrade_mail_is_left_as_written():
    e = ev("Your card has been upgraded",
           "Your Regalia Credit Card ending 1111 has been upgraded. Regalia Gold benefits now apply.")
    assert e.product == "Regalia" and not e.variant_loose


def test_full_alias_match_is_not_marked_loose():
    e = ev("Your Regalia Gold Credit Card statement", "Statement for card ending 1234.")
    assert e.product == "Regalia Gold" and not e.variant_loose


def test_flag_survives_storage():
    e = ev("Regalia Gold - your statement is ready", "Your Regalia Credit Card ending 1234 statement.")
    assert Evidence.from_dict(e.to_dict()).variant_loose is True
    assert Evidence.from_dict({k: v for k, v in e.to_dict().items() if k != "variant_loose"}).variant_loose is False


# ---------------------------------------------------------------- the card's product

def _statement(product_phrase, days_ago, last4="4321", bank="hdfc"):
    return mail(bank, f"Your {product_phrase} Credit Card Statement",
                f"Dear Shubham Kadam, your statement for card ending {last4} is ready.", days_ago=days_ago)


def test_newer_bare_name_does_not_undo_the_variant():
    # Before: shown as "Regalia" because the newest statements only said "Regalia".
    msgs = [_statement("HDFC Bank Regalia Gold", 90), _statement("HDFC Bank Regalia", 60),
            _statement("HDFC Bank Regalia", 30)]
    cards, _, _ = run(msgs)
    assert cards[0].product == "Regalia Gold"


def test_fullest_name_wins_through_nested_variants():
    msgs = [_statement("ICICI Bank Emeralde Private Metal", 90, bank="icici"),
            _statement("ICICI Bank Emeralde Private", 60, bank="icici"),
            _statement("ICICI Bank Emeralde", 30, bank="icici")]
    cards, _, _ = run(msgs)
    assert cards[0].product == "Emeralde Private Metal"


def test_product_change_email_resets_the_names():
    # An upgrade email to the bare name is a real change; the Gold seen before it no longer counts.
    msgs = [_statement("HDFC Bank Regalia Gold", 300, last4="1111"),
            mail("hdfc", "Your card has been upgraded",
                 "Your Regalia Gold Credit Card ending 1111 has been upgraded to Regalia Credit Card.",
                 days_ago=200),
            _statement("HDFC Bank Regalia", 100, last4="1111")]
    cards, _, _ = run(msgs)
    assert cards[0].product == "Regalia"


def test_unrelated_newer_name_still_wins():
    msgs = [_statement("HDFC Bank Millennia", 90), _statement("HDFC Bank Regalia", 30)]
    cards, _, _ = run(msgs)
    assert cards[0].product == "Regalia"


def test_card_gets_the_variant_from_its_statements():
    # Before: shown as "Regalia" because no statement had "card" next to "Regalia Gold".
    msgs = [mail("hdfc", "Regalia Gold - your statement is ready",
                 "Dear Shubham Kadam, your statement for your Regalia Credit Card ending 4321 is ready.",
                 days_ago=10 + 30 * i) for i in range(3)]
    cards, _, _ = run(msgs)
    assert by_last4(cards)["4321"].product == "Regalia Gold"
