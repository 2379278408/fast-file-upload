from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event

import pytest

from app.config import Settings
from app.database import Database
from app.repository import new_sortable_id


def test_initialize_creates_required_tables(settings: Settings) -> None:
    db = Database(settings.database_path)
    db.initialize()
    with db.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"messages", "files", "events", "migration_imports", "audit_events"} <= names


def test_message_id_is_lexically_sortable() -> None:
    first = new_sortable_id(datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc))
    second = new_sortable_id(datetime(2026, 7, 17, 10, 1, tzinfo=timezone.utc))
    assert first < second


def test_initialize_migrates_existing_delete_and_reservation_schema(
    settings: Settings,
) -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE files (
                id TEXT PRIMARY KEY, original_name TEXT NOT NULL,
                storage_name TEXT NOT NULL UNIQUE, mime_type TEXT NOT NULL,
                extension TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL, created_at TEXT NOT NULL, purged_at TEXT
            );
            CREATE TABLE upload_reservations (
                client_request_id TEXT PRIMARY KEY, file_id TEXT NOT NULL,
                original_name TEXT NOT NULL, storage_name TEXT NOT NULL,
                mime_type TEXT NOT NULL, extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO files VALUES (
                'legacy-file', 'legacy.txt', 'legacy-file_legacy.txt',
                'text/plain', '.txt', 1, 'hash',
                '2026-07-17T00:00:00+00:00', NULL
            );
            """
        )

    database = Database(settings.database_path)
    database.initialize()
    with database.connect() as connection:
        file_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(files)")
        }
        reservation_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(upload_reservations)")
        }
        migrated = connection.execute(
            "SELECT purge_state, purge_claimed_at, purge_claim_token "
            "FROM files WHERE id = 'legacy-file'"
        ).fetchone()

    assert {"purge_state", "purge_claimed_at", "purge_claim_token"} <= file_columns
    assert {"device_id", "device_name"} <= reservation_columns
    assert tuple(migrated) == ("active", None, None)


def test_initialize_assigns_owner_token_to_existing_claim(
    settings: Settings,
) -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "CREATE TABLE files (id TEXT PRIMARY KEY, original_name TEXT NOT NULL, "
            "storage_name TEXT NOT NULL UNIQUE, mime_type TEXT NOT NULL, "
            "extension TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, "
            "created_at TEXT NOT NULL, purged_at TEXT, "
            "purge_state TEXT NOT NULL DEFAULT 'active', purge_claimed_at TEXT)"
        )
        connection.execute(
            "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "claimed-file", "claimed.txt", "claimed-file_claimed.txt", "text/plain",
                ".txt", 1, "hash", "2026-07-17T00:00:00+00:00", None,
                "claimed", "2026-07-17T00:00:01+00:00",
            ),
        )

    database = Database(settings.database_path)
    database.initialize()
    with database.connect() as connection:
        migrated = connection.execute(
            "SELECT purge_state, purge_claim_token FROM files WHERE id = 'claimed-file'"
        ).fetchone()

    assert migrated["purge_state"] == "claimed"
    assert migrated["purge_claim_token"]


def test_initialize_normalizes_existing_purged_file_state(
    settings: Settings,
) -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "CREATE TABLE files (id TEXT PRIMARY KEY, original_name TEXT NOT NULL, "
            "storage_name TEXT NOT NULL UNIQUE, mime_type TEXT NOT NULL, "
            "extension TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, "
            "created_at TEXT NOT NULL, purged_at TEXT, "
            "purge_state TEXT NOT NULL DEFAULT 'active', purge_claimed_at TEXT, "
            "purge_claim_token TEXT)"
        )
        connection.execute(
            "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "already-purged", "purged.txt", "already-purged_purged.txt",
                "text/plain", ".txt", 1, "hash", "2026-07-17T00:00:00+00:00",
                "2026-07-17T00:01:00+00:00", "claimed",
                "2026-07-17T00:00:30+00:00", "obsolete-owner",
            ),
        )

    database = Database(settings.database_path)
    database.initialize()
    with database.connect() as connection:
        migrated = connection.execute(
            "SELECT purge_state, purge_claimed_at, purge_claim_token "
            "FROM files WHERE id = 'already-purged'"
        ).fetchone()

    assert tuple(migrated) == ("purged", None, None)


def test_connections_enable_wal_and_configured_busy_timeout(settings: Settings) -> None:
    database = Database(settings.database_path, busy_timeout_ms=137)
    database.initialize()

    with database.connect() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode == "wal"
    assert busy_timeout == 137


def test_transaction_retries_real_sqlite_lock_contention(settings: Settings) -> None:
    database = Database(
        settings.database_path,
        busy_timeout_ms=10,
        lock_retry_attempts=20,
        lock_retry_delay=0.01,
    )
    database.initialize()
    blocker = sqlite3.connect(settings.database_path, timeout=0)
    blocker.execute("BEGIN IMMEDIATE")
    started = Event()

    def write_after_lock() -> None:
        started.set()
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_events (action, entity_id, detail, created_at) "
                "VALUES ('contention', NULL, 'retried', '2026-07-17T00:00:00+00:00')"
            )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(write_after_lock)
        assert started.wait(timeout=1)
        time.sleep(0.04)
        blocker.rollback()
        blocker.close()
        future.result(timeout=2)

    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'contention'"
        ).fetchone()[0] == 1


def test_transaction_lock_retry_is_bounded(settings: Settings) -> None:
    database = Database(
        settings.database_path,
        busy_timeout_ms=5,
        lock_retry_attempts=2,
        lock_retry_delay=0,
    )
    database.initialize()
    blocker = sqlite3.connect(settings.database_path, timeout=0)
    blocker.execute("BEGIN IMMEDIATE")

    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with database.transaction():
                pytest.fail("transaction body must not run while the lock is held")
    finally:
        blocker.rollback()
        blocker.close()


def test_transaction_does_not_retry_business_exceptions(settings: Settings) -> None:
    database = Database(settings.database_path, lock_retry_attempts=5)
    database.initialize()
    calls = 0

    with pytest.raises(ValueError, match="business rule"):
        with database.transaction():
            calls += 1
            raise ValueError("business rule")

    assert calls == 1
