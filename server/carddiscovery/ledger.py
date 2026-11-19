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
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS decisions (
                mailbox TEXT NOT NULL,
                card_key TEXT NOT NULL,
                decision TEXT NOT NULL CHECK (decision IN ('yes', 'no')),
                decided_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (mailbox, card_key)
            )"""
        )
        self.db.commit()

    # ---- the user's Yes/No answers: sticky across runs and cache refreshes
    def decisions(self, mailbox: str) -> dict[str, str]:
        rows = self.db.execute("SELECT card_key, decision FROM decisions WHERE mailbox=?", (mailbox,))
        return dict(rows)

    def decide(self, mailbox: str, card_key: str, decision: str | None) -> None:
        if decision is None:
            self.db.execute("DELETE FROM decisions WHERE mailbox=? AND card_key=?", (mailbox, card_key))
        else:
            self.db.execute(
                "INSERT OR REPLACE INTO decisions (mailbox, card_key, decision) VALUES (?, ?, ?)",
                (mailbox, card_key, decision))
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

    def forget(self, mailbox: str | None = None, keep_decisions: bool = True) -> int:
        """Drop cached evidence (e.g. for --fresh). Answers are kept unless asked."""
        where, args = ("WHERE mailbox=?", (mailbox,)) if mailbox else ("", ())
        cur = self.db.execute(f"DELETE FROM processed_messages {where}", args)
        if not keep_decisions:
            self.db.execute(f"DELETE FROM decisions {where}", args)
        self.db.commit()
        return cur.rowcount
