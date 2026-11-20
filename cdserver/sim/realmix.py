"""A one-year test inbox shaped like a real Indian cardholder's mail (about 20 cards).

Same senders, subjects and wording as real bank mail seen in development, with made-up
numbers, amounts and merchants. It reproduces the cases that broke discovery on real mail:
  - HSBC statements name the product ("(HSBC LIVE+ CREDIT CARD)") but never the number (it's
    in a locked PDF); alerts give the number but never the product; only an offer in the
    Promotions tab says both ("your HSBC Visa Platinum Credit Card ending with 8823")
  - SBI subjects say "Transaction Alert from BPCL Octane Card" (no "credit"); the number is
    only in the body; a card and its add-on get twin alerts for one purchase
  - an IDFC card known only from CRED's bill emails ("your IDFC First Bank card XXXX-2296")
  - a card blocked mid-year ("Your SBI Card Account Deactivation")
  - HDFC statement PDFs named after the card ("0036XXXXXXXXXX40_...pdf"), Axis "ending XX30"
  - look-alikes that are not cards: Kotak "CRN: xxxx5056" in an app-activation email, a
    savings account, a debit card, SBI's e-PIN example numbers and mobile number, HSBC's
    password example "5120 0000 0000 4200", an HDFC EMI reference number
  - shops' order mail naming a card ("paid with card ending 4408") from non-bank senders

    python -m cdserver sim generate --mix real     # answer file only
    python -m cdserver sim import --mix real       # into the test inbox (Gmail import, nothing sent)

Deterministic for a seed. Uses Mail from generate.py, so import and clean work unchanged.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .generate import IST, PERSONA, Mail
from .pdfgen import make_pdf

TODAY = date(2026, 11, 20)
START = TODAY - timedelta(days=365)
DOB = date.fromisoformat(PERSONA["dob"])

UPDATES = ["INBOX", "CATEGORY_UPDATES"]
PERSONAL = ["INBOX", "CATEGORY_PERSONAL"]
PROMOS = ["INBOX", "CATEGORY_PROMOTIONS"]

MERCHANTS = ["AMAZON PAY INDIA", "SWIGGY", "ZOMATO", "BIGBASKET", "IRCTC ETICKETING", "UBER INDIA", "FLIPKART",
             "DMART", "STAR BAZAAR", "DISTRICT MOVIE TICKET", "APOLLO PHARMACY", "BHARAT PETROLEUM",
             "VAIKUNTH PETROLEUM", "CROMA", "MYNTRA", "MAKEMYTRIP", "AIRTEL PAYMENTS", "JIO RECHARGE"]


@dataclass
class Card:
    bank: str
    last4: str
    product: str | None              # canonical name the scan should end up with (None: mail never says)
    style: str                       # which bank's mail shapes to use
    title: str = ""                  # how the bank writes the card in subjects
    alerts_per_month: tuple[int, int] = (3, 8)
    statement_day: int = 5
    opened: date = START
    closed: date | None = None
    addon_of: str | None = None      # last4 of the primary: twin alerts with it
    full_number: str = ""            # made up; only used to lock the PDF the way the bank does
    group: str = "owned"
    note: str = ""

    def active(self, d: date) -> bool:
        return self.opened <= d and (self.closed is None or d < self.closed)


# Twenty cards, made-up numbers. The shapes are what matters.
CARDS = [
    Card("hsbc", "4471", "Live+", "hsbc", "HSBC LIVE+ CREDIT CARD", statement_day=6, full_number="4386280012344471",
         note="number only in alerts, product only in statements"),
    Card("hsbc", "8823", "Visa Platinum", "hsbc", "HSBC PLATINUM", (0, 2), statement_day=22,
         full_number="4213450056788823", note="number+product together only in a Promotions-tab offer"),
    Card("sbi", "5512", "BPCL Octane", "sbi", "BPCL Octane Card", (4, 9), statement_day=12, full_number="4726420011115512"),
    Card("sbi", "5520", None, "sbi", "BPCL Octane Card", (0, 0), addon_of="5512", group="any",
         note="add-on: twin alerts under the primary's card name"),
    Card("sbi", "3361", "IRCTC Platinum", "sbi", "IRCTC SBI Platinum Card", (1, 4), statement_day=24,
         full_number="4026210022223361"),
    Card("sbi", "9047", "IRCTC Platinum", "sbi", "IRCTC SBI Platinum Card", (1, 3), statement_day=24,
         closed=date(2026, 3, 18), group="any", note="blocked in March: deactivation email"),
    Card("hdfc", "6640", "Diners Club Black Metal", "hdfc", "Diners Club Black Metal", statement_day=17,
         full_number="3600361234566640", note="PDF named 3600XXXXXXXXXX40_..."),
    Card("hdfc", "2290", "PhonePe Ultimo", "hdfc", "PhonePe Ultimo", (2, 5), statement_day=3, full_number="4893770033332290"),
    Card("hdfc", "9911", "Regalia Gold", "hdfc", "Regalia Gold", (2, 4), statement_day=9, opened=date(2026, 9, 28),
         full_number="5228010044449911", note="new this year: welcome + dispatch"),
    Card("axis", "1130", "MY Zone", "axis", "MY Zone", (2, 6), statement_day=11, note="statement says ending XX30"),
    Card("axis", "7787", "Airtel", "axis", "Airtel", (1, 3), statement_day=15),
    Card("icici", "4408", "Amazon Pay", "icici", "Amazon Pay", (3, 7), statement_day=20),
    Card("icici", "1290", "Coral", "icici", "Coral", (0, 0), group="any", note="dormant: only an annual fee email"),
    Card("idfc", "2296", None, "cred_idfc", "", (0, 0), statement_day=25, note="only CRED bill emails"),
    Card("indusind", "6605", "Legend", "indusind", "Legend", (1, 3), statement_day=8),
    Card("rbl", "3092", "IndianOil XTRA", "rbl", "IndianOil XTRA", (1, 4), statement_day=14),
    Card("yes", "5506", "ACE", "yes", "YES_BANK_ACE", (1, 3), statement_day=19),
    Card("au", "1834", "Zenith", "au", "Zenith", (1, 3), statement_day=27),
    Card("federal", "7700", "Scapia", "federal", "Scapia", (2, 5), statement_day=2),
    Card("hsbc", "8823", "Visa Platinum", "hsbc_offer", "", (0, 0)),   # the Promotions-tab offers (not a card)
]

MUST_NOT_FIND = [
    {"bank": "kotak", "last4": "5056", "why": "customer number (CRN) in an app-activation email"},
    {"bank": "kotak", "last4": "4585", "why": "savings account"},
    {"bank": "hdfc", "last4": "5544", "why": "debit card"},
    {"bank": "hdfc", "last4": "9921", "why": "EMI request reference number"},
    {"bank": "sbi", "last4": "4321", "why": "example card number in e-PIN instructions"},
    {"bank": "sbi", "last4": "3245", "why": "registered mobile number"},
    {"bank": "hsbc", "last4": "4200", "why": "example card number in PDF password instructions"},
    {"bank": "*", "last4": "7319", "why": "card named only by a shop, not a bank"},
]


class Gen:
    def __init__(self, to: str, seed: int):
        self.to, self.r, self.mails, self.n = to, random.Random(seed), [], 0

    def add(self, when: datetime, sender: str, subject: str, text: str, *, labels=None, auth: str | None = None,
            html: bool = True, attachments=None, unsubscribe=False, kind="") -> None:
        self.n += 1
        body_html = None
        if html:
            paras = "".join(f"<p>{p}</p>" for p in text.split("\n\n"))
            body_html = (f"<html><body><table width='600'><tr><td style='font:13px Arial'>{paras}"
                         f"</td></tr></table></body></html>")
        self.mails.append(Mail(key=f"rm{self.n:05d}", when=when, sender=sender, subject=subject, text=text,
                               html=body_html, labels=list(labels or UPDATES), attachments=list(attachments or []),
                               auth=auth, unsubscribe=unsubscribe, kind=kind))

    def at(self, d: date, h0=8, h1=22) -> datetime:
        return datetime.combine(d, time(self.r.randint(h0, h1), self.r.randint(0, 59), self.r.randint(0, 59)), IST)

    def amt(self, lo=150, hi=9000) -> str:
        return f"{self.r.uniform(lo, hi):,.2f}"

    def merchant(self) -> str:
        return self.r.choice(MERCHANTS)


def _dom(addr: str) -> str:
    return addr.rsplit("@", 1)[1].rstrip(">")


def _months(a: date, b: date):
    y, m = a.year, a.month
    while date(y, m, 1) <= b:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _day(y: int, m: int, d: int) -> date:
    return date(y, m, min(d, 28))


# ---------------------------------------------------------------- per bank

HSBC_FOOT = ("Issued by The Hongkong and Shanghai Banking Corporation Limited, India. Incorporated in Hong Kong SAR "
             "with limited liability. This e-mail is confidential. Please do not reply back to this e-mail.")


def hsbc(g: Gen, c: Card) -> None:
    stmt, alerts, credits, otp = ("HSBC India <creditcardstatement@mail.hsbc.co.in>", "HSBC India <hsbc@mail.hsbc.co.in>",
                                  "HSBC India <alerts@mail.hsbc.bank.in>", "HSBC <hsbc@hsbc.bank.in>")
    pw = DOB.strftime("%d%m%y") + c.full_number[-6:]
    for y, m in _months(START, TODAY):
        sd = _day(y, m, c.statement_day)
        if START <= sd <= TODAY and c.active(sd):
            pdf = make_pdf(["HSBC", f"{c.title.title()}", f"Card number {c.full_number[:4]} XXXX XXXX {c.last4}",
                            f"Statement date {sd:%d %b %Y}", f"Total amount due Rs. {g.amt(800, 30000)}"], pw)
            g.add(g.at(sd, 3, 5), stmt, f"Your HSBC Credit Card statement ({c.title})",
                  "This e-mail has been sent by The Hongkong and Shanghai Banking Corporation Limited, India Branch, "
                  "52/60, Mahatma Gandhi Road, Fort, Mumbai - 400 001. Customer service hotline numbers HSBC\n\n"
                  "Dear Cardholder,\n\nThank you for your continued association with HSBC Credit Cards. Your monthly "
                  "statement is attached. Please refer to the guidelines below on how to access this statement in a "
                  "safe and simple manner.\n\nPassword protection: This statement is secured by a unique password "
                  "assigned to you for your personal access only. Your password is a combination of your date of "
                  "birth (in DDMMYY format) followed by the last 6 digits of the primary credit card number\n\n"
                  "For example: If your primary credit card number is 5120 0000 0000 4200 and your birth date is "
                  "2 June 1970, then the password is 020670004200\n\nYours sincerely,\nHSBC India\n\n" + HSBC_FOOT,
                  labels=PERSONAL, auth="pass:mail.hsbc.co.in", attachments=[(f"{sd:%Y%m%d}.pdf", pdf)],
                  kind="statement")
            pd = sd + timedelta(days=2)
            if pd <= TODAY:
                g.add(datetime.combine(pd, time(4, 47), IST), credits, "We have received credits on your HSBC credit card",
                      f"HSBC India Dear Customer, We have received credits of ₹ {g.amt(500, 20000)} on your HSBC credit "
                      f"card ending with {c.last4} on {pd:%d/%m/%Y}. The amount has been adjusted against your "
                      "outstanding balance.\n\nYours sincerely,\nHSBC India.\n\n" + HSBC_FOOT,
                      labels=PERSONAL, auth="pass:mail.hsbc.bank.in", kind="payment")
        for _ in range(g.r.randint(*c.alerts_per_month)):
            d = _day(y, m, g.r.randint(1, 28))
            if not (START <= d <= TODAY and c.active(d)):
                continue
            when, mer, a = g.at(d), g.merchant(), g.amt()
            g.add(when - timedelta(seconds=40), otp, "Your HSBC Credit Card OTP",
                  "This e-mail has been sent by The Hongkong and Shanghai Banking Corporation Limited, India Branch. "
                  f"Dear Customer, A One Time Password has been sent for your transaction of INR {a} at {mer}. "
                  "Do not share it with anyone.", labels=PERSONAL, auth="pass:hsbc.bank.in", kind="otp")
            g.add(when, alerts, f"You have used your HSBC Credit Card ending with {c.last4} for a purchase transaction",
                  f"Dear Customer,\n\nWe write to confirm that your Credit card no ending with {c.last4},has been used "
                  f"for INR {a} for payment to {mer} on {when:%d %b %Y} at {when:%H:%M}.\n\nIf you want to report "
                  "this as a fraud transaction and block your card, call 18002673456 or SMS 'BLOCK HSBC CC last 4 "
                  "digits of your card number' to 575750.\n\n(This is a system generated communication)",
                  labels=UPDATES, auth="pass:mail.hsbc.co.in", kind="alert")


def hsbc_offers(g: Gen, card: Card) -> None:
    """Offers in the Promotions tab: some name the held card with its number, most don't."""
    sender = "HSBC Credit Cards <cmpadh423_8051@custcomm.hsbc.co.in>"
    for k in range(5):
        d = TODAY - timedelta(days=50 + 3 * k)
        g.add(g.at(d, 9, 11), sender, "Unlock a voucher from Amazon, Myntra & more with spends on your HSBC VISA "
              "Platinum credit card!",
              "This e-mail has been sent by The Hongkong and Shanghai Banking Corporation Limited, India Branch, "
              "52/60, Mahatma Gandhi Road, Fort, Mumbai - 400 001. Customer service hotline numbers. If you cannot see "
              "this page properly, please click here\n\nFrom everyday purchases to bigger spends, make them count "
              "towards your next milestone with 5 transactions of INR 1000 or more each, with your HSBC Visa Platinum "
              f"Credit Card.\n\nMake 5 transactions of INR 1000 or more with your HSBC Visa Platinum Credit Card ending "
              f"with {card.last4} and get a INR 250 voucher* of your choice\n\nOffer period: 3 September 2026 to "
              "3 October 2026\n\n" + HSBC_FOOT, labels=PROMOS, auth="pass:custcomm.hsbc.co.in", unsubscribe=True,
              kind="offer_named")
    for k in range(10):
        d = START + timedelta(days=30 * k + 7)
        g.add(g.at(d, 9, 11), "HSBC Credit Cards <cmpadh423_7747@custcomm.hsbc.co.in>",
              "HSBC Spend Fest is here! Your spends can now unlock exciting rewards!",
              "If you cannot see this page properly, please click here\n\nSpend with your HSBC Credit Card and unlock "
              "iPhone, holiday voucher, amazon voucher and more on achieving your spend milestones.\n\n" + HSBC_FOOT,
              labels=PROMOS, auth="pass:custcomm.hsbc.co.in", unsubscribe=True, kind="promo")


