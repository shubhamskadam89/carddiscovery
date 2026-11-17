# Card Discovery: architecture (v2, server-side)

Status: proposal for review · Date: 2026-11-16 (rev. 2: Gmail quotas of May 2026) · Supersedes the processing location in [mobile-design.md](mobile-design.md) (on-device). Problem analysis, rules, scoring and test kits from [card-discovery-design.md](card-discovery-design.md) and the POC still apply.

---

## 0. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | **The card picker is the main way to add cards.** Gmail is optional: it pre-fills the picker, reads statements and catches new cards | Works for everyone, with no Gmail approval needed; Gmail adds convenience and depth |
| D2 | **Processing runs on the server.** The app sends Google's one-time code; the server keeps the token encrypted and talks to Gmail; the phone only shows progress | Reliable daily sync (iOS limits background work), PDF parsing and AI belong on a server, and one Python engine needs no port to Kotlin or Swift |
| D3 | **Two phases, both budgeted in Gmail quota units (§1a).** Phase 1 spends about **1,000 units** (about 15 searches + about 40 header reads) and shows cards within about 10 s; Phase 2 runs in the background within a per-user budget (full detection, then statement PDFs and reconciliation) | Users see value immediately. Since May 2026 one user can spend only 6,000 units a minute, so the quota, not the servers, sets the pace |
| D4 | **Fixed parsers per bank; AI only for the long tail** (unknown formats, merchant names, drafting new parsers, human-reviewed) | Predictable, cheap, explainable; AI where rules can't reach |
| D5 | **Daily incremental sync by search:** bank senders `after:` the last sync (one 5-unit search), then read only the new bank emails. Gmail's history API is kept for when push notifications are added | History returns every new email, personal mail included, and each costs a 20-unit read to inspect; a sender-filtered search returns only bank mail |
| D6 | **Boring stack:** Postgres (data and job queue), EC2 workers, cron; SQS later if needed | One database, fewer moving parts |
| D7 | **Start Google OAuth verification and the CASA assessment now** | Both are required for this design and take weeks |

| D8 | **Card identity = user + bank + last 4**, with the product as an attribute and links for upgrades and reissues | "User + product" breaks when someone holds two of the same card (test matrix case 4); reissues are handled by replacement links |
| D9 | **Default lookback:** 12 months in Phase 1, **3 years** for full detection in Phase 2 (1/3/5 selectable), statement PDFs for the **last 6 months** per card | Dormant cards need history, but every extra year costs quota; statements older than 6 months add little |

**Accepted trade-off of D2:** the server holds users' Gmail tokens and briefly processes their bank mail. So the **CASA security assessment is required every year**, and token security (§5) is the most important engineering work in the project.

---

## 1a. Gmail API quotas (from May 2026)

From Google's Gmail API usage limits page, checked 2026-11-16. They apply to Cloud projects created on or after 1 May 2026; projects that used the API between November 2025 and April 2026 keep their earlier quotas.

| Call | Units |
|---|---|
| `messages.list` (search, up to 500 IDs per page) | 5 |
| `messages.get`, `format=metadata` (headers + preview) | **20** |
| `messages.get`, `format=full` / `raw` / `html` | **60** |
| `messages.attachments.get` (PDF) | **20** |
| `history.list` | 2 |
| `users.watch` (push) | 100 |

| Limit | Value |
|---|---|
| Per user, per project | **6,000 units / minute** (about 100 per second) |
| Per project | 1,200,000 units / minute |
| Per project, per day | **80,000,000 units free.** Google: charges above this are "planned … later in 2026" with at least 90 days' notice; this daily threshold **can't be raised** |

**What this means for the design:**
- **Header reads are the main cost** (20 units each), and full reads cost three times as much (60). Read full emails only when the header and preview aren't enough.
- **A first scan is paced by the 6,000/minute per-user limit**, not by servers. Phase 1 must fit in about 1,000 units to finish in about 10 s.
- **Keep the whole product under 80M units a day** (§9), with alerts at 60% and 80%.

---

## 1. Components

