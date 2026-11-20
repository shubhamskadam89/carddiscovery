"""Statement first: a statement PDF names the bank, the card number and the product at once."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from carddiscovery.models import Evidence, EvidenceType as T
from carddiscovery.registry import Registry
from cdserver import engine, jobs, pdf
from cdserver.sim.pdfgen import make_pdf

import pytest

NOW = datetime.now(timezone.utc)
REG = Registry.load()


@pytest.fixture(autouse=True)
def unlocked_test_pdfs(monkeypatch):
    # The PDFs built here have no password. Real HSBC statements need 6 digits of the card
    # number, so the step wouldn't download them at all (see test_a_pdf_we_cant_open_is_not_downloaded).
    monkeypatch.setitem(pdf.PATTERNS, "hsbc", [])

LIVE = ["HSBC Live+ Credit Card Statement", "Name: NIKHIL JAIN", "Card Number: XXXX XXXX XXXX 3408",
        "Statement Date: 05 Nov 2026", "Total Amount Due: Rs. 1,200.00"]
PLATINUM = ["HSBC Visa Platinum Credit Card Statement", "Name: NIKHIL JAIN", "Card Number: 4XXX XXXX XXXX 9719",
            "Upgrade to HSBC Live+ Credit Card and earn 10% cashback! Apply now.",  # an advert, not this card
            "Statement Date: 23 Oct 2026", "Total Amount Due: Rs. 502.00"]


class PdfGmail:
    """Only what the statement step calls: list a message's PDFs, fetch one. Charges the meter."""

    throttled, backoff_s = 0, 0.0  # read by the job's heartbeat

    def __init__(self, meter, pdfs: dict[str, bytes], mails: dict | None = None):
        self.meter, self.pdfs, self.fetched = meter, pdfs, []
        self.mails = mails or {}  # message id -> MessageMeta as a full read returns it

    def pdf_attachments(self, mid):
        return self.full_with_pdfs(mid)[1]

    def full_with_pdfs(self, mid):
        self.meter.charge("full")
        from carddiscovery.models import MessageMeta
        meta = self.mails.get(mid) or MessageMeta(mid, NOW, "", "", "")
        name = meta.attachment_names[0] if meta.attachment_names else "statement.pdf"
        return meta, ([(name, f"att-{mid}")] if mid in self.pdfs else [])

    def attachment(self, mid, att):
        self.meter.charge("attachment")
        self.fetched.append(mid)
        return self.pdfs[mid]


def ev(mid, typ, days, last4s=(), product=None):
    return Evidence(message_id=mid, date=NOW - timedelta(days=days), bank_id="hsbc", type=typ, last4s=list(last4s),
                    product=product, products=[product] if product else [], dkim_pass=True, used_body=True)


def setup(c, account, pdfs):
    a = account()
    c.execute("INSERT INTO gmail_connections (account_id, mailbox, token_blob, scopes, years, names, pdf_opt_in) "
              "VALUES (%s, 'me@example.com', '{}', '{x}', 3, '{Nikhil Jain}', true)", (a,))
    engine.EvidenceStore(c, a).save([
        ev("a1", T.TRANSACTION_ALERT, 10, ["3408"]), ev("a2", T.TRANSACTION_ALERT, 12, ["9719"]),
        ev("s1", T.STATEMENT, 14, product="Live+"),   # number-less Live+ statements
        ev("s2", T.STATEMENT, 44, product="Live+"),
        ev("s3", T.STATEMENT, 27),                    # names neither number nor product
    ])
    j = jobs.Job(c, {"id": 1, "account_id": a, "kind": "pdf_enrich", "payload": {}}, REG, None, "w")
    j._gmail = PdfGmail(j.meter, pdfs)
    return a, j


def hsbc_cards(c, a):
    return sorted((r["last4"], r["product"], r["grp"]) for r in
                  c.execute("SELECT last4, product, grp FROM cards WHERE account_id = %s AND bank_id = 'hsbc'", (a,)))


