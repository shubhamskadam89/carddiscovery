"""Test mode: personal addresses stand in for banks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from carddiscovery.classify import analyze
from carddiscovery.models import EvidenceType as T, MessageMeta, Status
from carddiscovery.registry import TEST_BANK_ID, Registry
from carddiscovery.resolve import resolve
from carddiscovery.score import group, score_all

ME = "shubham.test@gmail.com"
REG = Registry.load(test_senders=[ME])
NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)
_n = 0


def selfmail(subject, body, sender=f"Shubham <{ME}>"):
    global _n
    _n += 1
    return MessageMeta(message_id=f"t{_n}", date=NOW - timedelta(days=1), sender=sender,
                       subject=subject, snippet=body[:200], body=body, to=ME, auth_results="")


def run(msgs):
    ev = [analyze(m, REG, [ME], ["Shubham Kadam"]) for m in msgs]
    cards, apps = resolve(ev)
    score_all(cards, NOW)
    return cards, apps, ev


def test_bank_read_from_text_and_trusted():
    cards, _, ev = run([selfmail("Your HDFC Bank Regalia Gold Credit Card Statement",
                                 "Dear Shubham Kadam, statement for card ending 1234 is ready.")])
    assert ev[0].bank_id == "hdfc" and ev[0].dkim_pass and ev[0].test
    assert cards[0].product == "Regalia Gold" and group(cards[0]) == "owned"


def test_bank_mentions():
    assert REG.bank_from_text("Your SBI Card ELITE statement") == "sbi"
    assert REG.bank_from_text("American Express Platinum Travel") == "amex"
    assert REG.bank_from_text("Amex card ending 1111") == "amex"
    assert REG.bank_from_text("Kotak White card") == "kotak"
    assert REG.bank_from_text("yes, I will send the card") is None  # "yes" is not YES Bank


def test_date_tag_makes_mail_historical():
    msgs = [selfmail("Your ICICI Bank Coral Credit Card Statement",
                     f"[date:{2019 + i}-03-05] Statement for card ending 4444") for i in range(3)]
    cards, _, _ = run(msgs)
    c = cards[0]
    assert c.first_seen.year == 2019 and c.last_seen.year == 2021
    assert c.status == Status.DORMANT


def test_unnamed_bank_falls_back_to_test_bank():
    cards, _, _ = run([selfmail("Credit Card Statement", "Statement for card ending 9999")])
    assert cards[0].bank_id == TEST_BANK_ID


def test_personal_mail_without_card_ignored():
    _, _, ev = run([selfmail("Welcome to the team!", "Excited to have you. Statement of work attached.")])
    assert ev[0].bank_id is None


def test_non_test_sender_unaffected():
    m = selfmail("Your HDFC Bank Credit Card Statement", "card ending 1234", sender="Friend <friend@gmail.com>")
    _, _, ev = run([m])
    assert ev[0].bank_id is None and not ev[0].test


def test_promo_from_test_sender_creates_nothing():
    cards, _, ev = run([selfmail("Apply now for HDFC Bank Infinia Credit Card", "Exclusive offer, apply now")])
    assert cards == [] and ev[0].type == T.PROMOTIONAL


# ---------------------------------------------------------------- docs/test-emails.md kit

def _kit():
    """The 21 emails from docs/test-emails.md, parsed from its table."""
    from pathlib import Path
    rows = []
    for line in (Path(__file__).parent.parent / "docs" / "test-emails.md").read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0].isdigit():
            rows.append((cells[1], cells[2]))
    return rows


def test_kit_matches_documented_results():
    rows = _kit()
    assert len(rows) == 21
    cards, apps, _ = run([selfmail(s, b) for s, b in rows])
    got = {(c.bank_id, c.last4 or c.product): (group(c), c.status.value, c.product) for c in cards}
    expected = {
        ("hdfc", "1111"): ("owned", "active", "Regalia Gold"),
        ("hdfc", "7777"): ("owned", "active", "Regalia Gold"),
        ("icici", "2222"): ("owned", "dormant", "Amazon Pay"),
        ("sbi", "3333"): ("owned", "dormant", "ELITE"),
        ("axis", "4444"): ("owned", "active", "ACE"),
        ("axis", "5555"): ("owned", "active", "ACE"),
        ("kotak", "White"): ("review", "approved_not_seen", "White"),
        ("icici", "9191"): ("review", "active", None),
        ("hdfc", "5656"): ("review", "active", None),
        ("hdfc", "3434"): ("review", "active", "Millennia"),
        ("axis", "1212"): ("review", "possibly_closed", "Magnus"),
        ("hdfc", "6666"): ("history", "replaced", "Regalia"),
        ("icici", "8888"): ("history", "closed", "Coral"),
    }
    assert got == expected
    assert [(a.bank_id, a.status) for a in apps] == [("indusind", "rejected")]


# ---------------------------------------------------------------- messy emails

def test_messy_emails_match_expectations():
    from carddiscovery.classify import needs_body
    from carddiscovery.providers.gmail import html_to_text
    from tests.messy_emails import MESSY

    evs = []
    for e in MESSY:
        body = html_to_text(e["html"]) if "html" in e else e["body"]
        m = selfmail(e["subject"], body)
        m.snippet, m.body = " ".join(body.split())[:200], None  # like Gmail: preview first
        ev = analyze(m, REG, [ME], ["Shubham Kadam"])
        if needs_body(ev):
            m.body = body
            ev = analyze(m, REG, [ME], ["Shubham Kadam"])
        evs.append(ev)
    cards, _ = resolve(evs)
    score_all(cards, NOW)
    got = {(c.bank_id, c.last4 or c.product): (group(c), c.product) for c in cards}
    expected = {k: v for e in MESSY if e["expect"] for k, v in e["expect"].items()}
    assert got == expected