SBI_HEAD = ("Having trouble viewing this e-mail? View this message in your browser SBI Card - App SBI Card "
            "TRANSACTION ALERT!\n\n")
SBI_FOOT = ("Never share your Card Number, CVV, PIN, OTP, Internet Banking User ID, Password or URN with anyone. "
            "Warm regards, SBI Card. This emailer is addressed to and intended only for SBI Credit Cardholder.")


def sbi(g: Gen, c: Card, all_cards: list[Card]) -> None:
    alerts, stmts = "SBI Card Transaction Alert <onlinesbicard@sbicard.com>", "SBI Card <Statements@sbicard.com>"
    addons = [a for a in all_cards if a.addon_of == c.last4]
    pw = DOB.strftime("%d%m%Y") + c.last4
    for y, m in _months(START, TODAY):
        sd = _day(y, m, c.statement_day)
        if START <= sd <= TODAY and c.active(sd) and c.addon_of is None:
            pdf = make_pdf(["SBI Card", c.title, f"Credit Card Number XXXX XXXX XXXX {c.last4}",
                            f"Statement Date {sd:%d %b %Y}", f"Total Amount Due Rs {g.amt(500, 25000)}"], pw)
            g.add(g.at(sd, 6, 8), stmts, f"Your {c.title} Monthly Statement - {sd:%b %Y}",
                  f"Dear Cardholder,\n\nPlease find attached your {c.title} statement for the period ending "
                  f"{sd:%d %b %Y} for your card ending with XXXX XXXX XXXX XX{c.last4[-2:]}.\n\nThe statement is "
                  "password protected. The password is your date of birth (DDMMYYYY) followed by the last 4 digits "
                  "of your card.\n\n" + SBI_FOOT, labels=UPDATES, auth="pass:sbicard.com",
                  attachments=[(f"{sd:%d%m%Y}_statement.pdf", pdf)], kind="statement")
        if c.addon_of:
            continue
        for _ in range(g.r.randint(*c.alerts_per_month)):
            d = _day(y, m, g.r.randint(1, 28))
            if not (START <= d <= TODAY and c.active(d)):
                continue
            when, mer, a = g.at(d), g.merchant(), g.amt()
            if g.r.random() < 0.5:
                g.add(when - timedelta(seconds=50), "onlinesbicard@sbicard.com",
                      f"One Time Password (OTP) for your online transaction at {mer[:10]}",
                      f"Dear Cardholder, The One Time Password (OTP) for your transaction at {mer[:10]} of INR {a} "
                      f"with your SBI Credit Card ending {c.last4} is {g.r.randint(100000, 999999)}. This OTP is "
                      "valid for 10 minutes or 1 successful attempt.", labels=UPDATES, auth="pass:sbicard.com",
                      html=False, kind="otp")
            # A card and its add-on: SBI sends the same alert for both, at the same second.
            for l4 in [c.last4] + [x.last4 for x in addons if g.r.random() < 0.5]:
                g.add(when, alerts, f"Transaction Alert from {c.title}",
                      SBI_HEAD + f"Dear Cardholder, This is to inform you that,Rs.{a} spent on your SBI Credit Card "
                      f"ending {l4} at {mer} on {when:%d/%m/%y}. Trxn. not done by you? Report at "
                      "https://sbicard.com/Dispute . If you have not authorized this transaction please contact the "
                      "SBI Card helpline.\n\n" + SBI_FOOT, labels=UPDATES, auth="pass:sbicard.com", kind="alert")
    if c.closed:
        g.add(g.at(c.closed), "SBI Card Fraud Control <blockalerts@sbicard.com>", "Your SBI Card Account Deactivation",
              f"Dear SBI Cardholder, As per your request, we have blocked your SBI Card ending {c.last4}. To reissue "
              "your SBI Card in future, please visit the SBI Card website or call our helpline number.",
              labels=UPDATES, auth="pass:sbicard.com", html=False, kind="closure")


