"""The study-set tooling: who may be stored, the store's encryption and delete-by date, the five
labels, the decision trace and where phrases are found. Made-up emails only."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from carddiscovery.registry import Registry
from cdserver import corpus

REG = Registry.load()
TRUTH = {"hdfc": {"last4": ["6113"], "products": ["Diners Club Black Metal"]},
         "axis": {"last4": ["5630"], "products": ["MY Zone"]}}


def mail(i, sender, subject, body, labels=("INBOX", "CATEGORY_UPDATES"), unsub=False, files=()):
    return corpus.Mail(id=f"x{i}", strata=["evidence"], sender=sender, to="me@example.com", subject=subject,
                       date="2026-11-01T10:00:00+00:00", labels=list(labels),
                       auth="mx.google.com; dkim=pass header.i=@" + sender.split("@")[1].rstrip(">"),
                       unsubscribe=unsub, snippet=body[:150], body=body, attachments=list(files))


HDFC = "HDFC Bank <alerts@hdfcbank.net>"
AXIS = "Axis Bank <statements@axis.bank.in>"


def test_only_allowed_accounts_can_be_stored(monkeypatch, tmp_path):
    monkeypatch.setenv("CARDDISCOVERY_DEV", "1")
    monkeypatch.setenv("CD_CORPUS_ACCOUNTS", "7")
    with pytest.raises(SystemExit):
        corpus.Store(8, root=tmp_path)
    monkeypatch.delenv("CARDDISCOVERY_DEV")
    with pytest.raises(SystemExit):
        corpus.Store(7, root=tmp_path)


def test_the_store_is_encrypted_and_has_a_delete_by_date(monkeypatch, tmp_path):
    from cdserver.vault import LocalKms, Vault
    monkeypatch.setenv("CARDDISCOVERY_DEV", "1")
    monkeypatch.setenv("CD_CORPUS_ACCOUNTS", "7")
    s = corpus.Store(7, root=tmp_path, vault=Vault("worker", LocalKms(tmp_path / "k")))
    s.start("me@example.com")
    m = mail(1, HDFC, "Rs.500 debited via Credit Card **6113", "Credit Card ending 6113 at SWIGGY")
    s.put(m)
    raw = (tmp_path / "7" / "x1.bin").read_bytes()
    assert b"SWIGGY" not in raw and b"6113" not in raw                 # sealed, not plain text
    assert s.get("x1").subject == m.subject
    meta = json.loads((tmp_path / "7" / "meta.json").read_text())
    assert meta["delete_by"] == (date.today() + timedelta(days=corpus.KEEP_DAYS)).isoformat()
    meta["delete_by"] = (date.today() - timedelta(days=1)).isoformat()   # past due: nothing is read
    (tmp_path / "7" / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(SystemExit):
        list(s.all())
    assert s.delete() == 2 and not (tmp_path / "7").exists()


@pytest.mark.parametrize("m,expected", [
    (mail(1, HDFC, "Rs.500 debited via Credit Card **6113", "Credit Card ending 6113 at SWIGGY"),
     corpus.CONFIRMED),
    (mail(2, HDFC, "Your HDFC Bank - Diners Club Black Metal Credit Card Statement", "Your statement is attached"),
     corpus.CONFIRMED),
    # Two from a second mailbox: neither is about a credit card.
    (mail(3, AXIS, "Axis Bank Statement : Money Quotient for March 2026",
          "Money Quotient, a smart and interactive account statement. Your Liberty account fees will be revised."),
     corpus.NOT),
    (mail(4, "IDFC FIRST Bank <alwaysyoufirst@emailer.idfcfirstbank.com>", "Income Tax Act 2025 Update",
          "An important regulatory update on outward remittance transactions. Form 15CA is now Form 145."),
     corpus.NOT),
    (mail(5, HDFC, "Get 10% cashback with your credit card", "Limited period offer. T&C apply.",
          labels=("INBOX", "CATEGORY_PROMOTIONS"), unsub=True), corpus.PROMO),
    (mail(6, HDFC, "Your credit card statement is ready", "Your credit card statement for October: total amount "
          "due Rs 4,200, minimum amount due Rs 210."), corpus.LIKELY),
])
def test_five_labels_come_from_what_the_email_says(m, expected):
    assert corpus.label(m, REG, TRUTH)[0] == expected


def test_trace_says_which_rule_fired_and_why_a_number_was_dropped():
    m = mail(7, "Kotak <bankalerts@kotak.bank.in>", "Alert! iPhone 12 activated for Kotak Mobile App",
             "Kotak Mobile Banking app has been activated. Please verify: CRN: xxxx3056 Device Name: iPhone 12")
    t = corpus.trace(m, REG, [])
    assert t["path"][0][0] == "non_credit_words" and t["type"] == "not_credit_card"
    assert {n["digits"]: n["why"] for n in t["numbers"]}["3056"] == "identifier"


def test_phrases_are_tagged_with_where_they_appear():
    m = mail(8, AXIS, "Your Axis Bank Credit Card Statement", "Your credit card statement is attached.\n"
             "This e-mail is confidential. Do not reply. Credit card terms and conditions apply.",
             files=("Credit Card Statement.pdf",))
    f = corpus.features(m, owner=set())
    assert "subject:credit card statement" in f and "body:credit card statement" in f
    assert "file:credit card statement" in f and "sender:statements" in f
    # "Do not reply" lines are footer; a T&C line isn't (yet): the report shows which footer words matter.
    assert "footer:reply" in f and "body:reply" not in f
