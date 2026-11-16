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

# Test senders (dev builds only, see carddiscovery.registry.dev_mode): personal
# addresses whose mail is treated like bank mail, for testing without real cards.
TEST_SENDERS = [s.strip() for s in os.environ.get("CD_TEST_SENDERS", "").split(",") if s.strip()]

# Gmail API quota units (Google's usage-limits page, May 2026).
UNITS = {"search": 5, "metadata": 20, "full": 60, "attachment": 20, "profile": 1, "history": 2}
PER_USER_UNITS_PER_MIN = 5_500          # Google allows 6,000; keep headroom
PROJECT_UNITS_PER_DAY = 80_000_000      # can't be raised
DEFER_BACKGROUND_ABOVE = 0.90           # of the daily limit

BUDGETS = {"discover_quick": 1_000, "discover_full": 15_000, "daily_sync": 500, "pdf_enrich": 1_000,
           "token_check": 10, "disconnect": 0}
PRIORITY = {"disconnect": 0, "discover_quick": 10, "discover_full": 20, "daily_sync": 30,
            "pdf_enrich": 40, "token_check": 50}

QUICK_LOOKBACK_YEARS = 1
QUICK_MAX_READS = 40
FULL_ALERT_YEARS = 1
FULL_CATCH_ALL_CAP = 200
PDF_MONTHS = 6
SYNC_EVERY_HOURS = 24
RESUME_DELAY_S = 60
MAX_ATTEMPTS = 5
STALE_LOCK_MIN = 10