def sbi_epin(g: Gen) -> None:
    d = START + timedelta(days=40)
    g.add(g.at(d), "SBI Card <onlinesbicard@sbicard.com>", "e-PIN for your SBI Card",
          "Dear Cardholder,\n\nThe e-PIN for your card no. ending 61 has been generated. To view the e-PIN, open the "
          "attachment using a password made of the last 4 digits of your Card number, your date of birth and the last "
          "4 digits of your mobile number.\n\nFor example, if your Card number is 'XXXX-XXXX-XXXX-4321', your "
          "Date-of-Birth 'XX-XX-1989', your Mobile Number 'XXXXXX3245', the password will be 432119893245.\n\n"
          "If you did not request it, call us from your registered mobile number XXXXXX3245 or SMS BLOCK to 5676791.",
          labels=UPDATES, auth="pass:sbicard.com", kind="trap")


def hdfc(g: Gen, c: Card) -> None:
    alerts, stmts = "HDFC Bank InstaAlerts <alerts@hdfcbank.net>", "HDFC Bank <Emailstatements.cards@hdfcbank.net>"
    pw = PERSONA["first"][:4].upper() + DOB.strftime("%d%m")
    if c.opened > START:
        g.add(g.at(c.opened), "HDFC Bank <cards@hdfcbank.net>", f"Welcome to the world of HDFC Bank {c.title} "
              "Credit Card", f"Dear Cardholder,\n\nWelcome! Your HDFC Bank {c.title} Credit Card ending {c.last4} "
              "has been approved and will be dispatched shortly.", labels=PERSONAL, auth="pass:hdfcbank.net",
              kind="welcome")
        g.add(g.at(c.opened + timedelta(days=3)), "HDFC Bank <cards@hdfcbank.net>",
              "Your HDFC Bank Credit Card has been dispatched", f"Your HDFC Bank Credit Card ending {c.last4} has been "
              "dispatched via Blue Dart. AWB 77812345.", labels=UPDATES, auth="pass:hdfcbank.net", kind="dispatch")
    for y, m in _months(START, TODAY):
        sd = _day(y, m, c.statement_day)
        if START <= sd <= TODAY and c.active(sd) and sd > c.opened + timedelta(days=20):
            pdf = make_pdf(["HDFC Bank Credit Cards", f"{c.title} Credit Card", f"Card No {c.full_number[:4]} XXXX "
                            f"XXXX {c.last4}", f"Statement Date {sd:%d/%m/%Y}", f"Total Dues {g.amt(500, 40000)}"], pw)
            name = f"{c.full_number[:4]}XXXXXXXXXX{c.last4[-2:]}_{sd:%d-%m-%Y}_{g.r.randint(100, 999)}.pdf"
            g.add(g.at(sd, 7, 9), stmts, f"Your HDFC Bank {c.title} Credit Card Statement for {sd:%B %Y}",
                  "Dear Cardholder,\n\nPlease find attached your credit card statement. The statement is password "
                  "protected: the first 4 letters of your name in capitals followed by your date of birth (DDMM).",
                  labels=UPDATES, auth="pass:hdfcbank.net", attachments=[(name, pdf)], kind="statement")
        for _ in range(g.r.randint(*c.alerts_per_month)):
            d = _day(y, m, g.r.randint(1, 28))
            if not (START <= d <= TODAY and c.active(d)):
                continue
            when, a = g.at(d), g.amt()
            g.add(when, alerts, f"Rs.{a} debited via Credit Card **{c.last4}",
                  f"Dear Customer, Rs.{a} is debited from your HDFC Bank Credit Card ending {c.last4} towards "
                  f"{g.merchant()} on {when:%d %b, %Y} at {when:%H:%M:%S}. If you did not authorize this transaction, "
                  "please report it immediately by calling 18002586161.", labels=UPDATES, auth="pass:hdfcbank.net",
                  kind="alert")


