"""Development only: a study set of real emails, to learn which words and combinations make a
bank email evidence of a credit card (and which make it look like one when it isn't).

    CARDDISCOVERY_DEV=1 CD_CORPUS_ACCOUNTS=60 python -m cdserver corpus fetch --account 60
    ... corpus label --account 60 --truth truth.json     (five labels, with a reason each)
    ... corpus report --account 60                        (writes .local/analysis/<account>/report.md)
    ... corpus delete --account 60                        (removes every stored email at once)

This is the one place email text is kept, and only for accounts whose owner agreed (their own
mailbox, named in CD_CORPUS_ACCOUNTS). Each email is sealed with the vault key, one file per email
under .local/corpus/<account>/ (ignored by git), and the set carries a delete-by date (30 days):
past it, nothing is read or added until it's deleted or the date is renewed. Production tables
never hold this text.

What's fetched (one full read each, plus PDFs that can be opened):
  evidence   every email the engine already read for this account
  skipped    samples of bank mail the scan didn't read, by kind:
               card_subject   "card" in the subject
               card_body      "credit card" in the text but not the subject
               pdf            a PDF attached
               transaction    transaction words, no "card" in the subject
               random         anything from the bank, spread over the years
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config

KEEP_DAYS = 30
SAMPLE = {"card_subject": 150, "card_body": 150, "pdf": 100, "transaction": 100, "random": 150}


# ---------------------------------------------------------------- the store

def _allowed(account_id: int) -> None:
    if os.environ.get("CARDDISCOVERY_DEV") != "1":
        raise SystemExit("development only: set CARDDISCOVERY_DEV=1")
    allowed = {x.strip() for x in os.environ.get("CD_CORPUS_ACCOUNTS", "").split(",") if x.strip()}
    if str(account_id) not in allowed:
        raise SystemExit(f"account {account_id} isn't in CD_CORPUS_ACCOUNTS: only the owner's own mailbox, with "
                         "their agreement, may be stored")


@dataclass
class Mail:
    id: str
    strata: list[str]
    sender: str
    to: str
    subject: str
    date: str
    labels: list[str]
    auth: str
    unsubscribe: bool
    snippet: str
    body: str
    attachments: list[str]
    pdf_text: str = ""            # first PDF that could be opened, if any
    pdf_status: str = ""          # read / password / no_rule / unreadable / none


class Store:
    def __init__(self, account_id: int, root: Path | None = None, vault=None):
        _allowed(account_id)
        from .vault import Vault
        self.account = account_id
        self.dir = (root or config.HOME / "corpus") / str(account_id)
        self.vault = vault or Vault("worker")
        self.meta_file = self.dir / "meta.json"

    def meta(self) -> dict:
        return json.loads(self.meta_file.read_text()) if self.meta_file.exists() else {}

    def start(self, mailbox: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        m = self.meta()
        if not m:
            m = {"account": self.account, "mailbox": mailbox, "created": date.today().isoformat(),
                 "delete_by": (date.today() + timedelta(days=KEEP_DAYS)).isoformat()}
            self.meta_file.write_text(json.dumps(m, indent=1))
        self.check()

    def check(self) -> None:
        m = self.meta()
        if m and date.fromisoformat(m["delete_by"]) < date.today():
            raise SystemExit(f"this study set was due for deletion on {m['delete_by']}: run `corpus delete` "
                             "(or renew it deliberately in meta.json)")

    def _path(self, mid: str) -> Path:
        return self.dir / f"{re.sub(r'[^A-Za-z0-9]', '', mid)}.bin"

    def has(self, mid: str) -> bool:
        return self._path(mid).exists()

    def put(self, mail: Mail) -> None:
        blob = self.vault.seal(json.dumps(asdict(mail)).encode(), f"corpus:{self.account}:{mail.id}")
        p = self._path(mail.id)
        p.write_bytes(blob)
        os.chmod(p, 0o600)

    def add_stratum(self, mid: str, stratum: str) -> None:
        m = self.get(mid)
        if stratum not in m.strata:
            m.strata.append(stratum)
            self.put(m)

    def get(self, mid: str) -> Mail:
        return Mail(**json.loads(self.vault.open(self._path(mid).read_bytes(), f"corpus:{self.account}:{mid}")))

    def all(self):
        self.check()
        for p in sorted(self.dir.glob("*.bin")):
            yield self.get(p.stem)

    def delete(self) -> int:
        n = 0
        if self.dir.exists():
            for p in self.dir.iterdir():
                p.unlink()
                n += 1
            self.dir.rmdir()
        # The analysis folder holds subjects, previews and review sheets in plain text: it goes too.
        import shutil
        adir = config.HOME / "analysis" / str(self.account)
        if adir.exists():
            n += sum(1 for p in adir.rglob("*") if p.is_file())
            shutil.rmtree(adir)
        return n


# ---------------------------------------------------------------- fetching (read-only Gmail)

def fetch(account_id: int, log=print) -> dict:
    """Every evidence email of the account, then the skipped-mail samples, each read once in full
    (60 units) plus its first openable PDF. Resumable: what's stored is never read again."""
    from carddiscovery.queries import Query
    from carddiscovery.registry import Registry
    from cdserver import db, pdf as pdfmod
    from cdserver.gmailx import Meter, MeteredGmail
    from cdserver.jobs import _token_info
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from carddiscovery.providers.gmail import SCOPES

    c = db.connect()
    conn = c.execute("SELECT * FROM gmail_connections WHERE account_id = %s", (account_id,)).fetchone()
    if not conn or conn["status"] != "active":
        raise SystemExit(f"account {account_id} has no active Gmail connection")
    store = Store(account_id)
    store.start(conn["mailbox"])
    creds = Credentials.from_authorized_user_info(_token_info(conn["token_blob"]), SCOPES)
    creds.refresh(Request())
    g = MeteredGmail(creds, Meter(400_000))
    reg = Registry.load()
    name = (conn["names"] or [None])[0]
    dob = None
    if conn["dob_blob"]:
        dob = date.fromisoformat(store.vault.open(bytes(conn["dob_blob"]), f"dob:{account_id}").decode())

    evidence = [r["message_id"] for r in c.execute(
        "SELECT message_id FROM evidence WHERE account_id = %s AND message_id NOT LIKE '%%#pdf' "
        "ORDER BY data->>'date' DESC", (account_id,))]
    bank_l4 = {}
    for r in c.execute("SELECT data FROM evidence WHERE account_id = %s", (account_id,)):
        d = r["data"]
        if d.get("bank_id"):
            bank_l4.setdefault(d["bank_id"], set()).update(d.get("last4s") or [])
    plan: list[tuple[str, str]] = [(m, "evidence") for m in evidence]
    known = set(evidence)
    senders = tuple(sorted({d for b in reg.banks.values() for d in b.domains} | {"bank.in"}))
    groups = [senders[i:i + 25] for i in range(0, len(senders), 25)]
    years = conn["years"] or 3
    since = date.today() - timedelta(days=365 * years)
    searches = {
        "card_subject": dict(subject_any=("card",)),
        "card_body": dict(text_any=("credit card",), exclude_subject=("card",)),
        "pdf": dict(extra=("has:attachment", "filename:pdf")),
        "transaction": dict(text_any=("transaction", "debited", "spent", "txn"), exclude_subject=("card",)),
        "random": dict(exclude_subject=("card",)),
    }
    for stratum, kw in searches.items():
        picked: list[str] = []
        # Spread over the years: newest-first search pages would otherwise give only recent mail.
        spans = [(since + timedelta(days=365 * k), min(date.today() + timedelta(days=1),
                                                         since + timedelta(days=365 * (k + 1))))
                 for k in range(years)]
        per = max(1, SAMPLE[stratum] // (len(spans) * len(groups)))
        for after, before in spans:
            for grp in groups:
                q = Query(family="corpus", senders=grp, after=after, before=before, max_results=per * 4, **kw)
                ids = [i for i in g.search(q) if i not in known]
                picked += ids[:per]
                known.update(ids[:per])
        plan += [(m, stratum) for m in picked]
    log(f"to read: {len(plan)} ({len(evidence)} evidence, {len(plan) - len(evidence)} skipped-mail samples)")

    done = 0
    t0 = time.monotonic()
    for mid, stratum in plan:
        if store.has(mid):
            store.add_stratum(mid, stratum)
            continue
        meta, pdfs = g.full_with_pdfs(mid)
        mail = Mail(id=mid, strata=[stratum], sender=meta.sender or "", to=meta.to or "", subject=meta.subject or "",
                    date=meta.date.isoformat() if meta.date else "", labels=list(meta.labels or ()),
                    auth=meta.auth_results or "", unsubscribe=bool(meta.list_unsubscribe), snippet=meta.snippet or "",
                    body=meta.body or "", attachments=list(meta.attachment_names or ()), pdf_status="none")
        bank = reg.bank_for_sender(mail.sender)
        if pdfs and bank:
            rule = pdfmod.password_rule(meta.text())
            rules = [rule] if rule else pdfmod.PATTERNS.get(bank, [])
            l4s = sorted(bank_l4.get(bank, ()))
            if rules and not any(pdfmod.can_open(r, bool(name), bool(dob), l4s) for r in rules):
                mail.pdf_status = "no_rule"
            else:
                for fname, att in [p for p in pdfs if not re.search(r"terms|mitc|conditions", p[0], re.I)][:1]:
                    data = g.attachment(mid, att)
                    try:
                        text, _ = pdfmod.open_pdf_with(
                            data, [p for p, _ in pdfmod.keyed_candidates(bank, name, dob, l4s, rule)])
                        mail.pdf_text, mail.pdf_status = text[:60_000], "read"
                    except pdfmod.PasswordNeeded:
                        mail.pdf_status = "password"
                    except Exception:
                        mail.pdf_status = "unreadable"
                    finally:
                        data = None
        store.put(mail)
        done += 1
        if done % 50 == 0:
            log(f"  {done} read, {g.meter.units} units, {time.monotonic() - t0:.0f}s")
    return {"read": done, "planned": len(plan), "units": g.meter.units, "secs": round(time.monotonic() - t0)}


# ---------------------------------------------------------------- what the engine did, and why

def to_meta(m: Mail):
    from carddiscovery.models import MessageMeta
    return MessageMeta(message_id=m.id, date=datetime.fromisoformat(m.date) if m.date else datetime.now(timezone.utc),
                       sender=m.sender, subject=m.subject, snippet=m.snippet, to=m.to, auth_results=m.auth,
                       list_unsubscribe=m.unsubscribe, labels=tuple(m.labels), body=m.body,
                       attachment_names=tuple(m.attachments))


def _hit(rx, text: str) -> str | None:
    h = rx.search(text or "")
    return h.group(0)[:60] if h else None


def trace(m: Mail, reg, names: list[str]) -> dict:
    """The engine's decision for one email, step by step: which branch of the type decision
    fired and on which words, every card-number-shaped match and why it was kept or dropped,
    which products matched where, and the evidence it produced."""
    from carddiscovery import classify as cl, extract as ex
    from carddiscovery.classify import analyze

    msg = to_meta(m)
    ev = analyze(to_meta(m), reg, [], names)
    bank = reg.bank_for_sender(m.sender)
    app = None if bank else reg.app_for_sender(m.sender)
    text = msg.text()
    head = f"{m.subject}\n{m.snippet}"
    full = cl.core_text(text)
    path = []
    if bank not in cl.CREDIT_ONLY_BANKS and cl._DEBIT_SUBJECT.search(m.subject or "") \
            and not cl.CREDIT_WORDS.search(m.subject or ""):
        path.append(("debit_subject", _hit(cl._DEBIT_SUBJECT, m.subject)))
    elif cl.OTP.search(m.subject or ""):
        path.append(("otp_subject", _hit(cl.OTP, m.subject)))
    elif bank not in cl.CREDIT_ONLY_BANKS and cl.NON_CREDIT.search(head) \
            and not cl.CREDIT_WORDS.search(cl._without_offers(full)):
        path.append(("non_credit_words", _hit(cl.NON_CREDIT, head)))
    elif cl._APPROVED.search(full) and not cl._REJECTED.search(full):
        path.append(("approved", _hit(cl._APPROVED, full)))
    else:
        subject_offer = cl.OFFER.search(m.subject or "")
        sub_rule = None if subject_offer else next(((t.value, _hit(p, m.subject)) for t, p in cl.RULES
                                                    if p.search(m.subject or "")), None)
        if sub_rule:
            path.append(("subject_rule", sub_rule))
        elif cl.OFFER.search(head):
            path.append(("offer_words", _hit(cl.OFFER, head)))
        else:
            body_rule = next(((t.value, _hit(p, full)) for t, p in cl.RULES if p.search(full)), None)
            path.append(("body_rule", body_rule) if body_rule else ("fallback", ev.type.value))
    numbers = []
    for p in ex._LAST4_PATTERNS:
        for h in p.finditer(text):
            before = text[max(0, h.start() - 40):h.start()]
            why = ("account" if ex._ACCOUNT_BEFORE.search(before) else "phone" if ex._PHONE_BEFORE.search(before)
                   else "identifier" if ex._ID_BEFORE.search(before)
                   else "example" if ex._EXAMPLE_BEFORE.search(text[max(0, h.start() - 160):h.start()]) else "kept")
            numbers.append({"digits": h.group(1), "why": why, "before": " ".join(before.split())[-35:]})
    footer = [u for u in cl._units(text) if cl.BOILERPLATE.search(u)]
    return {"bank": bank, "app": app, "path": path, "type": ev.type.value, "last4s": ev.last4s,
            "last_digits": ev.last_digits, "product": ev.product, "products": ev.products, "held": ev.held,
            "numbers": numbers, "dkim": ev.dkim_pass, "forwarded": ev.forwarded, "footer_units": len(footer)}


# ---------------------------------------------------------------- ground truth: five labels

CONFIRMED, LIKELY, NOT, PROMO, AMBIG = "CONFIRMED_CARD", "LIKELY_CARD", "NOT_CARD", "PROMOTIONAL", "AMBIGUOUS"
_NOT_CARD = re.compile(
    r"\b(debit\s+card|savings?\s+(?:a/?c|account)|current\s+account|account\s+statement|statement\s+of\s+(?:your\s+)?"
    r"account|e-?account\s+statement|fixed\s+deposit|recurring\s+deposit|loan\s+account|home\s+loan|"
    r"outward\s+remittance|remittance|mutual\s+fund|demat|insurance\s+policy|policy\s+number|account\s+balance|"
    r"available\s+balance|neft|imps|rtgs|money\s+quotient|nominee)\b", re.I)
_CARDISH = re.compile(r"\bcredit\s*card\b|\bcard\s+(?:no\.?|number|ending|account)\b|\bcardmember\b|\bcardholder\b",
                      re.I)
_CARD_ACTIVITY = re.compile(
    r"\b(statement|total\s+(?:amount\s+)?due|minimum\s+(?:amount\s+)?due|payment\s+due|spent|transaction|"
    r"reward\s+points?|annual\s+fee|renewal|welcome|dispatch\w*|activat\w*|blocked|credit\s+limit|emi)\b", re.I)
_APPLICATION = re.compile(r"\bapplication\b|\bapplied\b", re.I)
_MONTHS = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)


