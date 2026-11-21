"""Development only: the second study pass, answering the review of the first formula.

    CARDDISCOVERY_DEV=1 CD_CORPUS_ACCOUNTS=60 python -m cdserver corpus blind --account 60 --truth t.json
    ... corpus blind-apply --account 60       (reads your answers from blind_review.csv)
    ... corpus evaluate --account 60 --truth t.json   (writes evaluation.md)

What changed against the first pass (corpus_model.study):
  - Four separate labels per email instead of one: KIND (16 kinds: statement, payment reminder,
    lifecycle, security notice, card-linked loan ...), CARD-RELATED (specific card / credit cards in
    general / unrelated), OWNERSHIP (established / not / unknown) and IDENTIFIES (product, number,
    both, issuer only). Relevance and ownership are learned and checked against separate truths.
  - Ground truth that doesn't come from word rules: blind_review.csv shows emails without any label
    or score, in random order; your answers are the only labels the final numbers are measured on.
    How often the rule labels disagree with yours, per rule label, is the label-leakage measure.
  - Boilerplate learned from the mailbox: a sentence in at least 3 templates and 30% of a sender's
    templates is a footer, kept in its own channel and out of the content features and pairs.
  - Campaign families: templates of one sender with similar subjects ("HDFC Card EMI offer",
    "Last chance: HDFC Card EMI offer") are one family. Validation folds and bootstraps keep a family
    together, and a family counts once as card evidence.
  - Products by context: held ("your X card ending"), applied for, offered, in a notice, or plain.
  - Calibration, not only ranking: PR-AUC, Brier score, a reliability table, precision, recall and
    false-discovery rate at each threshold, per bank, and a sign-stability check per weight.
  - Card level by event diversity: a card reaches the wallet on independent event families
    (statement, payment, dispatch, transaction ...), not on two copies of one campaign.
  - Identifier provenance: every card-shaped number with where it was, its context, its type and why
    a guard kept or dropped it; dropped numbers that are the owner's real cards are listed.
"""

from __future__ import annotations

import csv
import json
import math
import random
import re
from collections import Counter, defaultdict

from carddiscovery import evidence_model as em

from . import config
from . import corpus_model as cm

# ---------------------------------------------------------------- kinds (16) and the four labels

def _rx(p: str) -> re.Pattern:
    return re.compile(p, re.I)


# Checked in this order on subject + preview; the first match is the kind.
KIND_RULES = [
    ("application", _rx(r"\bapplication\b|\bapplied\b|dear\s+applicant|application\s+(?:status|number|id)")),
    ("card_linked_loan", _rx(r"loan\s+(?:on|against)\s+(?:your\s+)?(?:\w+\s+){0,3}credit\s+card|"
                             r"(?:insta|jumbo|smart)\s*(?:loan|jumbo)|cash\s+on\s+call|personal\s+loan")),
    ("statement", _rx(r"\be?-?statement\b")),
    ("payment_reminder", _rx(r"amount\s+due|minimum\s+(?:amount\s+)?due|total\s+(?:amount\s+)?due|due\s+date|"
                             r"payment\s+reminder|bill\s+(?:is\s+)?(?:ready|due|generated)|overdue")),
    ("payment_confirmation", _rx(r"payment\s+(?:received|confirmation|successful|of\s+rs)|thank\s+you\s+for\s+"
                                 r"(?:your\s+|the\s+)?payment|credited\s+(?:to|into)\s+your\s+(?:\w+\s+)?card|"
                                 r"bill\s+paid|paid\s+(?:your|the)\s+bill|refund")),
    ("limit_update", _rx(r"credit\s+limit|limit\s+(?:increase|enhance|revis|upgrade)")),
    ("card_lifecycle", _rx(r"dispatch|on\s+its\s+way|deliver|activat|\bpin\b|renew|blocked|hotlist|replac|"
                           r"card\s+generation|closure|closed|tokeni[sz]|contactless|card\s+control|"
                           r"transaction\s+settings|switched\s+off|enabled\s+on\s+upi|welcome")),
    ("transaction", _rx(r"\bspent\b|debited|swiped|transaction\s+alert|used\s+(?:for|at)|\btxn\b|"
                        r"transaction\s+(?:of|on|at)|purchase\s+(?:of|at)")),
    ("card_security", _rx(r"fraud|phishing|vishing|scam|beware|safety|safe\s+banking|protect|security|"
                          r"stay\s+(?:alert|secure|safe)|\bapk\b")),
    ("reward_notification", _rx(r"reward\s+points?|\bpoints\s+(?:earned|credited|expir|redeem|balance)|"
                                r"cashback\s+(?:credited|earned)|edge\s+(?:reward|miles)|neucoins|milestone")),
    ("offer", _rx(r"\boffer|cashback|discount|voucher|%\s*off|\bfree\b|\bdeal|upgrade\s+to|\b2nd\b|second\s+card|"
                  r"invitation|limited\s+period|\bwin\b|exclusive|festive|lifetime\s+free|\bperks\b|apply\s+(?:now|"
                  r"today)|pre-?approved|celebrate|introducing|explore|\bemi\b|smartemi")),
    ("notice_generic", _rx(r"maintenance|downtime|financial\s+results|new\s+website|important\s+(?:update|"
                           r"communication|notice|information)|changes?\s+(?:in|to|on)|revision|terms")),
]
KINDS = [k for k, _ in KIND_RULES] + ["offer_named", "card_cross_sell", "account", "service", "unclear"]
# Kinds that, about a credit card the person holds, show they hold it (event families for the card).
OWNING_KINDS = {"statement", "payment_reminder", "payment_confirmation", "limit_update", "card_lifecycle",
                "transaction"}
# Kinds that never show a card is held, whatever they mention.
NOT_OWNING_KINDS = {"application", "card_cross_sell", "account", "notice_generic", "card_security"}

_CREDIT = _rx(r"\bcredit\s*card|\bcard\s*(?:no\.?|number|ending|account)\b|\bcard\s*member|\bcardholder|"
              r"\bcard\s+[xX*•]+\d")
_ACCOUNT = _rx(r"\ba/?c\b|account\s+no\b|savings|salary\s+account|current\s+account|debit\s+card|\bnach\b|"
               r"term\s+deposit|fixed\s+deposit|recurring\s+deposit|fastag|\bupi\s+txn|v-?kyc|video\s+kyc|"
               r"net\s*banking|mobile\s*banking|demat|mutual\s+fund|remittance|cheque|beneficiary|insurance|"
               r"home\s+loan|loan\s+account|money\s+quotient|nominee")


