"""Local web app that walks through the real product flow, in a phone frame.

    carddiscovery serve      → opens http://127.0.0.1:<port>/?t=<token>

Flow (what the mobile app will do):
  1. Log in with phone number + OTP          (accounts.AccountStore)
  2. Our explainer: which banks, how far back, what's kept
  3. Google's own consent screen (redirect)  (providers.gmail.start_web_flow)
     - Google handles the Google password and Google 2FA; we never see them
     - state + PKCE protect the callback; unticked Gmail access is detected
  4. Scan, then review with Yes/No
  5. Card facts saved to the logged-in account; disconnect / delete any time

Local-server protections:
- listens on 127.0.0.1 only; Host header must be 127.0.0.1/localhost (no DNS rebinding)
- every API call needs the per-run app token (X-Token), plus a session cookie
  (HttpOnly, SameSite=Lax) after login
- the OAuth callback is authorised by its single-use `state` and must match the
  logged-in account
- no CORS headers; strict Content-Security-Policy; never returns email text
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .accounts import Account, AccountStore, OtpError, SESSION_TTL, mask_phone
from .explain import explain
from .ledger import Ledger
from .registry import Registry
from .score import group

GROUP_ORDER = ["owned", "review", "history", "rejected"]
OAUTH_PENDING_TTL = 600
COOKIE = "cd_session"


@dataclass
class Scan:
    state: str = "idle"          # idle | running | done | error
    progress: list[str] = field(default_factory=list)
    error: str | None = None
    mailbox: str | None = None
    result: object = None        # pipeline.RunResult


class App:
    def __init__(self, registry: Registry, credentials: Path, state_dir: Path, names: list[str],
                 test_senders: list[str], accounts: AccountStore | None = None,
                 provider_factory=None, oauth_start=None, oauth_finish=None, creds_loader=None):
        from .providers import gmail
        self.registry = registry
        self.credentials = credentials
        self.state_dir = state_dir
        self.names = names
        self.test_senders = test_senders
        self.accounts = accounts or AccountStore(state_dir / "accounts.db")
        self.provider_factory = provider_factory or (lambda creds: gmail.GmailProvider(creds))
        self.oauth_start = oauth_start or gmail.start_web_flow
        self.oauth_finish = oauth_finish or gmail.finish_web_flow
        self.creds_loader = creds_loader or gmail.load_creds
        self.pending: dict[str, dict] = {}       # oauth state -> {flow, account, years, names, at}
        self.scans: dict[int, Scan] = {}
        self.lock = threading.Lock()

    def token_file(self, account_id: int) -> Path:
        return self.state_dir / "tokens" / f"{account_id}.json"

    def ledger(self) -> Ledger:
        return Ledger(self.state_dir / "ledger.db")

    # ---- Google permission ----------------------------------------------
    def begin_connect(self, acc: Account, redirect_uri: str, years: int, names: list[str]) -> str:
        url, state, flow = self.oauth_start(self.credentials, redirect_uri)
        now = time.time()
        with self.lock:
            self.pending = {s: p for s, p in self.pending.items() if now - p["at"] < OAUTH_PENDING_TTL}
            self.pending[state] = {"flow": flow, "account": acc.id, "years": years, "names": names, "at": now}
        return url

    def finish_connect(self, acc: Account, state: str, code: str | None, error: str | None) -> str:
        """Returns the screen to show next: scanning | denied | noscope | expired."""
        from .providers.gmail import ScopeNotGranted
        with self.lock:
            p = self.pending.pop(state, None)  # single use
        if not p or p["account"] != acc.id or time.time() - p["at"] > OAUTH_PENDING_TTL:
            return "expired"
        if error or not code:
            return "denied"
        try:
            creds = self.oauth_finish(p["flow"], code, self.token_file(acc.id))
        except ScopeNotGranted:
            return "noscope"
        self.start_scan(acc, p["years"], p["names"], creds)
        return "scanning"

    # ---- scanning --------------------------------------------------------
    def start_scan(self, acc: Account, years: int, names: list[str], creds=None) -> bool:
        creds = creds or self.creds_loader(self.token_file(acc.id))
        if creds is None:
            raise PermissionError("Gmail isn't connected, or access has expired. Connect again.")
        with self.lock:
            scan = self.scans.get(acc.id)
            if scan and scan.state == "running":
                return False
            self.scans[acc.id] = scan = Scan("running", ["Connected to Gmail"])
        threading.Thread(target=self._scan, args=(acc, scan, years, names, creds), daemon=True).start()
        return True

    def _scan(self, acc: Account, scan: Scan, years: int, names: list[str], creds) -> None:
        from .pipeline import discover
        try:
            provider = self.provider_factory(creds)
            scan.mailbox = provider.mailbox()
            self.accounts.set_connection(acc.id, scan.mailbox)
            scan.progress.append(f"Reading bank emails in {scan.mailbox}")
            scan.result = discover(provider, self.registry, self.ledger(),
                                   user_names=names or self.names, history_years=years,
                                   progress=lambda m: scan.progress.append(m.capitalize()))
            self.accounts.save_cards(acc.id, self._flat(scan))
            scan.progress.append(f"Done in {scan.result.stats.seconds}s")
            scan.state = "done"
        except Exception as e:  # message only; never email content
            scan.error, scan.state = f"{type(e).__name__}: {e}", "error"

    # ---- answers, disconnect, delete ------------------------------------
    def decide(self, acc: Account, key: str, answer: str | None) -> None:
        scan = self.scans.get(acc.id)
        mailbox = (scan and scan.mailbox) or self.accounts.connection(acc.id)
        if not mailbox:
            raise ValueError("no scan yet")
        self.ledger().decide(mailbox, key, answer)
        if scan and scan.result:
            for c in scan.result.cards:
                if c.decision_key == key:
                    c.decision = answer
            self.accounts.save_cards(acc.id, self._flat(scan))

    def disconnect(self, acc: Account) -> dict:
        from .providers.gmail import revoke
        tf = self.token_file(acc.id)
        revoked = revoke(tf)
        if tf.exists():
            tf.unlink()
        mailbox = self.accounts.connection(acc.id)
        if mailbox:
            self.ledger().forget(mailbox, keep_decisions=False)
        self.accounts.delete_cards(acc.id, source="gmail")
        self.accounts.set_connection(acc.id, None)
        self.scans.pop(acc.id, None)
        return {"revoked": revoked}

    def delete_account(self, acc: Account) -> dict:
        out = self.disconnect(acc)
        self.accounts.delete_account(acc.id)
        return out

    # ---- views -----------------------------------------------------------
    def _cards_view(self, scan: Scan) -> dict:
        groups: dict[str, list] = {g: [] for g in GROUP_ORDER}
        for c in sorted(scan.result.cards, key=lambda c: (c.bank_id, c.last4 or "")):
            e = explain(c, self.registry)
            groups[group(c)].append({
                "key": c.decision_key, "title": e.title, "status": e.status, "headline": e.headline,
                "evidence": e.evidence, "concerns": e.concerns, "question": e.question,
                "decision": c.decision, "bank": c.bank_id, "last4": c.last4, "product": c.product,
            })
        return groups

    def _flat(self, scan: Scan) -> list[dict]:
        return [{**c, "group": g} for g, cs in self._cards_view(scan).items() for c in cs]

    def status(self, acc: Account) -> dict:
        scan = self.scans.get(acc.id) or Scan()
        out = {"state": scan.state, "progress": scan.progress[-8:], "error": scan.error}
        if scan.state == "done" and scan.result:
            out["result"] = self.result_view(scan)
        return out

    def result_view(self, scan: Scan) -> dict:
        res = scan.result
        apps = [{"bank": self.registry.banks[a.bank_id].name if a.bank_id in self.registry.banks else a.bank_id,
                 "product": a.product, "status": a.status} for a in res.applications]
        s = res.stats
        return {"mailbox": res.mailbox, "groups": self._cards_view(scan), "applications": apps,
                "stats": {"seconds": s.seconds, "emails_read": s.metadata_fetched,
                          "full_emails": s.bodies_fetched, "searches": s.queries}}

    def saved_view(self, acc: Account) -> dict:
        """Cards saved on the account (what the app shows after a restart)."""
        groups: dict[str, list] = {g: [] for g in GROUP_ORDER}
        for c in self.accounts.cards(acc.id):
            groups.setdefault(c.get("group", "review"), []).append(c)
        return {"mailbox": self.accounts.connection(acc.id), "groups": groups, "applications": [],
                "stats": None}

    def me(self, acc: Account) -> dict:
        return {"phone": mask_phone(acc.phone), "mailbox": self.accounts.connection(acc.id),
                "connected": self.token_file(acc.id).exists(), "saved_cards": len(self.accounts.cards(acc.id))}

    def banks(self) -> list[str]:
        return sorted(b.name for b in self.registry.banks.values() if b.domains)


def make_handler(app: App, token: str, port_ref: list[int]):
    page = resources.files("carddiscovery.data").joinpath("app.html").read_text()

    class Handler(BaseHTTPRequestHandler):
        server_version = "carddiscovery"

        def log_message(self, fmt, *args):  # never log request bodies (phone numbers, codes)
            pass

        # ---- helpers
        def _origin(self) -> str:
            return f"http://127.0.0.1:{port_ref[0]}"

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").lower() in {
                f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}"}

        def _session_token(self) -> str | None:
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return c[COOKIE].value if COOKIE in c else None

        def _account(self) -> Account | None:
            return app.accounts.session(self._session_token())

        def _send(self, code: int, body, ctype="application/json", headers: dict | None = None):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                             "form-action 'none'; base-uri 'none'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str, headers: dict | None = None):
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _parse(self):
            url = urlparse(self.path)
            return url.path, parse_qs(url.query)

        def _app_token_ok(self, query) -> bool:
            given = self.headers.get("X-Token") or (query.get("t") or [""])[0]
            return secrets.compare_digest(given, token)

        def _body(self) -> dict:
            n = min(int(self.headers.get("Content-Length") or 0), 10_000)
            return json.loads(self.rfile.read(n) or b"{}")

        # ---- GET
        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            path, query = self._parse()
            if path == "/oauth/callback":
                return self._oauth_callback(query)
            if not self._app_token_ok(query):
                return self._send(403, {"error": "missing or wrong token"})
            if path == "/":
                return self._send(200, page.replace("__TOKEN__", token).replace(
                    "__NAMES__", json.dumps(", ".join(app.names))), "text/html")
            if path == "/api/config":
                from .accounts import ConsoleSender
                return self._send(200, {"banks": app.banks(), "test_mode": bool(app.test_senders),
                                        "otp_dev": isinstance(app.accounts.sender, ConsoleSender)})
            acc = self._account()
            if not acc:
                return self._send(401, {"error": "login required"})
            if path == "/api/me":
                return self._send(200, app.me(acc))
            if path == "/api/status":
                return self._send(200, app.status(acc))
            if path == "/api/cards":
                return self._send(200, app.saved_view(acc))
            return self._send(404, {"error": "not found"})

        def _oauth_callback(self, query):
            # Authorised by the single-use state (unguessable) + the session cookie.
            acc = self._account()
            state = (query.get("state") or [""])[0]
            screen = "expired"
            if acc and state:
                screen = app.finish_connect(acc, state, (query.get("code") or [None])[0],
                                            (query.get("error") or [None])[0])
            self._redirect(f"{self._origin()}/?t={quote(token)}#{screen}")

        # ---- POST
        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            path, query = self._parse()
            if not self._app_token_ok(query):
                return self._send(403, {"error": "missing or wrong token"})
            try:
                body = self._body()
                if path == "/api/otp/send":
                    return self._send(200, app.accounts.send_otp(str(body.get("phone", ""))))
                if path == "/api/otp/verify":
                    acc, session = app.accounts.verify_otp(str(body.get("phone", "")), str(body.get("code", "")))
                    cookie = (f"{COOKIE}={session}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_TTL}")
                    return self._send(200, app.me(acc), headers={"Set-Cookie": cookie})
                acc = self._account()
                if not acc:
                    return self._send(401, {"error": "login required"})
                if path == "/api/logout":
                    app.accounts.logout(self._session_token())
                    return self._send(200, {"ok": True},
                                      headers={"Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"})
                if path in ("/api/connect", "/api/scan"):
                    years = int(body.get("years", 5))
                    if years not in (1, 3, 5):
                        raise ValueError("years must be 1, 3 or 5")
                    names = [n.strip() for n in str(body.get("name", "")).split(",") if n.strip()]
                    if path == "/api/connect":
                        url = app.begin_connect(acc, f"{self._origin()}/oauth/callback", years, names)
                        return self._send(200, {"url": url})
                    started = app.start_scan(acc, years, names)
                    return self._send(202 if started else 409, {"started": started})
                if path == "/api/decide":
                    answer = body.get("answer")
                    if answer not in ("yes", "no", None):
                        raise ValueError("answer must be yes, no or null")
                    app.decide(acc, str(body["key"]), answer)
                    return self._send(200, app.status(acc))
                if path == "/api/disconnect":
                    return self._send(200, app.disconnect(acc))
                if path == "/api/delete_account":
                    out = app.delete_account(acc)
                    return self._send(200, out, headers={
                        "Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"})
                return self._send(404, {"error": "not found"})
            except OtpError as e:
                return self._send(400, {"error": str(e)})
            except PermissionError as e:
                return self._send(409, {"error": str(e), "reconnect": True})
            except (ValueError, KeyError) as e:
                return self._send(400, {"error": str(e)})

    return Handler


def make_server(app: App, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    token = secrets.token_urlsafe(24)
    port_ref = [port]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, token, port_ref))
    port_ref[0] = httpd.server_address[1]
    return httpd, token


def serve(app: App, port: int = 0, open_browser: bool = True) -> None:
    httpd, token = make_server(app, port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?t={token}"
    print(f"Card discovery is running at:\n  {url}\nOTP codes (dev) will be printed here.\nPress Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
