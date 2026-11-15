"""Rule-based email classification and the per-message analysis step.

Classification looks at the subject first, because bank subjects are templated
and reliable, and falls back to the snippet and body. Offer wording in the subject
or snippet turns an email into marketing unless it names a card the user already holds.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from . import extract
from .models import Evidence, EvidenceType as T, MessageMeta
from .registry import TEST_BANK_ID, Registry

_DATE_TAG = re.compile(r"\[date:\s*(\d{4})-(\d{2})-(\d{2})\]", re.IGNORECASE)

# Issuers that only issue credit/charge cards, so "debit"/"account" words don't
# mean the email is about something else.
CREDIT_ONLY_BANKS = {"sbi", "amex", "onecard", "bobcard"}


def _r(p: str) -> re.Pattern:
    return re.compile(p, re.IGNORECASE)


OTP = _r(r"\botp\b|one[\s-]time[\s-]password|verification code")
OFFER = _r(
    r"pre[\s-]?approved|apply now|apply today|apply here|upgrade now|eligible for|you are eligible|"
    r"you're eligible|limited (period|time)|exclusive offer|instant approval|get (a|your|an)\b.{0,30}\bcard|"
    r"\bavail\b|hurry|last chance|\d+\s?% off|introducing|invitation|special offer|offer for you|"
    r"unlock|grab|don't miss|do not miss|apply for|exclusive(ly)? for you|festive|offer valid|"
    r"\bearn \d+\s?x\b|\d+x (reward|points)"
)
NON_CREDIT = _r(r"debit card|savings account|\ba/c\b|account no|\bloan\b|fixed deposit|\bfd\b|demat|"
                r"mutual fund|insurance|\bupi\b|net ?banking|current account")
CREDIT_WORDS = _r(r"credit card|\bcc\b|charge card|card ?member")  # "card ending" also appears in debit mail

RULES: list[tuple[T, re.Pattern]] = [
    (T.APPLICATION_REJECTED, _r(
        r"regret to inform|regret that we|unable to approve|unable to process your (application|request)|"
        r"not (been )?approved|"
        r"(application|request)\b.{0,40}\b(declined|rejected|unsuccessful)")),
    (T.APPLICATION_RECEIVED, _r(
        r"(application|request)\b.{0,40}\b(received|submitted|in progress|under (process|review)|status)|"
        r"thank(s| you) for (applying|your application)|track your (card )?application|application (no|number|ref)")),
    (T.CLOSURE, _r(
        r"(card|account)\b.{0,30}\b(has been|is|was|have been)\s+(closed|cancell?ed|terminated)|"
        r"closure (confirmation|of your|request (processed|completed))|no dues|no objection certificate")),
    (T.UPGRADE, _r(
        r"(has been|successfully|is now)\s+upgraded|upgrade (is )?(successful|completed|confirmed)|"
        r"upgraded to|converted to|(has been|successfully|is now)\s+converted")),
    (T.ANNUAL_FEE, _r(r"annual fee|renewal fee|membership fee|joining fee|fee waiver|annual membership")),
    (T.REPLACEMENT, _r(r"replacement card|re-?issued?\b|reissue|card (has been )?replaced")),
    (T.RENEWAL, _r(r"card renewal|renewed card|renewal card|card (has been|is) renewed")),
    (T.DISPATCH_ACTIVATION, _r(
        r"dispatch(ed)?\b|on (its|the) way|\bdelivered\b|out for delivery|courier|\bactivated\b|"
        r"activation (is )?(successful|complete)|green pin|pin (has been )?(set|generated|changed)|"
        r"card is ready|welcome kit")),
    (T.WELCOME, _r(r"\bwelcome\b")),
    (T.INACTIVITY_NOTICE, _r(
        r"\binactive\b|not been used|\bdormant\b|haven'?t used|have not used|"
        r"no transactions?\b.{0,30}\b(year|months)")),
    (T.STATEMENT, _r(r"\bstatement\b|e-?statement")),
    (T.POINTS, _r(r"reward points?|points\b.{0,40}\b(expir\w*|balance|credited|earned)|"
                  r"cashback (credited|earned)|membership rewards")),
    (T.PAYMENT, _r(r"payment (received|due|reminder|successful|of (rs|inr))|due date|minimum amount due|"
                   r"total amount due|thank you for (the |your )?payment|bill (is )?(generated|due)")),
    (T.TRANSACTION_ALERT, _r(
        r"transaction|\bspent\b|\btxn\b|debited|used (for|at)|purchase|has been charged|"
        r"charged (rs|inr|₹)|was used")),
    (T.APPROVAL, _r(r"(?<!pre-)(?<!pre )(?<!pre)\bapproved\b|congratulations")),
]


# Lines every bank email carries: security tips, RBI notices, grievance contacts,
# legal footers. They mention "closed", "not been used", "regret", "OTP" and other
# card products, so rules must not read them.
BOILERPLATE = _r(
    r"reserve bank of india|\brbi\b|ombudsman|grievance|nodal officer|never ask|do not share|"
    r"don't share|system[- ]generated|do not reply|unsubscribe|registered office|\bcin\b|disclaimer|"
    r"intended recipient|all rights reserved|registered trademark|most important terms|\bmitc\b|"
    r"regret any inconvenience|toll[- ]free|report (it )?immediately|to block your card|sms block|"
    r"view (this email )?in (your )?browser|stay safe|customer care"
)


def _units(text: str) -> list[str]:
    """Lines, then sentences: the pieces boilerplate and ads are removed in."""
    return [u for u in re.split(r"\n|(?<=[.!?])\s+", text) if u.strip()]


def core_text(text: str) -> str:
    """The email without boilerplate lines."""
    return "\n".join(u for u in _units(text) if not BOILERPLATE.search(u))


def _without_offers(text: str) -> str:
    return "\n".join(u for u in _units(text) if not OFFER.search(u))


def _match_rules(text: str) -> T | None:
    for t, pattern in RULES:
        if pattern.search(text):
            return t
    return None


def classify(msg: MessageMeta, bank_id: str | None, has_last4: bool) -> T:
    subject = msg.subject or ""
    head = f"{subject}\n{msg.snippet or ''}"
    full = core_text(msg.text())

    if OTP.search(subject):
        return T.OTP
    # Debit/account mail often carries a "get a credit card" ad; ignore ad sentences
    # when deciding whether the email is about a credit card at all.
    if (bank_id not in CREDIT_ONLY_BANKS and NON_CREDIT.search(head)
            and not CREDIT_WORDS.search(_without_offers(full))):
        return T.NOT_CREDIT_CARD

    subject_offer = bool(OFFER.search(subject))
    if not subject_offer:
        t = _match_rules(subject)
        if t:
            return t

    if OFFER.search(head):
        return T.OFFER_EXISTING_CARD if has_last4 else T.PROMOTIONAL

    t = _match_rules(full)
    if t:
        return t

    if not has_last4 and (msg.list_unsubscribe or "CATEGORY_PROMOTIONS" in msg.labels):
        return T.PROMOTIONAL
    return T.UNKNOWN


# ---------------------------------------------------------------- authenticity

# A pass counts when DKIM was signed by a bank domain, or when DMARC passed for a
# bank From-domain (banks often sign through an email provider's domain).
_DKIM = re.compile(
    r"(?:dkim=pass[^;\n]*?header\.(?:d|i)=@?([\w.-]+))|(?:dmarc=pass[^;\n]*?header\.from=([\w.-]+))",
    re.IGNORECASE,
)
_FWD_SUBJECT = re.compile(r"^\s*(fwd?|fw)\s*:", re.IGNORECASE)
_FWD_BODY = re.compile(r"-{3,}\s*forwarded message\s*-{3,}|begin forwarded message", re.IGNORECASE)


def dkim_pass(msg: MessageMeta, bank_id: str | None, registry: Registry) -> bool:
    if not bank_id:
        return False
    for dkim_d, dmarc_d in _DKIM.findall(msg.auth_results or ""):
        if registry.bank_for_domain(dkim_d or dmarc_d) == bank_id:
            return True
    return False


def is_forwarded(msg: MessageMeta) -> bool:
    return bool(_FWD_SUBJECT.search(msg.subject or "") or _FWD_BODY.search(msg.body or ""))


def addressed_to_user(msg: MessageMeta, user_addresses: list[str]) -> bool:
    if not msg.to or not user_addresses:
        return True  # unknown: don't penalise
    to = msg.to.lower()
    return any(a.lower() in to for a in user_addresses)


# ---------------------------------------------------------------- analysis

def analyze(
    msg: MessageMeta,
    registry: Registry,
    user_addresses: list[str] | None = None,
    user_names: list[str] | None = None,
) -> Evidence:
    """Turn one email into evidence. The email text is not kept."""
    # Redact full card numbers before any other step sees the text.
    msg.subject, _ = extract.redact_pans(msg.subject or "")
    msg.snippet, _ = extract.redact_pans(msg.snippet or "")
    if msg.body:
        msg.body, _ = extract.redact_pans(msg.body)
    text = msg.text()

    test = registry.is_test_sender(msg.sender)
    if test:
        # Test mode: the bank is whichever one the email names, the sender is
        # trusted (self-sent mail carries no DKIM result), and a [date:YYYY-MM-DD]
        # tag lets a test email pretend to be years old.
        # Ordinary personal mail from the same address must not turn into cards.
        if not re.search(r"\bcard\b", text, re.IGNORECASE):
            # Preview text may be junk ("View in browser..."): ask for the body once.
            bank_id = TEST_BANK_ID if msg.body is None else None
        else:
            bank_id = registry.bank_from_text(text) or TEST_BANK_ID
        m = _DATE_TAG.search(text)
        if m:
            msg.date = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
    else:
        bank_id = registry.bank_for_sender(msg.sender)

    l4 = extract.last4s(text)
    etype = classify(msg, bank_id, bool(l4))
    # Products come from the email minus boilerplate and ad banners, so a statement
    # that also advertises another card still names the card it is about.
    products, best = _products(registry, bank_id, msg.subject or "", text, l4)
    return Evidence(
        message_id=msg.message_id,
        date=msg.date,
        bank_id=bank_id,
        type=etype,
        last4s=l4,
        products=products,
        product=best,
        product_raw=None if products else _clean_raw_product(extract.product_raw(text), registry),
        dkim_pass=True if test else dkim_pass(msg, bank_id, registry),
        forwarded=is_forwarded(msg),
        to_user=addressed_to_user(msg, user_addresses or []),
        name_match=extract.name_matches(extract.holder_name(text), user_names or []),
        addon=extract.is_addon(text),
        used_body=msg.body is not None,
        test=test,
    )


def _products(registry: Registry, bank_id: str | None, subject: str, text: str,
              l4: list[str]) -> tuple[list[str], str | None]:
    """All products named (in order) and the one this email is most likely about:
    the one in the subject, else the one written closest to the card number."""
    if not bank_id:
        return [], None
    clean = _without_offers(core_text(text))
    products = registry.products_in(bank_id, clean) or registry.products_in(bank_id, core_text(text))
    if len(products) <= 1:
        return products, (products[0] if products else None)
    in_subject = [p for p in registry.products_in(bank_id, subject) if p in products]
    if len(in_subject) == 1:
        return products, in_subject[0]
    if l4:
        anchor = clean.find(l4[0])
        if anchor >= 0:
            def distance(p: str) -> int:
                spans = registry.product_spans(bank_id, clean, p)
                return min((abs(s - anchor) for s in spans), default=10**9)
            return products, min(products, key=distance)
    return products, products[0]


_FILLER = {"your", "the", "a", "an", "my", "bank", "card", "new", "dear", "of", "for", "on"}


def _clean_raw_product(raw: str | None, registry: Registry) -> str | None:
    """Drop bank names and filler from an unrecognised product phrase.
    "ICICI Bank" alone is a bank, not a product."""
    if not raw:
        return None
    for pattern, _ in registry._mention_patterns:
        raw = pattern.sub(" ", raw)
    words = [w for w in raw.split() if w.lower() not in _FILLER]
    return " ".join(words) or None


# Types for which a missing last4 or product is worth a body fetch.
BODY_WORTHY = {
    T.STATEMENT, T.WELCOME, T.DISPATCH_ACTIVATION, T.APPROVAL, T.ANNUAL_FEE, T.RENEWAL,
    T.REPLACEMENT, T.UPGRADE, T.CLOSURE, T.INACTIVITY_NOTICE, T.POINTS, T.UNKNOWN,
    T.TRANSACTION_ALERT, T.PAYMENT,
}


def needs_body(ev: Evidence) -> bool:
    if ev.bank_id is None or ev.type not in BODY_WORTHY:
        return False
    if ev.type in (T.UPGRADE, T.REPLACEMENT):
        return len(ev.last4s) < 2 or not ev.products
    if ev.type == T.STATEMENT:
        return True  # rare and valuable: read in full to catch add-on cards further down
    if ev.type in (T.TRANSACTION_ALERT, T.PAYMENT, T.POINTS):
        return not ev.last4s  # these rarely name the product; the number is what matters
    return not ev.last4s or not ev.products