def hdfc_traps(g: Gen) -> None:
    for k in range(6):  # a debit card's alerts
        d = START + timedelta(days=45 * k + 3)
        a = g.amt(100, 3000)
        g.add(g.at(d), "HDFC Bank InstaAlerts <alerts@hdfcbank.net>", f"Rs.{a} debited via Debit Card **5544",
              f"Dear Customer, Rs.{a} has been debited from your HDFC Bank Debit Card ending 5544 at {g.merchant()}.",
              labels=UPDATES, auth="pass:hdfcbank.net", kind="trap")
    d = TODAY - timedelta(days=70)
    g.add(g.at(d), "HDFC Bank <cards@hdfcbank.net>", "Your EMI request has been registered",
          "Dear Customer, Your EMI request has been registered. Request reference no. XXXX9921. The EMI will reflect "
          "in your next credit card statement.", labels=UPDATES, auth="pass:hdfcbank.net", kind="trap")


def axis(g: Gen, c: Card) -> None:
    for y, m in _months(START, TODAY):
        sd = _day(y, m, c.statement_day)
        if START <= sd <= TODAY and c.active(sd):
            g.add(g.at(sd, 6, 8), "Axis Bank <cc.statements@axisbank.com>",
                  f"Your Axis Bank {c.title} Credit Card Statement ending XX{c.last4[-2:]}",
                  f"Dear Customer,\n\nYour Axis Bank {c.title} Credit Card statement for the card ending "
                  f"XX{c.last4[-2:]} has been generated. Total amount due: INR {g.amt(300, 20000)}.",
                  labels=UPDATES, auth="pass:axisbank.com", kind="statement")
        for _ in range(g.r.randint(*c.alerts_per_month)):
            d = _day(y, m, g.r.randint(1, 28))
            if START <= d <= TODAY:
                when, a = g.at(d), g.amt()
                g.add(when, "Axis Bank Alerts <alerts@axisbank.com>",
                      f"Transaction alert on Axis Bank Credit Card no. XX{c.last4}",
                      f"Transaction Amount: INR {a}\nMerchant Name: {g.merchant()}\nAxis Bank Credit Card No. "
                      f"XX{c.last4}\nDate & Time: {when:%d-%m-%Y, %H:%M:%S} IST", labels=UPDATES,
                      auth="pass:axisbank.com", kind="alert")


