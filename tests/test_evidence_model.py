"""The shadow evidence model: card-level combination by event family, cost-based thresholds,
and that scoring an email never changes what the rules decide."""

from __future__ import annotations

import pytest

from carddiscovery import evidence_model as em
from carddiscovery.models import Evidence, EvidenceType as T
from carddiscovery.score import group

from .test_pipeline import REG, mail, monthly_statements, run

MODEL = {"version": "t", "costs": {"wrong_wallet": 100, "missed": 10, "ask": 1},
         "relevance": {"intercept": -1.0, "weights": {"credit_card_S": 2.0, "masked_number": 2.0}},
         "ownership": {"intercept": -2.0, "weights": {"masked_number": 3.0, "statement_S": 2.0,
                                                      "application_SP": -2.0, "offer_SP": -1.0}}}


def test_thresholds_come_from_costs():
    assert em.thresholds({"wrong_wallet": 100, "missed": 10, "ask": 1}) == {"wallet": 0.99, "ask": 0.1}
    # Questions that cost more push the ask bar up; it never passes the wallet bar.
    assert em.thresholds({"wrong_wallet": 100, "missed": 10, "ask": 5})["ask"] == 0.5
    t = em.thresholds({"wrong_wallet": 10, "missed": 10, "ask": 50})
    assert t["ask"] == t["wallet"]


def test_copies_of_one_event_count_once_but_different_events_add_up():
    offers = [{"p_own": 0.6, "event": "weak"} for _ in range(10)]
    events = [{"p_own": 0.6, "event": e} for e in ("statement", "transaction", "dispatch")]
    assert em.card_confidence(offers)[0] == pytest.approx(0.6)
    assert em.card_confidence(events)[0] == pytest.approx(1 - 0.4 ** 3)


def test_applications_alone_wait_and_never_add_confidence():
    d = em.decide([{"p_own": 0.9, "event": "application"}] * 3, MODEL)
    assert d["decision"] == "wait" and d["confidence"] == 0.0


def test_statements_reach_the_wallet_and_an_offer_alone_does_not():
    strong = [{"p_own": 0.95, "event": "statement"}, {"p_own": 0.9, "event": "transaction"}]
    assert em.decide(strong, MODEL)["decision"] == "wallet"
    assert em.decide([{"p_own": 0.4, "event": "weak"}], MODEL)["decision"] == "ask"
    assert em.decide([{"p_own": 0.05, "event": "weak"}], MODEL)["decision"] == "hidden"
    assert em.decide([], MODEL) is None


def test_weak_mail_alone_never_reaches_the_wallet():
    # One card-control or offer email showing a number scores ~1.0: still only a question.
    d = em.decide([{"p_own": 1.0, "event": "weak"}], MODEL)
    assert d["decision"] == "ask" and d["confidence"] == 1.0
    assert em.decide([{"p_own": 1.0, "event": "weak"}, {"p_own": 0.99, "event": "statement"}],
                     MODEL)["decision"] == "wallet"


def test_event_family_from_engine_type_and_subject():
    assert em.event_of(T.DISPATCH_ACTIVATION, "Your card is on its way") == "dispatch"
    assert em.event_of(T.DISPATCH_ACTIVATION, "Green PIN generated") == "pin"
    assert em.event_of(T.STATEMENT) == "statement" and em.event_of(T.OFFER_EXISTING_CARD) == "weak"
    assert em.event_of(T.APPROVAL) == "application"


def test_template_id_ignores_numbers_and_months():
    a = em.template_id("HDFC <alerts@hdfcbank.net>", "Statement for card 6113, March 2026")
    b = em.template_id("alerts@hdfcbank.net", "Statement for card 3254, April 2025")
    assert a == b and len(a) == 12


def test_an_offered_product_is_not_a_held_one():
    subject = "Apply for the HDFC Bank Diners Club Black Credit Card"
    parts = {"subject": subject, "preview": "", "body": ""}
    assert [c for _, _, c in em.product_contexts(mail("hdfc", subject), REG, "hdfc", parts)] == ["offer"]


def test_shadow_is_stored_and_never_changes_the_rules_group(monkeypatch):
    msgs = monthly_statements("hdfc", "HDFC Bank Millennia", "2846", 10, 3)
    monkeypatch.setattr(em, "load", lambda path=None: None)
    cards_off, _, ev_off = run(msgs)
    assert all(e.shadow is None for e in ev_off)
    monkeypatch.setattr(em, "load", lambda path=None: MODEL)
    cards_on, _, ev_on = run(msgs)
    assert all(e.shadow and 0 <= e.shadow["p_own"] <= 1 and e.shadow["event"] == "statement" for e in ev_on)
    assert Evidence.from_dict(ev_on[0].to_dict()).shadow == ev_on[0].shadow
    assert [group(c) for c in cards_on] == [group(c) for c in cards_off]


def test_a_broken_model_never_breaks_reading_mail(monkeypatch):
    monkeypatch.setattr(em, "load", lambda path=None: {"version": "x"})  # no weights: scoring raises
    _, _, ev = run([mail("hdfc", "Your HDFC Bank Credit Card Statement", "card ending 2846")])
    assert ev[0].shadow is None and ev[0].type == T.STATEMENT
