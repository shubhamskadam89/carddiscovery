"""Local SQLite ledger of processed messages.

Stores only derived evidence (type, bank, last4, product, dates, flags) keyed by
message id, so re-runs skip messages already seen. No subjects, snippets or bodies.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import EXTRACTOR_VERSION
from .models import Evidence


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS processed_messages (
                mailbox TEXT NOT NULL,
                message_id TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                evidence TEXT NOT NULL,
                processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (mailbox, message_id)
            )"""
        )
        self.db.commit()

    def known(self, mailbox: str) -> dict[str, Evidence]:
        rows = self.db.execute(
            "SELECT message_id, evidence FROM processed_messages WHERE mailbox=? AND extractor_version=?",
            (mailbox, EXTRACTOR_VERSION),
        )
        return {mid: Evidence.from_dict(json.loads(ev)) for mid, ev in rows}

    def save(self, mailbox: str, evidence: list[Evidence]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO processed_messages (mailbox, message_id, extractor_version, evidence) "
            "VALUES (?, ?, ?, ?)",
            [(mailbox, e.message_id, EXTRACTOR_VERSION, json.dumps(e.to_dict())) for e in evidence],
        )
        self.db.commit()

    def forget(self, mailbox: str | None = None) -> int:
        if mailbox:
            cur = self.db.execute("DELETE FROM processed_messages WHERE mailbox=?", (mailbox,))
        else:
            cur = self.db.execute("DELETE FROM processed_messages")
        self.db.commit()
        return cur.rowcount
