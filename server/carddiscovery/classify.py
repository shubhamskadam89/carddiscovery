"""Rule-based email classification and the per-message analysis step.

Classification looks at the subject first, because bank subjects are templated
and reliable, and falls back to the snippet and body. Offer wording in the subject
or snippet turns an email into marketing unless it names a card the user already holds.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import getaddresses

from . import extract
from .models import Evidence, EvidenceType as T, MessageMeta
from .registry import TEST_BANK_ID, Registry, sender_domain

_DEBIT_SUBJECT = re.compile(r"\bdebit\s+card\b", re.IGNORECASE)
_DATE_TAG = re.compile(r"\[date:\s*(\d{4})-(\d{2})-(\d{2})\]", re.IGNORECASE)

def _r(p: str) -> re.Pattern:
    return re.compile(p, re.IGNORECASE)


# Patterns and rules come from the rules bundle (data/rules.json, or a signed
# bundle passed with --bundle). configure() compiles them into these globals.
CREDIT_ONLY_BANKS: set[str] = set()  # issue only credit/charge cards: "account" words don't disqualify
OTP: re.Pattern
OFFER: re.Pattern
NON_CREDIT: re.Pattern
CREDIT_WORDS: re.Pattern  # "card ending" is not here: it also appears in debit mail
# Lines every bank email carries (security tips, RBI notices, grievance contacts,
# legal footers). They mention "closed", "not been used", "regret", "OTP" and
# other card products, so rules must not read them.
BOILERPLATE: re.Pattern
RULES: list[tuple[T, re.Pattern]] = []


def configure(cfg: dict) -> None:
    """Load classification rules from a bundle's "classify" section."""
    global CREDIT_ONLY_BANKS, OTP, OFFER, NON_CREDIT, CREDIT_WORDS, BOILERPLATE, RULES
    CREDIT_ONLY_BANKS = set(cfg["credit_only_banks"])
    OTP = _r(cfg["otp"])
    OFFER = _r(cfg["offer"])
    NON_CREDIT = _r(cfg["non_credit"])
    CREDIT_WORDS = _r(cfg["credit_words"])
    BOILERPLATE = _r(cfg["boilerplate"])
    RULES = [(T(r["type"]), _r(r["pattern"])) for r in cfg["rules"]]




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
    # A subject that says "debit card" (and not credit) settles it, whatever ads the
    # body carries ("Get more rewards with a Credit Card!").
    if (bank_id not in CREDIT_ONLY_BANKS and _DEBIT_SUBJECT.search(subject)
            and not CREDIT_WORDS.search(subject)):
        return T.NOT_CREDIT_CARD
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
    """`msg.auth_results` holds only the result Gmail itself added (see
    providers.gmail.trusted_auth_results), so a pass here can't be forged."""
    if not bank_id:
        return False
    for dkim_d, dmarc_d in _DKIM.findall(msg.auth_results or ""):
        if registry.bank_for_domain(dkim_d or dmarc_d) == bank_id:
            return True
    return False


def sender_signed(msg: MessageMeta) -> bool:
    """Gmail's own result says the From address's domain really signed this email.
    Used in test mode, where the 'bank' is a personal address."""
    domain = sender_domain(msg.sender)
    if not domain:
        return False
    for dkim_d, dmarc_d in _DKIM.findall(msg.auth_results or ""):
        d = (dkim_d or dmarc_d).lower().lstrip("@")
        if d == domain or domain.endswith("." + d):
            return True
    return False


def is_forwarded(msg: MessageMeta) -> bool:
    return bool(_FWD_SUBJECT.search(msg.subject or "") or _FWD_BODY.search(msg.body or ""))


def addressed_to_user(msg: MessageMeta, user_addresses: list[str]) -> bool:
    """Whole-address match, so "a@x.com" doesn't match "aa@x.com"."""
    if not msg.to or not user_addresses:
        return True  # unknown: don't penalise
    recipients = {addr.lower() for _, addr in getaddresses([msg.to]) if addr}
    return bool(recipients & {a.lower() for a in user_addresses})


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
        # Test mode (dev builds only, see registry.dev_mode): the bank is whichever
        # one the email names, the signature must still pass for the test sender's
        # own domain, and a [date:YYYY-MM-DD] tag lets a test email look years old.
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
        dkim_pass=sender_signed(msg) if test else dkim_pass(msg, bank_id, registry),
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


def needs_body(ev: Evidence, full_statements: bool = True) -> bool:
    """Whether the full email (60 quota units, vs 20 for headers) is worth reading.
    `full_statements=False` reads a statement in full only when the header lacks the
    number or product: cheaper, but add-on cards further down are missed."""
    if ev.bank_id is None or ev.type not in BODY_WORTHY:
        return False
    if ev.type in (T.UPGRADE, T.REPLACEMENT):
        return len(ev.last4s) < 2 or not ev.products
    if ev.type == T.STATEMENT and full_statements:
        return True  # rare and valuable: read in full to catch add-on cards further down
    if ev.type in (T.TRANSACTION_ALERT, T.PAYMENT, T.POINTS):
        return not ev.last4s  # these rarely name the product; the number is what matters
    return not ev.last4s or not ev.products


def _configure_default() -> None:
    from .rules import source_bundle
    configure(source_bundle()["classify"])


_configure_default()
