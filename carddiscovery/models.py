"""Core data types shared by every pipeline stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class EvidenceType(str, Enum):
    STATEMENT = "statement"
    DISPATCH_ACTIVATION = "dispatch_activation"
    WELCOME = "welcome"
    APPROVAL = "approval"
    ANNUAL_FEE = "annual_fee"
    RENEWAL = "renewal"
    POINTS = "points"
    INACTIVITY_NOTICE = "inactivity_notice"
    TRANSACTION_ALERT = "transaction_alert"
    PAYMENT = "payment"
    UPGRADE = "upgrade"
    REPLACEMENT = "replacement"
    CLOSURE = "closure"
    OFFER_EXISTING_CARD = "offer_existing_card"
    APPLICATION_RECEIVED = "application_received"
    APPLICATION_REJECTED = "application_rejected"
    PROMOTIONAL = "promotional"
    OTP = "otp"
    NOT_CREDIT_CARD = "not_credit_card"
    UNKNOWN = "unknown"


# Types that say something about a specific card (as opposed to applications,
# marketing or noise).
CARD_EVIDENCE_TYPES = {
    EvidenceType.STATEMENT,
    EvidenceType.DISPATCH_ACTIVATION,
    EvidenceType.WELCOME,
    EvidenceType.APPROVAL,
    EvidenceType.ANNUAL_FEE,
    EvidenceType.RENEWAL,
    EvidenceType.POINTS,
    EvidenceType.INACTIVITY_NOTICE,
    EvidenceType.TRANSACTION_ALERT,
    EvidenceType.PAYMENT,
    EvidenceType.UPGRADE,
    EvidenceType.REPLACEMENT,
    EvidenceType.CLOSURE,
    EvidenceType.OFFER_EXISTING_CARD,
}

APPLICATION_TYPES = {EvidenceType.APPLICATION_RECEIVED, EvidenceType.APPLICATION_REJECTED}

USAGE_TYPES = {EvidenceType.TRANSACTION_ALERT, EvidenceType.STATEMENT, EvidenceType.PAYMENT}


@dataclass
class MessageMeta:
    """What we know about one email. Bodies are held in memory only, never stored."""

    message_id: str
    date: datetime
    sender: str  # full From header
    subject: str
    snippet: str
    to: str = ""
    auth_results: str = ""
    list_unsubscribe: bool = False
    labels: tuple[str, ...] = ()
    body: str | None = None  # plain text, filled only when we fetch the body
    attachment_names: tuple[str, ...] = ()

    def text(self) -> str:
        parts = [self.subject, self.snippet]
        if self.body:
            parts.append(self.body)
        parts.extend(self.attachment_names)
        return "\n".join(p for p in parts if p)


@dataclass
class Evidence:
    """Derived facts from one email. Safe to persist: contains no email text."""

    message_id: str
    date: datetime
    bank_id: str | None
    type: EvidenceType
    last4s: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)  # canonical names, in order of appearance
    product_raw: str | None = None
    dkim_pass: bool = False
    forwarded: bool = False
    to_user: bool = True
    name_match: bool | None = None  # None = no name found in the email
    addon: bool = False
    used_body: bool = False
    test: bool = False  # came from a --test-sender address

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "date": self.date.isoformat(),
            "bank_id": self.bank_id,
            "type": self.type.value,
            "last4s": self.last4s,
            "products": self.products,
            "product_raw": self.product_raw,
            "dkim_pass": self.dkim_pass,
            "forwarded": self.forwarded,
            "to_user": self.to_user,
            "name_match": self.name_match,
            "addon": self.addon,
            "used_body": self.used_body,
            "test": self.test,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Evidence":
        return cls(
            message_id=d["message_id"],
            date=datetime.fromisoformat(d["date"]),
            bank_id=d["bank_id"],
            type=EvidenceType(d["type"]),
            last4s=list(d.get("last4s", [])),
            products=list(d.get("products", [])),
            product_raw=d.get("product_raw"),
            dkim_pass=d.get("dkim_pass", False),
            forwarded=d.get("forwarded", False),
            to_user=d.get("to_user", True),
            name_match=d.get("name_match"),
            addon=d.get("addon", False),
            used_body=d.get("used_body", False),
            test=d.get("test", False),
        )


class Status(str, Enum):
    ACTIVE = "active"
    DORMANT = "dormant"
    POSSIBLY_CLOSED = "possibly_closed"
    CLOSED = "closed"
    REPLACED = "replaced"
    APPROVED_NOT_SEEN = "approved_not_seen"
    UNKNOWN = "unknown"


@dataclass
class Card:
    bank_id: str
    last4: str | None
    product: str | None
    evidence: list[Evidence] = field(default_factory=list)
    product_history: list[tuple[datetime, str]] = field(default_factory=list)
    replaced_by: "Card | None" = None
    flags: set[str] = field(default_factory=set)
    ownership: float = 0.0
    status: Status = Status.UNKNOWN
    status_confidence: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.bank_id}:{self.last4 or 'unknown'}:{self.product or 'unknown'}"

    @property
    def first_seen(self) -> datetime | None:
        return min((e.date for e in self.evidence), default=None)

    @property
    def last_seen(self) -> datetime | None:
        return max((e.date for e in self.evidence), default=None)


@dataclass
class Application:
    bank_id: str
    product: str | None
    status: str  # in_progress | rejected
    first_seen: datetime
    last_seen: datetime
