"""Which bank email to believe, and how much.

1. Gate. Only mail Gmail itself verified for a bank's domain counts: DMARC passed for a
   registered bank domain, or a DKIM signature from one (see carddiscovery.classify.dkim_pass;
   only Gmail's own Authentication-Results header is read). A forged "From: alerts@hdfcbank.net",
   a look-alike domain, or mail signed only by a bulk-mail service never counts. Rejected mail
   stays recorded (so it is never read again) and is counted per bank, but creates no card,
   changes no status and teaches no number.
2. Sender history. An exact bank address with verified mail in 2 or more different months is a
   known sender; a new one is believed but weighs less until it shows up again.
3. Confirmation. A card is owned only with a strong verified email (statement, welcome, dispatch,
   fee, lifecycle), or verified mail in 2+ different months, or one weak verified email (alert,
   payment, points) from a known sender. Otherwise it waits: "New, waiting to confirm".
4. Big status changes need follow-up evidence: an upgrade or replacement counts only if the new
   number shows up in later verified mail, or the old one goes quiet; a closure only if no
   verified spending follows it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta

from carddiscovery.models import Evidence, EvidenceType as T

WEAK = {T.TRANSACTION_ALERT, T.PAYMENT, T.POINTS, T.OFFER_EXISTING_CARD, T.UNKNOWN}
USAGE = {T.TRANSACTION_ALERT, T.STATEMENT, T.PAYMENT}
LIFECYCLE_CHANGES = {T.UPGRADE, T.REPLACEMENT, T.CLOSURE}
KNOWN_SENDER_MONTHS = 2


def verified(ev: Evidence) -> bool:
    return bool(ev.bank_id) and bool(ev.dkim_pass)


def split_verified(evs: list[Evidence]) -> tuple[list[Evidence], Counter]:
    """(verified bank mail, rejected count per bank). Mail with no bank isn't 'rejected'."""
    kept, rejected = [], Counter()
    for e in evs:
        if verified(e):
            kept.append(e)
        elif e.bank_id and not e.test:
            rejected[e.bank_id] += 1
    return kept, rejected


def known_senders(evs: list[Evidence]) -> set[str]:
    months: dict[str, set[str]] = defaultdict(set)
    for e in evs:
        if e.sender and verified(e):
            months[e.sender].add(e.date.strftime("%Y-%m"))
    return {s for s, m in months.items() if len(m) >= KNOWN_SENDER_MONTHS}


def corroborate(evs: list[Evidence]) -> tuple[list[Evidence], int]:
    """Drop upgrade/replacement/closure evidence that later verified mail doesn't back up.
    Returns (kept, dropped)."""
    by_card: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
    for e in evs:
        for l4 in e.last4s:
            by_card[(e.bank_id, l4)].append(e)

    def later_mail(card, after, types=None, exclude=LIFECYCLE_CHANGES) -> bool:
        return any(x.date > after and x.type not in exclude and (types is None or x.type in types)
                   for x in by_card.get(card, ()))

    kept, dropped = [], 0
    for e in evs:
        if e.type in (T.UPGRADE, T.REPLACEMENT) and len(e.last4s) >= 2:
            old, new = (e.bank_id, e.last4s[0]), (e.bank_id, e.last4s[-1])
            if not (later_mail(new, e.date - timedelta(days=1)) or not later_mail(old, e.date + timedelta(days=7), USAGE)):
                dropped += 1
                continue
        elif e.type == T.CLOSURE and e.last4s:
            if later_mail((e.bank_id, e.last4s[0]), e.date + timedelta(days=7), USAGE):
                dropped += 1
                continue
        kept.append(e)
    return kept, dropped


def confirmed(card, known: set[str]) -> bool:
    evs = [e for e in card.evidence if verified(e)]
    if any(e.type not in WEAK for e in evs):
        return True
    if len({e.date.strftime("%Y-%m") for e in evs}) >= 2:
        return True
    return any(e.sender in known for e in evs)
