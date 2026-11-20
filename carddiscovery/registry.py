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
    # Family base -> its variants, e.g. "Regalia" -> ["Regalia Gold", "Regalia First"]
    families: dict[str, list[str]] = field(default_factory=dict)
    # Family base -> (loose regex, variant), longest first. See variants_in().
    variant_patterns: dict[str, list[tuple[re.Pattern, str]]] = field(default_factory=dict)


# An alias only counts when "card" sits within a few words of it.
_NEAR_CARD = r"(?:\W+\w+){0,3}?\W+(?:credit\s+)?card\b"
_CARD_BEFORE = r"\bcard(?:\W+\w+){0,2}?\W+"


def _alias_pattern(alias: str) -> re.Pattern:
    a = re.escape(alias).replace(r"\ ", r"\s+")
    # \b does not work after symbols such as "+", so use lookarounds instead.
    core = rf"(?<![\w]){a}(?![\w])"
    return re.compile(rf"(?:{core}{_NEAR_CARD}|{_CARD_BEFORE}{core})", re.IGNORECASE)


# Only banks licensed by the RBI can register names under .bank.in, so a sender there whose
# name starts with a bank's key is that bank, whichever variant it uses: hsbc.bank.in,
# mail.hsbc.bank.in, icici.bank.in next to icicibank.bank.in, hdfc.bank.in next to
# hdfcbank.bank.in. Every bank domain a real inbox showed missing was one of these. Other
# endings (.com, .in, .co) aren't wildcarded: anyone can register "hdfc-alerts.com".
BANK_IN = ".bank.in"


def _bank_in_keys(banks: dict[str, Bank]) -> list[tuple[str, str]]:
    """Each bank's keys: its own .bank.in names with a trailing "bank" dropped, shortest
    form kept ("hdfcbank", "hdfc" -> "hdfc"; "sbicard" stays, so State Bank of India's
    sbi.bank.in isn't taken for SBI Card). Refuses keys where one starts with another bank's,
    since a name could then mean either bank."""
    keys: dict[str, str] = {}
    for b in banks.values():
        mine = set()
        for d in b.domains:
            if d.lower().endswith(BANK_IN):
                label = d.lower()[:-len(BANK_IN)].rsplit(".", 1)[-1]
                mine.add(label[:-4] if label.endswith("bank") and len(label) > 4 else label)
        for k in mine:
            if not any(o != k and k.startswith(o) for o in mine):
                keys[k] = b.id
    for k1, b1 in keys.items():
        for k2, b2 in keys.items():
            if b1 != b2 and k1.startswith(k2):
                raise ValueError(f".bank.in keys clash: {k1!r} ({b1}) starts with {k2!r} ({b2})")
    return sorted(keys.items(), key=lambda x: -len(x[0]))


def _spaced(text: str) -> str:
    """Underscores read as spaces ("YES_BANK_ACE Rupay Credit Card"), keeping every
    position the same so spans still line up with the original text."""
    return text.replace("_", " ")


def _loose_pattern(alias: str) -> re.Pattern:
    """`alias` with its words joined by up to 4 spaces, hyphens or brackets, and no
    need for "card" nearby. Only used to tell variants of a product already found."""
    tokens = re.findall(r"\w+|[^\w\s]", alias)
    body = r"[\s\-–—()]{0,4}".join(re.escape(t) for t in tokens)
    return re.compile(rf"(?<![\w]){body}(?![\w])", re.IGNORECASE)


def _extends(variant: str, base: str) -> bool:
    """"Regalia Gold" extends "Regalia"; "MoneyBack+" extends "MoneyBack"; "Platinumx" doesn't."""
    v, b = variant.lower(), base.lower()
    return len(v) > len(b) and v.startswith(b) and not v[len(b)].isalnum()


