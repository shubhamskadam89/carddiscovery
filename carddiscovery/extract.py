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
# Mask groups with a single space or hyphen between them: "XXXX XXXX XXXX", "xxx-xxxx".
# Written so a run of mask characters can be split only one way. The earlier form,
# (?:MASK{2,}[\s-]?)+, tried every split of the run and took minutes on a 40-character
# "*****" divider line, which hung the workers.
_MASK_RUN = rf"{_MASK}{{2,}}(?:[\s-]{_MASK}{{2,}})*"
_GAP = r"\s{0,6}"  # whitespace between words of a phrase, including a line break
_LAST4_PATTERNS = [
    # XX1234, xxxx xxxx xxxx 1234, **** 1234, 4XXXXXXXXXXX1234, 5xxx-xxxx-xxxx-1234
    # Starts only where a run starts (not inside "*****"), so a long run is scanned once.
    re.compile(rf"(?<![\w*•●·#])\d{{0,6}}{_MASK_RUN}[\s-]?(\d{{4}})(?!\d)"),
    # ending 1234 / ending with 1234 / ending in XX1234 / ends with 1234
    # Gaps are capped ({_GAP}): several open-ended \s* in a row tried every split of a long
    # blank run after "ending" and never finished.
    re.compile(
        rf"\bend(?:ing|s){_GAP}(?:with|in)?{_GAP}(?:no\.?|number|digits)?{_GAP}[:\-]?{_GAP}{_MASK}*{_GAP}"
        rf"\d?(\d{{4}})(?!\d)",
        re.IGNORECASE,
    ),
    # last 4 digits 1234 / last four digits: 1234
    re.compile(rf"\blast{_GAP}(?:4|four){_GAP}digits?{_GAP}(?:of\s+\w+\s+)?(?:are|is)?{_GAP}[:\-]?{_GAP}"
               r"(\d{4})(?!\d)", re.IGNORECASE),
]


# Masked numbers right after these words are bank accounts, not cards
# ("Savings A/c XX7788", "linked account number XXXXXX4455"). "Account ending"
# alone is kept: Amex calls its cards accounts.
_ACCOUNT_BEFORE = re.compile(
    r"(?:\ba/c|\bacct\.?|\baccount\s+(?:no\.?|number)|\bbank\s+account|\b(?:savings|current|salary|linked|loan)\s+"
    r"(?:a/c|acct|account)\w*(?:\s+(?:no\.?|number))?)\s*[:\-]?\s*$",
    re.IGNORECASE,
)


# "your account XX7785", "HDFC Bank Account ***7785": a number right after the word
# "account". Banks that also run savings accounts use it for those; card issuers that only
# issue cards (Amex: "Card account ending 41005") use it for cards, so the caller decides.
_AFTER_ACCOUNT = re.compile(
    rf"(?<!card\s)\baccount{_GAP}(?:no\.?|number|ending(?:{_GAP}(?:with|in))?)?{_GAP}[:\-]?{_GAP}\d{{0,6}}"
    rf"(?:{_MASK_RUN})?[\s-]?(\d{{4}})(?!\d)", re.IGNORECASE)


def account_last4s(text: str) -> set[str]:
    """Last 4 digits written right after "account" ("your account XX7785")."""
    return {m.group(1) for m in _AFTER_ACCOUNT.finditer(text)}


# Masked numbers that aren't a card: an example in instructions ("For example, if your Card
# number is 'XXXX-XXXX-XXXX-4321', your Date-of-Birth 'XX-XX-1989', your Mobile Number
# 'XXXXXX3245'": SBI's e-PIN email made three cards out of it), a phone number, a masked date.
_EXAMPLE_BEFORE = re.compile(r"\b(?:for\s+example|example|e\.g\.|eg:|for\s+instance|sample)\b", re.IGNORECASE)
_PHONE_BEFORE = re.compile(r"\b(?:mobile|phone|contact|whatsapp)(?:\s+(?:no\.?|number))?\s*(?:is)?\s*[:\-]?\s*['\"]?\s*$",
                           re.IGNORECASE)
_MASKED_DATE = re.compile(r"^[xX*]{2}[-/.][xX*]{2}[-/.]\d{4}$")
# Customer and login identifiers banks mask like card numbers: Kotak's "CRN: xxxx3056" in a
# mobile-app activation email made a card out of the customer number.
_ID_BEFORE = re.compile(
    r"\b(?:crn|cif|customer\s+(?:id|no\.?|number|relationship\s+(?:no\.?|number))|cust(?:omer)?\.?\s*id|"
    r"relationship\s+(?:no\.?|number)|user\s*(?:id|name)|login\s+id|client\s+id|"
    r"device\s+(?:id|name)|ref(?:erence)?\.?\s*(?:no\.?|number|id)?|order\s+(?:id|no\.?))\s*[:\-#]?\s*$",
    re.IGNORECASE)


def _not_a_card(text: str, m: re.Match) -> bool:
    before = text[max(0, m.start() - 40):m.start()]
    return bool(_ACCOUNT_BEFORE.search(before) or _PHONE_BEFORE.search(before) or _ID_BEFORE.search(before)
                or _MASKED_DATE.match(m.group(0).strip(" '\""))
                or _EXAMPLE_BEFORE.search(text[max(0, m.start() - 160):m.start()]))