def icici(g: Gen, c: Card) -> None:
    if c.alerts_per_month == (0, 0):  # dormant: only the yearly fee
        d = date(2026, 3, 14)
        g.add(g.at(d), "ICICI Bank <credit_cards@icicibank.com>",
              f"Annual fee charged on your ICICI Bank {c.title} Credit Card",
              f"Dear Customer, An annual fee of Rs 499 + GST has been charged on your ICICI Bank {c.title} Credit Card "
              f"XX{c.last4}. Spend Rs 1,50,000 in a year to get it waived.", labels=UPDATES,
              auth="pass:icicibank.com", kind="fee")
        return
    for y, m in _months(START, TODAY):
        sd = _day(y, m, c.statement_day)
        if START <= sd <= TODAY:
            g.add(g.at(sd, 6, 8), "ICICI Bank <credit_cards@icicibank.com>",
                  f"ICICI Bank {c.title} Credit Card Statement for {sd:%B %Y}",
                  f"Dear Customer, your ICICI Bank {c.title} Credit Card statement is ready. Credit Card Number "
                  f"XXXX XXXX XXXX {c.last4}. Total amount due INR {g.amt(300, 20000)}.", labels=UPDATES,
                  auth="pass:icicibank.com", kind="statement")
        for _ in range(g.r.randint(*c.alerts_per_month)):
            d = _day(y, m, g.r.randint(1, 28))
            if START <= d <= TODAY:
                g.add(g.at(d), "ICICI Bank <alerts@icicibank.com>", "Transaction alert for your ICICI Bank Credit Card",
                      f"Dear Customer, Your ICICI Bank Credit Card XX{c.last4} has been used for a transaction of "
                      f"INR {g.amt()} on {d:%b %d, %Y} at {g.merchant()}.", labels=UPDATES,
                      auth="pass:icicibank.com", kind="alert")


