"""The local web app: OTP login, Google consent round-trip, scan, answers, disconnect.

Google is replaced by fakes: oauth_start returns a URL and state, oauth_finish
returns stand-in credentials, and the provider is the fake mailbox.
"""

from __future__ import annotations

import http.cookiejar
import json
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from carddiscovery.accounts import AccountStore, ConsoleSender
from carddiscovery.providers.gmail import ScopeNotGranted
from carddiscovery.registry import Registry
from carddiscovery.web import App, make_server
from tests.test_run import FakeGmail
from tests.test_test_mode import ME, _kit, selfmail

PHONE = "+919876543210"


class Box(FakeGmail):
    def mailbox(self):
        return ME

    def user_addresses(self):
        return [ME]


class Client:
    """Browser stand-in: keeps cookies, sends the app token, doesn't follow redirects."""

    def __init__(self, base, token):
        self.base, self.token = base, token
        self.jar = http.cookiejar.CookieJar()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)

    def call(self, path, body=None, token=True, host=None):
        req = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None)
        if token:
            req.add_header("X-Token", self.token)
        if host:
            req.add_header("Host", host)
        try:
            with self.opener.open(req, timeout=5) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if "json" in r.headers.get("Content-Type", "") else raw), r.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}"), e.headers
            except ValueError:
                return e.code, raw, e.headers


