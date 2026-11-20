"""Statement PDFs: the password rule an email states, skipping PDFs we can't open, and the card
name read from the logo spot (OCR) where the PDF's text doesn't have it."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from carddiscovery.classify import analyze
from carddiscovery.models import EvidenceType as T, MessageMeta
from carddiscovery.registry import Registry
from cdserver import pdf

REG = Registry.load()

HSBC = ("Password protection: This statement is secured by a unique password assigned to you for your personal "
        "access only. Your password is a combination of your date of birth (in DDMMYY format) followed by the last "
        "6 digits of the primary credit card number. For example: If your primary credit card number is "
        "5120 0000 0000 4200 and your birth date is 2 June 1970, then the password is 020670004200")
ICICI = ("Access your e-Statement by entering the first 4 letters of your name as it appears on your card followed "
         "by Date of Birth in DDMM format. Example: Name on the Card: S Gupta Date of Birth: Jan 05, 1992 Password "
         "sgup0501 First 4 letters of your name on the card + Date of Birth in DDMM format (Enter all letters in "
         "lowercase without adding any special characters, spaces or salutation).")
AMEX = ("Please note this document is password protected. Here is how to open the attachment: Enter your date of "
        "birth in DDMMYYYY format. For example, if your date of birth is 01/11/1975, then the password would be "
        "01111975.")
SBI = ("The statement is password protected. The password is your date of birth (DDMMYYYY) followed by the last 4 "
       "digits of your card.")


# ICICI's payment line, just above its password instructions, mentions the card's last 4 digits.
ICICI_WITH_UPI = ("*Pay from any UPI-enabled app to the ICICI Bank UPI ID: ccpay.<10-digit registered mobile "
                  "number><last 4 digits of Credit Card number>@icici | Pre-approved Loans and Card offers | " + ICICI)


@pytest.mark.parametrize("text,rule", [(HSBC, "{DDMMYY}{L6}"), (ICICI, "{n4}{DDMM}"), (ICICI_WITH_UPI, "{n4}{DDMM}"),
                                       (AMEX, "{DDMMYYYY}"),
                                       (SBI, "{DDMMYYYY}{L4}"), ("Please find your statement attached.", None)])
def test_the_password_rule_is_read_from_the_email(text, rule):
    assert pdf.password_rule(text) == rule


def test_a_pdf_we_cant_open_isnt_worth_downloading():
    assert not pdf.can_open("{DDMMYY}{L6}", True, True, ["4471"])     # HSBC: never (6 digits)
    assert not pdf.can_open("{DDMMYYYY}{L4}", True, True, [])          # SBI without a known number
    assert pdf.can_open("{DDMMYYYY}{L4}", True, True, ["5512"])
    assert not pdf.can_open("{n4}{DDMM}", True, False, [])             # no date of birth given
    assert pdf.can_open("{n4}{DDMM}", True, True, [])


def test_the_rule_the_email_states_is_the_only_one_tried():
    keyed = pdf.keyed_candidates("icici", "Kirtish Jain", date(1996, 6, 16), [], "{n4}{DDMM}")
    assert [p for p, _ in keyed] == ["kirt1606"]
    assert pdf.keyed_candidates("hsbc", "A Name", date(1990, 1, 1), ["1234"], "{DDMMYY}{L6}") == []


def test_amex_approval_names_the_card():
    body = ("Congratulations, your application is approved. Dear Customer, Congratulations! Your application for "
            "the American Express Platinum Travel Credit Card has been approved. Your new Card should reach you "
            "within 3-7 business days. Privacy statement")
    m = MessageMeta("a", datetime.now(timezone.utc), "American Express <AmericanExpress@welcome.americanexpress.com>",
                    "Important update on the status of your application", body[:150],
                    auth_results="mx.google.com; dkim=pass header.i=@welcome.americanexpress.com", body=body)
    ev = analyze(m, REG, [], [])
    assert ev.type == T.APPROVAL and ev.bank_id == "amex" and ev.product == "Platinum Travel"


def test_a_rejection_is_not_read_as_approval():
    body = "We regret to inform you that your application for the Platinum Travel Credit Card has not been approved."
    m = MessageMeta("r", datetime.now(timezone.utc), "American Express <AmericanExpress@welcome.americanexpress.com>",
                    "Update on your application", body, body=body,
                    auth_results="mx.google.com; dkim=pass header.i=@welcome.americanexpress.com")
    assert analyze(m, REG, [], []).type == T.APPLICATION_REJECTED


needs_ocr = pytest.mark.skipif(pdf._ocr_tools() is None, reason="pdftoppm and tesseract not installed")


def _statement(header: str, password: str | None = None) -> bytes:
    from cdserver.sim.pdfgen import make_pdf
    return make_pdf([header, "", "CREDIT CARD STATEMENT", "Card Number 4315 XXXX XXXX 7008", "Total Amount Due 185.00"],
                    password)


CATALOG = sorted({p for _, p in REG.banks["icici"].product_patterns})


@needs_ocr
def test_ocr_reads_the_card_name_from_the_top_of_page_one():
    assert pdf.product_by_ocr(_statement("ICICI Bank   Sapphiro"), None, "icici", CATALOG) == "Sapphiro"


@needs_ocr
def test_ocr_opens_a_locked_pdf_with_the_password_that_worked():
    assert pdf.product_by_ocr(_statement("amazon pay   ICICI Bank", "kirt1606"), "kirt1606", "icici",
                              CATALOG) == "Amazon Pay"


@needs_ocr
def test_two_products_in_the_logo_spot_give_none():
    # An ad beside the logo ("Upgrade to Sapphiro") must not be taken for the card.
    assert pdf.product_by_ocr(_statement("Coral    Upgrade to Sapphiro"), None, "icici", CATALOG) is None


def test_no_ocr_for_banks_without_a_logo_spot():
    assert pdf.product_by_ocr(b"%PDF-1.4", None, "axis", ["MY Zone"]) is None