def cred_idfc(g: Gen, c: Card) -> None:
    for y, m in _months(START, TODAY):
        sd = _day(y, m, c.statement_day)
        if START <= sd <= TODAY:
            g.add(g.at(sd, 6, 7), "CRED <protect@cred.club>",
                  f"Important : New statement for your IDFC First Bank card XXXX-{c.last4} is here",
                  f"{PERSONA['first']}, your credit card bill has been generated\n\nHere is your {sd:%B} Bill\n\n"
                  f"IDFC First Bank XXXX-XXXX-XXXX-{c.last4}\n\ntotal amount due {g.amt(100, 9000)}\nminimum amount "
                  f"due 100.00\ndue date {sd + timedelta(days=14):%B %d, %Y}\n\nPay now", labels=UPDATES,
                  auth="pass:cred.club", kind="app_statement")


def simple_bank(g: Gen, c: Card) -> None:
    """IndusInd, RBL, Yes, AU, Federal: a statement email a month with the number, a few alerts."""
    s = {"indusind": ("IndusInd Bank <creditcards@indusind.com>", "indusind.com",
                      f"IndusInd Bank {c.title} Credit Card Statement - {{m}}",
                      f"Dear Customer, your IndusInd Bank {c.title} Credit Card statement for card ending {c.last4} "
                      "is attached."),
         "rbl": ("RBL Bank <cardservices@rblbank.com>", "rblbank.com",
                 f"RBL Bank {c.title} Credit Card e-statement for {{m}}",
                 f"Dear Customer, the e-statement for your RBL Bank {c.title} Credit Card ending {c.last4} is ready."),
         "yes": ("YES BANK <estatement@yesbank.in>", "yesbank.in", f"Your {c.title} Credit Card E-Statement for {{m}}",
                 f"Dear Customer, please find your credit card statement. Card Number: XXXX XXXX XXXX {c.last4}."),
         "au": ("AU Small Finance Bank <creditcards@aubank.in>", "aubank.in", f"AU {c.title} Credit Card Statement {{m}}",
                f"Dear Customer, your AU {c.title} Credit Card ending {c.last4} statement has been generated."),
         "federal": ("Federal Bank <creditcards@federalbank.co.in>", "federalbank.co.in",
                     f"Your {c.title} Federal Credit Card statement for {{m}}",
                     f"Hi {PERSONA['first']}, the statement for your {c.title} credit card ending in {c.last4} is "
                     "ready.")}[c.style]
    sender, dom, subj, body = s
    for y, m in _months(START, TODAY):
        sd = _day(y, m, c.statement_day)
        if START <= sd <= TODAY:
            g.add(g.at(sd, 6, 8), sender, subj.format(m=f"{sd:%b %Y}"), body, labels=UPDATES, auth=f"pass:{dom}",
                  kind="statement")
        for _ in range(g.r.randint(*c.alerts_per_month)):
            d = _day(y, m, g.r.randint(1, 28))
            if START <= d <= TODAY:
                g.add(g.at(d), sender, "Transaction alert on your credit card",
                      f"Dear Customer, INR {g.amt()} spent on your credit card ending {c.last4} at {g.merchant()}.",
                      labels=UPDATES, auth=f"pass:{dom}", kind="alert")


