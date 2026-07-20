from __future__ import annotations

import asyncio
import errno
import logging
import sqlite3
import time
import threading
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import safe_fs
from app.auth import SessionData
from app.config import ConfigurationError, Settings
from app.database import Database
from app.main import create_app
from app.repository import MessageRepository


def authenticated_client(
    settings: Settings,
    *,
    app: FastAPI | None = None,
    device_id: str = "source",
) -> TestClient:
    client = TestClient(app or create_app(settings))
    response = client.post(
        "/api/session",
        json={
            "access_token": settings.auth_token,
            "device_id": device_id,
            "device_name": device_id,
        },
    )
    assert response.status_code == 200
    return client


def create_upload(
    client: TestClient,
    request_id: str = "request-1",
    content: bytes = b"data",
    chunk_size: int | None = None,
    name: str = "report.txt",
) -> dict[str, object]:
    response = client.post(
        "/api/uploads",
        json={
            "client_request_id": request_id,
            "name": name,
            "size_bytes": len(content),
            "mime_type": "text/plain",
            "last_modified_ms": 1_784_412_345_000,
            "chunk_size_bytes": chunk_size or client.app.state.settings.upload_chunk_size_bytes,
            "sample_sha256": sha256(b"sample").hexdigest(),
        },
    )
    assert response.status_code == 200
    return response.json()


def test_create_rejects_chunk_size_that_differs_from_server(settings: Settings) -> None:
    client = authenticated_client(settings)
    response = client.post(
        "/api/uploads",
        json={
            "client_request_id": "wrong-chunk-size",
            "name": "report.txt",
            "size_bytes": 8,
            "mime_type": "text/plain",
            "last_modified_ms": 1,
            "chunk_size_bytes": settings.upload_chunk_size_bytes * 2,
            "sample_sha256": sha256(b"sample").hexdigest(),
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": f"chunk_size_bytes must equal the server value {settings.upload_chunk_size_bytes}"
    }


def test_create_uses_server_chunk_size_when_request_omits_it(settings: Settings) -> None:
    client = authenticated_client(settings)
    response = client.post(
        "/api/uploads",
        json={
            "client_request_id": "server-chunk-size",
            "name": "report.txt",
            "size_bytes": 8,
            "mime_type": "text/plain",
            "last_modified_ms": 1,
            "sample_sha256": sha256(b"sample").hexdigest(),
        },
    )

    assert response.status_code == 200
    assert response.json()["chunk_size_bytes"] == settings.upload_chunk_size_bytes


def test_chunk_configuration_bounds_part_count_and_request_size(settings: Settings) -> None:
    maximum_size = 512 * 1024 * 1024
    with pytest.raises(ConfigurationError, match="10000 part limit"):
        replace(settings, max_upload_size=maximum_size, upload_chunk_size_bytes=1)
    with pytest.raises(ConfigurationError, match="64 MiB limit"):
        replace(
            settings,
            max_upload_size=maximum_size,
            upload_chunk_size_bytes=maximum_size,
        )
    configured = replace(
        settings,
        max_upload_size=maximum_size,
        upload_chunk_size_bytes=8 * 1024 * 1024,
    )
    assert configured.upload_chunk_size_bytes == 8 * 1024 * 1024


def test_chunk_configuration_can_exceed_max_upload_size(settings: Settings) -> None:
    configured = replace(
        settings,
        max_upload_size=1024,
        upload_chunk_size_bytes=2 * 1024,
    )

    assert configured.upload_chunk_size_bytes == 2 * 1024


def test_512_mib_session_reserves_upload_and_assembly_peak(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    maximum_size = 512 * 1024 * 1024
    configured = replace(
        settings,
        max_upload_size=maximum_size,
        upload_chunk_size_bytes=8 * 1024 * 1024,
        upload_storage_reserve_bytes=0,
    )
    app = create_app(configured)
    client = authenticated_client(configured, app=app)

    class Usage:
        total = 4 * maximum_size
        used = 0
        free = (2 * maximum_size) - 1

    monkeypatch.setattr("app.upload_service.shutil.disk_usage", lambda _path: Usage())
    payload = {
        "client_request_id": "peak-too-small",
        "name": "large.txt",
        "size_bytes": maximum_size,
        "mime_type": "text/plain",
        "last_modified_ms": 1,
        "chunk_size_bytes": configured.upload_chunk_size_bytes,
        "sample_sha256": sha256(b"sample").hexdigest(),
    }
    rejected = client.post("/api/uploads", json=payload)
    assert rejected.status_code == 507

    Usage.free = 2 * maximum_size
    payload["client_request_id"] = "peak-exact"
    accepted = client.post("/api/uploads", json=payload)
    assert accepted.status_code == 200


def put_part(
    client: TestClient,
    upload: dict[str, object],
    part_index: int,
    content: bytes,
) -> dict[str, object]:
    chunk_size = int(upload["chunk_size_bytes"])
    start = part_index * chunk_size
    end = start + len(content) - 1
    response = client.put(
        f"/api/uploads/{upload['upload_id']}/parts/{part_index}",
        content=content,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Range": f"bytes {start}-{end}/{upload['size_bytes']}",
            "X-Chunk-SHA256": sha256(content).hexdigest(),
        },
    )
    assert response.status_code == 200
    return response.json()


def put_single_part(
    client: TestClient, upload: dict[str, object], content: bytes
) -> dict[str, object]:
    return put_part(client, upload, 0, content)


def test_upload_events_share_strict_sequence_with_messages(settings: Settings) -> None:
    client = authenticated_client(settings, device_id="source")
    upload = create_upload(client)
    put_single_part(client, upload, b"data")
    response = client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})
    assert response.status_code == 200

    events = MessageRepository(Database(settings.database_path)).events_after(0)
    sequences = [int(event["sequence"]) for event in events]
    event_types = {str(event["event_type"]) for event in events}
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert {"upload.created", "upload.state_changed", "upload.completed"} <= event_types


def test_progress_event_does_not_extend_upload_expiry(settings: Settings) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    upload = create_upload(client)
    upload_id = str(upload["upload_id"])
    before = app.state.upload_repository.get(upload_id)

    app.state.upload_repository.persist_progress(
        upload_id,
        {
            "upload_id": upload_id,
            "status": before["status"],
            "confirmed_bytes": before["confirmed_bytes"],
            "in_flight_bytes": 1,
            "total_bytes": before["size_bytes"],
            "source_device_id": before["source_device_id"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    after = app.state.upload_repository.get(upload_id)
    assert after["updated_at"] == before["updated_at"]
    assert after["expires_at"] == before["expires_at"]


def test_part_completion_uses_only_coalesced_progress(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    upload = create_upload(client)
    forced: list[bool] = []
    original_publish = app.state.progress_publisher.publish

    async def capture_publish(upload_id, payload, force=False):
        forced.append(force)
        await original_publish(upload_id, payload, force)

    monkeypatch.setattr(app.state.progress_publisher, "publish", capture_publish)
    put_single_part(client, upload, b"data")

    assert forced
    assert not any(forced)


def test_cancel_discards_pending_and_rejects_late_progress(settings: Settings) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    upload = create_upload(client)
    upload_id = str(upload["upload_id"])

    with client:
        session = app.state.upload_repository.get(upload_id)

        def payload(value: int) -> dict[str, object]:
            return {
                "upload_id": upload_id,
                "status": session["status"],
                "confirmed_bytes": session["confirmed_bytes"],
                "in_flight_bytes": value,
                "total_bytes": session["size_bytes"],
                "source_device_id": session["source_device_id"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

        client.portal.call(app.state.progress_publisher.publish, upload_id, payload(1))
        client.portal.call(app.state.progress_publisher.publish, upload_id, payload(2))
        response = client.delete(f"/api/uploads/{upload_id}")
        assert response.status_code == 200
        client.portal.call(asyncio.sleep, 0.3)
        client.portal.call(app.state.progress_publisher.publish, upload_id, payload(3))

        events = MessageRepository(Database(settings.database_path)).events_after(0)
        cancelled_sequence = next(
            int(event["sequence"])
            for event in events
            if event["event_type"] == "upload.cancelled"
        )
        assert not any(
            event["event_type"] == "upload.progress"
            and int(event["sequence"]) > cancelled_sequence
            for event in events
        )
        assert [
            event["payload"]["in_flight_bytes"]
            for event in events
            if event["event_type"] == "upload.progress"
        ] == [1]


def test_complete_computes_server_hash_and_returns_one_permanent_message(
    settings: Settings,
) -> None:
    settings.upload_chunk_size_bytes = 4
    client = authenticated_client(settings, device_id="source")
    content = b"abcdefgh"
    upload = create_upload(
        client, request_id="complete-1", content=content, chunk_size=4
    )
    put_part(client, upload, 0, content[:4])
    put_part(client, upload, 1, content[4:])

    first = client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})
    replay = client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["file"]["sha256"] == sha256(content).hexdigest()
    assert first.json()["upload_id"] == upload["upload_id"]
    messages = client.get("/api/messages?limit=50").json()["items"]
    assert messages.count(first.json()) == 1


def test_complete_preserves_sanitized_filename_metadata(settings: Settings) -> None:
    client = authenticated_client(settings, device_id="source")
    content = b"filename"
    upload = create_upload(client, content=content, name="my  report.txt")
    put_single_part(client, upload, content)

    response = client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})

    assert response.status_code == 200
    assert response.json()["file"]["original_name"] == "my  report.txt"


def test_concurrent_complete_runs_assemble_and_finalize_once(
    settings: Settings,
) -> None:
    app = create_app(settings)
    original_assemble = app.state.chunk_storage.assemble
    original_finalize = app.state.upload_repository.finalize_publication
    assembling = threading.Event()
    release = threading.Event()
    calls = {"assemble": 0, "finalize": 0}

    def blocked_assemble(*args, **kwargs):
        calls["assemble"] += 1
        assembling.set()
        assert release.wait(timeout=2)
        return original_assemble(*args, **kwargs)

    def counted_finalize(*args, **kwargs):
        calls["finalize"] += 1
        return original_finalize(*args, **kwargs)

    app.state.chunk_storage.assemble = blocked_assemble
    app.state.upload_repository.finalize_publication = counted_finalize
    with TestClient(app) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        upload = create_upload(client, content=b"concurrent")
        put_single_part(client, upload, b"concurrent")
        responses: list[object] = []

        def first_complete() -> None:
            responses.append(
                client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})
            )

        first = threading.Thread(target=first_complete)
        first.start()
        assert assembling.wait(timeout=2)
        second = client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})
        release.set()
        first.join(timeout=2)

    assert second.status_code == 409
    assert len(responses) == 1
    assert responses[0].status_code == 200
    assert calls == {"assemble": 1, "finalize": 1}


