"""App accounts: phone + OTP login, sessions, and the card list saved per account.

In the real product this lives on the backend. For the POC it's a local SQLite
file (~/.carddiscovery/accounts.db) with the same rules a backend would enforce:

OTP
- 6 digits from a CSPRNG; stored only as an HMAC (server secret + phone + code)
- valid for 5 minutes; 5 wrong tries and the code is burned
- resend allowed after 30 s; at most 5 codes per phone per hour
- compared in constant time
Sessions
- random 256-bit token, stored only as a SHA-256 hash, valid 7 days
Cards
- only derived card facts (bank, product, last 4, status, the user's answer)
  and their source tag ("gmail"), never email content
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

OTP_TTL = 300
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_AFTER = 30
OTP_MAX_PER_HOUR = 5
SESSION_TTL = 7 * 24 * 3600

_INDIAN_MOBILE = re.compile(r"^(?:\+?91[\s-]?|0)?([6-9]\d{9})$")


class OtpError(ValueError):
    """Shown to the user as-is, so messages must be plain and safe."""


def normalize_phone(raw: str) -> str:
    """'98765 43210', '+91-9876543210', '09876543210' -> '+919876543210'."""
    digits = re.sub(r"[\s()-]", "", raw or "")
    m = _INDIAN_MOBILE.match(digits)
    if not m:
        raise OtpError("Enter a valid 10-digit Indian mobile number.")
    return "+91" + m.group(1)


def mask_phone(phone: str) -> str:
    return phone[:3] + " ••••• " + phone[-5:]


# ---------------------------------------------------------------- delivery

class OtpSender(Protocol):
    def send(self, phone: str, code: str) -> None: ...


class ConsoleSender:
    """Development sender: prints the code in the terminal running `serve`.

    A real sender (MSG91, Twilio, Gupshup...) implements the same `send()`. In India
    it also needs DLT registration: the sender ID (header) and the exact message
    template must be registered with a telecom operator before SMS will deliver.
    """

    def __init__(self):
        self.last: dict[str, str] = {}  # for tests

    def send(self, phone: str, code: str) -> None:
        self.last[phone] = code
        print(f"\n  [DEV OTP] {mask_phone(phone)}: {code}  (valid 5 minutes)\n", flush=True)


@dataclass
class Account:
    id: int
    phone: str


class AccountStore:
    def __init__(self, path: Path, sender: OtpSender | None = None, now=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.sender = sender or ConsoleSender()
        self.now = now
        self._secret = self._load_secret(path.parent / "server_secret")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY, phone TEXT UNIQUE NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS otps (
                phone TEXT PRIMARY KEY, code_hmac TEXT NOT NULL, expires_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, sent_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS otp_sends (phone TEXT NOT NULL, sent_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS cards (
                account_id INTEGER NOT NULL, card_key TEXT NOT NULL, data TEXT NOT NULL,
                source TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (account_id, card_key));
            CREATE TABLE IF NOT EXISTS connections (
                account_id INTEGER PRIMARY KEY, mailbox TEXT NOT NULL, connected_at REAL NOT NULL);
        """)
        self.db.commit()

    @staticmethod
    def _load_secret(path: Path) -> bytes:
        if path.exists():
            return path.read_bytes()
        secret = secrets.token_bytes(32)
        path.write_bytes(secret)
        os.chmod(path, 0o600)
        return secret

    def _hmac(self, phone: str, code: str) -> str:
        return hmac.new(self._secret, f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()

    # ---- OTP -----------------------------------------------------------
    def send_otp(self, raw_phone: str) -> dict:
        phone = normalize_phone(raw_phone)
        now = self.now()
        row = self.db.execute("SELECT sent_at FROM otps WHERE phone=?", (phone,)).fetchone()
        if row and now - row[0] < OTP_RESEND_AFTER:
            raise OtpError(f"Please wait {int(OTP_RESEND_AFTER - (now - row[0])) + 1} s before asking again.")
        recent = self.db.execute("SELECT COUNT(*) FROM otp_sends WHERE phone=? AND sent_at>?",
                                 (phone, now - 3600)).fetchone()[0]
        if recent >= OTP_MAX_PER_HOUR:
            raise OtpError("Too many codes requested. Try again in an hour.")
        code = f"{secrets.randbelow(10**6):06d}"
        self.db.execute("INSERT OR REPLACE INTO otps VALUES (?, ?, ?, 0, ?)",
                        (phone, self._hmac(phone, code), now + OTP_TTL, now))
        self.db.execute("INSERT INTO otp_sends VALUES (?, ?)", (phone, now))
        self.db.commit()
        self.sender.send(phone, code)
        return {"phone": phone, "masked": mask_phone(phone), "resend_after": OTP_RESEND_AFTER,
                "expires_in": OTP_TTL}

    def verify_otp(self, raw_phone: str, code: str) -> tuple[Account, str]:
        """Returns the account (created on first login) and a new session token."""
        phone = normalize_phone(raw_phone)
        code = re.sub(r"\D", "", code or "")
        row = self.db.execute("SELECT code_hmac, expires_at, attempts FROM otps WHERE phone=?",
                              (phone,)).fetchone()
        if not row:
            raise OtpError("Request a code first.")
        code_hmac, expires_at, attempts = row
        if self.now() > expires_at:
            self._burn(phone)
            raise OtpError("This code has expired. Request a new one.")
        if attempts >= OTP_MAX_ATTEMPTS:
            self._burn(phone)
            raise OtpError("Too many wrong tries. Request a new code.")
        if len(code) != 6 or not hmac.compare_digest(code_hmac, self._hmac(phone, code)):
            self.db.execute("UPDATE otps SET attempts=attempts+1 WHERE phone=?", (phone,))
            self.db.commit()
            left = OTP_MAX_ATTEMPTS - attempts - 1
            raise OtpError(f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left."
                           if left else "Too many wrong tries. Request a new code.")
        self._burn(phone)  # one-time
        self.db.execute("INSERT OR IGNORE INTO accounts (phone, created_at) VALUES (?, ?)", (phone, self.now()))
        acc_id = self.db.execute("SELECT id FROM accounts WHERE phone=?", (phone,)).fetchone()[0]
        token = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                        (hashlib.sha256(token.encode()).hexdigest(), acc_id, self.now() + SESSION_TTL))
        self.db.commit()
        return Account(acc_id, phone), token

    def _burn(self, phone: str) -> None:
        self.db.execute("DELETE FROM otps WHERE phone=?", (phone,))
        self.db.commit()

    # ---- sessions ------------------------------------------------------
    def session(self, token: str | None) -> Account | None:
        if not token:
            return None
        row = self.db.execute(
            "SELECT a.id, a.phone, s.expires_at FROM sessions s JOIN accounts a ON a.id=s.account_id "
            "WHERE s.token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row[2] < self.now():
            return None
        return Account(row[0], row[1])

    def logout(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        self.db.commit()

    # ---- Gmail connection and saved cards ------------------------------
    def set_connection(self, account_id: int, mailbox: str | None) -> None:
        if mailbox is None:
            self.db.execute("DELETE FROM connections WHERE account_id=?", (account_id,))
        else:
            self.db.execute("INSERT OR REPLACE INTO connections VALUES (?, ?, ?)", (account_id, mailbox, self.now()))
        self.db.commit()

    def connection(self, account_id: int) -> str | None:
        row = self.db.execute("SELECT mailbox FROM connections WHERE account_id=?", (account_id,)).fetchone()
        return row[0] if row else None

    def save_cards(self, account_id: int, cards: list[dict], source: str = "gmail") -> None:
        """Replace this account's cards from one source with a fresh scan result."""
        self.db.execute("DELETE FROM cards WHERE account_id=? AND source=?", (account_id, source))
        self.db.executemany("INSERT INTO cards VALUES (?, ?, ?, ?, ?)",
                            [(account_id, c["key"], json.dumps(c), source, self.now()) for c in cards])
        self.db.commit()

    def cards(self, account_id: int) -> list[dict]:
        rows = self.db.execute("SELECT data, source FROM cards WHERE account_id=? ORDER BY card_key", (account_id,))
        return [{**json.loads(d), "source": s} for d, s in rows]

    def delete_cards(self, account_id: int, source: str | None = None) -> None:
        if source:
            self.db.execute("DELETE FROM cards WHERE account_id=? AND source=?", (account_id, source))
        else:
            self.db.execute("DELETE FROM cards WHERE account_id=?", (account_id,))
        self.db.commit()

    def delete_account(self, account_id: int) -> None:
        for table in ("cards", "connections", "sessions"):
            self.db.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))
        self.db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        self.db.commit()