def test_statement_pdfs_name_both_hsbc_cards(c, account):
    # Before: ••3408 and ••9719 both "variant unknown" in review, Live+ statements set aside.
    a, j = setup(c, account, {"s1": make_pdf(LIVE), "s3": make_pdf(PLATINUM)})
    assert jobs.read_statement_gaps(j, "Nikhil Jain", None) == 2
    assert sorted(j.gmail.fetched) == ["s1", "s3"]          # newest of each group only; s2 never downloaded
    assert hsbc_cards(c, a) == [("3408", "Live+", "owned"), ("9719", "Visa Platinum", "owned")]


def test_a_settled_card_is_never_looked_up_again(c, account):
    a, j = setup(c, account, {"s1": make_pdf(LIVE), "s3": make_pdf(PLATINUM)})
    jobs.read_statement_gaps(j, "Nikhil Jain", None)
    # Next month's Live+ statement arrives: the group is already settled by ••3408's PDF.
    engine.EvidenceStore(c, a).save([ev("s0", T.STATEMENT, 1, product="Live+")])
    j2 = jobs.Job(c, {"id": 2, "account_id": a, "kind": "pdf_enrich", "payload": {}}, REG, None, "w")
    j2._gmail = PdfGmail(j2.meter, {"s0": make_pdf(LIVE)})
    assert jobs.read_statement_gaps(j2, "Nikhil Jain", None) == 0 and j2.gmail.fetched == []


def test_locked_pdf_is_recorded_and_not_downloaded_again(c, account):
    locked = make_pdf(PLATINUM, password="NIKH0101")  # needs a date of birth we don't have
    a, j = setup(c, account, {"s3": locked, "s1": make_pdf(LIVE)})
    jobs.read_statement_gaps(j, "Nikhil Jain", None)
    outcomes = {r["message_id"]: r["outcome"] for r in
                c.execute("SELECT message_id, outcome FROM statement_pdf_reads WHERE account_id = %s", (a,))}
    assert outcomes == {"s1": "read", "s3": "password"}
    assert c.execute("SELECT count(*) AS n FROM notifications WHERE account_id = %s AND kind = 'pdf_password'",
                     (a,)).fetchone()["n"] == 1
    j2 = jobs.Job(c, {"id": 2, "account_id": a, "kind": "pdf_enrich", "payload": {}}, REG, None, "w")
    j2._gmail = PdfGmail(j2.meter, {"s3": locked})
    jobs.read_statement_gaps(j2, "Nikhil Jain", None)
    assert "s3" not in j2.gmail.fetched


def test_statement_email_without_a_pdf_moves_on_to_the_next_one(c, account):
    a, j = setup(c, account, {"s2": make_pdf(LIVE)})  # newest Live+ email (s1) has no PDF attached
    jobs.read_statement_gaps(j, "Nikhil Jain", None)
    assert j.gmail.fetched == ["s2"]
    assert ("3408", "Live+", "owned") in hsbc_cards(c, a)


def test_statement_product_is_the_heading_not_the_advert():
    card = pdf.card_identity("\n".join(PLATINUM), REG, "hsbc")
    assert (card.last4s, card.product) == (["9719"], "Visa Platinum")


def test_statement_lists_add_on_cards_and_skips_account_numbers():
    text = "\n".join(["HDFC Bank Millennia Credit Card Statement", "Primary Card: XXXX XXXX XXXX 6113",
                      "Add-on Card: XXXX XXXX XXXX 2854", "Pay from your savings account no. XXXXXX7785"])
    assert pdf.card_identity(text, REG, "hdfc").last4s == ["6113", "2854"]


# ---------------------------------------------------------------- statement search, right after the quick scan

def test_hsbc_statement_subjects_name_their_products():
    from carddiscovery.classify import analyze
    from carddiscovery.models import MessageMeta
    for subject, product in [("Your HSBC Credit Card statement (HSBC PLATINUM)", "Visa Platinum"),
                             ("Your HSBC Credit Card statement (HSBC LIVE+ CREDIT CARD)", "Live+")]:
        m = MessageMeta("x", NOW, "HSBC India <creditcardstatement@mail.hsbc.co.in>", subject, "Dear customer",
                        auth_results="mx.google.com; dkim=pass header.i=@mail.hsbc.co.in")
        e = analyze(m, REG, [], [])
        assert (e.type, e.product) == (T.STATEMENT, product)


