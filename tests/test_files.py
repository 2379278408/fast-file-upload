from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import tracemalloc
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.auth import SessionData
from app.main import BatchRequest, _KeyedLockPool, create_app
from app.repository import MessageRepository, RestoreWindowExpired
from app.storage import FileStorage


def authenticated_client(
    settings: Settings, *, app=None, **kwargs: object
) -> TestClient:
    client = TestClient(app or create_app(settings), **kwargs)
    response = client.post(
        "/api/session",
        json={
            "access_token": settings.auth_token,
            "device_id": "file-tests",
            "device_name": "File tests",
        },
    )
    assert response.status_code == 200
    return client


def test_upload_creates_one_file_message_with_hash(settings: Settings) -> None:
    client = authenticated_client(settings)

    response = client.post(
        "/api/upload",
        data={"client_request_id": "upload-1"},
        files={"file": ("report.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    message = response.json()
    assert message["kind"] == "file"
    assert message["file"]["sha256"] == sha256(b"hello").hexdigest()
    assert "token=" not in message["file"]["download_url"]
    assert not list(settings.upload_dir.glob("*.uploading"))


def test_upload_keyed_lock_serializes_waiters_and_releases_entry() -> None:
    pool = _KeyedLockPool()
    active = 0
    peak_active = 0

    async def use_lock() -> None:
        nonlocal active, peak_active
        async with pool.hold("same-request"):
            active += 1
            peak_active = max(peak_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    async def scenario() -> None:
        await asyncio.gather(*(use_lock() for _ in range(20)))

    asyncio.run(scenario())
    assert peak_active == 1
    assert pool.size == 0


def test_upload_keyed_lock_allows_different_keys_in_parallel() -> None:
    pool = _KeyedLockPool()
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()

    async def use_lock(key: str, own: asyncio.Event, other: asyncio.Event) -> None:
        async with pool.hold(key):
            own.set()
            await asyncio.wait_for(other.wait(), timeout=1)

    async def scenario() -> None:
        await asyncio.gather(
            use_lock("first", first_entered, second_entered),
            use_lock("second", second_entered, first_entered),
        )

    asyncio.run(scenario())
    assert pool.size == 0


def test_upload_keyed_lock_releases_after_exception() -> None:
    pool = _KeyedLockPool()

    async def scenario() -> None:
        with pytest.raises(ValueError, match="forced"):
            async with pool.hold("failure"):
                raise ValueError("forced")

        assert pool.size == 0
        async with pool.hold("failure"):
            assert pool.size == 1

    asyncio.run(scenario())
    assert pool.size == 0


def test_upload_keyed_lock_run_releases_operation_exception_and_allows_reuse() -> None:
    pool = _KeyedLockPool()

    def fail_operation() -> None:
        raise ValueError("operation failed")

    async def scenario() -> None:
        with pytest.raises(ValueError, match="operation failed"):
            await pool.run("operation-error", fail_operation)
        assert pool.size == 0
        assert await pool.run("operation-error", lambda: "reused") == "reused"

    asyncio.run(scenario())
    assert pool.size == 0


def test_upload_keyed_lock_rebinds_after_entries_clear() -> None:
    pool = _KeyedLockPool()

    async def use_pool(result: str) -> str:
        return await pool.run("rebind", lambda: result)

    assert asyncio.run(use_pool("first-loop")) == "first-loop"
    assert pool.size == 0
    assert asyncio.run(use_pool("second-loop")) == "second-loop"
    assert pool.size == 0


async def _wait_for_async_lock_users(
    pool: _KeyedLockPool, key: str, expected: int
) -> None:
    deadline = asyncio.get_running_loop().time() + 1
    while pool.users_for(key) != expected:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"lock {key} did not reach {expected} users")
        await asyncio.sleep(0)


def test_upload_keyed_lock_releases_cancelled_waiter() -> None:
    pool = _KeyedLockPool()

    async def scenario() -> None:
        release = asyncio.Event()

        async def holder() -> None:
            async with pool.hold("cancelled"):
                await release.wait()

        async def waiter() -> None:
            async with pool.hold("cancelled"):
                pytest.fail("cancelled waiter must not enter")

        holder_task = asyncio.create_task(holder())
        await _wait_for_async_lock_users(pool, "cancelled", 1)
        waiter_task = asyncio.create_task(waiter())
        await _wait_for_async_lock_users(pool, "cancelled", 2)
        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task
        assert pool.users_for("cancelled") == 1
        release.set()
        await holder_task

    asyncio.run(scenario())
    assert pool.size == 0


def test_upload_keyed_lock_keeps_cancelled_operation_locked_until_worker_finishes() -> None:
    pool = _KeyedLockPool()
    entered = Event()
    release = Event()

    def blocking_operation() -> None:
        entered.set()
        assert release.wait(timeout=2)

    async def scenario() -> None:
        task = asyncio.create_task(pool.run("cancelled-operation", blocking_operation))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        assert pool.users_for("cancelled-operation") == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert pool.size == 0


def test_upload_keyed_lock_releases_high_cardinality_entries() -> None:
    pool = _KeyedLockPool(max_entries=32)

    async def scenario() -> None:
        for start in range(0, 500, 32):
            await asyncio.gather(
                *(
                    pool.run(str(index), lambda: None)
                    for index in range(start, min(start + 32, 500))
                )
            )

    asyncio.run(scenario())
    assert pool.size == 0


def test_upload_keyed_lock_rejects_new_key_at_capacity() -> None:
    pool = _KeyedLockPool(max_entries=1)

    async def scenario() -> None:
        release = asyncio.Event()

        async def hold_first() -> None:
            async with pool.hold("first"):
                await release.wait()

        holder = asyncio.create_task(hold_first())
        await _wait_for_async_lock_users(pool, "first", 1)
        with pytest.raises(RuntimeError, match="capacity"):
            async with pool.hold("second"):
                pytest.fail("second key must not enter")
        release.set()
        await holder

    asyncio.run(scenario())
    assert pool.size == 0


def test_upload_keyed_lock_wait_does_not_starve_default_executor() -> None:
    pool = _KeyedLockPool()
    entered = Event()
    release = Event()

    def blocking_operation() -> None:
        entered.set()
        assert release.wait(timeout=2)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=2)
        loop.set_default_executor(executor)
        first = asyncio.create_task(pool.run("same", blocking_operation))
        assert await asyncio.to_thread(entered.wait, 1)
        waiters = [
            asyncio.create_task(pool.run("same", lambda: None)) for _ in range(20)
        ]
        try:
            health_result = await asyncio.wait_for(
                asyncio.to_thread(lambda: "health-ready"), timeout=0.2
            )
            assert health_result == "health-ready"
        finally:
            release.set()
            await asyncio.gather(first, *waiters)
            executor.shutdown(wait=True)

    asyncio.run(scenario())
    assert pool.size == 0


def test_upload_keyed_lock_rejects_concurrent_cross_event_loop_use() -> None:
    pool = _KeyedLockPool()
    entered = Event()
    release = Event()

    async def hold_pool() -> None:
        async with pool.hold("loop-bound"):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)

    async def use_other_loop() -> None:
        async with pool.hold("other-loop"):
            pytest.fail("another loop must not enter an active pool")

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(asyncio.run, hold_pool())
        assert entered.wait(timeout=1)
        with pytest.raises(RuntimeError, match="single event loop"):
            asyncio.run(use_other_loop())
        release.set()
        holder.result(timeout=2)
    assert pool.size == 0


def test_upload_requires_client_request_id_and_always_returns_message(
    settings: Settings,
) -> None:
    client = authenticated_client(settings)

    missing = client.post(
        "/api/upload", files={"file": ("missing.txt", b"hello", "text/plain")}
    )

    assert missing.status_code == 422
    assert list(settings.upload_dir.iterdir()) == []

    uploaded = client.post(
        "/api/upload",
        data={"client_request_id": "complete-response"},
        files={"file": ("complete.txt", b"hello", "text/plain")},
    )

    assert uploaded.json()["kind"] == "file"
    assert uploaded.json()["file"]["name"] == "complete.txt"


def test_upload_writes_file_message_and_finalization_events(settings: Settings) -> None:
    client = authenticated_client(settings)

    message = client.post(
        "/api/upload",
        data={"client_request_id": "records-1"},
        files={"file": ("notes.MD", b"content", "text/markdown")},
    ).json()

    assert message["body"] is None
    assert message["file"]["original_name"] == "notes.MD"
    assert message["file"]["extension"] == ".md"
    assert message["file"]["size_bytes"] == 7
    with Database(settings.database_path).connect() as connection:
        file_row = connection.execute("SELECT * FROM files").fetchone()
        event_types = {
            row[0] for row in connection.execute("SELECT event_type FROM events")
        }
    assert file_row["storage_name"] == next(settings.upload_dir.glob("*_notes.MD")).name
    assert event_types == {"message.created", "file.finalized"}


def test_retry_returns_existing_message_and_discards_new_file(settings: Settings) -> None:
    client = authenticated_client(settings)
    request = {
        "data": {"client_request_id": "retry-1"},
        "files": {"file": ("retry.txt", b"first", "text/plain")},
    }

    first = client.post("/api/upload", **request)
    second = client.post(
        "/api/upload",
        data={"client_request_id": "retry-1"},
        files={"file": ("changed.txt", b"second", "text/plain")},
    )

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["file"]["sha256"] == sha256(b"first").hexdigest()
    assert len(list(settings.upload_dir.glob("*_retry.txt"))) == 1
    assert not list(settings.upload_dir.glob("*_changed.txt"))
    with Database(settings.database_path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_concurrent_retry_creates_one_indexed_file(settings: Settings) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)

    def upload(content: bytes) -> dict[str, object]:
        response = client.post(
            "/api/upload",
            data={"client_request_id": "concurrent-upload"},
            files={"file": ("parallel.txt", content, "text/plain")},
        )
        assert response.status_code == 200
        return response.json()

    with client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            messages = list(executor.map(upload, (b"first", b"second")))

    assert messages[0]["id"] == messages[1]["id"]
    with Database(settings.database_path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0] == 0
    uploaded = [
        path
        for path in settings.upload_dir.iterdir()
        if path.is_file() and path.name != ".audit.jsonl"
    ]
    assert len(uploaded) == 1


def test_finalize_failure_leaves_indexed_reservation_recoverable_after_restart(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    original_create = app.state.messages.create_file_message
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("forced database failure")
        return original_create(*args, **kwargs)

    monkeypatch.setattr(app.state.messages, "create_file_message", fail_once)
    client = authenticated_client(
        settings, app=app, raise_server_exceptions=False
    )

    response = client.post(
        "/api/upload",
        data={"client_request_id": "failure-1"},
        files={"file": ("failed.txt", b"partial", "text/plain")},
    )

    assert response.status_code == 500
    assert not list(settings.upload_dir.glob("*.uploading"))
    published = next(settings.upload_dir.glob("*_failed.txt"))
    with Database(settings.database_path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0] == 1
        compensation = connection.execute(
            "SELECT action, entity_id FROM audit_events"
        ).fetchall()
    assert [(row[0], row[1]) for row in compensation] == [
        ("upload.discarded", published.name.split("_", 1)[0])
    ]

    recovered_client = authenticated_client(settings)
    recovered = recovered_client.post(
        "/api/upload",
        data={"client_request_id": "failure-1"},
        files={"file": ("ignored.txt", b"ignored", "text/plain")},
    )

    assert recovered.status_code == 200
    assert recovered.json()["file"]["name"] == "failed.txt"
    with Database(settings.database_path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0] == 0
    assert not list(settings.upload_dir.glob("*_ignored.txt"))


def test_stale_reservation_without_file_is_replaced_by_new_upload(
    settings: Settings,
) -> None:
    Database(settings.database_path).initialize()
    with Database(settings.database_path).connect() as connection:
        connection.execute(
            "INSERT INTO upload_reservations "
            "(client_request_id, file_id, original_name, storage_name, mime_type, "
            "extension, size_bytes, sha256, created_at) "
            "VALUES ('stale-1', 'deadbeefdeadbeef', 'lost.txt', "
            "'deadbeefdeadbeef_lost.txt', 'text/plain', '.txt', 4, 'abc', 'now')"
        )
    client = authenticated_client(settings)

    response = client.post(
        "/api/upload",
        data={"client_request_id": "stale-1"},
        files={"file": ("fresh.txt", b"fresh", "text/plain")},
    )

    assert response.status_code == 200
    assert response.json()["file"]["name"] == "fresh.txt"
    with Database(settings.database_path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0] == 0


def _seed_upload_reservation(settings: Settings, *, state: str) -> tuple[str, Path]:
    database = Database(settings.database_path)
    database.initialize()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    file_id = f"{state:0<32}"[:32]
    storage_name = f"{file_id}_{state}.txt"
    final_path = settings.upload_dir / storage_name
    temporary_path = settings.upload_dir / f".{file_id}.uploading"
    if state == "published":
        final_path.write_bytes(b"recovered")
    elif state == "staged":
        temporary_path.write_bytes(b"recovered")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO upload_reservations "
            "(client_request_id, file_id, original_name, storage_name, mime_type, "
            "extension, size_bytes, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"startup-{state}",
                file_id,
                f"{state}.txt",
                storage_name,
                "text/plain",
                ".txt",
                len(b"recovered"),
                sha256(b"recovered").hexdigest(),
                "2026-07-17T00:00:00+00:00",
            ),
        )
    return file_id, final_path


