from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from carddiscovery.registry import Registry
from cdserver import config
from cdserver.api import create_app


@pytest.fixture
def client(c):
    app, api = create_app(Registry.load(), ["Test User"])
    cl = TestClient(app, base_url=f"http://127.0.0.1:{config.API_PORT}")
    cl.headers["Origin"] = config.FRONTEND_ORIGIN
    return cl


def test_login_comes_from_the_other_service_not_otp(client):
    assert client.post("/api/otp/send", json={"phone": "9876543210"}).status_code == 404
    assert client.get("/api/me").status_code == 401
    me = client.post("/api/dev/login", json={"user": "dev-user-1"}).json()
    assert me["user"] == "dev-user-1" and me["connected"] is False
    assert client.get("/api/status").json()["state"] == "idle"


def test_only_the_frontend_page_may_call_the_api(client):
    assert client.get("/api/config", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/config", headers={"Host": "evil.example"}).status_code == 403
    r = client.get("/api/config")
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == config.FRONTEND_ORIGIN
    pre = client.options("/api/connect", headers={"Access-Control-Request-Method": "POST"})
    assert pre.status_code == 200 and pre.headers["access-control-allow-credentials"] == "true"


def test_google_sign_in_returns_to_the_frontend(client):
    r = client.get("/oauth/callback", headers={"Origin": ""}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == config.FRONTEND_ORIGIN + "/#expired"


def test_bad_user_id_is_rejected(client):
    assert client.post("/api/dev/login", json={"user": "a b; drop"}).status_code == 400


def test_delete_account_without_gmail_removes_it_at_once(client, c):
    client.post("/api/dev/login", json={"user": "dev-user-1"})
    assert client.post("/api/delete_account", json={}).status_code == 200
    assert c.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"] == 0
