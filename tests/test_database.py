from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

import pytest

from app.config import Settings
from app.database import Database
from app.repository import new_sortable_id


def _insert_upload_session(
    connection: sqlite3.Connection,
    *,
    upload_id: str,
    client_request_id: str,
    message_id: str | None = None,
    size_bytes: int = 10,
    chunk_size_bytes: int = 5,
) -> None:
    connection.execute(
        "INSERT INTO upload_sessions ("
        "id, client_request_id, source_device_id, original_name, mime_type, "
        "size_bytes, last_modified_ms, sample_sha256, chunk_size_bytes, status, "
        "message_id, created_at, updated_at, expires_at"
        ") VALUES (?, ?, 'device-1', 'file.txt', 'text/plain', ?, 1, 'sample', ?, "
        "'active', ?, '2026-07-19T00:00:00+00:00', "
        "'2026-07-19T00:00:00+00:00', '2026-07-20T00:00:00+00:00')",
        (upload_id, client_request_id, size_bytes, chunk_size_bytes, message_id),
    )


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


def test_database_initializes_resumable_upload_schema(tmp_path: Path) -> None:
    database = Database(tmp_path / "timeline.sqlite3")
    database.initialize()
    with database.connect() as connection:
        sessions = {row[1] for row in connection.execute("PRAGMA table_info(upload_sessions)")}
        parts = {row[1] for row in connection.execute("PRAGMA table_info(upload_parts)")}
        foreign_keys = connection.execute("PRAGMA foreign_key_list(upload_parts)").fetchall()
    assert {
        "id", "client_request_id", "source_device_id", "original_name", "mime_type",
        "size_bytes", "last_modified_ms", "sample_sha256", "chunk_size_bytes", "status",
        "confirmed_bytes", "file_sha256", "message_id", "error_code", "publication_state",
        "created_at", "updated_at", "expires_at",
    } <= sessions
    assert {"upload_id", "part_index", "start_byte", "end_byte", "size_bytes", "sha256", "created_at"} <= parts
    assert any(row[2] == "upload_sessions" and row[6] == "CASCADE" for row in foreign_keys)


