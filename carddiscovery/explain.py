"""Plain-language explanations: why we think this is your card, and what we're unsure of.

Users see reasons, never scores. Everything here is built from derived evidence
(types, dates, flags), so no email text is needed or shown.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from .models import Card, EvidenceType as T, Status
from .registry import Registry
from .score import group

_NOUN = {
    T.STATEMENT: ("statement", "statements"),
    T.DISPATCH_ACTIVATION: ("card dispatch or activation email", "card dispatch or activation emails"),
    T.WELCOME: ("welcome email", "welcome emails"),
    T.APPROVAL: ("approval email", "approval emails"),
    T.ANNUAL_FEE: ("annual fee notice", "annual fee notices"),
    T.RENEWAL: ("renewal notice", "renewal notices"),
    T.POINTS: ("reward points email", "reward points emails"),
    T.INACTIVITY_NOTICE: ("inactivity warning", "inactivity warnings"),
    T.TRANSACTION_ALERT: ("transaction alert", "transaction alerts"),
    T.PAYMENT: ("payment email", "payment emails"),
    T.UPGRADE: ("upgrade email", "upgrade emails"),
    T.REPLACEMENT: ("replacement card email", "replacement card emails"),
    T.CLOSURE: ("closure confirmation", "closure confirmations"),
    T.OFFER_EXISTING_CARD: ("offer naming this card", "offers naming this card"),
    T.OTP: ("one-time password email for a payment", "one-time password emails for payments"),
    T.UNKNOWN: ("other bank email", "other bank emails"),
}
_ORDER = list(_NOUN)  # strongest-first order for listing evidence


def month(d: datetime | None) -> str:
    return d.strftime("%b %Y") if d else "unknown date"


EMAILS_SHOWN = 25


def emails(card: Card, limit: int = EMAILS_SHOWN) -> list[dict]:
    """The emails behind a card, for "where did you see this?": the newest of each kind first
    (so 200 alerts don't hide the one welcome email), then the newest of the rest. Only what
    evidence keeps: date, kind, what it showed, and the Gmail message id to open it. No subject
    or other email text: that's never stored, and opening the email in Gmail shows it."""
    evs = sorted(card.evidence, key=lambda e: e.date, reverse=True)
    first, rest, seen = [], [], set()
    for e in evs:
        (rest if e.type in seen else first).append(e)
        seen.add(e.type)
    out = []
    for e in (first + rest)[:limit]:
        showed = []
        if e.last4s:
            showed.append(" ".join(f"••{x}" for x in e.last4s[:3]))
        elif e.last_digits:
            showed.append(f"ending {e.last_digits}")
        if e.product:
            showed.append(e.product)
        pdf = e.message_id.endswith("#pdf")
        out.append({
            "id": e.message_id.split("#")[0],  # Gmail's message id (a PDF's row points to its email)
            "date": e.date.date().isoformat(),
            "kind": ("statement PDF" if pdf else _NOUN.get(e.type, ("email",))[0]),
            "showed": ", ".join(showed) or None,
            "via": e.via, "forwarded": e.forwarded,
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


@dataclass
class Explanation:
    title: str            # "HDFC Bank Regalia Gold ••7410"
    status: str           # "Active", "Not used recently", ...
    headline: str         # one sentence: the main reason
    evidence: list[str]   # "3 statements, latest Nov 2026"
    concerns: list[str] = field(default_factory=list)  # why it needs confirming
    question: str | None = None                        # asked when the card is in review


def explain(card: Card, registry: Registry) -> Explanation:
    bank = registry.banks[card.bank_id].name if card.bank_id in registry.banks else card.bank_id
    product = card.product or "credit card (variant unknown)"
    number = f" ••{card.last4}" if card.last4 else (f" ••{card.partial}" if card.partial else "")
    if card.last4 and card.twin and card.twin.isdigit():
        number = f" {card.twin}••{card.last4}"  # two cards end in these 4 digits: show how they start
    title = f"{bank} {product}{number}"

    by_type: dict[T, list[datetime]] = {}
    for e in card.evidence:
        by_type.setdefault(e.type, []).append(e.date)
    evidence = []
    for t in sorted(by_type, key=lambda t: _ORDER.index(t) if t in _ORDER else 99):
        dates = sorted(by_type[t])
        one, many = _NOUN.get(t, (t.value, t.value))
        if len(dates) == 1:
            evidence.append(f"{one.capitalize()}, {month(dates[0])}")
        else:
            evidence.append(f"{len(dates)} {many}, {month(dates[0])} to {month(dates[-1])}")

    status, headline = _status_sentence(card)
    concerns = _concerns(card)
    g = group(card)
    question = None
    if g == "review":
        question = "Is this your card?"
    elif card.status == Status.POSSIBLY_CLOSED:
        question = "Do you still have this card?"
    return Explanation(title, status, headline, evidence, concerns, question)


def _status_sentence(card: Card) -> tuple[str, str]:
    evs = card.evidence
    last_seen = month(card.last_seen)

    def last(types):
        return max((e.date for e in evs if e.type in types), default=None)

    if card.decision == "no":
        return "Not yours", "You told us this card isn't yours."
    if card.status == Status.REPLACED and card.replaced_by:
        new = f"••{card.replaced_by.last4}" if card.replaced_by.last4 else "a new card"
        how = "an upgrade" if any(e.type == T.UPGRADE for e in evs) else "a replacement"
        return "Replaced", f"Replaced by {new} after {how} in {month(last({T.UPGRADE, T.REPLACEMENT}))}."
    if card.status == Status.CLOSED:
        return "Closed", f"The bank confirmed this card was closed ({month(last({T.CLOSURE}))})."
    if card.status == Status.POSSIBLY_CLOSED:
        return ("May be closed",
                f"The bank warned it may close this unused card ({month(last({T.INACTIVITY_NOTICE}))}).")
    if card.status == Status.ACTIVE:
        used = last({T.TRANSACTION_ALERT, T.STATEMENT, T.PAYMENT})
        if used:
            return "Active", f"In use: latest activity {month(used)}."
        return "New", f"Recently issued or activated ({last_seen})."
    if card.status == Status.APPROVED_NOT_SEEN:
        return ("Approved, not seen since",
                f"An approval in {month(last({T.APPROVAL}))}, but no card number or spending since.")
    if card.status == Status.DORMANT:
        fee = last({T.ANNUAL_FEE, T.RENEWAL, T.POINTS})
        if fee and (not card.last_seen or fee >= card.last_seen):
            return "Not used recently", f"No spending seen lately, but the bank still sent a notice in {month(fee)}."
        return "Not used recently", f"No bank email about this card since {last_seen}. It's probably still open."
    return "Unknown", f"Last seen {last_seen}."


def _concerns(card: Card) -> list[str]:
    out = []
    f = card.flags
    if "name_mismatch" in f:
        out.append("The emails are addressed to someone else by name.")
    if "app_only" in f:
        out.append("We've only seen it in bill-payment app emails (such as CRED), which can also "
                   "be used to pay someone else's card.")
    if "forwarded_only" in f:
        out.append("We only saw it in an email forwarded to you.")
    if "addon" in f:
        out.append("It looks like an add-on card listed on another card's statement.")
    if "maybe_new" in f:
        out.append("The bank sent a welcome or approval email well after your other card "
                   "from this bank started, so this may be a new card.")
    if "same_last4" in f:
        out.append("Another card from this bank ends in the same 4 digits; we told them apart "
                   "by the card names in the bank's emails.")
    if "no_identifier" in f:
        out.append("The bank's emails didn't include a card number.")
    if card.evidence and not any(e.dkim_pass for e in card.evidence):
        out.append("We couldn't confirm the emails really came from the bank.")
    if card.evidence and not any(e.to_user for e in card.evidence):
        out.append("The emails were sent to a different address.")
    if not out and group(card) == "review":
        kinds = Counter(e.type for e in card.evidence)
        if set(kinds) <= {T.TRANSACTION_ALERT, T.PAYMENT, T.OFFER_EXISTING_CARD, T.UNKNOWN}:
            out.append("We've only seen alerts or payment emails for it, not a statement or welcome email.")
        elif set(kinds) <= {T.APPROVAL}:
            out.append("We saw the approval, but never the card itself.")
    return out
