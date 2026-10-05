"""Durable state of the review channel.

- deliveries: GitLab retries until it gets a 2xx, so one delivery can arrive
  twice; a second review would publish every comment twice.
- runs: per-MR run log behind the hourly limit, the last line of defence
  against a reply loop burning the model quota.
- drafts: proposed actions waiting for approval in Telegram. Each draft of an
  MR gets the next version, and ``replaced`` marks a draft that overwrote one
  the human may still have been reading.
- telegram offset: so a restart does not replay old approvals.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from nanobot.channels.gitlab_review.proposals import ProposedAction

DELIVERY_RETENTION = timedelta(days=14)
DRAFT_MESSAGE_RETENTION = timedelta(days=30)


class StoredDraft(NamedTuple):
    head_sha: str
    actions: tuple[ProposedAction, ...]
    version: int
    replaced: bool


class GitLabReviewStateStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_key TEXT PRIMARY KEY,
                    mr_iid INTEGER,
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_runs (
                    mr_iid INTEGER NOT NULL,
                    started_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_drafts (
                    mr_iid INTEGER PRIMARY KEY,
                    head_sha TEXT NOT NULL,
                    actions TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    replaced INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS draft_messages (
                    message_id INTEGER PRIMARY KEY,
                    mr_iid INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS draft_versions (
                    mr_iid INTEGER PRIMARY KEY,
                    last_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(review_drafts)")}
            if "version" not in columns:
                conn.execute(
                    "ALTER TABLE review_drafts ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
                )
            if "replaced" not in columns:
                conn.execute(
                    "ALTER TABLE review_drafts ADD COLUMN replaced INTEGER NOT NULL DEFAULT 0"
                )

    def claim(self, delivery_key: str, iid: int) -> bool:
        """Record a delivery; return ``False`` when it was already claimed."""
        moment = datetime.now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM webhook_deliveries WHERE received_at < ?",
                ((moment - DELIVERY_RETENTION).isoformat(),),
            )
            cursor = conn.execute(
                "INSERT OR IGNORE INTO webhook_deliveries "
                "(delivery_key, mr_iid, received_at) VALUES (?, ?, ?)",
                (delivery_key, iid, moment.isoformat()),
            )
            return cursor.rowcount > 0

    def release(self, delivery_key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM webhook_deliveries WHERE delivery_key = ?", (delivery_key,))

    def try_start_run(self, iid: int, limit_per_hour: int, now: datetime | None = None) -> bool:
        """Count a run unless the MR already used its hourly budget."""
        moment = now or datetime.now()
        since = (moment - timedelta(hours=1)).isoformat()
        with self._lock, self._connect() as conn:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM review_runs WHERE mr_iid = ? AND started_at > ?",
                (iid, since),
            ).fetchone()
            if count >= limit_per_hour:
                return False
            conn.execute(
                "INSERT INTO review_runs (mr_iid, started_at) VALUES (?, ?)",
                (iid, moment.isoformat()),
            )
            return True

    def record_draft_messages(self, iid: int, version: int, message_ids: list[int]) -> None:
        """Remember which Telegram messages show this draft, so a reply can name it."""
        moment = datetime.now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM draft_messages WHERE created_at < ?",
                ((moment - DRAFT_MESSAGE_RETENTION).isoformat(),),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO draft_messages (message_id, mr_iid, version, created_at) "
                "VALUES (?, ?, ?, ?)",
                [(mid, iid, version, moment.isoformat()) for mid in message_ids],
            )

    def draft_for_message(self, message_id: int) -> tuple[int, int] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT mr_iid, version FROM draft_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return (int(row[0]), int(row[1])) if row else None

    def next_draft_version(self, iid: int) -> int:
        """Reserve the version number of the next draft of this MR."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT last_version FROM draft_versions WHERE mr_iid = ?", (iid,)
            ).fetchone()
            version = (int(row[0]) if row else 0) + 1
            conn.execute(
                "INSERT OR REPLACE INTO draft_versions (mr_iid, last_version) VALUES (?, ?)",
                (iid, version),
            )
        return version

    def save_draft(
        self, iid: int, head_sha: str, actions: tuple[ProposedAction, ...], version: int
    ) -> None:
        payload = json.dumps([asdict(action) for action in actions], ensure_ascii=False)
        with self._lock, self._connect() as conn:
            replaced = conn.execute(
                "SELECT 1 FROM review_drafts WHERE mr_iid = ?", (iid,)
            ).fetchone() is not None
            conn.execute(
                "INSERT OR REPLACE INTO review_drafts "
                "(mr_iid, head_sha, actions, created_at, version, replaced) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (iid, head_sha, payload, datetime.now().isoformat(), version, int(replaced)),
            )

    def load_draft(self, iid: int) -> StoredDraft | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT head_sha, actions, version, replaced FROM review_drafts WHERE mr_iid = ?",
                (iid,),
            ).fetchone()
        if row is None:
            return None
        head_sha, raw, version, replaced = row
        actions = tuple(ProposedAction(**item) for item in json.loads(raw))
        return StoredDraft(str(head_sha), actions, int(version), bool(replaced))

    def delete_draft(self, iid: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM review_drafts WHERE mr_iid = ?", (iid,))

    def get_value(self, key: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_value(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