```
┌──────────────── Mobile app ────────────────┐        ┌──────────────── Google ───────────────┐
│ Phone + OTP login                           │        │ OAuth consent (account picker)         │
│ Card picker (bank → card → network)         │        │ Gmail API (read-only)                  │
│ "Connect Gmail" explainer → Google Sign-In  │──code─►│                                        │
│   (requests a server auth code)             │        └───────────▲────────────────────────────┘
│ Progress + live cards (polling / SSE)       │                    │ token exchange, Gmail calls
│ Review: Yes/No, suggestions into picker     │                    │
│ Settings: disconnect, delete                │                    │
└───────────────┬─────────────────────────────┘                    │
                │ HTTPS + app session + attestation                │
┌───────────────▼──────────────────── Backend (AWS, ap-south-1 / Mumbai) ─────────────────────────┐
│ API service (Python)        auth, OTP, picker catalogue, connect, status, cards, decisions      │
│ Token vault                 Gmail refresh tokens encrypted with KMS (envelope); API can't read  │
│ Job queue (Postgres)        jobs table, SELECT … FOR UPDATE SKIP LOCKED                          │
│ Workers (EC2)               discovery engine (POC package) + PDF module + parser registry       │
│ Cron                        daily sync scheduler, token health checks, cleanup                   │
│ AI service (optional)       redacted unknown-format extraction, merchant names, parser drafts   │
│ Rules + parsers registry    versioned banks.toml / rules.json / per-bank statement parsers      │
│ Postgres                    accounts, cards, evidence (derived), connections, jobs, audit       │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Where each step happens

| Step | Phone | API | Worker | Google |
|---|---|---|---|---|
| Phone + OTP login | Screens | Sends the SMS, verifies, issues the session | | |
| Card picker | Screens | Serves the catalogue | | |
| Explainer and consent | Screens | Records the consent version and time | | |
| Google permission | Google Sign-In SDK, asking for a **server auth code** with `gmail.readonly` | Receives the code over HTTPS; never logs it | | Consent screen |
| Code → tokens | | Exchanges the code; **encrypts the refresh token with KMS**; stores it | | Token endpoint |
| Phase 1 discovery | Shows progress and cards as they're found | Enqueues `discover_quick` | Gmail search + headers/previews (last 12 months, high-value types) → engine → cards | Gmail API |
| Phase 2 enrichment | Shows "Adding details…" | Enqueues `discover_full`, then `pdf_enrich` | Full chosen range, bodies, sampled alerts, statement PDFs, reconciliation | Gmail API |
| Review | Yes/No; accept suggestions into the picker | Saves decisions | | |
| Daily sync | Banner if there's a new card or a reconnect is needed; optional push notification | Cron enqueues `daily_sync` | One search of bank senders since the last sync → read only new mail → engine | Gmail API |
| Disconnect / delete | Button | Revokes with Google, deletes the token, evidence and Gmail-sourced cards; cancels running jobs | Stops at the next step and drops its output | Revoke endpoint |

---

## 3. Flows

### 3.1 Connect Gmail
1. The app shows our explainer: which banks, how far back (1/3/5 years), what's never done, and an optional toggle for **"Also read statement PDFs"**.
2. The Google Sign-In SDK runs with `gmail.readonly`, **offline access**, and a **server client ID**, and returns a one-time **server auth code**. On Android this is Google Identity Services with a server auth code; on iOS, the Google Sign-In SDK with a server client ID. Confirm the exact SDK calls during implementation.
3. The app sends `POST /gmail/connect {code, years, pdf_opt_in}`, carrying the app session.
4. The API:
   - exchanges the code at Google's token endpoint
   - **checks that `gmail.readonly` was granted** (users can untick it)
   - encrypts the refresh token
   - stores the connection
   - enqueues `discover_quick`
5. The app polls `GET /discovery/status` every 1–2 s, or keeps one SSE connection. Cards appear as they're found.

### 3.2 Phase 1: quick discovery (budget about 1,000 units; first card under 5 s, done under 10 s)
- **Searches:** about 15 `messages.list` calls (75 units), **last 12 months**, high-value families only: statements, onboarding (welcome, dispatch, activation), lifecycle (fee, upgrade, closure). Alerts and the catch-all are skipped.
- **Header reads:** at most **about 40** `messages.get(format=metadata)` calls (800 units), chosen to cover the most banks:
  - **Round-robin by bank:** the newest statement per bank first, then the newest onboarding email, then the next statement, until the budget runs out.
  - Within a bank, skip an email whose preview shows a card number already found.
- **No full emails, no PDFs** in Phase 1.
- Each email goes through the engine; a card is published to the app as soon as it reaches "owned" or "confirm".
- **Progress:** "banks checked / banks with mail", with each card appearing as it's found.
- **What Phase 1 can miss** (Phase 2 finds it): a second card at the same bank whose mail wasn't sampled, and dormant cards with no high-value mail in the last 12 months. Phase 1 is a fast preview, not the complete answer.

### 3.3 Phase 2: full discovery and enrichment (background, budgeted)
1. **`discover_full`**: a **card-aware scan** (default budget 15,000 units; measured about 5,200 for 5 years). Searches cost 5 units and reads 20, so once a card is known, its history is found with searches instead of reading every email about it.
   - **6-month windows, newest first.** Each window runs the family searches (statements, onboarding, lifecycle, applications, sampled payments, alerts for the last 12 months, a capped catch-all) with **every known card's number excluded** (`-2846 -XX2846 …`). Only mail about cards not seen yet comes back; it is read (headers first, full only when needed), new cards join the exclusion list, and the window is searched again only if more than one batch (50) came back. Recent windows find the cards in use; older windows only surface cards that went quiet. Progress: "Checked back to May 2023: 12 cards so far".
   - **Per card, a few searches by its number:** how many emails mention it, its newest and oldest email, newest usage email, lifecycle emails (fee, upgrade, replacement, closure, inactivity), its **newest statement, opened in full** (add-on cards are named there; 60 units per card), and its **statement-PDF emails** (`has:attachment filename:pdf`), which are indexed, not downloaded.
   - **Number forms.** Gmail matches whole words, case-insensitively: `2846` matches "ending 2846" and "XXXX XXXX XXXX 2846" but **not** "XX2846" (checked on real Gmail). Every number is therefore searched as `2846 OR XX2846 OR XXXX2846 OR XXXXXXXX2846 OR XXXXXXXXXXXX2846` (Amex: also the 10 possible 5-digit forms), plus every masked form actually seen in that card's mail, e.g. "4375XXXXXXXX2846" (learned while reading; never a full number). A form we miss costs extra reads, never a missed card.
   - **Card index** (`card_index` table): per card, email count, learned number forms, the Gmail search for its statement PDFs and those emails' ids (newest first), so later jobs go straight to them. Gmail labels aren't an option with read-only access.
   - **Someone else's card is hidden:** if every email that names a cardholder names someone other than the user (lenient match: any shared name word counts), the card isn't shown. The user can still say it's theirs.
   - **Already-read emails are skipped** (the evidence table), and **if the budget runs out** the job stops cleanly and resumes later; the user sees "Still checking older emails".
   - The earlier read-everything scan remains behind a switch (`CD_CARD_AWARE=0`) for comparison.
2. **`pdf_enrich`** (only if the user opted in): the **newest statement PDF per owned, active card** (dormant and may-be-closed cards have no recent statements), taken from the card index, or found by one search on the card's number if the index has none:
   - A full read lists the attachment id (60 units), then `attachments.get` (20 units), with a size cap: **80 units per card**.
   - Decrypt using the bank's password pattern from the rules file, built from name + date of birth (stored encrypted). Otherwise ask the user for that bank's password.
   - Extract text; per-bank statement parser; no text layer → OCR (optional).
   - Pull out card facts: number, product, credit limit, points, fees, statement date, totals.
   - **Reconcile:** do the parsed transactions add up to the stated total? If not, mark the parse as unreliable and don't use its fields.
   - The PDF exists **only in worker memory** (or an encrypted tmpfs). It's discarded when the item finishes.
3. Cards are re-scored; the app shows "Details added from your statements".

### 3.4 Daily sync
- Cron enqueues `daily_sync` for each active connection (search-based, §3.4), **spread across the day** so the workers aren't hit all at once. It also runs on app open if more than 12 h have passed since the last sync. Running daily keeps the token in use, so Google's "unused for 6 months" expiry never triggers.
- The worker runs **one search** (`from:(bank senders) after:<last_sync_date − 1 day>`, 5 units; the one-day overlap covers Gmail's date granularity), skips message IDs already processed, and reads only the new bank emails: header reads at 20 units, full reads only when needed.
- Typical cost: **about 50–150 units per user per day.**
- Gmail's history API (`history.list`, 2 units) is used only once push notifications (`users.watch`) are added, because history lists every mailbox change, not just bank mail.
- **New card** → a "We found a new card: add it?" suggestion, plus an optional push notification. **Updated details** → applied quietly.
- **Token refresh fails** (revoked, expired, Testing-mode 7-day limit) → mark the connection `needs_reconnect` → the app shows a banner. Saved cards stay.

### 3.5 Disconnect and delete
- Revoke the token at `oauth2.googleapis.com/revoke`.
- Delete the encrypted token, the evidence, the PDF-derived facts and the cards with `source = gmail`.
- Cancel queued and running jobs; the worker checks a cancel flag between steps. This is the race fixed in the POC (`597ab07`).
- Deleting the account also removes the user, the sessions and the picker cards.

---

## 4. AI

| Use | Where | What it sees | Guardrails |
|---|---|---|---|
| Unknown email formats (rules return "unknown" for a bank email) | Worker → model API | **Redacted** subject and body: full card numbers removed, names, addresses, phone numbers and amounts masked | The model's card number and product must appear word-for-word in the email; the model never decides ownership (scoring does) |
| Merchant name clean-up ("SWIGGY*BLR 0012" → "Swiggy") | Worker → model API, cached per merchant string | Merchant strings only | Global cache of merchant strings, no user identifiers |
| Drafting new parsers | **Offline**, a developer tool | Synthetic or consented examples only, **not** user mail | **Human review** before release; versioned in the parser registry |
| Training models | **Never on user Gmail data** | — | Google's Workspace API policy forbids general models built from user data |

The AI provider is a sub-processor: disclose it in the privacy policy and the Google verification, and use zero-retention terms. Expected volume is about 10–50 unclear emails per user on the first scan, near zero on daily syncs.

---

## 5. Security of the server-side design

| Concern | Control |
|---|---|
| Refresh tokens (the most valuable data held) | Envelope encryption with an AWS KMS key; **only the worker role** can decrypt (IAM); the API can't read tokens; every decrypt is audit-logged; tokens never in logs |
| Google auth code in transit | HTTPS only; single use; exchanged immediately; never logged |
| Email text and PDFs | Worker memory or encrypted tmpfs only; never in Postgres, S3, logs, queues or crash reports. Queue rows carry IDs, never content |
| Full card numbers | Blanked out on sight (already in the POC) |
| Forged bank emails | Trust only the `Authentication-Results` header Gmail added (already in the POC) |
| Test mode | Development builds only; never deployed (already locked in the POC) |
| Date of birth (PDF passwords) | Encrypted like tokens; used only inside the PDF job; optional (the user can type passwords instead) |
| App → API | App session plus attestation (Play Integrity / App Attest); rate limits on OTP and connect |
| Employees | No one can read email or tokens; break-glass access needs two people and is logged |
| Data residency | AWS Mumbai (ap-south-1) |
| Deletion | Disconnect and delete within 24 h; backups expire within 30 days (state this in the privacy policy) |

---

## 6. What is stored where

| Data | Store | Notes |
|---|---|---|
| Accounts, sessions, OTPs | Postgres | Sessions and OTPs stored hashed |
| Picker cards | Postgres `cards` (`source = manual`) | No Gmail restrictions |
| Gmail connection: encrypted refresh token, granted scopes, last sync time (plus a history bookmark once push is added), status, units used | Postgres `gmail_connections` | Token encrypted with KMS |
| Derived evidence per email (type, bank, last 4, product, date, flags; **no text**) | Postgres `evidence` | Lets daily syncs skip emails already read |
| Gmail-found cards | Postgres `cards` (`source = gmail`) | Source tag enforces Google's Limited Use rules (no ads, partners or lending) |
| Statement facts (statement date, total due, credit limit, points, fees) | Postgres `statement_facts` | Per card, per statement |
| Card index: email count, masked number forms (e.g. `XX2846`), the statement-PDF search and those emails' ids | Postgres `card_index` | No email text and no full card numbers; deleted on disconnect |
| Transactions | **Open decision (Q1)** | Not stored by default |
| Decisions (Yes/No) | Postgres | Sticky |
| Jobs | Postgres `jobs` | Status, attempts, errors; no content |
| Rules, parsers, password patterns | Postgres or S3, versioned and signed | |
| Merchant name cache | Postgres | Merchant strings only |
| Email text, HTML, PDFs | **Not stored** | |
| Google passwords, OTP codes in plain text, full card numbers | **Never** | |

---

## 7. API (sketch)

```
POST /auth/otp/send            {phone}
POST /auth/otp/verify          {phone, code}                → session
GET  /catalogue/banks          ; GET /catalogue/banks/{id}/cards ; networks only when > 1
POST /cards                    {catalogue_card_id, network?, last4?}       (picker)
GET  /cards                    ; POST /cards/{id}/decision {yes|no}
POST /gmail/connect            {server_auth_code, years, pdf_opt_in}       → 202
GET  /discovery/status         → {phase, progress, new_cards[]}  (or SSE /discovery/events)
POST /gmail/rescan             ; POST /gmail/disconnect
DELETE /account
```

---

## 8. Job queue (Postgres)

```sql
jobs(id, kind, account_id, payload jsonb, status, run_after, attempts, last_error,
     locked_by, locked_at, created_at)
