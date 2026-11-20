"""Statement PDFs: unlock in memory, pull out card facts, reconcile the totals.

Password patterns per bank are a seed list (UNVERIFIED: check each bank's
statement email, which usually states its format). Tokens:
  {N4} first 4 letters of the name, upper case   {n4} the same, lower case
  {DDMM} {DDMMYY} {DDMMYYYY} date of birth       {L4} last 4 card digits
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime

from pypdf import PdfReader

PATTERNS: dict[str, list[str]] = {
    "hdfc": ["{N4}{DDMM}"],
    "icici": ["{n4}{DDMM}"],
    "axis": ["{N4}{DDMM}"],
    "sbi": ["{DDMMYYYY}{L4}"],
    "kotak": ["{n4}{DDMM}"],
    "amex": [],
    "*": ["{N4}{DDMM}", "{n4}{DDMM}", "{DDMMYYYY}", "{DDMMYY}", "{DDMM}{L4}", "{DDMMYYYY}{L4}"],
}


def candidates(bank_id: str, name: str | None, dob: date | None, last4s: list[str]) -> list[str]:
    return [pw for pw, _ in keyed_candidates(bank_id, name, dob, last4s)]


def keyed_candidates(bank_id: str, name: str | None, dob: date | None,
                     last4s: list[str]) -> list[tuple[str, str | None]]:
    """(password, the card's last 4 it was built with, if any). SBI locks a statement with the
    date of birth plus that card's last 4, so the password that opens it says whose it is."""
    letters = re.sub(r"[^A-Za-z]", "", (name or "").split()[0] if name else "")[:4]
    out: list[tuple[str, str | None]] = []
    for pat in PATTERNS.get(bank_id, []) + PATTERNS["*"]:
        if ("{N4}" in pat or "{n4}" in pat) and len(letters) < 4:
            continue
        if "{DD" in pat and not dob:
            continue
        for l4 in (last4s or [""]) if "{L4}" in pat else [""]:
            if "{L4}" in pat and not l4:
                continue
            pw = pat.format(N4=letters.upper(), n4=letters.lower(),
                            DDMM=dob.strftime("%d%m") if dob else "", DDMMYY=dob.strftime("%d%m%y") if dob else "",
                            DDMMYYYY=dob.strftime("%d%m%Y") if dob else "", L4=l4)
            if pw not in [o for o, _ in out]:
                out.append((pw, l4 or None))
    return out


class PasswordNeeded(Exception):
    pass


def open_pdf(data: bytes, passwords: list[str]) -> str:
    """Text of the PDF, decrypted in memory. Raises PasswordNeeded."""
    return open_pdf_with(data, passwords)[0]


def open_pdf_with(data: bytes, passwords: list[str]) -> tuple[str, str | None]:
    """(text, the password that opened it; None if it wasn't locked). Raises PasswordNeeded."""
    reader = PdfReader(io.BytesIO(data))
    used = None
    if reader.is_encrypted:
        for pw in [""] + passwords:
            if reader.decrypt(pw):
                used = pw or None
                break
        else:
            raise PasswordNeeded()
    # Page by page: a bank PDF can have a page the library refuses (e.g. "Too many character
    # widths" on an embedded font); the other pages still carry the card number and product.
    texts = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            continue
    if not any(t.strip() for t in texts):
        raise Unreadable()
    return "\n".join(texts), used


class Unreadable(Exception):
    """Opened, but no page's text could be extracted."""


_AMT = r"(?:Rs\.?|INR|₹)?\s*([0-9][0-9,]*(?:\.\d{1,2})?)"


def _amount(text: str, *labels: str) -> float | None:
    for label in labels:
        m = re.search(label + r"[^0-9\n]{0,40}" + _AMT, text, re.IGNORECASE)
        if m:
            return float(m.group(1).replace(",", ""))
    return None


def _date(text: str) -> date | None:
    m = re.search(r"Statement\s+Date[^0-9A-Za-z\n]{0,10}(\d{1,2}[ /-][A-Za-z]{3}[ /-]\d{4}|\d{2}/\d{2}/\d{4})",
                  text, re.IGNORECASE)
    if not m:
        return None
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%d/%b/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(m.group(1), fmt).date()
        except ValueError:
            pass
    return None


@dataclass
class StatementFacts:
    last4: str | None
    statement_date: date | None
    total_due: float | None
    minimum_due: float | None
    credit_limit: float | None
    available_limit: float | None
    reward_points: float | None
    reconciled: bool | None


def parse(text: str) -> StatementFacts:
    m = re.search(r"(?:X{2,}|\*{2,}|x{2,})[\s-]*(?:[Xx*]{4}[\s-]*){0,2}(\d{4})\b", text)
    opening = _amount(text, r"(?:Opening|Previous)\s+Balance")
    payments = _amount(text, r"Payments?(?:\s*/\s*Credits?)?")
    purchases = _amount(text, r"Purchases?(?:\s*/\s*Debits?)?", r"New\s+Debits")
    fees = _amount(text, r"(?:Finance|Fee|Other)\s+Charges") or 0.0
    total = _amount(text, r"Total\s+(?:Amount\s+)?Due")
    reconciled = None
    if None not in (opening, payments, purchases, total):
        reconciled = abs(opening - payments + purchases + fees - total) <= 1.0
    return StatementFacts(
        last4=m.group(1) if m else None,
        statement_date=_date(text),
        total_due=total,
        minimum_due=_amount(text, r"Minimum\s+(?:Amount\s+)?Due"),
        credit_limit=_amount(text, r"(?<!Available )(?<!Cash )Credit\s+Limit"),
        available_limit=_amount(text, r"Available\s+(?:Credit\s+)?Limit"),
        reward_points=_amount(text, r"(?:Reward\s+Points|CashPoints|Points)\s+(?:Closing\s+)?Balance",
                              r"Closing\s+(?:Reward\s+)?Points"),
        reconciled=reconciled,
    )


# ---------------------------------------------------------------- which card the statement is for

# Raise when the PDF reading changes: earlier attempts (failed or read) are then made once more.
PDF_READER_VERSION = 2


@dataclass
class StatementCard:
    last4s: list[str]        # the card's number first, then any add-on cards listed
    products: list[str]      # catalogue products named, in order
    product: str | None      # the one nearest the card number (statements also advertise other cards)
    holder: str | None       # cardholder name as printed, if found
    partial: str | None = None  # last 2-3 digits when that's all it prints (SBI shows 2)


# Words that introduce the card's own number on a statement. A masked number elsewhere on the
# page (reference, account or transaction numbers print the same way) isn't taken for a card.
_CARD_LABEL = re.compile(
    r"(card\s*(?:no\.?|number|num|#|ending(?:\s+(?:with|in))?)|(?:primary|add[\s-]?on|supplementary|"
    r"additional|principal)\s+card(?:\s*holder)?|card\s*account)", re.IGNORECASE)
_LABEL_REACH = 60


def card_identity(text: str, registry, bank_id: str, known: set[str] = frozenset(),
                  unlocked_by: str | None = None, partial_hint: str | None = None) -> StatementCard:
    """Bank, number and product from a statement's text. Full card numbers are blanked out
    first. A masked number counts as a card only if a card-number label comes just before it,
    it's a card we already know (`known`), it fits the statement's 2-3 digit ending
    (`partial_hint`), or it's the last 4 whose password unlocked the PDF (`unlocked_by`: SBI
    locks each statement with that card's last 4, and prints only 2 digits). Numbers written as
    bank accounts are left out, except at card-only issuers such as Amex."""
    from carddiscovery import extract
    from carddiscovery.classify import CREDIT_ONLY_BANKS, _products

    text, _ = extract.redact_pans(text)
    found = extract.last4s(text)
    if bank_id not in CREDIT_ONLY_BANKS:
        accounts = extract.account_last4s(text)
        found = [x for x in found if x not in accounts]
    partial = extract.partial_last_digits(text) or partial_hint

    def labelled(l4: str) -> bool:
        """A card-number label earlier on the same line, close to the number."""
        for m in re.finditer(rf"(?<!\d){l4}(?!\d)", text):
            before = text[max(0, m.start() - _LABEL_REACH):m.start()].rsplit("\n", 1)[-1]
            if _CARD_LABEL.search(before):
                return True
        return False

    l4 = [x for x in found if labelled(x) or x in known or (partial and x.endswith(partial))]
    if unlocked_by and unlocked_by not in l4 and (not partial or unlocked_by.endswith(partial)):
        l4.insert(0, unlocked_by)
    # Statements advertise other cards ("Upgrade to HSBC Live+ Credit Card and earn 10%
    # cashback"), often right next to the card number, so advert lines are dropped and the
    # product in the statement's heading wins; nearest-to-the-number only breaks other ties.
    kept = [ln for ln in text.splitlines() if ln.strip() and not _ADVERT.search(ln)]
    body = "\n".join(kept)
    products, best = _products(registry, bank_id, "", body, l4)
    heading = registry.products_in(bank_id, "\n".join(kept[:HEADING_LINES]))
    if len(heading) == 1:
        best = heading[0]
    return StatementCard(l4, products, best, extract.holder_name(text), partial)


HEADING_LINES = 6
_ADVERT = re.compile(
    r"\b(upgrad\w*|apply|applied|earn\w*|cashback|offer\w*|introduc\w*|eligible|exclusive|avail\w*|switch\w*|"
    r"get\s+(?:a|an|your|the)\b|pre-?approved|limited\s+period|t&c|terms\s+and\s+conditions)", re.IGNORECASE)