# Guards the engine lacks today (found in this study, 2026-11): numbers right after these words aren't
# credit cards. "debited from your RuPay Credit Card (ending 3254) Paid to ******1763": a payee.
_DEBIT_BEFORE = _rx(r"debit\s+card(?:\s+(?:no\.?|number|ending(?:\s+with)?))?\s*[:\-]?\s*[xX*•]*\s*$")
_PAYEE_BEFORE = _rx(r"(?:paid\s+to|\bto\s+vpa|beneficiary|payee|transferred\s+to)\s*[:\-]?\s*[xX*•]*\s*$")
_ACCOUNT_NO_BEFORE = _rx(r"(?:account|a/c)\s*(?:no\.?|number)?\s*(?:ending\s+(?:with|in)\s*)?[:\-]?\s*[xX*•]*\s*$")


def card_ids(m, reg, bank: str | None, boiler: set[str]) -> dict:
    """What identifies a card in this email, with provenance: every card-shaped number (where, the
    words before it, its type, the guard's decision and reason), held phrases and products by context."""
    from carddiscovery import classify as cl, extract as ex
    from .corpus import _X_NUMBER
    parts = {"subject": m.subject or "", "preview": m.snippet or "",
             "body": content(m.body or "", boiler), "footer": footer(m.body or "", boiler),
             "pdf": (m.pdf_text or "")[:4000]}
    numbers = []
    for where, text in parts.items():
        hits = [(h, p) for p in ex._LAST4_PATTERNS for h in p.finditer(text)]
        hits += [(h, None) for h in _X_NUMBER.finditer(text)]
        engine_keeps = set(ex.last4s(text)) | set(_X_NUMBER.findall(text))
        for h, _p in hits:
            before = text[max(0, h.start() - 40):h.start()]
            reason = ("debit card number" if _DEBIT_BEFORE.search(before) else
                      "payee (UPI / paid to)" if _PAYEE_BEFORE.search(before) else
                      "account number" if ex._ACCOUNT_BEFORE.search(before) or _ACCOUNT_NO_BEFORE.search(before) else
                      "phone number" if ex._PHONE_BEFORE.search(before) else
                      "identifier (CRN, customer id ...)" if ex._ID_BEFORE.search(before) else
                      "example number" if ex._EXAMPLE_BEFORE.search(text[max(0, h.start() - 160):h.start()]) else "")
            if not reason and bank and bank not in cl.CREDIT_ONLY_BANKS and h.group(1) in ex.account_last4s(text):
                reason = "account number (after 'account')"
            if not reason and h.group(1) not in engine_keeps:
                reason = "other engine guard"
            # engine: what production does today; the study guard (above) is what it is measured against
            numbers.append({"digits": h.group(1), "where": where, "context": " ".join(before.split())[-40:],
                            "type": "card_last4" if not reason else reason.split(" (")[0].replace(" ", "_"),
                            "guard": "rejected" if reason else "accepted", "reason": reason or "card number",
                            "engine": "kept" if h.group(1) in engine_keeps else "dropped"})
    head_body = "\n".join(parts[k] for k in ("subject", "preview", "body"))
    held = ex.held_phrases(head_body)
    products = product_contexts(m, reg, bank, parts)
    ending = ex.partial_last_digits(head_body)
    return {"numbers": numbers, "held": held, "ending": ending, "products": products}


from carddiscovery.evidence_model import product_contexts  # noqa: E402  (shared with production)


def kind_of(m, ids: dict, real_l4: set[str]) -> str:
    head = f"{m.subject or ''}\n{m.snippet or ''}"
    shown = {n["digits"] for n in ids["numbers"] if n["guard"] == "accepted"}
    numbered = bool(shown) or bool(ids["held"])
    for k, rx in KIND_RULES:
        if rx.search(head):
            if k == "offer":
                return "offer_named" if numbered else "card_cross_sell"
            return k
    if _ACCOUNT.search(head) and not _CREDIT.search(head):
        return "account"
    if numbered or _CREDIT.search(head) or re.search(r"\bcard\b", m.subject or "", re.I):
        return "service"
    if _ACCOUNT.search(m.body or ""):
        return "account"
    return "unclear"


def dimensions(m, ids: dict, kind: str, bank: str | None, truth: dict) -> dict:
    """The rule-based proposal for the four labels (the blind review is the truth they're measured on)."""
    t = truth.get(bank or "", {})
    l4s, prods = set(t.get("last4", [])), {p.lower() for p in t.get("products", [])}
    shown = {n["digits"] for n in ids["numbers"] if n["guard"] == "accepted"} | {d for _, d in ids["held"]}
    ending = ids["ending"]
    own_num = (shown & l4s) or {x for x in l4s if ending and x.endswith(ending)}
    own_prod = {p for p, _, ctx in ids["products"] if ctx in ("held", "plain") and p.lower() in prods}
    any_prod = {p for p, _, _ in ids["products"]}
    text = f"{m.subject}\n{m.snippet}\n{m.body[:3000]}"
    credit = bool(_CREDIT.search(text))
    if kind in ("account",) and not shown:
        related = "unrelated"
    elif shown or ids["held"] or own_prod or (ending and credit):
        related = "specific"
    elif credit or any_prod or kind in OWNING_KINDS | {"application", "card_linked_loan", "reward_notification"}:
        related = "generic"
    elif kind == "unclear":
        related = "ambiguous"
    else:
        related = "unrelated"
    if kind in NOT_OWNING_KINDS and not (own_num and kind == "card_security"):
        ownership = "not_established"
    elif own_num and kind not in ("application", "card_cross_sell"):
        ownership = "established"
    elif kind in OWNING_KINDS and (own_prod or credit) and related != "unrelated":
        ownership = "established" if own_prod or ending else "unknown"
    else:
        ownership = "unknown" if related != "unrelated" else "not_established"
    has_num, has_prod = bool(shown or ending), bool(own_prod or any_prod)
    identifies = ("both" if has_num and has_prod else "number" if has_num else "product" if has_prod
                  else "issuer" if bank else "none")
    return {"kind": kind, "card_related": related, "ownership": ownership, "identifies": identifies}


# ---------------------------------------------------------------- boilerplate and campaign families

