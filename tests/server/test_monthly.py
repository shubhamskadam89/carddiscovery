"""CD_SCAN_MODE=monthly: month-by-month subjects, then incomplete cards' statements, then lifecycle."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from carddiscovery.models import Evidence, EvidenceType as T, MessageMeta
from carddiscovery.registry import Registry
from cdserver import engine, jobs
from cdserver.gmailx import Meter
from tests.server.fakes import MeteredFake

NOW = datetime.now(timezone.utc)
REG = Registry.load()


def test_month_windows_are_back_to_back_newest_first():
    ws = engine.month_windows(date(2026, 11, 20), 3)
    assert ws[0][1] == date(2026, 11, 21) and all(a[0] == b[1] for a, b in zip(ws, ws[1:]))
    assert ws[-1][0] == date(2026, 11, 20) - timedelta(days=365 * 3)    # reaches back exactly 3 years
    sizes = [(b - a).days for a, b in ws]
    assert sizes[1:12] == [30] * 11 and max(sizes[12:-1]) == 90       # 12 months one by one, then 3 at a time
    assert len(ws) == 21                                               # was 37 one-month windows


def _m(i, subject, body, days, sender="HDFC Bank <alerts@hdfcbank.net>"):
    return MessageMeta(f"m{i}", NOW - timedelta(days=days), sender, subject, body[:80],
                       auth_results="mx.google.com; dkim=pass header.i=@hdfcbank.net", body=body)


def test_monthly_scan_reads_card_subjects_only_window_by_window(c, account):
    box = [_m(1, "Rs.500 debited via Credit Card **1111", "Credit Card ending 1111", 5),
           _m(2, "Your HDFC Bank Millennia Credit Card Statement", "card ending 2222", 70),
           _m(3, "Your HDFC Bank annual fee", "fee for card ending 3333", 40),        # not a card subject
           _m(4, "Your savings account update", "nothing", 10)]
    a = account()
    fake = MeteredFake(box, Meter(10_000))
    seen = []
    engine.windowed_subject_scan(fake, REG, engine.EvidenceStore(c, a), engine.month_windows(date.today(), 1),
                                 engine.MONTHLY_SUBJECTS, names=[], on_window=lambda a_, b: seen.append(a_))
    known = engine.EvidenceStore(c, a).known()
    assert set(known) == {"m1", "m2"}                 # the fee mail waits for the lifecycle pass
    assert fake.meter.calls.get("full", 0) == 0       # subjects and previews only
    assert len(seen) == 13                            # every month reported, so the app can update


def test_lifecycle_pass_finds_what_card_subjects_missed(c, account):
    box = [_m(3, "Your HDFC Bank annual fee", "fee for card ending 3333", 400)]
    a = account()
    engine.windowed_subject_scan(MeteredFake(box, Meter(10_000)), REG, engine.EvidenceStore(c, a),
                                 engine.year_windows(date.today(), 3), engine.LIFECYCLE_SUBJECTS, names=[])
    assert [tuple(e.last4s) for e in engine.EvidenceStore(c, a).known().values()] == [("3333",)]


def test_statement_step_can_take_one_month_at_a_time():
    known = {f"s{d}": Evidence(message_id=f"s{d}", date=NOW - timedelta(days=d), bank_id="hsbc", type=T.STATEMENT,
                               product="Live+", products=["Live+"], dkim_pass=True) for d in (5, 40, 75)}
    month = (date.today() - timedelta(days=60), date.today() - timedelta(days=30))
    gaps = jobs.statement_gaps(known, {}, [], window=month)
    assert [e.message_id for _, e in gaps] == ["s40"]


def test_subjects_saying_card_without_credit_are_found(c, account):
    sbi = "SBI Card <onlinesbicard@sbicard.com>"
    box = [MessageMeta("o1", NOW - timedelta(days=4), sbi, "Transaction Alert from BPCL Octane Card",
                       "Dear Cardholder, Rs.1500 spent on your SBI Credit Card ending 8022 at petrol",
                       auth_results="mx.google.com; dkim=pass header.i=@sbicard.com",
                       body="Rs.1500 spent on your SBI Credit Card ending 8022 at petrol")]
    a = account()
    engine.windowed_subject_scan(MeteredFake(box, Meter(10_000)), REG, engine.EvidenceStore(c, a),
                                 engine.month_windows(date.today(), 1)[:1], engine.MONTHLY_SUBJECTS, names=[])
    ev = engine.EvidenceStore(c, a).known()["o1"]
    assert ev.bank_id == "sbi" and ev.product == "BPCL Octane"


def test_offers_outside_the_promotions_tab_are_not_read(c, account):
    # Real IndusInd/HDFC mail: offers filed under Updates, with "card" in the subject.
    box = [_m(51, "Congratulations! Your HDFC Bank Credit Card is eligible for an increased Credit Limit.", "x", 3),
           _m(52, "Update Your Insurance Nominee for Your HDFC Bank Credit Card", "x", 4),
           _m(53, "FLAT 50% discount at Starbucks with your HDFC Bank Credit Card", "x", 5),
           _m(54, "Rs.500 debited via Credit Card **1111", "Credit Card ending 1111", 6),
           MessageMeta("c1", NOW - timedelta(days=7), "SBI Card <Statements@sbicard.com>",
                       "Your CASHBACK SBI Card Monthly Statement", "card ending 4321",
                       auth_results="mx.google.com; dkim=pass header.i=@sbicard.com", body="card ending 4321")]
    a = account()
    engine.windowed_subject_scan(MeteredFake(box, Meter(10_000)), REG, engine.EvidenceStore(c, a),
                                 engine.month_windows(date.today(), 1)[:1], engine.MONTHLY_SUBJECTS, names=[])
    known = engine.EvidenceStore(c, a).known()
    assert set(known) == {"m54", "c1"}            # the three offers were never read (20 units each)
    assert known["c1"].product == "Cashback"     # a card named with an offer word is still read
    assert "cashback" not in engine.promo_words(REG) and "eligible" in engine.promo_words(REG)


def test_a_subject_read_often_is_skipped_then_rechecked(c, account):
    alerts = [_m(100 + d, "Transaction alert on your HDFC Bank credit card", "INR 50 spent", d)
              for d in range(1, 400, 7)]
    a = account()
    fake = MeteredFake(alerts, Meter(100_000))
    engine.windowed_subject_scan(fake, REG, engine.EvidenceStore(c, a), engine.month_windows(date.today(), 1),
                                 engine.MONTHLY_SUBJECTS, names=[])
    read = fake.meter.calls.get("metadata", 0)
    # Read in the first months until the template repeats, then only in the recheck month.
    assert read < len(alerts) // 2
    skipping = [q for q in fake.searches if "transaction alert on your hdfc bank credit card" in q.exclude_subject]
    assert skipping and len(skipping) < len(fake.searches)
