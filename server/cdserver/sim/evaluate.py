"""Compare a user's cards with the answer file, plus what each job cost."""

from __future__ import annotations

import json
from pathlib import Path

from carddiscovery.registry import Registry

from .. import cards as cards_mod


def evaluate(c, account_id: int, truth: dict, years: int, registry: Registry) -> dict:
    rows = c.execute("SELECT bank_id, last4, product, grp, view FROM cards WHERE account_id = %s AND source = 'gmail'",
                     (account_id,)).fetchall()
    found = {(r["bank_id"], r["last4"]): r for r in rows}
    used: set = set()
    checks = []
    for t in truth["cards"]:
        key = (t["bank"], t["last4"])
        r = found.get(key)
        in_range = t["min_years"] <= years
        if r:
            used.add(key)
        if not in_range:
            ok = r is None
            checks.append({**t, "result": "ok (outside lookback)" if ok else "found though outside lookback",
                           "ok": ok, "got": r and (r["grp"], r["view"].get("status"))})
            continue
        if not r:
            checks.append({**t, "result": "MISSED", "ok": False, "got": None})
            continue
        got_status = r["view"].get("status")
        problems = []
        if t["group"] != "any" and r["grp"] != t["group"]:
            problems.append(f"group {r['grp']} (want {t['group']})")
        if t["status"] != "any" and got_status != t["status"]:
            problems.append(f"status '{got_status}' (want '{t['status']}')")
        if t.get("product") and t["group"] != "any" and r["product"] != t["product"]:
            problems.append(f"product {r['product']} (want {t['product']})")
        checks.append({**t, "result": "ok" if not problems else "; ".join(problems), "ok": not problems,
                       "got": (r["grp"], got_status, r["product"])})

    false_pos = []
    for t in truth["must_not_find"]:
        for key, r in found.items():
            if key[1] == t["last4"] and (t["bank"] in ("*", key[0])):
                used.add(key)
                false_pos.append({**t, "got": (r["grp"], r["view"].get("status")),
                                  "severity": "BAD" if r["grp"] == "owned" else "flagged for review"})
    unexpected = [{"bank": k[0], "last4": k[1], "product": r["product"], "group": r["grp"],
                   "status": r["view"].get("status")} for k, r in found.items() if k not in used]

    apps = cards_mod.applications(c, account_id, registry)
    jobs = c.execute("SELECT kind, status, units_used, attempts, progress->'stats' AS stats, progress->'meter' AS meter, progress->'runs' AS runs, "
                     "round(extract(epoch FROM finished_at - started_at)::numeric, 1) AS secs "
                     "FROM jobs WHERE account_id = %s ORDER BY id", (account_id,)).fetchall()
    in_range = [x for x in checks if x["min_years"] <= years]
    return {
        "years": years,
        "score": {"expected": len(in_range), "correct": sum(x["ok"] for x in in_range),
                  "found": sum(x["got"] is not None for x in in_range),
                  "false_positives_owned": sum(f["severity"] == "BAD" for f in false_pos),
                  "flagged_for_review": sum(f["severity"] != "BAD" for f in false_pos), "unexpected": len(unexpected)},
        "cards": checks, "must_not_find_hits": false_pos, "unexpected": unexpected,
        "applications": {"got": apps, "want": truth["applications"]},
        "jobs": jobs,
    }


def print_report(rep: dict) -> None:
    s = rep["score"]
    print(f"\n=== Card discovery vs answer file (lookback {rep['years']} years) ===")
    print(f"Cards right: {s['correct']}/{s['expected']}   found: {s['found']}/{s['expected']}   "
          f"wrongly owned: {s['false_positives_owned']}   flagged for review: {s['flagged_for_review']}   "
          f"unexpected: {s['unexpected']}\n")
    for x in rep["cards"]:
        mark = "✓" if x["ok"] else "✗"
        print(f"  {mark} {x['bank']:<6} {x['last4']:<5} {x.get('product') or '':<16} want {x['group']}/{x['status']:<18} "
              f"→ {x['result']}")
    if rep["must_not_find_hits"]:
        print("\n  Should not have become cards:")
        for f in rep["must_not_find_hits"]:
            print(f"    {f['severity']}: {f['bank']} {f['last4']} ({f['why']}) → {f['got']}")
    if rep["unexpected"]:
        print("\n  Not in the answer file:")
        for u in rep["unexpected"]:
            print(f"    {u}")
    print(f"\n  Applications: got {rep['applications']['got']}\n                want {rep['applications']['want']}")
    print("\n  Jobs:")
    total_units = 0
    for j in rep["jobs"]:
        st, m = j["stats"] or {}, j["meter"] or {}
        total_units += j["units_used"] or 0
        print(f"    {j['kind']:<15} {j['status']:<9} {j['units_used']:>6} units  {str(j['secs']):>6}s  "
              f"searches {st.get('searches', '-')}  headers {st.get('header_reads', '-')}  full {st.get('full_reads', '-')}"
              f"  first card {st.get('first_card_s', '-')}s  units by kind {m.get('units_by_kind', {})}")
        for n, r in enumerate((j.get("runs") or []), 1):
            print(f"        run {n}: {r.get('wall_s')}s wall, {r.get('cpu_s')}s cpu ({r.get('cpu_pct')}%), memory "
                  f"{r.get('rss_start_mb')}→{r.get('rss_peak_mb')} MB peak (+{r.get('rss_growth_mb')}), {r.get('units')} units")
    print(f"    total {total_units} units\n")


def load_truth(path: Path) -> dict:
    return json.loads(path.read_text())
