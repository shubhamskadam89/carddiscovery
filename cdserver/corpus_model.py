"""Development only: learn how much each kind of signal says about a bank email, from a labelled
study set (cdserver/corpus.py), and check that it holds up on banks and months it never saw.

    CARDDISCOVERY_DEV=1 CD_CORPUS_ACCOUNTS=60 python -m cdserver corpus model --account 60

Built to transfer to other people's mailboxes, not to fit one:
  - Features are generic concepts tagged by where they appear (subject, preview, body, footer,
    sender, attachments, PDF, Gmail tab): "billing words in the subject or preview", "a masked card
    number", "an offers/marketing sender". No bank names, product names or one mailbox's phrases.
  - One row per template (sender + subject pattern), so a mail sent 50 times counts once.
  - Two targets are learned: RELEVANCE (about a card the person has or applied for) and OWNERSHIP
    (shows they hold one). Which card it is stays rule-based (number, ending, product).
  - L2-regularised logistic regression, strength chosen by leave-one-bank-out validation; weights
    are log-odds "points", with bootstrap ranges; a weight whose range crosses zero is not trusted.
  - Checked by leaving each bank out, by a time split, and at card level (where each real card would
    land: wallet, ask, hidden; and whether any false card gets through).
"""

from __future__ import annotations

import json
import math
import random
import re
from collections import Counter, defaultdict
from datetime import datetime

from . import config

# ---------------------------------------------------------------- generic concept features

# Shared with production (shadow scoring), so what is learned here is what runs there.
from carddiscovery.evidence_model import CONCEPTS, SENDERS, concept_features  # noqa: E402,F401


# ---------------------------------------------------------------- targets

RELEVANT = {"transaction", "offer_named", "service", "application"}
IRRELEVANT = {"cross_sell", "account", "notice"}
OWNERSHIP = {"transaction", "offer_named"}


# ---------------------------------------------------------------- a small L2 logistic regression

def fit(X: list[set[str]], y: list[int], names: list[str], l2: float, iters: int = 600, lr: float = 0.5,
        w0: list[float] | None = None) -> tuple[float, dict[str, float]]:
    idx = {n: i for i, n in enumerate(names)}
    rows = [[idx[f] for f in x if f in idx] for x in X]
    w = list(w0 or [0.0] * len(names))
    b = 0.0
    n = len(y)
    for _ in range(iters):
        gw = [0.0] * len(names)
        gb = 0.0
        for r, t in zip(rows, y):
            z = b + sum(w[i] for i in r)
            p = 1 / (1 + math.exp(-max(-30, min(30, z))))
            d = p - t
            gb += d
            for i in r:
                gw[i] += d
        b -= lr * gb / n
        for i in range(len(w)):
            w[i] -= lr * (gw[i] / n + l2 * w[i] / n)
    return b, {nm: w[i] for nm, i in idx.items()}


def score(b: float, w: dict[str, float], x: set[str]) -> float:
    z = b + sum(w.get(f, 0.0) for f in x)
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def auc(ps: list[float], ys: list[int]) -> float:
    pos = [p for p, y in zip(ps, ys) if y]
    neg = [p for p, y in zip(ps, ys) if not y]
    if not pos or not neg:
        return float("nan")
    wins = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def at(ps: list[float], ys: list[int], t: float) -> dict:
    tp = sum(p >= t and y for p, y in zip(ps, ys))
    fp = sum(p >= t and not y for p, y in zip(ps, ys))
    fn = sum(p < t and y for p, y in zip(ps, ys))
    return {"precision": round(tp / max(1, tp + fp), 3), "recall": round(tp / max(1, tp + fn), 3), "fp": fp, "fn": fn}


# ---------------------------------------------------------------- the study

def build(account_id: int) -> tuple[list[dict], list[dict]]:
    """Template rows (for learning) and email rows (for card-level checks)."""
    from carddiscovery.registry import Registry
    from .corpus import Store, template
    reg = Registry.load()
    labels = json.loads((config.HOME / "analysis" / str(account_id) / "labels.json").read_text())
    emails, tpl = [], {}
    for m in Store(account_id).all():
        lab = labels.get(m.id)
        if not lab or lab["intent"] == "unclear":
            continue
        bank = reg.bank_for_sender(m.sender) or (reg.bank_from_text(m.subject + "\n" + m.body)
                                                 if reg.app_for_sender(m.sender) else None) or "?"
        x = concept_features(m, reg)
        row = {"id": m.id, "bank": bank, "date": m.date[:10], "x": x, "intent": lab["intent"],
               "label": lab["label"], "reason": lab["reason"], "template": template(m)}
        emails.append(row)
        key = (row["template"], row["intent"])
        if key not in tpl:
            tpl[key] = dict(row, x=set(x), n=1)
        else:
            tpl[key]["x"] |= x
            tpl[key]["n"] += 1
            tpl[key]["date"] = min(tpl[key]["date"], row["date"])
    return list(tpl.values()), emails


def _xy(rows, target):
    keep = [r for r in rows if r["intent"] in (RELEVANT | IRRELEVANT)]
    if target == "ownership":
        return [r["x"] for r in keep], [int(r["intent"] in OWNERSHIP) for r in keep], keep
    return [r["x"] for r in keep], [int(r["intent"] in RELEVANT) for r in keep], keep


