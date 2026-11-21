"""Understanding bank email with Claude Haiku: what happened (event), which numbers are which
(credit card, debit card, account, payee, customer id, example), and the card product and its
context. It extracts; it never decides about a card.

Used two ways:
  - the llm_fallback job (cdserver/jobs.py), queued after a scan, for emails the rules call "unknown", in SHADOW mode:
    the answer is stored with the email's evidence and shown in the dev panel, and changes nothing;
  - the study tooling (cdserver/corpus_llm.py), on a consenting owner's stored mail.

What the model is told lives in skills/card-evidence/ (SKILL.md + schema.json), not here.
Only accounts listed in CD_LLM_ACCOUNTS are ever sent, and only when CD_LLM_FALLBACK names a
backend: "cli" (the developer's `claude -p` login, development only) or "api" (Anthropic API
key). Email text goes to Anthropic for these accounts only; nothing is cached across users.
Every number in the answer must appear masked in the email itself, and a product must be one of
the bank's known products, or it is dropped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import config

# ---------------------------------------------------------------- the skill: what the model is told
# skills/card-evidence/SKILL.md (instructions) and schema.json (the answer's fields) are the one place
# to change how emails are read: edit, deploy, and the next call uses it (re-read when the files
# change). Each answer records the skill version (a hash of both files), so a change can be traced
# and rolled back with git. The checks in check() stay in code: a skill edit can't switch them off.
SKILL_DIR = Path(os.environ.get("CD_SKILL_DIR", config.ROOT / "skills" / "card-evidence"))

EMAIL = """<email>
From: {sender}
Subject: {subject}
Preview: {preview}
Body:
{body}
</email>"""

_skill_cache: dict = {}


def skill() -> dict:
    """{"text", "schema", "version", "events", "number_kinds"} from the skill folder, cached until a
    file changes."""
    md, sc = SKILL_DIR / "SKILL.md", SKILL_DIR / "schema.json"
    key = (md.stat().st_mtime_ns, sc.stat().st_mtime_ns)
    if _skill_cache.get("key") != key:
        raw_md, raw_sc = md.read_text(), sc.read_text()
        text = re.sub(r"\A---\n.*?\n---\n", "", raw_md, flags=re.S).strip()  # frontmatter is for Claude Code
        schema = json.loads(raw_sc)
        props = schema["properties"]
        _skill_cache.update(key=key, text=text, schema=schema,
                            version=hashlib.sha1((raw_md + raw_sc).encode()).hexdigest()[:10],
                            events=props["event"]["enum"],
                            number_kinds=props["numbers"]["items"]["properties"]["kind"]["enum"])
    return _skill_cache


def prompt(sender: str, subject: str, preview: str, body: str) -> str:
    """The skill's instructions followed by the email, marked as untrusted data."""
    return skill()["text"] + "\n\n" + EMAIL.format(sender=sender, subject=subject, preview=preview, body=body)


class _Prompt:
    """Back-compatible PROMPT.format(...) for the study tooling."""

    def format(self, **kw) -> str:
        return prompt(**kw)


PROMPT = _Prompt()
SCHEMA = skill()["schema"]
EVENTS = skill()["events"]
NUMBER_KINDS = skill()["number_kinds"]


# ---------------------------------------------------------------- Claude (Anthropic API)
# Same prompt and fields as the local model, for a fair comparison. Development only, and only
# for a mailbox whose owner agreed to their mail going to Anthropic: CD_LLM_EXTERNAL_OK must name
# the account. Credentials come from the environment (ANTHROPIC_API_KEY or an `ant auth login`).
CLAUDE_MODEL = "claude-opus-5-5"


def _strict(schema: dict) -> dict:
    """The schema as structured outputs wants it: no extra keys, nullable written as anyOf."""
    s = dict(schema)
    if s.get("type") == ["string", "null"]:
        return {"anyOf": [{"type": "string"}, {"type": "null"}]}
    if s.get("type") == "object":
        s["properties"] = {k: _strict(v) for k, v in s["properties"].items()}
        s["additionalProperties"] = False
    if s.get("type") == "array":
        s["items"] = _strict(s["items"])
    return s


_client = None