def template(m: Mail) -> str:
    """Sender + subject with numbers, months and dates blanked: the monthly statement of one card,
    or one offer sent 55 times, is one template. Statistics count templates, so a mail sent often
    doesn't outweigh many different ones."""
    addr = (re.search(r"[\w.+-]+@[\w.-]+", m.sender or "") or [""])[0].lower()
    subj = _MONTHS.sub("<m>", re.sub(r"\d+", "#", (m.subject or "").lower()))
    return f"{addr}|{' '.join(subj.split())}"
_OFFERISH = re.compile(r"\b(offer|cashback|discount|voucher|apply\s+now|eligible|pre-?approved|upgrade\s+offer|"
                       r"exclusive|festive|limited\s+period|win|deal)\b", re.I)


def label(m: Mail, reg, truth: dict) -> tuple[str, str]:
    """(label, reason) from what the email says and the owner's confirmed cards, not from the
    engine's verdict. truth: {bank: {"last4": [...], "products": [...]}}"""
    from carddiscovery import classify as cl, extract as ex
    bank = reg.bank_for_sender(m.sender) or (reg.bank_from_text(f"{m.subject}\n{m.body}")
                                             if reg.app_for_sender(m.sender) else None)
    text = to_meta(m).text() + ("\n" + m.pdf_text[:4000] if m.pdf_text else "")
    core = cl.core_text(text)
    t = truth.get(bank or "", {})
    l4s, products = set(t.get("last4", [])), set(t.get("products", []))
    seen = set(ex.last4s(text)) & l4s
    ending = ex.partial_last_digits(text)
    by_end = {x for x in l4s if ending and x.endswith(ending)}
    lab, why, _ = label_intent(m, reg, truth, bank, text, core)
    return lab, why


