# Card discovery from email (proof of concept)

Finds the credit cards a person owns by reading their Gmail (read-only), including old, unused, upgraded and closed cards. It ignores promotions, offers, applications, OTPs and debit-card mail.

This POC runs on your computer. It mirrors the mobile design in [docs/mobile-design.md](docs/mobile-design.md): email is read and processed locally, and only card facts are kept. There are two ways to use it:

- **`carddiscovery serve`**: the real app flow in a phone frame. **Phone number → OTP → wallet → our explainer (which banks, how far back) → Google's own consent screen → scan → review with Yes/No → cards saved to your account.** Settings has scan again, disconnect Gmail, log out and delete account.
- **`carddiscovery discover`**: the same engine from the terminal.

## How it works

1. **Search, don't scan.** Gmail searches are built from a list of bank sender domains, limited to how far back you chose (default 5 years). Statements, welcome, dispatch, fee, upgrade, closure and application emails are read in full. Transaction alerts are sampled.
2. **Headers first.** Each email is classified from its sender, subject and Gmail's preview. The full email is fetched only when needed; statements are always read in full. Attachments are never downloaded.
3. **Rules as data.** Bank list, classification rules, ad and boilerplate patterns, and scoring weights live in `carddiscovery/data/` (`banks.toml`, `rules.json`), not in code. They can be packed into a **signed bundle**; a tampered bundle is refused.
4. **Evidence to cards.** Each email becomes evidence; evidence is grouped by bank + last 4 digits. An email without a number joins a card only when exactly one matches.
5. **Plain-language results.** Each card shows why it was found ("3 statements, Oct–Nov 2026"), its status ("Not used recently", "Replaced by ••7777 after an upgrade") and any concern ("addressed to someone else by name"). Uncertain cards ask "Is this your card?". Your answers are saved and always win.

## Privacy and security in this POC

| | |
|---|---|
| Google access | Read-only (`gmail.readonly`); token in `~/.carddiscovery/token.json`, readable only by you |
| What's read | Only emails from the bank list, within the chosen years |
| What's stored | Derived facts only (bank, product, last 4 digits, dates, flags) plus your Yes/No answers, in `~/.carddiscovery/ledger.db`. No email text. |
| Full card numbers | Blanked out before anything else reads the text |
| Forged bank emails | Only the signature result Gmail itself added (`mx.google.com`) is trusted; a sender can't fake one |
| Test mode | `--test-sender` only works with `CARDDISCOVERY_DEV=1`, and test emails must still pass Gmail's signature check for the test sender's own address |
| Local web page | Listens on `127.0.0.1` only, needs a random token on every request, rejects foreign Host headers, and never returns email text |
| App login | Phone + 6-digit OTP: stored only as an HMAC, valid 5 minutes, 5 wrong tries burns it, resend after 30 s, max 5 per hour, single use. Sessions are random tokens stored hashed, in an HttpOnly cookie, valid 7 days. Accounts live in `~/.carddiscovery/accounts.db`. |
| Google consent | A redirect from the app with a single-use `state` and PKCE; the callback must match the logged-in account. One Google token per account in `~/.carddiscovery/tokens/`. |
| Real SMS | Not wired up. A real sender (MSG91, Twilio, Gupshup...) plugs into `accounts.OtpSender`; in India it needs DLT registration of the sender ID and message template. |
| Disconnect | `carddiscovery forget` (or the button on the page) revokes access with Google and deletes the token and cache |
| Printed subjects | `--show-unknown` hides digits before printing |

## Setup (one time, about 10 minutes)

You need your own Google Cloud OAuth client in "Testing" mode (up to 100 test users; sign-ins expire after 7 days).

