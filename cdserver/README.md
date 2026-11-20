# Card discovery server: APIs in and out

This server finds the credit cards a user holds by reading their Gmail with **read-only** access.
It has two sides:

1. **Our API** (FastAPI, `cdserver/api.py`). The user's app calls it to sign in, connect Gmail,
   start a scan and read the cards found.
2. **Google's APIs.** The server calls Google to get the user's permission (OAuth 2.0) and then to
   search and read bank emails (Gmail API v1). Only the workers (`cdserver/worker.py`,
   `cdserver/jobs.py`) read mail. The API process talks to Google only once, while the user
   connects.

```
 User's app                Our server (API)            Postgres          Workers                  Google
 ──────────                ────────────────            ────────          ───────                  ──────
 POST /api/dev/login  ──►  session cookie
 POST /api/connect    ──►  builds sign-in URL ─────────────────────────────────────────────────►  (consent screen)
 browser opens URL ───────────────────────────────────────────────────────────────────────────►  user approves
                           GET /oauth/callback  ◄──────────────────────────────────────────────  ?code=…&state=…
                           code ─► POST oauth2.googleapis.com/token ───────────────────────────►
                           ◄── access token + refresh token ◄──────────────────────────────────
                           save token, queue a scan ─► gmail_connections, jobs
                                                                  ◄── claim job
                                                                       refresh token ─────────►  oauth2 /token
                                                                       search / read mail ────►  gmail.googleapis.com
                                                                       save cards ─► cards
 GET /api/status      ──►  progress and cards ◄── cards, jobs
```

---

## 1. Our API: what the server takes from the user

Base address `http://127.0.0.1:8790` (`CD_PORT` changes the port, `CD_BIND` the listen address).

**Every request must have:**

| What | Where | Why |
|---|---|---|
| `Host` | `127.0.0.1:8790` or `localhost:8790` | Requests for any other host get `403 bad host`. |
| Server token | `X-Token` header, or `?t=` in the URL | Random per server start, printed in the log (`API: http://127.0.0.1:8790/?t=…`). Wrong or missing: `403`. Not needed on `/oauth/callback`, which Google's redirect calls. |
| Session cookie `cd_session` | cookie | Set by `/api/dev/login`. Lasts 7 days. Missing or expired on an endpoint that needs it: `401 login required`. |

Request bodies are JSON (at most 10 KB). Bad input returns `400 {"error": "..."}`.

### Endpoints

| Method and path | Login | Body sent by the app | Answer |
|---|---|---|---|
| `GET /api/config` | no | none | `{"banks": [bank names], "test_mode": bool}` |
| `POST /api/dev/login` | no | `{"user": "<id from your login service>"}` | Sets the `cd_session` cookie. Returns the same as `/api/me`. Stands in for a real login service. |
| `GET /api/me` | yes | none | `{"user", "mailbox", "connected", "needs_reconnect", "saved_cards", "notifications": [{"kind", "detail"}]}` |
| `POST /api/connect` | yes | `{"name": "Name, Other spelling", "years": 1\|3\|5, "pdf": bool, "dob": "YYYY-MM-DD" (optional)}` | `{"url": "https://accounts.google.com/o/oauth2/auth?..."}`. Open this URL in the browser. |
| `GET /oauth/callback` | cookie | Google adds `?code=…&state=…` (or `?error=…`) | Redirects to `/?t=…#scanning`, `#denied`, `#noscope` (Gmail box was unticked) or `#expired` (over 10 minutes or wrong user). |
| `GET /api/status` | yes | none | `{"state": "idle"\|"running"\|"done"\|"error", "progress": [messages], "error", "result"}`. `result` is only there when `state` is `done`. Poll this during a scan. |
| `GET /api/cards` | yes | none | `{"mailbox", "groups": {"owned", "review", "history", "rejected"}, "applications", "stats"}` |
| `POST /api/scan` | yes | `{"years": 1\|3\|5}` | `202 {"started": true}`. `409` if a scan is already running, or if Gmail isn't connected (`"reconnect": true`). |
| `POST /api/decide` | yes | `{"key": "<card key>", "answer": "yes"\|"no"\|null}` | The user confirms or rejects a card under review (`null` undoes it). Returns the same as `/api/status`. |
| `POST /api/disconnect` | yes | none | `{"revoking": bool}`. A worker revokes access at Google and deletes the token and all data taken from Gmail. |
| `POST /api/delete_account` | yes | none | Same as disconnect, then deletes the account and logs out. |
| `POST /api/logout` | yes | none | `{"ok": true}`. Clears the cookie. |
| `GET /api/dev/jobs` | token only | none | The last 30 jobs with Gmail units, time and errors, plus `units_today`. No email content. |
| `GET /` | token only | none | The web page when `cdserver/static/app.html` is present. In this server-only repository it returns `404 {"error": "no web page in this build; use the /api endpoints"}`. |

**Each card** in `groups` has these fields:
- `key`, `bank`, `product`, `title`, `headline`
- `status` (for example Active, Replaced, Closed, Not used recently)
- `group`
- `evidence` (short reasons, never email text)
- `first_seen`, `last_seen`, `count`
- `question` and `decision` when the user needs to confirm the card
- `concerns`, `apps`, `new`, `hidden`

---