-- kinds: discover_quick, discover_full, pdf_enrich, daily_sync, ai_extract, token_check, cleanup
-- worker loop: SELECT … WHERE status='queued' AND run_after<=now()
--              ORDER BY priority, run_after FOR UPDATE SKIP LOCKED LIMIT 1
```

- **Priority:** `discover_quick` > `discover_full` > `daily_sync` > `pdf_enrich` > `ai_extract`.
- **Per-user Gmail limiter:** a rolling-minute limiter of **5,900 units per minute** per Gmail connection, below Google's 6,000. Every call is charged its unit cost (5 / 20 / 60 / 20 / 2) **before** it's sent. Back off on 429/5xx.
- **Per-job budgets:** `discover_quick` 1,000 units, `discover_full` 15,000, `daily_sync` 500, `pdf_enrich` 20 per PDF. A job that hits its budget saves its place and requeues with a delay.
- **Project-wide daily meter:** units used today, across all users, in Postgres (later Redis). Alert at 60% and 80% of 80M. Above 90%, defer `discover_full` and `pdf_enrich` to the next day; keep `discover_quick` and `daily_sync` running.
- **Network failures:** the job returns to `queued` with an increasing `run_after` and resumes from its saved progress. The evidence table makes this safe to repeat.
- **Cancellation:** a flag on the connection, checked between steps.
- **Move to SQS** when one Postgres instance can't keep up with job churn. Keep the same job kinds.

---

## 9. Gmail API volume and cost (May 2026 quotas)

Unit costs from §1a: search 5, header read 20, full read 60, PDF 20.

### Per user

Measured on real Gmail with the simulated 10,000-email, 5-year inbox (12 cards plus an add-on, 2,489 promotions, 668 debit/savings emails, traps), at a 5,900 units/minute cap:

| | Quick scan | Full scan, 3 years | Full scan, 5 years | Statement PDFs | Daily sync (estimate) |
|---|---|---|---|---|---|
| Searches | 6 (30) | about 150 (about 750) | 206 (1,030) | 1–2 (5–10) | 1 (5) |
| Header reads | 40 (800) | about 110 (about 2,200) | 165 (3,300) | — | about 2–5 (40–100) |
| Full reads | 0 | about 20 (about 1,300) | 15 (900) | 4 (240) | about 0–1 (0–60) |
| PDF downloads | — | — | — | 4 (80) | — |
| **Units** | **832** | **about 4,300** (dry run) | **5,232** | **330** | **about 50–150** |
| Time | first card 2–4 s | — | 61 s | 4–16 s | under 2 s |

The full scan is no longer limited by quota (no job waited for the per-minute limit): its time is about 200 searches in sequential batches, at 1–2 s per call to Gmail.

**How it got here** (same inbox, 3 years):

| Version | Full-scan units | Full-scan time | Cards right |
|---|---|---|---|
| Read everything the searches return (catch-all reads promotions, every statement in full) | about 33,000 (dry run) | — | 12/12 (dry run) |
| Noise filtered in the search itself (`-category:promotions`, `-subject:` OTP/debit/savings/offers), sampled payments | 11,782 | 126 s | 11/12 (add-on missed) |
| Card-aware: known card numbers excluded, per-card searches | 2,997 | 11 s | 11/12 (add-on missed) |
| Card-aware with 6-month windows, newest statement in full, learned number forms (5 years) | 5,232 | 61 s | **13/13** |

### Product-wide, against 80M units a day

| Workload | Units each | What 80M a day allows on its own |
|---|---|---|
| New user, 3 years: quick + full + PDFs | about 5,500 (measured 4,179 before windows and add-ons) | about 14,000–19,000 new users a day |
| New user, 5 years: quick + full + PDFs | 6,394 (measured) | about 12,500 new users a day |
| Daily sync | about 50–150 | about 500,000–1,600,000 connected users |
| **Example mix:** 300,000 connected users syncing daily + 5,000 new users a day (5 years) | about 30M + about 32M = **about 62M** | Fits, with headroom |

When the meter trends towards 80M, the levers are:
- a shorter default lookback
- deferring Phase 2 and PDFs
- fewer header reads per bank
- Gmail push notifications instead of daily polling (only new mail is touched)

The daily threshold can't be raised, and pricing above it hasn't been published.

### Cost

| Item | Estimate |
|---|---|
| Gmail API | Free under 80M units a day per project; pricing above that not yet published |
| EC2 workers (start) | 1–2 small instances; the per-user quota limits throughput more than CPU does |
| Postgres (RDS) | 1 small instance with backups |
| KMS | About $1/month per key plus a small per-request fee |
| OTP SMS | Per-SMS rate from an Indian provider (DLT) |
| AI fallback | Under $0.01 per user at Claude Haiku 5.5 rates ($0.10 / $0.50 per million tokens); other providers per their pricing |
| CASA assessment | Required; reported about $550 (lower level, AL1) to about $5,400 (higher level, AL2) a year; Google assigns the level |

## 10. Google approval (start now)

1. A verified domain, homepage and privacy policy with the **Limited Use statement**.
2. An OAuth consent screen with the `gmail.readonly` justification, and a **demo video** of the explainer, the consent and the use of the data.
3. **CASA security assessment** (Tier 2), mandatory with server-side storage and processing. Get assessor quotes now.
4. Until approved: Testing mode, at most 100 test users, sign-ins expiring after 7 days. Enough for the internal pilot.

---

## 11. Open decisions

| # | Question | Recommendation |
|---|---|---|
| Q1 | Store transactions from statements (for merchant names and spend insights), or only use them in memory to reconcile? | Don't store them in v1; store per-statement totals only. Storing transactions widens privacy scope and the CASA review |
| Q2 | PDF passwords: date of birth (encrypted, patterns per bank) or the user types each bank's password? | Ask for the date of birth once, with a fallback to asking per bank |
| Q3 | Add high-confidence Gmail cards automatically, or always as suggestions? | Pre-tick high-confidence suggestions; the user taps "Add" once |
| Q4 | Progress delivery to the app: polling or SSE? | Polling every 1–2 s in v1; it's simpler on mobile networks |
| Q5 | Daily sync timing | Spread across the day; also on app open if more than 12 h have passed since the last sync |
| Q6 | AI provider and model | Per the cost and privacy review; zero-retention terms required |
| Q7 | Outlook / SMS sources | Later; same pipeline with a new provider adapter |

---

## 12. What carries over from the POC

| POC module | Becomes |
|---|---|
| `queries.py`, `classify.py`, `extract.py`, `resolve.py`, `score.py`, `explain.py` | The worker's engine, unchanged in shape |
| `providers/gmail.py` | Gmail client inside the worker (tokens from the vault instead of a file) |
| `ledger.py` (SQLite) | Postgres `evidence` table |
| `accounts.py` | API auth (real SMS sender) |
| `web.py` callback flow | Replaced by the app's server auth code plus `/gmail/connect` |
| `rules.py` signed bundle | Rules and parser registry |
| Test kits (clean, messy, HTML, long, professional, batches 2–3) | Regression suite for every parser change |
| Disconnect cancellation fix | Job cancellation flag |

---

## 13. The mini version (what we build next)

The same architecture, the same components and the same job flow, on one machine and at small scale. Every stand-in sits behind the interface the production part will use, so going to production means swapping implementations, not redesigning.

### 13.1 Components: production → mini

| Production | Mini version | Same interface? |
|---|---|---|
| Mobile app (picker, connect, progress, review, settings) | The phone-frame web page from the POC, extended with the **card picker** and **"found by Gmail" suggestions** | Same screens and API calls |
| Google Sign-In SDK → **server auth code** | Google's web consent redirect to the API, which receives the **same one-time code** and does the same exchange (desktop OAuth client, `127.0.0.1`) | Same `/gmail/connect` logic after the code arrives |
| API service | Python API process (the POC server grown into §7's endpoints) | Same endpoints |
| Postgres (RDS) | **Postgres 18**, the Mac's default Homebrew service (`localhost:5432`, database `carddiscovery`) | Same schema |
| Job queue (Postgres, `SKIP LOCKED`) | **Same:** `jobs` table and claim query | Identical |
| EC2 workers | 1–2 `carddiscovery worker` processes | Identical code |
| Cron | `carddiscovery scheduler` process (enqueues daily syncs, token checks, cleanup) | Same job kinds |
| KMS envelope encryption | A local master key file (`0600`) used through the same `encrypt()` / `decrypt()` interface, with a per-token data key (AES-GCM) | Swap for KMS later |
| Per-user rate limiter + daily meter | In Postgres: units charged per call, 5,900/min per connection, daily total with alert thresholds | Same numbers |
| Login (phone + OTP) | **Out of scope: a separate login service.** The mini version takes that service's user id through `/api/dev/login`; accounts are keyed by `external_id` | Production verifies the login service's token instead |
| App attestation | Skipped (local only) | — |
| Push notifications | In-app banner on next open | — |
| AI fallback | Interface present, **off by default**; on, it calls a model with redacted text | Same guardrails |
| Rules / parser registry | `banks.toml` + `rules.json` + signed bundle (from the POC) | Same |
| Statement PDFs | `pypdf` + password patterns from the rules file; in memory only | Same |

### 13.2 Jobs in the mini version

| Job | Budget | What it does |
|---|---|---|
| `discover_quick` | 1,000 units | §3.2: about 15 searches, about 40 header reads round-robin by bank, cards streamed |
| `discover_full` | 15,000 units | §3.3: 3 years, sampled alerts (12 months), tightened catch-all, full reads only when needed, resumable |
| `pdf_enrich` | 20 per PDF | Statements from the last 6 months for confirmed cards, if opted in |
| `daily_sync` | 500 units | One search since the last sync, new mail only |
| `token_check` | 0 | Marks connections `needs_reconnect` when a refresh fails |

### 13.3 Build order

| Step | Build | Done when |
|---|---|---|
| 1 | Postgres schema (§6), DB access layer, migrations | Tables created by one command |
| 2 | Job queue: enqueue / claim (`SKIP LOCKED`) / complete / retry with backoff / cancel; `worker` process | Two workers never take the same job; a crashed job is retried |
| 3 | Token vault (local envelope encryption) + `/gmail/connect` code exchange | Refresh token stored encrypted; the API process can't read it |
| 4 | Quota layer: per-call unit charging, 5,900/min limiter, per-job budgets, daily meter | Unit tests show a 15,000-unit job pausing and resuming |
| 5 | `discover_quick` (round-robin, about 40 reads) + streaming status to the page | Your test inbox shows cards in about 10 s |
| 6 | `discover_full` with the cost fixes (tightened catch-all, full reads only when needed) | Simulation scan costs about 10,000 units, not 49,000 |
| 7 | Card picker (bank → card → network only when needed) + Gmail suggestions merged into it | Picker and Gmail cards in one list, with `source` tags |
| 8 | `daily_sync` + scheduler + "new card found" banner + reconnect banner | A new test email appears as a suggestion after the next sync |
| 9 | `pdf_enrich` with password patterns, a date-of-birth prompt and reconciliation | A test statement PDF adds credit limit and points |
| 10 | Disconnect / delete across all stores; cancellation of running jobs | Everything for the user is gone; a running job stops |

Every step keeps the existing test kits passing, plus new tests for the queue, the limiter and the vault.

### 13.4 To confirm before building
1. **Postgres:** the local Homebrew Postgres 18, or a Docker container (cleaner to reset)?
2. **Q1 (§11):** totals only from statements, not transactions, in the mini version?
3. **Q2 (§11):** date of birth once, with a per-bank password fallback?