def test_cancel_preempts_assembly_and_prevents_publication(
    settings: Settings,
) -> None:
    app = create_app(settings)
    original_assemble = app.state.chunk_storage.assemble
    assembling = threading.Event()
    release = threading.Event()
    cancel_finished = threading.Event()
    broadcasts: list[dict[str, object]] = []

    def blocked_assemble(*args, **kwargs):
        assembling.set()
        assert release.wait(timeout=2)
        return original_assemble(*args, **kwargs)

    async def capture(event: dict[str, object]) -> None:
        broadcasts.append(event)

    app.state.chunk_storage.assemble = blocked_assemble
    app.state.hub.broadcast = capture
    with authenticated_client(settings, app=app, device_id="source") as client:
        upload = create_upload(client, content=b"cancel-assembly")
        upload_id = str(upload["upload_id"])
        put_single_part(client, upload, b"cancel-assembly")
        responses: dict[str, object] = {}

        def complete() -> None:
            responses["complete"] = client.post(
                f"/api/uploads/{upload_id}/complete", json={}
            )

        def cancel() -> None:
            responses["cancel"] = client.delete(f"/api/uploads/{upload_id}")
            cancel_finished.set()

        complete_thread = threading.Thread(target=complete)
        cancel_thread = threading.Thread(target=cancel)
        complete_thread.start()
        assert assembling.wait(timeout=2)
        verifying_was_broadcast = any(
            event["event_type"] == "upload.state_changed"
            and event["payload"]["status"] == "verifying"
            for event in broadcasts
        )

        cancel_thread.start()
        try:
            cancel_preempted = cancel_finished.wait(timeout=0.5)
        finally:
            release.set()
        complete_thread.join(timeout=2)
        cancel_thread.join(timeout=2)

        assert verifying_was_broadcast
        assert cancel_preempted
        assert responses["cancel"].status_code == 200
        assert responses["cancel"].json()["status"] == "cancelled"
        assert responses["complete"].status_code == 409
        assert app.state.upload_repository.get(upload_id)["status"] == "cancelled"
        assert client.get("/api/messages?limit=50").json()["items"] == []
        assert not (settings.upload_dir / f"{upload_id}_report.txt").exists()
        assert not (
            settings.upload_dir / ".resumable" / upload_id / "final.uploading"
        ).exists()


def test_complete_broadcasts_only_its_mutation_events(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    broadcasts: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        broadcasts.append(event)

    app.state.hub.broadcast = capture
    original_complete = app.state.upload_service.complete
    interleaved = False

    async def complete_with_interleaved_message(*args, **kwargs):
        nonlocal interleaved
        mutation = await original_complete(*args, **kwargs)
        if not interleaved:
            interleaved = True
            other = app.state.messages.create_text(
                "interleaved",
                "interleaved-message",
                SessionData("other", "Other device", 2_000_000_000),
            )
            await app.state.hub.broadcast(other["event"])
        return mutation

    monkeypatch.setattr(
        app.state.upload_service, "complete", complete_with_interleaved_message
    )
    with authenticated_client(settings, app=app, device_id="source") as client:
        upload = create_upload(client, content=b"events")
        put_single_part(client, upload, b"events")
        response = client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})
        assert response.status_code == 200
        assert response.json()["upload_id"] == upload["upload_id"]
        event_types = [event["event_type"] for event in broadcasts]
        assert event_types.count("message.created") == 2
        assert event_types.count("upload.completed") == 1
        assert event_types.count("file.finalized") == 1
        other_events = [
            event
            for event in broadcasts
            if event["entity_id"] != upload["upload_id"]
            and event["payload"].get("client_request_id") == "interleaved-message"
        ]
        assert len(other_events) == 1

        broadcasts.clear()
        replay = client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})
        assert replay.status_code == 200
        assert replay.json() == response.json()
        assert broadcasts == []


def test_complete_retry_resets_assembling_and_restarts_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    assemble_calls = 0
    original_assemble = app.state.chunk_storage.assemble

    def counted_assemble(*args, **kwargs):
        nonlocal assemble_calls
        assemble_calls += 1
        return original_assemble(*args, **kwargs)

    app.state.chunk_storage.assemble = counted_assemble
    original_set_state = app.state.upload_repository.set_publication_state
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected failure before assembled persistence")
        return original_set_state(*args, **kwargs)

    monkeypatch.setattr(app.state.upload_repository, "set_publication_state", fail_once)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        upload = create_upload(client, content=b"assembling-replay")
        put_single_part(client, upload, b"assembling-replay")

        first = client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})
        replay = client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})

    assert first.status_code == 500
    assert replay.status_code == 200
    assert assemble_calls == 2
    session = app.state.upload_repository.get(str(upload["upload_id"]))
    assert session["status"] == "complete"
    assert session["publication_state"] == "published"


@pytest.mark.parametrize("durable_state", ("assembled", "file_published"))
def test_complete_retry_continues_durable_publication_state(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    durable_state: str,
) -> None:
    app = create_app(settings)
    original_assemble = app.state.chunk_storage.assemble
    assemble_calls = 0

    def counted_assemble(*args, **kwargs):
        nonlocal assemble_calls
        assemble_calls += 1
        return original_assemble(*args, **kwargs)

    app.state.chunk_storage.assemble = counted_assemble
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        content = f"retry-{durable_state}".encode()
        upload = create_upload(client, content=content)
        upload_id = str(upload["upload_id"])
        put_single_part(client, upload, content)

        if durable_state == "assembled":
            original_operation = app.state.upload_service.storage.publish

            def fail_operation(*args, **kwargs):
                raise OSError("injected publish failure")

            monkeypatch.setattr(
                app.state.upload_service.storage, "publish", fail_operation
            )
        else:
            original_operation = app.state.upload_repository.finalize_publication

            def fail_operation(*args, **kwargs):
                raise RuntimeError("injected finalize failure")

            monkeypatch.setattr(
                app.state.upload_repository, "finalize_publication", fail_operation
            )

        first = client.post(f"/api/uploads/{upload_id}/complete", json={})
        assert first.status_code >= 500
        assert app.state.upload_repository.get(upload_id)["publication_state"] == durable_state

        if durable_state == "assembled":
            monkeypatch.setattr(
                app.state.upload_service.storage, "publish", original_operation
            )
        else:
            monkeypatch.setattr(
                app.state.upload_repository, "finalize_publication", original_operation
            )
        retry = client.post(f"/api/uploads/{upload_id}/complete", json={})
        replay = client.post(f"/api/uploads/{upload_id}/complete", json={})

        assert retry.status_code == 200
        assert replay.json() == retry.json()
        assert assemble_calls == 1
        messages = client.get("/api/messages?limit=50").json()["items"]
        assert [item["upload_id"] for item in messages] == [upload_id]
        assert (settings.upload_dir / retry.json()["file"]["storage_name"]).is_file()