# The seven kinds of bank mail seen in a real mailbox (2026-11), and the label each gets:
#   transaction  statement, alert, payment, refund, PIN, dispatch, renewal ...   CONFIRMED / LIKELY
#   offer_named  an offer that names the owner's card ("Card x6113 into SmartEMIs")   CONFIRMED
#   service      generic cardholder mail ("Changes in charges on your ... Credit Card")   LIKELY
#   application  applied / approved / rejected                                     LIKELY
#   cross_sell   "Your 2nd Credit Card is on Us!", invitations, loans on the card   PROMOTIONAL
#   account      bank account mail: A/c debits, NACH, deposits, FASTag, KYC, login   NOT_CARD
#   notice       fraud warnings, maintenance, results                               NOT_CARD
from carddiscovery.evidence_model import X_NUMBER as _X_NUMBER  # noqa: E402  "Card x6113" (one x)
_ACCOUNT_MAIL = re.compile(
    r"\ba/?c\b|\ba/c\s+no\b|account\s+no\b|(?:debit|credit)\s+notification|\bnach\b|\becs\b|term\s+deposit|"
    r"fixed\s+deposit|beneficiary|fastag|upi\s+txn|savings|salary\s+account|v-?kyc|video\s+kyc|"
    r"mobile\s*banking|login\s+pin|net\s*banking|cheque\s+book|tcs\s+certificate|payment\s+advice|"
    r"clearing\s+corporation|debit\s+card|demat|mutual\s+fund|money\s+quotient|remittance|"
    r"banking\s+programme|onboarding\s+kit|account\s+(?:has\s+been\s+)?activated|letter\s+of\s+sanction", re.I)
