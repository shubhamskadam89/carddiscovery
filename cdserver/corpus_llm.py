"""Development only: does a local LLM understand bank email better than the rules?

    CARDDISCOVERY_DEV=1 CD_CORPUS_ACCOUNTS=60 python -m cdserver corpus llm --account 60 --truth t.json

Runs a local model through Ollama (http://127.0.0.1:11434, nothing leaves this machine) on the
study set's emails and compares it with the rule engine and the shadow model:
  unknown   every email the engine typed "unknown": where an LLM fallback would act
  guards    emails with numbers that aren't cards (payees, debit cards, NPS, accounts, CRNs)
  control   a few of each kind the rules are sure about, to see whether the LLM agrees

The LLM only extracts what the email says (event, card numbers and what they are, product and
its context). It never decides about a card: in the product, the rules and evidence model would.
Email text is marked as untrusted data in the prompt. Results are cached per email under
.local/analysis/<account>/llm_results.json, so a stopped run resumes.
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.request
from collections import Counter, defaultdict

from . import config

OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen3:8b"

from .understanding import (  # noqa: E402  shared with the scan's Haiku fallback
    CLAUDE_MODEL, EVENTS, NUMBER_KINDS, PROMPT, SCHEMA, _strict, ask_claude, ask_claude_cli, validate)

def ask(prompt: str, model: str = MODEL, timeout: int = 180) -> tuple[dict | None, float, str]:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False,
                       "think": False, "format": SCHEMA,
                       "options": {"temperature": 0, "num_ctx": 8192, "seed": 7}}).encode()
    req = urllib.request.Request(OLLAMA, body, {"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    secs = time.monotonic() - t0
    text = out.get("message", {}).get("content", "")
    try:
        return validate(json.loads(text)), secs, ""
    except Exception as e:  # malformed output is a result too: counted, not retried
        return None, secs, f"{type(e).__name__}: {str(e)[:80]}"


# ---------------------------------------------------------------- the study

def pick(rows, seed: int = 5) -> dict[str, list]:
    rnd = random.Random(seed)
    unknown = [r for r in rows if r["engine"]["type"] == "unknown"]
    guard_words = re.compile(r"paid\s+to|debit\s+card|nps|a/c|account\s+(?:no|number)|crn|customer\s+id", re.I)
    guards = [r for r in rows if any(n["guard"] == "rejected" for n in r["ids"]["numbers"])
              or (r["ids"]["numbers"] and guard_words.search(" ".join(n["context"] for n in r["ids"]["numbers"])))]
    rnd.shuffle(guards)
    by_kind = defaultdict(list)
    for r in rows:
        if r["engine"]["type"] != "unknown":
            by_kind[r["kind"]].append(r)
    control = []
    for k in sorted(by_kind):
        rnd.shuffle(by_kind[k])
        control += by_kind[k][:3]
    seen = set()
    out = {}
    for name, lst, cap in (("unknown", unknown, 80), ("guards", guards, 25), ("control", control, 60)):
        keep = []
        for r in lst:
            if r["id"] not in seen and len(keep) < cap:
                seen.add(r["id"])
                keep.append(r)
        out[name] = keep
    return out


def run(account_id: int, truth: dict, provider: str = "ollama", log=print) -> str:
    import os
    from .corpus_eval import build, content
    model = {"claude": CLAUDE_MODEL, "claude-cli": CLAUDE_MODEL, "claude-cli-haiku": "claude-haiku-5-5"}.get(provider, MODEL)
    if provider.startswith("claude") and str(account_id) not in os.environ.get("CD_LLM_EXTERNAL_OK", "").split(","):
        raise SystemExit(f"sending account {account_id}'s mail to Anthropic needs the owner's agreement: "
                         f"set CD_LLM_EXTERNAL_OK={account_id} once they have said yes")
    adir = config.HOME / "analysis" / str(account_id)
    cache_f = adir / ("llm_results.json" if provider == "ollama" else f"llm_results_{provider}.json")
    cache = json.loads(cache_f.read_text()) if cache_f.exists() else {}
    rows, boiler, _, mails = build(account_id, truth)
    by_id = {m.id: m for m in mails}
    sets = pick(rows)
    todo = [(name, r) for name, lst in sets.items() for r in lst]
    log(f"{len(todo)} emails: " + ", ".join(f"{k} {len(v)}" for k, v in sets.items()))
    def one(r):
        m = by_id[r["id"]]
        prompt = PROMPT.format(sender=(re.search(r"[\w.+-]+@[\w.-]+", m.sender or "") or [""])[0],
                               subject=m.subject, preview=" ".join((m.snippet or "").split()),
                               body=content(m.body or "", boiler)[:3500])
        try:
            if provider == "claude":
                return ask_claude(prompt, model)
            if provider == "claude-cli":
                return ask_claude_cli(prompt)
            if provider == "claude-cli-haiku":
                return ask_claude_cli(prompt, "haiku")
            return ask(prompt, model)
        except Exception as e:
            return None, 0.0, f"{type(e).__name__}: {e}"

    from concurrent.futures import ThreadPoolExecutor
    pending = [r for _, r in todo if f"{model}|{r['id']}" not in cache]
    workers = 1 if provider == "ollama" else 4  # one local model at a time; the API takes a few in parallel
    with ThreadPoolExecutor(workers) as pool:
        for i, (r, (res, secs, err)) in enumerate(zip(pending, pool.map(one, pending)), 1):
            cache[f"{model}|{r['id']}"] = {"result": res, "secs": round(secs, 2), "error": err}
            if i % 5 == 0 or i == len(pending):
                cache_f.write_text(json.dumps(cache, indent=0))
                log(f"  {i}/{len(pending)}  last {secs:.1f}s {err[:40]}")
    cache_f.write_text(json.dumps(cache, indent=0))
    return report(account_id, rows, sets, cache, model)


# ---------------------------------------------------------------- relabelling the whole study set

# Haiku's answer as the study's labels. MODERATE (service mail clearly about their own card) shows the
# card is held; WEAK (an offer that names their card) is left out of the ownership target, as unknown.
REL_OF = {"SPECIFIC_CARD": "specific", "CREDIT_CARDS_GENERAL": "generic", "NOT_CREDIT_CARD": "unrelated",
          "UNKNOWN": "ambiguous"}
OWN_OF = {"STRONG": "established", "MODERATE": "established", "WEAK": "unknown", "NONE": "not_established",
          "UNKNOWN": "unknown"}


def relabel(account_id: int, truth: dict, provider: str = "claude-cli-haiku", workers: int = 6,
            review_agree: float = 0.1, seed: int = 3, log=print) -> str:
    """Haiku reads one email of every template (repeats of a template share its answer) and its answer
    becomes a second, independent set of labels next to the rules'. Nothing replaces the rules' labels:
    the disagreements, plus a random tenth of the agreements, go to blind_review.csv for the owner,
    with no label shown, and the owner's answers are the truth both are measured against."""
    import csv
    import os
    from concurrent.futures import ThreadPoolExecutor
    from .corpus_eval import build, content
    if str(account_id) not in os.environ.get("CD_LLM_EXTERNAL_OK", "").split(","):
        raise SystemExit(f"set CD_LLM_EXTERNAL_OK={account_id} once the owner agreed to their mail going to Anthropic")
    model = {"claude-cli-haiku": "claude-haiku-5-5", "claude-cli": CLAUDE_MODEL}[provider]
    adir = config.HOME / "analysis" / str(account_id)
    cache_f = adir / f"llm_results_{provider}.json"
    cache = json.loads(cache_f.read_text()) if cache_f.exists() else {}
    rows, boiler, _, mails = build(account_id, truth)
    by_id = {m.id: m for m in mails}
    rep = {}
    for r in sorted(rows, key=lambda r: r["date"], reverse=True):  # newest email of each template
        rep.setdefault(r["template"], r)
    # a template already answered through another of its emails counts as done
    done_tpl = {r["template"] for r in rows if f"{model}|{r['id']}" in cache and cache[f"{model}|{r['id']}"]["result"]}
    todo = [r for t, r in rep.items() if t not in done_tpl]
    log(f"{len(rep)} templates, {len(rep) - len(todo)} already answered, {len(todo)} to ask")

    def one(r):
        m = by_id[r["id"]]
        prompt = PROMPT.format(sender=(re.search(r"[\w.+-]+@[\w.-]+", m.sender or "") or [""])[0],
                               subject=m.subject, preview=" ".join((m.snippet or "").split()),
                               body=content(m.body or "", boiler)[:3500])
        return ask_claude_cli(prompt, "haiku" if provider == "claude-cli-haiku" else "opus")

    with ThreadPoolExecutor(workers) as pool:
        for i, (r, (res, secs, err)) in enumerate(zip(todo, pool.map(one, todo)), 1):
            cache[f"{model}|{r['id']}"] = {"result": res, "secs": round(secs, 2), "error": err}
            if i % 20 == 0 or i == len(todo):
                cache_f.write_text(json.dumps(cache, indent=0))
                log(f"  {i}/{len(todo)}  last {secs:.1f}s {err[:40]}")
    # the template's answer -> every email of the template
    ans = {}
    for r in rows:
        c = cache.get(f"{model}|{r['id']}")
        if c and c["result"]:
            ans.setdefault(r["template"], c["result"])
    labels = {}
    for r in rows:
        a = ans.get(r["template"])
        if a:
            labels[r["id"]] = {"event": a["event"], "card_related": REL_OF[a["about_credit_card"]],
                               "ownership": OWN_OF[a["ownership_evidence"]], "product": a["product"],
                               "product_context": a["product_context"],
                               "numbers": [[n["digits"], n["kind"]] for n in a["numbers"]], "template": r["template"]}
    (adir / "haiku_labels.json").write_text(json.dumps(labels, indent=0))
    # Review sheet: every template where Haiku and the rules disagree, plus a random tenth of the rest
    rnd = random.Random(seed)
    dis, agree = [], []
    for t, r in rep.items():
        h = labels.get(r["id"])
        if not h:
            continue
        # Only hard conflicts go to the owner: held vs not held, or a specific card vs not about cards.
        # Shades (generic vs specific, a definite answer vs unknown) are left to Haiku's answer.
        differ = ({h["ownership"], r["ownership"]} == {"established", "not_established"}
                  or {h["card_related"], r["card_related"]} == {"specific", "unrelated"})
        (dis if differ else agree).append(r)
    rnd.shuffle(agree)
    pick = dis + agree[:max(1, int(len(agree) * review_agree))]
    rnd.shuffle(pick)  # disagreements and agreements mixed, so neither is recognisable
    sheet = adir / "blind_review.csv"
    if sheet.exists():
        sheet.rename(adir / "blind_review_previous.csv")
    from .corpus_eval import KINDS
    with open(sheet, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["n", "id", "bank", "date", "sender", "subject", "preview", "content (footers removed)",
                    "kind: " + " / ".join(KINDS), "card_related: specific / generic / unrelated / unsure",
                    "ownership: established / not_established / unknown",
                    "identifies: both / number / product / issuer / none", "notes"])
        for i, r in enumerate(pick, 1):
            m = by_id[r["id"]]
            w.writerow([i, m.id, r["bank"], m.date[:10], m.sender, m.subject[:140], " ".join(m.snippet.split())[:200],
                        content(m.body or "", boiler)[:600], "", "", "", "", ""])
    rel = Counter((r["card_related"], labels[r["id"]]["card_related"]) for r in rep.values() if r["id"] in labels)
    own = Counter((r["ownership"], labels[r["id"]]["ownership"]) for r in rep.values() if r["id"] in labels)
    errs = Counter(v["error"][:40] for v in cache.values() if v["error"])
    md = ["# Haiku relabelling against the rule labels", "",
          f"{len(ans)} of {len(rep)} templates answered; {len(labels)} of {len(rows)} emails labelled. "
          f"Errors: {dict(errs) or 'none'}.", "",
          f"Review sheet: {len(dis)} disagreements + {len(pick) - len(dis)} random agreements = {len(pick)} emails, "
          "mixed, no labels shown.", "", "## About a credit card (templates): rules → Haiku", "",
          "| rules | haiku | templates |", "|---|---|---|"]
    md += [f"| {a} | {b} | {n} |" for (a, b), n in sorted(rel.items())]
    md += ["", "## Ownership (templates): rules → Haiku", "", "| rules | haiku | templates |", "|---|---|---|"]
    md += [f"| {a} | {b} | {n} |" for (a, b), n in sorted(own.items())]
    (adir / "relabel.md").write_text("\n".join(md))
    return f"{adir / 'relabel.md'}; review sheet {sheet} ({len(pick)} emails)"