def test_complete_retry_finishes_published_state_idempotently(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with authenticated_client(settings, app=app, device_id="source") as client:
        upload = create_upload(client, content=b"published-retry")
        upload_id = str(upload["upload_id"])
        put_single_part(client, upload, b"published-retry")
        completed = client.post(f"/api/uploads/{upload_id}/complete", json={})
        assert completed.status_code == 200
        with app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE upload_sessions SET status = 'verifying' WHERE id = ?",
                (upload_id,),
            )

        retry = client.post(f"/api/uploads/{upload_id}/complete", json={})

        assert retry.status_code == 200
        assert retry.json() == completed.json()
        assert app.state.upload_repository.get(upload_id)["status"] == "complete"
        assert len(client.get("/api/messages?limit=50").json()["items"]) == 1


@pytest.mark.parametrize("damage", ("corrupt", "missing"))
def test_complete_invalidates_damaged_confirmed_part_for_reupload(
    settings: Settings,
    damage: str,
) -> None:
    settings.upload_chunk_size_bytes = 4
    app = create_app(settings)
    with authenticated_client(settings, app=app, device_id="source") as client:
        content = b"abcdefgh"
        upload = create_upload(client, content=content, chunk_size=4)
        upload_id = str(upload["upload_id"])
        put_part(client, upload, 0, content[:4])
        put_part(client, upload, 1, content[4:])
        damaged_path = app.state.chunk_storage.part_path(upload_id, 1)
        if damage == "corrupt":
            damaged_path.write_bytes(b"WXYZ")
        else:
            damaged_path.unlink()

        failed = client.post(f"/api/uploads/{upload_id}/complete", json={})

        assert failed.status_code == 409
        current = client.get(f"/api/uploads/{upload_id}").json()
        assert current["status"] == "failed"
        assert current["confirmed_bytes"] == 4
        assert current["confirmed_parts"] == [0]
        assert not damaged_path.exists()

        resumed = client.patch(
            f"/api/uploads/{upload_id}", json={"action": "resume"}
        )
        assert resumed.status_code == 200
        put_part(client, upload, 1, content[4:])
        completed = client.post(f"/api/uploads/{upload_id}/complete", json={})

        assert completed.status_code == 200
        assert completed.json()["file"]["sha256"] == sha256(content).hexdigest()
        assert len(client.get("/api/messages?limit=50").json()["items"]) == 1


def test_restart_recovers_file_published_session_without_duplicate_message(
    settings: Settings,
) -> None:
    content = b"recover-me"
    upload_id = "d" * 32
    now = datetime(2026, 7, 19, tzinfo=timezone.utc)
    settings.upload_dir.mkdir(parents=True)
    (settings.upload_dir / f"{upload_id}_report.txt").write_bytes(content)
    database = Database(settings.database_path)
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO upload_sessions "
            "(id, client_request_id, source_device_id, source_device_name, original_name, mime_type, size_bytes, "
            "last_modified_ms, sample_sha256, chunk_size_bytes, status, confirmed_bytes, "
            "file_sha256, message_id, error_code, publication_state, created_at, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
            (
                upload_id,
                "recover-request",
                "source",
                "Source device",
                "report.txt",
                "text/plain",
                len(content),
                1_784_412_345_000,
                sha256(b"sample").hexdigest(),
                8 * 1024 * 1024,
                "verifying",
                len(content),
                sha256(content).hexdigest(),
                "file_published",
                now.isoformat(),
                now.isoformat(),
                "2026-07-20T00:00:00+00:00",
            ),
        )

    app = create_app(settings)
    app.state.clock = lambda: now
    with authenticated_client(
        settings, app=app, device_id="source"
    ) as client:
        active = client.get("/api/uploads/active").json()
        messages = client.get("/api/messages?limit=50").json()["items"]

    assert active == []
    assert len([item for item in messages if item.get("upload_id")]) == 1


def test_published_file_is_unavailable_until_database_finalization(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post(
        "/api/session",
        json={
            "access_token": settings.auth_token,
            "device_id": "source",
            "device_name": "source",
        },
    ).status_code == 200
    upload = create_upload(client, content=b"data")
    put_single_part(client, upload, b"data")

    def fail_finalize(*args, **kwargs):
        raise RuntimeError("injected database finalization failure")

    monkeypatch.setattr(
        app.state.upload_repository, "finalize_publication", fail_finalize
    )
    response = client.post(
        f"/api/uploads/{upload['upload_id']}/complete", json={}
    )

    assert response.status_code == 500
    assert client.get(f"/download/{upload['upload_id']}").status_code == 404
    assert app.state.upload_repository.get(str(upload["upload_id"]))[
        "publication_state"
    ] == "file_published"


@pytest.mark.parametrize(
    "failure_point, expected_state",
    (
        ("assembled", "assembled"),
        ("renamed", "assembled"),
        ("database", "file_published"),
    ),
)
def test_restart_recovers_each_publication_failure_to_one_message(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    expected_state: str,
) -> None:
    settings.upload_chunk_size_bytes = 4
    content = f"failure-{failure_point}".encode()
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        upload = create_upload(
            client,
            request_id=f"failure-{failure_point}",
            content=content,
            chunk_size=4,
        )
        for index in range(0, len(content), 4):
            put_part(client, upload, index // 4, content[index : index + 4])

        if failure_point == "assembled":
            original = app.state.upload_repository.set_publication_state

            def fail_after_assembled(upload_id, state, digest, now, ttl):
                result = original(upload_id, state, digest, now, ttl)
                if state == "assembled":
                    raise RuntimeError("injected failure after assembly")
                return result

            monkeypatch.setattr(
                app.state.upload_repository,
                "set_publication_state",
                fail_after_assembled,
            )
        elif failure_point == "renamed":
            original_publish = app.state.upload_service.storage.publish

            def fail_after_rename(pending):
                original_publish(pending)
                raise RuntimeError("injected failure after final rename")

            monkeypatch.setattr(
                app.state.upload_service.storage, "publish", fail_after_rename
            )
        else:
            monkeypatch.setattr(
                app.state.upload_repository,
                "finalize_publication",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    RuntimeError("injected database finalization failure")
                ),
            )

        failed = client.post(
            f"/api/uploads/{upload['upload_id']}/complete", json={}
        )
        assert failed.status_code == 500
        assert client.get(f"/download/{upload['upload_id']}").status_code == 404
        assert app.state.upload_repository.get(str(upload["upload_id"]))[
            "publication_state"
        ] == expected_state
        monkeypatch.undo()
        retry = client.post(
            f"/api/uploads/{upload['upload_id']}/complete", json={}
        )
        assert retry.status_code == 200

    recovered_app = create_app(settings)
    with authenticated_client(
        settings, app=recovered_app, device_id="source"
    ) as recovered:
        messages = recovered.get("/api/messages?limit=50").json()["items"]
        linked = [
            item
            for item in messages
            if item.get("upload_id") == upload["upload_id"]
        ]
        assert len(linked) == 1
        assert recovered.get(f"/download/{upload['upload_id']}").content == content
        replay = recovered.post(
            f"/api/uploads/{upload['upload_id']}/complete", json={}
        )
        assert replay.status_code == 200
        assert replay.json() == linked[0]


def test_restart_reconciles_missing_confirmed_part(settings: Settings) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    upload = create_upload(client, content=b"data")
    put_single_part(client, upload, b"data")
    app.state.chunk_storage.part_path(str(upload["upload_id"]), 0).unlink()

    restarted = create_app(settings)
    with authenticated_client(settings, app=restarted) as recovered:
        session = recovered.get(f"/api/uploads/{upload['upload_id']}").json()

    assert session["status"] == "failed"
    assert session["error_code"] == "missing_part"
    assert session["confirmed_bytes"] == 0
    assert session["confirmed_parts"] == []


def test_startup_recovery_broadcasts_state_mutations_in_sequence(
    settings: Settings,
) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    cases: dict[str, str] = {}
    for index, state in enumerate(("missing", "assembling", "publication_error")):
        content = f"recover-{state}".encode()
        upload = create_upload(
            client,
            request_id=f"startup-state-{index}",
            content=content,
        )
        upload_id = str(upload["upload_id"])
        cases[state] = upload_id
        put_single_part(client, upload, content)
        if state == "missing":
            app.state.chunk_storage.part_path(upload_id, 0).unlink()
            continue
        with app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE upload_sessions SET status = 'verifying', publication_state = ?, "
                "file_sha256 = ? WHERE id = ?",
                (
                    "assembling" if state == "assembling" else "assembled",
                    None if state == "assembling" else sha256(content).hexdigest(),
                    upload_id,
                ),
            )

    restarted = create_app(settings)
    broadcasts: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        broadcasts.append(event)

    restarted.state.hub.broadcast = capture
    with TestClient(restarted):
        pass

    state_events = [
        event
        for event in broadcasts
        if event["event_type"] == "upload.state_changed"
        and event["entity_id"] in cases.values()
    ]
    assert [int(event["sequence"]) for event in state_events] == sorted(
        int(event["sequence"]) for event in state_events
    )
    by_upload = {str(event["entity_id"]): event["payload"] for event in state_events}
    assert by_upload[cases["missing"]]["status"] == "failed"
    assert by_upload[cases["missing"]]["error_code"] == "missing_part"
    assert by_upload[cases["assembling"]]["status"] == "uploading"
    assert by_upload[cases["assembling"]]["publication_state"] == "none"
    assert by_upload[cases["publication_error"]]["status"] == "failed"
    assert by_upload[cases["publication_error"]]["error_code"] == "publication_error"


