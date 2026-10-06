"""Durable state of the review channel.

- deliveries: GitLab retries until it gets a 2xx, so one delivery can arrive
  twice; a second review would publish every comment twice.
- runs: per-MR run log behind the hourly limit, the last line of defence
  against a reply loop burning the model quota.
- drafts: proposed actions waiting for approval in Telegram. Each draft of an
  MR gets the next version, and ``replaced`` marks a draft that overwrote one
  the human may still have been reading.
- telegram offset: so a restart does not replay old approvals.
- reviews: the archive. One row per review run with its Claude session, the
  task keys it concerns, and a log of everything said about it, so a later
  question can be answered from it. Each review is also exported as a Markdown
  file the agent can search.
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
MAX_EVENT_CHARS = 20000


class StoredDraft(NamedTuple):
    head_sha: str
    actions: tuple[ProposedAction, ...]
    version: int
    replaced: bool


class ReviewRecord(NamedTuple):
    id: int
    iid: int | None
    title: str
    web_url: str
    task_keys: tuple[str, ...]
    kind: str
    session_id: str
    head_sha: str
    started_at: str
    status: str
    author: str = ""


class ReviewEvent(NamedTuple):
    at: str
    who: str
    text: str


_REVIEW_COLUMNS = (
    "id, mr_iid, title, web_url, task_keys, kind, session_id, head_sha, started_at, status, author"
)


class GitLabReviewStateStore:
    def __init__(self, db_path: Path, archive_dir: Path | None = None) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_dir = archive_dir or db_path.parent / "archive"
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
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mr_iid INTEGER,
                    title TEXT NOT NULL,
                    web_url TEXT NOT NULL,
                    task_keys TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_events (
                    review_id INTEGER NOT NULL,
                    at TEXT NOT NULL,
                    who TEXT NOT NULL,
                    text TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_messages (
                    message_id INTEGER PRIMARY KEY,
                    review_id INTEGER NOT NULL
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
            review_columns = {row[1] for row in conn.execute("PRAGMA table_info(reviews)")}
            if "author" not in review_columns:
                conn.execute("ALTER TABLE reviews ADD COLUMN author TEXT NOT NULL DEFAULT ''")
            if "published" not in review_columns:
                conn.execute("ALTER TABLE reviews ADD COLUMN published INTEGER NOT NULL DEFAULT 0")

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

    def pending_drafts(self) -> list[tuple[int, int]]:
        """``(iid, version)`` of every draft still waiting for a decision, oldest first."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT mr_iid, version FROM review_drafts ORDER BY created_at"
            ).fetchall()
        return [(int(iid), int(version)) for iid, version in rows]

    def start_review(
        self,
        *,
        iid: int | None,
        title: str,
        web_url: str,
        task_keys: list[str],
        kind: str,
        session_id: str,
        head_sha: str,
        author: str = "",
    ) -> ReviewRecord:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO reviews (mr_iid, title, web_url, task_keys, kind, session_id, "
                "head_sha, started_at, status, author) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    iid, title, web_url, " ".join(task_keys), kind, session_id, head_sha,
                    datetime.now().isoformat(timespec="seconds"), "in progress", author,
                ),
            )
            review_id = int(cursor.lastrowid or 0)
        record = self.get_review(review_id)
        assert record is not None
        self._export(record)
        return record

    def get_review(self, review_id: int) -> ReviewRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT {_REVIEW_COLUMNS} FROM reviews WHERE id = ?", (review_id,)
            ).fetchone()
        return _record(row) if row else None

    def latest_review(self, iid: int) -> ReviewRecord | None:
        """The newest review of this MR (a reply in its thread counts too)."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT {_REVIEW_COLUMNS} FROM reviews WHERE mr_iid = ? AND kind != 'chat' "
                "ORDER BY id DESC LIMIT 1",
                (iid,),
            ).fetchone()
        return _record(row) if row else None

    def find_reviews(self, iids: list[int], keys: list[str], limit: int) -> list[ReviewRecord]:
        """Reviews of the given MRs or naming the given task keys, newest first."""
        if not iids and not keys:
            return []
        clauses: list[str] = []
        params: list[object] = []
        if iids:
            clauses.append(f"mr_iid IN ({', '.join('?' * len(iids))})")
            params.extend(iids)
        for key in keys:
            clauses.append("(' ' || task_keys || ' ') LIKE ?")
            params.append(f"% {key} %")
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_REVIEW_COLUMNS} FROM reviews WHERE kind != 'chat' AND "
                f"({' OR '.join(clauses)}) ORDER BY id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_record(row) for row in rows]

    def add_published(self, review_id: int, count: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE reviews SET published = published + ? WHERE id = ?", (count, review_id)
            )

    def author_stats(self, username: str) -> str:
        """How often this developer's MRs were reviewed and commented on, counted in code."""
        with self._lock, self._connect() as conn:
            reviewed, published = conn.execute(
                "SELECT COUNT(DISTINCT mr_iid), COALESCE(SUM(published), 0) FROM reviews "
                "WHERE kind != 'chat' AND author = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
        if not reviewed:
            return ""
        return f"MRs of theirs reviewed: {reviewed}, comments published: {published}."

    def latest_active_review(self, since: datetime) -> ReviewRecord | None:
        """The review or conversation with the most recent message after *since*."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT review_id FROM review_events WHERE at >= ? ORDER BY rowid DESC LIMIT 1",
                (since.isoformat(timespec="seconds"),),
            ).fetchone()
        return self.get_review(int(row[0])) if row else None

    def bind_review(
        self, review_id: int, *, iid: int, title: str, web_url: str, head_sha: str, author: str
    ) -> ReviewRecord | None:
        """Tie a conversation that started without an MR to the MR it turned out to be about."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE reviews SET mr_iid = ?, title = ?, web_url = ?, head_sha = ?, author = ?, "
                "kind = 'conversation' WHERE id = ? AND mr_iid IS NULL",
                (iid, title, web_url, head_sha, author, review_id),
            )
        self._export_id(review_id)
        return self.get_review(review_id)

    def set_review_status(self, review_id: int, status: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE reviews SET status = ? WHERE id = ?", (status, review_id))
        self._export_id(review_id)

    def add_event(self, review_id: int, who: str, text: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO review_events (review_id, at, who, text) VALUES (?, ?, ?, ?)",
                (
                    review_id, datetime.now().isoformat(timespec="seconds"), who,
                    text[:MAX_EVENT_CHARS],
                ),
            )
        self._export_id(review_id)

    def events(self, review_id: int) -> list[ReviewEvent]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT at, who, text FROM review_events WHERE review_id = ? ORDER BY rowid",
                (review_id,),
            ).fetchall()
        return [ReviewEvent(str(at), str(who), str(text)) for at, who, text in rows]

    def record_review_messages(self, review_id: int, message_ids: list[int]) -> None:
        """Remember which Telegram messages belong to this review's conversation."""
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO review_messages (message_id, review_id) VALUES (?, ?)",
                [(mid, review_id) for mid in message_ids],
            )

    def review_for_message(self, message_id: int) -> ReviewRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT review_id FROM review_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return self.get_review(int(row[0])) if row else None

    def render_review(self, record: ReviewRecord) -> str:
        """The archive entry of one review as Markdown."""
        mr = f"MR !{record.iid}" if record.iid is not None else "Conversation without an MR"
        lines = [f"# {mr} {record.title}".rstrip()]
        if record.web_url:
            lines.append(record.web_url)
        lines.append("")
        if record.task_keys:
            lines.append(f"- Tasks: {', '.join(record.task_keys)}")
        if record.author:
            lines.append(f"- MR author: {record.author}")
        lines += [
            f"- Kind: {record.kind}",
            f"- Started: {record.started_at}",
            f"- Status: {record.status}",
            f"- Commit: {record.head_sha or '—'}",
            f"- Claude session: {record.session_id}",
        ]
        for event in self.events(record.id):
            lines += ["", f"## {event.at} — {event.who}", "", event.text]
        return "\n".join(lines) + "\n"

    def _export_id(self, review_id: int) -> None:
        record = self.get_review(review_id)
        if record is not None:
            self._export(record)

    def _export(self, record: ReviewRecord) -> None:
        suffix = f"mr{record.iid}" if record.iid is not None else "chat"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        path = self.archive_dir / f"{record.id:05d}-{suffix}.md"
        path.write_text(self.render_review(record), encoding="utf-8")

    def get_value(self, key: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_value(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))


def _record(row: tuple[object, ...]) -> ReviewRecord:
    review_id, iid, title, web_url, keys, kind, session_id, head_sha, started_at, status, author = row
    return ReviewRecord(
        id=int(str(review_id)),
        iid=int(str(iid)) if iid is not None else None,
        title=str(title),
        web_url=str(web_url),
        task_keys=tuple(str(keys).split()),
        kind=str(kind),
        session_id=str(session_id),
        head_sha=str(head_sha),
        started_at=str(started_at),
        status=str(status),
        author=str(author or ""),
    )
