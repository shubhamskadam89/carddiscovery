"""Forged, spoofed and phishing mail for the last 12 months: 1 in 20 of that year's emails.

None of it may become a card, and none of it may change a real card's status (a forged
"your card is closed" must not close it). About a quarter is filed in Spam, as Gmail
does with much phishing; the rest reaches the inbox.

Kinds:
  spoof      From the real bank domain, but Gmail's check failed (DKIM, SPF and DMARC fail)
  attack     spoofed lifecycle mail naming the person's REAL cards (closure, upgrade, block)
  statement  spoofed statements for fake cards, in the person's own name
  lookalike  look-alike domains (hdfcbank-alerts.com) that pass the check for their own domain
  display    "HDFC Bank <...@gmail.com>": a bank name on a personal address
  relay      signed by a bulk-mail service, but DMARC for the bank's domain failed
"""

from __future__ import annotations

import random
from datetime import timedelta

from .generate import BANKS, PERSONA, TODAY, Gen, Mail

FAKE_NUMBERS = ["8812", "4417", "3090", "6021", "7354", "1199", "5508", "2468", "9137", "6640"]
REAL_TARGETS = [("hdfc", "2846", "Millennia"), ("hdfc", "7777", "Regalia Gold"), ("icici", "2222", "Amazon Pay"),
                ("yes", "4321", "Marquee"), ("axis", "6190", "Magnus")]
LOOKALIKES = {"hdfc": "hdfcbank-alerts.com", "icici": "icicibank-secure.in", "sbi": "sbicard-rewards.com",
              "axis": "axisbank-kyc.co", "kotak": "kotak-cards.info"}


def _fail(dom: str) -> str:
    return f"fail:{dom}"


def _relay(bank_dom: str) -> str:
    return (f"mx.google.com; dkim=pass header.i=@sendgrid.net header.s=s1; spf=pass smtp.mailfrom=bounce.sendgrid.net; "
            f"dmarc=fail (p=REJECT) header.from={bank_dom}")