def test_restart_uses_file_published_state_when_confirmed_part_is_missing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"durable-final"
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        upload = create_upload(client, content=content)
        put_single_part(client, upload, content)
        monkeypatch.setattr(
            app.state.upload_repository,
            "finalize_publication",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("injected database finalization failure")
            ),
        )
        assert client.post(
            f"/api/uploads/{upload['upload_id']}/complete", json={}
        ).status_code == 500
        app.state.chunk_storage.part_path(str(upload["upload_id"]), 0).unlink()

    monkeypatch.undo()
    with authenticated_client(
        settings, app=create_app(settings), device_id="source"
    ) as recovered:
        messages = recovered.get("/api/messages?limit=50").json()["items"]

    linked = [item for item in messages if item.get("upload_id") == upload["upload_id"]]
    assert len(linked) == 1
    assert linked[0]["file"]["sha256"] == sha256(content).hexdigest()


def test_expired_unrecoverable_assembled_session_becomes_expired(
    settings: Settings,
) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    content = b"missing-assembled"
    upload = create_upload(client, content=content)
    put_single_part(client, upload, content)
    upload_id = str(upload["upload_id"])
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    with app.state.database.transaction() as connection:
        connection.execute(
            "UPDATE upload_sessions SET status = 'verifying', publication_state = 'assembled', "
            "file_sha256 = ?, expires_at = ? WHERE id = ?",
            (sha256(content).hexdigest(), "2026-07-20T00:00:00+00:00", upload_id),
        )

    mutations = asyncio.run(app.state.upload_service.expire(now))

    assert [
        event["event_type"]
        for mutation in mutations
        for event in mutation["events"]
    ] == ["upload.state_changed", "upload.expired"]
    assert app.state.upload_repository.get(upload_id)["status"] == "expired"


def test_cancel_file_published_session_removes_unavailable_final_file(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"cancel-published"
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        upload = create_upload(client, content=content)
        put_single_part(client, upload, content)
        monkeypatch.setattr(
            app.state.upload_repository,
            "finalize_publication",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("injected database finalization failure")
            ),
        )
        assert client.post(
            f"/api/uploads/{upload['upload_id']}/complete", json={}
        ).status_code == 500
        final_path = settings.upload_dir / (
            f"{upload['upload_id']}_{upload['original_name']}"
        )
        assert final_path.is_file()

        assert client.delete(f"/api/uploads/{upload['upload_id']}").status_code == 200

    assert not final_path.exists()


@pytest.mark.parametrize("durable_state", ("assembled", "file_published"))
def test_restart_finishes_cancelled_publication_cleanup(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    durable_state: str,
) -> None:
    content = b"restart-cancel-cleanup"
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        upload = create_upload(client, content=content)
        put_single_part(client, upload, content)
        if durable_state == "assembled":
            original_set_state = app.state.upload_repository.set_publication_state

            def fail_file_published(*args, **kwargs):
                if args[1] == "file_published":
                    raise RuntimeError("injected state write failure after rename")
                return original_set_state(*args, **kwargs)

            monkeypatch.setattr(
                app.state.upload_repository,
                "set_publication_state",
                fail_file_published,
            )
        else:
            monkeypatch.setattr(
                app.state.upload_repository,
                "finalize_publication",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    RuntimeError("injected database finalization failure")
                ),
            )
        assert client.post(
            f"/api/uploads/{upload['upload_id']}/complete", json={}
        ).status_code == 500
        assert app.state.upload_repository.get(str(upload["upload_id"]))[
            "publication_state"
        ] == durable_state
        final_path = settings.upload_dir / (
            f"{upload['upload_id']}_{upload['original_name']}"
        )
        original_cleanup = safe_fs.quarantine_verified_file
        monkeypatch.setattr(
            safe_fs,
            "quarantine_verified_file",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("injected cleanup failure")
            ),
        )
        assert client.delete(f"/api/uploads/{upload['upload_id']}").status_code == 200
        assert final_path.is_file()

    monkeypatch.setattr(safe_fs, "quarantine_verified_file", original_cleanup)
    with authenticated_client(settings, app=create_app(settings)):
        pass

    assert not final_path.exists()