def kotak_traps(g: Gen) -> None:
    g.add(g.at(TODAY - timedelta(days=130)), "Kotak Mahindra Bank <bankalerts@kotak.bank.in>",
          "Alert! iPhone 12 activated for Kotak Mobile App",
          "Dear Customer, Kotak Mobile Banking app has been activated after successful mobile number verification. "
          "Please verify below details: CRN: xxxx5056 Device Name: iPhone 12 Date & Time: 11-07-2026 19:10:53 IST. "
          "If this has not been initiated by you please REPORT FRAUD or call our contact centre on 1800 4100. "
          "Warm Regards, Team Kotak Mahindra Bank", labels=PERSONAL, auth="pass:kotak.bank.in", kind="trap")
    for k in range(10):
        d = START + timedelta(days=33 * k + 5)
        g.add(g.at(d), "Kotak Mahindra Bank <bankalerts@kotak.bank.in>", "Kotak Bank: Your account has been debited",
              f"Dear Customer, Rs. {g.amt(200, 5000)} has been debited from your account XXXXXX4585 on {d:%d-%m-%Y} "
              f"towards UPI/{g.merchant()}. Available balance: Rs. {g.amt(5000, 50000)}", labels=UPDATES,
              auth="pass:kotak.bank.in", html=False, kind="trap")


