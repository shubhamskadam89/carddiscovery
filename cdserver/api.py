"""API service (FastAPI). Serves the phone-frame page and the endpoints it calls.

It stores the Gmail token as Google returns it (plain JSON, not encrypted); every
Gmail call after the first sign-in happens in the workers.
"""

from __future__ import annotations

import json
import re
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


def is_admin(mailbox: str | None) -> bool:
    """The team: everyone in local development; on the shared server, the Gmail addresses in CD_ADMIN_EMAILS."""
    return config.DEV_LOGIN or bool(mailbox and mailbox.lower() in config.ADMIN_EMAILS)


class Api:
    def __init__(self, registry: Registry, names: list[str]):
        self.registry = registry
        self.names = names
        self.vault = Vault("api")
        self.token = secrets.token_urlsafe(24)
        self.pending: dict[str, dict] = {}
        self.lock = threading.Lock()

    # ---- Gmail connect: the API receives Google's one-time code and stores the token
    def begin_connect(self, acc: dict, redirect_uri: str, years: int, names: list[str], pdf_opt_in: bool,
                      dob: str | None) -> str:
        url, state, flow = g.start_web_flow(config.GOOGLE_CLIENT_FILE, redirect_uri)
        now = time.time()
        with self.lock:
            self.pending = {s: p for s, p in self.pending.items() if now - p["at"] < OAUTH_TTL}
            self.pending[state] = {"flow": flow, "account": acc["id"], "years": years, "names": names,
                                   "pdf": pdf_opt_in, "dob": dob, "at": now}
        return url

    def finish_connect(self, c, acc: dict, state: str, code: str | None, error: str | None) -> tuple[str, int | None]:
        """(screen, account to sign in as). A browser's empty account that connects a Gmail already
        connected before becomes that account: Google has just shown it's the same person."""
        screen, target = self._finish_connect(c, acc, state, code, error)
        return screen, target

    def _finish_connect(self, c, acc: dict, state: str, code: str | None, error: str | None) -> tuple[str, int | None]:
        with self.lock:
            p = self.pending.pop(state, None)
        if not p or p["account"] != acc["id"] or time.time() - p["at"] > OAUTH_TTL:
            return "expired", None
        if error or not code:
            return "denied", None
        flow = p["flow"]
        import os
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        flow.fetch_token(code=code)
        creds = flow.credentials
        granted = set(getattr(creds, "granted_scopes", None) or creds.scopes or [])
        if g.SCOPES[0] not in granted:
            return "noscope", None
        mailbox = g.GmailProvider(creds).mailbox()
        from .auth import is_anonymous
        target = acc["id"]
        if is_anonymous(acc):
            owner = c.execute(
                "SELECT account_id FROM gmail_connections WHERE lower(mailbox) = lower(%s) AND account_id <> %s "
                "ORDER BY (status = 'active') DESC, connected_at DESC LIMIT 1", (mailbox, acc["id"])).fetchone()
            if owner:
                target = owner["account_id"]
                acc = {"id": target}
        blob = creds.to_json().encode()  # stored as is: not encrypted
        dob_blob = self.vault.seal(p["dob"].encode(), f"dob:{acc['id']}") if p["dob"] else None
        with c.transaction():
            old = c.execute("SELECT mailbox, status FROM gmail_connections WHERE account_id = %s",
                            (acc["id"],)).fetchone()
            if old and old["status"] == "disconnecting":
                return "expired", None
            if old and old["mailbox"] != mailbox:  # a different Gmail account: start clean
                for t in ("evidence", "statement_facts", "decisions", "email_reports", "card_index", "set_aside",
                          "statement_pdf_reads"):
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
            if mailbox.lower() != config.REPORT_INBOX:  # the team's report inbox is read, never scanned
                queue.enqueue(c, "discover_quick", acc["id"])
            else:
                queue.enqueue(c, "report_inbox", acc["id"])
            if target != p["account"]:  # the empty browser account isn't needed any more
                c.execute("DELETE FROM accounts WHERE id = %s AND external_id LIKE 'anon-%%' AND NOT EXISTS "
                          "(SELECT 1 FROM gmail_connections WHERE account_id = %s)", (p["account"], p["account"]))
        return "scanning", target

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
                "admin": is_admin(conn and conn["mailbox"]),
                "report_to": config.REPORT_INBOX or None,
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
    page_file = Path(__file__).parent / "static" / "app.html"
    page = page_file.read_text() if page_file.exists() else None  # server-only builds have no web page
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    from urllib.parse import urlparse
    origin = config.PUBLIC_URL
    hosts = {f"127.0.0.1:{config.API_PORT}", f"localhost:{config.API_PORT}", urlparse(origin).netloc.lower()}
    secure = origin.startswith("https://")

    def ok_host(req: Request) -> bool:
        return (req.headers.get("host") or "").lower() in hosts

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
        if page is None:
            return j(404, {"error": "no web page in this build; use the /api endpoints"})
        return HTMLResponse(page)  # the same static file the frontend container serves

    @app.get("/api/config")
    def cfg():
        return {"banks": sorted(b.name for b in registry.banks.values() if b.domains),
                "test_mode": bool(registry.test_senders), "dev_login": config.DEV_LOGIN,
                "default_name": ", ".join(names)}

    def with_cookie(r, token: str):
        r.set_cookie(COOKIE, token, httponly=True, samesite="lax", path="/", max_age=SESSION_TTL, secure=secure)
        return r

    # Each browser gets its own account: the one its cookie names, or a fresh empty one. Who the person
    # is comes from Google when they connect Gmail (finish_connect), so nobody lands in someone else's data.
    @app.post("/api/session")
    def session_(req: Request):
        with db.pool().connection() as c:
            acc = account(c, req)
            if acc:
                return j(200, api.me(c, acc))
            acc, token = auth.anonymous(c)
            return with_cookie(j(200, api.me(c, acc)), token)

    # Local development only: "?user=<name>" signs in as that test user.
    @app.post("/api/dev/login")
    async def dev_login(req: Request):
        if not config.DEV_LOGIN:
            return j(404, {"error": "not available"})
        b = await body(req)
        with db.pool().connection() as c:
            acc, token = auth.login(c, str(b.get("user", "")))
            return with_cookie(j(200, api.me(c, acc)), token)

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

    def admin_only(handler):
        """For the team only: anyone else gets a plain 404, as if the endpoint didn't exist."""
        def inner(c, acc, b, req):
            conn = c.execute("SELECT mailbox FROM gmail_connections WHERE account_id = %s", (acc["id"],)).fetchone()
            if not is_admin(conn and conn["mailbox"]):
                return j(404, {"error": "not found"})
            return handler(c, acc, b, req)
        inner.__name__ = handler.__name__
        return inner

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
        token = None
        with db.pool().connection() as c:
            acc = account(c, req)
            if acc and q.get("state"):
                screen, target = api.finish_connect(c, acc, q["state"], q.get("code"), q.get("error"))
                if target and target != acc["id"]:  # this person's existing account: sign the browser in to it
                    auth.logout(c, req.cookies.get(COOKIE))
                    token = auth.new_session(c, target)
        r = RedirectResponse(f"{origin}/?t={quote(api.token)}#{screen}", status_code=303)
        return with_cookie(r, token) if token else r

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
        from . import reports
        if answer == "no":
            card = c.execute("SELECT bank_id, last4 FROM cards WHERE account_id = %s AND card_key = %s",
                             (acc["id"], key)).fetchone()
            reports.log(c, acc["id"], "app", "not_my_card", "applied", card_key=key,
                        bank_id=card and card["bank_id"], last4=card and card["last4"], note=b.get("note"),
                        evidence_ids=reports.card_evidence_ids(c, acc["id"], key))
        elif answer is None:
            reports.undo(c, acc["id"], card_key=key)
        if answer is None:
            c.execute("DELETE FROM decisions WHERE account_id = %s AND card_key = %s", (acc["id"], key))
        else:
            c.execute("INSERT INTO decisions (account_id, card_key, decision) VALUES (%s, %s, %s) ON CONFLICT "
                      "(account_id, card_key) DO UPDATE SET decision = EXCLUDED.decision, decided_at = now()",
                      (acc["id"], key, answer))
        cards.rebuild(c, acc["id"], registry)
        return api.status(c, acc)

    @app.post("/api/report_email")
    @authed
    def report_email(c, acc, b, req):
        """An email we showed under a card is wrong ("not about this card"), or undo that. Logged."""
        from . import reports
        mid = str(b["id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", mid):
            raise ValueError("bad message id")
        wrong = bool(b.get("wrong", True))
        reports.report_wrong(c, acc["id"], mid, wrong, b.get("note"))
        if wrong:
            reports.log(c, acc["id"], "app", "wrong_email", "applied", message_id=mid, note=b.get("note"))
        else:
            reports.undo(c, acc["id"], message_id=mid)
        cards.rebuild(c, acc["id"], registry)
        return api.status(c, acc)

    @app.post("/api/dev/check_reports")
    @authed
    @admin_only
    def check_reports(c, acc, b, req):
        """Read the report inbox now instead of waiting for the scheduler."""
        row = c.execute("SELECT account_id FROM gmail_connections WHERE lower(mailbox) = %s AND status = 'active'",
                        (config.REPORT_INBOX or "-",)).fetchone()
        if not row:
            return j(409, {"error": "The report inbox isn't connected (set CD_REPORT_INBOX and sign it in)."})
        return j(202, {"started": queue.enqueue(c, "report_inbox", row["account_id"]) is not None})

    @app.get("/api/catalog")
    @authed
    def catalog(c, acc, b, req):
        """Banks and their cards, for "Report a mistake"."""
        from . import reports
        return {"banks": reports.catalog(registry)}

    @app.post("/api/report_mistake")
    @authed
    def report_mistake(c, acc, b, req):
        """This card is wrong: the user's right bank, card and last 4 digits (or "not mine at all")."""
        from . import reports
        key = str(b["key"])
        if b.get("not_mine"):
            card = c.execute("SELECT bank_id, last4 FROM cards WHERE account_id = %s AND card_key = %s",
                             (acc["id"], key)).fetchone()
            reports.log(c, acc["id"], "app", "not_my_card", "applied", card_key=key, note=b.get("note"),
                        bank_id=card and card["bank_id"], last4=card and card["last4"],
                        evidence_ids=reports.card_evidence_ids(c, acc["id"], key))
            c.execute("INSERT INTO decisions (account_id, card_key, decision) VALUES (%s, %s, 'no') ON CONFLICT "
                      "(account_id, card_key) DO UPDATE SET decision = 'no', decided_at = now()", (acc["id"], key))
        else:
            reports.correction(c, registry, acc["id"], key, str(b.get("bank") or ""), b.get("product") or None,
                               (b.get("last4") or "").strip() or None, b.get("note"))
        cards.rebuild(c, acc["id"], registry)
        return api.status(c, acc)

    @app.post("/api/dev/report_emails")
    @authed
    @admin_only
    def report_emails(c, acc, b, req):
        """The emails behind one report, fetched live with the reporting user's read-only access."""
        from . import reports
        return reports.review_emails(c, int(b["id"]))

    @app.get("/api/dev/reports")
    @authed
    @admin_only
    def dev_reports(c, acc, b, req):
        """Every user report, newest first, for the team (subjects and notes the users chose to send)."""
        from . import reports
        return {"reports": reports.recent(c, 100), "inbox": config.REPORT_INBOX or None}

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

    # ---- dev: one button for a fresh test run. Drops what earlier scans found (cards, evidence,
    # statement reads) but keeps the Gmail connection and the user's yes/no answers, then starts
    # the quick scan, which queues the statements step and the full scan after it.
    @app.post("/api/dev/fresh_scan")
    @authed
    @admin_only
    def dev_fresh_scan(c, acc, b, req):
        a = acc["id"]
        conn = c.execute("SELECT status FROM gmail_connections WHERE account_id = %s", (a,)).fetchone()
        if not conn or conn["status"] != "active":
            return j(409, {"error": "Gmail isn't connected. Connect it first."})
        if c.execute("SELECT 1 FROM jobs WHERE account_id = %s AND status = 'running'", (a,)).fetchone():
            return j(409, {"error": "A scan is running. Wait for it to finish."})
        with c.transaction():
            c.execute("DELETE FROM jobs WHERE account_id = %s AND status = 'queued'", (a,))
            for t in ("evidence", "statement_facts", "card_index", "set_aside", "statement_pdf_reads"):
                c.execute(f"DELETE FROM {t} WHERE account_id = %s", (a,))
            c.execute("DELETE FROM cards WHERE account_id = %s AND source = 'gmail'", (a,))
            c.execute("UPDATE gmail_connections SET last_synced_at = NULL WHERE account_id = %s", (a,))
            job = queue.enqueue(c, "discover_quick", a)
        return {"started": job is not None, "job": job}

    # ---- dev: live numbers for the side panel (counts and dates only; no email text)
    @app.get("/api/dev/live")
    @authed
    @admin_only
    def dev_live(c, acc, b, req):
        a = acc["id"]
        jobs_ = c.execute("SELECT id, kind, status, units_used, attempts, last_error, progress, "
                          "started_at, finished_at, run_after, "
                          "round(extract(epoch FROM coalesce(finished_at, now()) - started_at)::numeric, 1) AS secs "
                          "FROM jobs WHERE account_id = %s ORDER BY id DESC LIMIT 10", (a,)).fetchall()
        cur = next((x for x in jobs_ if x["status"] in ("running", "queued")), jobs_[0] if jobs_ else None)
        current = None
        if cur:
            pr = cur["progress"] or {}
            current = {"id": cur["id"], "kind": cur["kind"], "status": cur["status"], "secs": cur["secs"],
                       "units": cur["units_used"], "meter": pr.get("meter"), "stats": pr.get("stats"),
                       "dev": pr.get("dev"), "last": (pr.get("messages") or [])[-1:],
                       "run_after": cur["run_after"].isoformat() if cur["status"] == "queued" else None,
                       "error": cur["last_error"]}
        pdfs = {r["outcome"]: r["n"] for r in c.execute(
            "SELECT outcome, count(*) AS n FROM statement_pdf_reads WHERE account_id = %s GROUP BY outcome", (a,))}
        attach = sum(((x["progress"] or {}).get("meter") or {}).get("calls", {}).get("attachment", 0) for x in jobs_)
        ev = {r["type"]: r["n"] for r in c.execute(
            "SELECT data->>'type' AS type, count(*) AS n FROM evidence WHERE account_id = %s GROUP BY 1 ORDER BY 2 DESC", (a,))}
        span = c.execute("SELECT min((data->>'date')::timestamptz)::date AS oldest, "
                         "max((data->>'date')::timestamptz)::date AS newest FROM evidence WHERE account_id = %s", (a,)).fetchone()
        grp = {r["grp"]: r["n"] for r in c.execute(
            "SELECT grp, count(*) AS n FROM cards WHERE account_id = %s GROUP BY grp", (a,))}
        banks = [dict(r) for r in c.execute(
            "SELECT bank_id, count(*) AS n FROM cards WHERE account_id = %s AND grp = 'owned' "
            "GROUP BY bank_id ORDER BY 2 DESC, 1", (a,))]
        day = c.execute("SELECT units FROM quota_daily WHERE day = current_date").fetchone()
        mine = c.execute("SELECT coalesce(sum(units_used), 0) AS u FROM jobs WHERE account_id = %s "
                         "AND created_at >= current_date", (a,)).fetchone()["u"]
        # Shadow model next to the rules: the rules place the card, the model only says what it would do.
        same = {"owned": {"wallet"}, "review": {"ask", "wait"}}
        shadow_cards = []
        for r in c.execute("SELECT view->>'title' AS title, grp, view->'shadow' AS s FROM cards "
                           "WHERE account_id = %s AND source = 'gmail' AND grp IN ('owned', 'review') "
                           "ORDER BY bank_id, last4", (a,)):
            s = r["s"] or {}
            shadow_cards.append({"title": r["title"], "rules": r["grp"], "model": s.get("decision"),
                                 "confidence": s.get("confidence"), "events": s.get("events"),
                                 "agree": s.get("decision") in same.get(r["grp"], set()) if s else None})
        from carddiscovery.evidence_model import load as load_model, thresholds
        mdl = load_model()
        shadow = {"model": mdl and mdl["version"], "status": mdl and mdl.get("status"),
                  "thresholds": mdl and thresholds(mdl["costs"]), "cards": shadow_cards,
                  "scored_emails": c.execute("SELECT count(*) AS n FROM evidence WHERE account_id = %s "
                                             "AND data->'shadow' IS NOT NULL AND data->>'shadow' <> 'null'",
                                             (a,)).fetchone()["n"]}
        # Haiku fallback (shadow): emails the rules couldn't type, and the held cards Haiku found in them
        from . import understanding
        llm_rows = c.execute("SELECT data->>'bank_id' AS bank, data->'shadow'->'llm' AS l FROM evidence "
                             "WHERE account_id = %s AND data->'shadow'->'llm' IS NOT NULL", (a,)).fetchall()
        have = {(r["bank_id"], r["last4"]) for r in c.execute(
            "SELECT bank_id, last4 FROM cards WHERE account_id = %s AND source = 'gmail'", (a,))}
        found = {}
        for r in llm_rows:
            lw = r["l"] or {}
            if "error" in lw or lw.get("ownership") not in ("STRONG", "MODERATE"):
                continue
            for d in lw.get("cards") or []:
                key = (r["bank"], next((l4 for b, l4 in have if b == r["bank"] and l4 and l4.endswith(d)), d))
                f = found.setdefault(key, {"bank": r["bank"], "last4": key[1], "events": set(),
                                           "known_card": key in have})
                f["events"].add(lw.get("event"))
        shadow["llm"] = {"enabled": understanding.enabled(a), "backend": understanding.BACKEND,
                         "asked": len(llm_rows), "errors": sum("error" in (r["l"] or {}) for r in llm_rows),
                         "numbers_dropped": sum((r["l"] or {}).get("dropped", 0) for r in llm_rows),
                         "held_cards": [dict(f, events=sorted(f["events"])) for f in found.values()]}
        return {"scan_mode": config.SCAN_MODE, "current": current, "shadow": shadow,
                "jobs": [{"id": x["id"], "kind": x["kind"], "status": x["status"], "units": x["units_used"],
                          "secs": x["secs"]} for x in jobs_],
                "pdfs": {"outcomes": pdfs, "attachments_downloaded": attach},
                "evidence": {"by_type": ev, "oldest": span and span["oldest"] and span["oldest"].isoformat(),
                             "newest": span and span["newest"] and span["newest"].isoformat()},
                "cards": {"by_group": grp, "owned_by_bank": banks},
                "quota": {"account_today": mine, "all_accounts_today": day and day["units"]}}

    # ---- dev: what each job cost (units, time); no email content
    @app.get("/api/dev/jobs")
    @authed
    @admin_only
    def dev_jobs(c, acc, b, req):
        if True:
            rows = c.execute("SELECT id, kind, account_id, status, attempts, units_used, last_error, "
                             "progress->'stats' AS stats, progress->'meter' AS meter, "
                             "round(extract(epoch FROM finished_at - started_at)::numeric, 2) AS seconds "
                             "FROM jobs ORDER BY id DESC LIMIT 30").fetchall()
            day = c.execute("SELECT units FROM quota_daily WHERE day = current_date").fetchone()
        return {"jobs": rows, "units_today": day and day["units"]}

    return app, api