# What the rules' kinds and the engine's types mean in the LLM's event words (for agreement).
KIND_TO_EVENT = {
    "statement": {"STATEMENT", "PAYMENT_DUE"}, "payment_reminder": {"PAYMENT_DUE", "STATEMENT"},
    "payment_confirmation": {"PAYMENT_RECEIVED"}, "transaction": {"TRANSACTION"},
    "limit_update": {"LIMIT_CHANGE", "OFFER_ON_HELD_CARD"}, "application": {"APPLICATION", "APPROVAL", "REJECTION"},
    "card_lifecycle": {"DISPATCH_OR_DELIVERY", "ACTIVATION", "PIN", "RENEWAL_OR_FEE", "BLOCK_OR_REPLACE", "CLOSURE",
                       "SERVICE_NOTICE"},
    "card_security": {"SECURITY_NOTICE"}, "notice_generic": {"SERVICE_NOTICE", "SECURITY_NOTICE", "OTHER"},
    "card_cross_sell": {"CROSS_SELL"}, "offer_named": {"OFFER_ON_HELD_CARD", "CROSS_SELL"},
    "card_linked_loan": {"CROSS_SELL", "OFFER_ON_HELD_CARD"}, "reward_notification": {"REWARD"},
    "account": {"BANK_ACCOUNT"},
}
STRONG_EVENTS = {"DISPATCH_OR_DELIVERY", "ACTIVATION", "PIN", "STATEMENT", "PAYMENT_DUE", "PAYMENT_RECEIVED",
                 "TRANSACTION", "LIMIT_CHANGE", "RENEWAL_OR_FEE", "BLOCK_OR_REPLACE", "CLOSURE"}


