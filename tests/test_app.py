from __future__ import annotations

import asyncio
import json
from pathlib import Path
import os
import subprocess
import time
from threading import Event
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from app.auth import SessionData
from app.config import Settings
from app.main import create_app
from app.storage import sanitize_filename


def test_readme_documents_required_runtime_and_test_contracts() -> None:
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")

    for expected in (
        "UPLOAD_TOKEN",
        "30 天",
        "30 秒",
        "WebSocket",
        "/api/session",
        "/api/messages",
        "/api/events",
        "purge",
        "recovery",
        "requirements-test.txt",
        "pytest",
        "RETENTION_DAYS",
        "LOGIN_RATE_LIMIT_MAX_CLIENTS",
        "CLIENT_REQUEST_LOCK_CAPACITY",
        "DELETE /api/session",
        "无有效会话时幂等清理 cookie",
        "静态资源、`POST /api/session` 和 `DELETE /api/session`",
    ):
        assert expected in readme
    assert "可选访问令牌" not in readme
    assert "除静态页面和 `POST /api/session` 外" not in readme


def test_gitignore_covers_runtime_artifacts_and_preserves_placeholders() -> None:
    root = Path(__file__).resolve().parent.parent
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()

    for pattern in (
        "timeline.sqlite3",
        "uploads/*",
        ".worktree-runtime/cache/",
        ".worktree-runtime/",
    ):
        assert pattern in ignored
    if (root / "uploads" / ".gitkeep").exists():
        assert "!uploads/.gitkeep" in ignored

    runtime_cache = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", ".worktree-runtime/cache/item.bin"],
        cwd=root,
        check=False,
    )
    source_cache = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", "src/cache/module.py"],
        cwd=root,
        check=False,
    )
    assert runtime_cache.returncode == 0
    assert source_cache.returncode == 1
    for trackable_zip in (
        "fixtures/transfer-fixture.zip",
        "docs/example.zip",
        "docs/example.zip.tmp",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", trackable_zip],
            cwd=root,
            check=False,
        )
        assert result.returncode == 1, trackable_zip


def authenticate(client: TestClient, token: str = "secret-token") -> None:
    response = client.post(
        "/api/session",
        json={
            "access_token": token,
            "device_id": "test-device",
            "device_name": "Test device",
        },
    )
    assert response.status_code == 200


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        upload_dir=tmp_path / "uploads",
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt", ".md"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
        upload_chunk_size_bytes=1024,
        event_retention_limit=100,
    )
    client = TestClient(create_app(settings))
    authenticate(client)
    return client


