"""Entity resolution: group evidence into distinct cards.

Identity is bank + last4 where we have it. Evidence without a last4 is attached
only when exactly one card fits it. If several known cards fit, it is set aside: it
is about one of them and doesn't show a new card. If none fits, it becomes a
provisional card for the user to confirm. We never guess between two candidates.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import (
    APPLICATION_TYPES, CARD_EVIDENCE_TYPES, Application, Card, Evidence, EvidenceType as T,
)
from .registry import _extends


# Mail sent when a card is issued. Without a number it can only belong to a known card
# if it isn't much later than that card's first numbered email; otherwise it is likely
# a new card. Dispatch/activation mail is left out on purpose: its rule also matches
# PIN changes and re-activations, which come all through a card's life.
ISSUANCE = {T.WELCOME, T.APPROVAL}
ISSUANCE_GRACE = timedelta(days=90)

_SET_ASIDE = object()  # _pick's answer when several known cards fit equally

# Mail that states which product a card is. An unrecognised "... Card" phrase is used as a
# card's name only from these: in OTP, alert or delivery subjects it is "One Time Password",
# "Activate" or "Delivery Alert", not a product.
NAMES_PRODUCT = {T.STATEMENT, T.WELCOME, T.APPROVAL, T.UPGRADE, T.REPLACEMENT, T.RENEWAL, T.ANNUAL_FEE}
# (Not dispatch/delivery mail: its subjects are notices, "Delivery Alert Yesbank".)
# Mail whose product match is often the merchant: "Rs 500 spent on Flipkart using your Axis
# card" reads as the Flipkart card. Such a name is used only when no other mail names the card,
# and doesn't stop a statement ("MY Zone ... ending XX30") from naming it.
WEAK_PRODUCT = {T.TRANSACTION_ALERT, T.OTP}


@dataclass
class SetAside:
    """An email about a card we already have that can't say which one. Kept (without
    text) so it can be counted, and re-read or asked about later."""
    message_id: str
    bank_id: str
    type: T
    reason: str      # "several_cards": no number, fits 2+ cards; "shared_last4": number shared by twins
    candidates: int  # how many cards it could be
    last4: str | None = None
    used_body: bool = False  # False: only the header was read, the body may say more


@dataclass
class Resolution:
    cards: list[Card]
    applications: list[Application]
    set_aside: list[SetAside] = field(default_factory=list)


def resolve(evidence: list[Evidence]) -> tuple[list[Card], list[Application]]:
    r = resolve_full(evidence)
    return r.cards, r.applications


def resolve_full(evidence: list[Evidence]) -> Resolution:
    usable = [e for e in evidence if e.bank_id and (
        (e.type in CARD_EVIDENCE_TYPES and e.type != T.OTP) or (e.type in (T.UNKNOWN, T.OTP) and e.last4s))]
    # OTP mail only adds use to a card other mail already shows: "OTP for transaction on your
    # Axis Bank Card XX1234" is sent for debit cards too, so it must never create a card.
    shown = {(e.bank_id, l4) for e in usable if e.type != T.OTP for l4 in e.last4s}
    usable = [e for e in usable if e.type != T.OTP or all((e.bank_id, l4) in shown for l4 in e.last4s)]
    usable.sort(key=lambda e: e.date)

    cards: dict[tuple[str, str, str | None], Card] = {}
    weak: dict[int, list[str]] = {}  # id(card) -> product names from alerts/OTPs (WEAK_PRODUCT)
    twins, inferred = _same_last4_twins([e for e in usable if e.last4s])
    set_aside: dict[tuple[str, str | None], SetAside] = {}

    def put_aside(ev: Evidence, reason: str, candidates: int, l4: str | None = None) -> None:
        set_aside.setdefault((ev.message_id, l4), SetAside(
            ev.message_id, ev.bank_id, ev.type, reason, candidates, l4, ev.used_body))

    def card_for(bank: str, l4: str, ev: Evidence) -> Card | None:
        """None: this email's number is shared by two cards and it can't say which."""
        twin = twins.get((ev.message_id, bank, l4))
        if twin is _SET_ASIDE:
            n = len({t for (_, b, l), t in twins.items() if (b, l) == (bank, l4) and t is not _SET_ASIDE})
            put_aside(ev, "shared_last4", n, l4)
            return None
        if (bank, l4, twin) not in cards:
            c = Card(bank_id=bank, last4=l4, product=None, twin=twin)
            if (bank, l4) in inferred:
                c.flags.add("same_last4")
            cards[(bank, l4, twin)] = c
        return cards[(bank, l4, twin)]

    # Pass 1: evidence that names a card number.
    for ev in (e for e in usable if e.last4s):
        bank = ev.bank_id
        if ev.type in (T.UPGRADE, T.REPLACEMENT) and len(ev.last4s) >= 2:
            old, new = card_for(bank, ev.last4s[0], ev), card_for(bank, ev.last4s[-1], ev)
            for c in (old, new):
                if c is not None:
                    c.evidence.append(ev)
            if old is not None and new is not None:
                old.replaced_by = new
                if len(ev.products) >= 2:
                    old.product_history.append((ev.date, ev.products[0]))
            if new is not None and ev.products:
                new.product_history.append((ev.date, ev.products[-1]))
            continue

        primary = card_for(bank, ev.last4s[0], ev)
        if primary is not None:
            primary.evidence.append(ev)
        if ev.type == T.OFFER_EXISTING_CARD:
            # Products in an offer are what's being sold, not what's held, unless the offer
            # names the card with its number ("your HSBC Visa Platinum Credit Card ending
            # with 9719"): the number and the product side by side, as in a statement.
            if ev.held and ev.product and primary is not None:
                primary.product_history.append((ev.date, ev.product))
            continue
        if primary is not None:
            if ev.type == T.UPGRADE and ev.products:
                # Same number, new variant.
                primary.product_history.append((ev.date, ev.products[-1]))
            elif ev.product and (len(ev.last4s) == 1 or ev.addon):
                # With add-on numbers present, the product belongs to the primary card.
                if ev.type in WEAK_PRODUCT:
                    weak.setdefault(id(primary), []).append(ev.product)
                else:
                    primary.product_history.append((ev.date, ev.product))
        for extra in ev.last4s[1:]:
            c = card_for(bank, extra, ev)
            if c is None:
                continue
            c.evidence.append(ev)
            if ev.addon:
                c.flags.add("addon")
        if primary is not None and ev.product_raw and not ev.products and ev.type in NAMES_PRODUCT:
            primary.flags.add(f"raw:{ev.product_raw}")

    # Pass 2: evidence without a number. Each email goes to the one card it fits;
    # if several fit it is set aside (it's about one of them, and doesn't show a new
    # card); if none fits it becomes a provisional card for the user to confirm.
    # Taken before Pass 2 adds anything, so the result doesn't depend on email order:
    lifespans = _replacement_lifespans(list(cards.values()))
    starts = {id(c): c.first_seen for c in cards.values()}
    provisional: dict[tuple[str, str | None, str | None], Card] = {}

    def provisional_card(bank: str, product: str | None, ev: Evidence) -> Card:
        key = (bank, product, ev.last_digits)  # "Airtel ••87" and "Airtel ••12" are two cards
        if key not in provisional:
            p = Card(bank_id=bank, last4=None, product=product, partial=ev.last_digits)
            if product:
                p.product_history.append((ev.date, product))
            else:
                p.flags.add("no_identifier")
            provisional[key] = p
        return provisional[key]

    unnumbered = [e for e in usable if not e.last4s and e.type != T.OFFER_EXISTING_CARD]

    # 2a: emails that name a product. Done first so 2b can use the cards they create.
    adopters = _unnamed_adopters([e for e in unnumbered if _product_of(e)], list(cards.values()))
    # Emails showing the card's last digits go first: "PhonePe Ultimo … ending 54" names ••3254,
    # and the same product's emails without digits then find it named, instead of each
    # becoming a "number unknown" card of their own.
    for ev in sorted((e for e in unnumbered if _product_of(e)), key=lambda e: (e.last_digits is None, e.date)):
        product = _product_of(ev)
        # Cards with exactly this name; failing that, a card called by a fuller name of it:
        # "Diners Club Black" mail for the card named "Diners Club Black Metal" (the same rule
        # as _current_product). An exact name wins, so after Regalia ••1111 became Regalia Gold
        # ••9999, "Regalia" mail still goes to ••1111.
        bank_cards = [c for c in cards.values() if c.bank_id == ev.bank_id]
        named = ([c for c in bank_cards if product in _products(c)]
                 or [c for c in bank_cards if any(_extends(p, product) for p in _products(c))])
        matches = _digits_fit(_alive_at(named, ev.date, lifespans), ev)
        unnamed, one_product = adopters.get(ev.bank_id, ([], None))
        # The email's last digits ("ending XX30") can pick the card; without them, only when the
        # bank's number-less mail is about one unknown product and this email names it.
        if ev.last_digits:
            adoptable = _digits_fit(unnamed, ev)
        else:
            adoptable = unnamed if one_product and (product == one_product or _extends(product, one_product)) else []
        # Digits decide even when another card has this name: "IRCTC Platinum, ending 72" is
        # ••2872 (no product yet), not ••7724 (the IRCTC Platinum that ends in 24).
        if (not named or (ev.last_digits and not matches)) and adoptable:
            # No card has this name, but the bank's card(s) with no known product may be it.
            target, fitting = _pick(_alive_at(adoptable, ev.date, lifespans), ev, starts)
            if isinstance(target, Card):
                target.evidence.append(ev)
                target.product_history.append((ev.date, product))  # the card learns its name
                continue
            if target is _SET_ASIDE:
                put_aside(ev, "several_cards", fitting)
                continue
            target = provisional_card(ev.bank_id, product, ev)  # a welcome too late for them: maybe new
            target.flags.add("maybe_new")
            target.evidence.append(ev)
            continue
        target, fitting = _pick(matches, ev, starts)
        if target is _SET_ASIDE:
            put_aside(ev, "several_cards", fitting)
            continue
        if target is None:
            target = provisional_card(ev.bank_id, product, ev)
            if matches:
                target.flags.add("maybe_new")
        target.evidence.append(ev)

    # 2b: emails with neither number nor product. At a bank with no numbered card, a
    # lone provisional card from 2a counts as that bank's card, so the user isn't
    # asked about "HDFC Regalia, number unknown" and "HDFC card, number unknown" both.
    starts.update({id(p): p.first_seen for p in provisional.values()})
    for ev in (e for e in unnumbered if not _product_of(e)):
        bank = ev.bank_id
        known = [c for c in cards.values() if c.bank_id == bank]
        if known:
            candidates = _digits_fit(_alive_at(known, ev.date, lifespans), ev)
        else:
            candidates = _digits_fit([p for (b, prod, _), p in provisional.items()
                                      if b == bank and prod is not None], ev)
        if not candidates and ev.last_digits:
            # "e-PIN for your card no. ending 16": no known number ends in 16, but statements
            # made "IRCTC Platinum ••16". It's that card, not another one.
            candidates = [p for (b, prod, d), p in provisional.items()
                          if b == bank and prod is not None and d == ev.last_digits]
        target, fitting = _pick(candidates, ev, starts)
        if target is _SET_ASIDE:
            put_aside(ev, "several_cards", fitting)
            continue
        if target is None:
            target = provisional_card(bank, None, ev)
            if candidates:
                target.flags.add("maybe_new")
            if ev.product_raw and ev.type in NAMES_PRODUCT:
                target.flags.add(f"raw:{ev.product_raw}")
        target.evidence.append(ev)

    all_cards = list(cards.values()) + list(provisional.values())
    for c in all_cards:
        c.product_history.sort(key=lambda x: x[0])
        if c.product_history:
            c.product = _current_product(c)
        elif weak.get(id(c)):
            c.product = Counter(weak[id(c)]).most_common(1)[0][0]  # only alerts named it
        else:
            raws = Counter(f[4:] for f in c.flags if f.startswith("raw:"))
            if raws:
                c.product = raws.most_common(1)[0][0] + " (unrecognised)"
    return Resolution(all_cards, _applications(evidence, all_cards), list(set_aside.values()))


def _products(c: Card) -> set[str]:
    return {p for _, p in c.product_history}


def _current_product(c: Card) -> str:
    """The newest product name, unless the card was also called by a fuller name of
    it: a statement saying "Regalia" doesn't undo the "Regalia Gold" seen before on
    the same card. Only an upgrade or replacement email changes the product, so names
    from before the latest one don't count. Needs product_history sorted by date."""
    current = c.product_history[-1][1]
    changes = [e.date for e in c.evidence if e.type in (T.UPGRADE, T.REPLACEMENT)]
    since = max((d for d in changes if d <= c.product_history[-1][0]), default=None)
    names = [p for d, p in c.product_history if since is None or d >= since]
    while True:  # "Emeralde" -> "Emeralde Private" -> "Emeralde Private Metal"
        fuller = [p for p in names if _extends(p, current)]
        if not fuller:
            return current
        current = fuller[-1]  # the newest of them


