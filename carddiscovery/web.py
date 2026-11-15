"""Local web app that mimics the mobile flow, for the proof of concept.

    carddiscovery serve            → opens http://127.0.0.1:<port>/?t=<token>

Security for a local server that reads your Gmail:
- listens on 127.0.0.1 only (never the network);
- a random token is required on every request, so other pages and other local
  users can't drive it;
- the Host header must be 127.0.0.1/localhost, which blocks DNS-rebinding attacks;
- no CORS headers, so browsers block cross-site reads;
- responses carry only derived card facts and explanations, never email text.
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .explain import explain
from .ledger import Ledger
from .registry import Registry
from .score import group

GROUP_ORDER = ["owned", "review", "history", "rejected"]


class App:
    """State shared by request handlers: one scan at a time, latest result in memory."""

    def __init__(self, registry: Registry, credentials: Path, state_dir: Path,
                 names: list[str], test_senders: list[str], provider_factory=None):
        self.registry = registry
        self.credentials = credentials
        self.state_dir = state_dir
        self.names = names
        self.test_senders = test_senders
        self.provider_factory = provider_factory or self._gmail
        self.lock = threading.Lock()
        self.state = "idle"          # idle | running | done | error
        self.progress: list[str] = []
        self.error: str | None = None
        self.mailbox: str | None = None
        self.result = None           # pipeline.RunResult

    # ---- scanning ------------------------------------------------------
    def start_scan(self, years: int, names: list[str]) -> bool:
        with self.lock:
            if self.state == "running":
                return False
            self.state, self.progress, self.error = "running", ["Signing in to Google…"], None
        threading.Thread(target=self._scan, args=(years, names), daemon=True).start()
        return True

    def _gmail(self):
        from .providers.gmail import GmailProvider, authorize
        return GmailProvider(authorize(self.credentials))

    def _scan(self, years: int, names: list[str]) -> None:
        from .pipeline import discover
        try:
            provider = self.provider_factory()
            self.mailbox = provider.mailbox()
            self.progress.append(f"Signed in as {self.mailbox}")
            res = discover(
                provider, self.registry, Ledger(self.state_dir / "ledger.db"),
                user_names=names or self.names, history_years=years,
                progress=lambda m: self.progress.append(m.capitalize()),
            )
            self.result = res
            self.progress.append(f"Done in {res.stats.seconds}s")
            self.state = "done"
        except Exception as e:  # shown to the user; no email content can be in it
            self.error = f"{type(e).__name__}: {e}"
            self.state = "error"

    # ---- answers -------------------------------------------------------
    def decide(self, card_key: str, answer: str | None) -> None:
        if not self.result or not self.mailbox:
            raise ValueError("no scan yet")
        Ledger(self.state_dir / "ledger.db").decide(self.mailbox, card_key, answer)
        for c in self.result.cards:
            if c.decision_key == card_key:
                c.decision = answer

    def forget(self) -> dict:
        from .providers.gmail import revoke
        revoked = revoke(self.state_dir / "token.json")
        for f in ("token.json", "ledger.db"):
            p = self.state_dir / f
            if p.exists():
                p.unlink()
        self.state, self.result, self.mailbox, self.progress = "idle", None, None, []
        return {"revoked": revoked}

    # ---- views ---------------------------------------------------------
    def status(self) -> dict:
        out = {"state": self.state, "progress": self.progress[-8:], "error": self.error}
        if self.state == "done" and self.result:
            out["result"] = self.result_view()
        return out

    def result_view(self) -> dict:
        res = self.result
        groups: dict[str, list] = {g: [] for g in GROUP_ORDER}
        for c in sorted(res.cards, key=lambda c: (c.bank_id, c.last4 or "")):
            e = explain(c, self.registry)
            groups[group(c)].append({
                "key": c.decision_key, "title": e.title, "status": e.status, "headline": e.headline,
                "evidence": e.evidence, "concerns": e.concerns, "question": e.question,
                "decision": c.decision,
            })
        apps = [{"bank": self.registry.banks[a.bank_id].name if a.bank_id in self.registry.banks else a.bank_id,
                 "product": a.product, "status": a.status} for a in res.applications]
        s = res.stats
        return {"mailbox": res.mailbox, "groups": groups, "applications": apps,
                "stats": {"seconds": s.seconds, "emails_read": s.metadata_fetched,
                          "full_emails": s.bodies_fetched, "searches": s.queries}}

    def banks(self) -> list[str]:
        return sorted(b.name for b in self.registry.banks.values() if b.domains)


def make_handler(app: App, token: str, port_ref: list[int]):
    page = resources.files("carddiscovery.data").joinpath("app.html").read_text()

    class Handler(BaseHTTPRequestHandler):
        server_version = "carddiscovery"

        def log_message(self, fmt, *args):  # keep the terminal quiet; never log bodies
            pass

        # -- guards
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in {f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}"}

        def _token_ok(self, query: dict) -> bool:
            given = self.headers.get("X-Token") or (query.get("t") or [""])[0]
            return secrets.compare_digest(given, token)

        def _send(self, code: int, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self) -> dict | None:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if not self._host_ok():
                self._send(403, {"error": "bad host"})
                return None
            if not self._token_ok(query):
                self._send(403, {"error": "missing or wrong token"})
                return None
            return {"path": url.path, "query": query}

        def _json_body(self) -> dict:
            n = min(int(self.headers.get("Content-Length") or 0), 10_000)
            return json.loads(self.rfile.read(n) or b"{}")

        # -- routes
        def do_GET(self):
            req = self._guard()
            if not req:
                return
            if req["path"] == "/":
                self._send(200, page.replace("__TOKEN__", token).replace(
                    "__NAMES__", json.dumps(", ".join(app.names))), "text/html")
            elif req["path"] == "/api/banks":
                self._send(200, {"banks": app.banks(), "test_mode": bool(app.test_senders)})
            elif req["path"] == "/api/status":
                self._send(200, app.status())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            req = self._guard()
            if not req:
                return
            try:
                body = self._json_body()
                if req["path"] == "/api/scan":
                    years = int(body.get("years", 5))
                    if years not in (1, 3, 5):
                        raise ValueError("years must be 1, 3 or 5")
                    names = [n.strip() for n in str(body.get("name", "")).split(",") if n.strip()]
                    ok = app.start_scan(years, names)
                    self._send(202 if ok else 409, {"started": ok})
                elif req["path"] == "/api/decide":
                    answer = body.get("answer")
                    if answer not in ("yes", "no", None):
                        raise ValueError("answer must be yes, no or null")
                    app.decide(str(body["key"]), answer)
                    self._send(200, app.status())
                elif req["path"] == "/api/forget":
                    self._send(200, app.forget())
                else:
                    self._send(404, {"error": "not found"})
            except (ValueError, KeyError) as e:
                self._send(400, {"error": str(e)})

    return Handler


def make_server(app: App, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    token = secrets.token_urlsafe(24)
    port_ref = [port]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, token, port_ref))
    port_ref[0] = httpd.server_address[1]
    return httpd, token


def serve(app: App, port: int = 0, open_browser: bool = True) -> None:
    httpd, token = make_server(app, port)
    port_ref = [httpd.server_address[1]]
    url = f"http://127.0.0.1:{port_ref[0]}/?t={token}"
    print(f"Card discovery is running at:\n  {url}\nPress Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
