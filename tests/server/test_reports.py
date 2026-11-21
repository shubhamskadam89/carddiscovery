"""Users telling us a card or an email we showed is wrong: the app's buttons, and forwards to the team's
report inbox. Every report is logged."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from carddiscovery.models import Evidence, EvidenceType as T, MessageMeta
from carddiscovery.registry import Registry
from cdserver import cards, engine, reports

NOW = datetime.now(timezone.utc)
REG = Registry.load()
USER = "nikhil@gmail.com"
INBOX = "team.reports@gmail.com"
SIGNED = "mx.google.com; dkim=pass header.i=@gmail.com; dmarc=pass header.from=gmail.com"
WHEN = NOW - timedelta(days=3)

GMAIL_FWD = f"""This is not my card, it's my wife's

---------- Forwarded message ---------
From: HDFC Bank InstaAlerts <alerts@hdfcbank.net>
Date: {WHEN:%a, %d %b %Y} at {WHEN:%H:%M}
Subject: Alert: transaction on your HDFC Bank Credit Card
To: <nikhil@gmail.com>


Rs 499 spent on your HDFC Bank Credit Card ending 4444 at AMAZON.
"""

WRONG_EMAIL_FWD = GMAIL_FWD.replace("This is not my card, it's my wife's", "Wrong email for my 6113 card") \
    .replace("4444", "6113")

APPLE_FWD = """Begin forwarded message:

From: SBI Card <statements@sbicard.com>
Subject: Your SBI Card statement
Date: 3 November 2026 at 9:02:11 AM GMT+5:30
To: nikhil@gmail.com

