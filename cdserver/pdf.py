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
    "hdfc": ["{N4}{L4}"],               # card number needed (found on real statements, 2026-11)
    "icici": ["{n4}{DDMM}"],
    "axis": ["{N4}{DDMM}"],
    "sbi": ["{DDMMYYYY}{L4}"],          # card number needed
    "hsbc": ["{DDMMYY}{L6}"],           # 6 digits of the card number: never known, so never opened
    "kotak": ["{n4}{DDMM}"],
    "amex": ["{DDMMYYYY}"],
    "*": ["{N4}{DDMM}", "{n4}{DDMM}", "{DDMMYYYY}", "{DDMMYY}", "{DDMM}{L4}", "{DDMMYYYY}{L4}"],
}

# ---------------------------------------------------------------- the password rule an email states
# Statement emails usually say how the PDF is locked: "Your password is a combination of your date
# of birth (in DDMMYY format) followed by the last 6 digits of the primary credit card number"
# (HSBC), "first 4 letters of your name ... followed by Date of Birth in DDMM format ... lowercase"
# (ICICI), "Enter your date of birth in DDMMYYYY format" (Amex). Read that, and a PDF that needs
# what we don't have isn't downloaded at all.
_PW_CONTEXT = re.compile(r"password|unlock|open (?:the|this) (?:attachment|statement|document|pdf)|"
                         r"access (?:your|the) e-?statement", re.I)
# Lines that mention card digits for something else: ICICI's "Pay from any UPI app to ccpay.<mobile>
# <last 4 digits of Credit Card number>@icici", just above its password instructions.
_PW_NOT_RULE = re.compile(r"\bupi\b|@|ccpay|\bpay\s+(?:from|using|via)\b", re.I)
_PW_PARTS = [
    ("{L6}", re.compile(r"last\s+(?:6|six)\s+digits", re.I)),
    ("{L4}", re.compile(r"last\s+(?:4|four)\s+digits\s+of\s+(?:your\s+|the\s+)?(?:primary\s+)?(?:credit\s+)?card", re.I)),
    ("{NAME}", re.compile(r"first\s+(?:4|four)\s+(?:letters|characters)\s+of\s+(?:your\s+)?(?:first\s+)?name", re.I)),
    ("{DDMMYYYY}", re.compile(r"\bDDMMYYYY\b|\bDD/?MM/?YYYY\b", re.I)),
    ("{DDMMYY}", re.compile(r"\bDDMMYY\b(?!YY)", re.I)),
    ("{DDMM}", re.compile(r"\bDDMM\b(?!YY)", re.I)),
    ("{DOB}", re.compile(r"date\s+of\s+birth|\bDOB\b", re.I)),
]


def password_rule(text: str) -> str | None:
    """The password pattern an email describes, in the tokens above, or None if it states none.
    Parts are put in the order the email writes them; "lowercase"/"capital" picks {n4}/{N4}."""
    if not text or not _PW_CONTEXT.search(text):
        return None
    m = _PW_CONTEXT.search(text)
    window = text[max(0, m.start() - 250):m.end() + 600]
    window = "\n".join(s for s in re.split(r"(?<=[.!?|])\s+|\n", window) if not _PW_NOT_RULE.search(s))
    # Upper or lower case is often said after the example (ICICI: "Enter all letters in lowercase").
    lower = re.search(r"lower\s*case|small\s+letters", window, re.I)
    window = re.split(r"for\s+example|e\.g\.|example\s*:", window, maxsplit=1, flags=re.I)[0]  # rule, not sample
    found: list[tuple[int, str]] = []
    for token, rx in _PW_PARTS:
        hit = rx.search(window)
        if not hit:
            continue
        if token == "{DOB}" and any(t.startswith("{DD") for _, t in found):
            continue  # the format word already says how the date is written
        if token == "{DDMM}" and any(t in ("{DDMMYYYY}", "{DDMMYY}") for _, t in found):
            continue
        found.append((hit.start(), token))
    if not found or not any(t in ("{DDMMYYYY}", "{DDMMYY}", "{DDMM}", "{DOB}") or t == "{NAME}" for _, t in found):
        return None
    name = "{n4}" if lower else "{N4}"
    rule = "".join(t for _, t in sorted(found)).replace("{NAME}", name).replace("{DOB}", "{DDMMYYYY}")
    return rule


def can_open(rule: str, have_name: bool, have_dob: bool, last4s: list[str]) -> bool:
    """Whether a PDF locked with this rule can be opened with what we have. {L6} never can:
    only the last 4 digits of a card are ever known."""
    if "{L6}" in rule:
        return False
    if ("{N4}" in rule or "{n4}" in rule) and not have_name:
        return False
    if "{DD" in rule and not have_dob:
        return False
    if "{L4}" in rule and not last4s:
        return False
    return True


