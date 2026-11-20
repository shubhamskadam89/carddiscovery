"""Provenance (which rules read each email) and the set-aside log (emails about a
card we have that can't say which one)."""

from __future__ import annotations

from carddiscovery import rules
from carddiscovery.models import Evidence, EvidenceType as T
from carddiscovery.resolve import resolve, resolve_full

from .test_pipeline import mail, monthly_statements, run


# ---------------------------------------------------------------- rules version

def test_every_email_is_stamped_with_the_rules_version():
    _, _, ev = run(monthly_statements("hdfc", "HDFC Bank Millennia", "1111", 10, 1))
    assert ev[0].rules_version == rules.active_version() and len(ev[0].rules_version) == 12
    assert Evidence.from_dict(ev[0].to_dict()).rules_version == ev[0].rules_version
    older = {k: v for k, v in ev[0].to_dict().items() if k != "rules_version"}
    assert Evidence.from_dict(older).rules_version is None  # rows from before this field


def test_rules_version_follows_the_rules():
    base = rules.source_bundle()
    changed = {**base, "banks": {**base["banks"], "hdfc": {**base["banks"]["hdfc"], "name": "HDFC"}}}
    assert rules.version_of(changed) != rules.version_of(base)
    try:
        rules.activate(changed)
        assert rules.active_version() == rules.version_of(changed)
    finally:
        rules.activate(base)
    assert rules.active_version() == rules.version_of(base)


# ---------------------------------------------------------------- set-aside log

def _two_ace_cards():
    return (monthly_statements("axis", "Axis Bank ACE", "1111", 10, 2)
            + monthly_statements("axis", "Axis Bank ACE", "2222", 12, 2))


def test_email_fitting_two_cards_is_logged_not_lost():
    approval = mail("axis", "Your Axis Bank ACE Credit Card is approved",
                    "Your ACE Credit Card has been approved", days_ago=400)
    _, _, ev = run([approval] + _two_ace_cards())
    r = resolve_full(ev)
    assert len(r.cards) == 2
    [s] = r.set_aside
    assert (s.message_id, s.bank_id, s.type, s.reason, s.candidates, s.last4) == \
        (approval.message_id, "axis", T.APPROVAL, "several_cards", 2, None)
    assert s.used_body is False  # header only: worth re-reading in full later


def test_email_with_a_shared_number_is_logged_not_lost():
    msgs = []
    for i in range(3):
        msgs.append(mail("hdfc", "Your HDFC Bank Millennia Credit Card Statement",
                         "Statement for card 4375XXXXXXXX4321.", days_ago=20 + 30 * i))
        msgs.append(mail("hdfc", "Your HDFC Bank Regalia Credit Card Statement",
                         "Statement for card 5241XXXXXXXX4321.", days_ago=25 + 30 * i))
    alert = mail("hdfc", "Transaction alert", "Rs 450 spent on your credit card XX4321.", days_ago=3)
    _, _, ev = run(msgs + [alert])
    [s] = resolve_full(ev).set_aside
    assert (s.message_id, s.reason, s.candidates, s.last4) == (alert.message_id, "shared_last4", 2, "4321")


def test_nothing_is_logged_in_the_normal_case():
    _, _, ev = run(monthly_statements("hdfc", "HDFC Bank Millennia", "1111", 10, 3))
    assert resolve_full(ev).set_aside == []


def test_resolve_still_returns_cards_and_applications():
    _, _, ev = run(_two_ace_cards())
    cards, apps = resolve(ev)
    assert len(cards) == 2 and apps == []
