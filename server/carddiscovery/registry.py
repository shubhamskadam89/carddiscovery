"""Bank sender registry and product alias catalogue."""

from __future__ import annotations

import csv
import re
import tomllib
from dataclasses import dataclass, field
from email.utils import parseaddr
from importlib import resources
from pathlib import Path


@dataclass
class Bank:
    id: str
    name: str
    domains: list[str]
    # (alias regex, canonical product), longest alias first
    product_patterns: list[tuple[re.Pattern, str]] = field(default_factory=list)


# An alias only counts when "card" sits within a few words of it.
_NEAR_CARD = r"(?:\W+\w+){0,3}?\W+(?:credit\s+)?card\b"
_CARD_BEFORE = r"\bcard(?:\W+\w+){0,2}?\W+"


def _alias_pattern(alias: str) -> re.Pattern:
    a = re.escape(alias).replace(r"\ ", r"\s+")
    # \b does not work after symbols such as "+", so use lookarounds instead.
    core = rf"(?<![\w]){a}(?![\w])"
    return re.compile(rf"(?:{core}{_NEAR_CARD}|{_CARD_BEFORE}{core})", re.IGNORECASE)


TEST_BANK_ID = "testbank"


def dev_mode() -> bool:
    """Test senders are a development tool: they make mail from a personal address
    count as bank mail. They only work with CARDDISCOVERY_DEV=1 set, so a normal
    run (or a shipped build) can never be fed fake bank mail this way."""
    import os
    return os.environ.get("CARDDISCOVERY_DEV") == "1"


class TestModeLocked(RuntimeError):
    pass


class Registry:
    def __init__(self, banks: dict[str, Bank], test_senders: list[str] | None = None):
        self.banks = banks
        self._domain_index: list[tuple[str, str]] = sorted(
            ((d.lower(), b.id) for b in banks.values() for d in b.domains),
            key=lambda x: -len(x[0]),
        )
        # Test mode: personal addresses that stand in for banks. The bank is then
        # read from the email text ("HDFC Bank ...") instead of the sender domain.
        if test_senders and not dev_mode():
            raise TestModeLocked("--test-sender needs CARDDISCOVERY_DEV=1 (development only)")
        self.test_senders = [s.lower().strip() for s in (test_senders or [])]
        if self.test_senders and TEST_BANK_ID not in banks:
            banks[TEST_BANK_ID] = Bank(id=TEST_BANK_ID, name="Test Bank (bank not named)", domains=[])
        self._mention_patterns = _mention_patterns(banks)

    # ---- loading -------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None, catalog_csv: Path | None = None,
             test_senders: list[str] | None = None) -> "Registry":
        if path is None:
            raw = resources.files("carddiscovery.data").joinpath("banks.toml").read_bytes()
        else:
            raw = Path(path).read_bytes()
        data = tomllib.loads(raw.decode())
        return cls.from_banks(data["banks"], catalog_csv=catalog_csv, test_senders=test_senders)

    @classmethod
    def from_banks(cls, banks_cfg: dict, catalog_csv: Path | None = None,
                   test_senders: list[str] | None = None) -> "Registry":
        """Build from the "banks" section of a rules bundle (same shape as banks.toml)."""
        aliases: dict[str, dict[str, list[str]]] = {}
        banks: dict[str, Bank] = {}
        for bank_id, cfg in banks_cfg.items():
            banks[bank_id] = Bank(id=bank_id, name=cfg["name"], domains=list(cfg["domains"]))
            aliases[bank_id] = {p: list(a) for p, a in cfg.get("products", {}).items()}
        if catalog_csv:
            _merge_catalog(aliases, catalog_csv)
        for bank_id, products in aliases.items():
            if bank_id not in banks:
                continue
            pairs = []
            for product, extra in products.items():
                for alias in {product, *extra}:
                    pairs.append((alias, product))
            pairs.sort(key=lambda x: -len(x[0]))
            banks[bank_id].product_patterns = [(_alias_pattern(a), p) for a, p in pairs]
        return cls(banks, test_senders)

    # ---- test mode -----------------------------------------------------
    def is_test_sender(self, from_header: str) -> bool:
        if not self.test_senders:
            return False
        _, addr = parseaddr(from_header)
        return addr.lower() in self.test_senders

    def bank_from_text(self, text: str) -> str | None:
        """The bank named earliest in the text, e.g. 'HDFC Bank' or 'SBI Card'."""
        best: tuple[int, str] | None = None
        for pattern, bank_id in self._mention_patterns:
            m = pattern.search(text)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), bank_id)
        return best[1] if best else None

    # ---- lookups -------------------------------------------------------
    def bank_for_sender(self, from_header: str) -> str | None:
        domain = sender_domain(from_header)
        if not domain:
            return None
        for d, bank_id in self._domain_index:
            if domain == d or domain.endswith("." + d):
                return bank_id
        return None

    def bank_for_domain(self, domain: str) -> str | None:
        domain = domain.lower().strip(".")
        for d, bank_id in self._domain_index:
            if domain == d or domain.endswith("." + d):
                return bank_id
        return None

    def all_domains(self) -> list[str]:
        """Everything to search `from:` for: bank domains plus any test senders."""
        return [d for b in self.banks.values() for d in b.domains] + self.test_senders

    def product_spans(self, bank_id: str, text: str, product: str) -> list[int]:
        bank = self.banks.get(bank_id)
        if not bank:
            return []
        return [m.start() for pat, p in bank.product_patterns if p == product for m in pat.finditer(text)]

    def products_in(self, bank_id: str, text: str) -> list[str]:
        """Canonical products mentioned in text, ordered by first position."""
        bank = self.banks.get(bank_id)
        if not bank:
            return []
        found: dict[str, int] = {}
        taken: list[tuple[int, int]] = []
        for pattern, product in bank.product_patterns:  # longest alias first
            for m in pattern.finditer(text):
                span = m.span()
                if any(s < span[1] and span[0] < e for s, e in taken):
                    continue  # overlaps a longer alias already matched
                taken.append(span)
                found.setdefault(product, span[0])
                found[product] = min(found[product], span[0])
        return [p for p, _ in sorted(found.items(), key=lambda x: x[1])]


