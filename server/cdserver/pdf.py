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
    letters = re.sub(r"[^A-Za-z]", "", (name or "").split()[0] if name else "")[:4]
    out: list[str] = []
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
            if pw not in out:
                out.append(pw)
    return out


class PasswordNeeded(Exception):
    pass


def open_pdf(data: bytes, passwords: list[str]) -> str:
    """Text of the PDF, decrypted in memory. Raises PasswordNeeded."""
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        for pw in [""] + passwords:
            if reader.decrypt(pw):
                break
        else:
            raise PasswordNeeded()
    return "\n".join(page.extract_text() or "" for page in reader.pages)


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