# ---------------------------------------------------------------- same last 4 (rare)

def _same_last4_twins(numbered: list[Evidence]):
    """The rare case of two different cards at one bank ending in the same 4 digits.

    Normally a card is bank + last4 and this returns nothing. Only when one number's
    mail shows a conflict is it split:
      - different leading digits ("4375XXXXXXXX4321" vs "5241XXXXXXXX4321"): certain;
      - else, unrelated product names that keep alternating with no upgrade or
        replacement email (Millennia, Regalia, Millennia, Regalia): likely, so those
        cards also go to review ("same_last4").
    Returns ({(message_id, bank, last4): twin tag or _SET_ASIDE}, {(bank, last4) split
    by product name}). Emails that can't say which card they're about are set aside."""
    groups: dict[tuple[str, str], list[Evidence]] = {}
    for ev in numbered:
        for l4 in ev.last4s:
            groups.setdefault((ev.bank_id, l4), []).append(ev)
    twins: dict[tuple[str, str, str], object] = {}
    inferred: set[tuple[str, str]] = set()
    for (bank, l4), evs in groups.items():
        assign = _split_by_prefix(evs, l4)
        if assign is None:
            assign = _split_by_product_line(evs)
            if assign is None:
                continue
            inferred.add((bank, l4))
        for ev in evs:
            twins[(ev.message_id, bank, l4)] = assign.get(ev.message_id, _SET_ASIDE)
    return twins, inferred


