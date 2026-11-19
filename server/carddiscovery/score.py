"""Ownership and status scoring.

Ownership is a capped noisy-OR: each evidence *type* contributes once (its
strongest instance), so sixty statements don't count sixty times. Status comes
from evidence, never from recency alone: silence makes a card dormant, not closed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import USAGE_TYPES, Card, Evidence, EvidenceType as T, Status

# Weights, thresholds and modifiers come from the rules bundle ("score" section).
# They are hand-set guesses until fitted on real, labelled cards.
WEIGHTS: dict[T, float] = {}
OWNED_THRESHOLD = 0.90
ACTIVE_WINDOW = timedelta(days=180)
LIFECYCLE_WINDOW = timedelta(days=400)
CLOSURE_GRACE = timedelta(days=7)
MODIFIERS: dict[str, float] = {}


def configure(cfg: dict) -> None:
    global WEIGHTS, OWNED_THRESHOLD, ACTIVE_WINDOW, LIFECYCLE_WINDOW, CLOSURE_GRACE, MODIFIERS
    WEIGHTS = {T(k): float(v) for k, v in cfg["weights"].items()}
    OWNED_THRESHOLD = float(cfg["owned_threshold"])
    ACTIVE_WINDOW = timedelta(days=cfg["active_days"])
    LIFECYCLE_WINDOW = timedelta(days=cfg["lifecycle_days"])
    CLOSURE_GRACE = timedelta(days=cfg["closure_grace_days"])
    MODIFIERS = dict(cfg["modifiers"])


LIFECYCLE = {T.ANNUAL_FEE, T.RENEWAL, T.POINTS, T.REPLACEMENT}
NEW_CARD = {T.WELCOME, T.DISPATCH_ACTIVATION, T.UPGRADE, T.REPLACEMENT}


def evidence_weight(ev: Evidence) -> float:
    w = WEIGHTS.get(ev.type, 0.0)
    if not ev.dkim_pass:
        w *= MODIFIERS["dkim_fail"]
    if ev.forwarded:
        w *= MODIFIERS["forwarded"]
    if ev.name_match is False:
        w *= MODIFIERS["name_mismatch"]
    if not ev.to_user:
        w *= MODIFIERS["not_to_user"]
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
        after = [e for e in evs if e.date > closure + CLOSURE_GRACE
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
    if lifecycle and now - lifecycle <= LIFECYCLE_WINDOW:
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
    """owned | review | history | rejected. The user's own answer always wins."""
    if card.decision == "no":
        return "rejected"
    if card.status in (Status.CLOSED, Status.REPLACED):
        return "history"
    if card.decision == "yes":
        return "owned"
    blocking = (card.flags & REVIEW_FLAGS) | ({"forwarded_only", "name_mismatch"} & card.flags)
    if card.ownership >= OWNED_THRESHOLD and not blocking:
        return "owned"
    return "review"


def _configure_default() -> None:
    from .rules import source_bundle
    configure(source_bundle()["score"])


_configure_default()