def test_statement_search_wants_attachments_from_every_bank_sender():
    from datetime import date
    plan = engine.statement_plan(REG, date(2026, 11, 19))
    assert any(q.senders == ("bank.in",) for q in plan)
    g = plan[0].to_gmail()
    # Gmail matches whole words: Yes writes "Credit Card E-Statement", SBI "Platinum Card Monthly Statement".
    assert ('subject:("credit card statement" OR "card statement" OR "card e-statement" OR "card estatement" '
            'OR "card monthly statement")') in g and "has:attachment" in g
    assert "after:2025/11/24" in g  # 12 months


def test_statements_job_runs_before_the_full_scan():
    from cdserver import config
    assert config.PRIORITY["statements"] < config.PRIORITY["discover_full"]
    assert "statements" in jobs.HANDLERS


def test_statements_of_a_fully_known_card_are_not_opened(c, account):
    a, j = setup(c, account, {"s1": make_pdf(LIVE), "s3": make_pdf(PLATINUM), "x1": make_pdf(LIVE)})
    store = engine.EvidenceStore(c, a)
    # Axis MY Zone ••5630 is known with number and product; its statements show only "XX30".
    myzone = Evidence(message_id="x0", date=NOW - timedelta(days=40), bank_id="axis", type=T.STATEMENT,
                      last4s=["5630"], product="MY Zone", products=["MY Zone"], dkim_pass=True, used_body=True)
    stmt = Evidence(message_id="x1", date=NOW - timedelta(days=5), bank_id="axis", type=T.STATEMENT,
                    product="MY Zone", products=["MY Zone"], last_digits="30", dkim_pass=True, used_body=True)
    store.save([myzone, stmt])
    jobs.read_statement_gaps(j, "Nikhil Jain", None)
    assert "x1" not in j.gmail.fetched                     # tied to ••5630 by its subject: no PDF needed
    assert sorted(j.gmail.fetched) == ["s1", "s3"]          # the HSBC gaps are still read


def test_every_job_kind_can_be_queued(c, account):
    from cdserver import config, queue
    a = account()
    for kind in jobs.HANDLERS:
        assert queue.enqueue(c, kind, a) is not None, kind
    assert set(jobs.HANDLERS) <= set(config.PRIORITY) and set(jobs.HANDLERS) <= set(config.BUDGETS)


def test_a_pdf_the_library_cannot_parse_is_recorded_not_retried(c, account):
    a, j = setup(c, account, {"s3": b"%PDF-1.4 not really a pdf", "s1": make_pdf(LIVE)})
    jobs.read_statement_gaps(j, "Nikhil Jain", None)  # must not raise
    outcomes = {r["message_id"]: r["outcome"] for r in
                c.execute("SELECT message_id, outcome FROM statement_pdf_reads WHERE account_id = %s", (a,))}
    assert outcomes == {"s1": "read", "s3": "unreadable"}