_CROSS_SELL = re.compile(
    r"(?:2nd|second)\s+(?:credit\s+)?card|why\s+not\s+two|why\s+stop\s+at|lifetime\s+free|invitation|upgrade\s+to|"
    r"personal\s+loan|loan\s+(?:of|on|against)|pre-?approved|apply\s+(?:now|today)|limited\s+period|"
    r"\d+\s*%\s*off|cashback|voucher|\bwin\b|winning|claim\s+your|\bfree\b|explore|introducing|forex|"
    r"celebrate|festive|deal|shop|gift|enjoy|unbelievable|perks|devaluation|\ba\s+metal\s+credit\s+card|"
    r"(?:free|get|claim)\s+(?:your\s+)?(?:upi\s+)?rupay\s+credit\s+card|credit\s+card\s+in\s+a\s+few\s+steps|"
    r"get\s+your\s+.*card|journeys", re.I)
_NOTICE = re.compile(
    r"phishing|vishing|fraud|scam|\bapk\b|maintenance|downtime|financial\s+results|new\s+website|"
    r"awareness|beware|stay\s+(?:alert|secure|safe)|safety\s+tips|safe\s+banking|protect|secure\s+your\s+card|"
    r"login\s+(?:alert|attempt)|whatsapp\s+connect|touch\s+id|feedback|ckyc|logged\s+on|log\s+on", re.I)