## 2. What the server sends to Google

All calls go over HTTPS using Google's Python libraries (`google-auth-oauthlib`,
`google-api-python-client`). The OAuth client comes from `credentials.json`, a **Desktop app**
client downloaded from Google Cloud. Set `CD_GOOGLE_CLIENT` to use another path.

### 2a. Permission (OAuth 2.0 with PKCE)

| Step | Request to Google | What we send |
|---|---|---|
| Sign-in URL (`/api/connect`) | The user's browser is sent to `https://accounts.google.com/o/oauth2/auth` | `client_id`, `redirect_uri=http://127.0.0.1:8790/oauth/callback`, `response_type=code`, `scope=https://www.googleapis.com/auth/gmail.readonly`, `access_type=offline` (so we get a refresh token), `prompt=consent`, `include_granted_scopes=false`, a random `state`, and `code_challenge` + `code_challenge_method=S256` (PKCE) |
| Code for tokens (`/oauth/callback`) | `POST https://oauth2.googleapis.com/token` | `grant_type=authorization_code`, `code`, `code_verifier` (kept only in server memory, 10 minutes), `client_id`, `client_secret`, `redirect_uri` |
| New access token (every worker job) | `POST https://oauth2.googleapis.com/token` | `grant_type=refresh_token`, `refresh_token`, `client_id`, `client_secret` |
| Revoke (`/api/disconnect`) | `POST https://oauth2.googleapis.com/revoke` | `token=<refresh token>` |

The only scope is `gmail.readonly`. The server can't send, delete, label or change mail.

### 2b. Reading mail (Gmail API v1, `https://gmail.googleapis.com`)

Each call carries `Authorization: Bearer <access token>`. Up to 50 calls are grouped into one
`POST https://gmail.googleapis.com/batch/gmail/v1` request.

| Call | Used for | Google's quota cost |
|---|---|---|
| `GET users/me/profile` | The mailbox address (shown to the user, and used to notice when a different Gmail account connects) | 1 unit |
| `GET users/me/messages?q=…&maxResults=…&pageToken=…` | Searches for bank mail: sender domains from `carddiscovery/data/banks.toml`, date windows (3 months by default), card-number forms, statement, welcome and closure words. Spam and Trash are excluded. | 5 units |
| `GET users/me/messages/{id}?format=metadata&metadataHeaders=…` | Headers only: `From`, `To`, `Delivered-To`, `Subject`, `Date`, `Authentication-Results`, `ARC-Authentication-Results`, `List-Unsubscribe`. This is how most email is read. | 20 units |
| `GET users/me/messages/{id}?format=full` | The full email, only for the few that need their body read (for example the card number is only in the body) | 60 units |
| `GET users/me/messages/{id}/attachments/{attachmentId}` | Statement PDFs. Off by default; `CD_READ_PDFS=1` turns it on. | 20 units |

**Limits the server keeps to** (`cdserver/config.py`, `cdserver/gmailx.py`):
- **Per user:** 5,900 units a minute. Google allows 6,000.
- **Per job:** quick scan 1,000 units, full scan 15,000, daily sync 500, PDFs 1,000. A full scan
  that runs out saves its place and continues in a later job.
- **Errors:** replies `403`, `429`, `500` and `503` are retried with growing waits, up to 32 s.
  The number of retries and the time spent waiting are recorded for each job.

---

## 3. What the server gets back from Google, and what it keeps

| From | What comes back | What we keep |
|---|---|---|
| Token endpoint | `access_token` (about 1 hour), `refresh_token`, `expires_in`, `scope`, `token_type` | The whole token JSON in `gmail_connections.token_blob`, **as plain text, not encrypted**, plus the scopes granted. If the Gmail box was unticked, nothing is saved. |
| Profile | `emailAddress`, `messagesTotal`, `historyId` | `emailAddress` only, as `mailbox` |
| Search | Message ids (`id`, `threadId`), `nextPageToken`, `resultSizeEstimate` | Nothing directly; the ids are used for the next reads |
| Metadata read | The headers listed above, `labelIds`, `internalDate`, `snippet` | Short facts per email in `evidence`: date, bank, type (statement, welcome, closure, …), last 4 digits, product name, whether Gmail's sender check passed, forwarded or not, name match. **No subject, body or snippet is stored.** |
| Full read | The MIME body (base64url parts: text and HTML) | Same facts as above; the body is read in memory and dropped |
| Attachment | PDF bytes (base64url) | From statements: credit limit, reward points, total due, statement date (`statement_facts`). The PDF itself isn't stored. |
| Revoke | `200` if access was revoked | Nothing: the token and all Gmail data are deleted |

From these facts the server builds `cards` (what `/api/cards` returns). Every read of a token
is recorded in `audit_log` (`read_token:<job kind>`).

**If Google refuses the refresh token** (the user revoked access, or a Testing-mode app's 7-day
limit ran out), the connection is marked `needs_reconnect`, the user gets a notification, and
`/api/me` shows `needs_reconnect: true`.

> **Security note.** Gmail tokens are stored unencrypted. Anyone who can read the database can
> read every connected user's mail until the tokens are revoked. Keep the database private, and
> encrypt tokens again before real users connect. Google's verification for the restricted
> `gmail.readonly` scope expects stored tokens to be protected.
