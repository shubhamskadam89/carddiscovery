"""The learned evidence model, run in SHADOW mode: it scores every bank email and every card next
to the rule engine, for comparison in the dev panel. It never changes which group a card is in.

Three stages, kept apart on purpose:
  1. Identity (which card) stays with the rules: extract.last4s and its guards, products, filenames.
  2. Email evidence: two L2 logistic models over generic concept features (data/evidence_model.json,
     trained by `python -m cdserver corpus export`):
       p_rel  is the email about a credit card?
       p_own  does it show the person holds the card? (calibrated on the mail the engine reads)
  3. Card level: the card's independent event families (statement, transaction, payment, dispatch,
     PIN ...) combined, each family counted once however many emails it has, then a decision from
     product costs: wallet, ask, wait (applications only) or hidden.

Features are concepts tagged by where they appear (subject S, preview P, body B, footer F, Gmail
tab), never a bank, product, sender or person's name, so they mean the same at a bank the model
never saw.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from functools import lru_cache
from pathlib import Path

from .models import EvidenceType as T

MODEL_FILE = Path(__file__).parent / "data" / "evidence_model.json"


def _rx(p: str) -> re.Pattern:
    return re.compile(p, re.I)


# ---------------------------------------------------------------- concept features

CONCEPTS = {
    # name: (where, pattern)   where: S subject, P preview, SP either, B body (no footer), F footer
    "card_word_S": ("S", _rx(r"\bcard\b")),
    "credit_card_S": ("S", _rx(r"\bcredit\s*card\b")),
    "statement_S": ("S", _rx(r"\be?-?statement\b")),
    "billing_SP": ("SP", _rx(r"amount\s+due|minimum\s+(?:amount\s+)?due|total\s+(?:amount\s+)?due|due\s+date|"
                            r"bill\s+(?:is\s+)?(?:ready|due|generated)")),
    "spend_SP": ("SP", _rx(r"\bspent\b|debited|swiped|transaction\s+alert|used\s+(?:for|at)|purchase|\btxn\b")),
    "payment_SP": ("SP", _rx(r"payment\s+(?:received|confirmation|successful)|thank\s+you\s+for\s+(?:your\s+|the\s+)?"
                            r"payment|refund|credited\s+(?:to|into)\s+your\s+card")),
    "lifecycle_SP": ("SP", _rx(r"dispatch|on\s+its\s+way|deliver|activat|\bpin\b|renew|blocked|suspend|"
                              r"card\s+generation|replac")),
    "welcome_SP": ("SP", _rx(r"\bwelcome\b")),
    "reward_SP": ("SP", _rx(r"reward\s+points?|\bpoints\b")),
    "otp_S": ("S", _rx(r"\botp\b|one\s+time\s+password")),
    "application_SP": ("SP", _rx(r"application|applied|applicant")),
    "emi_SP": ("SP", _rx(r"\bemi\b|emis\b")),
    "offer_SP": ("SP", _rx(r"\boffer|cashback|discount|voucher|%\s*off|\bfree\b|\bdeal|upgrade\s+to|\b2nd\b|"
                          r"second\s+card|invitation|limited\s+period|\bwin\b|exclusive|festive|lifetime\s+free|"
                          r"\bperks\b|apply\s+(?:now|today)")),
    "loan_SP": ("SP", _rx(r"\bloan\b|pre-?approved|disbursed")),
    "account_SP": ("SP", _rx(r"\ba/?c\b|account\s+no|savings|debit\s+card|deposit|\bupi\b|\bnach\b|investment|"
                            r"insurance|mutual\s+fund|demat|\bkyc\b|net\s*banking|mobile\s*banking|fastag|nps\b")),
    "notice_SP": ("SP", _rx(r"fraud|scam|phishing|vishing|maintenance|downtime|login|safety|beware|protect|"
                           r"security|website")),
    "important_update_S": ("S", _rx(r"important\s+(?:update|communication|notice|information)|changes?\s+(?:in|to|on)")),
    "dear_cardholder_PB": ("PB", _rx(r"dear\s+(?:card\s*member|cardholder)")),
    "dear_applicant_PB": ("PB", _rx(r"dear\s+applicant")),
    "mitc_FB": ("FB", _rx(r"most\s+important\s+terms|\bmitc\b")),
    "spend_B_only": ("B", _rx(r"\bspent\b|debited|transaction|purchase")),
    "reward_B_only": ("B", _rx(r"reward\s+points?")),
    "statement_B_only": ("B", _rx(r"\bstatement\b")),
}
SENDERS = {
    "sender_alerts": _rx(r"alert|instaalert|txn|transaction"),
    "sender_statements": _rx(r"statement|estatement"),
    "sender_cards": _rx(r"card|\bcc\b|^cc|cc\."),
    "sender_marketing": _rx(r"offer|market|mailer|emailer|cmp|promo|news|campaign|info|alwaysyou|engage|custcom"),
    "sender_service": _rx(r"service|care|support|noreply|no-reply|donotreply|customer"),
}
_PDF_BILLING = _rx(r"minimum\s+amount\s+due|available\s+credit\s+limit|total\s+amount\s+due|payment\s+due\s+date")
X_NUMBER = re.compile(r"\bcard\s*(?:no\.?\s*)?[xX*]+\s?(\d{4})\b", re.I)    # "Card x6113" (one x)

# Left out of the production model: sender names encode one bank's habits, attachment and
# unsubscribe flags reflect how the study set was sampled, PDF text isn't read when an email is
# scored, and the rest had weights that flipped sign between banks.
EXCLUDED = frozenset({"sender_alerts", "sender_statements", "sender_cards", "sender_marketing", "sender_service",
                      "has_pdf", "gmail_unsubscribe", "pdf_billing", "product_S", "product_B_only",
                      "masked_number_SP", "reward_B_only", "spend_SP", "loan_SP", "emi_SP", "important_update_S",
                      "reward_SP"})


def _sender_local(sender: str) -> str:
    return ((re.search(r"[\w.+-]+@", sender or "") or [""])[0]).lower()


def concept_features(m, reg, body: str | None = None, footer: str | None = None) -> set[str]:
    """Concepts present in one email. m needs subject, snippet, body, sender, labels, unsubscribe,
    attachments, pdf_text. body/footer: the email already split (default: the curated boilerplate)."""
    from . import classify as cl, extract as ex
    if body is None or footer is None:
        units = cl._units(m.body or "")
        body = " ".join(u for u in units if not cl.BOILERPLATE.search(u))
        footer = " ".join(u for u in units if cl.BOILERPLATE.search(u))
    where = {"S": m.subject or "", "P": m.snippet or "", "B": body, "F": footer}
    f = set()
    for name, (pos, rx) in CONCEPTS.items():
        if name.endswith("_B_only"):
            if rx.search(body) and not rx.search(where["S"] + " " + where["P"]):
                f.add(name)
        elif any(rx.search(where[p]) for p in pos):
            f.add(name)
    text_sp = f"{where['S']}\n{where['P']}"
    head_body = f"{where['S']}\n{where['P']}\n{body}"
    bank = reg.bank_for_sender(m.sender)
    # Card-shaped numbers as the engine keeps them (its guards drop accounts, payees, CRNs ...).
    nums = set(ex.last4s(head_body)) | set(X_NUMBER.findall(head_body))
    if bank and bank not in cl.CREDIT_ONLY_BANKS:
        nums -= ex.account_last4s(head_body)
    if nums:
        f.add("masked_number")
        if (set(ex.last4s(text_sp)) | set(X_NUMBER.findall(text_sp))) & nums:
            f.add("masked_number_SP")
    elif ex.partial_last_digits(head_body):
        f.add("partial_ending")
    if ex.held_phrases(head_body):
        f.add("your_card_ending_phrase")
    if bank and reg.products_in(bank, where["S"]):
        f.add("product_S")
    elif bank and reg.products_in(bank, body):
        f.add("product_B_only")
    local = _sender_local(m.sender)
    for name, rx in SENDERS.items():
        if rx.search(local):
            f.add(name)
    if "CATEGORY_PROMOTIONS" in (m.labels or ()):
        f.add("gmail_promotions")
    if m.unsubscribe:
        f.add("gmail_unsubscribe")
    if any(a.lower().endswith(".pdf") for a in m.attachments or ()):
        f.add("has_pdf")
    if getattr(m, "pdf_text", "") and _PDF_BILLING.search(m.pdf_text):
        f.add("pdf_billing")
    if reg.app_for_sender(m.sender):
        f.add("from_app")
    return f


# Products by context, sentence by sentence: held ("your X card ending"), applied for, offered,
# in a notice, or plain. An offer of a bank's product says nothing about holding it.
_P_HELD = _rx(r"\byour\b[^.\n]{0,50}$")
_P_APPLY = _rx(r"application|applied|applicant")
_P_OFFER = _rx(r"\boffer|apply|upgrade|introducing|explore|get\s+(?:the|your|an?)\b|invite|invitation|lifetime\s+free|"
               r"benefit|\bfree\b|eligible|pre-?approved|\bwin\b|cashback|voucher|discount|why\s+not|launch")
_P_NOTICE = _rx(r"fraud|safety|security|protect|maintenance|change|revision|terms|charges|fee")


def product_contexts(m, reg, bank, parts: dict[str, str]) -> list[tuple[str, str, str]]:
    """(product, where, context) for each product of the bank named in subject, preview or body."""
    from . import classify as cl
    if not bank:
        return []
    out = []
    for where in ("subject", "preview", "body"):
        for unit in cl._units(parts.get(where) or ""):
            for prod in reg.products_in(bank, unit):
                i = unit.lower().find(prod.lower().split()[0])
                before = unit[:max(0, i)]
                ctx = ("held" if _P_HELD.search(before) and not _P_OFFER.search(before) else
                       "application" if _P_APPLY.search(unit) else
                       "offer" if _P_OFFER.search(unit) or "CATEGORY_PROMOTIONS" in (m.labels or ()) else
                       "notice" if _P_NOTICE.search(unit) else "plain")
                out.append((prod, where, ctx))
    return out


def features(m, reg, bank: str | None, body: str | None = None, footer: str | None = None) -> set[str]:
    """What the model sees: the concepts minus EXCLUDED, plus products by context."""
    from . import classify as cl
    if body is None or footer is None:
        units = cl._units(m.body or "")
        body = " ".join(u for u in units if not cl.BOILERPLATE.search(u))
        footer = " ".join(u for u in units if cl.BOILERPLATE.search(u))
    x = concept_features(m, reg, body, footer) - EXCLUDED
    for _, where, ctx in product_contexts(m, reg, bank, {"subject": m.subject, "preview": m.snippet, "body": body}):
        x.add(f"product_{ctx}" + ("_S" if where == "subject" else ""))
    return x


class _View:
    """A MessageMeta seen with the attribute names the feature code uses."""

    def __init__(self, msg):
        self.subject, self.snippet, self.body = msg.subject or "", msg.snippet or "", msg.body or ""
        self.sender, self.labels = msg.sender or "", tuple(msg.labels or ())
        self.unsubscribe = bool(msg.list_unsubscribe)
        self.attachments = tuple(msg.attachment_names or ())
        self.pdf_text = ""


# ---------------------------------------------------------------- the model file

@lru_cache(maxsize=1)
def load(path: str | None = None) -> dict | None:
    p = Path(path) if path else MODEL_FILE
    return json.loads(p.read_text()) if p.exists() else None


def sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def linear(part: dict, x: set[str]) -> float:
    return part["intercept"] + sum(part["weights"].get(f, 0.0) for f in x)


def calibrate(cal: dict | None, p: float) -> float:
    """Platt scaling fitted on held-out scores of the mail the engine reads."""
    if not cal:
        return p
    z = math.log(max(1e-6, p) / max(1e-6, 1 - p))
    return sigmoid(cal["a"] * z + cal["b"])


# ---------------------------------------------------------------- event families

_LIFE = [("dispatch", _rx(r"dispatch|on\s+its\s+way|deliver|courier|shipped")), ("pin", _rx(r"\bpin\b")),
         ("activation", _rx(r"activat")), ("block_replace", _rx(r"block|hotlist|replac|reissu"))]
EVENT_OF_TYPE = {
    T.STATEMENT: "statement", T.TRANSACTION_ALERT: "transaction", T.PAYMENT: "payment",
    T.WELCOME: "welcome", T.RENEWAL: "renewal", T.REPLACEMENT: "block_replace", T.ANNUAL_FEE: "annual_fee",
    T.CLOSURE: "closure", T.INACTIVITY_NOTICE: "inactivity", T.UPGRADE: "upgrade",
    T.APPROVAL: "application", T.APPLICATION_RECEIVED: "application", T.APPLICATION_REJECTED: "application",
    T.POINTS: "weak", T.OFFER_EXISTING_CARD: "weak", T.OTP: "weak", T.UNKNOWN: "weak",
}
# Families that are separate things happening to a held card. Offers, points, OTPs and unknown
# mail share one "weak" family: however many there are, they count as one.
STRONG = frozenset({"statement", "transaction", "payment", "welcome", "renewal", "block_replace", "annual_fee",
                    "closure", "inactivity", "upgrade", "dispatch", "pin", "activation", "lifecycle"})


def event_of(etype: T, subject: str = "") -> str:
    if etype == T.DISPATCH_ACTIVATION:
        return next((name for name, rx in _LIFE if rx.search(subject or "")), "lifecycle")
    return EVENT_OF_TYPE.get(etype, "other")


def template_id(sender: str, subject: str) -> str:
    """Sender + subject with numbers and months blanked, hashed: copies of one mail share it."""
    addr = (re.search(r"[\w.+-]+@[\w.-]+", sender or "") or [""])[0].lower()
    subj = re.sub(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", "<m>",
                  re.sub(r"\d+", "#", (subject or "").lower()), flags=re.I)
    return hashlib.sha1(f"{addr}|{' '.join(subj.split())}".encode()).hexdigest()[:12]


# ---------------------------------------------------------------- scoring one email

def shadow(msg, ev, reg, model: dict | None = None) -> dict | None:
    """The model's view of one email, stored with its evidence (numbers only, no text)."""
    model = model or load()
    if not model or not ev.bank_id:
        return None
    x = features(_View(msg), reg, ev.bank_id)
    p_rel = sigmoid(linear(model["relevance"], x))
    p_own = calibrate(model["ownership"].get("calibration"), sigmoid(linear(model["ownership"], x)))
    return {"v": model["version"], "p_rel": round(p_rel, 3), "p_own": round(p_own, 3),
            "event": event_of(ev.type, msg.subject or ""), "tpl": template_id(msg.sender, msg.subject)}


