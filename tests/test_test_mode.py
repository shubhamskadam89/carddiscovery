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
# What Gmail adds to mail that really came from a gmail.com account.
GMAIL_SIGNED = "mx.google.com; dkim=pass header.i=@gmail.com header.s=20230601; spf=pass; dmarc=pass header.from=gmail.com"
_n = 0


def selfmail(subject, body, sender=f"Shubham <{ME}>"):
    global _n
    _n += 1
    return MessageMeta(message_id=f"t{_n}", date=NOW - timedelta(days=1), sender=sender,
                       subject=subject, snippet=body[:200], body=body, to=ME,
                       auth_results=GMAIL_SIGNED)


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


# ---------------------------------------------------------------- HTML emails

def _gmail_payload(html: str, plain: str | None = None) -> dict:
    import base64
    b64 = lambda s: base64.urlsafe_b64encode(s.encode()).decode()
    parts = [{"mimeType": "text/plain", "body": {"data": b64(plain)}}] if plain else []
    parts.append({"mimeType": "text/html", "body": {"data": b64(html)}})
    return {"mimeType": "multipart/alternative", "parts": parts}


def test_html_emails_match_expectations():
    from tests.html_emails import HTML
    _check_html_kit(HTML)


def test_long_formal_emails_match_expectations():
    from tests.long_emails import LONG
    _check_html_kit(LONG)


def _check_html_kit(kit):
    from carddiscovery.classify import needs_body
    from carddiscovery.providers.gmail import _extract_body, html_to_text

    evs = []
    for e in kit:
        body, _ = _extract_body(_gmail_payload(e["html"], e.get("plain")))
        m = selfmail(e["subject"], body)
        m.snippet, m.body = html_to_text(e["html"])[:200], None
        ev = analyze(m, REG, [ME], ["Shubham Kadam"])
        if needs_body(ev):
            m.body = body
            ev = analyze(m, REG, [ME], ["Shubham Kadam"])
        evs.append(ev)
    cards, _ = resolve(evs)
    score_all(cards, NOW)
    got = {(c.bank_id, c.last4 or c.product): (group(c), c.product) for c in cards}
    expected = {k: v for e in kit if e["expect"] for k, v in e["expect"].items()}
    assert got == expected


def test_html_parsing_details():
    from carddiscovery.extract import last4s
    from carddiscovery.providers.gmail import _extract_body, html_to_text

    assert "XXXX 3691" in html_to_text("XXXX <span>36</span><span>91</span>")
    assert html_to_text("<td>a</td><td>b</td>") == "a\nb"  # cells stay on separate lines
    assert html_to_text("A&nbsp;&zwnj;B") == "A B"
    body, _ = _extract_body(_gmail_payload("<p>card ending 1234</p>", plain="View in an HTML client"))
    assert "1234" in body
    assert last4s("Savings A/c XX7788, card XX1111, linked account number XXXXXX4455") == ["1111"]
    assert last4s("Card Member account ending 71005") == ["1005"]


# ---------------------------------------------------------------- security

def test_test_sender_still_needs_a_real_signature():
    m = selfmail("Your HDFC Bank Regalia Gold Credit Card Statement", "statement for card ending 1234")
    m.auth_results = ""  # e.g. forged From with no Gmail signature result
    cards, _, ev = run([m])
    assert not ev[0].dkim_pass and group(cards[0]) == "review"


def test_test_mode_is_locked_without_dev_flag(monkeypatch):
    import pytest
    from carddiscovery.registry import TestModeLocked
    monkeypatch.delenv("CARDDISCOVERY_DEV", raising=False)
    with pytest.raises(TestModeLocked):
        Registry.load(test_senders=[ME])


def test_only_gmails_own_auth_header_is_trusted():
    from carddiscovery.providers.gmail import trusted_auth_results
    forged = "attacker.example; dkim=pass header.d=hdfcbank.net"
    real = "mx.google.com; dkim=fail header.d=evil.example; dmarc=fail"
    assert trusted_auth_results([real, forged]) == real
    assert trusted_auth_results([forged]) == ""


def test_forged_auth_header_cannot_make_bank_mail_trusted():
    from carddiscovery.classify import dkim_pass
    from carddiscovery.providers.gmail import _to_meta
    raw = {"id": "f1", "internalDate": "1700000000000", "snippet": "card ending 1234", "payload": {"headers": [
        {"name": "Authentication-Results", "value": "mx.google.com; dkim=fail header.d=evil.example"},
        {"name": "Authentication-Results", "value": "evil.example; dkim=pass header.d=hdfcbank.net"},
        {"name": "From", "value": "HDFC Bank <alerts@hdfcbank.net>"},
        {"name": "Subject", "value": "Your HDFC Bank Credit Card Statement"},
    ]}}
    m = _to_meta(raw)
    assert not dkim_pass(m, "hdfc", Registry.load())


def test_recipient_match_is_whole_address():
    from carddiscovery.classify import addressed_to_user
    m = selfmail("x card", "card")
    m.to = "Someone <aashubham.test@gmail.com>"
    assert not addressed_to_user(m, [ME])
    m.to = f"Shubham <{ME}>, other@example.com"
    assert addressed_to_user(m, [ME])


# ---------------------------------------------------------------- explanations and answers

def _kit_cards():
    return run([selfmail(s, b) for s, b in _kit()])[0]


def test_explanations_are_plain_sentences():
    from carddiscovery.explain import explain
    cards = {c.last4: c for c in _kit_cards()}
    e = explain(cards["6666"], REG)
    assert e.title == "HDFC Bank Regalia ••6666" and e.status == "Replaced"
    assert "••7777" in e.headline and "upgrade" in e.headline
    e = explain(cards["9191"], REG)
    assert e.question == "Is this your card?"
    assert any("someone else" in c for c in e.concerns)
    e = explain(cards["8888"], REG)
    assert e.status == "Closed" and "Jan 2022" in e.headline


def test_answers_override_grouping_and_persist(tmp_path):
    from carddiscovery.ledger import Ledger
    led = Ledger(tmp_path / "l.db")
    cards = {c.last4: c for c in _kit_cards()}
    led.decide(ME, cards["9191"].decision_key, "yes")
    led.decide(ME, cards["1111"].decision_key, "no")
    answers = Ledger(tmp_path / "l.db").decisions(ME)  # survives reopening
    for c in cards.values():
        c.decision = answers.get(c.decision_key)
    assert group(cards["9191"]) == "owned" and group(cards["1111"]) == "rejected"
    led.forget(ME)  # clearing the cache keeps answers
    assert led.decisions(ME)


def test_professional_emails_match_expectations():
    from tests.pro_emails import PRO
    _check_html_kit(PRO)


def test_script_content_is_never_read():
    from carddiscovery.providers.gmail import _extract_body
    from tests.pro_emails import PRO, TRAP
    for e in PRO:
        body, _ = _extract_body(_gmail_payload(e["html"]))
        assert TRAP not in body and "track()" not in body
