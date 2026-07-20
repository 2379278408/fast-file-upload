from __future__ import annotations

import gc
import tracemalloc
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


CHUNK_SIZE = 8 * 1024 * 1024


def authenticated_large_client(settings: Settings) -> TestClient:
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/session",
        json={
            "access_token": settings.auth_token,
            "device_id": "large-test",
            "device_name": "Large test",
        },
    )
    assert response.status_code == 200
    return client


def create_upload_for_path(
    client: TestClient, path: Path, chunk_size: int
) -> dict[str, object]:
    response = client.post(
        "/api/uploads",
        json={
            "client_request_id": "large-upload-request",
            "name": path.name,
            "size_bytes": path.stat().st_size,
            "mime_type": "application/octet-stream",
            "last_modified_ms": int(path.stat().st_mtime * 1000),
            "chunk_size_bytes": chunk_size,
            "sample_sha256": sha256(path.name.encode()).hexdigest(),
        },
    )
    assert response.status_code == 200
    return response.json()


def put_large_part(
    client: TestClient,
    upload: dict[str, object],
    part_index: int,
    chunk: bytes,
) -> None:
    chunk_size = int(upload["chunk_size_bytes"])
    start = part_index * chunk_size
    end = start + len(chunk) - 1
    response = client.put(
        f"/api/uploads/{upload['upload_id']}/parts/{part_index}",
        content=chunk,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Range": f"bytes {start}-{end}/{upload['size_bytes']}",
            "X-Chunk-SHA256": sha256(chunk).hexdigest(),
        },
    )
    assert response.status_code == 200


@pytest.mark.large
def test_sparse_512mb_upload_completes_with_server_sha256(
    settings: Settings, tmp_path: Path
) -> None:
    size = 512 * 1024 * 1024
    source = tmp_path / "sparse-512mb.bin"
    with source.open("wb") as output:
        output.seek(size - 1)
        output.write(b"\0")

    expected = sha256()
    large_settings = replace(
        settings,
        max_upload_size=size,
        allowed_extensions={".bin"},
    )
    client = authenticated_large_client(large_settings)
    with client:
        upload = create_upload_for_path(client, source, chunk_size=CHUNK_SIZE)
        tracemalloc.start()
        with source.open("rb") as input_file:
            for part_index, chunk in enumerate(
                iter(lambda: input_file.read(CHUNK_SIZE), b"")
            ):
                expected.update(chunk)
                put_large_part(client, upload, part_index, chunk)
                gc.collect()
        response = client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    assert response.status_code == 200
    assert response.json()["file"]["sha256"] == expected.hexdigest()
    assert peak < 40 * 1024 * 1024