def norm_unit(u: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", u.lower())).strip()[:200]


def _domain(sender: str) -> str:
    return ((re.search(r"@([\w.-]+)", sender or "") or [None, ""])[1]).lower()


def learn_boilerplate(mails, tmpl) -> set[str]:
    """Sentences repeated across many of a sender's templates: footers, disclaimers, app-store lines."""
    from carddiscovery import classify as cl
    seen: dict[str, set[str]] = defaultdict(set)
    per_domain: dict[str, set[str]] = defaultdict(set)
    unit_domain: dict[str, set[str]] = defaultdict(set)
    for m in mails:
        t, d = tmpl[m.id], _domain(m.sender)
        per_domain[d].add(t)
        for u in {norm_unit(u) for u in cl._units(m.body or "")}:
            if len(u) >= 25:
                seen[u].add(t)
                unit_domain[u].add(d)
    out = set()
    for u, ts in seen.items():
        if len(ts) < 3:
            continue
        share = max(len({t for t in ts if t in per_domain[d]}) / max(1, len(per_domain[d])) for d in unit_domain[u])
        if share >= 0.3 or len(unit_domain[u]) >= 3:
            out.add(u)
    return out


def content(body: str, boiler: set[str]) -> str:
    from carddiscovery import classify as cl
    return " ".join(u for u in cl._units(body) if not cl.BOILERPLATE.search(u) and norm_unit(u) not in boiler)


def footer(body: str, boiler: set[str]) -> str:
    from carddiscovery import classify as cl
    return " ".join(u for u in cl._units(body) if cl.BOILERPLATE.search(u) or norm_unit(u) in boiler)


_SUBJ_STOP = {"your", "the", "for", "and", "with", "you", "a", "an", "of", "on", "to", "in", "is", "now", "#", "<m>",
              "bank", "card", "credit", "from", "our", "get", "this", "are", "has", "dear", "|"}


def families(tmpl_by_id: dict[str, str]) -> dict[str, str]:
    """template -> campaign family: same sender domain and subject words overlapping by half or more."""
    tpls = sorted(set(tmpl_by_id.values()))
    words = {}
    for t in tpls:
        addr, _, subj = t.partition("|")
        words[t] = (addr.split("@")[-1], {w for w in re.findall(r"[a-z<>#]+", subj) if w not in _SUBJ_STOP})
    parent = {t: t for t in tpls}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    by_dom = defaultdict(list)
    for t in tpls:
        by_dom[words[t][0]].append(t)
    for ts in by_dom.values():
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                wa, wb = words[a][1], words[b][1]
                if wa and wb and len(wa & wb) / len(wa | wb) >= 0.5:
                    parent[find(a)] = find(b)
    return {t: find(t) for t in tpls}


# ---------------------------------------------------------------- features (boilerplate-aware, products by context)

def features(m, reg, bank, boiler=None) -> set[str]:
    """Exactly what production scores (carddiscovery.evidence_model.features). boiler: also drop
    the sentences learned as boilerplate from this mailbox (study only; production can't learn
    them from one email, so the shipped model is trained without)."""
    if boiler is None:
        return em.features(m, reg, bank)
    return em.features(m, reg, bank, content(m.body or "", boiler), footer(m.body or "", boiler))


# ---------------------------------------------------------------- metrics

def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / max(1, len(ys))


def average_precision(ps, ys):
    order = sorted(zip(ps, ys), key=lambda t: -t[0])
    tp, out, P = 0, 0.0, sum(ys)
    for i, (_, y) in enumerate(order, 1):
        if y:
            tp += 1
            out += tp / i
    return out / max(1, P)


def reliability(ps, ys, bins=(0, .1, .3, .5, .7, .9, 1.01)):
    rows = []
    for lo, hi in zip(bins, bins[1:]):
        sel = [(p, y) for p, y in zip(ps, ys) if lo <= p < hi]
        if sel:
            rows.append({"bin": f"{lo:.1f}-{min(hi, 1):.1f}", "n": len(sel),
                         "mean_score": round(sum(p for p, _ in sel) / len(sel), 2),
                         "observed": round(sum(y for _, y in sel) / len(sel), 2)})
    return rows


def at(ps, ys, t):
    tp = sum(p >= t and y for p, y in zip(ps, ys))
    fp = sum(p >= t and not y for p, y in zip(ps, ys))
    fn = sum(p < t and y for p, y in zip(ps, ys))
    return {"t": t, "precision": round(tp / max(1, tp + fp), 3), "recall": round(tp / max(1, tp + fn), 3),
            "fdr": round(fp / max(1, tp + fp), 3), "fp": fp, "fn": fn}


def platt(ps, ys, iters=400):
    """Maps raw scores to calibrated probabilities on held-out scores: p' = σ(a·logit(p) + b)."""
    a, b = 1.0, 0.0
    zs = [math.log(max(1e-6, p) / max(1e-6, 1 - p)) for p in ps]
    for _ in range(iters):
        ga = gb = 0.0
        for z, y in zip(zs, ys):
            q = 1 / (1 + math.exp(-max(-30, min(30, a * z + b))))
            ga += (q - y) * z
            gb += q - y
        a -= 0.05 * ga / len(zs)
        b -= 0.05 * gb / len(zs)
    return a, b


def apply_platt(a, b, p):
    z = math.log(max(1e-6, p) / max(1e-6, 1 - p))
    return 1 / (1 + math.exp(-max(-30, min(30, a * z + b))))


# ---------------------------------------------------------------- building the study rows

def build(account_id: int, truth: dict):
    from carddiscovery.registry import Registry
    from .corpus import Store, template, trace
    reg = Registry.load()
    mails = list(Store(account_id).all())
    tmpl = {m.id: template(m) for m in mails}
    boiler = learn_boilerplate(mails, tmpl)
    fam = families(tmpl)
    gt = _ground_truth(account_id)
    hf = config.HOME / "analysis" / str(account_id) / "haiku_labels.json"
    haiku = json.loads(hf.read_text()) if hf.exists() else {}
    rows = []
    for m in mails:
        bank = reg.bank_for_sender(m.sender) or (reg.bank_from_text(m.subject + "\n" + m.body)
                                                 if reg.app_for_sender(m.sender) else None)
        ids = card_ids(m, reg, bank, boiler)
        real = set(truth.get(bank or "", {}).get("last4", []))
        kind = kind_of(m, ids, real)
        dims = dimensions(m, ids, kind, bank, truth)
        tr = trace(m, reg, [])
        from carddiscovery.models import EvidenceType
        rows.append({"id": m.id, "bank": bank or "?", "date": m.date[:10], "template": tmpl[m.id],
                     "family": fam[tmpl[m.id]], "x": features(m, reg, bank), "x_learned": features(m, reg, bank, boiler),
                     "ids": ids, **dims, "truth": gt.get(m.id), "haiku": haiku.get(m.id), "engine": tr, "subject": m.subject, "real_l4": real,
                     "strata": list(m.strata), "event": em.event_of(EvidenceType(tr["type"]), m.subject)})
    return rows, boiler, reg, mails


def _ground_truth(account_id: int) -> dict:
    p = config.HOME / "analysis" / str(account_id) / "ground_truth.json"
    return json.loads(p.read_text()) if p.exists() else {}


def targets(r, which: str, source: str = "auto"):
    """1 / 0 / None (left out). relevance: about a credit card (a specific one or in general);
    ownership: shows a card is held (unknown left out)."""
    d = r["truth"] if source == "truth" else r["haiku"] if source == "haiku" else r
    if not d:
        return None
    if which == "relevance":
        return {"specific": 1, "generic": 1, "unrelated": 0}.get(d["card_related"])
    return {"established": 1, "not_established": 0}.get(d["ownership"])


def template_rows(rows, which, source="auto", key="x"):
    """One row per (template, target value): repeated mail counts once."""
    out = {}
    for r in rows:
        y = targets(r, which, source)
        if y is None:
            continue
        k = (r["template"], y)
        if k not in out:
            out[k] = {"x": set(r[key]), "y": y, "bank": r["bank"], "family": r["family"], "date": r["date"],
                      "template": r["template"], "n": 1}
        else:
            out[k]["x"] |= r[key]
            out[k]["n"] += 1
            out[k]["date"] = min(out[k]["date"], r["date"])
    return list(out.values())


# ---------------------------------------------------------------- validation

def _fit(tr, names, l2):
    return cm.fit([t["x"] for t in tr], [t["y"] for t in tr], names, l2)


def oof(trows, names, l2, group: str, k: int = 5, seed: int = 7):
    """Out-of-fold scores with whole groups held out: group='bank' (each bank alone) or 'family'
    (campaign families in k folds), so no campaign is in both training and test."""
    if group == "bank":
        folds = {g: g for g in {t["bank"] for t in trows}}
    else:
        fams = sorted({t["family"] for t in trows})
        random.Random(seed).shuffle(fams)
        folds = {f: i % k for i, f in enumerate(fams)}
    key = (lambda t: folds[t["bank"]]) if group == "bank" else (lambda t: folds[t["family"]])
    scores = [None] * len(trows)
    signs = defaultdict(list)
    for fold in set(folds.values()):
        tr = [t for t in trows if key(t) != fold]
        te_i = [i for i, t in enumerate(trows) if key(t) == fold]
        if not te_i or len({t["y"] for t in tr}) < 2:
            continue
        b, w = _fit(tr, names, l2)
        for n in names:
            signs[n].append(w[n])
        for i in te_i:
            scores[i] = cm.score(b, w, trows[i]["x"])
    keep = [i for i, s in enumerate(scores) if s is not None]
    return [scores[i] for i in keep], [trows[i]["y"] for i in keep], [trows[i] for i in keep], signs


def time_split(trows, names, l2, cut="2026-05-20"):
    """Train on families first seen before the cut, test on families first seen after: new campaigns."""
    first = {}
    for t in trows:
        first[t["family"]] = min(first.get(t["family"], "9"), t["date"])
    tr = [t for t in trows if first[t["family"]] < cut]
    te = [t for t in trows if first[t["family"]] >= cut]
    if not te or len({t["y"] for t in tr}) < 2:
        return None
    b, w = _fit(tr, names, l2)
    ps, ys = [cm.score(b, w, t["x"]) for t in te], [t["y"] for t in te]
    return {"templates": len(te), "auc": round(cm.auc(ps, ys), 3), "ap": round(average_precision(ps, ys), 3),
            "brier": round(brier(ps, ys), 3)}


def family_bootstrap(trows, names, l2, w0, n=40, seed=7):
    """Weight ranges from resampling whole campaign families."""
    rnd = random.Random(seed)
    by_f = defaultdict(list)
    for t in trows:
        by_f[t["family"]].append(t)
    fams = list(by_f)
    boots = defaultdict(list)
    for _ in range(n):
        sample = [t for f in (rnd.choice(fams) for _ in fams) for t in by_f[f]]
        if len({t["y"] for t in sample}) < 2:
            continue
        _, w = cm.fit([t["x"] for t in sample], [t["y"] for t in sample], names, l2, iters=300,
                      w0=[w0[k] for k in names])
        for k, v in w.items():
            boots[k].append(v)
    return boots


# ---------------------------------------------------------------- card level by event diversity

# Card candidates as the engine makes them (its own numbers, after its guards), so the card-level
# numbers say what production would do.
def card_groups(rows, truth):
    """Emails per real card, by the engine's numbers (or a 2-3 digit ending only one card has)."""
    groups = defaultdict(list)
    for r in rows:
        l4s = r["real_l4"]
        e = r["engine"]["last_digits"]
        hit = (set(r["engine"]["last4s"]) & l4s) | {x for x in l4s if e and x.endswith(e)
                                                    and len([y for y in l4s if y.endswith(e)]) == 1}
        for x in hit:
            groups[(r["bank"], x)].append(r)
    return groups


def false_groups(rows):
    """Emails per engine number that is none of the owner's cards: would-be false cards."""
    groups = defaultdict(list)
    for r in rows:
        if r["engine"]["type"] in _CARD_TYPES:
            for x in set(r["engine"]["last4s"]) - r["real_l4"]:
                groups[(r["bank"], x)].append(r)
    return groups


def decide_old(es, p):
    """The first proposal: wallet at best >= 0.7 or two emails >= 0.5, ask at >= 0.3."""
    ps = sorted((p[e["id"]] for e in es), reverse=True)
    strong = sum(q >= 0.5 for q in ps)
    return "wallet" if ps[0] >= 0.7 or strong >= 2 else "ask" if ps[0] >= 0.3 else "hidden"


def decide_events(es, p, costs):
    """Production's rule (evidence_model.decide): event families, then product costs."""
    model = {"version": "study", "costs": costs}
    return em.decide([{"p_own": p[e["id"]], "event": e["event"]} for e in es], model)


# ---------------------------------------------------------------- the blind review

def blind(account_id: int, truth: dict, n_per_kind: int = 6, hard: int = 30, seed: int = 11) -> str:
    """blind_review.csv: emails to label by hand, no rule label or score shown, in random order."""
    rows, boiler, _, mails = build(account_id, truth)
    by_id = {m.id: m for m in mails}
    rnd = random.Random(seed)
    one = {}
    for r in rows:  # one email per template
        one.setdefault(r["template"], r)
    tpl_rows = list(one.values())
    rnd.shuffle(tpl_rows)
    pick, per_kind = [], Counter()
    for r in tpl_rows:
        if per_kind[r["kind"]] < n_per_kind:
            pick.append(r)
            per_kind[r["kind"]] += 1
    # Hard cases: offers, loans, rewards, notices and service mail that show a number or a product,
    # products outside a held context, and anything the engine and the rules disagree on.
    picked = {r["id"] for r in pick}
    hard_rows = [r for r in tpl_rows if r["id"] not in picked and (
        (r["kind"] in ("offer_named", "card_cross_sell", "card_linked_loan", "reward_notification", "card_security",
                       "service", "notice_generic") and (r["ids"]["numbers"] or r["ids"]["products"]))
        or any(ctx in ("offer", "notice", "application") for _, _, ctx in r["ids"]["products"]))]
    pick += hard_rows[:hard]
    rnd.shuffle(pick)
    adir = config.HOME / "analysis" / str(account_id)
    path = adir / "blind_review.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["n", "id", "bank", "date", "sender", "subject", "preview", "content (footers removed)",
                    "kind: " + " / ".join(KINDS),
                    "card_related: specific / generic / unrelated / unsure",
                    "ownership: established / not_established / unknown",
                    "identifies: both / number / product / issuer / none", "notes"])
        for i, r in enumerate(pick, 1):
            m = by_id[r["id"]]
            w.writerow([i, m.id, r["bank"], m.date[:10], m.sender, m.subject[:140],
                        " ".join(m.snippet.split())[:200], content(m.body or "", boiler)[:600], "", "", "", "", ""])
    return f"{path} ({len(pick)} emails: {dict(per_kind)} + {min(hard, len(hard_rows))} hard cases)"


