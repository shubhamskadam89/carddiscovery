"""Cards known by one half only (number without name, or name without number) get the other
half from mail the subject scan skips, offers included, when the offer names the held card
with its number: "your HSBC Visa Platinum Credit Card ending with 9719"."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from carddiscovery.classify import analyze
from carddiscovery.models import EvidenceType as T, MessageMeta
from carddiscovery.registry import Registry
from carddiscovery.resolve import resolve_full
from cdserver import engine
from cdserver.gmailx import Meter
from tests.server.fakes import MeteredFake

NOW = datetime.now(timezone.utc)
REG = Registry.load()
AUTH = "mx.google.com; dkim=pass header.i=@hsbc.co.in"


def _m(i, subject, body, days, sender="HSBC India <alerts@mail.hsbc.co.in>", labels=()):
    return MessageMeta(f"h{i}", NOW - timedelta(days=days), sender, subject, body[:90], auth_results=AUTH,
                       body=body, labels=labels)


PROMO = _m(1, "Unlock a voucher with spends on your HSBC VISA Platinum credit card!",
           "From everyday purchases to bigger spends, make them count with your HSBC Visa Platinum Credit Card. "
           "Make 5 transactions of INR 1000 or more with your HSBC Visa Platinum Credit Card ending with 9719 "
           "and get a INR 250 voucher.", 50, sender="HSBC Credit Cards <cmp@custcomm.hsbc.co.in>",
           labels=("CATEGORY_PROMOTIONS",))
ALERT = _m(2, "You have used your HSBC Credit Card ending with 9719 for a purchase transaction",
           "We write to confirm that your Credit card no ending with 9719 has been used for INR 2624.40", 150)
# As in a real mailbox: two HSBC cards, each known by halves. Statements name the product but
# not the number (it's in the locked PDF); alerts give the number but not the product.
STATEMENTS = [_m(10 + k, "Your HSBC Credit Card statement (HSBC PLATINUM)",
                 "Dear Cardholder, your monthly statement is attached.", 120 + 30 * k) for k in range(3)]
LIVE = [_m(20 + k, "Your HSBC Credit Card statement (HSBC LIVE+ CREDIT CARD)",
           "Dear Cardholder, your monthly statement is attached.", 14 + 30 * k) for k in range(3)]
ALERT_3408 = _m(30, "We have received credits on your HSBC credit card",
                "We have received credits of Rs 1231.00 on your HSBC credit card ending with 3408", 12)
HALVES = STATEMENTS + LIVE + [ALERT, ALERT_3408]


def _ev(msg):
    return analyze(MessageMeta(**vars(msg)), REG, [], [])


def test_offer_naming_the_held_card_with_its_number_gives_its_product():
    ev = _ev(PROMO)
    assert ev.type == T.OFFER_EXISTING_CARD and ev.held
    assert ev.product == "Visa Platinum" and ev.last4s == ["9719"]


def test_offer_selling_another_card_does_not_name_the_held_one():
    ev = _ev(_m(3, "Upgrade offer for you", "Upgrade your Credit Card ending with 9719 to the HSBC Premier "
                "Credit Card today. Limited period offer.", 20, labels=("CATEGORY_PROMOTIONS",)))
    assert not ev.held


def _hsbc(evs):
    return sorted(((c.product, c.last4) for c in resolve_full(evs).cards if c.bank_id == "hsbc"), key=str)


def test_four_halves_become_two_cards_with_the_offer():
    evs = [_ev(m) for m in HALVES]
    assert len(_hsbc(evs)) == 4                    # Live+, Visa Platinum, ••3408, ••9719
    # The offer pairs Visa Platinum with ••9719; Live+ and ••3408 are then the only ones left.
    assert _hsbc(evs + [_ev(PROMO)]) == [("Live+", "3408"), ("Visa Platinum", "9719")]


def test_lookup_reads_offers_for_the_missing_half(c, account):
    a = account()
    store = engine.EvidenceStore(c, a)
    # What the subject scan already holds: statements by name, an alert by number (header only).
    store.save([_ev(MessageMeta(**{**vars(m), "body": None})) for m in HALVES])
    fake = MeteredFake(HALVES + [PROMO], Meter(10_000))
    done = []
    st = engine.half_card_lookup(fake, REG, store, date.today() - timedelta(days=365), names=[],
                                 on_card=lambda *x: done.append(x))
    assert all(ok for *_, ok in done) and len(done) == 4
    assert _hsbc(list(store.known().values())) == [("Live+", "3408"), ("Visa Platinum", "9719")]
    # One find settled all four: the searches stop once nothing is left half-known.
    assert st.searches <= 2 and st.full_reads <= engine.HALF_READS


def test_nothing_to_look_up_costs_nothing(c, account):
    a = account()
    fake = MeteredFake([], Meter(10_000))
    st = engine.half_card_lookup(fake, REG, engine.EvidenceStore(c, a), date.today(), names=[])
    assert st.searches == 0 and fake.meter.units == 0
