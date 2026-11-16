"""Who is calling. Login itself (phone + OTP) belongs to a separate service; this
service only needs that service's user id.

In production the API would verify the login service's token on each request.
In the mini version, /api/dev/login stands in for it: it takes a user id and
issues our own session cookie (random 256-bit token, stored only as a SHA-256 hash).
"""

from __future__ import annotations

import hashlib
import re
import secrets

SESSION_TTL = 7 * 24 * 3600
_USER_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def login(c, user_id: str) -> tuple[dict, str]:
    """Account for this external user id (created on first sight) and a new session token."""
    if not _USER_ID.match(user_id or ""):
        raise ValueError("user id must be 1-64 letters, digits or _.:@-")
    token = secrets.token_urlsafe(32)
    with c.transaction():
        acc = c.execute("INSERT INTO accounts (external_id) VALUES (%s) ON CONFLICT (external_id) DO UPDATE SET "
                        "external_id = EXCLUDED.external_id RETURNING id, external_id, deleting", (user_id,)).fetchone()
        if acc["deleting"]:
            raise ValueError("This account is being deleted. Try again in a minute.")
        c.execute("INSERT INTO sessions (token_hash, account_id, expires_at) "
                  "VALUES (%s, %s, now() + make_interval(secs => %s))", (_hash(token), acc["id"], SESSION_TTL))
    return acc, token


def session(c, token: str | None) -> dict | None:
    if not token:
        return None
    return c.execute("SELECT a.id, a.external_id FROM sessions s JOIN accounts a ON a.id = s.account_id "
                     "WHERE s.token_hash = %s AND s.expires_at > now() AND NOT a.deleting",
                     (_hash(token),)).fetchone()


def logout(c, token: str | None) -> None:
    if token:
        c.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash(token),))
