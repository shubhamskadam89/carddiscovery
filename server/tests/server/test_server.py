from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from carddiscovery.models import MessageMeta
from carddiscovery.registry import Registry
from cdserver import config, engine, pdf, queue
from cdserver.gmailx import BudgetExhausted, Meter
from cdserver.vault import LocalKms, Vault
from cdserver.sim.pdfgen import make_pdf
from tests.server.fakes import MeteredFake

ME = "me@example.com"
AUTH = "mx.google.com; dkim=pass header.i=@icicibank.com"


def msg(i, subject, body, days_ago=10):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return MessageMeta(message_id=f"m{i}", date=when, sender="ICICI Bank <cc@icicibank.com>", subject=subject,
                       snippet=body[:120], body=body, to=ME, auth_results=AUTH)


# ---------------------------------------------------------------- vault

def test_api_role_can_seal_but_not_open(tmp_path):
    kms = LocalKms(tmp_path / "k")
    blob = Vault("api", kms).seal(b"refresh-token", "gmail:1")
    with pytest.raises(PermissionError):
        Vault("api", kms).open(blob, "gmail:1")
    assert Vault("worker", kms).open(blob, "gmail:1") == b"refresh-token"
    with pytest.raises(Exception):
        Vault("worker", kms).open(blob, "gmail:2")  # sealed for another user: refuses


# ---------------------------------------------------------------- meter

def test_meter_refuses_calls_over_budget():
    m = Meter(budget=100)
    m.charge("metadata", 4)
    assert m.affordable("metadata") == 1
    with pytest.raises(BudgetExhausted):
        m.charge("metadata", 2)
    assert m.units == 80