_TRANSACTION = re.compile(
    r"statement|bill\s+(?:is\s+)?(?:ready|due|generated)|amount\s+due|payment|refund|spent|swiped|"
    r"transaction\s+alert|debited\s+via\s+credit|on\s+its\s+way|dispatch|deliver|welcome|activat|"
    r"\bpin\b|renew|credit\s+balance|card\s+generation|transaction\s+settings|switched\s+off|blocked|"
    r"suspended|card\s+control|tokeni[sz]e|contactless|apple\s+pay|enabled\s+on\s+upi", re.I)


def label_intent(m: Mail, reg, truth: dict, bank: str | None = None, text: str | None = None,
                 core: str | None = None) -> tuple[str, str, str]:
    """(label, reason, intent): which of the seven kinds of bank mail this is, and its label."""
    from carddiscovery import classify as cl, extract as ex
    if bank is None:
        bank = reg.bank_for_sender(m.sender) or (reg.bank_from_text(f"{m.subject}\n{m.body}")
                                                 if reg.app_for_sender(m.sender) else None)
    text = text or to_meta(m).text() + ("\n" + m.pdf_text[:4000] if m.pdf_text else "")
    core = core or cl.core_text(text)
    head = f"{m.subject}\n{m.snippet}"
    t = truth.get(bank or "", {})
    l4s, products = set(t.get("last4", [])), set(t.get("products", []))
    seen = (set(ex.last4s(text)) | set(_X_NUMBER.findall(text))) & l4s
    ending = ex.partial_last_digits(text)
    by_end = {x for x in l4s if ending and x.endswith(ending)}
    named = set(reg.products_in(bank, core)) & products if bank else set()
    promo_tab = "CATEGORY_PROMOTIONS" in m.labels
    offer = promo_tab or bool(_OFFERISH.search(m.subject or "") or _CROSS_SELL.search(m.subject or ""))
    if seen or by_end:
        intent = "offer_named" if offer else "transaction"
        return CONFIRMED, f"shows ••{'/'.join(sorted(seen | by_end))}", intent
    if _APPLICATION.search(m.subject or "") or re.search(r"dear\s+applicant", head, re.I):
        return LIKELY, "application" + (f" for {'/'.join(sorted(named))}" if named else ""), "application"
    if named and not offer:
        return CONFIRMED, f"names {'/'.join(sorted(named))}", "transaction"
    card_in_subject = bool(re.search(r"\bcredit\s*card\b|\bcard\b", m.subject or "", re.I))
    acc = _ACCOUNT_MAIL.search(head)
    if acc and not re.search(r"\bcredit\s*card\b", m.subject or "", re.I):
        return NOT, f"bank account mail ('{acc.group(0)}')", "account"
    if offer:
        return PROMO, "offer" + (f" naming {'/'.join(sorted(named))}" if named else ""), "cross_sell"
    notice = _NOTICE.search(head)
    if notice and not _TRANSACTION.search(m.subject or ""):
        return NOT, f"notice ('{notice.group(0)}')", "notice"
    cardish = _CARDISH.search(core) or card_in_subject
    trans = _TRANSACTION.search(head)
    if cardish and trans:
        return LIKELY, f"card + '{trans.group(0)}', no number", "transaction"
    if cardish:
        return LIKELY, "about the bank's credit card in general", "service"
    noncard = _NOT_CARD.search(core) or _NOT_CARD.search(m.subject or "")
    if noncard:
        return NOT, f"says '{noncard.group(0)}', never 'credit card'", "account"
    return AMBIG, "no clear card or non-card words", "unclear"


def _truth(path: str | None) -> dict:
    if not path:
        raise SystemExit("--truth is required: the owner's confirmed cards")
    raw = json.loads(Path(path).read_text())["cards"]
    return {b: (v if isinstance(v, dict) else {"last4": v, "products": []}) for b, v in raw.items()}