def last4s(text: str) -> list[str]:
    """Distinct last-4 digits of masked card numbers, in order of appearance."""
    hits: list[tuple[int, str]] = []
    for p in _LAST4_PATTERNS:
        for m in p.finditer(text):
            if _not_a_card(text, m):
                continue
            hits.append((m.start(1), m.group(1)))
    seen: list[str] = []
    for _, d in sorted(hits):
        if d not in seen:
            seen.append(d)
    return seen


# The leading digits a bank shows before the mask: "4375XXXXXXXX4321", "5xxx-xxxx-xxxx-1234",
# "4375 XXXX XXXX 4321". The digits must touch the mask, or be a 4-digit group in the
# 4-4-4-4 layout, so an amount before a masked number ("Rs 500 XX1234") isn't taken.
_PREFIX_PATTERNS = [
    re.compile(rf"(?<![\w])(\d{{1,6}}){_MASK_RUN}[\s-]?(\d{{4}})(?!\d)"),
    re.compile(rf"(?<![\w])(\d{{4}})(?:[\s-]{_MASK}{{4}}){{2}}[\s-](\d{{4}})(?!\d)"),
]


# Some banks show only the last 2 or 3 digits: "Statement ending XX87" (Axis). Only after
# "ending"/"ends with", or after a long mask, so other short masked numbers don't count.
_PARTIAL_PATTERNS = [
    # "e-PIN for your card no. ending 16" (SBI): "card" right before "ending", no mask
    re.compile(rf"\bcard{_GAP}(?:no\.?|number)?{_GAP}end(?:ing|s){_GAP}(?:with|in)?{_GAP}(\d{{2,3}})(?!\d)",
               re.IGNORECASE),
    # "ending XX87" (Axis), "ending with XXXX XXXX XXXX XX72" (SBI Card: groups with spaces)
    re.compile(rf"\bend(?:ing|s){_GAP}(?:with|in)?{_GAP}{_MASK_RUN}(\d{{2,3}})(?!\d)", re.IGNORECASE),
    # A long mask, after up to 6 leading digits: HDFC names statement PDFs like this
    # ("0036XXXXXXXXXX13_17-11-2026_544.pdf": first 4 and last 2 digits of the card).
    re.compile(rf"(?<![\w*•●·#])\d{{0,6}}{_MASK}{{6,}}(\d{{2,3}})(?!\d)"),
]


def partial_last_digits(text: str) -> str | None:
    """The last 2-3 digits of a card number when that's all the email shows ("XX87"), if
    every such mention agrees. Only looked for when no full last 4 digits were found."""
    found = set()
    for p in _PARTIAL_PATTERNS:
        for m in p.finditer(text):
            if not _not_a_card(text, m):
                found.add(m.group(1))
    return found.pop() if len(found) == 1 else None


def number_prefixes(text: str) -> dict[str, list[str]]:
    """Leading digits shown for each masked card number, keyed by its last 4:
    {"4321": ["4375"]}. Only used to tell apart two cards that end in the same 4 digits."""
    out: dict[str, set[str]] = {}
    for p in _PREFIX_PATTERNS:
        for m in p.finditer(text):
            if _not_a_card(text, m):
                continue
            out.setdefault(m.group(2), set()).add(m.group(1))
    return {l4: sorted(ps) for l4, ps in out.items()}


# The name must be on the greeting's own line: Gmail cuts the preview at "...hotline numbers
# HSBC Dear", and with the body after it, "Dear\nHSBC" read the bank as the cardholder, which
# marked every HSBC email as someone else's and hid the card.
_SALUTATION = re.compile(
    r"\b(?:dear|hello|hi)[ \t]+(?:(?:mr|mrs|ms|miss|dr)\.?[ \t]+)?([A-Za-z][A-Za-z .']{1,60}?)[ \t]*[,:\n]",
    re.IGNORECASE,
)
_GENERIC_NAMES = {"customer", "cardholder", "card holder", "card member", "cardmember", "valued customer",
                  "valued cardholder", "valued member", "sir", "madam", "sir/madam", "member", "user"}


def holder_name(text: str) -> str | None:
    m = _SALUTATION.search(text)
    if not m:
        return None
    name = " ".join(m.group(1).split())
    words = name.lower().split()
    # "Dear SBI Cardholder", "Dear Credit Cardholder", "Dear Applicant": a role, not a name.
    if name.lower() in _GENERIC_NAMES or (words and words[-1] in _GENERIC_ROLES):
        return None
    return name


_GENERIC_ROLES = {"cardholder", "cardholders", "holder", "customer", "customers", "member", "cardmember",
                  "applicant", "client", "user", "sir", "madam", "sir/madam", "patron", "subscriber", "team",
                  "investor", "depositor", "beneficiary"}


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


# "your HSBC Visa Platinum Credit Card ending with 9719": the card the user holds, named
# with its number in one phrase. Offers often advertise other cards, so in an offer only
# this exact form says which product a number is.
_HELD = re.compile(
    r"\byour\s+([^.\n:;!?]{1,60}?)\s+credit\s+card\s+(?:no\.?\s+|number\s+)?ending\s+(?:with\s+|in\s+)?"
    r"[xX*•]*\s*(\d{4})(?!\d)", re.IGNORECASE)


def held_phrases(text: str) -> list[tuple[str, str]]:
    """(words before "credit card", last4) for each "your ... credit card ending with NNNN"."""
    flat = re.sub(r"\s+", " ", text or "")
    return [(m.group(1), m.group(2)) for m in _HELD.finditer(flat)]