def test_meter_waits_instead_of_exceeding_the_per_minute_limit(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("cdserver.gmailx.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("cdserver.gmailx.time.sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    m = Meter(budget=10_000, per_minute=100)
    m.charge("metadata", 5)          # 100 units: the minute is used up
    m.charge("metadata")             # must wait for the window to roll
    assert m.waited_s >= 59
    assert m.units == 120


# ---------------------------------------------------------------- queue

def test_enqueue_twice_is_one_job(c, account):
    a = account()
    assert queue.enqueue(c, "discover_quick", a)
    assert queue.enqueue(c, "discover_quick", a) is None


def test_two_workers_never_take_the_same_job(database, c, account):
    for n in range(20):
        queue.enqueue(c, "discover_quick", account(f"user-{n}"))
    taken: list[int] = []

    def worker(name):
        conn = database.connect()
        while (row := queue.claim(conn, name)):
            taken.append(row["id"])
            queue.finish(conn, row["id"])
        conn.close()

    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(taken) == list(range(1, 21))


def test_one_running_job_per_user(c, account):
    a = account()
    queue.enqueue(c, "discover_quick", a)
    queue.enqueue(c, "daily_sync", a)
    first = queue.claim(c, "w1")
    assert first["kind"] == "discover_quick"
    assert queue.claim(c, "w2") is None  # the same user's quota is busy
    queue.finish(c, first["id"])
    assert queue.claim(c, "w2")["kind"] == "daily_sync"


def test_paused_job_resumes_and_stuck_job_is_reaped(c, account):
    a = account()
    queue.enqueue(c, "discover_full", a)
    row = queue.claim(c, "w1")
    queue.resume_later(c, row["id"], {"paused": "budget"}, delay_s=0)
    again = queue.claim(c, "w1")
    assert again["id"] == row["id"] and again["attempts"] == 1
    c.execute("UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id = %s", (row["id"],))
    assert queue.reap_stale(c) == 1


def test_background_jobs_wait_when_the_daily_quota_is_nearly_used(c, account):
    c.execute("INSERT INTO quota_daily (day, units) VALUES (current_date, %s)", (int(config.PROJECT_UNITS_PER_DAY * 0.95),))
    a, b = account(), account("user-2")
    queue.enqueue(c, "discover_full", a)
    queue.enqueue(c, "discover_quick", b)
    assert queue.claim(c, "w")["kind"] == "discover_quick"
    assert queue.claim(c, "w") is None


# ---------------------------------------------------------------- engine

def _box():
    return [
        msg(1, "Your ICICI Bank Coral Credit Card Statement", "Statement for card ending 1111. Total due Rs 5,000"),
        msg(2, "Welcome to ICICI Bank Sapphiro Credit Card", "Your Sapphiro Credit Card ending 2222 is dispatched"),
        *[msg(10 + n, "Transaction alert", f"INR {n}0 spent on ICICI Bank Credit Card XX1111", days_ago=n + 1)
          for n in range(30)],
    ]


def test_quick_scan_reads_at_most_40_headers_and_no_full_emails(c, account):
    a = account()
    reg = Registry.load()
    fake = MeteredFake(_box(), Meter(config.BUDGETS["discover_quick"]))
    st = engine.scan(fake, reg, engine.EvidenceStore(c, a), engine.quick_plan(reg, date.today()), names=[],
                     max_reads=config.QUICK_MAX_READS, allow_full_reads=False, interleave=True)
    assert st.full_reads == 0 and st.header_reads <= 40
    assert fake.meter.units <= config.BUDGETS["discover_quick"]
    keys = {e.last4s[0] for e in engine.EvidenceStore(c, a).known().values() if e.last4s}
    assert {"1111", "2222"} <= keys


def test_full_scan_out_of_budget_saves_progress_and_resumes(c, account):
    a = account()
    reg = Registry.load()
    store = engine.EvidenceStore(c, a)
    plan = engine.full_plan(reg, date.today(), 3)
    with pytest.raises(BudgetExhausted):
        engine.scan(MeteredFake(_box(), Meter(budget=len(plan) * 5 + 2 + 10 * 20)), reg, store, plan, names=[])
    first = len(store.known())
    assert 0 < first < 32
    st = engine.scan(MeteredFake(_box(), Meter(budget=15_000)), reg, store, plan, names=[])
    assert st.skipped_known == first  # the rerun only reads what's left
    assert len(store.known()) == 32


def test_full_scan_opens_emails_the_quick_scan_could_only_skim(c, account):
    """The quick scan reads headers only. A card whose number is only in the body
    must still be found by the full scan, without paying for the headers twice."""
    a = account()
    reg = Registry.load()
    box = [msg(1, "Welcome to the ICICI Bank family", "Dear customer, we are delighted to have you with us.", 5)]
    box[0].body = "Your Sapphiro Credit Card ending 4730 has been dispatched."
    store = engine.EvidenceStore(c, a)
    engine.scan(MeteredFake(box, Meter(1000)), reg, store, engine.quick_plan(reg, date.today()), names=[],
                max_reads=40, allow_full_reads=False, interleave=True)
    assert not store.known()["m1"].last4s
    fake = MeteredFake(box, Meter(15_000))
    st = engine.scan(fake, reg, store, engine.full_plan(reg, date.today(), 3), names=[])
    assert st.reopened == 1 and fake.meter.calls.get("metadata", 0) == 0
    assert store.known()["m1"].last4s == ["4730"]


# ---------------------------------------------------------------- statement PDFs

STATEMENT = ["HDFC Bank Millennia Credit Card Statement", "Card Number: XXXX XXXX XXXX 2846",
             "Statement Date: 05 Nov 2026", "Opening Balance: Rs. 4,000.00", "Payments / Credits: Rs. 4,000.00",
             "Purchases / Debits: Rs. 9,412.00", "Total Amount Due: Rs. 9,412.00", "Minimum Amount Due: Rs. 470.00",
             "Credit Limit: Rs. 1,50,000", "Available Credit Limit: Rs. 1,40,588", "CashPoints Balance: 471"]


def test_locked_statement_opens_with_the_bank_pattern_and_reconciles():
    dob = date(1998, 3, 14)
    data = make_pdf(STATEMENT, password="SHUB1403")
    text = pdf.open_pdf(data, pdf.candidates("hdfc", "Shubham Kadam", dob, ["2846"]))
    f = pdf.parse(text)
    assert (f.last4, f.total_due, f.credit_limit, f.available_limit, f.reward_points) == \
           ("2846", 9412.0, 150000.0, 140588.0, 471.0)
    assert f.statement_date == date(2026, 11, 5) and f.reconciled is True


def test_wrong_password_asks_the_user_and_bad_totals_are_flagged():
    with pytest.raises(pdf.PasswordNeeded):
        pdf.open_pdf(make_pdf(STATEMENT, password="XXXX0000"), ["SHUB1403"])
    bad = [l.replace("9,412.00", "9,999.00") if l.startswith("Total") else l for l in STATEMENT]
    assert pdf.parse(pdf.open_pdf(make_pdf(bad), [])).reconciled is False


# ---------------------------------------------------------------- card-aware scan pieces

def test_number_forms_are_learned_from_mail_and_never_include_a_full_number():
    text = "Card 4375XXXXXXXX2846 used. Also XX2846, ending 2846, Rs 2846 and 4375123412342846."
    assert engine.learn_number_tokens(text, ["2846"]) == {"2846": {"4375XXXXXXXX2846", "XX2846"}}
    terms = engine.number_terms("hdfc", "2846", ["4375XXXXXXXX2846", "xx2846"])
    assert terms[:3] == ["2846", "XX2846", "XXXX2846"] and terms.count("XX2846") == 1
    assert "4375XXXXXXXX2846" in terms
    assert "41005" in engine.number_terms("amex", "1005")


def test_windows_cover_the_lookback_newest_first_without_gaps():
    for schedule, count in (((6,), 11), ((3,), 21), ((1,), 61), ((1, 2, 3, 6, 12), 9)):
        ws = engine.windows(date(2026, 11, 16), 5, schedule)
        assert ws[0][1] == date(2026, 11, 17) and ws[-1][0] == date(2021, 11, 1)
        assert all(a[0] == b[1] for a, b in zip(ws, ws[1:]))  # each window ends where the next starts
        assert len(ws) == count, schedule


def test_a_card_only_ever_in_someone_elses_name_is_hidden(c, account):
    a = account()
    store = engine.EvidenceStore(c, a)
    from carddiscovery.models import Evidence, EvidenceType as ET
    now = datetime.now(timezone.utc)
    ev = lambda i, l4, nm: Evidence(message_id=f"e{i}", date=now - timedelta(days=30 * i), bank_id="hdfc",
                                    type=ET.STATEMENT, last4s=[l4], product="Millennia", dkim_pass=True,
                                    name_match=nm)
    store.save([ev(1, "2846", True), ev(2, "2846", None), ev(3, "9988", False), ev(4, "9988", False)])
    from cdserver import cards
    out = cards.rebuild(c, a, Registry.load())
    shown = {r["last4"] for r in c.execute("SELECT last4 FROM cards WHERE account_id = %s", (a,))}
    assert shown == {"2846"} and out["hidden"] == 1