_EXTRA_MENTIONS = {"amex": ["Amex"], "citi": ["Citibank", "Citi"], "sbi": ["SBI"], "au": ["AU Bank"],
                   "bobcard": ["Bank of Baroda", "BOB Card"], "kotak": ["Kotak"], "idfc": ["IDFC"]}
_NO_SHORT_FORM = {"federal"}  # "Federal" alone is an ordinary word


def _mention_patterns(banks: dict[str, Bank]) -> list[tuple[re.Pattern, str]]:
    """Patterns that spot a bank being named in text. Short all-caps forms such as
    'YES' or 'RBL' are matched case-sensitively so ordinary words don't match."""
    out = []
    for b in banks.values():
        if b.id == TEST_BANK_ID:
            continue
        forms = {b.name, *_EXTRA_MENTIONS.get(b.id, [])}
        short = re.sub(r"\s+(Bank|Card)$", "", b.name).split(" (")[0]
        if short != b.name and b.id not in _NO_SHORT_FORM:
            forms.add(short)
        for f in forms:
            flags = 0 if (f.isupper() or len(f) <= 4) else re.IGNORECASE
            out.append((re.compile(rf"(?<![\w]){re.escape(f)}(?![\w])", flags), b.id))
    return out


def sender_domain(from_header: str) -> str | None:
    _, addr = parseaddr(from_header)
    if "@" not in addr:
        return None
    return addr.rsplit("@", 1)[1].lower().strip(">").strip()


def _merge_catalog(aliases: dict[str, dict[str, list[str]]], path: Path) -> None:
    """Merge a catalogue CSV with columns: bank_id, product, aliases ("|"-separated)."""
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            bank_id = row["bank_id"].strip()
            product = row["product"].strip()
            extra = [a.strip() for a in (row.get("aliases") or "").split("|") if a.strip()]
            aliases.setdefault(bank_id, {}).setdefault(product, []).extend(extra)
