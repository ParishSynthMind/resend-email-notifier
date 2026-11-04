"""SQLite suppression list and webhook event log.

Addresses that hard-bounced or complained go on the suppression list, and
the sender checks it before every send. Sending again to those addresses
hurts the sending domain's reputation and can get an account paused.

The event log is keyed on the webhook's svix-id, so a webhook that Resend
retries is recorded once.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppressions (
    email TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    source_event_id TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_events (
    svix_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    email_id TEXT,
    recipients TEXT,
    event_created_at TEXT,
    received_at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_email_id ON webhook_events(email_id);
"""


def normalise(address: str) -> str:
    """Lower-case and strip; also unwraps 'Name <user@example.com>'."""
    address = address.strip()
    if "<" in address and address.endswith(">"):
        address = address[address.rindex("<") + 1:-1]
    return address.strip().lower()


class SuppressionStore:
    def __init__(self, db_path: Union[str, Path] = "suppressions.db"):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---- suppressions ----
    def is_suppressed(self, address: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM suppressions WHERE email = ?",
                                     (normalise(address),)).fetchone()
        return row is not None

    def add(self, address: str, reason: str, source_event_id: Optional[str] = None) -> bool:
        """Returns True if the address was newly added."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO suppressions(email, reason, source_event_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (normalise(address), reason, source_event_id, time.time()))
        return cur.rowcount == 1

    def remove(self, address: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM suppressions WHERE email = ?", (normalise(address),))
        return cur.rowcount == 1

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT email, reason, source_event_id, created_at FROM suppressions ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- webhook events ----
    def record_event(self, svix_id: str, event: Dict[str, Any]) -> bool:
        """Store a verified webhook event. Returns False if this svix-id was seen before."""
        data = event.get("data") or {}
        to = data.get("to") or []
        if isinstance(to, str):
            to = [to]
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO webhook_events"
                "(svix_id, type, email_id, recipients, event_created_at, received_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (svix_id, str(event.get("type", "")), data.get("email_id"), json.dumps(to),
                 event.get("created_at"), time.time(), json.dumps(event)))
        return cur.rowcount == 1

    def events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT svix_id, type, email_id, recipients, event_created_at, received_at "
                "FROM webhook_events ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