def build_forged(to: str, n_last_year: int, seed: int = 99) -> tuple[list[Mail], list[dict]]:
    g = Gen(to, seed)
    r: random.Random = g.r
    total = round(n_last_year / 19)  # 1 in 20 of the year's mail, counting these
    kinds = (["spoof"] * 35 + ["attack"] * 15 + ["statement"] * 15 + ["lookalike"] * 15 + ["display"] * 10
             + ["relay"] * 10)
    must_not_find: dict[str, dict] = {}
    name = PERSONA["name"]
    for i in range(total):
        kind = kinds[i % len(kinds)] if i < len(kinds) else r.choice(kinds)
        d = TODAY - timedelta(days=r.randint(0, 364))
        bid = r.choice(["hdfc", "icici", "sbi", "axis", "kotak"])
        b = BANKS[bid]
        dom = b.alerts.split("@")[1]
        fake = r.choice(FAKE_NUMBERS)
        labels = ["SPAM"] if r.random() < 0.25 else ["INBOX"]
        if kind == "spoof":
            subj, text = r.choice([
                (f"Your {b.name} Credit Card XX{fake} has been blocked",
                 f"Dear Customer, your {b.name} Credit Card XX{fake} has been temporarily blocked due to incomplete KYC. "
                 f"Verify within 24 hours to avoid permanent closure: http://verify-{bid}.example.invalid"),
                (f"{r.randint(5, 40)},000 reward points expiring on your card ending {fake}",
                 f"Dear Customer, reward points on your {b.name} Credit Card ending {fake} expire tonight. "
                 f"Redeem now at http://redeem-{bid}.example.invalid"),
                (f"Transaction alert: Rs. {g.amount(9000, 90000)} spent on your {b.name} Credit Card",
                 f"Rs. {g.amount(9000, 90000)} was spent on your {b.name} Credit Card XX{fake}. Not you? Call 080-0000-0000 "
                 f"to block it.")])
            g.bank_mail("forged-spoof", b, b.alerts, d, subj, text, auth=_fail(dom), labels=labels)
            must_not_find[fake] = {"bank": "*", "last4": fake, "why": "forged bank mail (DMARC fail)"}
        elif kind == "attack":
            tb, l4, prod = r.choice(REAL_TARGETS)
            tbank = BANKS[tb]
            tdom = tbank.cards.split("@")[1]
            subj, text = r.choice([
                (f"Your {tbank.name} {prod} Credit Card has been closed",
                 f"Dear {name},\nYour {tbank.name} {prod} Credit Card XX{l4} has been closed at your request. "
                 f"To reactivate, verify your details: http://reactivate.example.invalid"),
                (f"Your card has been upgraded",
                 f"Dear {name},\nYour {prod} Credit Card XX{l4} has been upgraded to a new card XX{fake}. "
                 f"Activate it now: http://upgrade.example.invalid"),
                (f"Replacement card dispatched",
                 f"Dear {name},\nYour {prod} Credit Card ending {l4} was reported lost and has been blocked. "
                 f"Replacement card ending {fake} has been dispatched.")])
            g.bank_mail("forged-attack", tbank, tbank.cards, d, subj, text, auth=_fail(tdom), labels=labels)
            must_not_find[fake] = {"bank": "*", "last4": fake, "why": "forged upgrade/replacement of a real card"}
        elif kind == "statement":
            g.bank_mail("forged-statement", b, b.cards, d, f"Your {b.name} Platinum Credit Card Statement - {d:%B %Y}",
                        f"Dear {name},\nYour {b.name} Platinum Credit Card XX{fake} statement for {d:%B %Y} is ready.\n"
                        f"Total amount due: Rs. {g.amount(5000, 90000)}\nPay now to avoid late fees.",
                        auth=_fail(dom), labels=labels)
            must_not_find[fake] = {"bank": "*", "last4": fake, "why": "forged statement in the user's name"}
        elif kind == "lookalike":
            ldom = LOOKALIKES[bid]
            g.add("forged-lookalike", d, f"{b.name} <alerts@{ldom}>",
                  f"Action needed: {b.name} Credit Card XX{fake} suspended",
                  f"Dear {name}, your {b.name} Credit Card XX{fake} has been suspended. Re-verify your PAN and card details "
                  f"at http://{ldom}/verify to restore it.", auth=f"pass:{ldom}", labels=labels)
            must_not_find[fake] = {"bank": "*", "last4": fake, "why": "look-alike domain"}
        elif kind == "display":
            g.add("forged-display", d, f"{b.name} Credit Cards <{bid}.cards.helpdesk@gmail.com>",
                  f"Welcome to your new {b.name} Credit Card ending {fake}",
                  f"Dear {name}, congratulations! Your {b.name} Credit Card ending {fake} has been approved and dispatched. "
                  f"Pay the Rs. 499 activation fee to start using it.", auth="pass:gmail.com", labels=labels)
            must_not_find[fake] = {"bank": "*", "last4": fake, "why": "bank name on a personal address"}
        else:  # relay
            g.bank_mail("forged-relay", b, b.alerts, d, f"{b.name}: your Credit Card XX{fake} is now active",
                        f"Dear {name}, your {b.name} Credit Card XX{fake} has been activated. Set your PIN here: "
                        f"http://pin-{bid}.example.invalid", auth=_relay(dom), labels=labels)
            must_not_find[fake] = {"bank": "*", "last4": fake, "why": "signed by a mail service, DMARC failed"}
    return g.mails, list(must_not_find.values())


# ---------------------------------------------------------------- the flood: 19 fake for every real email

SCAM_SENDERS = ["Lucky Draw India <winner@lucky-draw-india.top>", "Crypto Rewards <bonus@cryptorewards.biz>",
                "Instant Loan <offers@instaloan-approve.xyz>", "KYC Update Team <kyc@rbi-kyc-update.info>",
                "Amazon Gift <gift@amaz0n-rewards.shop>", "Income Tax Refund <refund@incometax-refund.online>",
                "Card Upgrade Desk <upgrade@creditcard-upgrade.in.net>"]


