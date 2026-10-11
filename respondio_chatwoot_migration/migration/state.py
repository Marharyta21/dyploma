"""SQLite migration state: mappings between respond.io ids and Chatwoot ids.

This is what makes the migration idempotent and resumable. Every created
object is recorded here before the next API call is made.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    respond_contact_id   TEXT PRIMARY KEY,
    chatwoot_contact_id  INTEGER,
    source_id            TEXT,
    action               TEXT,          -- created | reused
    status               TEXT NOT NULL, -- pending | done | failed
    error                TEXT,
    updated_at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    respond_conversation_id  TEXT PRIMARY KEY,
    respond_contact_id       TEXT NOT NULL,
    chatwoot_conversation_id INTEGER,
    status                   TEXT NOT NULL, -- pending | created | messages_done | resolved | failed
    error                    TEXT,
    updated_at               TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    message_key              TEXT PRIMARY KEY,  -- "<respond contact id>:<respond message id>"
    respond_message_id       TEXT NOT NULL,
    respond_conversation_id  TEXT NOT NULL,
    respond_contact_id       TEXT NOT NULL,
    chatwoot_message_id      INTEGER,
    chatwoot_conversation_id INTEGER,
    has_media                INTEGER NOT NULL DEFAULT 0,
    media_uploaded           INTEGER NOT NULL DEFAULT 0,
    status                   TEXT NOT NULL, -- done | failed
    error                    TEXT,
    updated_at               TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    message_key         TEXT PRIMARY KEY,
    respond_message_id  TEXT NOT NULL,
    url                 TEXT NOT NULL,
    kind                TEXT,
    mime                TEXT,
    local_path          TEXT,
    bytes               INTEGER,
    status              TEXT NOT NULL, -- downloaded | failed
    http_status         INTEGER,
    attempts            INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    updated_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(respond_conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversations_contact ON conversations(respond_contact_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ContactRecord:
    respond_contact_id: str
    chatwoot_contact_id: Optional[int]
    source_id: Optional[str]
    action: Optional[str]
    status: str
    error: Optional[str]


@dataclass
class ConversationRecord:
    respond_conversation_id: str
    respond_contact_id: str
    chatwoot_conversation_id: Optional[int]
    status: str
    error: Optional[str]


@dataclass
class MessageRecord:
    message_key: str
    respond_message_id: str
    respond_conversation_id: str
    respond_contact_id: str
    chatwoot_message_id: Optional[int]
    chatwoot_conversation_id: Optional[int]
    has_media: bool
    media_uploaded: bool
    status: str
    error: Optional[str]


@dataclass
class MediaRecord:
    message_key: str
    respond_message_id: str
    url: str
    kind: Optional[str]
    mime: Optional[str]
    local_path: Optional[str]
    bytes: Optional[int]
    status: str
    http_status: Optional[int]
    attempts: int
    error: Optional[str]


SCHEMA_VERSION = 2


class StateSchemaError(Exception):
    pass


class StateDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._check_schema_version()
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    def _check_schema_version(self) -> None:
        has_tables = self.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0
        if not has_tables:
            return
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise StateSchemaError(
                f"{self.path} was created by an older version of this tool (schema {version}, expected {SCHEMA_VERSION}). "
                "Rename or delete it before running again (it only contains mappings; Chatwoot is not affected)."
            )

    def close(self) -> None:
        self.conn.close()

    # ---- runs --------------------------------------------------------------
    def start_run(self, mode: str, note: str = "") -> int:
        cur = self.conn.execute("INSERT INTO runs(mode, started_at, note) VALUES (?,?,?)", (mode, _now(), note))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, note: str = "") -> None:
        self.conn.execute("UPDATE runs SET finished_at=?, note=COALESCE(NULLIF(?, ''), note) WHERE id=?", (_now(), note, run_id))
        self.conn.commit()

    # ---- contacts ----------------------------------------------------------
    def get_contact(self, respond_contact_id: str) -> Optional[ContactRecord]:
        row = self.conn.execute("SELECT * FROM contacts WHERE respond_contact_id=?", (respond_contact_id,)).fetchone()
        return ContactRecord(*[row[k] for k in ContactRecord.__dataclass_fields__]) if row else None

    def save_contact(
        self,
        respond_contact_id: str,
        chatwoot_contact_id: Optional[int],
        source_id: Optional[str],
        action: Optional[str],
        status: str,
        error: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO contacts(respond_contact_id, chatwoot_contact_id, source_id, action, status, error, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(respond_contact_id) DO UPDATE SET
                 chatwoot_contact_id=COALESCE(excluded.chatwoot_contact_id, contacts.chatwoot_contact_id),
                 source_id=COALESCE(excluded.source_id, contacts.source_id),
                 action=COALESCE(excluded.action, contacts.action),
                 status=excluded.status, error=excluded.error, updated_at=excluded.updated_at""",
            (respond_contact_id, chatwoot_contact_id, source_id, action, status, error, _now()),
        )
        self.conn.commit()

    def contacts(self) -> Iterator[ContactRecord]:
        for row in self.conn.execute("SELECT * FROM contacts ORDER BY respond_contact_id"):
            yield ContactRecord(*[row[k] for k in ContactRecord.__dataclass_fields__])

    # ---- conversations -----------------------------------------------------
    def get_conversation(self, respond_conversation_id: str) -> Optional[ConversationRecord]:
        row = self.conn.execute(
            "SELECT * FROM conversations WHERE respond_conversation_id=?", (respond_conversation_id,)
        ).fetchone()
        return ConversationRecord(*[row[k] for k in ConversationRecord.__dataclass_fields__]) if row else None

    def save_conversation(
        self,
        respond_conversation_id: str,
        respond_contact_id: str,
        chatwoot_conversation_id: Optional[int],
        status: str,
        error: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO conversations(respond_conversation_id, respond_contact_id, chatwoot_conversation_id, status, error, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(respond_conversation_id) DO UPDATE SET
                 chatwoot_conversation_id=COALESCE(excluded.chatwoot_conversation_id, conversations.chatwoot_conversation_id),
                 status=excluded.status, error=excluded.error, updated_at=excluded.updated_at""",
            (respond_conversation_id, respond_contact_id, chatwoot_conversation_id, status, error, _now()),
        )
        self.conn.commit()

    def conversations_for_contact(self, respond_contact_id: str) -> list[ConversationRecord]:
        rows = self.conn.execute("SELECT * FROM conversations WHERE respond_contact_id=?", (respond_contact_id,)).fetchall()
        return [ConversationRecord(*[r[k] for k in ConversationRecord.__dataclass_fields__]) for r in rows]

    def conversations(self) -> Iterator[ConversationRecord]:
        for row in self.conn.execute("SELECT * FROM conversations"):
            yield ConversationRecord(*[row[k] for k in ConversationRecord.__dataclass_fields__])

    # ---- messages ----------------------------------------------------------
    def get_message(self, message_key: str) -> Optional[MessageRecord]:
        row = self.conn.execute("SELECT * FROM messages WHERE message_key=?", (message_key,)).fetchone()
        return self._msg(row) if row else None

    @staticmethod
    def _msg(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            message_key=row["message_key"],
            respond_message_id=row["respond_message_id"],
            respond_conversation_id=row["respond_conversation_id"],
            respond_contact_id=row["respond_contact_id"],
            chatwoot_message_id=row["chatwoot_message_id"],
            chatwoot_conversation_id=row["chatwoot_conversation_id"],
            has_media=bool(row["has_media"]),
            media_uploaded=bool(row["media_uploaded"]),
            status=row["status"],
            error=row["error"],
        )

    def save_message(
        self,
        message_key: str,
        respond_message_id: str,
        respond_conversation_id: str,
        respond_contact_id: str,
        chatwoot_message_id: Optional[int],
        chatwoot_conversation_id: Optional[int],
        has_media: bool,
        media_uploaded: bool,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO messages(message_key, respond_message_id, respond_conversation_id, respond_contact_id, chatwoot_message_id,
                                    chatwoot_conversation_id, has_media, media_uploaded, status, error, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(message_key) DO UPDATE SET
                 chatwoot_message_id=COALESCE(excluded.chatwoot_message_id, messages.chatwoot_message_id),
                 chatwoot_conversation_id=COALESCE(excluded.chatwoot_conversation_id, messages.chatwoot_conversation_id),
                 has_media=excluded.has_media, media_uploaded=excluded.media_uploaded,
                 status=excluded.status, error=excluded.error, updated_at=excluded.updated_at""",
            (
                message_key,
                respond_message_id,
                respond_conversation_id,
                respond_contact_id,
                chatwoot_message_id,
                chatwoot_conversation_id,
                int(has_media),
                int(media_uploaded),
                status,
                error,
                _now(),
            ),
        )
        self.conn.commit()

    def messages_for_conversation(self, respond_conversation_id: str) -> dict[str, MessageRecord]:
        rows = self.conn.execute("SELECT * FROM messages WHERE respond_conversation_id=?", (respond_conversation_id,)).fetchall()
        return {r["message_key"]: self._msg(r) for r in rows}

    def failed_messages(self) -> list[MessageRecord]:
        rows = self.conn.execute("SELECT * FROM messages WHERE status='failed' ORDER BY respond_contact_id").fetchall()
        return [self._msg(r) for r in rows]

    def message_counts(self) -> dict[str, int]:
        out = {}
        for row in self.conn.execute("SELECT status, COUNT(*) AS n FROM messages GROUP BY status"):
            out[row["status"]] = row["n"]
        return out

    # ---- media -------------------------------------------------------------
    def get_media(self, message_key: str) -> Optional[MediaRecord]:
        row = self.conn.execute("SELECT * FROM media WHERE message_key=?", (message_key,)).fetchone()
        return MediaRecord(*[row[k] for k in MediaRecord.__dataclass_fields__]) if row else None

    def save_media(
        self,
        message_key: str,
        respond_message_id: str,
        url: str,
        kind: Optional[str],
        mime: Optional[str],
        local_path: Optional[str],
        nbytes: Optional[int],
        status: str,
        http_status: Optional[int],
        attempts: int,
        error: Optional[str],
    ) -> None:
        self.conn.execute(
            """INSERT INTO media(message_key, respond_message_id, url, kind, mime, local_path, bytes, status, http_status, attempts, error, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(message_key) DO UPDATE SET
                 url=excluded.url, kind=excluded.kind, mime=excluded.mime, local_path=excluded.local_path, bytes=excluded.bytes,
                 status=excluded.status, http_status=excluded.http_status, attempts=excluded.attempts, error=excluded.error,
                 updated_at=excluded.updated_at""",
            (message_key, respond_message_id, url, kind, mime, local_path, nbytes, status, http_status, attempts, error, _now()),
        )
        self.conn.commit()

    def failed_media(self) -> list[MediaRecord]:
        rows = self.conn.execute("SELECT * FROM media WHERE status='failed' ORDER BY message_key").fetchall()
        return [MediaRecord(*[r[k] for k in MediaRecord.__dataclass_fields__]) for r in rows]

    def media_counts(self) -> dict[str, int]:
        out = {}
        for row in self.conn.execute("SELECT status, COUNT(*) AS n FROM media GROUP BY status"):
            out[row["status"]] = row["n"]
        return out