@pytest.fixture
def env(tmp_path):
    msgs = []
    for i, (s, b) in enumerate(_kit()):
        m = selfmail(s, b)
        m.message_id, m.date = f"k{i}", datetime.now(timezone.utc) - timedelta(minutes=i)
        msgs.append(m)
    sender = ConsoleSender()
    granted = {"scope": True}

    def oauth_start(credentials, redirect_uri):
        return f"https://accounts.google.example/consent?redirect_uri={redirect_uri}", f"state{time.time_ns()}", "flow"

    def oauth_finish(flow, code, token_file):
        if not granted["scope"]:
            raise ScopeNotGranted("unticked")
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text("{}")
        return "creds"

    app = App(Registry.load(test_senders=[ME]), tmp_path / "creds.json", tmp_path, ["Shubham Kadam"], [ME],
              accounts=AccountStore(tmp_path / "accounts.db", sender=sender),
              provider_factory=lambda creds: Box(msgs), oauth_start=oauth_start, oauth_finish=oauth_finish,
              creds_loader=lambda tf: "creds" if tf.exists() else None)
    httpd, token = make_server(app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield Client(base, token), sender, app, granted, tmp_path
    httpd.shutdown()


def login(c, sender):
    assert c.call("/api/otp/send", {"phone": "98765 43210"})[0] == 200
    code, me, headers = c.call("/api/otp/verify", {"phone": "9876543210", "code": sender.last[PHONE]})
    assert code == 200
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    return me


def connect_and_scan(c, app):
    _, out, _ = c.call("/api/connect", {"years": 5, "name": "Shubham Kadam"})
    state = next(iter(app.pending))
    assert "redirect_uri=http://127.0.0.1:" in out["url"]
    code, _, headers = c.call(f"/oauth/callback?state={state}&code=abc", token=False)
    assert code == 303 and headers["Location"].endswith("#scanning")
    for _ in range(50):
        st = c.call("/api/status")[1]
        if st["state"] == "done":
            return st
        time.sleep(0.1)
    raise AssertionError("scan didn't finish")


def test_guards(env):
    c, sender, *_ = env
    assert c.call("/api/me", token=False)[0] == 403
    assert c.call("/api/me", host="evil.example:80")[0] == 403
    assert c.call("/api/me")[0] == 401  # app token alone isn't a login
    assert c.call("/api/connect", {"years": 5})[0] == 401
    assert c.call("/api/otp/send", {"phone": "123"})[0] == 400


def test_full_flow_login_connect_scan_answer_disconnect(env):
    c, sender, app, _, tmp = env
    me = login(c, sender)
    assert me["phone"].endswith("43210") and not me["connected"]
    st = connect_and_scan(c, app)
    groups = st["result"]["groups"]
    assert len(groups["owned"]) == 6 and len(groups["review"]) == 5 and len(groups["history"]) == 2
    assert not any(s in json.dumps(st) for s, _ in _kit())  # no email text leaves the server

    # Answers are saved to the account's card list.
    c.call("/api/decide", {"key": "icici:9191", "answer": "no"})
    saved = c.call("/api/cards")[1]
    assert [x["key"] for x in saved["groups"]["rejected"]] == ["icici:9191"]
    assert all(x["source"] == "gmail" for g in saved["groups"].values() for x in g)

    # Rescan uses the saved Google access, no consent screen again.
    assert c.call("/api/scan", {"years": 5})[0] in (202, 409)

    out = c.call("/api/disconnect", {})[1]
    assert "revoked" in out and not (tmp / "tokens" / "1.json").exists()
    assert c.call("/api/cards")[1]["groups"]["owned"] == []
    assert c.call("/api/scan", {"years": 5})[1].get("reconnect") is True


def test_oauth_callback_checks_state_and_session(env):
    c, sender, app, granted, _ = env
    login(c, sender)
    c.call("/api/connect", {"years": 5})
    # Wrong state → expired, nothing starts.
    _, _, h = c.call("/oauth/callback?state=forged&code=x", token=False)
    assert h["Location"].endswith("#expired")
    state = next(iter(app.pending))
    # User pressed Cancel on Google's screen.
    _, _, h = c.call(f"/oauth/callback?state={state}&error=access_denied", token=False)
    assert h["Location"].endswith("#denied")
    # User unticked Gmail access.
    granted["scope"] = False
    c.call("/api/connect", {"years": 5})
    state = next(iter(app.pending))
    _, _, h = c.call(f"/oauth/callback?state={state}&code=abc", token=False)
    assert h["Location"].endswith("#noscope")
    # A state can't be replayed.
    _, _, h = c.call(f"/oauth/callback?state={state}&code=abc", token=False)
    assert h["Location"].endswith("#expired")


def test_callback_from_a_different_browser_is_refused(env):
    c, sender, app, _, _ = env
    login(c, sender)
    c.call("/api/connect", {"years": 5})
    state = next(iter(app.pending))
    stranger = Client(c.base, c.token)  # no session cookie
    _, _, h = stranger.call(f"/oauth/callback?state={state}&code=abc", token=False)
    assert h["Location"].endswith("#expired")


def test_logout_and_delete_account(env):
    c, sender, app, _, _ = env
    login(c, sender)
    connect_and_scan(c, app)
    assert c.call("/api/logout", {})[0] == 200
    assert c.call("/api/me")[0] == 401
    c.call("/api/otp/send", {"phone": "9876543210"})  # within cooldown → error, so wait it out
    app.accounts.db.execute("DELETE FROM otps")
    app.accounts.db.commit()
    c.call("/api/otp/send", {"phone": "9876543210"})
    c.call("/api/otp/verify", {"phone": "9876543210", "code": sender.last[PHONE]})
    assert c.call("/api/me")[1]["saved_cards"] > 0  # cards survived logout
    c.call("/api/delete_account", {})
    assert c.call("/api/me")[0] == 401
    assert app.accounts.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_disconnect_during_a_running_scan_keeps_nothing(env):
    """Regression: a scan that finished after Disconnect used to save its cards again."""
    c, sender, app, _, tmp = env
    login(c, sender)
    connect_and_scan(c, app)
    acc = app.accounts.session(next(ck.value for ck in c.jar if ck.name == "cd_session"))

    paused, release = threading.Event(), threading.Event()
    real_factory = app.provider_factory

    def slow_factory(creds):
        box = real_factory(creds)
        real_get = box.get_metadata

        def get_metadata(ids):
            paused.set()
            release.wait(5)  # hold the scan mid-read until the test has disconnected
            return real_get(ids)
        box.get_metadata = get_metadata
        return box

    app.provider_factory = slow_factory
    app.ledger().forget(ME)  # make the rescan actually read mail
    assert c.call("/api/scan", {"years": 5})[0] == 202
    assert paused.wait(5)
    c.call("/api/disconnect", {})
    release.set()
    time.sleep(0.5)

    assert app.accounts.cards(acc.id) == []
    assert app.accounts.connection(acc.id) is None
    assert app.ledger().known(ME) == {}
    assert c.call("/api/status")[1]["state"] == "idle"
