# Card discovery from email

Finds the credit cards a person owns by reading their Gmail (read-only), including old, unused, upgraded and closed cards. It ignores promotions, offers, applications, OTPs and debit-card mail.

This is the first working version of the pipeline described in [docs/card-discovery-design.md](docs/card-discovery-design.md): a command-line tool you run on your own inbox to see what it finds.

## How it works

1. **Search, don't scan.** It runs typed Gmail searches built from a list of bank sender domains (`carddiscovery/data/banks.toml`). Rare, valuable mail (statements, welcome, dispatch, annual fee, upgrade, closure, applications) is read in full. Transaction alerts are sampled: up to 40 per 6-month window.
2. **Headers first.** Each email is classified from its sender, subject and Gmail's preview text. The full email is fetched only when the card number or product is missing. Attachments are never downloaded.
3. **Rules, not AI (yet).** Classification and extraction are regex rules (`classify.py`, `extract.py`). Any full card number is blanked out before anything else reads the text.
4. **Evidence to cards.** Each email becomes "evidence". Evidence is grouped into cards by bank + last 4 digits (`resolve.py`). An email without a number attaches to a card only when exactly one card matches. Otherwise it becomes a card for you to confirm.
5. **Scoring.** Ownership and status come from the evidence (`score.py`). A card with no recent mail is marked **dormant**. It is marked **closed** only when there is a closure email.

Nothing from the email text is stored. A local SQLite file (`~/.carddiscovery/ledger.db`) keeps only what was derived (type, bank, last 4 digits, product, date, flags), so a second run skips emails it has already read.

## Setup (one time, about 10 minutes)

You need your own Google Cloud OAuth client. The app stays in "Testing" mode, which is enough for you and up to 100 test users.

1. Go to <https://console.cloud.google.com/>, create a project, then go to **APIs & Services → Library** and enable the **Gmail API**.
2. Under **APIs & Services → OAuth consent screen**, choose External, fill in the app name and your email, and add the scope `https://www.googleapis.com/auth/gmail.readonly`. Under **Test users**, add the Gmail addresses you'll test with.
3. Under **APIs & Services → Credentials → Create credentials → OAuth client ID**, choose **Desktop app**. Download the JSON file and save it in this folder as `credentials.json`. Git already ignores it.
4. Install:

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e '.[dev]'
```

5. Sign in. A browser window opens; Google warns that the app isn't verified. That's expected in Testing mode, so continue.

```bash
.venv/bin/carddiscovery auth
```

In Testing mode, Google expires the sign-in after 7 days, so you'll have to sign in again after that.

## Run it

```bash
.venv/bin/carddiscovery discover --name "Your Name" --show-unknown --json results.json
```

- `--name` is your name as banks write it, and can be repeated. It's used to spot cards that belong to someone else.
- `--show-unknown` prints subjects of bank emails the rules couldn't classify. They're printed on screen only, never saved. This is the fastest way to find gaps in the rules.
- `--fresh` ignores cached results and reads everything again (use it after changing rules).
- `--catalog cards.csv` adds product names from your catalogue. Columns: `bank_id,product,aliases`, with aliases separated by `|`.

Output is grouped into **YOUR CARDS**, **PLEASE CONFIRM** and **HISTORY**, plus applications and run stats (time, emails fetched, how many passed the bank signature check).

**No credit cards to test with?** Send yourself the emails in [docs/test-emails.md](docs/test-emails.md) and run with `--test-sender you@gmail.com`. Mail from that address is treated as bank mail, and test results never appear in normal runs.

Find card-related senders that aren't in the bank list yet:

```bash
.venv/bin/carddiscovery senders
```

Delete the local token and cache:

```bash
.venv/bin/carddiscovery forget
```

## Improving results

| You see | Fix |
|---|---|
| A bank's mail is missing entirely | Run `carddiscovery senders` and add the domain to `banks.toml` |
| Card shows as "variant unknown" or "(unrecognised)" | Add the product or alias under that bank in `banks.toml`, or pass `--catalog` |
| Bank emails listed under UNCLASSIFIED | Add a pattern for that wording in `RULES` in `classify.py` |
| A real card sits in PLEASE CONFIRM because of a failed signature check | That bank signs from a domain we don't know: add it to `banks.toml` |

After changing rules, bump `EXTRACTOR_VERSION` in `carddiscovery/__init__.py` (or run with `--fresh`) so cached results are recalculated.

## Tests

```bash
.venv/bin/pytest
```

The tests replay the scenarios from the design doc's test matrix (§13) on synthetic emails.

## Not built yet

AI fallback for emails the rules miss · Outlook · API server and database · background sync · credit bureau check. See the design doc for the plan.