def ask_claude(prompt: str, model: str = CLAUDE_MODEL) -> tuple[dict | None, float, str]:
    import anthropic
    import os
    global _client
    if _client is None:
        # A key in .local/anthropic.key (mode 600, git-ignored) so it never has to be pasted into a chat.
        kf = config.HOME / "anthropic.key"
        key = None if os.environ.get("ANTHROPIC_API_KEY") or not kf.exists() else kf.read_text().strip()
        _client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    t0 = time.monotonic()
    try:
        r = _client.beta.messages.create(
            model=model, max_tokens=4096,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _strict(skill()["schema"])}},
            # On a safety decline, the API re-runs the request on a fallback model it picks.
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            messages=[{"role": "user", "content": prompt}])
    except anthropic.RateLimitError as e:
        return None, time.monotonic() - t0, f"rate limited: {e.message[:60]}"
    except anthropic.APIStatusError as e:
        return None, time.monotonic() - t0, f"API {e.status_code}: {e.message[:80]}"
    except anthropic.APIConnectionError:
        return None, time.monotonic() - t0, "network error"
    secs = time.monotonic() - t0
    if r.stop_reason == "refusal":
        return None, secs, f"refusal: {getattr(r.stop_details, 'category', None)}"
    text = next((b.text for b in r.content if b.type == "text"), "")
    try:
        return validate(json.loads(text)), secs, ""
    except Exception as e:
        return None, secs, f"{type(e).__name__}: {str(e)[:80]}"


def ask_claude_cli(prompt: str, model: str = "opus") -> tuple[dict | None, float, str]:
    """The same request through the Claude Code CLI (`claude -p`) on the developer's own login: no
    tools, no MCP servers, no settings, and a one-line system prompt instead of Claude Code's own,
    so a call is ~2.7k tokens. For development tests only; production would call the API."""
    import subprocess
    t0 = time.monotonic()
    cmd = ["claude", "-p", "--output-format", "json", "--model", model, "--effort", "low",
           "--json-schema", json.dumps(_strict(skill()["schema"])), "--tools", "", "--no-session-persistence",
           "--system-prompt", "You extract structured facts from bank emails. Answer only in the requested JSON.",
           "--strict-mcp-config", "--setting-sources", ""]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=240, cwd="/tmp")
        out = json.loads(r.stdout)
    except Exception as e:
        return None, time.monotonic() - t0, f"cli: {type(e).__name__}: {str(e)[:80]}"
    secs = time.monotonic() - t0
    if out.get("is_error") or not out.get("structured_output"):
        return None, secs, f"cli: {str(out.get('result') or out.get('subtype'))[:80]}"
    try:
        return validate(out["structured_output"]), secs, ""
    except Exception as e:
        return None, secs, f"{type(e).__name__}: {str(e)[:80]}"


def validate(d: dict) -> dict:
    sk = skill()
    if d.get("event") not in sk["events"]:
        raise ValueError(f"event {d.get('event')!r}")
    nums = []
    for n in d.get("numbers") or []:
        digits = re.sub(r"\D", "", str(n.get("digits", "")))[-4:]
        if len(digits) >= 2 and n.get("kind") in sk["number_kinds"]:
            nums.append({"digits": digits, "kind": n["kind"], "quote": str(n.get("quote", ""))[:80]})
    d["numbers"] = nums
    return d




# ---------------------------------------------------------------- the scan's fallback (shadow)

HAIKU = "claude-haiku-5-5"
BACKEND = os.environ.get("CD_LLM_FALLBACK", "off")           # off | cli | api
ACCOUNTS = {a.strip() for a in os.environ.get("CD_LLM_ACCOUNTS", "").split(",") if a.strip()}
WORKERS = 6
# How strongly each answer shows a card is held: the shadow model's p_own for these emails.
OWN_P = {"STRONG": 0.95, "MODERATE": 0.8, "WEAK": 0.5, "NONE": 0.05, "UNKNOWN": 0.3}
# Haiku's event -> the evidence model's event families (STRONG ones count toward the wallet).
EVENT_FAMILY = {"DISPATCH_OR_DELIVERY": "dispatch", "ACTIVATION": "activation", "PIN": "pin",
                "STATEMENT": "statement", "PAYMENT_DUE": "statement", "PAYMENT_RECEIVED": "payment",
                "TRANSACTION": "transaction", "LIMIT_CHANGE": "lifecycle", "RENEWAL_OR_FEE": "renewal",
                "BLOCK_OR_REPLACE": "block_replace", "CLOSURE": "closure", "APPLICATION": "application",
                "APPROVAL": "application", "REJECTION": "application"}