@pytest.mark.parametrize("state", ["published", "staged"])
def test_startup_recovers_reserved_file_without_client_retry(
    settings: Settings, state: str
) -> None:
    file_id, final_path = _seed_upload_reservation(settings, state=state)

    with authenticated_client(settings) as client:
        items = client.get("/api/messages").json()["items"]

    assert len(items) == 1
    assert items[0]["file_id"] == file_id
    assert items[0]["client_request_id"] == f"startup-{state}"
    assert final_path.read_bytes() == b"recovered"
    with Database(settings.database_path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM migration_imports").fetchone()[0] == 0


def test_startup_audits_and_clears_reservation_with_missing_files(settings: Settings) -> None:
    file_id, _ = _seed_upload_reservation(settings, state="missing")

    with authenticated_client(settings):
        pass

    with Database(settings.database_path).connect() as connection:
        reservation_count = connection.execute(
            "SELECT COUNT(*) FROM upload_reservations"
        ).fetchone()[0]
        audit = connection.execute(
            "SELECT action, entity_id FROM audit_events "
            "WHERE action = 'upload.recovery_missing'"
        ).fetchone()
    assert reservation_count == 0
    assert tuple(audit) == ("upload.recovery_missing", file_id)


def test_startup_preserves_reserved_staged_file_when_publish_recovery_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id, _ = _seed_upload_reservation(settings, state="staged")
    temporary_path = settings.upload_dir / f".{file_id}.uploading"
    app = create_app(settings)

    def fail_publish(pending) -> None:
        raise OSError("forced publish failure")

    monkeypatch.setattr(app.state.storage, "publish", fail_publish)
    with authenticated_client(settings, app=app):
        pass

    assert temporary_path.read_bytes() == b"recovered"
    with Database(settings.database_path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0] == 1
        audit = connection.execute(
            "SELECT action, entity_id FROM audit_events "
            "WHERE action = 'upload.recovery_failed'"
        ).fetchone()
    assert tuple(audit) == ("upload.recovery_failed", file_id)


def test_periodic_purge_uses_injected_clock_and_stops_cleanly(
    settings: Settings, clock
) -> None:
    worker_settings = replace(settings, maintenance_interval_seconds=0.01)
    app = create_app(worker_settings)
    app.state.clock = clock

    with authenticated_client(worker_settings, app=app) as client:
        message = _upload_clocked_file(client, "worker.txt", b"worker", "worker-purge")
        client.delete(f"/api/messages/{message['id']}")
        clock.advance(seconds=30)
        deadline = time.monotonic() + 0.5
        purged_at = None
        while time.monotonic() < deadline and purged_at is None:
            with Database(settings.database_path).connect() as connection:
                purged_at = connection.execute(
                    "SELECT purged_at FROM files WHERE id = ?", (message["file_id"],)
                ).fetchone()[0]
            if purged_at is None:
                time.sleep(0.01)
        assert purged_at is not None
        maintenance_task = app.state.maintenance_task

    assert maintenance_task.done()


def test_periodic_purge_failure_isolated_and_worker_still_shuts_down(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_settings = replace(settings, maintenance_interval_seconds=0.01)
    app = create_app(worker_settings)
    original_purge = app.state.messages.purge_expired_files
    failed_cycle = Event()
    calls = 0

    def fail_periodic_cycle(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls > 1:
            failed_cycle.set()
            raise sqlite3.OperationalError("forced worker failure")
        return original_purge(*args, **kwargs)

    monkeypatch.setattr(app.state.messages, "purge_expired_files", fail_periodic_cycle)
    with authenticated_client(worker_settings, app=app):
        assert failed_cycle.wait(timeout=0.5)
        maintenance_task = app.state.maintenance_task

    assert maintenance_task.done()


def test_periodic_worker_forward_recovers_stale_claim_with_missing_file(
    settings: Settings, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_settings = replace(
        settings,
        maintenance_interval_seconds=0.01,
        purge_claim_lease_seconds=0.02,
    )
    app = create_app(worker_settings)
    app.state.clock = clock
    monkeypatch.setattr(
        app.state.messages,
        "purge_expired_files",
        lambda *args, **kwargs: {"result": [], "event": None, "events": []},
    )

    with authenticated_client(worker_settings, app=app) as client:
        message = _upload_clocked_file(
            client, "worker-recovery.txt", b"worker", "worker-recovery"
        )
        client.delete(f"/api/messages/{message['id']}")
        clock.advance(seconds=30)
        app.state.messages._claim_expired_files(
            clock() - timedelta(seconds=worker_settings.undo_seconds),
            clock() - timedelta(seconds=worker_settings.purge_claim_lease_seconds + 0.001),
            "crashed-worker-owner",
        )
        app.state.storage.purge_file(message["file"]["storage_name"])

        deadline = time.monotonic() + 0.5
        state = None
        while time.monotonic() < deadline and state != "purged":
            with Database(settings.database_path).connect() as connection:
                state = connection.execute(
                    "SELECT purge_state FROM files WHERE id = ?",
                    (message["file_id"],),
                ).fetchone()[0]
            if state != "purged":
                time.sleep(0.01)

        assert state == "purged"
        events = app.state.messages.events_after(0)
        assert [
            event["event_type"]
            for event in events
            if event["event_type"] == "file.purged"
        ] == ["file.purged"]


@pytest.mark.parametrize(
    ("name", "content", "expected_status"),
    [
        ("blocked.exe", b"x", 400),
        ("empty.txt", b"", 400),
        ("large.txt", b"x" * 2049, 413),
    ],
)
def test_rejected_uploads_leave_no_temporary_or_final_file(
    settings: Settings, name: str, content: bytes, expected_status: int
) -> None:
    client = authenticated_client(settings)

    response = client.post(
        "/api/upload",
        data={"client_request_id": f"rejected-{name}"},
        files={"file": (name, content, "application/octet-stream")},
    )

    assert response.status_code == expected_status
    assert [path for path in settings.upload_dir.iterdir() if path.name != ".audit.jsonl"] == []


def test_image_preview_and_svg_download_rules_are_preserved(settings: Settings) -> None:
    media_settings = replace(settings, allowed_extensions={".png", ".svg"})
    client = authenticated_client(media_settings)

    image_message = client.post(
        "/api/upload",
        data={"client_request_id": "image-1"},
        files={"file": ("cover.png", b"png", "image/png")},
    ).json()
    image = image_message["file"]
    svg = client.post(
        "/api/upload",
        data={"client_request_id": "svg-1"},
        files={"file": ("icon.svg", b"<svg></svg>", "image/svg+xml")},
    ).json()["file"]

    assert image_message["kind"] == "image"
    assert image["media_kind"] == "image"
    assert image["is_previewable"] is True
    assert svg["media_kind"] == "document"
    assert svg["is_previewable"] is False
    assert client.get(svg["download_url"]).content == b"<svg></svg>"


def _write_legacy_file(upload_dir: Path, name: str, content: bytes, mtime: float) -> Path:
    upload_dir.mkdir(parents=True, exist_ok=True)
    legacy = upload_dir / name
    legacy.write_bytes(content)
    os.utime(legacy, (mtime, mtime))
    return legacy


def _migration_completed_details(settings: Settings) -> list[dict[str, int]]:
    with Database(settings.database_path).connect() as connection:
        rows = connection.execute(
            "SELECT detail FROM audit_events WHERE action = 'migration.completed' "
            "ORDER BY id"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def test_legacy_migration_is_idempotent_and_uses_mtime(settings: Settings) -> None:
    timestamp = datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp()
    _write_legacy_file(settings.upload_dir, "abcdef123456_report.txt", b"history", timestamp)

    with authenticated_client(settings) as first:
        items = first.get("/api/messages").json()["items"]
        assert len(items) == 1
        assert items[0]["created_at"].startswith("2025-01-02")

    with authenticated_client(settings) as second:
        assert len(second.get("/api/messages").json()["items"]) == 1

    with Database(settings.database_path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM migration_imports").fetchone()[0] == 1
    assert _migration_completed_details(settings) == [
        {"imported": 1, "skipped": 0, "failed": 0},
        {"imported": 0, "skipped": 1, "failed": 0},
    ]


def test_legacy_migration_imports_sha256_and_marks_device(settings: Settings) -> None:
    content = b"legacy-bytes"
    timestamp = datetime(2025, 3, 4, 5, 6, 7, tzinfo=timezone.utc).timestamp()
    legacy = _write_legacy_file(settings.upload_dir, "deadbeefcafe_notes.txt", content, timestamp)

    with authenticated_client(settings) as client:
        items = client.get("/api/messages").json()["items"]

    assert len(items) == 1
    message = items[0]
    assert message["kind"] == "file"
    assert message["client_request_id"] == f"legacy-import-{legacy.name}"
    assert message["device_id"] == "migration"
    assert message["device_name"] == "Legacy import"
    assert message["created_at"] == "2025-03-04T05:06:07+00:00"
    assert message["file"]["sha256"] == sha256(content).hexdigest()
    assert message["file"]["name"] == "notes.txt"
    assert message["file"]["size_bytes"] == len(content)
    with Database(settings.database_path).connect() as connection:
        event_types = {
            row[0] for row in connection.execute("SELECT event_type FROM events")
        }
        import_row = connection.execute("SELECT * FROM migration_imports").fetchone()
    assert event_types == {"message.created", "file.finalized"}
    assert import_row["storage_name"] == legacy.name
    assert import_row["file_id"] == "deadbeefcafe"
    assert import_row["message_id"] == message["id"]


def test_legacy_migration_failure_continues_with_next_candidate(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp()
    _write_legacy_file(settings.upload_dir, "aaaaaaaaaaaa_broken.txt", b"broken", timestamp)
    _write_legacy_file(settings.upload_dir, "bbbbbbbbbbbb_health.txt", b"healthy", timestamp)

    original_open = Path.open

    def flaky_open(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "aaaaaaaaaaaa_broken.txt":
            raise PermissionError("denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)

    with authenticated_client(settings) as client:
        items = client.get("/api/messages").json()["items"]

    assert len(items) == 1
    assert items[0]["file"]["name"] == "health.txt"
    assert items[0]["file"]["sha256"] == sha256(b"healthy").hexdigest()
    with Database(settings.database_path).connect() as connection:
        failures = connection.execute(
            "SELECT entity_id, detail FROM audit_events WHERE action = 'migration.failed'"
        ).fetchall()
        assert connection.execute("SELECT COUNT(*) FROM migration_imports").fetchone()[0] == 1
    assert [(row[0], row[1]) for row in failures] == [
        ("aaaaaaaaaaaa_broken.txt", "PermissionError")
    ]
    assert _migration_completed_details(settings) == [
        {"imported": 1, "skipped": 0, "failed": 1}
    ]


def test_legacy_migration_excludes_invalid_temporary_and_internal_names(
    settings: Settings,
) -> None:
    timestamp = datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp()
    settings = replace(
        settings, database_path=settings.upload_dir / ".transfer.sqlite3"
    )
    _write_legacy_file(settings.upload_dir, "abcdef123456_ok.txt", b"ok", timestamp)
    _write_legacy_file(settings.upload_dir, "zzzzzzzzzzzz_bad.txt", b"bad", timestamp)
    _write_legacy_file(settings.upload_dir, "short_notes.txt", b"bad", timestamp)
    _write_legacy_file(settings.upload_dir, "no-separator.txt", b"bad", timestamp)
    _write_legacy_file(
        settings.upload_dir, "abcdef123456_partial.uploading", b"partial", timestamp
    )
    _write_legacy_file(settings.upload_dir, ".audit.jsonl", b"{}\n", timestamp)

    with authenticated_client(settings) as client:
        items = client.get("/api/messages").json()["items"]

    assert len(items) == 1
    assert items[0]["file"]["name"] == "ok.txt"
    with Database(settings.database_path).connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'migration.failed'"
        ).fetchone()[0] == 0
    assert _migration_completed_details(settings) == [
        {"imported": 1, "skipped": 0, "failed": 0}
    ]
    assert (settings.upload_dir / "abcdef123456_partial.uploading").is_file()
    assert (settings.upload_dir / ".audit.jsonl").is_file()


def test_startup_migration_does_not_claim_reserved_crash_recovery_file(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    original_create = app.state.messages.create_file_message

    def fail_once(*args: object, **kwargs: object) -> dict[str, object]:
        raise sqlite3.OperationalError("forced database failure")

    monkeypatch.setattr(app.state.messages, "create_file_message", fail_once)
    crashed_client = authenticated_client(
        settings, app=app, raise_server_exceptions=False
    )
    crashed = crashed_client.post(
        "/api/upload",
        data={"client_request_id": "crash-1"},
        files={"file": ("pending.txt", b"partial", "text/plain")},
    )
    assert crashed.status_code == 500
    monkeypatch.setattr(app.state.messages, "create_file_message", original_create)

    with authenticated_client(settings) as client:
        retry = client.post(
            "/api/upload",
            data={"client_request_id": "crash-1"},
            files={"file": ("ignored.txt", b"ignored", "text/plain")},
        )

        assert retry.status_code == 200
        assert retry.json()["file"]["name"] == "pending.txt"
        items = client.get("/api/messages").json()["items"]
    assert len(items) == 1
    assert items[0]["device_id"] != "migration"
    with Database(settings.database_path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM migration_imports").fetchone()[0] == 0


def test_database_file_with_storage_pattern_name_is_excluded(
    settings: Settings,
) -> None:
    timestamp = datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp()
    settings = replace(
        settings, database_path=settings.upload_dir / "abcdef123456_data.sqlite3"
    )
    _write_legacy_file(settings.upload_dir, "bbbbbbbbbbbb_ok.txt", b"ok", timestamp)

    with authenticated_client(settings) as client:
        items = client.get("/api/messages").json()["items"]

    assert len(items) == 1
    assert items[0]["file"]["name"] == "ok.txt"


def _upload_clocked_file(
    client: TestClient, name: str, content: bytes, request_id: str
) -> dict[str, object]:
    response = client.post(
        "/api/upload",
        data={"client_request_id": request_id},
        files={"file": (name, content, "text/plain")},
    )
    assert response.status_code == 200
    return response.json()


def test_file_message_delete_restore_and_final_purge(
    clocked_client: TestClient, settings: Settings, clock
) -> None:
    message = _upload_clocked_file(clocked_client, "undo.txt", b"undo-me", "purge-1")
    file_id = str(message["file_id"])
    storage_name = str(message["file"]["storage_name"])
    physical = settings.upload_dir / storage_name

    deleted = clocked_client.delete(f"/api/messages/{message['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted_at"] is not None
    assert physical.is_file()
    assert clocked_client.get(f"/download/{file_id}").status_code == 410

    restored = clocked_client.post(f"/api/messages/{message['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None
    download = clocked_client.get(f"/download/{file_id}")
    assert download.status_code == 200
    assert download.content == b"undo-me"

    clocked_client.delete(f"/api/messages/{message['id']}")
    clock.advance(seconds=31)
    purged = clocked_client.post("/api/maintenance/purge")
    assert purged.status_code == 200
    assert purged.json() == {"purged": [file_id]}

    assert not physical.exists()
    with Database(settings.database_path).connect() as connection:
        row = connection.execute(
            "SELECT purged_at FROM files WHERE id = ?", (file_id,)
        ).fetchone()
    assert row["purged_at"] is not None
    assert clocked_client.get(f"/download/{file_id}").status_code == 410
    assert clocked_client.post("/api/maintenance/purge").json() == {"purged": []}


def test_purge_respects_thirty_second_cutoff(
    clocked_client: TestClient, settings: Settings, clock
) -> None:
    message = _upload_clocked_file(clocked_client, "window.txt", b"window", "purge-2")
    file_id = str(message["file_id"])
    physical = settings.upload_dir / str(message["file"]["storage_name"])

    clocked_client.delete(f"/api/messages/{message['id']}")
    clock.advance(seconds=29)
    assert clocked_client.post("/api/maintenance/purge").json() == {"purged": []}
    assert physical.is_file()

    clock.advance(seconds=1)
    assert clocked_client.post("/api/maintenance/purge").json() == {"purged": [file_id]}
    assert not physical.exists()


def test_purge_claim_prevents_restore_success_before_physical_delete(
    clocked_client: TestClient, settings: Settings, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = _upload_clocked_file(clocked_client, "claimed.txt", b"claimed", "claim-wins")
    clocked_client.delete(f"/api/messages/{message['id']}")
    clock.advance(seconds=29.999)
    purge_now = clock() + timedelta(milliseconds=1)
    repository = clocked_client.app.state.messages
    competing_repository = MessageRepository(Database(settings.database_path))
    storage = FileStorage(
        settings.upload_dir, settings.max_upload_size, settings.allowed_extensions
    )
    claimed = Event()
    finish_delete = Event()
    original_purge = storage.purge_file

    def blocked_purge(storage_name: str) -> None:
        claimed.set()
        assert finish_delete.wait(timeout=2)
        original_purge(storage_name)

    monkeypatch.setattr(storage, "purge_file", blocked_purge)
    with ThreadPoolExecutor(max_workers=1) as executor:
        # Repository A commits the claim before entering the blocked physical delete.
        purge = executor.submit(
            repository.purge_expired_files, storage, purge_now, settings.undo_seconds
        )
        assert claimed.wait(timeout=2)
        # Repository B uses independent connections and must observe A's live lease.
        recovery = competing_repository.recover_purge_claims(
            storage,
            purge_now,
            settings.purge_claim_lease_seconds,
        )
        assert recovery["result"] == []
        with Database(settings.database_path).connect() as connection:
            claim_row = connection.execute(
                "SELECT purge_state, purge_claim_token FROM files WHERE id = ?",
                (message["file_id"],),
            ).fetchone()
        assert claim_row["purge_state"] == "claimed"
        assert claim_row["purge_claim_token"]
        with pytest.raises(RestoreWindowExpired):
            competing_repository.restore(message["id"], clock(), settings.undo_seconds)
        finish_delete.set()
        mutation = purge.result(timeout=2)

    assert mutation["result"] == [message["file_id"]]
    terminal_events = [
        event
        for event in repository.events_after(0)
        if event["event_type"] in {"message.restored", "file.purged"}
    ]
    assert [event["event_type"] for event in terminal_events] == ["file.purged"]


def test_stale_claim_recovery_fences_late_original_owner(
    clocked_client: TestClient,
    settings: Settings,
    clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _upload_clocked_file(
        clocked_client, "stale-owner.txt", b"stale", "stale-owner"
    )
    clocked_client.delete(f"/api/messages/{message['id']}")
    clock.advance(seconds=30)
    owner = MessageRepository(Database(settings.database_path))
    recovery = MessageRepository(Database(settings.database_path))
    owner_storage = FileStorage(
        settings.upload_dir, settings.max_upload_size, settings.allowed_extensions
    )
    recovery_storage = FileStorage(
        settings.upload_dir, settings.max_upload_size, settings.allowed_extensions
    )
    owner_reached_unlink = Event()
    allow_late_owner = Event()
    original_owner_purge = owner_storage.purge_file

    def blocked_owner_purge(storage_name: str) -> None:
        owner_reached_unlink.set()
        assert allow_late_owner.wait(timeout=2)
        original_owner_purge(storage_name)

    monkeypatch.setattr(owner_storage, "purge_file", blocked_owner_purge)
    with ThreadPoolExecutor(max_workers=1) as executor:
        owner_mutation = executor.submit(
            owner.purge_expired_files,
            owner_storage,
            clock(),
            settings.undo_seconds,
        )
        assert owner_reached_unlink.wait(timeout=2)
        recovered = recovery.recover_purge_claims(
            recovery_storage,
            clock() + timedelta(seconds=settings.purge_claim_lease_seconds + 0.001),
            settings.purge_claim_lease_seconds,
        )
        allow_late_owner.set()
        late_owner_result = owner_mutation.result(timeout=2)

    assert recovered["result"] == [message["file_id"]]
    assert late_owner_result["result"] == []
    assert not (settings.upload_dir / message["file"]["storage_name"]).exists()
    with Database(settings.database_path).connect() as connection:
        final_state = connection.execute(
            "SELECT purge_state, purge_claim_token, purged_at FROM files WHERE id = ?",
            (message["file_id"],),
        ).fetchone()
    assert final_state["purge_state"] == "purged"
    assert final_state["purge_claim_token"] is None
    assert final_state["purged_at"] is not None
    assert [
        event["event_type"]
        for event in recovery.events_after(0)
        if event["event_type"] == "file.purged"
    ] == ["file.purged"]


def test_claim_owner_token_guards_release_and_finalize(
    clocked_client: TestClient, settings: Settings, clock
) -> None:
    message = _upload_clocked_file(
        clocked_client, "owner-token.txt", b"owner", "owner-token"
    )
    clocked_client.delete(f"/api/messages/{message['id']}")
    clock.advance(seconds=30)
    owner = MessageRepository(Database(settings.database_path))
    competing = MessageRepository(Database(settings.database_path))
    rows = owner._claim_expired_files(
        clock() - timedelta(seconds=settings.undo_seconds),
        clock(),
        "owner-a",
    )
    assert [row["file_id"] for row in rows] == [message["file_id"]]

    competing._release_purge_claim(message["file_id"], "owner-b")
    assert competing._finalize_purge_claim(
        message["file_id"], "owner-b", clock()
    ) is None

    with Database(settings.database_path).connect() as connection:
        guarded = connection.execute(
            "SELECT purge_state, purge_claim_token, purged_at FROM files WHERE id = ?",
            (message["file_id"],),
        ).fetchone()
    assert tuple(guarded) == ("claimed", "owner-a", None)
    owner._release_purge_claim(message["file_id"], "owner-a")


def test_two_repositories_compete_for_one_stale_claim_token(
    clocked_client: TestClient, settings: Settings, clock
) -> None:
    message = _upload_clocked_file(
        clocked_client, "takeover-race.txt", b"race", "takeover-race"
    )
    clocked_client.delete(f"/api/messages/{message['id']}")
    clock.advance(seconds=30)
    owner = MessageRepository(Database(settings.database_path))
    owner._claim_expired_files(
        clock() - timedelta(seconds=settings.undo_seconds),
        clock(),
        "stale-owner",
    )
    stale_before = clock() + timedelta(milliseconds=1)
    takeover_at = clock() + timedelta(
        seconds=settings.purge_claim_lease_seconds + 0.001
    )
    competitors = [
        MessageRepository(Database(settings.database_path)),
        MessageRepository(Database(settings.database_path)),
    ]
    start = Barrier(2)

    def compete(repository: MessageRepository) -> str | None:
        start.wait(timeout=2)
        return repository._take_over_stale_purge_claim(
            message["file_id"],
            "stale-owner",
            stale_before,
            takeover_at,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(compete, competitors))

    winners = [token for token in results if token is not None]
    assert len(winners) == 1
    with Database(settings.database_path).connect() as connection:
        claimed = connection.execute(
            "SELECT purge_state, purge_claim_token, purge_claimed_at "
            "FROM files WHERE id = ?",
            (message["file_id"],),
        ).fetchone()
    assert tuple(claimed) == ("claimed", winners[0], takeover_at.isoformat())


def test_old_owner_unlink_failure_cannot_release_takeover_claim(
    clocked_client: TestClient, settings: Settings, clock
) -> None:
    message = _upload_clocked_file(
        clocked_client, "takeover-release.txt", b"release", "takeover-release"
    )
    clocked_client.delete(f"/api/messages/{message['id']}")
    clock.advance(seconds=30)
    old_owner = MessageRepository(Database(settings.database_path))
    new_owner = MessageRepository(Database(settings.database_path))
    old_owner._claim_expired_files(
        clock() - timedelta(seconds=settings.undo_seconds),
        clock(),
        "old-owner",
    )
    takeover_at = clock() + timedelta(
        seconds=settings.purge_claim_lease_seconds + 0.001
    )
    new_token = new_owner._take_over_stale_purge_claim(
        message["file_id"],
        "old-owner",
        clock() + timedelta(milliseconds=1),
        takeover_at,
    )
    assert new_token is not None

    old_owner._release_purge_claim(
        message["file_id"], "old-owner", PermissionError("late unlink failure")
    )

    with Database(settings.database_path).connect() as connection:
        claimed = connection.execute(
            "SELECT purge_state, purge_claim_token, purge_claimed_at "
            "FROM files WHERE id = ?",
            (message["file_id"],),
        ).fetchone()
    assert tuple(claimed) == ("claimed", new_token, takeover_at.isoformat())


def test_claim_recovery_requires_age_strictly_greater_than_lease(
    clocked_client: TestClient, settings: Settings, clock
) -> None:
    message = _upload_clocked_file(
        clocked_client, "lease-boundary.txt", b"lease", "lease-boundary"
    )
    clocked_client.delete(f"/api/messages/{message['id']}")
    clock.advance(seconds=30)
    owner = MessageRepository(Database(settings.database_path))
    recovery = MessageRepository(Database(settings.database_path))
    storage = FileStorage(
        settings.upload_dir, settings.max_upload_size, settings.allowed_extensions
    )
    owner._claim_expired_files(
        clock() - timedelta(seconds=settings.undo_seconds),
        clock(),
        "lease-owner",
    )

    at_lease = recovery.recover_purge_claims(
        storage,
        clock() + timedelta(seconds=settings.purge_claim_lease_seconds),
        settings.purge_claim_lease_seconds,
    )
    with Database(settings.database_path).connect() as connection:
        at_boundary = connection.execute(
            "SELECT purge_state, purge_claim_token FROM files WHERE id = ?",
            (message["file_id"],),
        ).fetchone()

    assert at_lease["result"] == []
    assert tuple(at_boundary) == ("claimed", "lease-owner")

    after_lease = recovery.recover_purge_claims(
        storage,
        clock() + timedelta(seconds=settings.purge_claim_lease_seconds + 0.001),
        settings.purge_claim_lease_seconds,
    )
    with Database(settings.database_path).connect() as connection:
        after_boundary = connection.execute(
            "SELECT purge_state, purge_claim_token FROM files WHERE id = ?",
            (message["file_id"],),
        ).fetchone()

    assert after_lease["result"] == [message["file_id"]]
    assert after_boundary["purge_state"] == "purged"
    assert after_boundary["purge_claim_token"] is None


def test_finalize_failure_keeps_claim_for_forward_recovery(
    clocked_client: TestClient,
    settings: Settings,
    clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _upload_clocked_file(
        clocked_client, "finalize-failure.txt", b"forward", "finalize-failure"
    )
    clocked_client.delete(f"/api/messages/{message['id']}")
    clock.advance(seconds=30)
    repository = MessageRepository(Database(settings.database_path))
    storage = FileStorage(
        settings.upload_dir, settings.max_upload_size, settings.allowed_extensions
    )
    original_finalize = repository._finalize_purge_claim

    def fail_finalize(*args: object, **kwargs: object):
        raise sqlite3.OperationalError("forced finalize failure")

    monkeypatch.setattr(repository, "_finalize_purge_claim", fail_finalize)
    mutation = repository.purge_expired_files(
        storage, clock(), settings.undo_seconds
    )

    assert mutation["result"] == []
    assert not (settings.upload_dir / message["file"]["storage_name"]).exists()
    with Database(settings.database_path).connect() as connection:
        claimed = connection.execute(
            "SELECT purge_state, purge_claim_token, purged_at FROM files WHERE id = ?",
            (message["file_id"],),
        ).fetchone()
    assert claimed["purge_state"] == "claimed"
    assert claimed["purge_claim_token"]
    assert claimed["purged_at"] is None

    monkeypatch.setattr(repository, "_finalize_purge_claim", original_finalize)
    recovery_now = clock() + timedelta(
        seconds=settings.purge_claim_lease_seconds + 0.001
    )
    recovered = MessageRepository(Database(settings.database_path)).recover_purge_claims(
        storage,
        recovery_now,
        settings.purge_claim_lease_seconds,
    )

    assert recovered["result"] == [message["file_id"]]
    with Database(settings.database_path).connect() as connection:
        finalized = connection.execute(
            "SELECT purge_state, purge_claim_token, purged_at FROM files WHERE id = ?",
            (message["file_id"],),
        ).fetchone()
    assert tuple(finalized) == ("purged", None, recovery_now.isoformat())


def test_restore_wins_when_concurrent_purge_has_not_claimed(
    clocked_client: TestClient, settings: Settings, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = _upload_clocked_file(clocked_client, "restored.txt", b"restored", "restore-wins")
    clocked_client.delete(f"/api/messages/{message['id']}")
    clock.advance(seconds=29.999)
    purge_now = clock() + timedelta(milliseconds=1)
    purging_repository = MessageRepository(Database(settings.database_path))
    restoring_repository = MessageRepository(Database(settings.database_path))
    storage = FileStorage(
        settings.upload_dir, settings.max_upload_size, settings.allowed_extensions
    )
    purge_started = Event()
    allow_claim = Event()
    original_claim = purging_repository._claim_expired_files

    def blocked_claim(*args: object, **kwargs: object):
        purge_started.set()
        assert allow_claim.wait(timeout=2)
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(purging_repository, "_claim_expired_files", blocked_claim)
    with ThreadPoolExecutor(max_workers=1) as executor:
        # Repository A pauses before opening its claim transaction.
        purge = executor.submit(
            purging_repository.purge_expired_files,
            storage,
            purge_now,
            settings.undo_seconds,
        )
        assert purge_started.wait(timeout=2)
        # Repository B commits restore on a different connection before A may claim.
        restored = restoring_repository.restore(
            message["id"], clock(), settings.undo_seconds
        )
        allow_claim.set()
        mutation = purge.result(timeout=2)

    assert restored is not None
    assert restored["result"]["deleted_at"] is None
    assert mutation["result"] == []
    assert (settings.upload_dir / message["file"]["storage_name"]).is_file()
    terminal_events = [
        event
        for event in restoring_repository.events_after(0)
        if event["event_type"] in {"message.restored", "file.purged"}
    ]
    assert [event["event_type"] for event in terminal_events] == ["message.restored"]


def test_legacy_file_delete_soft_deletes_owning_message_and_can_restore(
    clocked_client: TestClient, settings: Settings
) -> None:
    message = _upload_clocked_file(clocked_client, "legacy-delete.txt", b"legacy", "legacy-delete")
    physical = settings.upload_dir / message["file"]["storage_name"]

    deleted = clocked_client.delete(f"/api/files/{message['file_id']}")
    restored = clocked_client.post(f"/api/messages/{message['id']}/restore")

    assert deleted.status_code == 200
    assert deleted.json()["id"] == message["id"]
    assert deleted.json()["deleted_at"] is not None
    assert physical.is_file()
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None
    assert clocked_client.get(f"/download/{message['file_id']}").content == b"legacy"


def test_legacy_file_delete_uses_database_message_when_physical_file_is_missing(
    clocked_client: TestClient, settings: Settings
) -> None:
    message = _upload_clocked_file(clocked_client, "missing-legacy.txt", b"missing", "legacy-missing")
    (settings.upload_dir / message["file"]["storage_name"]).unlink()

    deleted = clocked_client.delete(f"/api/files/{message['file_id']}")

    assert deleted.status_code == 200
    assert deleted.json()["id"] == message["id"]
    assert deleted.json()["deleted_at"] is not None


def test_startup_reconciles_crashed_purge_claim_and_finishes_at_deadline(
    clocked_client: TestClient, settings: Settings, clock
) -> None:
    message = _upload_clocked_file(clocked_client, "crashed-claim.txt", b"claim", "crashed-claim")
    clocked_client.delete(f"/api/messages/{message['id']}")
    with Database(settings.database_path).transaction() as connection:
        connection.execute(
            "UPDATE files SET purge_state = 'claimed', purge_claimed_at = ?, "
            "purge_claim_token = ? WHERE id = ?",
            (
                (clock() - timedelta(seconds=settings.purge_claim_lease_seconds + 1)).isoformat(),
                "crashed-owner-token",
                message["file_id"],
            ),
        )
    clock.advance(seconds=30)
    restarted_app = create_app(settings)
    restarted_app.state.clock = clock

    with authenticated_client(settings, app=restarted_app):
        pass

    with Database(settings.database_path).connect() as connection:
        file_row = connection.execute(
            "SELECT purge_state, purged_at FROM files WHERE id = ?", (message["file_id"],)
        ).fetchone()
        terminal_events = connection.execute(
            "SELECT event_type FROM events WHERE event_type IN ('message.restored', 'file.purged')"
        ).fetchall()
    assert tuple(file_row) == ("purged", clock().isoformat())
    assert [row[0] for row in terminal_events] == ["file.purged"]
    assert not (settings.upload_dir / message["file"]["storage_name"]).exists()


def test_purge_failure_records_audit_and_continues(
    clocked_client: TestClient,
    settings: Settings,
    clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _upload_clocked_file(clocked_client, "broken.txt", b"broken", "purge-3a")
    second = _upload_clocked_file(clocked_client, "healthy.txt", b"healthy", "purge-3b")
    broken_storage_name = str(first["file"]["storage_name"])

    clocked_client.delete(f"/api/messages/{first['id']}")
    clocked_client.delete(f"/api/messages/{second['id']}")
    clock.advance(seconds=31)

    original_unlink = Path.unlink

    def flaky_unlink(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == broken_storage_name:
            raise PermissionError("denied")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    purged = clocked_client.post("/api/maintenance/purge")
    assert purged.status_code == 200
    assert purged.json() == {"purged": [second["file_id"]]}

    assert (settings.upload_dir / broken_storage_name).is_file()
    assert not (settings.upload_dir / str(second["file"]["storage_name"])).exists()
    with Database(settings.database_path).connect() as connection:
        failures = connection.execute(
            "SELECT entity_id, detail FROM audit_events WHERE action = 'purge.failed'"
        ).fetchall()
        states = {
            row["id"]: (row["purge_state"], row["purge_claim_token"], row["purged_at"])
            for row in connection.execute(
                "SELECT id, purge_state, purge_claim_token, purged_at FROM files"
            )
        }
    assert [(row[0], row[1]) for row in failures] == [
        (first["file_id"], "PermissionError")
    ]
    assert states[first["file_id"]] == ("active", None, None)
    assert states[second["file_id"]][0:2] == ("purged", None)
    assert states[second["file_id"]][2] is not None


def test_soft_deleted_file_is_not_purged_without_delete(
    clocked_client: TestClient, settings: Settings, clock
) -> None:
    message = _upload_clocked_file(clocked_client, "active.txt", b"active", "purge-4")
    clock.advance(seconds=3600)

    assert clocked_client.post("/api/maintenance/purge").json() == {"purged": []}
    assert (settings.upload_dir / str(message["file"]["storage_name"])).is_file()
    assert clocked_client.get(f"/download/{message['file_id']}").status_code == 200


# --- Task 7: File Library, Batch Operations, Storage Audit ---


@pytest.fixture
def protected_settings(tmp_path: Path) -> Settings:
    return Settings(
        upload_dir=tmp_path / "uploads",
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt", ".md", ".png"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
        upload_chunk_size_bytes=1024,
        event_retention_limit=100,
    )


@pytest.fixture
def pclient(protected_settings: Settings) -> TestClient:
    return TestClient(create_app(protected_settings))


def unlock(
    client: TestClient,
    device_id: str = "browser-01",
    device_name: str = "Work computer",
) -> None:
    response = client.post(
        "/api/session",
        json={
            "access_token": "secret-token",
            "device_id": device_id,
            "device_name": device_name,
        },
    )
    assert response.status_code == 200


def test_file_library_filters_by_type_device_and_date(
    pclient: TestClient, protected_settings: Settings
) -> None:
    unlock(pclient, device_id="phone", device_name="手机")
    image = pclient.post(
        "/api/upload",
        data={"client_request_id": "lib-img-1"},
        files={"file": ("photo.png", b"png", "image/png")},
    ).json()
    text = pclient.post(
        "/api/upload",
        data={"client_request_id": "lib-txt-1"},
        files={"file": ("note.txt", b"text", "text/plain")},
    ).json()

    page = pclient.get("/api/files?type=image").json()
    assert [item["id"] for item in page["items"]] == [image["id"]]

    page = pclient.get("/api/files?type=document").json()
    assert [item["id"] for item in page["items"]] == [text["id"]]

    page = pclient.get("/api/files?device_id=phone").json()
    assert len(page["items"]) == 2

    page = pclient.get("/api/files?device_id=other-device").json()
    assert page["items"] == []

    created_at = image["created_at"][:10]
    page = pclient.get(f"/api/files?from={created_at}").json()
    assert len(page["items"]) == 2

    future = "2099-01-01"
    page = pclient.get(f"/api/files?from={future}").json()
    assert page["items"] == []

    page = pclient.get(f"/api/files?to={future}").json()
    assert len(page["items"]) == 2


def test_file_library_pagination(pclient: TestClient) -> None:
    unlock(pclient)
    ids: list[str] = []
    for i in range(55):
        resp = pclient.post(
            "/api/upload",
            data={"client_request_id": f"page-{i:03d}"},
            files={"file": (f"f-{i:03d}.txt", b"x", "text/plain")},
        )
        assert resp.status_code == 200
        ids.append(resp.json()["id"])

    page1 = pclient.get("/api/files?limit=50").json()
    assert len(page1["items"]) == 50
    assert page1["next_cursor"] is not None
    assert page1["items"] == sorted(
        page1["items"], key=lambda m: (m["created_at"], m["id"])
    )

    page2 = pclient.get(
        "/api/files", params={"cursor": page1["next_cursor"], "limit": 50}
    ).json()
    assert len(page2["items"]) == 5
    assert page2["next_cursor"] is None

    all_ids = [m["id"] for m in page1["items"]] + [m["id"] for m in page2["items"]]
    assert sorted(all_ids) == sorted(ids)


def test_batch_download_zip_contents_and_name_dedup(
    pclient: TestClient, protected_settings: Settings
) -> None:
    unlock(pclient)
    m1 = pclient.post(
        "/api/upload",
        data={"client_request_id": "dl-1"},
        files={"file": ("report.txt", b"first", "text/plain")},
    ).json()
    m2 = pclient.post(
        "/api/upload",
        data={"client_request_id": "dl-2"},
        files={"file": ("report.txt", b"second", "text/plain")},
    ).json()
    m3 = pclient.post(
        "/api/upload",
        data={"client_request_id": "dl-3"},
        files={"file": ("report.txt", b"third", "text/plain")},
    ).json()

    response = pclient.post(
        "/api/files/batch-download",
        json={"message_ids": [m1["id"], m2["id"], m3["id"]]},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(response.content))
    names = zf.namelist()
    assert names == ["report.txt", "report (2).txt", "report (3).txt"]
    assert zf.read("report.txt") == b"first"
    assert zf.read("report (2).txt") == b"second"
    assert zf.read("report (3).txt") == b"third"


def test_batch_download_uses_custom_storage_directory(
    settings: Settings, tmp_path: Path
) -> None:
    custom_settings = replace(settings, upload_dir=tmp_path / "custom-artifacts")
    client = authenticated_client(custom_settings)
    message = client.post(
        "/api/upload",
        data={"client_request_id": "custom-zip"},
        files={"file": ("custom.txt", b"custom", "text/plain")},
    ).json()

    response = client.post(
        "/api/files/batch-download", json={"message_ids": [message["id"]]}
    )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("custom.txt") == b"custom"


def test_batch_download_rejects_missing_source_file(settings: Settings) -> None:
    client = authenticated_client(settings)
    message = client.post(
        "/api/upload",
        data={"client_request_id": "missing-zip-source"},
        files={"file": ("missing.txt", b"gone", "text/plain")},
    ).json()
    physical = settings.upload_dir / message["file"]["storage_name"]
    physical.unlink()

    response = client.post(
        "/api/files/batch-download", json={"message_ids": [message["id"]]}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Source file is missing: missing.txt"
    assert response.headers["content-type"].startswith("application/json")


def test_batch_download_rejects_empty_archive(settings: Settings) -> None:
    client = authenticated_client(settings)

    response = client.post(
        "/api/files/batch-download",
        json={"message_ids": ["0" * 13 + "a" * 32]},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "No downloadable files found"


def test_batch_download_enforces_total_byte_ceiling(settings: Settings) -> None:
    limited_settings = replace(settings, max_batch_download_total_bytes=5)
    client = authenticated_client(limited_settings)
    first = client.post(
        "/api/upload",
        data={"client_request_id": "zip-limit-1"},
        files={"file": ("first.txt", b"123", "text/plain")},
    ).json()
    second = client.post(
        "/api/upload",
        data={"client_request_id": "zip-limit-2"},
        files={"file": ("second.txt", b"456", "text/plain")},
    ).json()

    response = client.post(
        "/api/files/batch-download",
        json={"message_ids": [first["id"], second["id"]]},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Batch download exceeds the 5-byte limit"


def test_batch_download_build_memory_is_bounded_by_streaming_chunks(
    settings: Settings, tmp_path: Path
) -> None:
    large_settings = replace(
        settings,
        max_upload_size=5 * 1024 * 1024,
        allowed_extensions={".bin"},
    )
    app = create_app(large_settings)
    client = authenticated_client(large_settings, app=app)
    content = os.urandom(4 * 1024 * 1024)
    message = client.post(
        "/api/upload",
        data={"client_request_id": "bounded-memory-zip"},
        files={"file": ("random.bin", content, "application/octet-stream")},
    ).json()
    zip_path = tmp_path / "bounded.zip"

    tracemalloc.start()
    tracemalloc.reset_peak()
    app.state.messages.build_batch_download_zip(
        [message["id"]],
        app.state.storage,
        5 * 1024 * 1024,
        zip_path,
    )
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert zip_path.is_file()
    assert peak < 2 * 1024 * 1024


def test_batch_download_uses_disk_tempfile_and_cleans_it_after_response(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    client = authenticated_client(settings)
    message = client.post(
        "/api/upload",
        data={"client_request_id": "disk-backed-zip"},
        files={"file": ("large.txt", b"x" * 1500, "text/plain")},
    ).json()

    response = client.post(
        "/api/files/batch-download", json={"message_ids": [message["id"]]}
    )

    assert response.status_code == 200
    assert not list(tmp_path.glob("transfer-*.zip"))


def _batch_download_endpoint(app):
    return next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/files/batch-download"
    )


def _zip_session() -> SessionData:
    return SessionData(device_id="zip-test", device_name="ZIP test", expires_at=2**31)


async def _wait_for_zip_cleanup(app, path: Path) -> None:
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        if not path.exists() and app.state.zip_temp_paths.size == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"temporary ZIP was not cleaned: {path}")


def test_batch_download_cancellation_cleans_only_after_worker_finishes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    message = client.post(
        "/api/upload",
        data={"client_request_id": "cancelled-zip"},
        files={"file": ("cancel.txt", b"cancel", "text/plain")},
    ).json()
    started = Event()
    release = Event()
    original_build = app.state.messages.build_batch_download_zip

    def blocked_build(*args: object, **kwargs: object):
        started.set()
        assert release.wait(timeout=2)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(app.state.messages, "build_batch_download_zip", blocked_build)
    endpoint = _batch_download_endpoint(app)

    async def scenario() -> None:
        task = asyncio.create_task(
            endpoint(BatchRequest(message_ids=[message["id"]]), _zip_session())
        )
        assert await asyncio.to_thread(started.wait, 1)
        paths = app.state.zip_temp_paths.paths
        assert len(paths) == 1
        path = paths[0]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert path.exists()
        release.set()
        await _wait_for_zip_cleanup(app, path)

    asyncio.run(scenario())


def test_batch_download_build_exception_cleans_tempfile(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    message = client.post(
        "/api/upload",
        data={"client_request_id": "failed-zip"},
        files={"file": ("failed.txt", b"failed", "text/plain")},
    ).json()

    def fail_build(*args: object, **kwargs: object):
        raise RuntimeError("forced ZIP build failure")

    monkeypatch.setattr(app.state.messages, "build_batch_download_zip", fail_build)
    endpoint = _batch_download_endpoint(app)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="forced ZIP build failure"):
            await endpoint(BatchRequest(message_ids=[message["id"]]), _zip_session())
        assert app.state.zip_temp_paths.size == 0
        assert app.state.zip_temp_paths.paths == []

    asyncio.run(scenario())


@pytest.mark.parametrize("send_fails", [False, True])
def test_batch_download_stream_cleanup_on_completion_and_send_error(
    settings: Settings, send_fails: bool
) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    message = client.post(
        "/api/upload",
        data={"client_request_id": f"stream-zip-{send_fails}"},
        files={"file": ("stream.txt", b"stream", "text/plain")},
    ).json()
    endpoint = _batch_download_endpoint(app)

    async def scenario() -> None:
        response = await endpoint(
            BatchRequest(message_ids=[message["id"]]), _zip_session()
        )
        paths = app.state.zip_temp_paths.paths
        assert len(paths) == 1
        path = paths[0]
        sent_body = False

        async def send(event: dict[str, object]) -> None:
            nonlocal sent_body
            if event["type"] == "http.response.body" and event.get("body"):
                sent_body = True
                if send_fails:
                    raise OSError("forced client disconnect")

        async def receive() -> dict[str, str]:
            return {"type": "http.disconnect"}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/files/batch-download",
            "headers": [],
            "asgi": {"spec_version": "2.4"},
        }
        if send_fails:
            with pytest.raises(Exception):
                await response(scope, receive, send)
        else:
            await response(scope, receive, send)
        assert sent_body is True
        await _wait_for_zip_cleanup(app, path)

    asyncio.run(scenario())


def test_batch_download_cleans_tempfile_when_response_start_send_fails(
    settings: Settings,
) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    message = client.post(
        "/api/upload",
        data={"client_request_id": "start-failure-zip"},
        files={"file": ("start-failure.txt", b"start failure", "text/plain")},
    ).json()
    endpoint = _batch_download_endpoint(app)

    async def scenario() -> None:
        response = await endpoint(
            BatchRequest(message_ids=[message["id"]]), _zip_session()
        )
        paths = app.state.zip_temp_paths.paths
        assert len(paths) == 1
        path = paths[0]

        async def send(event: dict[str, object]) -> None:
            assert event["type"] == "http.response.start"
            raise OSError("forced response start failure")

        async def receive() -> dict[str, str]:
            return {"type": "http.disconnect"}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/files/batch-download",
            "headers": [],
            "asgi": {"spec_version": "2.4"},
        }
        with pytest.raises(Exception):
            await response(scope, receive, send)
        assert not path.exists()
        assert app.state.zip_temp_paths.paths == []
        assert app.state.zip_temp_paths.size == 0

    asyncio.run(scenario())


def test_batch_download_cleanup_runs_once_across_generator_response_and_repeats(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    message = client.post(
        "/api/upload",
        data={"client_request_id": "once-cleanup-zip"},
        files={"file": ("once.txt", b"once", "text/plain")},
    ).json()
    cleanup_calls = 0
    original_cleanup = app.state.zip_temp_paths.cleanup

    def counted_cleanup(path: Path) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        original_cleanup(path)

    monkeypatch.setattr(app.state.zip_temp_paths, "cleanup", counted_cleanup)
    endpoint = _batch_download_endpoint(app)

    async def scenario() -> None:
        response = await endpoint(
            BatchRequest(message_ids=[message["id"]]), _zip_session()
        )
        paths = app.state.zip_temp_paths.paths
        assert len(paths) == 1
        path = paths[0]
        iterator = response.body_iterator

        assert await anext(iterator)
        await iterator.aclose()
        assert cleanup_calls == 1

        async def send(_: dict[str, object]) -> None:
            return None

        async def receive() -> dict[str, str]:
            return {"type": "http.disconnect"}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/files/batch-download",
            "headers": [],
            "asgi": {"spec_version": "2.4"},
        }
        await response(scope, receive, send)
        await response._run_cleanup()
        await response._run_cleanup()

        assert cleanup_calls == 1
        assert not path.exists()
        assert app.state.zip_temp_paths.paths == []
        assert app.state.zip_temp_paths.size == 0

    asyncio.run(scenario())


def test_download_resolves_persisted_storage_name_without_scanning(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    message = client.post(
        "/api/upload",
        data={"client_request_id": "direct-storage-name"},
        files={"file": ("direct.txt", b"direct", "text/plain")},
    ).json()

    def reject_scan() -> list[object]:
        raise AssertionError("download must not scan the upload directory")

    monkeypatch.setattr(app.state.storage, "list_files", reject_scan)

    response = client.get(message["file"]["download_url"])

    assert response.status_code == 200
    assert response.content == b"direct"


def test_cross_operation_client_request_id_collisions_return_409(
    settings: Settings
) -> None:
    client = authenticated_client(settings)
    text = client.post(
        "/api/messages",
        json={"body": "text", "client_request_id": "text-then-file"},
    )
    assert text.status_code == 200

    file_collision = client.post(
        "/api/upload",
        data={"client_request_id": "text-then-file"},
        files={"file": ("collision.txt", b"file", "text/plain")},
    )
    assert file_collision.status_code == 409

    uploaded = client.post(
        "/api/upload",
        data={"client_request_id": "file-then-text"},
        files={"file": ("collision.txt", b"file", "text/plain")},
    )
    assert uploaded.status_code == 200

    text_collision = client.post(
        "/api/messages",
        json={"body": "text", "client_request_id": "file-then-text"},
    )
    assert text_collision.status_code == 409


def _wait_for_lock_users(pool: _KeyedLockPool, key: str, expected: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if pool.users_for(key) == expected:
            return
        time.sleep(0.005)
    raise AssertionError(f"lock {key} did not reach {expected} users")


def _assert_cross_operation_state(
    settings: Settings, *, expected_messages: int, expected_files: int
) -> None:
    with Database(settings.database_path).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == expected_messages
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == expected_files
        assert connection.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0] == 0
    physical = [
        path
        for path in settings.upload_dir.iterdir()
        if path.is_file() and path.name != ".audit.jsonl"
    ]
    assert len(physical) == expected_files
    assert not list(settings.upload_dir.glob(".*.uploading"))


def test_concurrent_cross_operation_text_wins_before_upload_staging(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    text_entered = Event()
    release_text = Event()
    stage_called = Event()
    original_create_text = app.state.messages.create_text
    original_stage = app.state.storage.stage_upload

    def blocked_text(*args: object, **kwargs: object) -> dict[str, object]:
        text_entered.set()
        assert release_text.wait(timeout=2)
        return original_create_text(*args, **kwargs)

    def tracked_stage(*args: object, **kwargs: object):
        stage_called.set()
        return original_stage(*args, **kwargs)

    monkeypatch.setattr(app.state.messages, "create_text", blocked_text)
    monkeypatch.setattr(app.state.storage, "stage_upload", tracked_stage)
    client = authenticated_client(settings, app=app)
    text_client = client
    upload_client = client

    def send_text():
        return text_client.post(
            "/api/messages",
            json={"body": "winner", "client_request_id": "text-wins"},
        )

    def send_upload():
        return upload_client.post(
            "/api/upload",
            data={"client_request_id": "text-wins"},
            files={"file": ("loser.txt", b"loser", "text/plain")},
        )

    with client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            text_future = executor.submit(send_text)
            assert text_entered.wait(timeout=1)
            upload_future = executor.submit(send_upload)
            _wait_for_lock_users(app.state.upload_locks, "text-wins", 2)
            assert not stage_called.is_set()
            release_text.set()
            text_response = text_future.result(timeout=3)
            upload_response = upload_future.result(timeout=3)

    assert text_response.status_code == 200
    assert upload_response.status_code == 409
    assert not stage_called.is_set()
    _assert_cross_operation_state(settings, expected_messages=1, expected_files=0)
    assert app.state.upload_locks.size == 0


def test_concurrent_cross_operation_upload_wins_before_text_insert(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    upload_published = Event()
    release_upload = Event()
    original_publish = app.state.storage.publish

    def blocked_publish(*args: object, **kwargs: object) -> None:
        original_publish(*args, **kwargs)
        upload_published.set()
        assert release_upload.wait(timeout=2)

    monkeypatch.setattr(app.state.storage, "publish", blocked_publish)
    client = authenticated_client(settings, app=app)
    upload_client = client
    text_client = client

    def send_upload():
        return upload_client.post(
            "/api/upload",
            data={"client_request_id": "upload-wins"},
            files={"file": ("winner.txt", b"winner", "text/plain")},
        )

    def send_text():
        return text_client.post(
            "/api/messages",
            json={"body": "loser", "client_request_id": "upload-wins"},
        )

    with client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            upload_future = executor.submit(send_upload)
            assert upload_published.wait(timeout=1)
            text_future = executor.submit(send_text)
            _wait_for_lock_users(app.state.upload_locks, "upload-wins", 2)
            release_upload.set()
            upload_response = upload_future.result(timeout=3)
            text_response = text_future.result(timeout=3)

    assert upload_response.status_code == 200
    assert text_response.status_code == 409
    _assert_cross_operation_state(settings, expected_messages=1, expected_files=1)
    assert app.state.upload_locks.size == 0


def test_image_messages_remain_file_like_for_purge(settings: Settings, clock) -> None:
    image_settings = replace(settings, allowed_extensions={".png"})
    app = create_app(image_settings)
    app.state.clock = clock
    client = authenticated_client(image_settings, app=app)
    message = client.post(
        "/api/upload",
        data={"client_request_id": "image-purge"},
        files={"file": ("photo.png", b"png", "image/png")},
    ).json()
    physical = image_settings.upload_dir / message["file"]["storage_name"]

    client.delete(f"/api/messages/{message['id']}")
    clock.advance(seconds=31)
    response = client.post("/api/maintenance/purge")

    assert response.json() == {"purged": [message["file_id"]]}
    assert not physical.exists()


def test_batch_download_skips_purged_and_deleted(
    pclient: TestClient, protected_settings: Settings, clock
) -> None:
    from app.main import create_app

    app = create_app(protected_settings)
    app.state.clock = clock
    c = TestClient(app)
    unlock(c)

    m1 = c.post(
        "/api/upload",
        data={"client_request_id": "skip-1"},
        files={"file": ("keep.txt", b"keep", "text/plain")},
    ).json()
    m2 = c.post(
        "/api/upload",
        data={"client_request_id": "skip-2"},
        files={"file": ("deleted.txt", b"gone", "text/plain")},
    ).json()
    m3 = c.post(
        "/api/upload",
        data={"client_request_id": "skip-3"},
        files={"file": ("purged.txt", b"purge", "text/plain")},
    ).json()

    c.delete(f"/api/messages/{m2['id']}")
    clock.advance(seconds=31)
    c.delete(f"/api/messages/{m3['id']}")
    c.post("/api/maintenance/purge")

    response = c.post(
        "/api/files/batch-download",
        json={"message_ids": [m1["id"], m2["id"], m3["id"]]},
    )
    assert response.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    assert zf.namelist() == ["keep.txt"]
    assert zf.read("keep.txt") == b"keep"


def test_batch_delete_soft_deletes_messages(
    pclient: TestClient, protected_settings: Settings
) -> None:
    unlock(pclient)
    m1 = pclient.post(
        "/api/messages",
        json={"body": "batch-text", "client_request_id": "bdel-1"},
    ).json()
    m2 = pclient.post(
        "/api/upload",
        data={"client_request_id": "bdel-2"},
        files={"file": ("batch.txt", b"file", "text/plain")},
    ).json()

    response = pclient.post(
        "/api/messages/batch-delete",
        json={"message_ids": [m1["id"], m2["id"]]},
    )
    assert response.status_code == 200
    assert response.json()["deleted"] == 2

    repeated = pclient.post(
        "/api/messages/batch-delete",
        json={"message_ids": [m1["id"], m2["id"]]},
    )
    assert repeated.json()["deleted"] == 0

    assert pclient.get("/api/messages").json()["items"] == []
    assert pclient.get("/api/search", params={"q": "batch"}).json()["items"] == []

    with Database(protected_settings.database_path).connect() as conn:
        row = conn.execute(
            "SELECT deleted_at FROM messages WHERE id = ?", (m1["id"],)
        ).fetchone()
    assert row["deleted_at"] is not None


def test_storage_endpoint_returns_stats_and_audit(
    pclient: TestClient, protected_settings: Settings
) -> None:
    unlock(pclient)
    pclient.post(
        "/api/upload",
        data={"client_request_id": "stor-1"},
        files={"file": ("a.txt", b"a", "text/plain")},
    )
    pclient.post(
        "/api/upload",
        data={"client_request_id": "stor-2"},
        files={"file": ("b.txt", b"bb", "text/plain")},
    )

    response = pclient.get("/api/storage")
    assert response.status_code == 200
    data = response.json()
    assert data["file_count"] == 2
    assert data["total_bytes"] == 3
    assert isinstance(data["total_size"], str)
    assert len(data["largest_files"]) == 2
    assert data["largest_files"][0]["size_bytes"] >= data["largest_files"][1]["size_bytes"]
    assert isinstance(data["audit_events"], list)


def test_batch_operations_reject_over_50_ids(pclient: TestClient) -> None:
    unlock(pclient)
    too_many = [f"id-{i}" for i in range(51)]

    response = pclient.post(
        "/api/files/batch-download",
        json={"message_ids": too_many},
    )
    assert response.status_code == 422

    response = pclient.post(
        "/api/messages/batch-delete",
        json={"message_ids": too_many},
    )
    assert response.status_code == 422
