from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import sleep


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, original_name TEXT NOT NULL,
 storage_name TEXT NOT NULL UNIQUE, mime_type TEXT NOT NULL, extension TEXT NOT NULL,
 size_bytes INTEGER NOT NULL CHECK(size_bytes > 0), sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL, purged_at TEXT,
 purge_state TEXT NOT NULL DEFAULT 'active', purge_claimed_at TEXT,
 purge_claim_token TEXT);
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, kind TEXT NOT NULL,
 body TEXT, file_id TEXT REFERENCES files(id), client_request_id TEXT NOT NULL UNIQUE,
 device_id TEXT NOT NULL, device_name TEXT NOT NULL, created_at TEXT NOT NULL,
 deleted_at TEXT);
CREATE INDEX IF NOT EXISTS messages_order ON messages(created_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 event_type TEXT NOT NULL, entity_id TEXT NOT NULL, payload TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS migration_imports (storage_name TEXT PRIMARY KEY,
 file_id TEXT NOT NULL, message_id TEXT NOT NULL, imported_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS upload_reservations (client_request_id TEXT PRIMARY KEY,
 file_id TEXT NOT NULL, original_name TEXT NOT NULL, storage_name TEXT NOT NULL,
 mime_type TEXT NOT NULL, extension TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 sha256 TEXT NOT NULL, created_at TEXT NOT NULL, device_id TEXT, device_name TEXT);
CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
 action TEXT NOT NULL, entity_id TEXT, detail TEXT NOT NULL, created_at TEXT NOT NULL);
"""


class Database:
    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_ms: int = 5000,
        lock_retry_attempts: int = 3,
        lock_retry_delay: float = 0.05,
    ) -> None:
        if busy_timeout_ms < 0 or lock_retry_attempts < 0 or lock_retry_delay < 0:
            raise ValueError("Database timeout and retry settings must be non-negative")
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self.lock_retry_attempts = lock_retry_attempts
        self.lock_retry_delay = lock_retry_delay

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            self._add_column(connection, "files", "purge_state TEXT NOT NULL DEFAULT 'active'")
            self._add_column(connection, "files", "purge_claimed_at TEXT")
            self._add_column(connection, "files", "purge_claim_token TEXT")
            connection.execute(
                "UPDATE files SET purge_state = 'purged', purge_claimed_at = NULL, "
                "purge_claim_token = NULL WHERE purged_at IS NOT NULL"
            )
            connection.execute(
                "UPDATE files SET purge_claim_token = 'migration-' || id "
                "WHERE purge_state = 'claimed' AND purge_claim_token IS NULL"
            )
            self._add_column(connection, "upload_reservations", "device_id TEXT")
            self._add_column(connection, "upload_reservations", "device_name TEXT")
            self._add_column(connection, "upload_reservations", "reservation_state TEXT NOT NULL DEFAULT 'staged'")
            self._add_column(connection, "upload_reservations", "owner_token TEXT")
            self._add_column(connection, "upload_reservations", "leased_until TEXT")

    @staticmethod
    def _add_column(
        connection: sqlite3.Connection, table: str, definition: str
    ) -> None:
        column = definition.split()[0]
        columns = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            if immediate:
                self._begin_immediate(connection)
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _begin_immediate(self, connection: sqlite3.Connection) -> None:
        for attempt in range(self.lock_retry_attempts + 1):
            try:
                connection.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as error:
                if not self._is_lock_error(error) or attempt == self.lock_retry_attempts:
                    raise
                sleep(self.lock_retry_delay)

    @staticmethod
    def _is_lock_error(error: sqlite3.OperationalError) -> bool:
        error_code = getattr(error, "sqlite_errorcode", None)
        return error_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or any(
            marker in str(error).lower() for marker in ("database is locked", "database table is locked")
        )
