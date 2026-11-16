"""API service (FastAPI). Serves the phone-frame page and the endpoints it calls.

It can store a Gmail token (sealed with the vault) but never decrypt one: every
Gmail call after the first sign-in happens in the workers.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from carddiscovery.providers import gmail as g
from carddiscovery.registry import Registry

from . import auth, cards, config, db, queue
from .auth import SESSION_TTL
from .vault import Vault

COOKIE = "cd_session"
OAUTH_TTL = 600


class Api:
    def __init__(self, registry: Registry, names: list[str]):
        self.registry = registry
        self.names = names
        self.vault = Vault("api")
        self.token = secrets.token_urlsafe(24)
        self.pending: dict[str, dict] = {}
        self.lock = threading.Lock()

    # ---- Gmail connect: the API receives Google's one-time code and seals the token
    def begin_connect(self, acc: dict, redirect_uri: str, years: int, names: list[str], pdf_opt_in: bool,
                      dob: str | None) -> str:
        url, state, flow = g.start_web_flow(config.GOOGLE_CLIENT_FILE, redirect_uri)
        now = time.time()
        with self.lock:
            self.pending = {s: p for s, p in self.pending.items() if now - p["at"] < OAUTH_TTL}
            self.pending[state] = {"flow": flow, "account": acc["id"], "years": years, "names": names,
                                   "pdf": pdf_opt_in, "dob": dob, "at": now}
        return url

    def finish_connect(self, c, acc: dict, state: str, code: str | None, error: str | None) -> str:
        with self.lock:
            p = self.pending.pop(state, None)
        if not p or p["account"] != acc["id"] or time.time() - p["at"] > OAUTH_TTL:
            return "expired"
        if error or not code:
            return "denied"
        flow = p["flow"]
        import os
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        flow.fetch_token(code=code)
        creds = flow.credentials
        granted = set(getattr(creds, "granted_scopes", None) or creds.scopes or [])
        if g.SCOPES[0] not in granted:
            return "noscope"
        mailbox = g.GmailProvider(creds).mailbox()
        blob = self.vault.seal(creds.to_json().encode(), f"gmail:{acc['id']}")
        dob_blob = self.vault.seal(p["dob"].encode(), f"dob:{acc['id']}") if p["dob"] else None
        with c.transaction():
            old = c.execute("SELECT mailbox, status FROM gmail_connections WHERE account_id = %s",
                            (acc["id"],)).fetchone()
            if old and old["status"] == "disconnecting":
                return "expired"
            if old and old["mailbox"] != mailbox:  # a different Gmail account: start clean
                for t in ("evidence", "statement_facts", "decisions"):
                    c.execute(f"DELETE FROM {t} WHERE account_id = %s", (acc["id"],))
                c.execute("DELETE FROM cards WHERE account_id = %s AND source = 'gmail'", (acc["id"],))
            c.execute(
                "INSERT INTO gmail_connections (account_id, mailbox, token_blob, scopes, years, names, pdf_opt_in, "
                "dob_blob) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (account_id) DO UPDATE SET "
                "mailbox = EXCLUDED.mailbox, token_blob = EXCLUDED.token_blob, scopes = EXCLUDED.scopes, "
                "years = EXCLUDED.years, names = EXCLUDED.names, pdf_opt_in = EXCLUDED.pdf_opt_in, "
                "dob_blob = coalesce(EXCLUDED.dob_blob, gmail_connections.dob_blob), status = 'active', "
                "cancel_requested = false, connected_at = now()",
                (acc["id"], mailbox, blob, sorted(granted), p["years"], p["names"] or self.names, p["pdf"], dob_blob))
            queue.enqueue(c, "discover_quick", acc["id"])
        return "scanning"

    # ---- views
    def status(self, c, acc: dict) -> dict:
        jobs = c.execute("SELECT DISTINCT ON (kind) kind, status, progress, last_error, units_used, "
                         "extract(epoch FROM coalesce(finished_at, now()) - started_at) AS secs "
                         "FROM jobs WHERE account_id = %s ORDER BY kind, id DESC", (acc["id"],)).fetchall()
        by = {j["kind"]: j for j in jobs}
        quick = by.get("discover_quick")
        if not quick:
            return {"state": "idle", "progress": [], "error": None}
        msgs = list((quick["progress"] or {}).get("messages", []))
        bg = [by[k] for k in ("discover_full", "pdf_enrich", "daily_sync") if k in by]
        for b in bg:
            if b["status"] in ("queued", "running"):
                msgs += (b["progress"] or {}).get("messages", [])[-2:] or ["Checking older emails in the background"]
        if quick["status"] in ("queued", "running"):
            return {"state": "running", "progress": msgs[-8:] or ["Waiting for a worker"], "error": None}
        if quick["status"] == "failed":
            return {"state": "error", "progress": msgs[-8:], "error": quick["last_error"]}
        if quick["status"] == "cancelled":
            return {"state": "idle", "progress": [], "error": None}
        return {"state": "done", "progress": msgs[-8:], "error": None, "result": self.result(c, acc, by)}

    def result(self, c, acc: dict, by: dict | None = None) -> dict:
        conn = c.execute("SELECT mailbox FROM gmail_connections WHERE account_id = %s", (acc["id"],)).fetchone()
        st = None
        if by:
            done = [by[k] for k in ("discover_quick", "discover_full") if k in by]
            s = [(j["progress"] or {}).get("stats", {}) for j in done]
            units = sum(j["units_used"] for j in done)
            running = any(j["status"] in ("queued", "running") for j in by.values())
            st = {"seconds": round(sum(x.get("seconds", 0) for x in s), 1),
                  "emails_read": sum(x.get("header_reads", 0) for x in s),
                  "full_emails": sum(x.get("full_reads", 0) for x in s),
                  "searches": sum(x.get("searches", 0) for x in s), "units": units, "background": running}
        return {"mailbox": conn and conn["mailbox"], "groups": cards.view(c, acc["id"]),
                "applications": cards.applications(c, acc["id"], self.registry), "stats": st}

    def me(self, c, acc: dict) -> dict:
        conn = c.execute("SELECT mailbox, status FROM gmail_connections WHERE account_id = %s",
                         (acc["id"],)).fetchone()
        n = c.execute("SELECT count(*) AS n FROM cards WHERE account_id = %s", (acc["id"],)).fetchone()["n"]
        notes = c.execute("SELECT kind, detail FROM notifications WHERE account_id = %s AND NOT seen "
                          "ORDER BY id DESC LIMIT 5", (acc["id"],)).fetchall()
        return {"user": acc["external_id"], "mailbox": conn and conn["mailbox"],
                "connected": bool(conn and conn["status"] == "active"),
                "needs_reconnect": bool(conn and conn["status"] == "needs_reconnect"),
                "saved_cards": n, "notifications": notes}

    def disconnect(self, c, acc: dict, delete_account: bool = False) -> dict:
        with c.transaction():
            conn = c.execute("UPDATE gmail_connections SET status = 'disconnecting', cancel_requested = true "
                             "WHERE account_id = %s RETURNING account_id", (acc["id"],)).fetchone()
            queue.cancel_queued(c, acc["id"])
            c.execute("DELETE FROM cards WHERE account_id = %s AND source = 'gmail'", (acc["id"],))
            if delete_account:
                c.execute("UPDATE accounts SET deleting = true WHERE id = %s", (acc["id"],))
                c.execute("DELETE FROM sessions WHERE account_id = %s", (acc["id"],))
            if conn:  # a worker revokes at Google and deletes the rest
                queue.enqueue(c, "disconnect", acc["id"], {"delete_account": delete_account})
            elif delete_account:
                c.execute("DELETE FROM accounts WHERE id = %s", (acc["id"],))
        return {"revoking": bool(conn)}


def create_app(registry: Registry, names: list[str]) -> tuple[FastAPI, Api]:
    api = Api(registry, names)
    page = (Path(__file__).parent / "static" / "app.html").read_text()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    origin = f"http://127.0.0.1:{config.API_PORT}"

    def ok_host(req: Request) -> bool:
        return (req.headers.get("host") or "").lower() in {f"127.0.0.1:{config.API_PORT}", f"localhost:{config.API_PORT}"}

    def ok_token(req: Request) -> bool:
        given = req.headers.get("x-token") or req.query_params.get("t") or ""
        return secrets.compare_digest(given, api.token)

    def j(code: int, body) -> JSONResponse:
        return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def guard(req: Request, call_next):
        if not ok_host(req):
            return j(403, {"error": "bad host"})
        if req.url.path != "/oauth/callback" and not ok_token(req):
            return j(403, {"error": "missing or wrong token"})
        resp = await call_next(req)
        resp.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                             "X-Frame-Options": "DENY", "Cache-Control": "no-store",
                             "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
                             "frame-ancestors 'none'; form-action 'none'; base-uri 'none'"})
        return resp

    def account(c, req: Request) -> dict | None:
        return auth.session(c, req.cookies.get(COOKIE))

    async def body(req: Request) -> dict:
        raw = await req.body()
        return json.loads(raw[:10_000] or b"{}")

    @app.exception_handler(ValueError)
    async def val_err(req, e):
        return j(400, {"error": str(e)})

    @app.get("/")
    def index():
        return HTMLResponse(page.replace("__TOKEN__", api.token).replace("__NAMES__", json.dumps(", ".join(names))))

    @app.get("/api/config")
    def cfg():
        return {"banks": sorted(b.name for b in registry.banks.values() if b.domains),
                "test_mode": bool(registry.test_senders)}

    # Stand-in for the separate login service: takes its user id, issues our session.
    @app.post("/api/dev/login")
    async def dev_login(req: Request):
        b = await body(req)
        with db.pool().connection() as c:
            acc, token = auth.login(c, str(b.get("user", "")))
            r = j(200, api.me(c, acc))
        r.set_cookie(COOKIE, token, httponly=True, samesite="lax", path="/", max_age=SESSION_TTL)
        return r

    def authed(handler):
        async def wrapper(req: Request):
            with db.pool().connection() as c:
                acc = account(c, req)
                if not acc:
                    return j(401, {"error": "login required"})
                b = await body(req) if req.method == "POST" else {}
                return handler(c, acc, b, req)
        wrapper.__name__ = handler.__name__
        return wrapper

    @app.get("/api/me")
    @authed
    def me(c, acc, b, req):
        return api.me(c, acc)

    @app.get("/api/status")
    @authed
    def status(c, acc, b, req):
        return api.status(c, acc)

    @app.get("/api/cards")
    @authed
    def saved(c, acc, b, req):
        return api.result(c, acc)

    @app.post("/api/logout")
    @authed
    def logout(c, acc, b, req):
        auth.logout(c, req.cookies.get(COOKIE))
        r = j(200, {"ok": True})
        r.delete_cookie(COOKIE, path="/")
        return r

    def years_of(b) -> int:
        years = int(b.get("years", 3))
        if years not in (1, 3, 5):
            raise ValueError("years must be 1, 3 or 5")
        return years

    @app.post("/api/connect")
    @authed
    def connect(c, acc, b, req):
        names_ = [n.strip() for n in str(b.get("name", "")).split(",") if n.strip()]
        dob = b.get("dob") or None
        if dob:
            from datetime import date
            d = date.fromisoformat(str(dob))  # ValueError -> 400
            if not date(1920, 1, 1) <= d <= date(2012, 12, 31):
                raise ValueError("date of birth looks wrong")
            dob = d.isoformat()
        url = api.begin_connect(acc, f"{origin}/oauth/callback", years_of(b), names_, bool(b.get("pdf")), dob)
        return {"url": url}

    @app.get("/oauth/callback")
    def callback(req: Request):
        q = req.query_params
        screen = "expired"
        with db.pool().connection() as c:
            acc = account(c, req)
            if acc and q.get("state"):
                screen = api.finish_connect(c, acc, q["state"], q.get("code"), q.get("error"))
        return RedirectResponse(f"{origin}/?t={quote(api.token)}#{screen}", status_code=303)

    @app.post("/api/scan")
    @authed
    def rescan(c, acc, b, req):
        conn = c.execute("SELECT status FROM gmail_connections WHERE account_id = %s", (acc["id"],)).fetchone()
        if not conn or conn["status"] != "active":
            return j(409, {"error": "Gmail isn't connected, or access has expired. Connect again.", "reconnect": True})
        c.execute("UPDATE gmail_connections SET years = %s WHERE account_id = %s", (years_of(b), acc["id"]))
        started = queue.enqueue(c, "discover_quick", acc["id"]) is not None
        return j(202 if started else 409, {"started": started})

    @app.post("/api/decide")
    @authed
    def decide(c, acc, b, req):
        answer, key = b.get("answer"), str(b["key"])
        if answer not in ("yes", "no", None):
            raise ValueError("answer must be yes, no or null")
        if answer is None:
            c.execute("DELETE FROM decisions WHERE account_id = %s AND card_key = %s", (acc["id"], key))
        else:
            c.execute("INSERT INTO decisions (account_id, card_key, decision) VALUES (%s, %s, %s) ON CONFLICT "
                      "(account_id, card_key) DO UPDATE SET decision = EXCLUDED.decision, decided_at = now()",
                      (acc["id"], key, answer))
        cards.rebuild(c, acc["id"], registry)
        return api.status(c, acc)

    @app.post("/api/disconnect")
    @authed
    def disconnect(c, acc, b, req):
        return api.disconnect(c, acc)

    @app.post("/api/delete_account")
    @authed
    def delete_account(c, acc, b, req):
        r = j(200, api.disconnect(c, acc, delete_account=True))
        r.delete_cookie(COOKIE, path="/")
        return r

    # ---- dev: what each job cost (units, time); no email content
    @app.get("/api/dev/jobs")
    def dev_jobs():
        with db.pool().connection() as c:
            rows = c.execute("SELECT id, kind, account_id, status, attempts, units_used, last_error, "
                             "progress->'stats' AS stats, progress->'meter' AS meter, "
                             "round(extract(epoch FROM finished_at - started_at)::numeric, 2) AS seconds "
                             "FROM jobs ORDER BY id DESC LIMIT 30").fetchall()
            day = c.execute("SELECT units FROM quota_daily WHERE day = current_date").fetchone()
        return {"jobs": rows, "units_today": day and day["units"]}

    return app, api