# ---------------------------------------------------------------- card level

def card_confidence(shadows: list[dict]) -> tuple[float, dict[str, float]]:
    """Combine a card's emails: the best p_own per event family (copies and one campaign count
    once), then families together as independent events. Returns (confidence, best per family)."""
    best: dict[str, float] = {}
    for s in shadows:
        fam = s["event"] if s["event"] in STRONG else ("application" if s["event"] == "application" else "weak")
        best[fam] = max(best.get(fam, 0.0), s["p_own"])
    keep = [p for f, p in best.items() if f != "application"]
    miss = 1.0
    for p in keep:
        miss *= 1 - p
    return (1 - miss if keep else 0.0), best


def thresholds(costs: dict) -> dict:
    """From product costs. wrong_wallet: a card that isn't theirs shown as theirs; missed: a real
    card left out; ask: one question. With a calibrated confidence c, the cheapest action is
    wallet when (1-c)·wrong ≤ min(ask, c·missed), ask when ask ≤ c·missed, else hidden."""
    w, m, a = costs["wrong_wallet"], costs["missed"], costs["ask"]
    wallet = max(1 - a / w, w / (w + m))
    return {"wallet": round(wallet, 3), "ask": round(min(a / m, wallet), 3)}


def decide(shadows: list[dict], model: dict | None = None) -> dict | None:
    """Shadow decision for one card: wallet / ask / wait / hidden, with why."""
    model = model or load()
    scored = [s for s in shadows if s]
    if not model or not scored:
        return None
    conf, best = card_confidence(scored)
    t = thresholds(model["costs"])
    strong = any(f in STRONG for f in best)
    if all(s["event"] == "application" for s in scored):
        d = "wait"  # applied or approved: nothing yet shows the card arrived
    elif conf >= t["wallet"] and strong:
        # Only something that happened to the card (statement, transaction, payment, dispatch,
        # PIN ...) puts it in the wallet. Offers, OTPs and unknown mail can show a number, and a
        # number alone scores ~1.0 (HDFC "Primary Credit Card xxxx7091" in one card-control mail).
        d = "wallet"
    elif conf >= t["ask"]:
        d = "ask"
    else:
        d = "hidden"
    return {"decision": d, "confidence": round(conf, 3), "events": {k: round(v, 2) for k, v in sorted(best.items())},
            "scored": len(scored), "thresholds": t, "model": model["version"]}
