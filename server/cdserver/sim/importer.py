"""Write the simulated emails into a real test inbox with Gmail's messages.import.

Nothing is sent: import places a fully formed email in the mailbox, filed at the
date in its Date header. Uses its own token (gmail.modify on the TEST account only),
separate from the app's read-only token, and refuses to run against any mailbox
other than the one named. Resumable: imported keys are recorded as it goes.
Every imported email carries the label "cd-sim", which `clean` uses to trash them.
"""

from __future__ import annotations

import base64
import json
import os
import time

from .. import config
from .generate import Mail

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
LABEL = "cd-sim"
SIM_DIR = config.HOME / "sim"
TOKEN = SIM_DIR / "import_token.json"
STATE = SIM_DIR / "imported.json"
BATCH = 25


def _creds(to: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
        if not creds.valid and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(config.GOOGLE_CLIENT_FILE), SCOPES)
        print(f"Opening Google sign-in. Sign in as {to} and allow access.", flush=True)
        creds = flow.run_local_server(port=0, login_hint=to, prompt="consent", open_browser=True,
                                      authorization_prompt_message="If the browser didn't open, visit: {url}")
        SIM_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN.write_text(creds.to_json())
        os.chmod(TOKEN, 0o600)
    return creds


def _service(to: str):
    import google_auth_httplib2
    import httplib2
    from googleapiclient.discovery import build
    http = google_auth_httplib2.AuthorizedHttp(_creds(to), http=httplib2.Http(timeout=180))
    svc = build("gmail", "v1", http=http, cache_discovery=False)
    me = svc.users().getProfile(userId="me").execute()["emailAddress"]
    if me.lower() != to.lower():
        TOKEN.unlink(missing_ok=True)
        raise SystemExit(f"Signed in as {me}, but the test inbox is {to}. Nothing was imported. Run again and "
                         f"sign in as {to}.")
    return svc


def _label_id(svc) -> str:
    for lab in svc.users().labels().list(userId="me").execute().get("labels", []):
        if lab["name"] == LABEL:
            return lab["id"]
    return svc.users().labels().create(userId="me", body={"name": LABEL, "labelListVisibility": "labelShow",
                                                          "messageListVisibility": "show"}).execute()["id"]


def _load_state() -> dict[str, str]:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def _save_state(state: dict[str, str]) -> None:
    SIM_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


def _reconcile(svc, chunk: list[Mail], state: dict[str, str]) -> None:
    for m in chunk:
        if m.key in state:
            continue
        for _ in range(3):
            try:
                hits = svc.users().messages().list(userId="me", q=f"rfc822msgid:{m.message_id}",
                                                   includeSpamTrash=True).execute().get("messages", [])
                if hits:
                    state[m.key] = hits[0]["id"]
                break
            except Exception:
                time.sleep(5)


def import_all(mails: list[Mail], to: str) -> None:
    import httplib2
    from googleapiclient.errors import HttpError

    svc = _service(to)
    label = _label_id(svc)
    state = _load_state()
    todo = [m for m in mails if m.key not in state]
    print(f"{len(state)} already imported, {len(todo)} to go, into {to}", flush=True)
    t0, pause, no_categories = time.monotonic(), 0.0, False
    i = 0
    while i < len(todo):
        chunk = [m for m in todo[i:i + BATCH] if m.key not in state]  # a retry skips ones already in
        if not chunk:
            i += BATCH
            continue
        failed: list[Mail] = []
        bad_label: list[Mail] = []

        def cb(rid, resp, exc, chunk=chunk):
            m = next(x for x in chunk if x.key == rid)
            if exc is None:
                state[m.key] = resp["id"]
            elif isinstance(exc, HttpError) and exc.resp.status in (429, 403, 500, 503):
                failed.append(m)
            elif isinstance(exc, HttpError) and exc.resp.status == 400 and "label" in str(exc).lower():
                bad_label.append(m)
            else:
                raise exc

        batch = svc.new_batch_http_request(callback=cb)
        for m in chunk:
            labels = [x for x in m.labels if not (no_categories and x.startswith("CATEGORY_"))] + [label]
            body = {"raw": base64.urlsafe_b64encode(m.raw(to)).decode(), "labelIds": labels}
            batch.add(svc.users().messages().import_(userId="me", body=body, internalDateSource="dateHeader",
                                                     neverMarkSpam=True, processForCalendar=False), request_id=m.key)
        try:
            batch.execute()
        except (TimeoutError, OSError, ConnectionError, httplib2.HttpLib2Error) as e:
            # The batch may have partly gone through: look each email up by its Message-ID
            # before retrying, so a retry never creates duplicates.
            _save_state(state)
            pause = min(max(pause * 2, 5.0), 120.0)
            print(f"  network error ({type(e).__name__}); checking what arrived, then retrying in {pause:.0f}s",
                  flush=True)
            time.sleep(pause)
            _reconcile(svc, chunk, state)
            _save_state(state)
            continue
        _save_state(state)
        if bad_label and not no_categories:
            no_categories = True  # Gmail refused category labels on import: retry without them
            print("Gmail refused the category labels; continuing without them.", flush=True)
            continue
        if failed:
            pause = min(max(pause * 2, 2.0), 60.0)
            print(f"  throttled on {len(failed)} emails, waiting {pause:.0f}s", flush=True)
            time.sleep(pause)
            continue  # redo the same chunk; imported ones are skipped below
        pause = max(0.0, pause / 2)
        i += BATCH
        todo_left = len(todo) - i
        if i % 500 < BATCH or todo_left <= 0:
            rate = i / max(1e-6, time.monotonic() - t0)
            print(f"  {len(state)} imported ({rate:.0f}/s), {max(0, todo_left)} left", flush=True)
    print(f"Done: {len(state)} emails in {to} under the label '{LABEL}' "
          f"({time.monotonic() - t0:.0f}s).", flush=True)


def clean(to: str) -> None:
    """Move every simulated email (label cd-sim) to Trash. Gmail empties Trash after 30 days."""
    svc = _service(to)
    ids: list[str] = []
    token = None
    while True:
        resp = svc.users().messages().list(userId="me", q=f"label:{LABEL}", maxResults=500, pageToken=token,
                                           includeSpamTrash=False).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    print(f"Moving {len(ids)} simulated emails to Trash in {to}", flush=True)
    for i in range(0, len(ids), 50):
        batch = svc.new_batch_http_request()
        for mid in ids[i:i + 50]:
            batch.add(svc.users().messages().trash(userId="me", id=mid))
        batch.execute()
        time.sleep(0.5)
    STATE.unlink(missing_ok=True)
    print("Done. They stay in Trash for 30 days; empty Trash in Gmail to remove them now.", flush=True)