def _consistent(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)  # "4" and "4375" can be the same card


def _split_by_prefix(evs: list[Evidence], l4: str) -> dict[str, str] | None:
    prefixes = sorted({p for ev in evs for p in ev.number_prefixes.get(l4, [])}, key=len, reverse=True)
    clusters: list[str] = []  # longest form of each card's leading digits
    for p in prefixes:
        if not any(_consistent(p, c) for c in clusters):
            clusters.append(p)
    if len(clusters) < 2:
        return None
    assign: dict[str, str] = {}
    for ev in evs:
        own = ev.number_prefixes.get(l4, [])
        fits = [c for c in clusters if own and all(_consistent(p, c) for p in own)]
        if len(fits) == 1:
            assign[ev.message_id] = fits[0]
    # Emails without leading digits: by product, when only one of the cards has it.
    products = {c: {ev.product for ev in evs if assign.get(ev.message_id) == c and ev.product}
                for c in clusters}
    for ev in evs:
        if ev.message_id in assign or not ev.product or len(ev.last4s) != 1:
            continue
        holders = [c for c in clusters if ev.product in products[c]]
        if len(holders) == 1:
            assign[ev.message_id] = holders[0]
    return assign


def _split_by_product_line(evs: list[Evidence]) -> dict[str, str] | None:
    if any(ev.type in (T.UPGRADE, T.REPLACEMENT) for ev in evs):
        return None  # the product may really have changed
    named = sorted((ev for ev in evs if len(ev.last4s) == 1 and ev.product
                    and ev.type != T.OFFER_EXISTING_CARD), key=lambda e: e.date)
    names = {ev.product for ev in named}
    # "Regalia" and "Regalia Gold" are one line; "Millennia" is another.
    line = {p: min((q for q in names if q == p or _extends(p, q)), key=len) for p in names}
    seq = [line[ev.product] for ev in named]
    if sum(n >= 2 for n in Counter(seq).values()) < 2:
        return None  # one stray email naming another product isn't a second card
    runs = [x for i, x in enumerate(seq) if i == 0 or seq[i - 1] != x]
    if len(runs) == len(set(runs)):
        return None  # switched once and stayed: a product change, not two cards
    return {ev.message_id: line[ev.product] for ev in named}


