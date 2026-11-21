from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from carddiscovery.registry import Registry
from cdserver import config
from cdserver.api import create_app


@pytest.fixture
def client(c, monkeypatch):
    monkeypatch.setattr(config, "DEV_LOGIN", True)  # these tests sign in as named test users
    app, api = create_app(Registry.load(), ["Test User"])
    cl = TestClient(app, base_url=f"http://127.0.0.1:{config.API_PORT}")
    cl.headers["X-Token"] = api.token
    return cl


def test_login_comes_from_the_other_service_not_otp(client):
    assert client.post("/api/otp/send", json={"phone": "9876543210"}).status_code == 404
    assert client.get("/api/me").status_code == 401
    me = client.post("/api/dev/login", json={"user": "dev-user-1"}).json()
    assert me["user"] == "dev-user-1" and me["connected"] is False
    assert client.get("/api/status").json()["state"] == "idle"


def test_requests_without_the_app_token_or_from_another_host_are_refused(client):
    assert client.get("/api/config", headers={"X-Token": "wrong"}).status_code == 403
    assert client.get("/api/config", headers={"Host": "evil.example"}).status_code == 403


def test_bad_user_id_is_rejected(client):
    assert client.post("/api/dev/login", json={"user": "a b; drop"}).status_code == 400


def test_delete_account_without_gmail_removes_it_at_once(client, c):
    client.post("/api/dev/login", json={"user": "dev-user-1"})
    assert client.post("/api/delete_account", json={}).status_code == 200
    assert c.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"] == 0


# ---- each browser its own account; Google shows who you are

@pytest.fixture
def public(c, monkeypatch):
    """The app as on the shared server: no "?user=" sign-in."""
    monkeypatch.setattr(config, "DEV_LOGIN", False)
    app, api = create_app(Registry.load(), ["Test User"])

    def new_browser():
        cl = TestClient(app, base_url=f"http://127.0.0.1:{config.API_PORT}")
        cl.headers["X-Token"] = api.token
        return cl
    return new_browser, api


def test_on_the_shared_server_nobody_can_sign_in_as_a_named_user(public):
    new_browser, _ = public
    assert new_browser().post("/api/dev/login", json={"user": "nikhil"}).status_code == 404


def test_every_new_browser_starts_with_its_own_empty_account(public):
    new_browser, _ = public
    a, b = new_browser(), new_browser()
    me_a, me_b = a.post("/api/session", json={}).json(), b.post("/api/session", json={}).json()
    assert me_a["user"].startswith("anon-") and me_b["user"].startswith("anon-") and me_a["user"] != me_b["user"]
    assert me_a["connected"] is False and me_a["saved_cards"] == 0
    assert a.post("/api/session", json={}).json()["user"] == me_a["user"]  # same browser, same account


def _fake_google(monkeypatch, mailbox):
    from carddiscovery.providers import gmail as g

    class Creds:
        granted_scopes = list(g.SCOPES)
        scopes = list(g.SCOPES)

        def to_json(self):
            return "{}"

    class Flow:
        credentials = Creds()

        def fetch_token(self, code):
            pass

    monkeypatch.setattr(g, "start_web_flow", lambda client_file, redirect: ("https://accounts.example/auth", "st-1", Flow()))
    monkeypatch.setattr(g.GmailProvider, "__init__", lambda self, creds: None)
    monkeypatch.setattr(g.GmailProvider, "mailbox", lambda self: mailbox)


def test_connecting_a_gmail_seen_before_signs_you_in_to_your_own_account(public, c, monkeypatch):
    new_browser, api = public
    owner = c.execute("INSERT INTO accounts (external_id) VALUES ('anon-owner') RETURNING id").fetchone()["id"]
    c.execute("INSERT INTO gmail_connections (account_id, mailbox, token_blob, scopes, years, names) "
              "VALUES (%s, 'friend@gmail.com', '{}', '{x}', 3, '{Friend}')", (owner,))
    c.execute("INSERT INTO cards (account_id, source, card_key, bank_id, last4, grp, view) VALUES "
              "(%s, 'gmail', 'hdfc:6113', 'hdfc', '6113', 'owned', '{\"title\": \"HDFC ••6113\"}')", (owner,))
    _fake_google(monkeypatch, "friend@gmail.com")
    laptop2 = new_browser()
    me = laptop2.post("/api/session", json={})
    assert me.json()["saved_cards"] == 0  # a new laptop starts empty
    laptop2.post("/api/connect", json={"years": 3, "name": "Friend"})
    r = laptop2.get("/oauth/callback?state=st-1&code=abc", follow_redirects=False)
    assert r.status_code == 303
    after = laptop2.get("/api/me").json()
    assert after["mailbox"] == "friend@gmail.com" and after["saved_cards"] == 1  # their own wallet
    assert c.execute("SELECT count(*) AS n FROM accounts WHERE external_id LIKE 'anon-%%' AND id <> %s",
                     (owner,)).fetchone()["n"] == 0  # the empty one is gone


def test_a_new_gmail_stays_in_the_new_browsers_own_account(public, c, monkeypatch):
    new_browser, _ = public
    _fake_google(monkeypatch, "newperson@gmail.com")
    b = new_browser()
    me = b.post("/api/session", json={}).json()
    b.post("/api/connect", json={"years": 3, "name": "New Person"})
    b.get("/oauth/callback?state=st-1&code=abc", follow_redirects=False)
    after = b.get("/api/me").json()
    assert after["user"] == me["user"] and after["mailbox"] == "newperson@gmail.com" and after["connected"]


def test_the_developer_panel_and_its_endpoints_are_for_the_team_only(public, c, monkeypatch):
    new_browser, _ = public
    b = new_browser()
    me = b.post("/api/session", json={}).json()
    assert me["admin"] is False
    for path in ("/api/dev/live", "/api/dev/reports", "/api/dev/jobs"):
        assert b.get(path).status_code == 404, path
    assert b.post("/api/dev/fresh_scan", json={}).status_code == 404
    acc = c.execute("SELECT id FROM accounts WHERE external_id = %s", (me["user"],)).fetchone()["id"]
    c.execute("INSERT INTO gmail_connections (account_id, mailbox, token_blob, scopes, years, names) "
              "VALUES (%s, 'lead@team.example', '{}', '{x}', 3, '{Lead}')", (acc,))
    monkeypatch.setattr(config, "ADMIN_EMAILS", {"lead@team.example"})
    assert b.get("/api/me").json()["admin"] is True
    assert b.get("/api/dev/reports").status_code == 200 and b.get("/api/dev/jobs").status_code == 200


def test_the_page_is_a_static_file_and_settings_come_from_the_api(public):
    new_browser, _ = public
    b = new_browser()
    page = b.get("/").text
    assert "__TOKEN__" not in page and "CardWise" not in page and "statusbar" not in page
    cfg = b.get("/api/config").json()
    assert cfg["dev_login"] is False and "default_name" in cfg and cfg["banks"]