def candidates(bank_id: str, name: str | None, dob: date | None, last4s: list[str]) -> list[str]:
    return [pw for pw, _ in keyed_candidates(bank_id, name, dob, last4s)]


def keyed_candidates(bank_id: str, name: str | None, dob: date | None,
                     last4s: list[str], rule: str | None = None) -> list[tuple[str, str | None]]:
    """(password, the card's last 4 it was built with, if any). SBI locks a statement with the
    date of birth plus that card's last 4, so the password that opens it says whose it is.
    `rule`: the pattern the statement email states (password_rule); then only that is tried."""
    letters = re.sub(r"[^A-Za-z]", "", (name or "").split()[0] if name else "")[:4]
    out: list[tuple[str, str | None]] = []
    for pat in ([rule] if rule else PATTERNS.get(bank_id, []) + PATTERNS["*"]):
        if "{L6}" in pat:
            continue
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


# ---------------------------------------------------------------- the product shown only as a logo
# Where each bank prints the card's name on page 1 of its statement, when it isn't in the PDF's
# text (found on real statements, 2026-11). (left, top, right, bottom) as fractions of the page.
#   ICICI: the logo across the top ("amazon pay | ICICI Bank", "Sapphiro")
#   RBL:   a picture just right of the name and address, top left
#   Yes:   top centre (its emails name the card too)
# IndusInd and Axis print it as text; AU only in the email; HSBC, SBI and HDFC can't be opened
# without the card number.
OCR_REGIONS: dict[str, tuple[float, float, float, float]] = {
    "icici": (0.0, 0.0, 1.0, 0.13),
    "rbl": (0.0, 0.0, 0.65, 0.18),
    "yes": (0.2, 0.0, 0.8, 0.13),
}
OCR_DPI = 250
OCR_MIN_SCORE = 0.85


def _ocr_tools() -> tuple[str, str] | None:
    import shutil
    a, b = shutil.which("pdftoppm"), shutil.which("tesseract")
    return (a, b) if a and b else None


def product_by_ocr(data: bytes, password: str | None, bank_id: str, catalog: list[str]) -> str | None:
    """The product the statement's logo shows, read by OCR from the bank's spot on page 1 and
    accepted only if it matches one of the bank's products closely (OCR_MIN_SCORE) and no other
    product matches as well. Only a product name is ever taken from OCR: never a number or a
    name. Rendering and OCR run through pipes; nothing is written to disk. None when this bank
    has no OCR spot, the tools aren't installed, or nothing matches clearly."""
    import difflib
    import subprocess
    region, tools = OCR_REGIONS.get(bank_id), _ocr_tools()
    if not region or not tools or not catalog:
        return None
    try:
        box = PdfReader(io.BytesIO(data), password=password or None).pages[0].mediabox
        w, h = float(box.width) * OCR_DPI / 72, float(box.height) * OCR_DPI / 72
        left, top, right, bottom = region
        cmd = [tools[0], "-png", "-r", str(OCR_DPI), "-f", "1", "-l", "1", "-singlefile",
               "-x", str(int(left * w)), "-y", str(int(top * h)),
               "-W", str(int((right - left) * w)), "-H", str(int((bottom - top) * h))]
        cmd += (["-upw", password] if password else []) + ["-"]
        png = subprocess.run(cmd, input=data, capture_output=True, check=True, timeout=30).stdout
        read = subprocess.run([tools[1], "stdin", "stdout", "--psm", "11"], input=png, capture_output=True,
                              check=True, timeout=30).stdout.decode("utf-8", "replace")
    except Exception:
        return None
    words = re.findall(r"[A-Za-z][A-Za-z+&]+", read)
    read = png = None
    grams = {"".join(words[i:i + n]).lower() for n in (1, 2, 3) for i in range(len(words) - n + 1)}
    scored = []
    for p in catalog:
        key = re.sub(r"[^a-z]", "", p.lower())
        scored.append((max((difflib.SequenceMatcher(None, key, g).ratio() for g in grams), default=0.0), p))
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    good = [p for s, p in scored if s >= OCR_MIN_SCORE]
    if not good:
        return None
    # Two different products both read clearly (an ad beside the logo): trust neither. One that's
    # a fuller name of the other ("Emirates Sapphiro" vs "Sapphiro") isn't a clash.
    best = good[0]
    rivals = [p for p in good[1:] if best.lower() not in p.lower() and p.lower() not in best.lower()]
    return None if rivals else best