def test_resumable_upload_schema_constraints_and_indexes(tmp_path: Path) -> None:
    database = Database(tmp_path / "timeline.sqlite3")
    database.initialize()
    with database.connect() as connection:
        session_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(upload_sessions)")
        }
        part_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(upload_parts)")
        }
        indexes = {
            row[1]: row for row in connection.execute("PRAGMA index_list(upload_sessions)")
        }
        active_index_columns = [
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(upload_sessions_active_expiry)"
            )
        ]
        session_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(upload_sessions)"
        ).fetchall()

        connection.execute(
            "INSERT INTO messages VALUES ("
            "'message-1', 'text', 'body', NULL, 'message-request-1', "
            "'device-1', 'Device', '2026-07-19T00:00:00+00:00', NULL)"
        )
        _insert_upload_session(
            connection,
            upload_id="upload-1",
            client_request_id="request-1",
            message_id="message-1",
        )
        defaults = connection.execute(
            "SELECT confirmed_bytes, publication_state FROM upload_sessions "
            "WHERE id = 'upload-1'"
        ).fetchone()

        assert session_columns["publication_state"][3] == 1
        assert session_columns["publication_state"][4] == "'none'"
        assert session_columns["confirmed_bytes"][3] == 1
        assert session_columns["confirmed_bytes"][4] == "0"
        assert all(
            session_columns[name][3] == 1
            for name in {
                "client_request_id",
                "source_device_id",
                "original_name",
                "mime_type",
                "size_bytes",
                "last_modified_ms",
                "sample_sha256",
                "chunk_size_bytes",
                "status",
                "confirmed_bytes",
                "publication_state",
                "created_at",
                "updated_at",
                "expires_at",
            }
        )
        assert all(
            part_columns[name][3] == 1
            for name in {
                "upload_id",
                "part_index",
                "start_byte",
                "end_byte",
                "size_bytes",
                "sha256",
                "created_at",
            }
        )
        assert part_columns["upload_id"][5] == 1
        assert part_columns["part_index"][5] == 2
        assert "upload_sessions_active_expiry" in indexes
        assert active_index_columns == ["status", "expires_at"]
        assert any(
            row[2] == "messages" and row[3] == "message_id" and row[4] == "id"
            for row in session_foreign_keys
        )
        assert tuple(defaults) == (0, "none")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE upload_sessions SET publication_state = NULL WHERE id = 'upload-1'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_upload_session(
                connection, upload_id="upload-2", client_request_id="request-1"
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_upload_session(
                connection,
                upload_id="upload-3",
                client_request_id="request-3",
                message_id="message-1",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_upload_session(
                connection,
                upload_id="upload-4",
                client_request_id="request-4",
                message_id="missing-message",
            )


@pytest.mark.parametrize(
    ("size_bytes", "chunk_size_bytes"),
    [(0, 5), (-1, 5), (10, 0), (10, -1)],
)
def test_upload_session_positive_size_checks(
    tmp_path: Path, size_bytes: int, chunk_size_bytes: int
) -> None:
    database = Database(tmp_path / "timeline.sqlite3")
    database.initialize()
    with database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_upload_session(
                connection,
                upload_id="invalid-upload",
                client_request_id="invalid-request",
                size_bytes=size_bytes,
                chunk_size_bytes=chunk_size_bytes,
            )


@pytest.mark.parametrize(
    ("part_index", "start_byte", "end_byte", "size_bytes"),
    [(-1, 0, 0, 1), (0, -1, 0, 1), (0, 2, 1, 1), (0, 0, 0, 0)],
)
def test_upload_part_checks(
    tmp_path: Path,
    part_index: int,
    start_byte: int,
    end_byte: int,
    size_bytes: int,
) -> None:
    database = Database(tmp_path / "timeline.sqlite3")
    database.initialize()
    with database.connect() as connection:
        _insert_upload_session(
            connection, upload_id="upload-1", client_request_id="request-1"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO upload_parts VALUES (?, ?, ?, ?, ?, 'hash', 'created')",
                ("upload-1", part_index, start_byte, end_byte, size_bytes),
            )


def test_upload_parts_composite_primary_key_and_delete_cascade(tmp_path: Path) -> None:
    database = Database(tmp_path / "timeline.sqlite3")
    database.initialize()
    with database.connect() as connection:
        _insert_upload_session(
            connection, upload_id="upload-1", client_request_id="request-1"
        )
        connection.execute(
            "INSERT INTO upload_parts VALUES "
            "('upload-1', 0, 0, 4, 5, 'hash-1', 'created')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO upload_parts VALUES "
                "('upload-1', 0, 5, 9, 5, 'hash-2', 'created')"
            )
        connection.execute("DELETE FROM upload_sessions WHERE id = 'upload-1'")
        remaining_parts = connection.execute(
            "SELECT COUNT(*) FROM upload_parts WHERE upload_id = 'upload-1'"
        ).fetchone()[0]

    assert remaining_parts == 0


def test_initialize_additively_migrates_legacy_upload_sessions(tmp_path: Path) -> None:
    database_path = tmp_path / "timeline.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE upload_sessions ("
            "id TEXT PRIMARY KEY, client_request_id TEXT NOT NULL UNIQUE, "
            "source_device_id TEXT NOT NULL, original_name TEXT NOT NULL, "
            "mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK(size_bytes > 0), "
            "last_modified_ms INTEGER NOT NULL, sample_sha256 TEXT NOT NULL, "
            "chunk_size_bytes INTEGER NOT NULL CHECK(chunk_size_bytes > 0), "
            "status TEXT NOT NULL, confirmed_bytes INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO upload_sessions VALUES ("
            "'legacy-upload', 'legacy-request', 'legacy-device', 'legacy.txt', "
            "'text/plain', 10, 1, 'sample', 5, 'active', 5, "
            "'created', 'updated', 'expires')"
        )

    database = Database(database_path)
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(upload_sessions)")
        }
        migrated = connection.execute(
            "SELECT client_request_id, confirmed_bytes, publication_state, "
            "file_sha256, message_id, error_code FROM upload_sessions "
            "WHERE id = 'legacy-upload'"
        ).fetchone()
        message_id_indexes = [
            row
            for row in connection.execute("PRAGMA index_list(upload_sessions)")
            if row[1] == "upload_sessions_message_id"
        ]

    assert {"publication_state", "file_sha256", "message_id", "error_code"} <= columns
    assert tuple(migrated) == ("legacy-request", 5, "none", None, None, None)
    assert len(message_id_indexes) == 1
    assert message_id_indexes[0][2] == 1


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