def _families(products: dict[str, list[str]], explicit: dict[str, list[str]],
              bank_id: str) -> dict[str, list[str]]:
    """Products whose name extends another product's name are that product's variants.
    banks.toml can override a family ("Base" = [variants]) or switch it off ("Base" = [])."""
    families = {base: [p for p in products if _extends(p, base)] for base in products}
    for base, variants in explicit.items():
        unknown = [n for n in (base, *variants) if n not in products]
        if unknown:
            raise ValueError(f"banks.{bank_id}.families names unknown products: {unknown}")
        families[base] = list(variants)
    return {b: v for b, v in families.items() if v}


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
    def __init__(self, banks: dict[str, Bank], test_senders: list[str] | None = None,
                 apps: dict[str, dict] | None = None):
        # Bill-payment apps (CRED, ...): {app id: {"name", "domains"}}. Their mail names the
        # bank in its text; see classify.analyze.
        self.apps = {a: {"name": c["name"], "domains": [d.lower() for d in c["domains"]]}
                     for a, c in (apps or {}).items()}
        self.banks = banks
        self._domain_index: list[tuple[str, str]] = sorted(
            ((d.lower(), b.id) for b in banks.values() for d in b.domains),
            key=lambda x: -len(x[0]),
        )
        self._bank_in_keys = _bank_in_keys(banks)
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
        return cls.from_banks(data["banks"], catalog_csv=catalog_csv, test_senders=test_senders,
                              apps_cfg=data.get("apps"))

    @classmethod
    def from_banks(cls, banks_cfg: dict, catalog_csv: Path | None = None,
                   test_senders: list[str] | None = None, apps_cfg: dict | None = None) -> "Registry":
        """Build from the "banks" (and "apps") sections of a rules bundle (same shape as banks.toml)."""
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
            families = _families(products, banks_cfg[bank_id].get("families", {}), bank_id)
            banks[bank_id].families = families
            banks[bank_id].variant_patterns = {
                base: sorted(((_loose_pattern(a), v) for v in variants for a in {v, *products[v]}),
                             key=lambda x: -len(x[0].pattern))
                for base, variants in families.items()}
        return cls(banks, test_senders, apps_cfg)

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
        return self.bank_for_domain(domain) if domain else None

    def app_for_sender(self, from_header: str) -> str | None:
        """The bill-payment app (CRED, ...) that sent this, by sender domain or a subdomain of it."""
        domain = sender_domain(from_header)
        if not domain:
            return None
        for app_id, app in self.apps.items():
            if any(domain == d or domain.endswith("." + d) for d in app["domains"]):
                return app_id
        return None

    def bank_for_domain(self, domain: str) -> str | None:
        """The bank a sender domain belongs to: a listed domain or any subdomain of it, or any
        name under .bank.in that starts with the bank's key (see _bank_in_keys)."""
        domain = domain.lower().strip(".")
        for d, bank_id in self._domain_index:
            if domain == d or domain.endswith("." + d):
                return bank_id
        if domain.endswith(BANK_IN):
            label = domain[:-len(BANK_IN)].rsplit(".", 1)[-1]
            for key, bank_id in self._bank_in_keys:  # longest key first
                if label.startswith(key):
                    return bank_id
        return None

    def all_domains(self) -> list[str]:
        """Everything to search `from:` for: bank domains, bill-payment apps, any test senders."""
        return ([d for b in self.banks.values() for d in b.domains]
                + [d for a in self.apps.values() for d in a["domains"]] + self.test_senders)

    def product_spans(self, bank_id: str, text: str, product: str) -> list[int]:
        text = _spaced(text)
        bank = self.banks.get(bank_id)
        if not bank:
            return []
        return [m.start() for pat, p in bank.product_patterns if p == product for m in pat.finditer(text)]

    def products_in(self, bank_id: str, text: str) -> list[str]:
        """Canonical products mentioned in text, ordered by first position."""
        text = _spaced(text)
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

    def families(self, bank_id: str) -> dict[str, list[str]]:
        bank = self.banks.get(bank_id)
        return bank.families if bank else {}

    def variants_in(self, bank_id: str, text: str, base: str) -> list[tuple[int, str]]:
        """(position, variant) for each variant of `base` written in text, loosely:
        no "card" needed nearby, and words may be joined by spaces, hyphens or
        brackets ("Regalia Gold", "REGALIA-GOLD", "Regalia (Gold)"). Longest first,
        so "Emeralde Private Metal" isn't also counted as "Emeralde Private"."""
        text = _spaced(text)
        bank = self.banks.get(bank_id)
        if not bank:
            return []
        found: list[tuple[int, str]] = []
        taken: list[tuple[int, int]] = []
        for pattern, variant in bank.variant_patterns.get(base, []):
            for m in pattern.finditer(text):
                span = m.span()
                if any(s < span[1] and span[0] < e for s, e in taken):
                    continue
                taken.append(span)
                found.append((span[0], variant))
        return sorted(found)


_EXTRA_MENTIONS = {"amex": ["Amex"], "citi": ["Citibank", "Citi"], "sbi": ["SBI"], "au": ["AU Bank"],
                   "bobcard": ["Bank of Baroda", "BOB Card"], "kotak": ["Kotak"], "idfc": ["IDFC"]}
# Short forms that are ordinary words or another bank's product: "Federal", "Indian" (Indian Oil),
# "DCB" (HDFC's Diners Club Black)
_NO_SHORT_FORM = {"federal", "indianbank", "dcb"}


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
