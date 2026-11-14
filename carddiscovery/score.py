"""Ownership and status scoring.

Ownership is a capped noisy-OR: each evidence *type* contributes once (its
strongest instance), so sixty statements don't count sixty times. Status comes
from evidence, never from recency alone: silence makes a card dormant, not closed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import USAGE_TYPES, Card, Evidence, EvidenceType as T, Status

WEIGHTS: dict[T, float] = {
    T.STATEMENT: 0.95,
    T.DISPATCH_ACTIVATION: 0.95,
    T.WELCOME: 0.90,
    T.ANNUAL_FEE: 0.85,
    T.RENEWAL: 0.85,
    T.REPLACEMENT: 0.90,  # names the new card number: as strong as a dispatch notice
    T.UPGRADE: 0.90,
    T.POINTS: 0.80,
    T.INACTIVITY_NOTICE: 0.85,
    T.CLOSURE: 0.85,  # a closure notice proves the card existed
    T.TRANSACTION_ALERT: 0.85,
    T.PAYMENT: 0.75,
    T.APPROVAL: 0.70,
    T.OFFER_EXISTING_CARD: 0.50,
    T.UNKNOWN: 0.30,
}

OWNED_THRESHOLD = 0.90
ACTIVE_WINDOW = timedelta(days=180)
LIFECYCLE = {T.ANNUAL_FEE, T.RENEWAL, T.POINTS, T.REPLACEMENT}
NEW_CARD = {T.WELCOME, T.DISPATCH_ACTIVATION, T.UPGRADE, T.REPLACEMENT}


def evidence_weight(ev: Evidence) -> float:
    w = WEIGHTS.get(ev.type, 0.0)
    if not ev.dkim_pass:
        w *= 0.3
    if ev.forwarded:
        w *= 0.2
    if ev.name_match is False:
        w *= 0.2
    if not ev.to_user:
        w *= 0.3
    return w


def ownership(card: Card) -> float:
    best: dict[T, float] = {}
    for ev in card.evidence:
        best[ev.type] = max(best.get(ev.type, 0.0), evidence_weight(ev))
    p_not = 1.0
    for w in best.values():
        p_not *= 1.0 - w
    return round(1.0 - p_not, 3)


def status(card: Card, now: datetime | None = None) -> tuple[Status, float]:
    now = now or datetime.now(timezone.utc)
    evs = sorted(card.evidence, key=lambda e: e.date)
    if not evs:
        return Status.UNKNOWN, 0.0

    if card.replaced_by is not None:
        return Status.REPLACED, 0.9

    def last(types) -> datetime | None:
        return max((e.date for e in evs if e.type in types), default=None)

    last_usage = last(USAGE_TYPES)
    closure = last({T.CLOSURE})
    if closure:
        after = [e for e in evs if e.date > closure + timedelta(days=7)
                 and e.type not in (T.CLOSURE, T.OFFER_EXISTING_CARD, T.POINTS)]
        if not after:
            return Status.CLOSED, 0.9

    inactivity = last({T.INACTIVITY_NOTICE})
    if inactivity and not (last_usage and last_usage > inactivity):
        return Status.POSSIBLY_CLOSED, 0.6

    if last_usage and now - last_usage <= ACTIVE_WINDOW:
        return Status.ACTIVE, 0.9

    # A card issued, activated or upgraded recently is new, not dormant.
    issued = last(NEW_CARD)
    if issued and now - issued <= ACTIVE_WINDOW:
        return Status.ACTIVE, 0.7

    if {e.type for e in evs} <= {T.APPROVAL, T.OFFER_EXISTING_CARD, T.UNKNOWN}:
        return Status.APPROVED_NOT_SEEN, 0.5

    lifecycle = last(LIFECYCLE)
    if lifecycle and now - lifecycle <= timedelta(days=400):
        return Status.DORMANT, 0.8  # unused, but the bank still treats it as open
    return Status.DORMANT, 0.5


REVIEW_FLAGS = {"addon", "ambiguous", "no_identifier"}


def score_all(cards: list[Card], now: datetime | None = None) -> None:
    for c in cards:
        c.ownership = ownership(c)
        c.status, c.status_confidence = status(c, now)
        if c.evidence and all(e.forwarded for e in c.evidence):
            c.flags.add("forwarded_only")
        if c.evidence and sum(e.name_match is False for e in c.evidence) > len(c.evidence) / 2:
            c.flags.add("name_mismatch")


def group(card: Card) -> str:
    """owned | review | history"""
    if card.status in (Status.CLOSED, Status.REPLACED):
        return "history"
    blocking = (card.flags & REVIEW_FLAGS) | ({"forwarded_only", "name_mismatch"} & card.flags)
    if card.ownership >= OWNED_THRESHOLD and not blocking:
        return "owned"
    return "review"