def study(account_id: int, seed: int = 7) -> str:
    rows, emails = build(account_id)
    support = Counter(f for r in rows for f in r["x"])
    names = sorted(f for f, c in support.items() if c >= 5)
    out = {"templates": len(rows), "emails": len(emails), "features": len(names), "targets": {}}
    md = ["# Signal weights learned from the study set", "",
          f"{len(rows)} templates ({len(emails)} emails), {len(names)} generic features with support >= 5.", ""]
    for target in ("relevance", "ownership"):
        X, y, keep = _xy(rows, target)
        banks = sorted({r["bank"] for r in keep})
        # 1. Penalty strength by leave-one-bank-out
        best = None
        for l2 in (1, 3, 10, 30, 100):
            ps, ys = [], []
            for bk in banks:
                tr = [(x, t) for x, t, r in zip(X, y, keep) if r["bank"] != bk]
                te = [(x, t) for x, t, r in zip(X, y, keep) if r["bank"] == bk]
                if not te or len({t for _, t in tr}) < 2:
                    continue
                b, w = fit([x for x, _ in tr], [t for _, t in tr], names, l2)
                ps += [score(b, w, x) for x, _ in te]
                ys += [t for _, t in te]
            a = auc(ps, ys)
            if best is None or a > best[1]:
                best = (l2, a, ps, ys)
        l2, lobo_auc, ps, ys = best
        # 2. Final weights on everything, with bootstrap ranges
        b, w = fit(X, y, names, l2)
        rnd = random.Random(seed)
        boots = defaultdict(list)
        for _ in range(40):
            sample = [rnd.randrange(len(X)) for _ in range(len(X))]
            bb, ww = fit([X[i] for i in sample], [y[i] for i in sample], names, l2, iters=300, w0=list(w.values()))
            for k, v in ww.items():
                boots[k].append(v)
        weights = []
        for k in names:
            vs = sorted(boots[k])
            lo, hi = vs[int(0.05 * len(vs))], vs[int(0.95 * len(vs)) - 1]
            weights.append({"feature": k, "points": round(w[k], 2), "range": f"{lo:+.2f}..{hi:+.2f}",
                            "stable": lo > 0 or hi < 0, "templates": support[k]})
        weights.sort(key=lambda r: -abs(r["points"]))
        # 3. Time split: train on mail before the last 6 months, test on the last 6 months
        cut = "2026-05-20"
        tr = [(x, t) for x, t, r in zip(X, y, keep) if r["date"] < cut]
        te = [(x, t) for x, t, r in zip(X, y, keep) if r["date"] >= cut]
        tb, tw = fit([x for x, _ in tr], [t for _, t in tr], names, l2)
        tps, tys = [score(tb, tw, x) for x, _ in te], [t for _, t in te]
        # 4. Per bank, out of sample
        per_bank = []
        for bk in banks:
            idx = [i for i, r in enumerate(keep) if r["bank"] == bk]
            if len(idx) < 4:
                continue
            tr2 = [(X[i], y[i]) for i in range(len(X)) if keep[i]["bank"] != bk]
            b2, w2 = fit([x for x, _ in tr2], [t for _, t in tr2], names, l2)
            pb = [score(b2, w2, X[i]) for i in idx]
            yb = [y[i] for i in idx]
            per_bank.append({"bank": bk, "templates": len(idx), "positives": sum(yb), "auc": round(auc(pb, yb), 3),
                             **at(pb, yb, 0.5)})
        out["targets"][target] = {"l2": l2, "intercept": round(b, 2), "weights": weights,
                                  "leave_one_bank_out_auc": round(lobo_auc, 3), "time_split_auc": round(auc(tps, tys), 3),
                                  "time_split_templates": len(te), "per_bank": per_bank,
                                  "thresholds": {str(t): at(ps, ys, t) for t in (0.3, 0.5, 0.7, 0.85)}}
        md += [f"## {target.upper()}", "",
               f"Templates: {len(X)} ({sum(y)} positive). Penalty chosen by leave-one-bank-out: l2 = {l2}. "
               f"Intercept {b:+.2f}.", "",
               f"- Leave-one-bank-out AUC: **{lobo_auc:.3f}** (each bank scored by a model that never saw it)",
               f"- Time split AUC (trained before {cut}, tested after, {len(te)} templates): **{auc(tps, tys):.3f}**", "",
               "| threshold | precision | recall | false pos | false neg |", "|---|---|---|---|---|"]
        md += [f"| {t} | {v['precision']} | {v['recall']} | {v['fp']} | {v['fn']} |"
               for t, v in out["targets"][target]["thresholds"].items()]
        md += ["", "| feature | points | 90% range | stable | templates |", "|---|---|---|---|---|"]
        md += [f"| {r['feature']} | {r['points']:+.2f} | {r['range']} | {'yes' if r['stable'] else 'NO'} | "
               f"{r['templates']} |" for r in weights]
        md += ["", "Per bank, scored by a model trained without that bank:", "",
               "| bank | templates | positive | AUC | precision@0.5 | recall@0.5 |", "|---|---|---|---|---|---|"]
        md += [f"| {r['bank']} | {r['templates']} | {r['positives']} | {r['auc']} | {r['precision']} | {r['recall']} |"
               for r in per_bank]
        md.append("")
    adir = config.HOME / "analysis" / str(account_id)
    (adir / "model.json").write_text(json.dumps(out, indent=1, default=str))
    (adir / "model.md").write_text("\n".join(md))
    return str(adir / "model.md")
