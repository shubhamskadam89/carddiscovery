"""A made-up person's inbox: ~10,000 emails over 5 years, with a known answer.

Deterministic for a given seed, so an import can resume and two runs compare.
Every email is built as a real RFC 822 message (headers, plain text and/or HTML,
optional PDF attachment) and carries Gmail labels to apply on import.

The answer file lists every card that should be found (bank, last 4, product,
expected group and status) and every number that must NOT become a card.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from .pdfgen import make_pdf

IST = timezone(timedelta(hours=5, minutes=30))
TODAY = date(2026, 11, 16)
START = date(2021, 11, 1)

PERSONA = {"name": "Shubham Kadam", "first": "Shubham", "dob": "1995-08-21"}
OTHER_PERSON = "Priya Nair"


@dataclass
class Mail:
    key: str
    when: datetime
    sender: str                      # "Name <addr>"
    subject: str
    text: str
    html: str | None = None
    labels: list[str] = field(default_factory=lambda: ["INBOX"])
    attachments: list[tuple[str, bytes]] = field(default_factory=list)
    auth: str | None = None          # "pass:<domain>" | "fail:<domain>" | None (no auth header)
    unsubscribe: bool = False
    kind: str = ""

    @property
    def message_id(self) -> str:
        return f"{self.key}@sim.carddiscovery.test"

    def raw(self, to: str) -> bytes:
        m = EmailMessage()
        if self.auth and self.auth.startswith("mx.google.com;"):
            m["Authentication-Results"] = self.auth  # a full result, e.g. signed by a mail service but DMARC failed
        elif self.auth:
            verdict, dom = self.auth.split(":", 1)
            # Gmail adds this header on delivery; imported mail has none, so we write the
            # result Gmail would have added (pass for real bank mail, fail for forged).
            m["Authentication-Results"] = (f"mx.google.com; dkim={verdict} header.i=@{dom} header.s=s1; "
                                           f"spf={verdict} smtp.mailfrom={dom}; dmarc={verdict} header.from={dom}")
        m["From"] = self.sender
        m["To"] = f"{PERSONA['name']} <{to}>"
        m["Subject"] = self.subject
        m["Date"] = format_datetime(self.when)
        m["Message-ID"] = f"<{self.message_id}>"  # fixed per email, so a retried import can be checked
        if self.unsubscribe:
            m["List-Unsubscribe"] = "<mailto:unsubscribe@example.invalid>"
        m.set_content(self.text)
        if self.html:
            m.add_alternative(self.html, subtype="html")
        for name, data in self.attachments:
            m.add_attachment(data, maintype="application", subtype="pdf", filename=name)
        return bytes(m)


# ---------------------------------------------------------------- banks

@dataclass(frozen=True)
class Bank:
    id: str
    name: str
    color: str
    cards: str      # statement / lifecycle sender
    alerts: str
    offers: str
    pdf_pattern: str | None  # how the statement password is built (see cdserver.pdf)


BANKS = {
    "hdfc": Bank("hdfc", "HDFC Bank", "#004c8f", "Emailstatements.cards@hdfcbank.net", "alerts@hdfcbank.net",
                 "offers@hdfcbank.com", "{N4}{DDMM}"),
    "icici": Bank("icici", "ICICI Bank", "#b02a30", "credit_cards@icicibank.com", "alerts@icicibank.com",
                  "offers@icicibank.com", "{n4}{DDMM}"),
    "sbi": Bank("sbi", "SBI Card", "#1a3d7c", "statements@sbicard.com", "onlinesbicard@sbicard.com",
                "offers@sbicard.com", "{DDMMYYYY}{L4}"),
    "axis": Bank("axis", "Axis Bank", "#97144d", "cc.statements@axisbank.com", "alerts@axisbank.com",
                 "offers@axisbank.com", "{N4}{DDMM}"),
    "amex": Bank("amex", "American Express", "#006fcf", "americanexpress@welcome.americanexpress.com",
                 "AmericanExpress@welcome.aexp.com", "offers@americanexpress.co.in", None),
    "kotak": Bank("kotak", "Kotak Mahindra Bank", "#ed1c24", "creditcardstatement@kotak.com", "creditcardalerts@kotak.com",
                  "offers@kotak.com", "{n4}{DDMM}"),
    "idfc": Bank("idfc", "IDFC FIRST Bank", "#9c1d26", "creditcard@idfcfirstbank.com", "alerts@idfcfirstbank.com",
                 "offers@idfcfirstbank.com", None),
    "yes": Bank("yes", "YES BANK", "#00518f", "creditcards@yesbank.in", "alerts@yesbank.in", "offers@yesbank.in", None),
    "indusind": Bank("indusind", "IndusInd Bank", "#8b1e3f", "creditcards@indusind.com", "alerts@indusind.com",
                     "offers@indusind.com", None),
    "au": Bank("au", "AU Small Finance Bank", "#f37021", "creditcards@aubank.in", "alerts@aubank.in",
               "offers@aubank.in", None),
}


def _sender(bank: Bank, addr: str) -> str:
    return f"{bank.name} <{addr}>"


def _domain(addr: str) -> str:
    return addr.split("@", 1)[1]


# ---------------------------------------------------------------- the person's cards

@dataclass
class Card:
    bank: str
    last4: str
    product: str
    opened: date
    last_used: date = TODAY            # after this: no alerts, statements stop (nil balance)
    ended: date | None = None          # upgrade / replacement / closure date
    end: str | None = None             # "upgrade" | "replacement" | "closure"
    successor: str | None = None
    alerts: int = 100
    addon: str | None = None
    pdfs: bool = False
    inactivity: date | None = None
    via: str | None = None             # "upgrade" | "replacement": no welcome email of its own
    fee: bool = True                   # False: lifetime-free card, so no yearly fee email keeps it visible
    expect_group: str = "owned"
    expect_status: str = "Active"
    min_years: int = 1                 # shortest lookback that can find it

    @property
    def stop(self) -> date:
        return min(self.ended or TODAY, self.last_used)


CARDS = [
    Card("hdfc", "2846", "Millennia", date(2021, 9, 10), alerts=420, addon="2854", pdfs=True),
    Card("hdfc", "6666", "Regalia", date(2021, 1, 5), ended=date(2024, 3, 12), end="upgrade", successor="7777",
         alerts=140, expect_group="history", expect_status="Replaced", min_years=3),
    Card("hdfc", "7777", "Regalia Gold", date(2024, 3, 12), alerts=320, pdfs=True, via="upgrade"),
    Card("icici", "2222", "Amazon Pay", date(2021, 11, 1), alerts=1200, pdfs=True),
    Card("icici", "4730", "Sapphiro", date(2022, 2, 14), last_used=date(2022, 12, 20), alerts=45,
         expect_status="Not used recently", min_years=1),
    Card("sbi", "3333", "ELITE", date(2021, 6, 1), ended=date(2025, 6, 18), end="closure", alerts=160,
         expect_group="history", expect_status="Closed", min_years=3),
    Card("axis", "6158", "Magnus", date(2022, 7, 7), ended=date(2025, 9, 2), end="replacement", successor="6190",
         alerts=150, expect_group="history", expect_status="Replaced", min_years=3),
    Card("axis", "6190", "Magnus", date(2025, 9, 2), alerts=70, pdfs=True, via="replacement"),
    Card("amex", "1005", "Platinum Travel", date(2022, 4, 1), alerts=210),
    Card("kotak", "2297", "League Platinum", date(2023, 1, 15), last_used=date(2025, 2, 1), alerts=60,
         inactivity=date(2026, 8, 10), expect_status="May be closed"),  # 2 years of statements: owned
    # lifetime free and unused since mid-2022: only a 5-year lookback can see it
    Card("idfc", "5521", "Wealth", date(2021, 12, 1), last_used=date(2022, 8, 1), alerts=30, fee=False,
         expect_status="Not used recently", min_years=5),
    Card("yes", "4321", "Marquee", date(2026, 11, 3), alerts=3),  # issued this month and already used
]


# ---------------------------------------------------------------- helpers

class Gen:
    def __init__(self, to: str, seed: int = 7):
        self.to = to
        self.r = random.Random(seed)
        self.mails: list[Mail] = []
        self.n = 0

    def at(self, d: date) -> datetime:
        return datetime.combine(d, time(self.r.randint(6, 22), self.r.randint(0, 59), self.r.randint(0, 59)), IST)

    def day_between(self, a: date, b: date) -> date:
        a, b = max(a, START - timedelta(days=400)), min(b, TODAY)
        return a + timedelta(days=self.r.randint(0, max(0, (b - a).days)))

    def amount(self, lo: float, hi: float) -> str:
        return f"{self.r.uniform(lo, hi):,.2f}"

    def add(self, kind: str, when: date | datetime, sender: str, subject: str, text: str, html: str | None = None,
            labels: list[str] | None = None, auth: str | None = None, unsubscribe: bool = False,
            attachments: list[tuple[str, bytes]] | None = None) -> None:
        if isinstance(when, date) and not isinstance(when, datetime):
            when = self.at(when)
        if when.date() > TODAY:
            return  # e.g. a payment reminder for a statement issued last week
        self.n += 1
        self.mails.append(Mail(f"{kind}-{self.n:05d}", when, sender, subject, text, html,
                               labels or ["INBOX"], attachments or [], auth, unsubscribe, kind))

    def bank_mail(self, kind: str, bank: Bank, addr: str, when, subject: str, text: str, html: bool | None = None,
                  **kw) -> None:
        use_html = self.r.random() < 0.6 if html is None else html
        body_html = _html(bank, subject, text) if use_html else None
        self.add(kind, when, _sender(bank, addr), subject, text + FOOTER, body_html,
                 auth=kw.pop("auth", f"pass:{_domain(addr)}"), **kw)


FOOTER = ("\n\nThis is a system generated email, please do not reply. Never share your card number, CVV, PIN or "
          "OTP with anyone. Bank officials will never ask for these details. For grievances, write to the "
          "Principal Nodal Officer. The Reserve Bank of India does not guarantee deposits.")


def _html(bank: Bank, title: str, text: str) -> str:
    paras = "".join(f"<p>{line}</p>" for line in text.split("\n") if line.strip())
    return (f"<html><head><meta charset='utf-8'><title>{title}</title><style>td{{font-family:Arial}}</style>"
            f"<script>window.dataLayer=[{{'id':'9999'}}];</script></head><body>"
            f"<table width='600' style='border:1px solid #ddd'><tr><td style='background:{bank.color};color:#fff;"
            f"padding:16px'><b>{bank.name}</b></td></tr><tr><td style='padding:20px'>{paras}</td></tr>"
            f"<tr><td style='font-size:11px;color:#777;padding:12px'>{FOOTER}</td></tr></table></body></html>")


def _months(a: date, b: date):
    d = date(a.year, a.month, 1)
    while d <= b:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _mask(g: Gen, l4: str) -> str:
    return g.r.choice([f"XX{l4}", f"XXXX XXXX XXXX {l4}", f"ending {l4}", f"ending with {l4}", f"xxxx{l4}",
                       f"**** {l4}"])


def _card_ref(g: Gen, bank: Bank, c: Card, l4: str | None = None) -> str:
    l4 = l4 or c.last4
    if bank.id == "amex":
        return g.r.choice([f"Card ending {'4' + l4}", f"Card Member account ending in {'4' + l4}"])
    return g.r.choice([f"Credit Card {_mask(g, l4)}", f"Credit Card ending {l4}", f"card no. XXXX XXXX XXXX {l4}"])


def _product_name(bank: Bank, c: Card) -> str:
    return {"sbi": f"SBI Card {c.product}", "idfc": f"IDFC FIRST {c.product} Credit Card",
            "amex": f"American Express {c.product} Credit Card"}.get(bank.id, f"{bank.name} {c.product} Credit Card")


def _password(bank: Bank, l4: str) -> str | None:
    if not bank.pdf_pattern:
        return None
    dob = date.fromisoformat(PERSONA["dob"])
    first = PERSONA["first"][:4]
    return bank.pdf_pattern.format(N4=first.upper(), n4=first.lower(), DDMM=dob.strftime("%d%m"),
                                   DDMMYYYY=dob.strftime("%d%m%Y"), L4=l4)


# ---------------------------------------------------------------- card mail

def card_mails(g: Gen, c: Card) -> None:
    b = BANKS[c.bank]
    prod = _product_name(b, c)
    hi = f"Dear {g.r.choice([PERSONA['name'], PERSONA['name'].upper(), 'Mr. ' + PERSONA['name']])},"

    if not c.via:
        g.bank_mail("welcome", b, b.cards, c.opened, g.r.choice([f"Welcome to the {prod} family",
                    f"Your {prod} is on its way", f"Welcome! Your new {prod}"]),
                    f"{hi}\nCongratulations and welcome! Your {prod} {_card_ref(g, b, c)} has been dispatched "
                    f"and will reach you in 5-7 working days. Track it with AWB {g.r.randint(10**9, 10**10)}.")
        g.bank_mail("activation", b, b.cards, c.opened + timedelta(days=g.r.randint(6, 12)),
                    f"Your {prod} is now active",
                    f"{hi}\nYour {prod} {_card_ref(g, b, c)} has been activated successfully and your PIN is set.")

    # statements, payment reminders and payments: monthly while in use
    stmt_months = list(_months(c.opened + timedelta(days=20), c.stop))
    pdf_from = len(stmt_months) - 6
    for i, m in enumerate(stmt_months):
        sd = date(m.year, m.month, min(28, c.opened.day))
        if sd > c.stop or sd > TODAY:
            continue
        due = g.amount(800, 60000)
        lines = [hi, f"Your {prod} statement for {sd.strftime('%B %Y')} is ready.",
                 f"Card number: {_card_ref(g, b, c)}", f"Total amount due: Rs. {due}",
                 f"Minimum amount due: Rs. {g.amount(200, 2000)}",
                 f"Payment due date: {(sd + timedelta(days=20)).strftime('%d %b %Y')}"]
        if c.addon:
            lines.append(f"Add-on card XX{c.addon} transactions are included in this statement.")
        lines.append(g.r.choice(["", "Upgrade to the Infinia Credit Card by invitation. Know more.",
                                 "Get 10X reward points at SmartBuy. Limited period offer."]))
        atts = []
        if c.pdfs and i >= pdf_from:
            atts = [(f"{b.id.upper()}_Statement_{sd:%b%Y}.pdf", _statement_pdf(g, b, c, sd, bad=(i == pdf_from)))]
        subj = g.r.choice([f"Your {prod} Statement - {sd:%B %Y}", f"{prod} e-Statement for {sd:%b %Y}",
                           f"Statement for your {prod} ending {c.last4 if b.id != 'amex' else '4' + c.last4}"])
        g.bank_mail("statement", b, b.cards, sd, subj, "\n".join(l for l in lines if l), attachments=atts)
        if g.r.random() < 0.7:
            g.bank_mail("payment", b, b.alerts, sd + timedelta(days=g.r.randint(5, 18)),
                        g.r.choice(["Payment received - thank you", f"Payment received on your {prod}"]),
                        f"{hi}\nWe have received your payment of Rs. {due} towards your {prod} "
                        f"{_card_ref(g, b, c)}. Thank you.")
        if g.r.random() < 0.4:
            g.bank_mail("payment", b, b.alerts, sd + timedelta(days=15), f"Payment reminder: {prod}",
                        f"{hi}\nThe payment due date for your {prod} {_card_ref(g, b, c)} is approaching. "
                        f"Total amount due: Rs. {due}.")

    # transaction alerts while in use
    merchants = ["AMAZON", "FLIPKART", "SWIGGY", "ZOMATO", "BIGBASKET", "UBER", "IRCTC", "MAKEMYTRIP", "BOOKMYSHOW",
                 "RELIANCE TRENDS", "DMART", "APOLLO PHARMACY", "SHELL PETROL", "MYNTRA", "NETFLIX"]
    for _ in range(c.alerts):
        d = g.day_between(c.opened, c.stop)
        amt = g.amount(49, 25000)
        mer = g.r.choice(merchants)
        if b.id == "amex" and g.r.random() < 0.3:
            ref = f"XXXX-XXXXXX-4{c.last4}"   # Amex's own format: a known gap in number extraction
        else:
            ref = _card_ref(g, b, c)
        subj = g.r.choice([f"Transaction alert for your {b.name} Credit Card", f"You've spent Rs. {amt} on your card",
                           f"Alert: Rs. {amt} spent on {b.name} Credit Card", "Card transaction alert"])
        g.bank_mail("alert", b, b.alerts, d, subj,
                    f"Dear Customer,\nRs. {amt} was spent on your {b.name} {ref} at {mer} on {d:%d-%m-%Y}. "
                    f"Available limit: Rs. {g.amount(5000, 300000)}. Not you? Call us immediately.",
                    html=g.r.random() < 0.2)

    # annual fee each year while the card exists (banks keep billing dormant cards)
    y = c.opened.year + 1 if c.fee else 10**4
    while y < 10**4 and date(y, c.opened.month, min(28, c.opened.day)) <= min(c.ended or TODAY, TODAY):
        d = date(y, c.opened.month, min(28, c.opened.day))
        g.bank_mail("fee", b, b.cards, d, g.r.choice([f"Annual fee charged on your {prod}", "Annual membership fee"]),
                    f"{hi}\nAn annual fee of Rs. {g.r.choice(['499', '1,000', '2,500', '10,000'])} + GST has been charged "
                    f"to your {prod} {_card_ref(g, b, c)}. Spend Rs. 3 lakh in a year to get it waived.")
        y += 1
    # reward points, quarterly while in use
    for m in list(_months(c.opened, c.stop))[2::3]:
        g.bank_mail("points", b, b.cards, m + timedelta(days=g.r.randint(0, 20)),
                    g.r.choice(["Your reward points summary", "Reward points credited", "Points balance update"]),
                    f"{hi}\nYou have {g.r.randint(200, 40000)} reward points on your {prod} {_card_ref(g, b, c)}. "
                    f"Points expire in 2 years.")

    if c.inactivity:
        g.bank_mail("inactivity", b, b.cards, c.inactivity, f"Your {prod} has not been used",
                    f"{hi}\nYour {prod} {_card_ref(g, b, c)} has not been used for over 12 months. As per RBI "
                    f"guidelines, it may be closed if it remains inactive. Use it or reply to keep it open.")
    if c.end == "upgrade":
        nxt = next(x for x in CARDS if x.last4 == c.successor)
        g.bank_mail("upgrade", b, b.cards, c.ended, f"Your card has been upgraded to {_product_name(b, nxt)}",
                    f"{hi}\nYour {prod} XX{c.last4} has been upgraded to the {_product_name(b, nxt)} XX{nxt.last4}. "
                    f"Your new card has been dispatched. Your old card will be deactivated on activation.")
    if c.end == "replacement":
        nxt = next(x for x in CARDS if x.last4 == c.successor)
        g.bank_mail("replacement", b, b.cards, c.ended, f"Replacement card dispatched for your {prod}",
                    f"{hi}\nAs requested, your {prod} ending {c.last4} reported lost has been blocked. Your replacement "
                    f"card ending {nxt.last4} has been dispatched.")
    if c.end == "closure":
        g.bank_mail("closure", b, b.cards, c.ended, f"Closure of your {prod}",
                    f"{hi}\nAs requested, your {prod} {_card_ref(g, b, c)} has been closed. There are no dues "
                    f"outstanding. A no dues certificate is attached for your records.")


def _statement_pdf(g: Gen, b: Bank, c: Card, sd: date, bad: bool) -> bytes:
    opening, payments, purchases = g.r.uniform(1000, 40000), 0.0, g.r.uniform(500, 50000)
    payments = round(opening, 2)
    total = round(opening - payments + purchases, 2)
    shown = total + (731.0 if bad else 0.0)   # one statement per card doesn't add up
    limit = g.r.choice([150000, 250000, 400000, 600000])
    lines = [f"{_product_name(b, c)} Statement", f"Name: {PERSONA['name'].upper()}",
             f"Card Number: XXXX XXXX XXXX {c.last4}", f"Statement Date: {sd:%d %b %Y}",
             f"Opening Balance: Rs. {opening:,.2f}", f"Payments / Credits: Rs. {payments:,.2f}",
             f"Purchases / Debits: Rs. {purchases:,.2f}", f"Total Amount Due: Rs. {shown:,.2f}",
             f"Minimum Amount Due: Rs. {shown * 0.05:,.2f}", f"Credit Limit: Rs. {limit:,}",
             f"Available Credit Limit: Rs. {limit - shown:,.2f}", f"Reward Points Balance: {g.r.randint(500, 60000)}"]
    return make_pdf(lines, password=_password(b, c.last4))


# ---------------------------------------------------------------- noise and traps

def bank_noise(g: Gen, n_promos: int, n_debit: int, n_otp: int) -> None:
    owned = [c for c in CARDS if c.stop >= TODAY - timedelta(days=60)]
    promo_cards = ["Infinia", "Diners Club Black", "Emeralde", "Cashback", "Atlas", "Reserve", "Legend", "LIT",
                   "Select", "Platinum Reserve"]
    for _ in range(n_promos):
        b = BANKS[g.r.choice(list(BANKS))]
        d = g.day_between(START - timedelta(days=365 * 3), TODAY)
        prod = g.r.choice(promo_cards)
        if g.r.random() < 0.08 and owned:   # an offer that names a card the user holds
            c = g.r.choice(owned)
            b = BANKS[c.bank]
            subj = g.r.choice([f"Exclusive offer on your {c.product} Credit Card", f"10X rewards on your card ending {c.last4}"])
            text = (f"Dear {PERSONA['first']}, enjoy 10X reward points on your {_product_name(b, c)} ending {c.last4} "
                    f"this festive season. Offer valid till 31 Dec. T&C apply.")
        else:
            subj = g.r.choice([f"You're pre-approved for the {b.name} {prod} Credit Card",
                               f"Lifetime FREE {prod} Credit Card - apply now", f"Upgrade now to {b.name} {prod}",
                               f"Limited period: {prod} Credit Card with 5,000 bonus points",
                               f"Introducing the {b.name} {prod} Credit Card", f"Last chance: instant approval on {prod}",
                               f"Exclusive for you: {prod} Credit Card"])
            text = (f"Dear Customer, you are eligible for the {b.name} {prod} Credit Card. Apply now in 2 minutes "
                    f"and get a welcome bonus. Limited period offer. T&C apply.")
        labels = ["INBOX", "CATEGORY_PROMOTIONS"] if g.r.random() < 0.85 else ["INBOX"]
        g.bank_mail("promo", b, b.offers, d, subj, text, labels=labels, unsubscribe=True)

    debit = [("hdfc", "5544"), ("icici", "8102"), ("axis", "3391")]
    for _ in range(n_debit):
        bid, l4 = g.r.choice(debit)
        b = BANKS[bid]
        d = g.day_between(START - timedelta(days=365 * 3), TODAY)
        kind = g.r.random()
        if kind < 0.6:
            subj, text = (g.r.choice([f"{b.name} Debit Card Transaction Alert", "Debit card used for purchase"]),
                          f"Dear Customer, Rs. {g.amount(50, 9000)} was spent on your {b.name} Debit Card XX{l4} "
                          f"at {g.r.choice(['DMART', 'AMAZON', 'ATM S1AW0123'])}. Savings A/c XX{g.r.randint(1000, 9999)}. "
                          f"Get more rewards with a {b.name} Credit Card!")
        elif kind < 0.8:
            subj, text = (f"Your {b.name} Savings Account statement for {d:%B %Y}",
                          f"Dear Customer, your savings account statement for account no XXXXXX{g.r.randint(1000, 9999)} "
                          f"is attached. Debit card XX{l4} is linked to this account.")
        else:
            subj, text = (g.r.choice(["Your new Debit Card has been dispatched", "Debit card activated successfully"]),
                          f"Dear {PERSONA['name']}, your new {b.name} Debit Card ending {l4}, linked to your savings "
                          f"account, has been dispatched and will reach you soon.")
        g.bank_mail("debit", b, b.alerts, d, subj, text)

    for _ in range(n_otp):
        c = g.r.choice(CARDS)
        b = BANKS[c.bank]
        d = g.day_between(c.opened, c.stop)
        g.bank_mail("otp", b, b.alerts, d, g.r.choice(["OTP for your transaction", "Your one time password"]),
                    f"{g.r.randint(100000, 999999)} is the OTP for your transaction of Rs. {g.amount(100, 20000)} on "
                    f"{b.name} Credit Card XX{c.last4}. Valid for 10 minutes. Do not share it with anyone.", html=False)


def traps(g: Gen) -> list[dict]:
    """Mail that must not produce a card. Returns their numbers for the answer file."""
    out = []
    hdfc, kotak, indus, au = BANKS["hdfc"], BANKS["kotak"], BANKS["indusind"], BANKS["au"]
    # someone else's card, sent to this inbox
    for m in (date(2026, 7, 5), date(2026, 8, 5), date(2026, 9, 5)):
        g.bank_mail("trap-other", hdfc, hdfc.cards, m, f"Your HDFC Bank Diners Club Black Credit Card Statement - {m:%B %Y}",
                    f"Dear {OTHER_PERSON},\nYour Diners Club Black Credit Card XX9988 statement is ready. "
                    f"Total amount due: Rs. 12,410.00")
    out.append({"bank": "hdfc", "last4": "9988", "why": "someone else's card (another name)"})
    # forged bank sender: From says hdfcbank.net but the signature check failed
    for m in (date(2026, 10, 1), date(2026, 10, 20)):
        g.bank_mail("trap-forged", hdfc, hdfc.alerts, m, "Your HDFC Bank Credit Card has been blocked",
                    "Dear Customer, your HDFC Bank Credit Card XX1357 has been blocked. Verify your details now to "
                    "unblock it.", auth="fail:hdfcbank-secure-alerts.com")
    out.append({"bank": "hdfc", "last4": "1357", "why": "forged sender (DKIM/DMARC fail)"})
    # forwarded statement from the person's own address (not a bank sender)
    g.add("trap-fwd", date(2026, 6, 2), f"{PERSONA['name']} <shubham.kadam.home@gmail.com>",
          "Fwd: Your Kotak White Credit Card Statement",
          "---------- Forwarded message ---------\nFrom: Kotak Mahindra Bank\nYour Kotak White Credit Card XX7766 "
          "statement is ready. Total amount due: Rs. 4,210.00")
    out.append({"bank": "kotak", "last4": "7766", "why": "forwarded by the user, not from the bank"})
    # merchant receipts naming a card number
    for _ in range(40):
        d = g.day_between(START, TODAY)
        g.add("trap-receipt", d, "Amazon.in <auto-confirm@amazon.in>", f"Your Amazon.in order #{g.r.randint(10**6, 10**7)}",
              f"Thanks for your order. Paid with Visa credit card ending 4417. Order total: Rs. {g.amount(200, 9000)}.",
              labels=["INBOX", "CATEGORY_UPDATES"], auth="pass:amazon.in")
    out.append({"bank": "*", "last4": "4417", "why": "merchant receipt, not a bank email"})
    # fintech app reminders (not banks)
    for _ in range(60):
        d = g.day_between(START, TODAY)
        g.add("trap-fintech", d, "CRED <protect@cred.club>", "Your HDFC Bank credit card bill is due",
              f"Your HDFC Bank credit card ending 2846 has a bill of Rs. {g.amount(1000, 50000)} due soon. Pay via CRED "
              f"and earn coins.", labels=["INBOX", "CATEGORY_UPDATES"], auth="pass:cred.club")
    # applications
    g.bank_mail("app-rejected", indus, indus.cards, date(2025, 5, 14), "Update on your IndusInd Bank Credit Card application",
                f"Dear {PERSONA['name']},\nWe regret to inform you that we are unable to approve your application for the "
                f"IndusInd Bank Legend Credit Card at this time.")
    g.bank_mail("app-received", au, au.cards, date(2026, 10, 28), "We have received your AU Credit Card application",
                f"Dear {PERSONA['name']},\nThank you for applying for the AU Small Finance Bank LIT Credit Card. Your "
                f"application number is AU{g.r.randint(10**8, 10**9)} and is under review.")
    return out


NON_BANK = [
    ("Amazon.in <shipment-tracking@amazon.in>", ["Your package has shipped", "Arriving today", "Delivered: your package"],
     "Your order is on the way. Track your package in Your Orders.", "CATEGORY_UPDATES"),
    ("Flipkart <noreply@flipkart.com>", ["Your Flipkart order is confirmed", "Big Billion Days are here"],
     "Thank you for shopping with Flipkart. Extra 10% off with select bank credit cards.", "CATEGORY_PROMOTIONS"),
    ("Swiggy <noreply@swiggy.in>", ["Your Swiggy order receipt", "Hungry? 50% off tonight"],
     "Your order from Behrouz Biryani was delivered. Paid via UPI.", "CATEGORY_UPDATES"),
    ("Zomato <noreply@zomato.com>", ["Your Zomato order", "Weekend feast deals"], "Enjoy your meal!", "CATEGORY_PROMOTIONS"),
    ("Uber Receipts <noreply@uber.com>", ["Your Tuesday evening trip with Uber", "Your trip receipt"],
     "Thanks for riding. Total Rs. 342.00 charged to your card.", "CATEGORY_UPDATES"),
    ("MakeMyTrip <noreply@makemytrip.com>", ["Booking confirmed: Pune to Goa", "Flash sale on flights"],
     "Your booking is confirmed. PNR X7Q2LM.", "CATEGORY_UPDATES"),
    ("IRCTC <ticketadmin@irctc.co.in>", ["Booking Confirmation on IRCTC", "E-ticket"], "Your ticket is booked. PNR 4521346798.",
     "CATEGORY_UPDATES"),
    ("Netflix <info@account.netflix.com>", ["Your Netflix membership", "New on Netflix this week"],
     "Your monthly payment went through.", "CATEGORY_PROMOTIONS"),
    ("LinkedIn <messages-noreply@linkedin.com>", ["You appeared in 12 searches this week", "New job matches for you"],
     "See who's viewing your profile.", "CATEGORY_SOCIAL"),
    ("GitHub <noreply@github.com>", ["[GitHub] A new SSH key was added", "Your weekly digest"], "Repository activity summary.",
     "CATEGORY_UPDATES"),
    ("Tata Power <billing@tatapower.com>", ["Your electricity bill for this month", "Payment received - thank you"],
     "Your bill of Rs. 2,140.00 has been paid via credit card. Thank you.", "CATEGORY_UPDATES"),
    ("Paytm <noreply@paytm.com>", ["Cashback credited", "Recharge successful"], "Rs. 25 cashback credited to your wallet.",
     "CATEGORY_PROMOTIONS"),
    ("Medium Daily Digest <noreply@medium.com>", ["5 stories for you", "Your daily read"], "Stories picked for you.",
     "CATEGORY_UPDATES"),
    ("Rahul Verma <rahul.verma.dev@gmail.com>", ["Weekend plan?", "Re: photos from the trip", "Lunch tomorrow?"],
     "Hey, are we still on for Saturday? Let me know.", None),
    ("Mom <kadam.family@gmail.com>", ["Call me when free", "Diwali plans"], "Beta, call me when you are free.", None),
    ("Moneycontrol <newsletter@moneycontrol.com>", ["Markets today", "Best credit cards of 2026"],
     "Our pick of the best credit cards this year: HDFC Infinia, Axis Atlas and more.", "CATEGORY_PROMOTIONS"),
]


def non_bank(g: Gen, n: int) -> None:
    for _ in range(n):
        sender, subjects, text, cat = g.r.choice(NON_BANK)
        d = g.day_between(START - timedelta(days=365 * 3), TODAY)
        labels = ["INBOX"] + ([cat] if cat else [])
        g.add("other", d, sender, g.r.choice(subjects), text, labels=labels,
              auth=f"pass:{sender.split('@')[1].rstrip('>')}", unsubscribe=cat == "CATEGORY_PROMOTIONS")


# ---------------------------------------------------------------- build

def build(to: str, total: int = 10_000, seed: int = 7) -> tuple[list[Mail], dict]:
    g = Gen(to, seed)
    for c in CARDS:
        card_mails(g, c)
    trap_list = traps(g)
    card_count = len(g.mails)
    rest = total - card_count
    promos, debit, otp = int(rest * 0.27), int(rest * 0.11), int(rest * 0.05)
    bank_noise(g, promos, debit, otp)
    non_bank(g, total - len(g.mails))
    g.mails.sort(key=lambda m: m.when)

    counts: dict[str, int] = {}
    for m in g.mails:
        counts[m.kind] = counts.get(m.kind, 0) + 1
    truth = {
        "mailbox": to, "seed": seed, "today": TODAY.isoformat(), "total": len(g.mails), "persona": PERSONA,
        "cards": [{"bank": c.bank, "last4": c.last4, "product": c.product, "group": c.expect_group,
                   "status": c.expect_status, "min_years": c.min_years, "opened": c.opened.isoformat(),
                   "pdf_statements": c.pdfs} for c in CARDS]
                 + [{"bank": "hdfc", "last4": "2854", "product": "Millennia", "group": "any", "status": "any",
                     "min_years": 1, "addon": True, "note": "add-on, named only inside statement bodies"}],
        "must_not_find": trap_list + [{"bank": b, "last4": l4, "why": "debit card"} for b, l4 in
                                      [("hdfc", "5544"), ("icici", "8102"), ("axis", "3391")]],
        "applications": [{"bank": "indusind", "status": "rejected"}, {"bank": "au", "status": "in_progress"}],
        "counts": counts,
    }
    return g.mails, truth


def write_truth(truth: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(truth, indent=2))
