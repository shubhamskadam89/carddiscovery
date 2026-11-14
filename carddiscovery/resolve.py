"""Entity resolution: group evidence into distinct cards.

Identity is bank + last4 where we have it. Evidence without a last4 is attached
only when there is exactly one matching card; otherwise it becomes a provisional
card for the user to confirm. We never guess between two candidates.
"""

from __future__ import annotations

from collections import Counter

from .models import (
    APPLICATION_TYPES, CARD_EVIDENCE_TYPES, Application, Card, Evidence, EvidenceType as T,
)


# Evidence that, without a number or product, says too little to justify a new card
# when the bank already has known cards.
WEAK_UNANCHORED = {T.POINTS, T.PAYMENT, T.TRANSACTION_ALERT, T.UNKNOWN, T.INACTIVITY_NOTICE}


def resolve(evidence: list[Evidence]) -> tuple[list[Card], list[Application]]:
    usable = [e for e in evidence if e.bank_id and (
        e.type in CARD_EVIDENCE_TYPES or (e.type == T.UNKNOWN and e.last4s))]
    usable.sort(key=lambda e: e.date)

    cards: dict[tuple[str, str], Card] = {}

    def card_for(bank: str, l4: str) -> Card:
        if (bank, l4) not in cards:
            cards[(bank, l4)] = Card(bank_id=bank, last4=l4, product=None)
        return cards[(bank, l4)]

    # Pass 1: evidence that names a card number.
    for ev in (e for e in usable if e.last4s):
        bank = ev.bank_id
        if ev.type in (T.UPGRADE, T.REPLACEMENT) and len(ev.last4s) >= 2:
            old, new = card_for(bank, ev.last4s[0]), card_for(bank, ev.last4s[-1])
            old.evidence.append(ev)
            new.evidence.append(ev)
            old.replaced_by = new
            if len(ev.products) >= 2:
                old.product_history.append((ev.date, ev.products[0]))
                new.product_history.append((ev.date, ev.products[-1]))
            elif ev.products:
                new.product_history.append((ev.date, ev.products[-1]))
            continue

        primary = card_for(bank, ev.last4s[0])
        primary.evidence.append(ev)
        if ev.type == T.OFFER_EXISTING_CARD:
            continue  # products in an offer are what's being sold, not what's held
        if ev.type == T.UPGRADE and ev.products:
            # Same number, new variant.
            primary.product_history.append((ev.date, ev.products[-1]))
        elif len(ev.products) == 1 and len(ev.last4s) == 1:
            primary.product_history.append((ev.date, ev.products[0]))
        for extra in ev.last4s[1:]:
            c = card_for(bank, extra)
            c.evidence.append(ev)
            if ev.addon:
                c.flags.add("addon")
        if ev.product_raw and not ev.products:
            primary.flags.add(f"raw:{ev.product_raw}")

    # Pass 2: evidence without a number.
    provisional: dict[tuple[str, str | None], Card] = {}
    for ev in (e for e in usable if not e.last4s):
        if ev.type == T.OFFER_EXISTING_CARD:
            continue
        bank = ev.bank_id
        same_bank = [c for c in cards.values() if c.bank_id == bank]
        product = ev.products[-1] if ev.products else None
        if product:
            matches = [c for c in same_bank if product in _products(c)]
            if len(matches) == 1:
                matches[0].evidence.append(ev)
                continue
            key = (bank, product)
            if key not in provisional:
                provisional[key] = Card(bank_id=bank, last4=None, product=product)
                provisional[key].product_history.append((ev.date, product))
            p = provisional[key]
            p.evidence.append(ev)
            if len(matches) > 1:
                p.flags.add("ambiguous")
        else:
            if len(same_bank) == 1:
                same_bank[0].evidence.append(ev)
                continue
            if same_bank and ev.type in WEAK_UNANCHORED:
                # e.g. "your reward points are expiring" from a bank where we already
                # know several cards: nothing to tie it to, and not worth a mystery card.
                continue
            key = (bank, None)
            if key not in provisional:
                provisional[key] = Card(bank_id=bank, last4=None, product=None)
                provisional[key].flags.add("no_identifier")
            provisional[key].evidence.append(ev)
            if ev.product_raw:
                provisional[key].flags.add(f"raw:{ev.product_raw}")

    all_cards = list(cards.values()) + list(provisional.values())
    for c in all_cards:
        c.product_history.sort(key=lambda x: x[0])
        if c.product_history:
            c.product = c.product_history[-1][1]
        else:
            raws = Counter(f[4:] for f in c.flags if f.startswith("raw:"))
            if raws:
                c.product = raws.most_common(1)[0][0] + " (unrecognised)"
    return all_cards, _applications(evidence, all_cards)


def _products(c: Card) -> set[str]:
    return {p for _, p in c.product_history}


def _applications(evidence: list[Evidence], cards: list[Card]) -> list[Application]:
    apps: dict[tuple[str, str | None], Application] = {}
    for ev in sorted((e for e in evidence if e.bank_id and e.type in APPLICATION_TYPES), key=lambda e: e.date):
        product = ev.products[-1] if ev.products else None
        key = (ev.bank_id, product)
        status = "rejected" if ev.type == T.APPLICATION_REJECTED else "in_progress"
        if key not in apps:
            apps[key] = Application(ev.bank_id, product, status, ev.date, ev.date)
        else:
            a = apps[key]
            a.last_seen = ev.date
            a.status = status
    # An application followed by a new numbered card of that bank/product was fulfilled.
    def fulfilled(a: Application) -> bool:
        return any(
            c.bank_id == a.bank_id and c.last4 and c.first_seen and c.first_seen >= a.first_seen
            and (a.product is None or a.product in _products(c))
            for c in cards
        )

    return [a for a in apps.values() if not fulfilled(a)]
