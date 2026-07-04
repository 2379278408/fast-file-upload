from __future__ import annotations

from pathlib import Path
import os
import time
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.storage import sanitize_filename


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        upload_dir=tmp_path / "uploads",
        max_upload_size=2 * 1024,
        allowed_extensions={".txt", ".md"},
        allowed_origins=["*"],
        auth_token=None,
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
    )
    return TestClient(create_app(settings))


def test_health_reports_stats(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["file_count"] == 0
    assert "upload_dir" not in payload


def test_security_headers_are_set(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_index_contains_frontend_interaction_contract(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    required_markup = [
        'role="dialog"',
        'aria-modal="true"',
        'aria-labelledby="previewTitle"',
        'id="opsSummary"',
        'id="selectedCount"',
        '选择当前结果',
        '复制所选链接',
        '清空选择',
        '刷新运营视图',
        'Drop files here',
        '支持批量、进度、失败重试。',
        '<h2>Library</h2>',
        '筛选、复制、下载与预览。',
        '存储占用、旧文件与审计事件。',
    ]
    required_hooks = [
        'trapPreviewFocus',
        'loadOperations',
        'toggleFileSelection',
        'copySelectedLinks',
        'retryUpload',
        'queue-error',
        'window.retryUpload',
        'const extensions = Array.from',
        'extensions.includes(current)',
        'function jsString',
        "deleteFile('${file.id}', ${jsString(file.name)})",
    ]

    for item in required_markup + required_hooks:
        assert item in html
    assert 'id="sortSelect"' not in html
    assert 'sortSelect.value' not in html


def test_upload_list_download_and_delete_flow(client: TestClient) -> None:
    upload = client.post("/api/upload", files={"file": ("report.txt", b"hello world", "text/plain")})
    assert upload.status_code == 200
    upload_payload = upload.json()
    file_id = upload_payload["id"]
    assert upload_payload["media_kind"] == "document"
    assert upload_payload["is_previewable"] is False
    assert upload_payload["sha256"] == sha256(b"hello world").hexdigest()

    listing = client.get("/api/files")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "report.txt"
    assert items[0]["media_kind"] == "document"
    assert items[0]["is_previewable"] is False
    assert items[0]["sha256"] == sha256(b"hello world").hexdigest()

    download = client.get(f"/download/{file_id}")
    assert download.status_code == 200
    assert download.content == b"hello world"

    deleted = client.delete(f"/api/files/{file_id}")
    assert deleted.status_code == 200
    assert client.get("/api/files").json()["items"] == []


def test_extension_allowlist_is_enforced(client: TestClient) -> None:
    response = client.post("/api/upload", files={"file": ("photo.png", b"x", "image/png")})
    assert response.status_code == 400
    assert "File type not allowed" in response.json()["detail"]


def test_size_limit_is_enforced(client: TestClient) -> None:
    payload = b"x" * 4096
    response = client.post("/api/upload", files={"file": ("large.txt", payload, "text/plain")})
    assert response.status_code == 413


def test_empty_files_are_rejected(client: TestClient) -> None:
    response = client.post("/api/upload", files={"file": ("empty.txt", b"", "text/plain")})
    assert response.status_code == 400
    assert response.json()["detail"] == "Empty files are not allowed"


def test_sanitized_filename_has_safe_length() -> None:
    long_name = f"{'a' * 260}.txt"
    safe_name = sanitize_filename(long_name)
    assert safe_name.endswith(".txt")
    assert len(safe_name) <= 120


def test_token_protection_for_protected_operations(tmp_path: Path) -> None:
    settings = Settings(
        upload_dir=tmp_path / "protected-uploads",
        max_upload_size=2048,
        allowed_extensions={".txt", ".svg"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
    )
    client = TestClient(create_app(settings))

    unauthorized = client.post("/api/upload", files={"file": ("note.txt", b"ok", "text/plain")})
    assert unauthorized.status_code == 401

    assert client.get("/api/files").status_code == 401

    authorized = client.post(
        "/api/upload",
        files={"file": ("note.txt", b"ok", "text/plain")},
        headers={"X-Upload-Token": "secret-token"},
    )
    assert authorized.status_code == 200
    file_id = authorized.json()["id"]

    listing = client.get("/api/files", headers={"X-Upload-Token": "secret-token"})
    assert listing.status_code == 200
    assert listing.json()["items"][0]["id"] == file_id

    assert client.get(f"/download/{file_id}").status_code == 401
    assert client.get(f"/download/{file_id}?token=secret-token").status_code == 200

    deleted = client.delete(f"/api/files/{file_id}", headers={"X-Upload-Token": "secret-token"})
    assert deleted.status_code == 200


def test_rejects_malformed_file_ids(client: TestClient) -> None:
    response = client.get("/download/not-a-valid-id")
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid file id"


def test_previewable_images_and_static_assets(tmp_path: Path) -> None:
    settings = Settings(
        upload_dir=tmp_path / "preview-uploads",
        max_upload_size=2 * 1024,
        allowed_extensions={".png", ".txt"},
        allowed_origins=["*"],
        auth_token=None,
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
    )
    client = TestClient(create_app(settings))

    upload = client.post("/api/upload", files={"file": ("cover.png", b"png-bytes", "image/png")})
    assert upload.status_code == 200
    payload = upload.json()
    assert payload["media_kind"] == "image"
    assert payload["is_previewable"] is True

    listing = client.get("/api/files")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["media_kind"] == "image"

    demo = client.get("/design-demo.html")
    assert demo.status_code == 200
    assert "front-end redesign study" in demo.text

    health = client.get("/api/health")
    assert health.status_code == 200


def test_svg_uploads_are_download_only(tmp_path: Path) -> None:
    settings = Settings(
        upload_dir=tmp_path / "svg-uploads",
        max_upload_size=2 * 1024,
        allowed_extensions={".svg"},
        allowed_origins=["*"],
        auth_token=None,
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
    )
    client = TestClient(create_app(settings))

    upload = client.post("/api/upload", files={"file": ("icon.svg", b"<svg></svg>", "image/svg+xml")})
    assert upload.status_code == 200
    payload = upload.json()
    assert payload["media_kind"] == "document"
    assert payload["is_previewable"] is False


def test_invalid_storage_entries_are_ignored(tmp_path: Path) -> None:
    upload_dir = tmp_path / "mixed-uploads"
    upload_dir.mkdir()
    (upload_dir / "not-a-valid-entry.txt").write_text("bad", encoding="utf-8")
    (upload_dir / "abcdef123456_report.txt").write_text("ok", encoding="utf-8")
    settings = Settings(
        upload_dir=upload_dir,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt"},
        allowed_origins=["*"],
        auth_token=None,
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
    )
    client = TestClient(create_app(settings))

    items = client.get("/api/files").json()["items"]
    assert [item["id"] for item in items] == ["abcdef123456"]


def test_upload_rate_limit_is_enforced(tmp_path: Path) -> None:
    settings = Settings(
        upload_dir=tmp_path / "limited-uploads",
        max_upload_size=2 * 1024,
        allowed_extensions={".txt"},
        allowed_origins=["*"],
        auth_token=None,
        rate_limit_count=2,
        rate_limit_window_seconds=60,
        retention_days=0,
    )
    client = TestClient(create_app(settings))

    assert client.post("/api/upload", files={"file": ("one.txt", b"1", "text/plain")}).status_code == 200
    assert client.post("/api/upload", files={"file": ("two.txt", b"2", "text/plain")}).status_code == 200
    limited = client.post("/api/upload", files={"file": ("three.txt", b"3", "text/plain")})
    assert limited.status_code == 429
    assert limited.json()["detail"] == "Too many requests"


def test_retention_cleanup_removes_expired_files(tmp_path: Path) -> None:
    upload_dir = tmp_path / "retained-uploads"
    upload_dir.mkdir()
    expired = upload_dir / "abcdef123456_old.txt"
    current = upload_dir / "abcdef123457_current.txt"
    expired.write_text("old", encoding="utf-8")
    current.write_text("current", encoding="utf-8")
    old_time = time.time() - (3 * 24 * 60 * 60)
    os.utime(expired, (old_time, old_time))
    settings = Settings(
        upload_dir=upload_dir,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt"},
        allowed_origins=["*"],
        auth_token=None,
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=1,
    )
    client = TestClient(create_app(settings))

    items = client.get("/api/files").json()["items"]
    assert [item["id"] for item in items] == ["abcdef123457"]
    assert not expired.exists()


def test_audit_log_records_upload_and_delete(client: TestClient) -> None:
    upload = client.post("/api/upload", files={"file": ("audit.txt", b"audit", "text/plain")})
    file_id = upload.json()["id"]
    assert client.delete(f"/api/files/{file_id}").status_code == 200

    audit = client.get("/api/audit")
    assert audit.status_code == 200
    events = audit.json()["events"]
    assert [event["action"] for event in events[-2:]] == ["upload", "delete"]
    assert events[-2]["file_id"] == file_id
    assert events[-2]["name"] == "audit.txt"
    assert events[-1]["file_id"] == file_id


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
        max_upload_size=2 * 1024,
        allowed_extensions={".txt"},
        allowed_origins=["*"],
        auth_token=None,
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
    )
    client = TestClient(create_app(settings))

    summary = client.get("/api/admin/summary").json()
    assert summary["stale_file_count"] == 1
    assert summary["large_file_count"] == 1
    assert summary["largest_files"][0]["name"] == "large.txt"
    assert summary["total_bytes"] >= 1539