def enabled(account_id: int) -> bool:
    return BACKEND in ("cli", "api") and str(account_id) in ACCOUNTS


def eligible(ev) -> bool:
    """Emails the rules couldn't type, from a known bank (or a bill-payment app that named one)."""
    from carddiscovery.models import EvidenceType as T
    return ev.type == T.UNKNOWN and bool(ev.bank_id)


def prompt_for(msg) -> str:
    """The prompt for one email: footers dropped, and any full card number masked before it leaves."""
    from carddiscovery import classify as cl, extract as ex
    body = "\n".join(u for u in cl._units(msg.body or "") if not cl.BOILERPLATE.search(u))
    subject, _ = ex.redact_pans(msg.subject or "")
    preview, _ = ex.redact_pans(" ".join((msg.snippet or "").split()))
    body, _ = ex.redact_pans(body[:3500])
    return prompt(sender=(re.search(r"[\w.+-]+@[\w.-]+", msg.sender or "") or [""])[0],
                  subject=subject, preview=preview, body=body)


# A number counts only if the email shows it masked or after "ending"/"no." - never a bare amount,
# date or reference that the model mistook for a card.
def _shown(text: str, digits: str) -> bool:
    d = re.escape(digits)
    return bool(re.search(rf"(?:[xX*•●#]{{2,}}[\s-]?|\bend(?:ing|s)\s*(?:with|in)?\s*[xX*•]*\s*|"
                          rf"\b(?:card|a/c|account)\s*(?:no\.?|number)?\s*[:\-]?\s*[xX*•]+\s?){d}(?!\d)", text, re.I))


def check(ans: dict, msg, reg, bank: str | None) -> dict:
    """Haiku's answer, verified against the email and the bank's catalogue, as shadow evidence."""
    text = msg.text()
    numbers, dropped = [], 0
    for n in ans["numbers"]:
        if _shown(text, n["digits"]):
            numbers.append([n["digits"], n["kind"]])
        else:
            dropped += 1
    product = None
    if ans.get("product") and bank:
        found = reg.products_in(bank, re.sub(r"\s*credit\s*card\s*$", "", ans["product"], flags=re.I) + " credit card")
        product = found[0] if len(found) == 1 else None
    cards = [d for d, k in numbers if k == "CREDIT_CARD"]
    return {"model": HAIKU, "skill": skill()["version"], "event": ans["event"], "about": ans["about_credit_card"],
            "ownership": ans["ownership_evidence"], "numbers": numbers, "cards": cards,
            "product": product, "product_context": ans["product_context"], "dropped": dropped,
            # the same shape the evidence model combines at card level
            "family": EVENT_FAMILY.get(ans["event"], "weak"), "p_own": OWN_P[ans["ownership_evidence"]]}


def fallback(pairs, reg, log=None) -> int:
    """Ask Haiku about each (msg, ev) pair, WORKERS at a time; attach the checked answer to
    ev.shadow["llm"]. Never raises: a failed call leaves the email as the rules typed it."""
    from concurrent.futures import ThreadPoolExecutor

    def one(pair):
        msg, ev = pair
        try:
            p = prompt_for(msg)
            ans, secs, err = ask_claude_cli(p, "haiku") if BACKEND == "cli" else ask_claude(p, HAIKU)
            return check(ans, msg, reg, ev.bank_id) if ans else {"model": HAIKU, "error": err[:80]}
        except Exception as e:
            return {"model": HAIKU, "error": f"{type(e).__name__}: {str(e)[:60]}"}

    done = 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for (msg, ev), res in zip(pairs, pool.map(one, pairs)):
            ev.shadow = dict(ev.shadow or {}, llm=res)
            done += "error" not in res
    if log:
        log(f"haiku fallback: {done} of {len(pairs)} answered")
    return done