def build_flood(to: str, n_authentic_last_year: int, per_authentic: int = 19, already: int = 0,
                seed: int = 123) -> tuple[list[Mail], list[dict]]:
    """per_authentic fake emails for every real one in the last 12 months, minus `already` imported.
    Fake card numbers are drawn from a pool of 400 that never clashes with a real card."""
    g = Gen(to, seed)
    r: random.Random = g.r
    real = {"2846", "2854", "6666", "7777", "2222", "4730", "3333", "6158", "6190", "1005", "2297", "5521", "4321",
            "5544", "8102", "3391", "9988", "1357"}
    pool = [n for n in (f"{r.randint(0, 9999):04d}" for _ in range(600)) if n not in real][:400]
    total = n_authentic_last_year * per_authentic - already
    kinds = (["spoof"] * 40 + ["attack"] * 10 + ["statement"] * 10 + ["lookalike"] * 15 + ["display"] * 10
             + ["relay"] * 5 + ["scam"] * 10)
    name = PERSONA["name"]
    used: set[str] = set()
    for i in range(total):
        kind = r.choice(kinds)
        d = TODAY - timedelta(days=r.randint(0, 364))
        bid = r.choice(["hdfc", "icici", "sbi", "axis", "kotak", "amex", "yes", "idfc"])
        b = BANKS[bid]
        dom = b.alerts.split("@")[1]
        fake = r.choice(pool)
        labels = ["SPAM"] if r.random() < 0.25 else ["INBOX"]
        if kind in ("spoof", "statement", "relay"):
            used.add(fake)
            auth = _relay(dom) if kind == "relay" else _fail(dom)
            subj, text = r.choice([
                (f"Your {b.name} Credit Card XX{fake} has been blocked",
                 f"Dear Customer, your {b.name} Credit Card XX{fake} has been blocked due to incomplete KYC. "
                 f"Verify within 24 hours: http://verify-{bid}.example.invalid"),
                (f"{r.randint(5, 40)},000 reward points expiring on your card ending {fake}",
                 f"Your reward points on {b.name} Credit Card ending {fake} expire tonight. Redeem: "
                 f"http://redeem-{bid}.example.invalid"),
                (f"Alert: Rs. {g.amount(9000, 90000)} spent on your {b.name} Credit Card",
                 f"Rs. {g.amount(9000, 90000)} was spent on your {b.name} Credit Card XX{fake}. Not you? Call 080-0000-0000."),
                (f"Your {b.name} Platinum Credit Card Statement - {d:%B %Y}",
                 f"Dear {name},\nYour {b.name} Platinum Credit Card XX{fake} statement is ready. Total amount due: "
                 f"Rs. {g.amount(5000, 90000)}"),
                (f"{b.name}: your Credit Card XX{fake} is now active",
                 f"Dear {name}, your {b.name} Credit Card XX{fake} has been activated. Set your PIN: "
                 f"http://pin-{bid}.example.invalid")])
            g.bank_mail(f"fake-{kind}", b, b.alerts, d, subj, text, auth=auth, labels=labels)
        elif kind == "attack":
            tb, l4, prod = r.choice(REAL_TARGETS)
            tbank = BANKS[tb]
            used.add(fake)
            subj, text = r.choice([
                (f"Your {tbank.name} {prod} Credit Card has been closed",
                 f"Dear {name},\nYour {tbank.name} {prod} Credit Card XX{l4} has been closed. To reactivate: "
                 f"http://reactivate.example.invalid"),
                ("Your card has been upgraded",
                 f"Dear {name},\nYour {prod} Credit Card XX{l4} has been upgraded to a new card XX{fake}."),
                ("Replacement card dispatched",
                 f"Dear {name},\nYour {prod} Credit Card ending {l4} was reported lost and has been blocked. "
                 f"Replacement card ending {fake} has been dispatched.")])
            g.bank_mail("fake-attack", tbank, tbank.cards, d, subj, text, auth=_fail(tbank.cards.split("@")[1]),
                        labels=labels)
        elif kind == "lookalike":
            ldom = LOOKALIKES.get(bid, f"{bid}-cards-alerts.com")
            g.add("fake-lookalike", d, f"{b.name} <alerts@{ldom}>", f"Action needed: {b.name} Credit Card XX{fake} suspended",
                  f"Dear {name}, your {b.name} Credit Card XX{fake} has been suspended. Re-verify at http://{ldom}/verify",
                  auth=f"pass:{ldom}", labels=labels)
            used.add(fake)
        elif kind == "display":
            g.add("fake-display", d, f"{b.name} Credit Cards <{bid}.cards.helpdesk{r.randint(1, 99)}@gmail.com>",
                  f"Welcome to your new {b.name} Credit Card ending {fake}",
                  f"Dear {name}, your {b.name} Credit Card ending {fake} is approved. Pay Rs. 499 to activate.",
                  auth="pass:gmail.com", labels=labels)
            used.add(fake)
        else:  # generic scam
            sender = r.choice(SCAM_SENDERS)
            sdom = sender.split("@")[1].rstrip(">")
            g.add("fake-scam", d, sender, r.choice(["You have won Rs. 25,00,000!", "Pre-approved loan of Rs. 5 lakh",
                                                    "Your KYC is expiring today", "Claim your income tax refund",
                                                    "Upgrade your credit card limit instantly"]),
                  "Click the link and enter your card number, CVV and OTP to claim: http://claim.example.invalid",
                  auth=f"pass:{sdom}", labels=labels)
    must_not = [{"bank": "*", "last4": n, "why": "number only in fake/forged mail"} for n in sorted(used)]
    return g.mails, must_not