_VALID = {"card_related": {"specific", "generic", "unrelated", "unsure"},
          "ownership": {"established", "not_established", "unknown"},
          "identifies": {"both", "number", "product", "issuer", "none"}}


def blind_apply(account_id: int) -> str:
    """Reads your answers; every email of the same template gets them. Wrong values are listed, not guessed."""
    from .corpus import Store, template
    adir = config.HOME / "analysis" / str(account_id)
    answers, problems = {}, []
    with open(adir / "blind_review.csv") as f:
        rd = csv.reader(f)
        next(rd)
        for row in rd:
            mid, kind, rel, own, ident = row[1], *[c.strip().lower() for c in row[8:12]]
            if not (kind or rel or own or ident):
                continue
            bad = [n for n, v in (("kind", kind), ("card_related", rel), ("ownership", own), ("identifies", ident))
                   if v and v not in (KINDS if n == "kind" else _VALID[n])]
            if bad or not (rel and own):
                problems.append(f"row {row[0]}: {'missing card_related/ownership' if not (rel and own) else bad}")
                continue
            answers[mid] = {"kind": kind or None, "card_related": rel, "ownership": own, "identifies": ident or None,
                            "notes": row[12] if len(row) > 12 else ""}
    by_tpl = {}
    mails = list(Store(account_id).all())
    tmpl = {m.id: template(m) for m in mails}
    for mid, a in answers.items():
        by_tpl[tmpl.get(mid)] = dict(a, reviewed=mid)
    gt = {m.id: by_tpl[tmpl[m.id]] for m in mails if tmpl[m.id] in by_tpl}
    (adir / "ground_truth.json").write_text(json.dumps(gt, indent=0))
    return f"{len(answers)} answers -> {len(gt)} emails labelled" + ("; problems: " + "; ".join(problems) if problems else "")