def test_health_reports_stats(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["file_count"] == 0
    assert "upload_dir" not in payload


@pytest.mark.parametrize(
    ("path", "method_name"),
    [("/api/health", "stats"), ("/api/admin/summary", "admin_summary")],
)
def test_storage_scans_and_hashing_run_off_event_loop(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    method_name: str,
) -> None:
    entered = Event()
    release = Event()
    storage = client.app.state.storage
    original = getattr(storage, method_name)

    def blocked_storage_call():
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        entered.set()
        assert release.wait(timeout=2)
        return original()

    monkeypatch.setattr(storage, method_name, blocked_storage_call)
    endpoint = next(
        route.endpoint
        for route in client.app.routes
        if getattr(route, "path", None) == path
    )
    session = SessionData(
        device_id="offload-test", device_name="Offload test", expires_at=2**31
    )

    async def scenario() -> None:
        task = asyncio.create_task(endpoint(session))
        assert await asyncio.to_thread(entered.wait, 1)
        ticked = False

        async def tick() -> None:
            nonlocal ticked
            await asyncio.sleep(0.01)
            ticked = True

        await asyncio.wait_for(tick(), timeout=0.2)
        assert ticked is True
        release.set()
        await task

    asyncio.run(scenario())


def test_security_headers_are_set(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_index_serves_html(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "<!DOCTYPE html>" in html
    assert "MonkeyCode" in html
    assert 'id="dropzone"' in html
    assert 'id="fileList"' in html
    assert 'id="previewModal"' in html


def test_upload_list_download_and_delete_flow(client: TestClient) -> None:
    upload = client.post(
        "/api/upload",
        data={"client_request_id": "app-flow-1"},
        files={"file": ("report.txt", b"hello world", "text/plain")},
    )
    assert upload.status_code == 200
    upload_payload = upload.json()["file"]
    file_id = upload_payload["id"]
    assert upload_payload["media_kind"] == "document"
    assert upload_payload["is_previewable"] is False
    assert upload_payload["sha256"] == sha256(b"hello world").hexdigest()
    assert upload_payload["download_url"] == f"/download/{file_id}"
    assert "token=" not in upload_payload["download_url"]

    listing = client.get("/api/files")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["file"]["name"] == "report.txt"
    assert items[0]["file"]["media_kind"] == "document"
    assert items[0]["file"]["is_previewable"] is False
    assert items[0]["file"]["sha256"] == sha256(b"hello world").hexdigest()
    assert "token=" not in items[0]["file"]["download_url"]

    download = client.get(f"/download/{file_id}")
    assert download.status_code == 200
    assert download.content == b"hello world"

    deleted = client.delete(f"/api/files/{file_id}")
    assert deleted.status_code == 200
    assert client.get("/api/files").json()["items"] == []


def test_extension_allowlist_is_enforced(client: TestClient) -> None:
    response = client.post(
        "/api/upload",
        data={"client_request_id": "ext-1"},
        files={"file": ("photo.png", b"x", "image/png")},
    )
    assert response.status_code == 400
    assert "File type not allowed" in response.json()["detail"]


def test_size_limit_is_enforced(client: TestClient) -> None:
    payload = b"x" * 4096
    response = client.post(
        "/api/upload",
        data={"client_request_id": "size-1"},
        files={"file": ("large.txt", payload, "text/plain")},
    )
    assert response.status_code == 413


def test_empty_files_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/upload",
        data={"client_request_id": "empty-1"},
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Empty files are not allowed"


def test_sanitized_filename_has_safe_length() -> None:
    long_name = f"{'a' * 260}.txt"
    safe_name = sanitize_filename(long_name)
    assert safe_name.endswith(".txt")
    assert len(safe_name) <= 120


def test_cookie_session_protects_operations(tmp_path: Path) -> None:
    settings = Settings(
        upload_dir=tmp_path / "protected-uploads",
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2048,
        allowed_extensions={".txt", ".svg"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
        upload_chunk_size_bytes=1024,
        event_retention_limit=100,
    )
    client = TestClient(create_app(settings))

    unauthorized = client.post("/api/upload", files={"file": ("note.txt", b"ok", "text/plain")})
    assert unauthorized.status_code == 401

    assert client.get("/api/files").status_code == 401

    session = client.post(
        "/api/session",
        json={
            "access_token": "secret-token",
            "device_id": "browser-01",
            "device_name": "Work computer",
        },
    )
    assert session.status_code == 200

    authorized = client.post(
        "/api/upload",
        data={"client_request_id": "protected-1"},
        files={"file": ("note.txt", b"ok", "text/plain")},
    )
    assert authorized.status_code == 200
    file_id = authorized.json()["file"]["id"]

    listing = client.get("/api/files")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["file"]["id"] == file_id

    download_url = f"/download/{file_id}"
    assert "token=" not in download_url
    assert client.get(download_url).status_code == 200

    deleted = client.delete(f"/api/files/{file_id}")
    assert deleted.status_code == 200


def test_rejects_malformed_file_ids(client: TestClient) -> None:
    response = client.get("/download/not-a-valid-id")
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid file id"


def test_previewable_images_and_static_assets(tmp_path: Path) -> None:
    settings = Settings(
        upload_dir=tmp_path / "preview-uploads",
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".png", ".txt"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
        upload_chunk_size_bytes=1024,
        event_retention_limit=100,
    )
    client = TestClient(create_app(settings))
    authenticate(client)

    upload = client.post(
        "/api/upload",
        data={"client_request_id": "preview-1"},
        files={"file": ("cover.png", b"png-bytes", "image/png")},
    )
    assert upload.status_code == 200
    payload = upload.json()["file"]
    assert payload["media_kind"] == "image"
    assert payload["is_previewable"] is True

    listing = client.get("/api/files")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["file"]["media_kind"] == "image"

    missing_asset = client.get("/design-demo.html")
    assert missing_asset.status_code == 404

    health = client.get("/api/health")
    assert health.status_code == 200


def test_svg_uploads_are_download_only(tmp_path: Path) -> None:
    settings = Settings(
        upload_dir=tmp_path / "svg-uploads",
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".svg"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
        upload_chunk_size_bytes=1024,
        event_retention_limit=100,
    )
    client = TestClient(create_app(settings))
    authenticate(client)

    upload = client.post(
        "/api/upload",
        data={"client_request_id": "svg-only-1"},
        files={"file": ("icon.svg", b"<svg></svg>", "image/svg+xml")},
    )
    assert upload.status_code == 200
    payload = upload.json()["file"]
    assert payload["media_kind"] == "document"
    assert payload["is_previewable"] is False


def test_invalid_storage_entries_are_ignored(tmp_path: Path) -> None:
    upload_dir = tmp_path / "mixed-uploads"
    upload_dir.mkdir()
    (upload_dir / "not-a-valid-entry.txt").write_text("bad", encoding="utf-8")
    (upload_dir / "abcdef123456_report.txt").write_text("ok", encoding="utf-8")
    settings = Settings(
        upload_dir=upload_dir,
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
        upload_chunk_size_bytes=1024,
        event_retention_limit=100,
    )
    with TestClient(create_app(settings)) as client:
        authenticate(client)
        items = client.get("/api/files").json()["items"]
        assert len(items) == 1
        assert items[0]["file"]["id"] == "abcdef123456"


def test_upload_rate_limit_is_enforced(tmp_path: Path) -> None:
    settings = Settings(
        upload_dir=tmp_path / "limited-uploads",
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=2,
        rate_limit_window_seconds=60,
        retention_days=0,
        upload_chunk_size_bytes=1024,
        event_retention_limit=100,
    )
    client = TestClient(create_app(settings))
    authenticate(client)

    assert client.post(
        "/api/upload",
        data={"client_request_id": "rate-1"},
        files={"file": ("one.txt", b"1", "text/plain")},
    ).status_code == 200
    assert client.post(
        "/api/upload",
        data={"client_request_id": "rate-2"},
        files={"file": ("two.txt", b"2", "text/plain")},
    ).status_code == 200
    limited = client.post(
        "/api/upload",
        data={"client_request_id": "rate-3"},
        files={"file": ("three.txt", b"3", "text/plain")},
    )
    assert limited.status_code == 429
    assert limited.json()["detail"] == "Too many requests"


def test_health_does_not_prune_expired_files(tmp_path: Path) -> None:
    upload_dir = tmp_path / "health-uploads"
    upload_dir.mkdir()
    expired = upload_dir / "abcdef123456_old.txt"
    expired.write_text("old", encoding="utf-8")
    old_time = time.time() - (3 * 24 * 60 * 60)
    os.utime(expired, (old_time, old_time))
    settings = Settings(
        upload_dir=upload_dir,
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=1,
        upload_chunk_size_bytes=1024,
        event_retention_limit=100,
    )
    client = TestClient(create_app(settings))
    authenticate(client)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert expired.exists()


def test_page_shell_and_static_fallback_are_public_when_api_is_protected(
    tmp_path: Path,
) -> None:
    settings = Settings(
        upload_dir=tmp_path / "uploads",
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
        upload_chunk_size_bytes=1024,
        event_retention_limit=100,
    )
    client = TestClient(create_app(settings))

    assert client.get("/").status_code == 200
    assert client.get("/missing.css").status_code == 404
    assert client.get("/api/messages").status_code == 401
    assert client.post("/api/messages", json={"body": "locked"}).status_code == 401
    assert client.get("/download/abcdef123456").status_code == 401


def test_audit_log_records_upload_and_delete(client: TestClient) -> None:
    upload = client.post(
        "/api/upload",
        data={"client_request_id": "audit-1"},
        files={"file": ("audit.txt", b"audit", "text/plain")},
    )
    file_id = upload.json()["file"]["id"]
    assert client.delete(f"/api/files/{file_id}").status_code == 200

    audit = client.get("/api/audit")
    assert audit.status_code == 200
    events = audit.json()["events"]
    assert [event["action"] for event in events[-2:]] == ["upload", "delete"]
    assert events[-2]["file_id"] == file_id
    assert events[-2]["name"] == "audit.txt"
    assert events[-1]["file_id"] == file_id


def test_audit_reads_only_tail_without_path_read_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = client.app.state.settings
    audit_path = settings.upload_dir / ".audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        "".join(
            json.dumps({"action": f"event-{index}"}) + "\n"
            for index in range(250)
        ),
        encoding="utf-8",
    )
    original_read_text = Path.read_text

    def reject_audit_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == audit_path:
            raise AssertionError("audit tail must be read incrementally")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_audit_read_text)

    response = client.get("/api/audit")

    assert response.status_code == 200
    events = response.json()["events"]
    assert len(events) == 200
    assert events[0]["action"] == "event-50"
    assert events[-1]["action"] == "event-249"


def test_admin_summary_reports_large_and_stale_files(tmp_path: Path) -> None:
    upload_dir = tmp_path / "summary-uploads"
    upload_dir.mkdir()
    stale = upload_dir / "abcdef123456_stale.txt"
    large = upload_dir / "abcdef123457_large.txt"
    stale.write_text("old", encoding="utf-8")
    large.write_text("x" * 1536, encoding="utf-8")
    old_time = time.time() - (40 * 24 * 60 * 60)
    os.utime(stale, (old_time, old_time))
    settings = Settings(
        upload_dir=upload_dir,
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
        upload_chunk_size_bytes=1024,
        event_retention_limit=100,
    )
    client = TestClient(create_app(settings))
    authenticate(client)

    summary = client.get("/api/admin/summary").json()
    assert summary["stale_file_count"] == 1
    assert summary["large_file_count"] == 1
    assert summary["largest_files"][0]["name"] == "large.txt"
    assert summary["total_bytes"] >= 1539


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/api/messages", {"body": "hello", "client_request_id": "bad id"}),
        ("post", "/api/messages", {"body": "hello", "client_request_id": "x" * 129}),
        ("delete", "/api/messages/not-a-message-id", None),
        ("post", "/api/messages/not-a-message-id/restore", None),
        ("get", "/api/messages?before=not-a-message-id", None),
        ("get", "/api/search?q=test&cursor=not-a-message-id", None),
    ],
)
def test_public_identifiers_have_length_and_format_boundaries(
    client: TestClient, method: str, path: str, payload: dict[str, str] | None
) -> None:
    response = client.request(method, path, json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"message_ids": []},
        {"message_ids": ["not-a-message-id"]},
        {"message_ids": [f"{'0' * 13}{index:032x}" for index in range(51)]},
    ],
)
def test_batch_message_ids_are_non_empty_bounded_and_well_formed(
    client: TestClient, payload: dict[str, list[str]]
) -> None:
    assert client.post("/api/messages/batch-delete", json=payload).status_code == 422
    assert client.post("/api/files/batch-download", json=payload).status_code == 422


@pytest.mark.parametrize(
    "query",
    [
        "type=archive",
        "from=2026-02-30",
        "from=17-07-2026",
        "to=2026-13-01",
        "from=2026-07-18&to=2026-07-17",
        "device_id=contains%20spaces",
    ],
)
def test_file_filters_are_strictly_validated(client: TestClient, query: str) -> None:
    assert client.get(f"/api/files?{query}").status_code == 422