def label_all(account_id: int, truth_path: str | None) -> dict:
    """Label every stored email; writes labels.json (no email text) and review.csv (subjects and
    previews of the LIKELY and AMBIGUOUS ones, for the owner to correct), both under .local."""
    import csv
    from collections import Counter
    from carddiscovery.registry import Registry
    store, reg, truth = Store(account_id), Registry.load(), _truth(truth_path)
    out, counts, intents = {}, Counter(), Counter()
    unclear: dict[str, list] = {}
    for m in store.all():
        lab, why, intent = label_intent(m, reg, truth)
        out[m.id] = {"label": lab, "reason": why, "intent": intent, "strata": m.strata, "template": template(m)}
        counts[lab] += 1
        intents[intent] += 1
        if lab == AMBIG:
            row = unclear.setdefault(template(m), [m.id, reg.bank_for_sender(m.sender) or "", m.date[:10],
                                                   m.subject[:120], " ".join(m.snippet.split())[:160], lab, why, 0, ""])
            row[7] += 1
    review_rows = list(unclear.values())
    adir = config.HOME / "analysis" / str(account_id)
    adir.mkdir(parents=True, exist_ok=True)
    os.chmod(adir, 0o700)
    lf = adir / "labels.json"
    prev = {}
    if lf.exists():  # keep the owner's corrections across re-runs
        prev = {k: v for k, v in json.loads(lf.read_text()).items() if v.get("by") == "owner"}
    out.update(prev)
    lf.write_text(json.dumps(out, indent=0))
    with open(adir / "review.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "bank", "date", "subject", "preview", "auto_label", "reason", "emails like it",
                    "your_label (CONFIRMED_CARD / LIKELY_CARD / NOT_CARD / PROMOTIONAL / AMBIGUOUS)"])
        w.writerows(sorted(review_rows, key=lambda r: (r[1], r[2])))
    return {"labels": dict(counts), "intents": dict(intents), "owner_corrections_kept": len(prev),
            "review_templates": len(review_rows),
            "files": [str(lf), str(adir / "review.csv")]}


def apply_review(account_id: int) -> int:
    """Copy the owner's answers from review.csv into labels.json (kept across re-labelling)."""
    import csv
    adir = config.HOME / "analysis" / str(account_id)
    labels = json.loads((adir / "labels.json").read_text())
    n = 0
    with open(adir / "review.csv") as f:
        for row in csv.DictReader(f):
            ans = (list(row.values())[-1] or "").strip().upper()
            if ans not in (CONFIRMED, LIKELY, NOT, PROMO, AMBIG) or row["id"] not in labels:
                continue
            tpl = labels[row["id"]].get("template")
            for mid, v in labels.items():  # the answer covers every email of the same template
                if mid == row["id"] or (tpl and v.get("template") == tpl):
                    v.update(label=ans, by="owner")
                    n += 1
    (adir / "labels.json").write_text(json.dumps(labels, indent=0))
    return n


# ---------------------------------------------------------------- the report

POSITIVE, NEGATIVE = (CONFIRMED, LIKELY), (NOT, PROMO)
STOP = {"the", "and", "for", "your", "you", "with", "this", "that", "from", "has", "have", "are", "was", "will",
        "our", "not", "any", "all", "can", "now", "get", "per", "via", "been", "here", "its", "into", "who", "to", "of",
        "on", "in", "is", "at", "by", "or", "an", "a", "be", "as", "it", "we", "us", "if", "dear", "please"}


def _tokens(text: str, owner: set[str]) -> list[str]:
    words = re.findall(r"[a-z][a-z+&'\-]*|\d+", (text or "").lower())
    return ["#" if w.isdigit() else w for w in words if w not in owner]


def _phrases(text: str, owner: set[str], n_max: int = 3) -> set[str]:
    """Words and 2-3 word phrases, numbers shown as '#'; phrases may start or end only on content
    words, so "credit card statement" is kept and "the statement of" isn't."""
    toks = _tokens(text, owner)
    out = set()
    for n in range(1, n_max + 1):
        for i in range(len(toks) - n + 1):
            g = toks[i:i + n]
            if g[0] in STOP or g[-1] in STOP or (n == 1 and (g[0] == "#" or len(g[0]) < 3)):
                continue
            out.add(" ".join(g))
    return out


def features(m: Mail, owner: set[str]) -> set[str]:
    """Phrases tagged with where they appear: sender, subject, preview, body (the email minus its
    footer), footer (disclaimer lines), file (attachment names), pdf (first page of a PDF)."""
    from carddiscovery import classify as cl
    addr = (re.search(r"[\w.+-]+@[\w.-]+", m.sender or "") or [""])[0].lower()
    local, _, dom = addr.partition("@")
    units = cl._units(m.body or "")
    footer = " ".join(u for u in units if cl.BOILERPLATE.search(u))
    body = " ".join(u for u in units if not cl.BOILERPLATE.search(u))
    f = {f"sender:{re.sub(r'[0-9]+', '#', local)}", f"domain:{dom}"}
    for where, text in (("subject", m.subject), ("preview", m.snippet), ("body", body), ("footer", footer),
                        ("file", " ".join(m.attachments)), ("pdf", (m.pdf_text or "")[:3000])):
        f |= {f"{where}:{p}" for p in _phrases(text, owner)}
    if "CATEGORY_PROMOTIONS" in m.labels:
        f.add("gmail:promotions tab")
    if m.unsubscribe:
        f.add("gmail:unsubscribe header")
    if m.attachments:
        f.add("gmail:has attachment")
    return f


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, c - h), min(1.0, c + h)