def report(account_id, rows, sets, cache, model) -> str:
    adir = config.HOME / "analysis" / str(account_id)
    md = [f"# Local LLM ({model}) against the rules", "",
          "Ollama on this Mac, temperature 0, thinking off, output forced to a JSON schema. The LLM only "
          "extracts; nothing here changes the engine.", ""]
    res_of = {r["id"]: cache.get(f"{model}|{r['id']}", {}) for lst in sets.values() for r in lst}
    all_r = [r for lst in sets.values() for r in lst]
    ok = [r for r in all_r if res_of[r["id"]].get("result")]
    secs = sorted(res_of[r["id"]]["secs"] for r in all_r if res_of[r["id"]].get("secs"))
    md += [f"- Emails: {len(all_r)}; valid JSON: {len(ok)} ({len(ok) / max(1, len(all_r)):.0%})",
           f"- Time per email: median {secs[len(secs) // 2] if secs else 0:.1f}s, slowest {secs[-1] if secs else 0:.1f}s, "
           f"total {sum(secs) / 60:.1f} min", ""]

    def tbl(rs, cols):
        return ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)] + [
            "| " + " | ".join(str(r.get(c, "")).replace("|", "/") for c in cols) + " |" for r in rs]

    # Numbers: which the LLM calls credit cards, against the owner's real cards and the engine
    num = Counter()
    false_cc, missed_cc = [], []
    for r in ok:
        res = res_of[r["id"]]["result"]
        real = r["real_l4"]
        llm_cc = {n["digits"] for n in res["numbers"] if n["kind"] == "CREDIT_CARD"}
        eng = set(r["engine"]["last4s"])
        for d in llm_cc:
            good = d in real or any(x.endswith(d) for x in real)
            num[("LLM: credit card", "real card" if good else "NOT a real card")] += 1
            if not good:
                false_cc.append((r, d, next(n["quote"] for n in res["numbers"] if n["digits"] == d)))
        for d in eng:
            num[("engine: card number", "real card" if d in real else "NOT a real card")] += 1
        for d in (eng & real) - llm_cc:
            if not any(x.endswith(d) or d.endswith(x) for x in llm_cc):
                missed_cc.append((r, d))
    md += ["## Card numbers", ""]
    md += tbl([{"who": k[0], "number is": k[1], "count": v} for k, v in sorted(num.items())], ("who", "number is", "count"))
    md += ["", f"Numbers the LLM called the person's credit card that are none of their cards ({len(false_cc)}):", ""]
    md += tbl([{"set": _set_of(sets, r), "bank": r["bank"], "digits": d, "quote": q, "subject": r["subject"][:60]}
               for r, d, q in false_cc[:40]], ("set", "bank", "digits", "quote", "subject"))
    md += ["", f"Real card numbers the engine found and the LLM didn't call a credit card ({len(missed_cc)}):", ""]
    md += tbl([{"set": _set_of(sets, r), "bank": r["bank"], "digits": d, "llm said": ", ".join(
        f"{n['digits']} {n['kind']}" for n in res_of[r['id']]['result']['numbers'])[:60], "subject": r["subject"][:60]}
               for r, d in missed_cc[:40]], ("set", "bank", "digits", "llm said", "subject"))

    # Events: agreement with the rules' kind on the control set
    md += ["", "## Event type against the rules (control set: emails the rules are sure about)", ""]
    agree = Counter()
    dis = []
    for r in sets["control"]:
        res = res_of[r["id"]].get("result")
        if not res or r["kind"] not in KIND_TO_EVENT:
            continue
        a = res["event"] in KIND_TO_EVENT[r["kind"]]
        agree[(r["kind"], a)] += 1
        if not a:
            dis.append({"rules": r["kind"], "llm": res["event"], "subject": r["subject"][:70], "llm reason": res["reason"][:90]})
    kinds = sorted({k for k, _ in agree})
    md += tbl([{"rules kind": k, "agree": agree[(k, True)], "of": agree[(k, True)] + agree[(k, False)]} for k in kinds],
              ("rules kind", "agree", "of"))
    md += ["", "Where they differ (read these: either side can be wrong):", ""]
    md += tbl(dis[:40], ("rules", "llm", "subject", "llm reason"))

    # Unknown set: would the LLM rescue anything?
    md += ["", "## Emails the engine called 'unknown': what the LLM makes of them", ""]
    ev = Counter()
    rescue = []
    for r in sets["unknown"]:
        res = res_of[r["id"]].get("result")
        if not res:
            ev[("(no valid answer)", "")] += 1
            continue
        ev[(res["event"], res["about_credit_card"])] += 1
        cc = {n["digits"] for n in res["numbers"] if n["kind"] == "CREDIT_CARD"}
        if res["event"] in STRONG_EVENTS and res["about_credit_card"] == "SPECIFIC_CARD":
            real = r["real_l4"]
            rescue.append({"bank": r["bank"], "llm event": res["event"], "llm numbers": ",".join(sorted(cc)) or "-",
                           "real card?": "yes" if cc & real or any(x.endswith(d) for d in cc for x in real) else
                           ("no number" if not cc else "NO"),
                           "subject": r["subject"][:60], "reason": res["reason"][:80]})
    md += tbl([{"llm event": k[0], "about credit card": k[1], "emails": v} for k, v in ev.most_common()],
              ("llm event", "about credit card", "emails"))
    md += ["", f"Unknown emails the LLM calls strong evidence about a specific card ({len(rescue)}):", ""]
    md += tbl(rescue, ("bank", "llm event", "llm numbers", "real card?", "subject", "reason"))

    # Guard set: does the LLM tell payees, debit cards and accounts apart?
    md += ["", "## Numbers that aren't credit cards (guard set): what the LLM calls them", ""]
    g = Counter()
    for r in sets["guards"]:
        res = res_of[r["id"]].get("result")
        if not res:
            continue
        rej = {n["digits"]: n["reason"] for n in r["ids"]["numbers"] if n["guard"] == "rejected"}
        for n in res["numbers"]:
            if n["digits"] in rej:
                g[(rej[n["digits"]], n["kind"])] += 1
    md += tbl([{"rules say": k[0], "llm says": k[1], "count": v} for k, v in sorted(g.items())],
              ("rules say", "llm says", "count"))
    errs = Counter(res_of[r["id"]].get("error") for r in all_r if res_of[r["id"]].get("error"))
    if errs:
        md += ["", "Errors: " + "; ".join(f"{k} ×{v}" for k, v in errs.items())]
    out = adir / f"llm_{re.sub(r'[^a-z0-9.]+', '_', model.lower())}.md"
    out.write_text("\n".join(md))
    return str(out)


def _set_of(sets, r):
    return next(k for k, v in sets.items() if any(x["id"] == r["id"] for x in v))
