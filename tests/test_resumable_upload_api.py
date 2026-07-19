from __future__ import annotations

import errno
import threading
from hashlib import sha256

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


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
    chunk_size: int = 8 * 1024 * 1024,
) -> dict[str, object]:
    response = client.post(
        "/api/uploads",
        json={
            "client_request_id": request_id,
            "name": "report.txt",
            "size_bytes": len(content),
            "mime_type": "text/plain",
            "last_modified_ms": 1_784_412_345_000,
            "chunk_size_bytes": chunk_size,
            "sample_sha256": sha256(b"sample").hexdigest(),
        },
    )
    assert response.status_code == 200
    return response.json()


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
            "chunk_size_bytes": 8 * 1024 * 1024,
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


def test_chunk_storage_capacity_error_returns_507(settings: Settings) -> None:
    app = create_app(settings)
    client = authenticated_client(settings, app=app)
    upload = create_upload(client)

    async def storage_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

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
    source = authenticated_client(settings, app=app)
    upload = create_upload(source)
    started = threading.Event()
    release = threading.Event()
    original_write = app.state.upload_service.chunks.write_part

    async def blocked_write(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2)
        return await original_write(*args, **kwargs)

    app.state.upload_service.chunks.write_part = blocked_write
    result: dict[str, object] = {}

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
    app = create_app(settings)
    source = authenticated_client(settings, app=app)
    upload = create_upload(source)
    started = threading.Event()
    release = threading.Event()
    original_write = app.state.upload_service.chunks.write_part

    async def blocked_write(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2)
        return await original_write(*args, **kwargs)

    app.state.upload_service.chunks.write_part = blocked_write
    result: dict[str, object] = {}

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
    cancelled = source.delete(f"/api/uploads/{upload['upload_id']}")
    release.set()
    thread.join(timeout=2)
    assert cancelled.status_code == 200
    assert result["response"].status_code == 409
    assert not app.state.upload_service.chunks.part_path(
        str(upload["upload_id"]), 0
    ).exists()


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
