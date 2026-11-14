"""Field extraction: masked card numbers, holder names, add-on flags.

Full card numbers are redacted before anything else looks at the text.
"""

from __future__ import annotations

import re

_PAN_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact_pans(text: str) -> tuple[str, int]:
    """Replace any Luhn-valid 13–19 digit number with a placeholder."""
    count = 0

    def sub(m: re.Match) -> str:
        nonlocal count
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            count += 1
            return "[REDACTED-PAN]"
        return m.group(0)

    return _PAN_CANDIDATE.sub(sub, text), count


_MASK = r"[xX*•●·#]"
_LAST4_PATTERNS = [
    # XX1234, xxxx xxxx xxxx 1234, **** 1234, 4XXXXXXXXXXX1234, 5xxx-xxxx-xxxx-1234
    re.compile(rf"(?<![\w])\d{{0,6}}(?:{_MASK}{{2,}}[\s-]?)+(\d{{4}})(?!\d)"),
    # ending 1234 / ending with 1234 / ending in XX1234 / ends with 1234
    re.compile(
        rf"\bend(?:ing|s)\s*(?:with|in)?\s*(?:no\.?|number|digits)?\s*[:\-]?\s*{_MASK}*\s*\d?(\d{{4}})(?!\d)",
        re.IGNORECASE,
    ),
    # last 4 digits 1234 / last four digits: 1234
    re.compile(r"\blast\s*(?:4|four)\s*digits?\s*(?:of\s+\w+\s+)?(?:are|is)?\s*[:\-]?\s*(\d{4})(?!\d)",
               re.IGNORECASE),
]


def last4s(text: str) -> list[str]:
    """Distinct last-4 digits of masked card numbers, in order of appearance."""
    hits: list[tuple[int, str]] = []
    for p in _LAST4_PATTERNS:
        for m in p.finditer(text):
            hits.append((m.start(1), m.group(1)))
    seen: list[str] = []
    for _, d in sorted(hits):
        if d not in seen:
            seen.append(d)
    return seen


_SALUTATION = re.compile(
    r"\b(?:dear|hello|hi)\s+(?:(?:mr|mrs|ms|miss|dr)\.?\s+)?([A-Za-z][A-Za-z .']{1,60}?)\s*[,:\n]",
    re.IGNORECASE,
)
_GENERIC_NAMES = {"customer", "cardholder", "card member", "cardmember", "valued customer", "sir", "madam",
                  "sir/madam", "member", "user"}


def holder_name(text: str) -> str | None:
    m = _SALUTATION.search(text)
    if not m:
        return None
    name = " ".join(m.group(1).split())
    if name.lower() in _GENERIC_NAMES:
        return None
    return name


def name_matches(found: str | None, user_names: list[str]) -> bool | None:
    """True/False if a name was found and user names are known; None otherwise."""
    if not found or not user_names:
        return None
    tokens = {t for t in re.split(r"[\s.]+", found.lower()) if len(t) > 1}
    for n in user_names:
        user_tokens = {t for t in re.split(r"[\s.]+", n.lower()) if len(t) > 1}
        if tokens & user_tokens:
            return True
    return False


_ADDON = re.compile(r"\b(add[\s-]?on|supplementary)\s+card", re.IGNORECASE)


def is_addon(text: str) -> bool:
    return bool(_ADDON.search(text))


_PRODUCT_RAW = re.compile(r"([A-Z][\w+&'-]*(?:\s+[A-Z0-9][\w+&'-]*){0,4})\s+Credit\s+Card", re.UNICODE)


def product_raw(text: str) -> str | None:
    """Best-effort product phrase before 'Credit Card', for unknown products."""
    m = _PRODUCT_RAW.search(text)
    return " ".join(m.group(1).split()) if m else None