def test_unreadable_page_is_skipped_not_fatal(monkeypatch):
    from pypdf import PageObject
    real = PageObject.extract_text
    calls = {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Too many character widths: 257 > 256")
        return real(self, *a, **k)
    monkeypatch.setattr(PageObject, "extract_text", flaky)
    from pypdf import PdfWriter
    import io
    w = PdfWriter()
    for part in (make_pdf(["cover page"]), make_pdf(LIVE)):
        from pypdf import PdfReader
        w.append(PdfReader(io.BytesIO(part)))
    buf = io.BytesIO(); w.write(buf)
    assert "3408" in pdf.open_pdf(buf.getvalue(), [])


# ---------------------------------------------------------------- reading the right numbers (real-mail cases)

def test_sbi_statement_number_comes_from_the_password_that_opened_it(c, account):
    from datetime import date
    # SBI prints only 2 digits and locks the PDF with date of birth + that card's last 4.
    sbi = ["SBI Card PRIME Statement", "Credit Card Number: XXXX XXXX XXXX XX72", "Total Amount Due: Rs. 900.00"]
    a, j = setup(c, account, {})
    store = engine.EvidenceStore(c, a)
    store.save([Evidence(message_id="k1", date=NOW - timedelta(days=60), bank_id="sbi", type=T.TRANSACTION_ALERT,
                         last4s=["2872"], dkim_pass=True),
                Evidence(message_id="k2", date=NOW - timedelta(days=61), bank_id="sbi", type=T.TRANSACTION_ALERT,
                         last4s=["8022"], dkim_pass=True),
                Evidence(message_id="q1", date=NOW - timedelta(days=5), bank_id="sbi", type=T.STATEMENT,
                         product="PRIME", products=["PRIME"], dkim_pass=True, used_body=True)])
    j._gmail = PdfGmail(j.meter, {"q1": make_pdf(sbi, password="140319982872")})
    jobs.read_statement_gaps(j, "Nikhil Jain", date(1998, 3, 14))
    row = c.execute("SELECT data FROM evidence WHERE account_id = %s AND message_id = 'q1#pdf'", (a,)).fetchone()
    assert row["data"]["last4s"] == ["2872"] and row["data"]["product"] == "PRIME"


def test_masked_numbers_without_a_card_label_are_not_cards():
    # An Axis statement printed three other masked numbers: they became phantom cards.
    text = "\n".join(["Axis Bank MY Zone Credit Card Statement", "Card No: XXXX XXXX XXXX 5630",
                      "Ref XXXX9679 Payment received", "Txn XXXXXX9023 Amazon", "Loan a/c XXXX0464"])
    assert pdf.card_identity(text, REG, "axis").last4s == ["5630"]
    assert pdf.card_identity("Statement ending XX30\nXXXX9679 ref", REG, "axis", known={"5630"}).last4s == []


def test_bank_tax_statement_is_not_a_card_statement():
    from carddiscovery.classify import analyze
    from carddiscovery.models import MessageMeta
    m = MessageMeta("x", NOW, "Kotak Bank <bankalerts@kotak.bank.in>", "Your Annual Tax Statement for FY 2025-26 is ready",
                    "Your Annual Tax Statement is now available", auth_results="dkim=pass header.i=@kotak.bank.in")
    assert analyze(m, REG, [], []).type == T.NOT_CREDIT_CARD


def test_pdfs_read_by_an_older_reader_are_read_once_more(c, account):
    a, j = setup(c, account, {"s3": make_pdf(PLATINUM)})
    # An old reader's attempt and what it took from the PDF (a phantom number).
    c.execute("INSERT INTO statement_pdf_reads (account_id, message_id, bank_id, outcome, pdf_version) "
              "VALUES (%s, 's3', 'hsbc', 'read', 1)", (a,))
    engine.EvidenceStore(c, a).save([Evidence(message_id="s3#pdf", date=NOW, bank_id="hsbc", type=T.STATEMENT,
                                              last4s=["0464"], dkim_pass=True)])
    jobs.read_statement_gaps(j, "Nikhil Jain", None)
    assert "s3" in j.gmail.fetched
    row = c.execute("SELECT data FROM evidence WHERE account_id = %s AND message_id = 's3#pdf'", (a,)).fetchone()
    assert row["data"]["last4s"] == ["9719"]


# ---------------------------------------------------------------- settled by the email alone

def _mail(mid, sender, subject, attachment, days):
    from carddiscovery.models import MessageMeta
    return MessageMeta(mid, NOW - timedelta(days=days), sender, subject, "Your latest eStatement is attached.",
                       auth_results=f"mx.google.com; dkim=pass header.d={sender.split('@')[1].rstrip('>')}",
                       body="Your latest eStatement is attached with this mail.", attachment_names=(attachment,))


def _setup_bank(c, account, bank, evidence, mails, pdfs=None):
    a = account()
    c.execute("INSERT INTO gmail_connections (account_id, mailbox, token_blob, scopes, years, names, pdf_opt_in) "
              "VALUES (%s, 'me@example.com', '{}', '{x}', 3, '{Nikhil Jain}', true)", (a,))
    engine.EvidenceStore(c, a).save([Evidence(message_id=m, date=NOW - timedelta(days=d), bank_id=bank, type=t,
                                              last4s=list(l4), dkim_pass=True, used_body=True)
                                     for m, t, d, l4 in evidence])
    j = jobs.Job(c, {"id": 1, "account_id": a, "kind": "pdf_enrich", "payload": {}}, REG, None, "w")
    j._gmail = PdfGmail(j.meter, pdfs or {}, mails)
    return a, j


def _cards(c, a, bank):
    return sorted((r["last4"], r["product"]) for r in
                  c.execute("SELECT last4, product FROM cards WHERE account_id = %s AND bank_id = %s", (a, bank)))


def test_hdfc_pdf_file_name_and_subject_settle_cards_without_opening_the_pdf(c, account):
    # HDFC statement emails carry no number, but the PDF is named after the card: first 4 and
    # last 2 digits. With the product in the subject, ••6113 and ••3254 are named, no download.
    sender = "HDFC Bank Cards <Emailstatements.cards@hdfcbank.bank.in>"
    mails = {
        "d1": _mail("d1", sender, "Your HDFC Bank - Diners Black Metal Credit Card Statement - November-2026",
                    "0036XXXXXXXXXX13_17-11-2026_544.pdf", 2),
        "p1": _mail("p1", sender, "Your HDFC Bank - PhonePe Ultimo HDFC Bank Credit Card Statement - November-2026",
                    "6530XXXXXXXXXX54_17-11-2026_5.pdf", 2),
    }
    a, j = _setup_bank(c, account, "hdfc", [("a1", T.TRANSACTION_ALERT, 5, ["6113"]),
                                            ("a2", T.TRANSACTION_ALERT, 6, ["3254"]),
                                            ("d1", T.STATEMENT, 2, []), ("p1", T.STATEMENT, 2, [])], mails)
    jobs.read_statement_gaps(j, "Nikhil Jain", None)
    assert j.gmail.fetched == []  # no PDF downloaded
    assert _cards(c, a, "hdfc") == [("3254", "PhonePe Ultimo"), ("6113", "Diners Club Black Metal")]
    outcomes = {r["message_id"]: r["outcome"] for r in
                c.execute("SELECT message_id, outcome FROM statement_pdf_reads WHERE account_id = %s", (a,))}
    assert outcomes == {"d1": "email", "p1": "email"}


def test_yes_bank_product_written_with_underscores_names_the_card(c, account):
    sender = "YES BANK <estatement@yes.bank.in>"
    mails = {"y1": _mail("y1", sender, "Your YES_BANK_ACE Rupay Credit Card E-Statement",
                         "560066_1005130002833182-448.pdf", 3)}  # no card digits in this file name
    a, j = _setup_bank(c, account, "yes", [("a1", T.TRANSACTION_ALERT, 4, ["6606"]), ("y1", T.STATEMENT, 3, [])],
                       mails)
    jobs.read_statement_gaps(j, "Nikhil Jain", None)
    assert _cards(c, a, "yes") == [("6606", "ACE")]


def test_without_pdf_consent_the_email_still_settles_and_no_pdf_is_opened(c, account):
    sender = "HDFC Bank Cards <Emailstatements.cards@hdfcbank.bank.in>"
    mails = {"d1": _mail("d1", sender, "Your HDFC Bank - Diners Black Metal Credit Card Statement - November-2026",
                         "0036XXXXXXXXXX13_17-11-2026_544.pdf", 2),
             "x1": _mail("x1", sender, "Your HDFC Bank Credit Card Statement", "statement.pdf", 9)}
    a, j = _setup_bank(c, account, "hdfc", [("a1", T.TRANSACTION_ALERT, 5, ["6113"]), ("d1", T.STATEMENT, 2, []),
                                            ("x1", T.STATEMENT, 9, [])], mails, pdfs={"x1": make_pdf(LIVE)})
    jobs.read_statement_gaps(j, None, None, pdfs=False)
    assert j.gmail.fetched == []
    assert _cards(c, a, "hdfc") == [("6113", "Diners Club Black Metal")]
    outcomes = {r["message_id"]: r["outcome"] for r in
                c.execute("SELECT message_id, outcome FROM statement_pdf_reads WHERE account_id = %s", (a,))}
    assert outcomes == {"d1": "email", "x1": "checked"}
    # Once the user consents (and gives the name HDFC's password uses), the checked email's PDF is tried.
    j2 = jobs.Job(c, {"id": 2, "account_id": a, "kind": "pdf_enrich", "payload": {}}, REG, None, "w")
    j2._gmail = PdfGmail(j2.meter, {"x1": make_pdf(LIVE)}, mails)
    jobs.read_statement_gaps(j2, "Nikhil Jain", None, pdfs=True)
    assert j2.gmail.fetched == ["x1"]
    # Without the name, a PDF locked with it isn't even downloaded.
    j3 = jobs.Job(c, {"id": 3, "account_id": a, "kind": "pdf_enrich", "payload": {}}, REG, None, "w")
    j3._gmail = PdfGmail(j3.meter, {"x1": make_pdf(LIVE)}, mails)
    c.execute("DELETE FROM statement_pdf_reads WHERE account_id = %s", (a,))
    jobs.read_statement_gaps(j3, None, None, pdfs=True)
    assert j3.gmail.fetched == []


# ---------------------------------------------------------------- subjects-first scan (CD_SCAN_MODE=subjects)

def test_year_windows_cover_each_year_newest_first_without_gaps():
    from datetime import date
    ws = engine.year_windows(date(2026, 11, 20), 3)
    assert len(ws) == 3 and ws[0][1] == date(2026, 11, 21)
    assert all(a[0] == b[1] for a, b in zip(ws, ws[1:]))


def test_subject_scan_reads_subjects_only_and_runs_the_pdf_step_after_the_first_year(c, account):
    from datetime import date
    from carddiscovery.models import MessageMeta
    from cdserver.gmailx import Meter
    from tests.server.fakes import MeteredFake
    today = date.today()

    def m(i, subject, body, days):
        return MessageMeta(f"x{i}", NOW - timedelta(days=days), "HDFC Bank <alerts@hdfcbank.net>", subject, body[:80],
                           auth_results="mx.google.com; dkim=pass header.i=@hdfcbank.net", body=body)

    box = [m(1, "Alert: your HDFC Bank Credit Card ending 1111 was used", "Credit Card ending 1111 used", 20),
           m(2, "Your HDFC Bank Millennia Credit Card Statement", "statement for credit card ending 2222", 500),
           m(3, "Your savings account update", "nothing about cards", 30)]
    a = account()
    fake = MeteredFake(box, Meter(10_000))
    calls = []
    st = engine.subject_scan(fake, REG, engine.EvidenceStore(c, a), today, 3, names=[],
                             after_first_year=lambda: calls.append("pdf step"))
    known = engine.EvidenceStore(c, a).known()
    assert {tuple(e.last4s) for e in known.values()} == {("1111",), ("2222",)}
    assert "x3" not in known                          # not about a credit card: never read
    assert fake.meter.calls.get("full", 0) == 0       # subjects and previews only
    assert calls == ["pdf step"]                      # once, right after the first year


def test_a_pdf_we_cant_open_is_not_downloaded(c, account, monkeypatch):
    # The statement email says how it's locked: date of birth + the last 6 digits of the card.
    # Only the last 4 are ever known, so the PDF isn't downloaded (20 units saved per statement).
    from carddiscovery.models import MessageMeta
    monkeypatch.setitem(pdf.PATTERNS, "hsbc", ["{DDMMYY}{L6}"])
    rule = ("Your password is a combination of your date of birth (in DDMMYY format) followed by the last 6 digits "
            "of the primary credit card number.")
    a, j = setup(c, account, {"s1": make_pdf(LIVE), "s3": make_pdf(PLATINUM)})
    j._gmail.mails = {m: MessageMeta(m, NOW, "HSBC <creditcardstatement@mail.hsbc.co.in>",
                                     "Your HSBC Credit Card statement", rule, body=rule) for m in ("s1", "s3")}
    assert jobs.read_statement_gaps(j, "Nikhil Jain", NOW.date()) == 0
    assert j.gmail.fetched == []                                   # nothing downloaded
    assert j.dev.get("pdfs_not_downloaded", {}).get("hsbc") == 2