def _digits_fit(candidates: list[Card], ev: Evidence) -> list[Card]:
    """Leave out cards whose number can't end in the digits the email shows ("XX87" rules
    out ••6190). Cards with no number, or no digits on the email, always fit."""
    d = ev.last_digits
    if not d:
        return candidates
    return [c for c in candidates
            if (c.last4.endswith(d) if c.last4 else c.partial in (None, d))]


def _unnamed_adopters(named: list[Evidence], numbered: list[Card]) -> dict[str, tuple[list[Card], str | None]]:
    """Per bank: its numbered cards with no product yet (a name from alerts doesn't count),
    and the one unknown product its number-less mail is about, if there is one: named by every
    such email, or dominant (in 3+ emails and 4x as many as any other, so a stray advert
    naming "YES First" once doesn't block "ACE" named in 26 statements). E.g. HSBC statements
    say "Live+" with no number, and HSBC ••3408 is known only from emails that never name a
    product: they're the same card, not two. Otherwise only the last digits can say which."""
    counts: dict[str, Counter] = {}
    for ev in named:
        p = _product_of(ev)
        if not any(c.bank_id == ev.bank_id and p in _products(c) for c in numbered):
            counts.setdefault(ev.bank_id, Counter())[p] += 1
    out: dict[str, tuple[list[Card], str | None]] = {}
    for bank in {c.bank_id for c in numbered}:  # every bank with unnamed cards: an ending can pick one
        per_product = counts.get(bank, Counter())
        unnamed = [c for c in numbered if c.bank_id == bank and not c.product_history]
        if not unnamed:
            continue
        if not per_product:
            out[bank] = (unnamed, None)
            continue
        products = set(per_product)
        root = {p: min((q for q in products if q == p or _extends(p, q)), key=len) for p in products}
        by_root = Counter()
        for p, n in per_product.items():
            by_root[root[p]] += n
        ranked = by_root.most_common()
        one = ranked[0][0] if (len(ranked) == 1 or (ranked[0][1] >= DOMINANT_MIN
                                                   and ranked[0][1] >= DOMINANT_RATIO * ranked[1][1])) else None
        out[bank] = (unnamed, one)
    return out