Your SBI Card ending 8022 statement is ready.
"""


def fwd(mid, body, sender=USER, auth=SIGNED):
    return MessageMeta(mid, NOW, sender, "Fwd: alert", "", to=INBOX, auth_results=auth, body=body)


def test_the_original_and_the_users_note_come_out_of_gmail_and_apple_forwards():
    orig, note = reports.unwrap(fwd("f1", GMAIL_FWD))
    assert orig.sender.endswith("<alerts@hdfcbank.net>") and "ending 4444" in orig.body
    assert note == "This is not my card, it's my wife's" and "wife" not in orig.body
    assert abs((orig.date - WHEN).total_seconds()) < 120
    orig, note = reports.unwrap(fwd("f2", APPLE_FWD))
    assert orig.sender.endswith("<statements@sbicard.com>") and note == ""
    assert reports.unwrap(fwd("f3", "just a note, no forward")) is None


class Gmail:
    def __init__(self, msgs):
        self.msgs, self.queries = {m.message_id: m for m in msgs}, []

    def search(self, q):
        self.queries.append(q.to_gmail())
        return list(self.msgs)

    def get_full(self, ids):
        return [self.msgs[i] for i in ids]


def _setup(c, account):
    a = account("nikhil")
    inbox = account("team-inbox")
    for acc, box in ((a, USER), (inbox, INBOX)):
        c.execute("INSERT INTO gmail_connections (account_id, mailbox, token_blob, scopes, years, names) "
                  "VALUES (%s, %s, '{}', '{x}', 3, '{Nikhil Jain}')", (acc, box))
    base = dict(bank_id="hdfc", dkim_pass=True, used_body=True)
    engine.EvidenceStore(c, a).save(
        [Evidence(message_id=f"s{i}", date=NOW - timedelta(days=30 * i + 1), type=T.STATEMENT, last4s=["6113"],
                  **base) for i in range(3)]
        + [Evidence(message_id="t6113", date=WHEN, type=T.TRANSACTION_ALERT, last4s=["6113"], **base),
           Evidence(message_id="t4444", date=WHEN, type=T.TRANSACTION_ALERT, last4s=["4444"], **base)])
    cards.rebuild(c, a, REG)
    return a


def groups(c, a):
    return {r["last4"]: r["grp"] for r in c.execute("SELECT last4, grp FROM cards WHERE account_id = %s", (a,))}


def test_a_not_my_card_forward_marks_the_card_no_and_is_logged(c, account):
    a = _setup(c, account)
    assert groups(c, a)["4444"] in ("owned", "review")
    g = Gmail([fwd("r1", GMAIL_FWD), fwd("r2", GMAIL_FWD, sender="Stranger <x@evil.example>")])
    out = reports.read_inbox(g, REG, c, INBOX)
    assert out == {"found": 2, "applied": 1, "open": 0, "ignored": 1}
    assert f"to:{INBOX}" in g.queries[0] and USER in g.queries[0] and INBOX not in g.queries[0].split("to:")[0]
    assert groups(c, a)["4444"] == "rejected"
    [r] = reports.recent(c, account_id=a)
    assert (r["source"], r["kind"], r["status"], r["bank_id"], r["last4"]) == ("forward", "not_my_card", "applied",
                                                                               "hdfc", "4444")
    assert "wife" in r["note"] and "transaction" in r["subject"]
    assert reports.read_inbox(g, REG, c, INBOX)["found"] == 1  # only the stranger's, never recorded, comes back


def test_a_wrong_email_forward_leaves_that_email_out(c, account):
    a = _setup(c, account)
    reports.read_inbox(Gmail([fwd("r3", WRONG_EMAIL_FWD)]), REG, c, INBOX)
    assert "t6113" in reports.wrong_ids(c, a) and groups(c, a)["6113"] == "owned"
    assert reports.recent(c, account_id=a)[0]["kind"] == "wrong_email"


def test_a_forward_we_cant_match_stays_open_for_the_team(c, account):
    a = _setup(c, account)
    reports.read_inbox(Gmail([fwd("r4", APPLE_FWD)]), REG, c, INBOX)
    [r] = reports.recent(c, account_id=a)
    assert r["status"] == "open" and r["bank_id"] == "sbi"


def test_app_reports_are_logged_and_undo_is_kept_in_the_log(c, account):
    a = _setup(c, account)
    reports.report_wrong(c, a, "t4444", True)
    reports.log(c, a, "app", "wrong_email", "applied", message_id="t4444")
    cards.rebuild(c, a, REG)
    assert "4444" not in groups(c, a)
    reports.report_wrong(c, a, "t4444", False)
    reports.undo(c, a, message_id="t4444")
    cards.rebuild(c, a, REG)
    assert "4444" in groups(c, a)
    [r] = reports.recent(c, account_id=a)
    assert r["status"] == "fixed" and "undone" in r["note"]


def test_not_my_card_keeps_every_evidence_id_and_the_team_can_fetch_those_emails(c, account):
    a = _setup(c, account)
    engine.EvidenceStore(c, a).save([Evidence(message_id="s1#pdf", date=NOW - timedelta(days=31), bank_id="hdfc",
                                              type=T.STATEMENT, last4s=["4444"], dkim_pass=True)])
    cards.rebuild(c, a, REG)
    key = c.execute("SELECT card_key FROM cards WHERE account_id = %s AND last4 = '4444'", (a,)).fetchone()["card_key"]
    ids = reports.card_evidence_ids(c, a, key)
    assert set(ids) == {"t4444", "s1#pdf"}
    rid = reports.log(c, a, "app", "not_my_card", "applied", card_key=key, note="my brother's", evidence_ids=ids)
    assert reports.recent(c, account_id=a)[0]["emails"] == 2

    class G:
        asked = []

        def get_full(self, ids):
            self.asked += ids
            return [MessageMeta(i, NOW, "HDFC <alerts@hdfcbank.net>", f"subject {i}", "",
                                body="Card 4111 1111 1111 1111 used", attachment_names=("st.pdf",)) for i in ids]

    g = G()
    out = reports.review_emails(c, rid, gmail_for=lambda c_, acc: g)
    assert sorted(g.asked) == ["s1", "t4444"] and out["total"] == 2
    assert all("4111 1111 1111 1111" not in m["text"] for m in out["emails"])
    assert {m["id"]: m["pdf_evidence"] for m in out["emails"]} == {"t4444": False, "s1": True}
    assert c.execute("SELECT count(*) AS n FROM audit_log WHERE account_id = %s AND actor = 'team_review'",
                     (a,)).fetchone()["n"] == 1


def test_the_catalogue_lists_banks_with_their_cards():
    banks = {b["id"]: b for b in reports.catalog(REG)}
    assert "Diners Club Black" in banks["hdfc"]["products"] and banks["hdfc"]["name"]


def test_a_correction_is_logged_with_the_cards_emails_and_shown_on_the_card(c, account):
    import pytest
    a = _setup(c, account)
    key = c.execute("SELECT card_key FROM cards WHERE account_id = %s AND last4 = '4444'", (a,)).fetchone()["card_key"]
    with pytest.raises(ValueError):
        reports.correction(c, REG, a, key, "hdfc", "No Such Card", "1234", None)
    with pytest.raises(ValueError):
        reports.correction(c, REG, a, key, "hdfc", "Diners Club Black", "12a4", None)
    reports.correction(c, REG, a, key, "hdfc", "Diners Club Black", "1234", "it's my Diners card")
    [r] = reports.recent(c, account_id=a)
    assert (r["kind"], r["status"], r["emails"]) == ("correction", "open", 1)
    assert r["correct"] == {"bank": "hdfc", "product": "Diners Club Black", "last4": "1234"}
    cards.rebuild(c, a, REG)
    view = c.execute("SELECT view FROM cards WHERE account_id = %s AND card_key = %s", (a, key)).fetchone()["view"]
    assert view["reported_correction"]["product"] == "Diners Club Black"
