"""Settings, read from the environment once."""

from __future__ import annotations

import getpass
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("CD_HOME", ROOT / ".local"))

# The Mac's default Postgres (Homebrew service on localhost:5432, user = your macOS login).
DATABASE_URL = os.environ.get("CD_DATABASE_URL", f"postgresql://{getpass.getuser()}@localhost:5432/carddiscovery")
MASTER_KEY_FILE = Path(os.environ.get("CD_MASTER_KEY", HOME / "master.key"))
GOOGLE_CLIENT_FILE = Path(os.environ.get("CD_GOOGLE_CLIENT", ROOT / "credentials.json"))
API_PORT = int(os.environ.get("CD_PORT", "8790"))
# Address the API listens on. 127.0.0.1 locally; 0.0.0.0 inside Docker, where the compose file
# publishes the port on the host's 127.0.0.1 only. The browser still opens http://127.0.0.1:8790.
API_BIND = os.environ.get("CD_BIND", "127.0.0.1")

# Test senders (dev builds only, see carddiscovery.registry.dev_mode): personal
# addresses whose mail is treated like bank mail, for testing without real cards.
TEST_SENDERS = [s.strip() for s in os.environ.get("CD_TEST_SENDERS", "").split(",") if s.strip()]

# Gmail API quota units (Google's usage-limits page, May 2026).
UNITS = {"search": 5, "metadata": 20, "full": 60, "attachment": 20, "profile": 1, "history": 2}
PER_USER_UNITS_PER_MIN = 5_900          # Google allows 6,000; 100 units of headroom
PROJECT_UNITS_PER_DAY = 80_000_000      # can't be raised
DEFER_BACKGROUND_ABOVE = 0.90           # of the daily limit

# Units one run of a job may spend before it pauses (RESUME_DELAY_S) and resumes. The meter already
# keeps to Gmail's 6,000 a minute, so a full scan's round is large enough to finish in one go:
# a pause only adds a minute (15,000 cost a real 3-year scan a 60 s pause).
BUDGETS = {"discover_quick": 1_000, "discover_full": 40_000, "daily_sync": 500, "pdf_enrich": 4_000, "statements": 4_000,
           "token_check": 10, "disconnect": 0, "llm_fallback": 6_000, "forwards": 1_500, "report_inbox": 2_000}
PRIORITY = {"disconnect": 0, "discover_quick": 10, "statements": 15, "discover_full": 20, "daily_sync": 30,
            "pdf_enrich": 40, "token_check": 50, "llm_fallback": 35, "forwards": 12, "report_inbox": 12}

CARD_AWARE_FULL_SCAN = os.environ.get("CD_CARD_AWARE", "1") != "0"  # search known cards' numbers
# "monthly": month by month subjects, then statements and PDFs of incomplete cards over 6 months,
# then a lifecycle pass (jobs._monthly_full_scan). "subjects": one year at a time, subjects only,
# statement PDFs only for incomplete cards
# (engine.subject_scan). Anything else: the card-aware scan.
SCAN_MODE = os.environ.get("CD_SCAN_MODE", "card_aware")
# Statement PDFs are indexed by the full scan (5 units per card) but not downloaded and read
# (80 units per card) unless this is on. Off for now: PDF analysis comes later.
READ_STATEMENT_PDFS = os.environ.get("CD_READ_PDFS", "1") == "1"  # still needs the user to opt in
QUICK_LOOKBACK_YEARS = 1
QUICK_MAX_READS = 40
FULL_ALERT_YEARS = 1
FULL_CATCH_ALL_CAP = 200
PDF_MONTHS = 6
SYNC_EVERY_HOURS = 24
RESUME_DELAY_S = 60
MAX_ATTEMPTS = 5
STALE_LOCK_MIN = 10

# The team's report inbox: users forward a bank email we showed wrongly to this address. It is connected
# like any Gmail account (read-only) but never scanned for cards; the scheduler reads it every few minutes.
REPORT_INBOX = os.environ.get("CD_REPORT_INBOX", "").strip().lower()
REPORT_EVERY_MIN = int(os.environ.get("CD_REPORT_EVERY_MIN", "10"))

# Where people reach the app (EC2: e.g. https://cards.example.com). Allowed Host header, cookie
# security and the Google sign-in return address all follow it; Google's console must list
# <PUBLIC_URL>/oauth/callback as an authorised redirect URI.
PUBLIC_URL = os.environ.get("CD_PUBLIC_URL", f"http://127.0.0.1:{API_PORT}").rstrip("/")
# "?user=<name>" sign-in as any test user: local development only, never on a shared server.
DEV_LOGIN = os.environ.get("CARDDISCOVERY_DEV") == "1"
# Who sees the developer panel and may call /api/dev/*: these Gmail addresses (comma-separated), plus
# everyone in local development. Normal users never see it, and the endpoints answer 404 to them.
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("CD_ADMIN_EMAILS", "").split(",") if e.strip()}
