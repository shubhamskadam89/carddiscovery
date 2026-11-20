"""The rare case of two different cards at one bank ending in the same 4 digits.

Identity stays bank + last4. Only when one number's mail conflicts (different leading
digits, or unrelated product names that keep alternating) is it split into two cards.
"""

from __future__ import annotations

from carddiscovery.explain import explain
from carddiscovery.models import Evidence
from carddiscovery.score import group

from .test_pipeline import REG, mail, run


def stmt(product, number, days_ago):
    return mail("hdfc", f"Your HDFC Bank {product} Credit Card Statement",
                f"Dear Shubham Kadam, your statement for card {number} is ready.", days_ago=days_ago)


def mail_ids(card):
    return {e.message_id for e in card.evidence}


def interleaved(a_product, a_number, b_product, b_number, months=4):
    msgs = []
    for i in range(months):
        msgs.append(stmt(a_product, a_number, 20 + 30 * i))
        msgs.append(stmt(b_product, b_number, 25 + 30 * i))
    return msgs


# ---------------------------------------------------------------- split by leading digits (certain)

def test_different_leading_digits_are_two_cards():
    # Before: one "Millennia" card; the Regalia card vanished.
    cards, _, _ = run(interleaved("Millennia", "4375XXXXXXXX4321", "Regalia", "5241XXXXXXXX4321"))
    by_twin = {c.twin: c for c in cards}
    assert set(by_twin) == {"4375", "5241"}
    assert by_twin["4375"].product == "Millennia" and by_twin["5241"].product == "Regalia"
    assert len(by_twin["4375"].evidence) == 4 and len(by_twin["5241"].evidence) == 4
    assert by_twin["4375"].decision_key != by_twin["5241"].decision_key
    assert all(group(c) == "owned" and "same_last4" not in c.flags for c in cards)
    assert explain(by_twin["4375"], REG).title == "HDFC Bank Millennia 4375••4321"


def test_two_cards_of_the_same_product_split_by_leading_digits():
    cards, _, _ = run(interleaved("Millennia", "4375XXXXXXXX4321", "Millennia", "5241XXXXXXXX4321"))
    assert sorted(c.twin for c in cards) == ["4375", "5241"]
    assert all(c.product == "Millennia" for c in cards)


def test_mail_without_leading_digits_goes_by_product_or_is_set_aside():
    msgs = interleaved("Millennia", "4375XXXXXXXX4321", "Regalia", "5241XXXXXXXX4321")
    named = stmt("Regalia", "XX4321", 5)  # product says which card
    alert = mail("hdfc", "Transaction alert", "Rs 450 spent on your credit card XX4321.", days_ago=3)
    cards, _, _ = run(msgs + [named, alert])
    by_twin = {c.twin: c for c in cards}
    assert named.message_id in mail_ids(by_twin["5241"])
    assert all(alert.message_id not in mail_ids(c) for c in cards)  # can't tell which: set aside
    assert len(cards) == 2


# ---------------------------------------------------------------- split by product names (likely)

def test_alternating_unrelated_products_are_two_cards_for_review():
    cards, _, _ = run(interleaved("Millennia", "ending 4321", "Regalia", "ending 4321"))
    by_twin = {c.twin: c for c in cards}
    assert set(by_twin) == {"Millennia", "Regalia"}
    assert all("same_last4" in c.flags and group(c) == "review" for c in cards)


# ---------------------------------------------------------------- the normal case is untouched

def test_product_switched_once_is_one_card():
    msgs = [stmt("Millennia", "ending 4321", d) for d in (200, 170, 140)]
    msgs += [stmt("Regalia", "ending 4321", d) for d in (100, 70, 40)]
    cards, _, _ = run(msgs)
    assert len(cards) == 1 and cards[0].product == "Regalia" and cards[0].twin is None


def test_one_stray_product_name_is_one_card():
    msgs = [stmt("Millennia", "ending 4321", d) for d in (150, 120, 60, 30)] + [stmt("Regalia", "ending 4321", 90)]
    cards, _, _ = run(msgs)
    assert len(cards) == 1 and cards[0].twin is None


def test_variants_of_one_product_alternating_is_one_card():
    cards, _, _ = run(interleaved("Regalia", "ending 4321", "Regalia Gold", "ending 4321"))
    assert len(cards) == 1 and cards[0].product == "Regalia Gold"


def test_shorter_leading_digits_of_the_same_number_are_one_card():
    cards, _, _ = run(interleaved("Millennia", "4XXXXXXXXXXX4321", "Millennia", "4375XXXXXXXX4321"))
    assert len(cards) == 1 and cards[0].twin is None


def test_alternating_names_with_an_upgrade_email_are_one_card():
    msgs = interleaved("Millennia", "ending 4321", "Regalia", "ending 4321")
    msgs.append(mail("hdfc", "Upgrade successful",
                     "Your card ending 4321 has been upgraded to Regalia Credit Card", days_ago=150))
    cards, _, _ = run(msgs)
    assert len(cards) == 1


def test_leading_digits_survive_storage():
    _, _, ev = run([stmt("Millennia", "4375XXXXXXXX4321", 10)])
    assert ev[0].number_prefixes == {"4321": ["4375"]}
    assert Evidence.from_dict(ev[0].to_dict()).number_prefixes == {"4321": ["4375"]}
    assert Evidence.from_dict({k: v for k, v in ev[0].to_dict().items() if k != "number_prefixes"}).number_prefixes == {}
