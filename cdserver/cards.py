"""Rebuild a user's Gmail cards from saved evidence. Pure: no Gmail calls, so the API
can do it too (after a Yes/No answer) without touching the token."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from carddiscovery.explain import explain
from carddiscovery.registry import Registry
from carddiscovery.resolve import resolve
from carddiscovery.score import group, score_all

from .engine import EvidenceStore

GROUPS = ["owned", "review", "history", "rejected"]


def rebuild(c, account_id: int, registry: Registry) -> dict:
    """Replace the account's gmail cards. Returns {"new": [keys], "apps": [...]}."""
    evs = list(EvidenceStore(c, account_id).known().values())
    cards, apps = resolve(evs)
    score_all(cards, datetime.now(timezone.utc))
    answers = {r["card_key"]: r["decision"] for r in
               c.execute("SELECT card_key, decision FROM decisions WHERE account_id = %s", (account_id,))}
    before = {r["card_key"] for r in
              c.execute("SELECT card_key FROM cards WHERE account_id = %s AND source = 'gmail'", (account_id,))}
    rows = []
    for card in cards:
        card.decision = answers.get(card.decision_key)
        e = explain(card, registry)
        g = group(card)
        view = {"key": card.decision_key, "title": e.title, "status": e.status, "headline": e.headline,
                "evidence": e.evidence, "concerns": e.concerns, "question": e.question,
                "decision": card.decision, "bank": card.bank_id, "last4": card.last4, "product": card.product,
                "group": g, "first_seen": card.first_seen.isoformat() if card.first_seen else None,
                "last_seen": card.last_seen.isoformat() if card.last_seen else None}
        rows.append((account_id, card.decision_key, card.bank_id, card.last4, card.product, g, json.dumps(view)))
    with c.transaction():
        c.execute("DELETE FROM cards WHERE account_id = %s AND source = 'gmail'", (account_id,))
        with c.cursor() as cur:
            cur.executemany("INSERT INTO cards (account_id, source, card_key, bank_id, last4, product, grp, view) "
                            "VALUES (%s, 'gmail', %s, %s, %s, %s, %s, %s)", rows)
    new = [r[1] for r in rows if r[1] not in before and r[5] in ("owned", "review")]
    app_view = [{"bank": registry.banks[a.bank_id].name if a.bank_id in registry.banks else a.bank_id,
                 "product": a.product, "status": a.status} for a in apps]
    return {"new": new, "apps": app_view, "count": len(rows)}


def view(c, account_id: int) -> dict:
    groups: dict[str, list] = {g: [] for g in GROUPS}
    for r in c.execute("SELECT c.view, c.source, c.bank_id, c.last4, "
                       "  (SELECT row_to_json(f) FROM (SELECT credit_limit, reward_points, total_due, statement_date, "
                       "     reconciled FROM statement_facts s WHERE s.account_id = c.account_id "
                       "     AND s.bank_id = c.bank_id AND s.last4 = c.last4 AND s.reconciled IS NOT FALSE "
                       "     ORDER BY statement_date DESC NULLS LAST LIMIT 1) f) AS facts "
                       "FROM cards c WHERE c.account_id = %s ORDER BY c.bank_id, c.last4", (account_id,)):
        v = dict(r["view"])
        v["source"] = r["source"]
        if r["facts"]:
            f = r["facts"]
            bits = []
            if f.get("credit_limit") is not None:
                bits.append(f"limit ₹{float(f['credit_limit']):,.0f}")
            if f.get("reward_points") is not None:
                bits.append(f"{float(f['reward_points']):,.0f} points")
            if f.get("total_due") is not None:
                bits.append(f"due ₹{float(f['total_due']):,.2f}")
            if bits:
                v["evidence"] = list(v.get("evidence") or []) + ["From statement PDF: " + ", ".join(bits)]
        groups.setdefault(v.get("group", "review"), []).append(v)
    return groups


def applications(c, account_id: int, registry: Registry) -> list[dict]:
    evs = list(EvidenceStore(c, account_id).known().values())
    _, apps = resolve(evs)
    return [{"bank": registry.banks[a.bank_id].name if a.bank_id in registry.banks else a.bank_id,
             "product": a.product, "status": a.status} for a in apps]