def test_cancel_removes_verified_final_after_publish_state_write_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"rename-before-state"
    app = create_app(settings)
    broadcasts: list[dict[str, object]] = []
    original_set_state = app.state.upload_repository.set_publication_state

    async def capture(event: dict[str, object]) -> None:
        broadcasts.append(event)

    def fail_file_published(*args, **kwargs):
        if args[1] == "file_published":
            raise RuntimeError("injected state write failure after rename")
        return original_set_state(*args, **kwargs)

    app.state.hub.broadcast = capture
    monkeypatch.setattr(
        app.state.upload_repository, "set_publication_state", fail_file_published
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        upload = create_upload(client, content=content)
        upload_id = str(upload["upload_id"])
        put_single_part(client, upload, content)

        failed = client.post(f"/api/uploads/{upload_id}/complete", json={})
        final_path = settings.upload_dir / f"{upload_id}_{upload['original_name']}"
        session_dir = settings.upload_dir / ".resumable" / upload_id

        assert failed.status_code == 500
        assert app.state.upload_repository.get(upload_id)["publication_state"] == "assembled"
        assert final_path.read_bytes() == content
        assert session_dir.is_dir()

        original_cleanup = safe_fs.quarantine_verified_file
        cleanup_calls = 0

        def fail_once(*args, **kwargs) -> bool:
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_calls == 1:
                raise OSError("injected transient final cleanup failure")
            return original_cleanup(*args, **kwargs)

        monkeypatch.setattr(safe_fs, "quarantine_verified_file", fail_once)

        cancelled = client.delete(f"/api/uploads/{upload_id}")

        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert final_path.is_file()
        assert not session_dir.exists()
        assert [event["event_type"] for event in broadcasts].count(
            "upload.cancelled"
        ) == 1

        mutations = client.portal.call(
            app.state.upload_service.expire, datetime.now(timezone.utc)
        )

        assert mutations == []
        assert cleanup_calls == 2
        assert not final_path.exists()
        assert not session_dir.exists()


def test_cancel_preserves_unverified_final_after_publish_state_write_failure(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    content = b"owned-content"
    replacement = b"other-content"
    app = create_app(settings)
    original_set_state = app.state.upload_repository.set_publication_state

    def fail_file_published(*args, **kwargs):
        if args[1] == "file_published":
            raise RuntimeError("injected state write failure after rename")
        return original_set_state(*args, **kwargs)

    monkeypatch.setattr(
        app.state.upload_repository, "set_publication_state", fail_file_published
    )
    caplog.set_level(logging.WARNING, logger="transfer.upload")
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        upload = create_upload(client, content=content)
        upload_id = str(upload["upload_id"])
        put_single_part(client, upload, content)
        assert client.post(f"/api/uploads/{upload_id}/complete", json={}).status_code == 500
        final_path = settings.upload_dir / f"{upload_id}_{upload['original_name']}"
        final_path.write_bytes(replacement)

        cancelled = client.delete(f"/api/uploads/{upload_id}")

        assert cancelled.status_code == 200
        assert not final_path.exists()
        isolated = list(settings.upload_dir.glob(".cleanup-*"))
        assert len(isolated) == 1
        assert isolated[0].read_bytes() == replacement
        assert f"isolated_name={isolated[0].name}" in caplog.text


def test_maintenance_retries_complete_session_chunk_cleanup(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"complete-cleanup-retry"
    app = create_app(settings)
    original_cleanup = app.state.upload_service.chunks.cleanup_session
    cleanup_calls = 0

    def fail_once(upload_id: str) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise OSError("injected transient cleanup failure")
        original_cleanup(upload_id)

    monkeypatch.setattr(app.state.upload_service.chunks, "cleanup_session", fail_once)
    with authenticated_client(settings, app=app, device_id="source") as client:
        upload = create_upload(client, content=content)
        upload_id = str(upload["upload_id"])
        put_single_part(client, upload, content)

        completed = client.post(f"/api/uploads/{upload_id}/complete", json={})
        session_dir = settings.upload_dir / ".resumable" / upload_id
        final_path = settings.upload_dir / completed.json()["file"]["storage_name"]
        messages_before = client.get("/api/messages?limit=50").json()["items"]

        assert completed.status_code == 200
        assert session_dir.is_dir()
        assert final_path.read_bytes() == content

        mutations = client.portal.call(
            app.state.upload_service.expire, datetime.now(timezone.utc)
        )

        assert mutations == []
        assert cleanup_calls == 2
        assert not session_dir.exists()
        assert final_path.read_bytes() == content
        assert client.get("/api/messages?limit=50").json()["items"] == messages_before


def test_startup_retries_complete_session_chunk_cleanup(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"complete-startup-cleanup"
    app = create_app(settings)
    monkeypatch.setattr(
        app.state.upload_service.chunks,
        "cleanup_session",
        lambda upload_id: (_ for _ in ()).throw(
            OSError("injected persistent cleanup failure")
        ),
    )
    with authenticated_client(settings, app=app, device_id="source") as client:
        upload = create_upload(client, content=content)
        upload_id = str(upload["upload_id"])
        put_single_part(client, upload, content)
        completed = client.post(f"/api/uploads/{upload_id}/complete", json={})
        session_dir = settings.upload_dir / ".resumable" / upload_id
        final_path = settings.upload_dir / completed.json()["file"]["storage_name"]
        messages_before = client.get("/api/messages?limit=50").json()["items"]

        assert completed.status_code == 200
        assert session_dir.is_dir()

    with authenticated_client(
        settings, app=create_app(settings), device_id="source"
    ) as restarted:
        messages_after = restarted.get("/api/messages?limit=50").json()["items"]

    assert not session_dir.exists()
    assert final_path.read_bytes() == content
    assert messages_after == messages_before


def test_expire_returns_recovery_mutation_for_maintenance_broadcast(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"maintenance-recovery"
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        upload = create_upload(client, content=content)
        put_single_part(client, upload, content)
        monkeypatch.setattr(
            app.state.upload_repository,
            "finalize_publication",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("injected database finalization failure")
            ),
        )
        assert client.post(
            f"/api/uploads/{upload['upload_id']}/complete", json={}
        ).status_code == 500
        with app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE upload_sessions SET expires_at = ? WHERE id = ?",
                ("2026-07-18T00:00:00+00:00", upload["upload_id"]),
            )

    monkeypatch.undo()
    mutations = asyncio.run(
        app.state.upload_service.expire(
            datetime(2026, 7, 19, tzinfo=timezone.utc)
        )
    )

    assert len(mutations) == 1
    assert mutations[0]["result"]["upload_id"] == upload["upload_id"]
    assert app.state.upload_repository.get(str(upload["upload_id"]))[
        "status"
    ] == "complete"
    assert any(
        event["event_type"] == "message.created" for event in mutations[0]["events"]
    )


def test_maintenance_waits_for_in_progress_assembly(
    settings: Settings,
) -> None:
    app = create_app(settings)
    original_assemble = app.state.chunk_storage.assemble
    assembled = threading.Event()
    release = threading.Event()

    def blocked_after_assembly(*args, **kwargs):
        pending = original_assemble(*args, **kwargs)
        assembled.set()
        assert release.wait(timeout=2)
        return pending

    app.state.chunk_storage.assemble = blocked_after_assembly
    with TestClient(app) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        upload = create_upload(client, content=b"maintenance-race")
        put_single_part(client, upload, b"maintenance-race")
        upload_id = str(upload["upload_id"])
        responses: dict[str, object] = {}

        def run_complete() -> None:
            responses["complete"] = client.post(
                f"/api/uploads/{upload_id}/complete", json={}
            )

        complete_thread = threading.Thread(target=run_complete)
        complete_thread.start()
        assert assembled.wait(timeout=2)
        with app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE upload_sessions SET expires_at = ? WHERE id = ?",
                ("2026-07-18T00:00:00+00:00", upload_id),
            )

        def run_expire() -> None:
            responses["expire"] = client.portal.call(
                app.state.upload_service.expire,
                datetime(2026, 7, 19, tzinfo=timezone.utc),
            )

        expire_thread = threading.Thread(target=run_expire)
        expire_thread.start()
        time.sleep(0.05)
        maintenance_waited = expire_thread.is_alive()
        assembled_path = settings.upload_dir / ".resumable" / upload_id / "final.uploading"
        assembled_survived = assembled_path.is_file()
        release.set()
        complete_thread.join(timeout=2)
        expire_thread.join(timeout=2)

    assert not maintenance_waited
    assert assembled_survived
    assert responses["complete"].status_code == 200
    assert responses["expire"] == []
    assert app.state.upload_repository.get(upload_id)["status"] == "complete"


def test_maintenance_does_not_recover_unexpired_verifying_session(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        content = b"not-due"
        upload = create_upload(client, content=content)
        put_single_part(client, upload, content)
        upload_id = str(upload["upload_id"])
        now = datetime(2026, 7, 19, tzinfo=timezone.utc)
        session = app.state.upload_repository.begin_completion(
            upload_id, now, settings.upload_session_ttl_seconds
        )
        pending = app.state.chunk_storage.assemble(
            session, app.state.upload_repository.list_parts(upload_id)
        )
        app.state.upload_repository.set_publication_state(
            upload_id,
            "assembled",
            pending.sha256,
            now,
            settings.upload_session_ttl_seconds,
        )

        mutations = client.portal.call(app.state.upload_service.expire, now)

    current = app.state.upload_repository.get(upload_id)
    assert mutations == []
    assert current["status"] == "verifying"
    assert current["publication_state"] == "assembled"
    assert pending.temporary_path.is_file()


def test_maintenance_recovers_due_verifying_session_under_upload_lock(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        content = b"due-recovery"
        upload = create_upload(client, content=content)
        put_single_part(client, upload, content)
        upload_id = str(upload["upload_id"])
        now = datetime(2026, 7, 19, tzinfo=timezone.utc)
        session = app.state.upload_repository.begin_completion(
            upload_id, now, settings.upload_session_ttl_seconds
        )
        pending = app.state.chunk_storage.assemble(
            session, app.state.upload_repository.list_parts(upload_id)
        )
        app.state.upload_repository.set_publication_state(
            upload_id,
            "assembled",
            pending.sha256,
            now,
            settings.upload_session_ttl_seconds,
        )
        with app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE upload_sessions SET expires_at = ? WHERE id = ?",
                ("2026-07-18T00:00:00+00:00", upload_id),
            )

        mutations = client.portal.call(app.state.upload_service.expire, now)

    assert len(mutations) == 1
    assert mutations[0]["result"]["upload_id"] == upload_id
    assert app.state.upload_repository.get(upload_id)["status"] == "complete"


def test_expire_cleans_temporary_parts_and_emits_event(settings: Settings) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    upload = create_upload(client, content=b"data")
    put_single_part(client, upload, b"data")
    upload_id = str(upload["upload_id"])
    session_dir = app.state.chunk_storage.part_path(upload_id, 0).parent
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    with app.state.database.transaction() as connection:
        connection.execute(
            "UPDATE upload_sessions SET expires_at = ? WHERE id = ?",
            ("2026-07-20T00:00:00+00:00", upload_id),
        )

    mutations = asyncio.run(app.state.upload_service.expire(now))

    assert len(mutations) == 1
    assert mutations[0]["events"][0]["event_type"] == "upload.expired"
    assert app.state.upload_repository.get(upload_id)["status"] == "expired"
    assert not session_dir.exists()


def test_create_upload_requires_signed_session(settings: Settings) -> None:
    client = TestClient(create_app(settings))
    payload = {
        "client_request_id": "request-1",
        "name": "report.txt",
        "size_bytes": 4,
        "mime_type": "text/plain",
        "last_modified_ms": 1,
        "chunk_size_bytes": 8,
        "sample_sha256": sha256(b"sample").hexdigest(),
    }
    assert client.post("/api/uploads", json=payload).status_code == 401
    client.cookies.set("transfer_session", "tampered")
    assert client.post("/api/uploads", json=payload).status_code == 401


def test_create_upload_replays_metadata_and_persists_session_device_name(
    settings: Settings,
) -> None:
    client = authenticated_client(settings, device_id="source-device")
    first = create_upload(client)
    replay = create_upload(client)

    assert replay == first
    assert first["status"] == "queued"
    assert first["confirmed_parts"] == []
    assert first["source_device_id"] == "source-device"
    assert first["source_device_name"] == "source-device"


def test_create_upload_conflicting_metadata_returns_409(settings: Settings) -> None:
    client = authenticated_client(settings)
    create_upload(client)
    response = client.post(
        "/api/uploads",
        json={
            "client_request_id": "request-1",
            "name": "other.txt",
            "size_bytes": 4,
            "mime_type": "text/plain",
            "last_modified_ms": 1_784_412_345_000,
            "chunk_size_bytes": settings.upload_chunk_size_bytes,
            "sample_sha256": sha256(b"sample").hexdigest(),
        },
    )
    assert response.status_code == 409


def test_create_upload_rejects_oversized_file(settings: Settings) -> None:
    client = authenticated_client(settings)
    response = client.post(
        "/api/uploads",
        json={
            "client_request_id": "too-large",
            "name": "report.txt",
            "size_bytes": settings.max_upload_size + 1,
            "mime_type": "text/plain",
            "last_modified_ms": 1,
            "chunk_size_bytes": settings.upload_chunk_size_bytes,
            "sample_sha256": sha256(b"sample").hexdigest(),
        },
    )
    assert response.status_code == 413


def test_create_upload_maps_active_and_storage_capacity(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.max_active_upload_sessions = 1
    client = authenticated_client(settings)
    create_upload(client)
    capacity = client.post(
        "/api/uploads",
        json={
            "client_request_id": "request-2",
            "name": "report.txt",
            "size_bytes": 4,
            "mime_type": "text/plain",
            "last_modified_ms": 1,
            "chunk_size_bytes": settings.upload_chunk_size_bytes,
            "sample_sha256": sha256(b"sample").hexdigest(),
        },
    )
    assert capacity.status_code == 429

    settings.max_active_upload_sessions = 2
    monkeypatch.setattr(
        "app.upload_service.shutil.disk_usage",
        lambda _: type("Usage", (), {"free": settings.upload_storage_reserve_bytes})(),
    )
    storage = client.post(
        "/api/uploads",
        json={
            "client_request_id": "request-3",
            "name": "report.txt",
            "size_bytes": 4,
            "mime_type": "text/plain",
            "last_modified_ms": 1,
            "chunk_size_bytes": settings.upload_chunk_size_bytes,
            "sample_sha256": sha256(b"sample").hexdigest(),
        },
    )
    assert storage.status_code == 507
    assert not (settings.upload_dir / ".resumable").exists()


def test_chunk_capacity_returns_503_before_creating_temporary_file(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.max_concurrent_chunk_handlers = 1
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    first = create_upload(client, request_id="chunk-capacity-1")
    second = create_upload(client, request_id="chunk-capacity-2")
    started = threading.Event()
    release = threading.Event()
    original_write = app.state.chunk_storage.write_part

    async def blocked_write(*args, **kwargs):
        started.set()
        await asyncio.to_thread(release.wait)
        return await original_write(*args, **kwargs)

    monkeypatch.setattr(app.state.chunk_storage, "write_part", blocked_write)
    responses: dict[str, object] = {}

    def upload_first() -> None:
        responses["first"] = client.put(
            f"/api/uploads/{first['upload_id']}/parts/0",
            content=b"data",
            headers={
                "Content-Range": "bytes 0-3/4",
                "X-Chunk-SHA256": sha256(b"data").hexdigest(),
            },
        )

    thread = threading.Thread(target=upload_first)
    thread.start()
    assert started.wait(timeout=2)
    excess = client.put(
        f"/api/uploads/{second['upload_id']}/parts/0",
        content=b"data",
        headers={
            "Content-Range": "bytes 0-3/4",
            "X-Chunk-SHA256": sha256(b"data").hexdigest(),
        },
    )
    release.set()
    thread.join(timeout=2)

    assert excess.status_code == 503
    assert excess.json()["detail"] == "Too many concurrent chunk uploads"
    assert responses["first"].status_code == 200
    assert not (settings.upload_dir / ".resumable" / str(second["upload_id"])).exists()


def test_chunk_rate_limit_uses_normalized_route_key(settings: Settings) -> None:
    settings.upload_chunk_size_bytes = 4
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    upload = create_upload(
        client, request_id="normalized-rate", content=b"abcdefgh", chunk_size=4
    )
    settings.rate_limit_count = 1

    put_part(client, upload, 0, b"abcd")
    response = client.put(
        f"/api/uploads/{upload['upload_id']}/parts/1",
        content=b"efgh",
        headers={
            "Content-Range": "bytes 4-7/8",
            "X-Chunk-SHA256": sha256(b"efgh").hexdigest(),
        },
    )

    assert response.status_code == 429


def test_create_upload_storage_check_error_returns_507(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = authenticated_client(settings)

    def failed_capacity_check(path) -> None:
        raise OSError(errno.EIO, "Storage failure")

    monkeypatch.setattr("app.upload_service.shutil.disk_usage", failed_capacity_check)
    response = client.post(
        "/api/uploads",
        json={
            "client_request_id": "storage-error",
            "name": "report.txt",
            "size_bytes": 4,
            "mime_type": "text/plain",
            "last_modified_ms": 1,
            "chunk_size_bytes": settings.upload_chunk_size_bytes,
            "sample_sha256": sha256(b"sample").hexdigest(),
        },
    )
    assert response.status_code == 507


def test_create_upload_replay_precedes_current_storage_and_extension_policy(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = authenticated_client(settings)
    created = create_upload(client)
    settings.allowed_extensions = {".md"}
    monkeypatch.setattr(
        "app.upload_service.shutil.disk_usage",
        lambda _: type("Usage", (), {"free": 0})(),
    )
    replay = create_upload(client)
    assert replay == created


@pytest.mark.parametrize("name", ["report.exe", "   ", "@@@"])
def test_create_upload_rejects_extension_or_empty_sanitized_name(
    settings: Settings, name: str
) -> None:
    client = authenticated_client(settings)
    response = client.post(
        "/api/uploads",
        json={
            "client_request_id": "invalid-name",
            "name": name,
            "size_bytes": 4,
            "mime_type": "text/plain",
            "last_modified_ms": 1,
            "chunk_size_bytes": settings.upload_chunk_size_bytes,
            "sample_sha256": sha256(b"sample").hexdigest(),
        },
    )
    assert response.status_code == 422


def test_resumable_and_legacy_uploads_conflict_in_both_orders(settings: Settings) -> None:
    first_app = create_app(settings)
    first = authenticated_client(settings, app=first_app)
    create_upload(first, request_id="resumable-first")
    legacy_conflict = first.post(
        "/api/upload",
        data={"client_request_id": "resumable-first"},
        files={"file": ("report.txt", b"data", "text/plain")},
    )
    assert legacy_conflict.status_code == 409

    second_settings = Settings(
        **{
            field: getattr(settings, field)
            for field in settings.__dataclass_fields__
            if field not in {"upload_dir", "database_path"}
        },
        upload_dir=settings.upload_dir.parent / "legacy-first",
        database_path=settings.database_path.parent / "legacy-first.sqlite3",
    )
    second = authenticated_client(second_settings)
    legacy = second.post(
        "/api/upload",
        data={"client_request_id": "legacy-first"},
        files={"file": ("report.txt", b"data", "text/plain")},
    )
    assert legacy.status_code == 200
    resumable_conflict = second.post(
        "/api/uploads",
        json={
            "client_request_id": "legacy-first",
            "name": "report.txt",
            "size_bytes": 4,
            "mime_type": "text/plain",
            "last_modified_ms": 1,
            "chunk_size_bytes": second_settings.upload_chunk_size_bytes,
            "sample_sha256": sha256(b"sample").hexdigest(),
        },
    )
    assert resumable_conflict.status_code == 409


def test_resumable_and_legacy_uploads_serialize_concurrent_request_id(
    settings: Settings,
) -> None:
    app = create_app(settings)
    original_stage = app.state.storage.stage_upload
    staged = threading.Event()
    release = threading.Event()

    def blocked_stage(*args, **kwargs):
        pending = original_stage(*args, **kwargs)
        staged.set()
        assert release.wait(timeout=2)
        return pending

    app.state.storage.stage_upload = blocked_stage
    with TestClient(app) as client:
        assert client.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        results: dict[str, object] = {}

        def legacy_request() -> None:
            results["legacy"] = client.post(
                "/api/upload",
                data={"client_request_id": "concurrent"},
                files={"file": ("report.txt", b"data", "text/plain")},
            )

        def resumable_request() -> None:
            results["resumable"] = client.post(
                "/api/uploads",
                json={
                    "client_request_id": "concurrent",
                    "name": "report.txt",
                    "size_bytes": 4,
                    "mime_type": "text/plain",
                    "last_modified_ms": 1,
                    "chunk_size_bytes": settings.upload_chunk_size_bytes,
                    "sample_sha256": sha256(b"sample").hexdigest(),
                },
            )

        legacy_thread = threading.Thread(target=legacy_request)
        resumable_thread = threading.Thread(target=resumable_request)
        legacy_thread.start()
        assert staged.wait(timeout=2)
        resumable_thread.start()
        time.sleep(0.05)
        release.set()
        legacy_thread.join(timeout=2)
        resumable_thread.join(timeout=2)
    assert results["legacy"].status_code == 200
    assert results["resumable"].status_code == 409


def test_first_confirmed_chunk_changes_queued_to_uploading(settings: Settings) -> None:
    client = authenticated_client(settings, device_id="source")
    upload = create_upload(client)
    assert upload["status"] == "queued"
    result = put_single_part(client, upload, b"data")
    assert result["status"] == "uploading"
    assert result["confirmed_parts"] == [0]


@pytest.mark.parametrize(
    ("part_index", "content_range", "digest"),
    [
        (-1, "bytes 0-3/4", sha256(b"data").hexdigest()),
        (1, "bytes 0-3/4", sha256(b"data").hexdigest()),
        (0, "bytes 1-3/4", sha256(b"data").hexdigest()),
        (0, "bytes 0-2/4", sha256(b"data").hexdigest()),
        (0, "bytes 0-3/5", sha256(b"data").hexdigest()),
        (0, "items 0-3/4", sha256(b"data").hexdigest()),
        (0, "bytes 0-3/4", "bad-digest"),
    ],
)
def test_chunk_rejects_invalid_index_range_total_or_digest(
    settings: Settings, part_index: int, content_range: str, digest: str
) -> None:
    client = authenticated_client(settings)
    upload = create_upload(client)
    response = client.put(
        f"/api/uploads/{upload['upload_id']}/parts/{part_index}",
        content=b"data",
        headers={"Content-Range": content_range, "X-Chunk-SHA256": digest},
    )
    assert response.status_code == 400


def test_chunk_rejects_body_size_and_digest_mismatch(settings: Settings) -> None:
    client = authenticated_client(settings)
    upload = create_upload(client)
    too_short = client.put(
        f"/api/uploads/{upload['upload_id']}/parts/0",
        content=b"dat",
        headers={
            "Content-Range": "bytes 0-3/4",
            "X-Chunk-SHA256": sha256(b"dat").hexdigest(),
        },
    )
    wrong_digest = client.put(
        f"/api/uploads/{upload['upload_id']}/parts/0",
        content=b"data",
        headers={
            "Content-Range": "bytes 0-3/4",
            "X-Chunk-SHA256": sha256(b"other").hexdigest(),
        },
    )
    assert too_short.status_code == 400
    assert wrong_digest.status_code == 400


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EIO])
def test_chunk_storage_errors_return_507(
    settings: Settings, error_number: int
) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    upload = create_upload(client)

    async def storage_full(*args, **kwargs):
        raise OSError(error_number, "Storage failure")

    app.state.upload_service.chunks.write_part = storage_full
    response = client.put(
        f"/api/uploads/{upload['upload_id']}/parts/0",
        content=b"data",
        headers={
            "Content-Range": "bytes 0-3/4",
            "X-Chunk-SHA256": sha256(b"data").hexdigest(),
        },
    )
    assert response.status_code == 507


def test_chunk_identical_replay_succeeds_and_conflicting_replay_returns_409(
    settings: Settings,
) -> None:
    client = authenticated_client(settings)
    upload = create_upload(client)
    first = put_single_part(client, upload, b"data")
    replay = put_single_part(client, upload, b"data")
    conflict = client.put(
        f"/api/uploads/{upload['upload_id']}/parts/0",
        content=b"DATA",
        headers={
            "Content-Range": "bytes 0-3/4",
            "X-Chunk-SHA256": sha256(b"DATA").hexdigest(),
        },
    )
    assert replay == first
    assert conflict.status_code == 409


def test_chunk_idempotent_replay_returns_before_reading_body(settings: Settings) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    upload = create_upload(client)
    put_single_part(client, upload, b"data")
    original_write = app.state.upload_service.chunks.write_part

    async def fail_if_streamed(*args, **kwargs):
        raise AssertionError("idempotent replay streamed the request body")

    app.state.upload_service.chunks.write_part = fail_if_streamed
    try:
        replay = put_single_part(client, upload, b"data")
    finally:
        app.state.upload_service.chunks.write_part = original_write
    assert replay["confirmed_parts"] == [0]


def test_upload_control_is_source_only_and_observer_can_cancel(settings: Settings) -> None:
    app = create_app(settings)
    source = authenticated_client(settings, app=app, device_id="source")
    observer = authenticated_client(settings, app=app, device_id="observer")
    upload = create_upload(source)

    denied = observer.patch(
        f"/api/uploads/{upload['upload_id']}", json={"action": "pause"}
    )
    paused = source.patch(
        f"/api/uploads/{upload['upload_id']}", json={"action": "pause"}
    )
    resumed = source.patch(
        f"/api/uploads/{upload['upload_id']}", json={"action": "resume"}
    )
    cancelled = observer.delete(f"/api/uploads/{upload['upload_id']}")

    assert denied.status_code == 409
    assert "source device" in denied.json()["detail"].lower()
    assert paused.json()["status"] == "paused"
    assert resumed.json()["status"] == "uploading"
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_paused_upload_rejects_new_chunk(settings: Settings) -> None:
    client = authenticated_client(settings)
    upload = create_upload(client)
    assert client.patch(
        f"/api/uploads/{upload['upload_id']}", json={"action": "pause"}
    ).status_code == 200
    response = client.put(
        f"/api/uploads/{upload['upload_id']}/parts/0",
        content=b"data",
        headers={
            "Content-Range": "bytes 0-3/4",
            "X-Chunk-SHA256": sha256(b"data").hexdigest(),
        },
    )
    assert response.status_code == 409


def test_cancel_is_idempotent_and_missing_upload_is_404(settings: Settings) -> None:
    client = authenticated_client(settings)
    upload = create_upload(client)
    first = client.delete(f"/api/uploads/{upload['upload_id']}")
    replay = client.delete(f"/api/uploads/{upload['upload_id']}")
    missing = client.get("/api/uploads/00000000000000000000000000000000")
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert missing.status_code == 404


def test_cancel_cleanup_failure_returns_cancelled_and_maintenance_retries(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(settings)
    broadcasts: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        broadcasts.append(event)

    app.state.hub.broadcast = capture
    original_cleanup = app.state.upload_service.chunks.cleanup_session

    def cleanup_failure(upload_id: str) -> None:
        raise OSError(errno.EIO, "Storage failure")

    with authenticated_client(settings, app=app) as client:
        upload = create_upload(client)
        upload_id = str(upload["upload_id"])
        put_single_part(client, upload, b"data")
        session_dir = settings.upload_dir / ".resumable" / upload_id
        app.state.upload_service.chunks.cleanup_session = cleanup_failure

        with caplog.at_level(logging.WARNING, logger="transfer.upload"):
            response = client.delete(f"/api/uploads/{upload_id}")

        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert app.state.upload_repository.get(upload_id)["status"] == "cancelled"
        assert [event["event_type"] for event in broadcasts].count("upload.cancelled") == 1
        assert upload_id in caplog.text
        assert session_dir.exists()

        app.state.upload_service.chunks.cleanup_session = original_cleanup
        mutations = client.portal.call(
            app.state.upload_service.expire, datetime.now(timezone.utc)
        )
        assert mutations == []
        assert not session_dir.exists()


def test_active_uploads_are_visible_to_observer(settings: Settings) -> None:
    app = create_app(settings)
    source = authenticated_client(settings, app=app, device_id="source")
    observer = authenticated_client(settings, app=app, device_id="observer")
    upload = create_upload(source)
    response = observer.get("/api/uploads/active")
    assert response.status_code == 200
    assert [item["upload_id"] for item in response.json()] == [upload["upload_id"]]


def test_upload_cors_preflight_allows_put_and_patch(settings: Settings) -> None:
    settings.allowed_origins = ["https://example.test"]
    client = TestClient(create_app(settings))
    for method in ("PUT", "PATCH"):
        response = client.options(
            "/api/uploads/example",
            headers={
                "Origin": "https://example.test",
                "Access-Control-Request-Method": method,
            },
        )
        assert response.status_code == 200
        assert method in response.headers["access-control-allow-methods"]


def test_pause_can_win_while_chunk_body_streams(settings: Settings) -> None:
    app = create_app(settings)
    started = threading.Event()
    release = threading.Event()
    original_write = app.state.upload_service.chunks.write_part

    async def blocked_write(*args, **kwargs):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.001)
        return await original_write(*args, **kwargs)

    app.state.upload_service.chunks.write_part = blocked_write
    result: dict[str, object] = {}
    with TestClient(app) as source:
        assert source.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        upload = create_upload(source)

        def upload_part() -> None:
            result["response"] = source.put(
                f"/api/uploads/{upload['upload_id']}/parts/0",
                content=b"data",
                headers={
                    "Content-Range": "bytes 0-3/4",
                    "X-Chunk-SHA256": sha256(b"data").hexdigest(),
                },
            )

        thread = threading.Thread(target=upload_part)
        thread.start()
        assert started.wait(timeout=2)
        paused = source.patch(
            f"/api/uploads/{upload['upload_id']}", json={"action": "pause"}
        )
        release.set()
        thread.join(timeout=2)
    assert paused.status_code == 200
    assert result["response"].status_code == 200
    assert result["response"].json()["status"] == "paused"


def test_cancel_can_win_while_chunk_body_streams_and_discards_part(
    settings: Settings,
) -> None:
    settings.client_request_lock_capacity = 1
    app = create_app(settings)
    started = threading.Event()
    release = threading.Event()
    original_write = app.state.upload_service.chunks.write_part

    async def blocked_write(
        upload_id, part_index, chunks, expected_size, expected_sha256, on_bytes=None
    ):
        async def after_real_write(count: int) -> None:
            assert list(
                app.state.upload_service.chunks
                .part_path(upload_id, part_index)
                .parent.glob("incoming-*")
            )
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.001)

        return await original_write(
            upload_id,
            part_index,
            chunks,
            expected_size,
            expected_sha256,
            after_real_write,
        )

    app.state.upload_service.chunks.write_part = blocked_write
    result: dict[str, object] = {}
    with TestClient(app, raise_server_exceptions=False) as source:
        assert source.post(
            "/api/session",
            json={
                "access_token": settings.auth_token,
                "device_id": "source",
                "device_name": "source",
            },
        ).status_code == 200
        upload = create_upload(source)

        def upload_part() -> None:
            result["response"] = source.put(
                f"/api/uploads/{upload['upload_id']}/parts/0",
                content=b"data",
                headers={
                    "Content-Range": "bytes 0-3/4",
                    "X-Chunk-SHA256": sha256(b"data").hexdigest(),
                },
            )

        thread = threading.Thread(target=upload_part)
        thread.start()
        assert started.wait(timeout=2)
        session_dir = app.state.upload_service.chunks.part_path(
            str(upload["upload_id"]), 0
        ).parent
        assert list(session_dir.glob("incoming-*"))
        capacity = source.post(
            "/api/uploads",
            json={
                "client_request_id": "other-key",
                "name": "other.txt",
                "size_bytes": 4,
                "mime_type": "text/plain",
                "last_modified_ms": 1,
                "chunk_size_bytes": settings.upload_chunk_size_bytes,
                "sample_sha256": sha256(b"sample").hexdigest(),
            },
        )
        cancelled = source.delete(f"/api/uploads/{upload['upload_id']}")
        release.set()
        thread.join(timeout=2)
    assert cancelled.status_code == 200
    assert capacity.status_code == 503
    assert result["response"].status_code == 409
    assert not session_dir.exists()


def test_complete_upload_cancellation_returns_409(settings: Settings) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app, device_id="source")
    upload = create_upload(client)
    put_single_part(client, upload, b"data")
    with app.state.database.transaction() as connection:
        connection.execute(
            "UPDATE upload_sessions SET status = 'complete' WHERE id = ?",
            (upload["upload_id"],),
        )
    response = client.delete(f"/api/uploads/{upload['upload_id']}")
    assert response.status_code == 409
    assert response.json()["detail"] == "Completed uploads cannot be cancelled"


def test_recover_isolates_session_failure_and_logs_upload_id(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    uploads = [
        create_upload(client, request_id=f"recover-isolation-{index}")
        for index in range(2)
    ]
    upload_ids = sorted(str(upload["upload_id"]) for upload in uploads)
    with app.state.database.transaction() as connection:
        connection.execute(
            "UPDATE upload_sessions SET status = 'verifying', "
            "publication_state = 'assembling' WHERE id IN (?, ?)",
            upload_ids,
        )
    original_discard = app.state.chunk_storage.discard_assembled

    def fail_first_discard(upload_id: str) -> None:
        if upload_id == upload_ids[0]:
            raise OSError("injected discard failure")
        original_discard(upload_id)

    monkeypatch.setattr(
        app.state.chunk_storage, "discard_assembled", fail_first_discard
    )
    caplog.set_level(logging.ERROR, logger="transfer.upload")

    mutations = asyncio.run(
        app.state.upload_service.recover(datetime.now(timezone.utc))
    )

    assert len(mutations) == 1
    assert mutations[0]["result"]["upload_id"] == upload_ids[1]
    assert mutations[0]["events"][0]["event_type"] == "upload.state_changed"
    assert app.state.upload_repository.get(upload_ids[0])["status"] == "verifying"
    assert app.state.upload_repository.get(upload_ids[1])["status"] == "uploading"
    assert any(
        upload_ids[0] in record.getMessage() and "recover" in record.getMessage()
        for record in caplog.records
    )


def test_expire_isolates_session_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    uploads = [
        create_upload(client, request_id=f"expire-isolation-{index}")
        for index in range(2)
    ]
    upload_ids = sorted(str(upload["upload_id"]) for upload in uploads)
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    with app.state.database.transaction() as connection:
        connection.execute(
            "UPDATE upload_sessions SET expires_at = ? WHERE id IN (?, ?)",
            ("2026-07-20T00:00:00+00:00", *upload_ids),
        )
    original_expire_one = app.state.upload_repository.expire_one

    def fail_first_expiry(upload_id: str, *args, **kwargs):
        if upload_id == upload_ids[0]:
            raise sqlite3.OperationalError("injected expiry failure")
        return original_expire_one(upload_id, *args, **kwargs)

    monkeypatch.setattr(
        app.state.upload_repository, "expire_one", fail_first_expiry
    )

    mutations = asyncio.run(app.state.upload_service.expire(now))

    assert [mutation["result"]["upload_id"] for mutation in mutations] == [
        upload_ids[1]
    ]
    assert app.state.upload_repository.get(upload_ids[0])["status"] == "queued"
    assert app.state.upload_repository.get(upload_ids[1])["status"] == "expired"


def test_startup_recovery_continues_after_single_session_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    uploads = [
        create_upload(client, request_id=f"startup-isolation-{index}")
        for index in range(2)
    ]
    upload_ids = sorted(str(upload["upload_id"]) for upload in uploads)
    with app.state.database.transaction() as connection:
        connection.execute(
            "UPDATE upload_sessions SET status = 'verifying', "
            "publication_state = 'assembling' WHERE id IN (?, ?)",
            upload_ids,
        )
    original_discard = app.state.chunk_storage.discard_assembled

    def fail_first_discard(upload_id: str) -> None:
        if upload_id == upload_ids[0]:
            raise RuntimeError("injected startup recovery failure")
        original_discard(upload_id)

    monkeypatch.setattr(
        app.state.chunk_storage, "discard_assembled", fail_first_discard
    )

    with TestClient(app):
        assert not app.state.maintenance_task.done()

    assert app.state.upload_repository.get(upload_ids[0])["status"] == "verifying"
    assert app.state.upload_repository.get(upload_ids[1])["status"] == "uploading"
