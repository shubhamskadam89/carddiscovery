"""Gmail adapter (read-only scope)."""

from __future__ import annotations

import base64
import os
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from ..models import MessageMeta
from ..queries import Query

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
METADATA_HEADERS = [
    "From", "To", "Delivered-To", "Subject", "Date",
    "Authentication-Results", "ARC-Authentication-Results", "List-Unsubscribe",
]
BATCH_SIZE = 50
MAX_RETRIES = 6

STATE_DIR = Path(os.environ.get("CARDDISCOVERY_HOME", Path.home() / ".carddiscovery"))


def authorize(credentials_file: Path, token_file: Path | None = None):
    """Run (or reuse) the OAuth flow and return Google credentials."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_file = token_file or STATE_DIR / "token.json"
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None  # refresh tokens of unverified apps expire after ~7 days
    if not creds or not creds.valid:
        if not credentials_file.exists():
            raise SystemExit(
                f"OAuth client file not found: {credentials_file}\n"
                "Create a Desktop OAuth client in Google Cloud and download it (see README)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
        creds = flow.run_local_server(port=0)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())
    os.chmod(token_file, 0o600)
    return creds


class GmailProvider:
    def __init__(self, creds, user_id: str = "me"):
        from googleapiclient.discovery import build

        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.user_id = user_id
        self._profile = None
        self.calls = 0

    # ---- identity ------------------------------------------------------
    def mailbox(self) -> str:
        if self._profile is None:
            self._profile = self._exec(self.svc.users().getProfile(userId=self.user_id))
        return self._profile["emailAddress"]

    def user_addresses(self) -> list[str]:
        addrs = {self.mailbox().lower()}
        try:
            resp = self._exec(self.svc.users().settings().sendAs().list(userId=self.user_id))
            addrs.update(s["sendAsEmail"].lower() for s in resp.get("sendAs", []))
        except Exception:
            pass
        return sorted(addrs)

    # ---- search --------------------------------------------------------
    def search_many(self, queries: list[Query]) -> list[list[str]]:
        """First page of every query in batched HTTP requests (50 per round trip),
        then follow-up pages only for the few queries that need them."""
        firsts: dict[str, dict] = {}
        for i in range(0, len(queries), BATCH_SIZE):
            chunk = list(enumerate(queries))[i:i + BATCH_SIZE]
            reqs = {
                str(n): (lambda q=q: self.svc.users().messages().list(
                    userId=self.user_id, q=q.to_gmail(), includeSpamTrash=False,
                    maxResults=min(500, q.max_results or 500)))
                for n, q in chunk
            }
            firsts.update(self._batch(reqs))
        out = []
        for n, q in enumerate(queries):
            resp = firsts.get(str(n))
            if resp is None:  # batch item failed after retries: fall back to a plain call
                out.append(self.search(q))
                continue
            ids = [m["id"] for m in resp.get("messages", [])]
            token = resp.get("nextPageToken")
            if token and (q.max_results is None or len(ids) < q.max_results):
                ids += self.search(q, page_token=token, already=len(ids))
            out.append(ids[: q.max_results] if q.max_results else ids)
        return out

    def search(self, query: Query, page_token: str | None = None, already: int = 0) -> list[str]:
        ids: list[str] = []
        token = page_token
        q = query.to_gmail()
        while True:
            page_size = 500
            if query.max_results is not None:
                page_size = min(500, query.max_results - already - len(ids))
                if page_size <= 0:
                    break
            req = self.svc.users().messages().list(
                userId=self.user_id, q=q, maxResults=page_size, pageToken=token,
                includeSpamTrash=False,
            )
            resp = self._exec(req)
            ids.extend(m["id"] for m in resp.get("messages", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        return ids

    # ---- fetch ---------------------------------------------------------
    def get_metadata(self, ids: Iterable[str]) -> list[MessageMeta]:
        ids = list(ids)
        out: list[MessageMeta] = []
        for i in range(0, len(ids), BATCH_SIZE):
            chunk = ids[i:i + BATCH_SIZE]
            results = self._batch_get(chunk, fmt="metadata")
            out.extend(_to_meta(r) for r in results)
        return out

    def fill_bodies(self, msgs: list[MessageMeta]) -> None:
        """Fetch full bodies for several messages in batched requests."""
        for i in range(0, len(msgs), BATCH_SIZE):
            chunk = msgs[i:i + BATCH_SIZE]
            raws = {r["id"]: r for r in self._batch_get([m.message_id for m in chunk], fmt="full")}
            for m in chunk:
                raw = raws.get(m.message_id)
                if raw:
                    m.body, names = _extract_body(raw.get("payload", {}))
                    m.attachment_names = tuple(names)

    def fill_body(self, msg: MessageMeta) -> MessageMeta:
        raw = self._exec(self.svc.users().messages().get(
            userId=self.user_id, id=msg.message_id, format="full"))
        text, names = _extract_body(raw.get("payload", {}))
        msg.body = text
        msg.attachment_names = tuple(names)
        return msg

    # ---- plumbing ------------------------------------------------------
    def _batch_get(self, ids: list[str], fmt: str) -> list[dict]:
        def make(mid: str):
            kwargs = {"userId": self.user_id, "id": mid, "format": fmt}
            if fmt == "metadata":
                kwargs["metadataHeaders"] = METADATA_HEADERS
            return self.svc.users().messages().get(**kwargs)

        done = self._batch({mid: (lambda mid=mid: make(mid)) for mid in ids})
        return [done[i] for i in ids if i in done]

    def _batch(self, requests: dict) -> dict[str, dict]:
        """Run up to BATCH_SIZE requests in one HTTP round trip, retrying throttled items.
        `requests` maps an id to a request, or to a zero-arg function that builds one."""
        from googleapiclient.errors import HttpError

        pending = dict(requests)
        done: dict[str, dict] = {}
        delay = 1.0
        for _ in range(MAX_RETRIES):
            if not pending:
                break
            failed: list[str] = []

            def cb(request_id, response, exception):
                if exception is None:
                    done[request_id] = response
                elif isinstance(exception, HttpError) and exception.resp.status in (403, 429, 500, 503):
                    failed.append(request_id)
                elif isinstance(exception, HttpError) and exception.resp.status == 404:
                    pass  # deleted between list and get
                else:
                    raise exception

            batch = self.svc.new_batch_http_request(callback=cb)
            for rid, req in pending.items():
                batch.add(req() if callable(req) else req, request_id=rid)
            self.calls += len(pending)
            batch.execute()
            # Rebuild failed requests from scratch before retrying.
            pending = {rid: requests[rid] for rid in failed}
            if pending:
                time.sleep(delay)
                delay = min(delay * 2, 32)
        return done

    def _exec(self, request):
        from googleapiclient.errors import HttpError

        delay = 1.0
        for attempt in range(MAX_RETRIES):
            try:
                self.calls += 1
                return request.execute()
            except HttpError as e:
                if e.resp.status in (403, 429, 500, 503) and attempt < MAX_RETRIES - 1:
                    time.sleep(delay)
                    delay = min(delay * 2, 32)
                    continue
                raise


def _to_meta(raw: dict) -> MessageMeta:
    headers = {}
    for h in raw.get("payload", {}).get("headers", []):
        name = h["name"].lower()
        # Several Authentication-Results headers can exist; keep them all.
        headers[name] = (headers.get(name, "") + "\n" + h["value"]).strip()
    date = datetime.fromtimestamp(int(raw.get("internalDate", "0")) / 1000, tz=timezone.utc)
    return MessageMeta(
        message_id=raw["id"],
        date=date,
        sender=headers.get("from", ""),
        subject=headers.get("subject", ""),
        snippet=raw.get("snippet", ""),
        to=" ".join(filter(None, [headers.get("to", ""), headers.get("delivered-to", "")])),
        auth_results="\n".join(filter(None, [
            headers.get("authentication-results", ""),
            headers.get("arc-authentication-results", ""),
        ])),
        list_unsubscribe="list-unsubscribe" in headers,
        labels=tuple(raw.get("labelIds", [])),
    )


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "td", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    return " ".join(" ".join(p.parts).split())


def _extract_body(payload: dict) -> tuple[str, list[str]]:
    plain: list[str] = []
    html: list[str] = []
    names: list[str] = []

    def walk(part: dict):
        mime = part.get("mimeType", "")
        fname = part.get("filename")
        if fname:
            names.append(fname)  # name only; attachment data is never downloaded
        data = part.get("body", {}).get("data")
        if data and not fname:
            text = base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
            if mime == "text/plain":
                plain.append(text)
            elif mime == "text/html":
                html.append(text)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    text = " ".join(plain) if plain else " ".join(html_to_text(h) for h in html)
    return " ".join(text.split())[:20000], names
