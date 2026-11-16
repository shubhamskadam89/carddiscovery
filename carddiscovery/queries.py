"""Query planner: turns the sender registry into typed, provider-neutral searches.

Each family has a fetch policy. Exhaustive families (rare, high-value mail) are
read in full; sampled families (transaction alerts) are read per time window
with a cap, which is enough to find every last4 without reading thousands of alerts.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import date, timedelta


@dataclass(frozen=True)
class Query:
    family: str
    senders: tuple[str, ...]
    subject_any: tuple[str, ...] = ()
    text_any: tuple[str, ...] = ()
    exclude_subject: tuple[str, ...] = ()
    after: date | None = None
    before: date | None = None
    max_results: int | None = None  # None = all pages
    extra: tuple[str, ...] = ()  # raw Gmail operators, e.g. "-category:promotions", "has:attachment"

    def to_gmail(self) -> str:
        parts = ["from:(" + " OR ".join(self.senders) + ")"]
        if self.subject_any:
            parts.append("subject:(" + " OR ".join(_q(t) for t in self.subject_any) + ")")
        if self.text_any:
            parts.append("(" + " OR ".join(_q(t) for t in self.text_any) + ")")
        for t in self.exclude_subject:
            parts.append(f"-subject:{_q(t)}")
        if self.after:
            parts.append(f"after:{self.after:%Y/%m/%d}")
        if self.before:
            parts.append(f"before:{self.before:%Y/%m/%d}")
        parts.extend(self.extra)
        return " ".join(parts)


# Smallest window we split down to. A full window this size keeps its capped
# sample instead of being split again, so dense alert history costs at most
# (years × 2) windows × the cap, not every alert.
MIN_WINDOW_DAYS = 183


def is_full(q: Query, ids: list[str]) -> bool:
    return q.max_results is not None and len(ids) >= q.max_results


def split(q: Query) -> list[Query]:
    """Halve a dated window, or return [] if it is already as small as we go."""
    if not (q.after and q.before) or (q.before - q.after).days <= MIN_WINDOW_DAYS:
        return []
    mid = q.after + (q.before - q.after) / 2
    return [replace(q, before=mid), replace(q, after=mid)]


def _q(term: str) -> str:
    return f'"{term}"' if " " in term else term


# Subject keywords per family. These only aim the search; the classifier
# decides what each email really is.
FAMILIES: dict[str, tuple[str, ...]] = {
    "statement": ("statement", "e-statement", "estatement"),
    "onboarding": (
        "welcome", "approved", "dispatched", "dispatch", "delivered", "activated",
        "activation", "on its way", "card is ready", "pin", "green pin", "kit",
    ),
    "lifecycle": (
        "renewal", "renewed", "reissue", "re-issued", "annual fee", "membership fee",
        "fee waiver", "upgrade", "upgraded", "replacement", "replaced", "closure",
        "closed", "inactive", "points", "reward", "limit", "expiry", "expiring", "expire",
    ),
    "application": ("application", "applied", "regret", "unable to approve"),
    "payment": ("payment received", "payment due", "due date", "reminder", "minimum amount"),
}

SAMPLED_FAMILY_SUBJECTS = ("transaction", "spent", "txn", "alert", "used", "debited", "purchase")

# Safety net: anything from a bank mentioning a card number shape.
CATCH_ALL_TEXT = ("credit card", "card ending", "ending with", "ending in", "card no")


def plan(
    domains: list[str],
    today: date,
    history_years: int = 15,
    alerts_per_window: int = 40,
    catch_all_cap: int = 2000,
    chunk: int = 25,
) -> list[Query]:
    """All queries for a full discovery run, highest-value first.

    `history_years` is how far back the user agreed to scan. It limits every
    search, so mail older than that is never fetched.
    """
    chunks = [tuple(domains[i:i + chunk]) for i in range(0, len(domains), chunk)]
    # Gmail's before: is exclusive, so the range ends tomorrow to include today.
    end = today + timedelta(days=1)
    start = date(today.year - history_years, today.month, 1)
    out: list[Query] = []
    for family, words in FAMILIES.items():
        for c in chunks:
            out.append(Query(family=family, senders=c, subject_any=words, exclude_subject=("otp",),
                             after=start))

    # Alerts are sampled adaptively: one capped search over the whole range per
    # sender group. The pipeline splits a window in half only when it comes back
    # full, so sparse alerts cost one search and dense ones get spread-out samples.
    for c in chunks:
        out.append(Query(
            family="alerts", senders=c, subject_any=SAMPLED_FAMILY_SUBJECTS,
            exclude_subject=("otp",), after=start, before=end,
            max_results=alerts_per_window,
        ))

    for c in chunks:
        out.append(Query(
            family="catch_all", senders=c, text_any=CATCH_ALL_TEXT,
            exclude_subject=("otp",), after=start, max_results=catch_all_cap,
        ))
    return out
