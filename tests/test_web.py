"""The local web app: guarded API, scan, answers, disconnect."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from carddiscovery.registry import Registry
from carddiscovery.web import App, make_server
from tests.test_run import FakeGmail
from tests.test_test_mode import ME, _kit, selfmail


class Box(FakeGmail):
    def mailbox(self):
        return ME

    def user_addresses(self):
        return [ME]


@pytest.fixture
def server(tmp_path):
    msgs = []
    for i, (s, b) in enumerate(_kit()):
        m = selfmail(s, b)
        m.message_id, m.date = f"k{i}", datetime.now(timezone.utc) - timedelta(minutes=i)
        msgs.append(m)
    (tmp_path / "token.json").write_text("{}")
    app = App(Registry.load(test_senders=[ME]), tmp_path / "creds.json", tmp_path, ["Shubham Kadam"], [ME],
              provider_factory=lambda: Box(msgs))
    httpd, token = make_server(app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", token, tmp_path
    httpd.shutdown()


def call(base, path, token=None, body=None, host=None):
    req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("X-Token", token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}") if "json" in r.headers["Content-Type"] else r.read()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_requests_without_token_or_with_foreign_host_are_refused(server):
    base, token, _ = server
    assert call(base, "/api/status")[0] == 403
    assert call(base, "/api/status", token="wrong")[0] == 403
    assert call(base, "/api/status", token=token, host="evil.example:80")[0] == 403
    assert call(base, "/api/status", token=token)[0] == 200


def test_page_is_served_with_the_token(server):
    base, token, _ = server
    code, html = call(base, f"/?t={token}")
    assert code == 200 and token.encode() in html and b"__TOKEN__" not in html


def test_scan_answer_and_disconnect(server):
    base, token, tmp = server
    assert call(base, "/api/scan", token, {"years": 5, "name": "Shubham Kadam"})[0] == 202
    for _ in range(50):
        status = call(base, "/api/status", token)[1]
        if status["state"] == "done":
            break
        time.sleep(0.1)
    groups = status["result"]["groups"]
    assert len(groups["owned"]) == 6 and len(groups["review"]) == 5 and len(groups["history"]) == 2
    # Only derived facts and explanations leave the server: no subject or body text.
    raw = json.dumps(status)
    assert not any(subject in raw for subject, _ in _kit())
    assert set(groups["owned"][0]) == {"key", "title", "status", "headline", "evidence", "concerns",
                                       "question", "decision"}
    key = next(c["key"] for c in groups["review"] if c["key"] == "icici:9191")
    after = call(base, "/api/decide", token, {"key": key, "answer": "no"})[1]
    assert [c["key"] for c in after["result"]["groups"]["rejected"]] == ["icici:9191"]
    assert call(base, "/api/decide", token, {"key": key, "answer": "maybe"})[0] == 400
    assert call(base, "/api/scan", token, {"years": 7})[0] == 400
    gone = call(base, "/api/forget", token, {})[1]
    assert not (tmp / "ledger.db").exists() and not (tmp / "token.json").exists()
    assert "revoked" in gone
