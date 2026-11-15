"""Test-sender mode is development-only; the test suite is a development context."""

import os

os.environ.setdefault("CARDDISCOVERY_DEV", "1")