1. In <https://console.cloud.google.com/>, create a project, then **APIs & Services → Library → Gmail API → Enable**.
2. **APIs & Services → OAuth consent screen:** External; add the scope `https://www.googleapis.com/auth/gmail.readonly`; under **Test users**, add the Gmail address you'll scan.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID → Desktop app.** Download the JSON and save it in this folder as `credentials.json` (ignored by git).
4. Install:

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e '.[dev]'
```

## Run the POC

### With your own test emails (no credit cards needed)

1. From a **different** Gmail account, send the emails in [docs/test-emails.md](docs/test-emails.md) (and the sets in `tests/messy_emails.py`, `tests/html_emails.py`, `tests/long_emails.py`) to the account you'll scan. Using a different account matters: Gmail then adds a real signature result, which test mode checks.
2. Start the web page in test mode:

```bash
CARDDISCOVERY_DEV=1 .venv/bin/carddiscovery serve --test-sender your.personal@gmail.com --name "Your Name"
```

3. In the phone frame:
   1. Enter any 10-digit Indian mobile number and tap **Get OTP**. In this development build the code is **printed in the terminal** running `serve` (look for `[DEV OTP]`), not sent by SMS.
   2. Enter the code. You land on your (empty) wallet.
   3. **Find my cards from Gmail** → choose how far back → tick the consent → **Continue with Google**.
   4. Google's own screen opens: pick the account you're scanning and allow "View your email messages and settings". Google handles your Google password and 2-step verification; the app never sees them. If you untick Gmail access or press Cancel, the app shows what happened and nothing is read.
   5. You come back to the app, the scan runs, and you review the cards. They're saved to your account and shown on the wallet.

Or from the terminal:

```bash
CARDDISCOVERY_DEV=1 .venv/bin/carddiscovery discover --test-sender your.personal@gmail.com --name "Your Name" --fresh
```

### With real bank mail

The same commands without `CARDDISCOVERY_DEV=1` and `--test-sender`:

```bash
.venv/bin/carddiscovery serve --name "Your Name"
```

Results from test runs are tagged and never appear in a normal run.

## Commands

| Command | What it does |
|---|---|
| `serve` | Local web page that mimics the mobile flow |
| `discover` | Find cards from the terminal. `--years 1-15` (default 5), `--fresh`, `--details` (scores instead of sentences), `--show-unknown`, `--json FILE`, `--catalog FILE`, `--bundle FILE` |
| `decide CARD yes\|no\|clear` | Answer "is this your card?" from the terminal (the card id is printed by `discover`, e.g. `icici:9191`) |
| `senders` | Card-related senders not in the bank list yet |
| `forget` | Revoke Google access and delete the local token and cache |
| `bundle keygen` | Create a signing key (`~/.carddiscovery/bundle_signing_key.pem`) and write the public key to `carddiscovery/data/bundle_public.pem` |
| `bundle build` | Sign the current `banks.toml` + `rules.json` into `dist/bundle.json` + `dist/bundle.sig` |
| `bundle verify` | Check a bundle's signature |

To use a signed bundle instead of the source files:

```bash
.venv/bin/carddiscovery bundle keygen
```

```bash
.venv/bin/carddiscovery bundle build
```

```bash
.venv/bin/carddiscovery discover --bundle dist/bundle.json
```

## Improving results

| You see | Fix |
|---|---|
| A bank's mail is missing entirely | Run `carddiscovery senders` and add the domain to `data/banks.toml` |
| "variant unknown" or "(unrecognised)" | Add the product or alias to `data/banks.toml`, or pass `--catalog` |
| Bank emails listed by `--show-unknown` | Add a pattern for that wording to `rules` in `data/rules.json` |
| Real cards stuck in "Please confirm" with "couldn't confirm the emails came from the bank" | That bank signs from a domain not in the list: add it to `data/banks.toml` |

After changing rules, run with `--fresh` (or bump `EXTRACTOR_VERSION` in `carddiscovery/__init__.py`) so cached results are recalculated.

## Tests

```bash
.venv/bin/pytest
```

The tests cover the design doc's scenario matrix, the four test-email sets (clean, messy, short HTML, long formal HTML), forged-signature and test-mode security checks, signed bundles, saved answers, and the local web server.

## Not in this POC

Mobile app · AI fallback for emails the rules miss · Outlook · Android SMS · background refresh · backend API. See [docs/mobile-design.md](docs/mobile-design.md) for the plan.


## Quick start with Docker (one command)

Needs Docker Desktop and a Google OAuth client for a **Desktop app** with the Gmail API enabled
(download it as `credentials.json` into this folder; it's git-ignored, never commit it). While the
Google project is in Testing mode, your Gmail address must be listed under **Test users**.

```bash
docker compose up --build
```

Open the `http://127.0.0.1:8790/?t=...` link printed in the log, then **Find my cards from Gmail**.
Postgres runs in its own container; data and the encryption key live in Docker volumes.

```bash
docker compose logs app | grep API:     # the link again
docker compose exec app python -m cdserver jobs   # Gmail units, time, CPU, memory per job
docker compose down                      # stop (keeps data)
docker compose down -v                   # stop and delete all data, including the key
```

## Mini server (`cdserver`)

The server-side version from [docs/architecture.md](docs/architecture.md) §13, on one machine:
API, workers claiming jobs from a Postgres queue, a scheduler, encrypted Gmail tokens, Gmail
quota metering and statement PDFs. Login is a separate service; `/api/dev/login` stands in for it.

```bash
pip install -e ".[dev,server]"
createdb carddiscovery                       # the Mac's default Postgres on localhost:5432
python -m cdserver dev --name "Your Name"     # API on 127.0.0.1:8790 + 2 workers + scheduler
python -m cdserver jobs                      # what each job cost: units, time, CPU, memory
python -m cdserver monitor                   # memory and CPU of every process, until Ctrl+C
```

Simulated 10,000-email inbox for end-to-end tests on real Gmail (nothing is sent; emails are
imported into a test inbox you own):

```bash
python -m cdserver sim dryrun                # offline run of the same scan code
python -m cdserver sim import --to you@example.com
python -m cdserver evaluate --mailbox you@example.com   # after connecting it in the app
python -m cdserver sim clean --to you@example.com       # moves the simulated mail to Trash
```