def promotions(g: Gen) -> None:
    """Bank offers with no card number (most of a real bank's mail)."""
    offers = [("HDFC Bank <offers@hdfcbank.net>", "hdfcbank.net", "Get up to 10% cashback on your next flight booking"),
              ("Axis Bank <offers@axisbank.com>", "axisbank.com", "Lifetime free credit card - apply now"),
              ("SBI Card <offers@sbicard.com>", "sbicard.com", "Festive offers on SBI Credit Cards are live"),
              ("ICICI Bank <offers@icicibank.com>", "icicibank.com", "Exclusive dining offers for ICICI Bank customers"),
              ("Kotak Mahindra Bank <offers@kotak.com>", "kotak.com", "Get a Kotak Credit Card with zero joining fee"),
              ("IndusInd Bank <offers@indusind.com>", "indusind.com", "Introducing the IndusInd Bank EazyDiner Card")]
    for k in range(180):
        sender, dom, subj = offers[k % len(offers)]
        d = START + timedelta(days=g.r.randint(0, 364))
        g.add(g.at(d, 9, 20), sender, subj, f"{subj}. Limited period offer. T&C apply. Click here to know more.",
              labels=PROMOS, auth=f"pass:{dom}", unsubscribe=True, kind="promo")


def non_bank(g: Gen) -> None:
    shops = [("Amazon.in <order-update@amazon.in>", "Your Amazon.in order has been dispatched",
              "Your order has been dispatched. Paid with Amazon Pay ICICI credit card ending 7319."),
             ("Swiggy <noreply@swiggy.in>", "Your Swiggy order was delivered", "Hope you enjoyed your meal!"),
             ("Zomato <noreply@zomato.com>", "Your Zomato order summary", "Order total Rs 420. Paid via UPI."),
             ("Uber Receipts <noreply@uber.com>", "Your Thursday trip with Uber", "Total Rs 230. Paid with card XX7319."),
             ("LinkedIn <messages-noreply@linkedin.com>", "You appeared in 12 searches this week", "See who's looking."),
             ("Myntra Updates <updates@myntra.com>", "Your Myntra order is out for delivery", "Arriving today.")]
    for k in range(320):
        sender, subj, body = shops[k % len(shops)]
        d = START + timedelta(days=g.r.randint(0, 364))
        g.add(g.at(d), sender, subj, body, labels=UPDATES, auth=f"pass:{_dom(sender)}", kind="non_bank")


def build(to: str, seed: int = 11) -> tuple[list[Mail], dict]:
    g = Gen(to, seed)
    real_cards = [c for c in CARDS if c.style != "hsbc_offer"]
    for c in CARDS:
        if c.style == "hsbc":
            hsbc(g, c)
        elif c.style == "hsbc_offer":
            hsbc_offers(g, c)
        elif c.style == "sbi":
            sbi(g, c, real_cards)
        elif c.style == "hdfc":
            hdfc(g, c)
        elif c.style == "axis":
            axis(g, c)
        elif c.style == "icici":
            icici(g, c)
        elif c.style == "cred_idfc":
            cred_idfc(g, c)
        else:
            simple_bank(g, c)
    sbi_epin(g)
    hdfc_traps(g)
    kotak_traps(g)
    promotions(g)
    non_bank(g)
    g.mails.sort(key=lambda m: m.when)
    counts: dict[str, int] = {}
    for m in g.mails:
        counts[m.kind] = counts.get(m.kind, 0) + 1
    truth = {
        "mailbox": to, "seed": seed, "today": TODAY.isoformat(), "total": len(g.mails), "persona": PERSONA,
        "mix": "real",
        "cards": [{"bank": c.bank, "last4": c.last4, "product": c.product, "group": c.group,
                   "status": "any", "min_years": 1, "opened": c.opened.isoformat(), "note": c.note}
                  for c in real_cards],
        "must_not_find": MUST_NOT_FIND,
        "applications": [],
        "counts": counts,
    }
    return g.mails, truth