DOMINANT_MIN, DOMINANT_RATIO = 3, 4


def _product_of(ev: Evidence) -> str | None:
    return ev.product or (ev.products[-1] if ev.products else None)


def _pick(candidates: list[Card], ev: Evidence, starts: dict[int, datetime | None]):
    """The card an un-numbered email belongs to, and how many cards fit it. The card is
    that card, None (no known card fits: make a provisional one) or _SET_ASIDE (several
    fit: it's one of them, can't say which)."""
    def fits(c: Card) -> bool:
        if ev.type not in ISSUANCE:
            return True
        start = starts.get(id(c))
        return start is None or ev.date <= start + ISSUANCE_GRACE
    fitting = [c for c in candidates if fits(c)]
    if not fitting:
        return None, 0
    return (fitting[0] if len(fitting) == 1 else _SET_ASIDE), len(fitting)


def _replacement_lifespans(cards: list[Card]) -> dict[int, tuple[datetime | None, datetime | None]]:
    """When each card in a replacement pair could have been the one in use.

    An old card that was replaced (upgrade or reissue to a new number) lasts until its
    last numbered email; the card that replaced it starts at its first. Cards not in a
    replacement have no limits: how far back their mail goes says nothing about when
    they were issued or closed. Keyed by id() because Card isn't hashable.
    """
    spans: dict[int, tuple[datetime | None, datetime | None]] = {}
    for old in cards:
        new = old.replaced_by
        if new is None or new is old:
            continue
        start, _ = spans.get(id(old), (None, None))
        spans[id(old)] = (start, old.last_seen)
        _, end = spans.get(id(new), (None, None))
        spans[id(new)] = (new.first_seen, end)
    return spans


def _alive_at(candidates: list[Card], when: datetime,
              spans: dict[int, tuple[datetime | None, datetime | None]]) -> list[Card]:
    """Leave out replaced cards that had already gone, and replacements not yet
    issued, on the email's date. Only narrows: if that would leave no candidate, the
    original list is kept, so this never creates a card that wasn't created before.
    Boundaries are inclusive, so on the replacement day itself both cards stay in."""
    def alive(c: Card) -> bool:
        start, end = spans.get(id(c), (None, None))
        return (start is None or when >= start) and (end is None or when <= end)
    return [c for c in candidates if alive(c)] or candidates


def _applications(evidence: list[Evidence], cards: list[Card]) -> list[Application]:
    apps: dict[tuple[str, str | None], Application] = {}
    for ev in sorted((e for e in evidence if e.bank_id and e.type in APPLICATION_TYPES), key=lambda e: e.date):
        product = ev.product or (ev.products[-1] if ev.products else None)
        key = (ev.bank_id, product)
        status = "rejected" if ev.type == T.APPLICATION_REJECTED else "in_progress"
        if key not in apps:
            apps[key] = Application(ev.bank_id, product, status, ev.date, ev.date)
        else:
            a = apps[key]
            a.last_seen = ev.date
            a.status = status
    # An in-progress application followed by a new numbered card of that bank/product
    # was fulfilled. A rejected one stays listed even if another card came later.
    def fulfilled(a: Application) -> bool:
        return a.status == "in_progress" and any(
            c.bank_id == a.bank_id and c.last4 and c.first_seen and c.first_seen >= a.first_seen
            and (a.product is None or a.product in _products(c))
            for c in cards
        )

    return [a for a in apps.values() if not fulfilled(a)]