# ---------------------------------------------------------------- the evaluation report

def _card_types():
    from carddiscovery.models import CARD_EVIDENCE_TYPES
    return {t.value for t in CARD_EVIDENCE_TYPES}


_CARD_TYPES = _card_types()
DROP = {"masked_number_SP", "reward_B_only", "spend_SP", "loan_SP", "emi_SP", "important_update_S", "reward_SP"}
WATCH = ["product_held", "product_held_S", "product_plain", "product_plain_S", "product_offer", "product_offer_S",
         "product_application", "product_application_S", "product_notice", "mitc_FB", "gmail_promotions",
         "welcome_SP", "dear_cardholder_PB", "statement_B_only"]


def evaluate(account_id: int, truth: dict, labels: str = "auto") -> str:
    from .corpus import _wilson
    rows, boiler, reg, mails = build(account_id, truth)
    adir = config.HOME / "analysis" / str(account_id)
    md = ["# Second study pass: labels, leakage, calibration, guards, card level", "",
          f"{len(rows)} emails, {len({r['template'] for r in rows})} templates, "
          f"{len({r['family'] for r in rows})} campaign families, {len(boiler)} learned boilerplate sentences. "
          "All model numbers are out of sample. Rule labels are a proposal; the blind review is the truth.", ""]

    def tbl(rs, cols):
        return ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)] + [
            "| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rs]

    # 1. The four labels
    md += ["## 1. Four labels per email (rule proposal)", ""]
    kc = Counter(r["kind"] for r in rows)
    md += tbl([{"kind": k, "emails": v, "templates": len({r["template"] for r in rows if r["kind"] == k}),
                "card_related": dict(Counter(r["card_related"] for r in rows if r["kind"] == k)),
                "ownership": dict(Counter(r["ownership"] for r in rows if r["kind"] == k))}
               for k, v in kc.most_common()], ("kind", "emails", "templates", "card_related", "ownership"))
    # 2. Leakage: rule labels vs blind truth
    md += ["", "## 2. Label leakage: rule labels against your blind labels", ""]
    labelled = [r for r in rows if r["truth"]]
    if not labelled:
        md += ["Not measured yet: fill in blind_review.csv and run `corpus blind-apply`. Until then every model "
               "number below is measured against rule labels and is optimistic by an unknown amount.", ""]
    else:
        old = json.loads((adir / "labels.json").read_text()) if (adir / "labels.json").exists() else {}
        for dim in ("card_related", "ownership", "kind"):
            per = defaultdict(lambda: [0, 0])
            for r in labelled:
                g = old.get(r["id"], {}).get("label", "?")
                per[g][0] += r[dim] == r["truth"].get(dim)
                per[g][1] += 1
            md += [f"**{dim}**: rule label agrees with yours", ""]
            md += tbl([{"first-pass label": g, "agree": a, "of": n, "rate": f"{a / n:.0%}",
                        "95% range": "%.2f-%.2f" % _wilson(a, n)} for g, (a, n) in sorted(per.items())],
                      ("first-pass label", "agree", "of", "rate", "95% range"))
            md.append("")
    # 3. Models
    calibration, ablation, fitted = {}, {}, {}
    support = Counter(f for r in rows for f in r["x"])
    for which in ("relevance", "ownership"):
        trows = template_rows(rows, which, labels)
        names = sorted(f for f, c in support.items() if c >= 5 and f not in DROP)
        best = None
        for l2 in (1, 3, 10, 30):
            ps, ys, _, _ = oof(trows, names, l2, "bank")
            a = cm.auc(ps, ys)
            if best is None or a > best[0]:
                best = (a, l2)
        l2 = best[1]
        b, w = _fit(trows, names, l2)
        boots = family_bootstrap(trows, names, l2, w)
        res = {}
        for grp in ("bank", "family"):
            ps, ys, kept, signs = oof(trows, names, l2, grp)
            res[grp] = (ps, ys, kept, signs)
        ts = time_split(trows, names, l2)
        md += ["", f"## 3. {which.upper()}: {len(trows)} templates ({sum(t['y'] for t in trows)} positive), l2 = {l2}",
               "", "| validation | AUC | PR-AUC | Brier |", "|---|---|---|---|"]
        for grp, label in (("bank", "leave one bank out"), ("family", "5 folds, campaign families kept whole")):
            ps, ys = res[grp][0], res[grp][1]
            md.append(f"| {label} | {cm.auc(ps, ys):.3f} | {average_precision(ps, ys):.3f} | {brier(ps, ys):.3f} |")
        if ts:
            md.append(f"| new campaigns only (families first seen after 2026-05-20, {ts['templates']} templates) | "
                      f"{ts['auc']} | {ts['ap']} | {ts['brier']} |")
        tl = template_rows(rows, which, labels, key="x_learned")
        names_l = sorted(f for f, c in Counter(f for t in tl for f in t["x"]).items() if c >= 5 and f not in DROP)
        pl, yl, _, _ = oof(tl, names_l, l2, "bank")
        md.append(f"| leave one bank out, with this mailbox's learned boilerplate removed too (study only) | "
                  f"{cm.auc(pl, yl):.3f} | {average_precision(pl, yl):.3f} | {brier(pl, yl):.3f} |")
        if labelled:
            trows_t = template_rows(rows, which, "truth")
            ps = [cm.score(b, w, t["x"]) for t in trows_t]
            ys = [t["y"] for t in trows_t]
            if len(set(ys)) == 2:
                md.append(f"| **your blind labels** ({len(trows_t)} templates; model trained on rule labels) | "
                          f"{cm.auc(ps, ys):.3f} | {average_precision(ps, ys):.3f} | {brier(ps, ys):.3f} |")
        ps, ys, kept, signs = res["bank"]
        # Calibration population: the mail the engine itself reads (the "evidence" sample, taken
        # whole, every email counted), which is all production ever scores. The other samples
        # were drawn to teach the model what non-card mail looks like, not in their natural share.
        p_by_email = _oof_by_email(rows, names, l2, which, labels)
        pop = [(p_by_email[r["id"]], targets(r, which, labels)) for r in rows
               if "evidence" in r["strata"] and targets(r, which, labels) is not None and r["id"] in p_by_email]
        pp, py = [p for p, _ in pop], [y for _, y in pop]
        a_, b_ = platt(pp, py)
        cal = [apply_platt(a_, b_, p) for p in pp]
        calibration[which] = {"a": round(a_, 4), "b": round(b_, 4), "emails": len(pop)}
        md += ["", f"Calibration on the mail the engine reads ({len(pop)} emails, every one counted, each scored by a "
               "model that never saw its bank). Does a score of 0.7 come true 70% of the time?", ""]
        md += ["Raw scores:", ""] + tbl(reliability(pp, py), ("bin", "n", "mean_score", "observed"))
        md += ["", "After Platt scaling:", ""] + tbl(reliability(cal, py), ("bin", "n", "mean_score", "observed"))
        md += ["", f"Brier {brier(pp, py):.3f} raw -> {brier(cal, py):.3f} after Platt scaling (a={a_:.2f}, b={b_:+.2f}). "
               "Fitted and measured on the same emails, so the 'after' numbers are optimistic.",
               "", "After calibration, on the same emails:", ""]
        md += tbl([at(cal, py, t) for t in (0.3, 0.5, 0.7, 0.85, 0.95)], ("t", "precision", "recall", "fdr", "fp", "fn"))
        per_bank = []
        for bk in sorted({t["bank"] for t in kept}):
            sel = [(p, y) for p, y, t in zip(ps, ys, kept) if t["bank"] == bk]
            if len(sel) >= 4:
                bp, by = [p for p, _ in sel], [y for _, y in sel]
                per_bank.append({"bank": bk, "templates": len(sel), "positive": sum(by),
                                 "auc": round(cm.auc(bp, by), 3), **{k: v for k, v in at(bp, by, 0.5).items() if k != "t"}})
        md += ["", "Per bank, at 0.5, each scored by a model that never saw it:", ""]
        md += tbl(per_bank, ("bank", "templates", "positive", "auc", "precision", "recall", "fdr"))
        wrows = []
        for k in names:
            vs = sorted(boots[k])
            lo, hi = vs[int(0.05 * len(vs))], vs[int(0.95 * len(vs)) - 1]
            fold_w = signs.get(k, [])
            same = sum((v > 0) == (w[k] > 0) for v in fold_w) / max(1, len(fold_w))
            pos = sum(1 for t in trows if k in t["x"] and t["y"])
            neg = sum(1 for t in trows if k in t["x"] and not t["y"])
            lo2, hi2 = _wilson(pos, pos + neg)
            wrows.append({"feature": k, "points": f"{w[k]:+.2f}", "family 90% range": f"{lo:+.2f}..{hi:+.2f}",
                          "same sign in bank folds": f"{same:.0%}",
                          "trusted": "yes" if (lo > 0 or hi < 0) and same >= 0.9 else "NO",
                          "templates +/-": f"{pos}/{neg}", "precision 95%": f"{lo2:.2f}-{hi2:.2f}",
                          "banks": len({t['bank'] for t in trows if k in t['x']}),
                          "families": len({t['family'] for t in trows if k in t['x']})})
        wrows.sort(key=lambda r: -abs(float(r["points"])))
        md += ["", f"Weights (intercept {b:+.2f}); trusted = range on whole campaign families stays on one side "
               "of zero and the sign holds in at least 90% of bank folds:", ""]
        md += tbl(wrows, ("feature", "points", "family 90% range", "same sign in bank folds", "trusted",
                          "templates +/-", "precision 95%", "banks", "families"))
        md += ["", "Features the review asked about:", ""]
        md += tbl([r for r in wrows if r["feature"] in WATCH], ("feature", "points", "trusted", "templates +/-",
                                                                 "precision 95%", "banks"))
        # Ablation: does each feature earn its place on banks the model never saw?
        base_auc, base_brier = cm.auc(ps, ys), brier(ps, ys)
        abl = []
        for k in names:
            pk, yk, _, _ = oof(trows, [n for n in names if n != k], l2, "bank")
            abl.append({"feature": k, "AUC without": f"{cm.auc(pk, yk):.3f}",
                        "AUC change": f"{base_auc - cm.auc(pk, yk):+.3f}",
                        "Brier change": f"{brier(pk, yk) - base_brier:+.3f}",
                        "verdict": "helps" if base_auc - cm.auc(pk, yk) > 0.002 or brier(pk, yk) - base_brier > 0.002
                        else "hurts" if base_auc - cm.auc(pk, yk) < -0.002 and brier(pk, yk) - base_brier < 0
                        else "no effect"})
        abl.sort(key=lambda r: -float(r["AUC change"]))
        md += ["", f"Drop one feature at a time (leave one bank out; positive change = the feature helps; "
               f"base AUC {base_auc:.3f}, Brier {base_brier:.3f}):", ""]
        md += tbl(abl, ("feature", "AUC without", "AUC change", "Brier change", "verdict"))
        ablation[which] = {r["feature"]: r["verdict"] for r in abl}
        fitted[which] = {"intercept": round(b, 4), "weights": {k: round(v, 4) for k, v in w.items()}, "l2": l2,
                         "lobo_auc": round(base_auc, 3), "lobo_brier": round(base_brier, 3)}
        if which == "ownership":
            own_model = (b, w, names, l2, trows)
    # 4. Pairs on content only
    md += ["", "## 4. Pairs, on content only (footers and learned boilerplate removed)", "",
           "Kept only when the pair is in at least 3 campaign families and 2 banks, and changes precision by 0.15 or "
           "more against either word alone. Measured on the ownership rule labels.", ""]
    md += _pairs(rows, mails, boiler, _owner_words(account_id))
    # 5. Guards and provenance
    md += ["", "## 5. Identifier guards: every card-shaped number, and why it was kept or dropped", ""]
    gc = Counter()
    lost = []
    for r in rows:
        for n in r["ids"]["numbers"]:
            real = n["digits"] in r["real_l4"]
            gc[(n["guard"], n["reason"], "real card" if real else "not a real card", n["engine"])] += 1
            if n["guard"] == "rejected" and real:
                lost.append((r, n))
    md += tbl([{"study guard": k[0], "reason": k[1], "number is": k[2], "engine today": k[3], "count": v}
               for k, v in sorted(gc.items())], ("study guard", "reason", "number is", "engine today", "count"))
    gaps = [(r, n) for r in rows for n in r["ids"]["numbers"]
            if n["engine"] == "kept" and n["guard"] == "rejected" and n["digits"] not in r["real_l4"]]
    md += ["", f"Numbers the engine keeps today that the study guards reject ({len(gaps)}), one per reason and context:", ""]
    once = {}
    for r, n in gaps:
        once.setdefault((n["reason"], n["context"][-25:]), (r, n))
    md += tbl([{"bank": r["bank"], "digits": n["digits"], "reason": n["reason"], "context": n["context"].replace("|", "/"),
                "engine made card evidence": "YES" if r["engine"]["type"] in _CARD_TYPES else "no"}
               for r, n in list(once.values())[:30]], ("bank", "digits", "reason", "context", "engine made card evidence"))
    md += ["", f"Real card numbers a guard rejected ({len(lost)}), with the words before them:", ""]
    md += tbl([{"bank": r["bank"], "date": r["date"], "digits": n["digits"], "where": n["where"], "reason": n["reason"],
                "context": n["context"].replace("|", "/"),
                "kept elsewhere in the email": "yes" if any(x["digits"] == n["digits"] and x["guard"] == "accepted"
                                                            for x in r["ids"]["numbers"]) else "NO"}
               for r, n in lost[:60]], ("bank", "date", "digits", "where", "reason", "context",
                                        "kept elsewhere in the email"))
    # Engine misses on mail that shows a real card
    miss = Counter()
    for r in rows:
        if r["ownership"] == "established" and r["card_related"] == "specific":
            tr = r["engine"]
            if not (tr["last4s"] or tr["last_digits"] or tr["product"]):
                miss[(tr["path"][0][0] if tr["path"] else "?", r["kind"])] += 1
    md += ["", "Mail that shows a held card but the engine identified nothing, by the engine's decision branch:", ""]
    md += tbl([{"engine branch": k[0], "kind": k[1], "emails": v} for k, v in miss.most_common(20)],
              ("engine branch", "kind", "emails"))
    # 6. Card level
    b, w, names, l2, trows = own_model
    raw = _oof_by_email(rows, names, l2, "ownership", labels)
    cal = calibration["ownership"]
    p_email = {k: apply_platt(cal["a"], cal["b"], v) for k, v in raw.items()}
    groups, fakes = card_groups(rows, truth), false_groups(rows)
    md += ["", "## 6. Card level: event families, then product costs", "",
           "Card candidates are the engine's own (its numbers after its guards). Each email is scored by an "
           "ownership model that never saw its bank, then calibrated. A card's confidence combines its event "
           "families (statement, transaction, payment, dispatch, PIN ...), each family once however many "
           "emails it has; offers, points, OTPs and unknown mail together count as one 'weak' family.", "",
           "Thresholds from costs: wallet when (1-c)·wrong_wallet ≤ min(ask, c·missed); ask when ask ≤ c·missed.", ""]
    settings = [("strict", {"wrong_wallet": 100, "missed": 10, "ask": 1}),
                ("balanced", {"wrong_wallet": 50, "missed": 10, "ask": 2}),
                ("few questions", {"wrong_wallet": 100, "missed": 10, "ask": 5})]
    summary = []
    for name, costs in settings:
        t = em.thresholds(costs)
        real = Counter(decide_events(es, p_email, costs)["decision"] for es in groups.values())
        fake = Counter(decide_events(es, p_email, costs)["decision"] for es in fakes.values())
        summary.append({"costs": f"{name}: wrong {costs['wrong_wallet']}, missed {costs['missed']}, ask {costs['ask']}",
                        "wallet at": t["wallet"], "ask at": t["ask"],
                        f"real cards ({len(groups)})": dict(real), f"non-card numbers ({len(fakes)})": dict(fake)})
    md += tbl(summary, ("costs", "wallet at", "ask at", f"real cards ({len(groups)})", f"non-card numbers ({len(fakes)})"))
    costs = settings[0][1]
    crow = []
    for key, es in sorted(groups.items()):
        d = decide_events(es, p_email, costs)
        crow.append({"card": f"{key[0]} ••{key[1]}", "emails": len(es), "confidence": d["confidence"],
                     "events": ", ".join(f"{k} {v}" for k, v in d["events"].items())[:90],
                     "first proposal": decide_old(es, p_email), f"costs ({settings[0][0]})": d["decision"]})
    md += ["", f"Real cards, costs '{settings[0][0]}':", ""]
    md += tbl(crow, ("card", "emails", "confidence", "events", "first proposal", f"costs ({settings[0][0]})"))
    frow = []
    for k, es in sorted(fakes.items()):
        d = decide_events(es, p_email, costs)
        frow.append({"number": f"{k[0]} ••{k[1]}", "emails": len(es), "confidence": d["confidence"],
                     "events": ", ".join(d["events"])[:60], "first proposal": decide_old(es, p_email),
                     f"costs ({settings[0][0]})": d["decision"]})
    md += ["", "Numbers the engine turns into card evidence that are none of the owner's cards:", ""]
    md += tbl(frow, ("number", "emails", "confidence", "events", "first proposal", f"costs ({settings[0][0]})"))
    md.insert(2, f"**Trained on {'Haiku' if labels == 'haiku' else 'rule'} labels.**\n")
    name = "evaluation_haiku" if labels == "haiku" else "evaluation"
    (adir / f"{name}.md").write_text("\n".join(md))
    (adir / f"{name}.json").write_text(json.dumps({"fitted": fitted, "calibration": calibration,
                                                      "ablation": ablation, "labels": labels if labels != "auto" else ("truth" if labelled else "rules"),
                                                      "templates": len({r['template'] for r in rows}),
                                                      "banks": sorted({r['bank'] for r in rows})}, indent=1))
    return str(adir / f"{name}.md")


def _oof_by_email(rows, names, l2, which="ownership", labels="auto"):
    """Score per email from a model trained on templates of every other bank."""
    trows = template_rows(rows, which, labels)
    out = {}
    for bk in {r["bank"] for r in rows}:
        tr = [t for t in trows if t["bank"] != bk]
        if len({t["y"] for t in tr}) < 2:
            continue
        b, w = _fit(tr, names, l2)
        for r in rows:
            if r["bank"] == bk:
                out[r["id"]] = cm.score(b, w, r["x"])
    return out


DEFAULT_COSTS = {"wrong_wallet": 100, "missed": 10, "ask": 1,
                 "_note": "illustrative until the product owner sets them; see evidence_model.thresholds"}


def export(account_id: int, costs: dict | None = None, labels: str = "auto") -> str:
    """Write carddiscovery/data/evidence_model.json from the last `corpus evaluate` run: weights
    and calibration only (no email text, names or numbers), with where they came from."""
    from datetime import date
    adir = config.HOME / "analysis" / str(account_id)
    ev = json.loads((adir / ("evaluation_haiku.json" if labels == "haiku" else "evaluation.json")).read_text())
    prev = em.load.__wrapped__() if em.MODEL_FILE.exists() else None
    n = int((prev or {}).get("version", "0").split(".")[0]) + 1 if prev else 1
    labels = ev["labels"]
    model = {
        "version": f"{n}.{date.today().isoformat()}",
        "status": "shadow" + {"truth": "", "haiku": ": labels from Claude Haiku, not yet checked by hand"}.get(
            labels, ": labels from rules, not yet checked by hand"),
        "trained_on": {"mailboxes": 1, "templates": ev["templates"], "banks": ev["banks"], "labels": labels},
        "features": sorted(ev["fitted"]["ownership"]["weights"]),
        "relevance": ev["fitted"]["relevance"],
        "ownership": dict(ev["fitted"]["ownership"], calibration=ev["calibration"]["ownership"]),
        "ablation": ev["ablation"],
        "costs": costs or (prev or {}).get("costs") or DEFAULT_COSTS,
    }
    em.MODEL_FILE.write_text(json.dumps(model, indent=1))
    em.load.cache_clear()
    return f"{em.MODEL_FILE} version {model['version']}"


def _owner_words(account_id: int) -> set[str]:
    """The owner's name and mailbox words, kept out of phrases ("nikhil jain" is no card signal)."""
    from . import db
    c = db.connect()
    r = c.execute("SELECT names, mailbox FROM gmail_connections WHERE account_id = %s", (account_id,)).fetchone()
    if not r:
        return set()
    out = {w.lower() for n in (r["names"] or []) for w in re.findall(r"[A-Za-z]+", n)}
    return out | set(re.findall(r"[a-z]+", (r["mailbox"] or "").split("@")[0].lower()))


# Words that say nothing about cards on their own: sign-offs, links, app-store and contact lines.
_FILLER = {"also", "warm", "regards", "warm regards", "note", "view", "download", "app", "check", "know", "know more",
           "click", "call", "support", "use", "above", "before", "available", "access", "ltd", "bank ltd", "terms",
           "conditions", "team", "sincerely", "thank", "thanks", "visit", "help", "details", "below", "link", "here"}


def _pairs(rows, mails, boiler, owner=frozenset()):
    from .corpus import _phrases, _row
    by_id = {m.id: m for m in mails}
    owner = set(owner)
    tp = {}
    for r in rows:
        y = targets(r, "ownership")
        if y is None:
            continue
        m = by_id[r["id"]]
        k = (r["template"], y)
        fs = {f"subject:{p}" for p in _phrases(m.subject, owner)} | {f"preview:{p}" for p in _phrases(m.snippet, owner)} \
            | {f"body:{p}" for p in _phrases(content(m.body or "", boiler)[:2500], owner)}
        fs = {f for f in fs if f.split(":", 1)[1] not in _FILLER}
        if k in tp:
            tp[k]["f"] |= fs
        else:
            tp[k] = {"f": fs, "y": y, "bank": r["bank"], "family": r["family"]}
    single = defaultdict(lambda: [0, 0])
    for t in tp.values():
        for f in t["f"]:
            single[f][t["y"]] += 1
    top = {f for f, (n0, n1) in sorted(single.items(), key=lambda kv: -sum(kv[1]))[:250] if n0 + n1 >= 8}
    pair = defaultdict(lambda: {"y": [0, 0], "banks": set(), "fams": set()})
    for t in tp.values():
        ff = sorted(t["f"] & top)
        for i in range(len(ff)):
            for j in range(i + 1, len(ff)):
                d = pair[(ff[i], ff[j])]
                d["y"][t["y"]] += 1
                d["banks"].add(t["bank"])
                d["fams"].add(t["family"])
    P = sum(t["y"] for t in tp.values())
    N = len(tp) - P
    prec1 = {f: n1 / max(1, n0 + n1) for f, (n0, n1) in single.items()}
    out = []
    for k, d in pair.items():
        n0, n1 = d["y"]
        if n0 + n1 < 10 or len(d["fams"]) < 3 or len(d["banks"]) < 2:
            continue
        prec = n1 / (n0 + n1)
        lift = min(abs(prec - prec1[k[0]]), abs(prec - prec1[k[1]]))
        if lift >= 0.15:
            r = _row(" + ".join(k), n1, n0, P, N)
            out.append(dict(r, changes_by=round(lift, 2), banks=len(d["banks"]), families=len(d["fams"])))
    out.sort(key=lambda r: (-r["changes_by"], -(r["card"] + r["not"])))
    cols = ("feature", "card", "not", "precision", "ci", "changes_by", "banks", "families")
    return ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)] + [
        "| " + " | ".join(str(r[c]) for c in cols) + " |" for r in out[:40]] + ["", f"{len(out)} pairs pass."]
