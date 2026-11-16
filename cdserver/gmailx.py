"""The POC's Gmail adapter, with every call charged its quota units before it is sent.

Meter enforces three limits at once:
  - the job's budget (BudgetExhausted: the job saves its place and resumes later)
  - 5,500 units per rolling minute per user (waits, so Google never throttles us)
  - cancellation (Cancelled: the user disconnected; stop and keep nothing)
"""

from __future__ import annotations

import base64
import time
from collections import Counter, deque
from typing import Callable

from carddiscovery.providers.gmail import GmailProvider, _extract_body, _to_meta
from carddiscovery.queries import Query

from . import config


class BudgetExhausted(Exception):
    pass


class Cancelled(Exception):
    pass


class Meter:
    def __init__(self, budget: int, per_minute: int = config.PER_USER_UNITS_PER_MIN,
                 on_flush: Callable[["Meter"], None] | None = None, flush_every_s: float = 1.0):
        self.budget = budget
        self.per_minute = per_minute
        self.units = 0
        self.by_kind: Counter = Counter()
        self.calls: Counter = Counter()
        self.waited_s = 0.0
        self._window: deque[tuple[float, int]] = deque()
        self._on_flush = on_flush
        self._flush_every = flush_every_s
        self._last_flush = time.monotonic()
        self.unflushed = 0

    @property
    def remaining(self) -> int:
        return self.budget - self.units

    def affordable(self, kind: str) -> int:
        return max(0, self.remaining // config.UNITS[kind])

    def charge(self, kind: str, n: int = 1) -> None:
        cost = config.UNITS[kind] * n
        if n <= 0:
            return
        if self.units + cost > self.budget:
            raise BudgetExhausted(f"{kind} x{n} needs {cost} units, {self.remaining} left")
        self._wait_for(cost)
        self.units += cost
        self.unflushed += cost
        self.by_kind[kind] += cost
        self.calls[kind] += n
        self._window.append((time.monotonic(), cost))
        self.maybe_flush()

    def _wait_for(self, cost: int) -> None:
        while True:
            now = time.monotonic()
            while self._window and now - self._window[0][0] >= 60:
                self._window.popleft()
            used = sum(c for _, c in self._window)
            if used + cost <= self.per_minute or not self._window:
                return
            pause = 60 - (now - self._window[0][0]) + 0.01
            self.waited_s += pause
            self.flush()  # heartbeat and cancel check while we wait
            time.sleep(pause)

    def maybe_flush(self) -> None:
        if time.monotonic() - self._last_flush >= self._flush_every:
            self.flush()

    def flush(self) -> None:
        self._last_flush = time.monotonic()
        if self._on_flush:
            self._on_flush(self)  # may raise Cancelled
        self.unflushed = 0

    def summary(self) -> dict:
        return {"units": self.units, "budget": self.budget, "units_by_kind": dict(self.by_kind),
                "calls": dict(self.calls), "waited_s": round(self.waited_s, 1)}


class MeteredGmail(GmailProvider):
    def __init__(self, creds, meter: Meter):
        super().__init__(creds)
        self.meter = meter

    def mailbox(self) -> str:
        if self._profile is None:
            self.meter.charge("profile")
        return super().mailbox()

    def user_addresses(self) -> list[str]:
        self.mailbox()
        self.meter.charge("profile")
        return super().user_addresses()

    def search_many(self, queries: list[Query]) -> list[list[str]]:
        self.meter.charge("search", len(queries))  # first pages; follow-up pages via search()
        return super().search_many(queries)

    def search(self, query: Query, page_token: str | None = None, already: int = 0) -> list[str]:
        ids: list[str] = []
        token = page_token
        q = query.to_gmail()
        while True:
            size = 500
            if query.max_results is not None:
                size = min(500, query.max_results - already - len(ids))
                if size <= 0:
                    break
            self.meter.charge("search")
            resp = self._exec(self.svc.users().messages().list(
                userId=self.user_id, q=q, maxResults=size, pageToken=token, includeSpamTrash=False))
            ids.extend(m["id"] for m in resp.get("messages", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        return ids

    def _batch_get(self, ids: list[str], fmt: str) -> list[dict]:
        self.meter.charge("metadata" if fmt == "metadata" else "full", len(ids))
        return super()._batch_get(ids, fmt)

    def get_full(self, ids: list[str]) -> list:
        """Headers and body in one 60-unit read, for emails whose headers we already read
        in an earlier job (no need to pay 20 again for the headers)."""
        out = []
        for i in range(0, len(ids), 50):
            for raw in self._batch_get(ids[i:i + 50], fmt="full"):
                m = _to_meta(raw)
                m.body, names = _extract_body(raw.get("payload", {}))
                m.attachment_names = tuple(names)
                out.append(m)
        return out

    def fill_body(self, msg):
        self.meter.charge("full")
        return super().fill_body(msg)

    # ---- statement PDFs ---------------------------------------------------
    def pdf_attachments(self, message_id: str) -> list[tuple[str, str]]:
        """(filename, attachment id) of PDFs. Needs a full read: only it lists attachment ids."""
        self.meter.charge("full")
        raw = self._exec(self.svc.users().messages().get(userId=self.user_id, id=message_id, format="full"))
        out: list[tuple[str, str]] = []

        def walk(part: dict) -> None:
            name = part.get("filename") or ""
            att = part.get("body", {}).get("attachmentId")
            if att and (name.lower().endswith(".pdf") or part.get("mimeType") == "application/pdf"):
                out.append((name, att))
            for sub in part.get("parts", []) or []:
                walk(sub)

        walk(raw.get("payload", {}))
        return out

    def attachment(self, message_id: str, attachment_id: str, max_bytes: int = 10_000_000) -> bytes:
        self.meter.charge("attachment")
        resp = self._exec(self.svc.users().messages().attachments().get(
            userId=self.user_id, messageId=message_id, id=attachment_id))
        if resp.get("size", 0) > max_bytes:
            raise ValueError("attachment too large")
        return base64.urlsafe_b64decode(resp["data"] + "===")