def _row(f: str, pos: int, neg: int, P: int, N: int) -> dict:
    lo, hi = _wilson(pos, pos + neg)
    lr = ((pos + 0.5) / (P + 1)) / ((neg + 0.5) / (N + 1))
    return {"feature": f, "card": pos, "not": neg, "precision": round(pos / max(1, pos + neg), 3),
            "ci": f"{lo:.2f}-{hi:.2f}", "recall": round(pos / max(1, P), 3), "fpr": round(neg / max(1, N), 3),
            "lr": round(lr, 2)}


def report(account_id: int, min_support: int = 8) -> str:
    """Writes report.md (and stats.json) under .local/analysis/<account>/. Card side = CONFIRMED +
    LIKELY, other side = NOT_CARD + PROMOTIONAL; AMBIGUOUS is left out of the numbers."""
    from collections import Counter, defaultdict
    from carddiscovery.models import CARD_EVIDENCE_TYPES, EvidenceType as T
    from carddiscovery.registry import Registry
    from cdserver import db
    store, reg = Store(account_id), Registry.load()
    adir = config.HOME / "analysis" / str(account_id)
    labels = json.loads((adir / "labels.json").read_text())
    c = db.connect()
    conn = c.execute("SELECT names, mailbox FROM gmail_connections WHERE account_id = %s", (account_id,)).fetchone()
    owner = {w.lower() for n in (conn["names"] or []) for w in re.findall(r"[A-Za-z]+", n)}
    owner |= set(re.findall(r"[a-z]+", (conn["mailbox"] or "").split("@")[0].lower()))
    names = list(conn["names"] or [])

    per_label, per_bank_label, strata_label = Counter(), defaultdict(Counter), defaultdict(Counter)
    feat = {"pos": Counter(), "neg": Counter()}          # per template
    emails = {"pos": Counter(), "neg": Counter()}        # per email, shown alongside
    bank_feat = defaultdict(lambda: {"pos": Counter(), "neg": Counter()})
    rows_feats: list[tuple[str, set[str], str | None]] = []
    seen_templates: dict[tuple[str, str], set[str]] = {}
    template_bank: dict[tuple[str, str], str] = {}
    templates, bank_templates = Counter(), defaultdict(Counter)
    errors = {"false_card": [], "missed_card": [], "dropped_number": []}
    path_label = Counter()
    for m in store.all():
        lab = labels.get(m.id, {}).get("label", AMBIG)
        tr = trace(m, reg, names)
        bank = tr["bank"] or (reg.bank_from_text(m.subject + "\n" + m.body) if tr["app"] else None) or "?"
        per_label[lab] += 1
        per_bank_label[bank][lab] += 1
        for s in m.strata:
            strata_label[s][lab] += 1
        kind = tr["path"][0][0] if tr["path"] else "?"
        path_label[(f"{kind}:{tr['type']}", lab)] += 1
        engine_card = T(tr["type"]) in CARD_EVIDENCE_TYPES and (tr["last4s"] or tr["last_digits"] or tr["product"])
        if engine_card and lab in NEGATIVE:
            errors["false_card"].append((m, tr, labels[m.id]["reason"]))
        if lab == CONFIRMED and not engine_card:
            errors["missed_card"].append((m, tr, labels[m.id]["reason"]))
        if lab in POSITIVE and any(n["why"] != "kept" for n in tr["numbers"]) and not tr["last4s"]:
            errors["dropped_number"].append((m, tr, labels[m.id]["reason"]))
        if lab in POSITIVE + NEGATIVE:
            side = "pos" if lab in POSITIVE else "neg"
            fs = features(m, owner)
            emails[side].update(fs)
            key = (template(m), side)
            if key in seen_templates:  # counted once per template: a repeated mail adds no new evidence
                seen_templates[key] |= fs
                continue
            seen_templates[key] = set(fs)
            template_bank[key] = bank
            templates[side] += 1
            bank_templates[bank][side] += 1
    for key, fs in seen_templates.items():
        side, bank = key[1], template_bank[key]
        feat[side].update(fs)
        bank_feat[bank][side].update(fs)
        rows_feats.append((side, fs, bank))
    P, N = templates["pos"], templates["neg"]
    keep = [f for f in set(feat["pos"]) | set(feat["neg"]) if feat["pos"][f] + feat["neg"][f] >= min_support]
    table = sorted((_row(f, feat["pos"][f], feat["neg"][f], P, N) for f in keep), key=lambda r: -r["lr"])

    # Pairs among the frequent features: kept when the pair says more than either part alone.
    top = sorted(keep, key=lambda f: -(feat["pos"][f] + feat["neg"][f]))[:250]
    tops = set(top)
    pair = {"pos": Counter(), "neg": Counter()}
    for side, fs, _ in rows_feats:
        ff = sorted(fs & tops)
        for i in range(len(ff)):
            for j in range(i + 1, len(ff)):
                pair[side][(ff[i], ff[j])] += 1
    single = {r["feature"]: r["precision"] for r in table}
    pairs = []
    for k in set(pair["pos"]) | set(pair["neg"]):
        p, n = pair["pos"][k], pair["neg"][k]
        if p + n < 10:
            continue
        prec = p / (p + n)
        lift = min(abs(prec - single.get(k[0], prec)), abs(prec - single.get(k[1], prec)))
        if lift >= 0.15:
            r = _row(" + ".join(k), p, n, P, N)
            r["changes_by"] = round(lift, 2)
            pairs.append(r)
    pairs.sort(key=lambda r: (-r["changes_by"], -(r["card"] + r["not"])))

    def tbl(rows, cols=("feature", "card", "not", "precision", "ci", "recall", "fpr", "lr")):
        out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        out += ["| " + " | ".join(str(r.get(c2, "")) for c2 in cols) + " |" for r in rows]
        return "\n".join(out)

    md = [f"# Card-evidence study: account {account_id}", "",
          f"Emails: {sum(per_label.values())} ({', '.join(f'{k} {v}' for k, v in per_label.most_common())}). "
          f"Counted by template (sender + subject with numbers and months blanked; a monthly statement or an "
          f"offer sent 55 times is one): card side (CONFIRMED + LIKELY) {P} templates, other side "
          f"(NOT_CARD + PROMOTIONAL) {N} templates. AMBIGUOUS is left out.", "",
          "All counts below are templates. precision = share of templates with the feature that are card mail; "
          "ci = 95% interval for it; "
          "recall = share of all card emails that have it; fpr = share of non-card emails that have it; "
          "lr = how many times likelier in card mail than in other mail (above 1: points to card).", "",
          "## Labels by sample", tbl([{"feature": s, **{k: v for k, v in cnt.items()}} for s, cnt in
                                      sorted(strata_label.items())], ("feature",) + (CONFIRMED, LIKELY, NOT, PROMO, AMBIG)),
          "", "## Labels by bank", tbl([{"feature": b, **dict(cnt)} for b, cnt in sorted(per_bank_label.items())],
                                       ("feature",) + (CONFIRMED, LIKELY, NOT, PROMO, AMBIG)),
          "", "## Engine decision path vs label",
          tbl([{"feature": k[0], "label": k[1], "card": v} for k, v in path_label.most_common(60)],
              ("feature", "label", "card")),
          "", f"## Strongest card signals (support >= {min_support})", tbl(table[:80]),
          "", "## Strongest not-card signals", tbl(table[-80:][::-1]),
          "", "## Pairs that change the picture (vs either word alone)", tbl(pairs[:80],
                                                                              ("feature", "card", "not", "precision", "ci", "changes_by", "lr"))]
    for b in sorted(bank_feat):
        Pb, Nb = bank_templates[b]["pos"], bank_templates[b]["neg"]
        fs = [f for f in set(bank_feat[b]["pos"]) | set(bank_feat[b]["neg"])
              if bank_feat[b]["pos"][f] + bank_feat[b]["neg"][f] >= 3]
        if Pb + Nb < 10 or not fs:
            continue
        rows = sorted((_row(f, bank_feat[b]["pos"][f], bank_feat[b]["neg"][f], Pb, Nb) for f in fs),
                      key=lambda r: -r["lr"])
        md += ["", f"## Bank: {b} ({Pb} card / {Nb} other)", "### card side", tbl(rows[:20]),
               "### other side", tbl(rows[-20:][::-1])]
    for name, items in errors.items():
        md += ["", f"## Errors: {name} ({len(items)})", "| id | bank | date | subject | engine path | engine saw | label reason |",
               "|---|---|---|---|---|---|---|"]
        for m, tr, why in items[:150]:
            saw = ", ".join(x for x in [("••" + "/".join(tr["last4s"])) if tr["last4s"] else "",
                                        f"ending {tr['last_digits']}" if tr["last_digits"] else "",
                                        tr["product"] or ""] if x) or "-"
            dropped = "; ".join(f"{n['digits']} dropped ({n['why']}: …{n['before'][-20:]})" for n in tr["numbers"]
                                if n["why"] != "kept")[:120]
            md.append(f"| {m.id} | {tr['bank']} | {m.date[:10]} | {m.subject[:70].replace('|', '/')} | "
                      f"{tr['path']} → {tr['type']} | {saw} {dropped} | {why} |")
    (adir / "report.md").write_text("\n".join(md))
    (adir / "stats.json").write_text(json.dumps({"features": table, "pairs": pairs, "P": P, "N": N}, indent=0))
    return str(adir / "report.md")
