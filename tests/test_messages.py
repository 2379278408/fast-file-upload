from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, Lock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import create_app
from app import repository


@pytest.fixture
def protected_settings(tmp_path: Path) -> Settings:
    return Settings(
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
    )


@pytest.fixture
def client(protected_settings: Settings) -> TestClient:
    return TestClient(create_app(protected_settings))


def unlock(client: TestClient) -> None:
    response = client.post(
        "/api/session",
        json={
            "access_token": "secret-token",
            "device_id": "browser-01",
            "device_name": "Work computer",
        },
    )
    assert response.status_code == 200


def create_text(client: TestClient, number: int) -> dict[str, object]:
    response = client.post(
        "/api/messages",
        json={"body": f"message-{number:02d}", "client_request_id": f"request-{number:02d}"},
    )
    assert response.status_code == 200
    return response.json()


def test_message_and_search_endpoints_require_session(client: TestClient) -> None:
    assert client.post(
        "/api/messages", json={"body": "hello", "client_request_id": "request"}
    ).status_code == 401
    assert client.get("/api/messages").status_code == 401
    assert client.get("/api/search?q=hello").status_code == 401


def test_create_text_rejects_blank_and_over_limit(client: TestClient) -> None:
    unlock(client)
    assert client.post(
        "/api/messages", json={"body": "  ", "client_request_id": "blank"}
    ).status_code == 422
    response = client.post(
        "/api/messages", json={"body": "x" * 10001, "client_request_id": "long"}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "string_too_long"


def test_text_idempotency_and_stable_50_item_pagination(client: TestClient) -> None:
    unlock(client)
    first = client.post(
        "/api/messages",
        json={"body": "https://example.com", "client_request_id": "same"},
    ).json()
    repeated = client.post(
        "/api/messages",
        json={"body": "changed", "client_request_id": "same"},
    ).json()
    assert repeated["id"] == first["id"]

    for number in range(50):
        create_text(client, number)

    first_page = client.get("/api/messages?limit=50").json()
    assert len(first_page["items"]) == 50
    assert first_page["next_before"] is not None
    assert first_page["items"] == sorted(
        first_page["items"], key=lambda item: (item["created_at"], item["id"])
    )

    second_page = client.get(
        "/api/messages", params={"before": first_page["next_before"], "limit": 50}
    ).json()
    assert len(second_page["items"]) == 1
    assert second_page["next_before"] is None
    first_ids = {item["id"] for item in first_page["items"]}
    second_ids = {item["id"] for item in second_page["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert first["id"] in first_ids | second_ids


def test_concurrent_idempotent_requests_create_one_message_and_event(
    protected_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients = [
        TestClient(create_app(protected_settings), raise_server_exceptions=False)
        for _ in range(2)
    ]
    for concurrent_client in clients:
        unlock(concurrent_client)

    write_barrier = Barrier(2)
    call_lock = Lock()
    call_count = 0

    def synchronized_utc_now() -> datetime:
        nonlocal call_count
        with call_lock:
            current_call = call_count
            call_count += 1
        if current_call < 2:
            write_barrier.wait(timeout=5)
        return datetime.now(timezone.utc)

    monkeypatch.setattr(repository, "utc_now", synchronized_utc_now)

    def submit(concurrent_client: TestClient):
        return concurrent_client.post(
            "/api/messages",
            json={"body": "sent once", "client_request_id": "concurrent-request"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit, clients))

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json()["id"] == responses[1].json()["id"]

    db = Database(protected_settings.database_path)
    with db.connect() as connection:
        message_count = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE client_request_id = ?",
            ("concurrent-request",),
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ? AND entity_id = ?",
            ("message.created", responses[0].json()["id"]),
        ).fetchone()[0]

    assert message_count == 1
    assert event_count == 1


def test_searches_body_and_filename_and_escapes_like_wildcards(
    client: TestClient, protected_settings: Settings
) -> None:
    unlock(client)
    body_message = client.post(
        "/api/messages",
        json={"body": "quarterly needle report", "client_request_id": "body-search"},
    ).json()
    literal_wildcard = client.post(
        "/api/messages",
        json={"body": "progress is 100%", "client_request_id": "wildcard-search"},
    ).json()
    client.post(
        "/api/messages",
        json={"body": "progress is 1000", "client_request_id": "wildcard-control"},
    )

    db = Database(protected_settings.database_path)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO files "
            "(id, original_name, storage_name, mime_type, extension, size_bytes, sha256, "
            "created_at, purged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                "file-1",
                "filename-needle.txt",
                "stored-file-1",
                "text/plain",
                ".txt",
                4,
                "sha256",
                "2026-07-17T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO messages "
            "(id, kind, body, file_id, client_request_id, device_id, device_name, "
            "created_at, deleted_at) VALUES (?, 'file', NULL, ?, ?, ?, ?, ?, NULL)",
            (
                "message-file-1",
                "file-1",
                "file-search",
                "browser-01",
                "Work computer",
                "2026-07-17T00:00:00+00:00",
            ),
        )

    body_results = client.get("/api/search", params={"q": "quarterly needle"}).json()
    filename_results = client.get("/api/search", params={"q": "filename-needle"}).json()
    wildcard_results = client.get("/api/search", params={"q": "100%"}).json()

    assert [item["id"] for item in body_results["items"]] == [body_message["id"]]
    assert [item["id"] for item in filename_results["items"]] == ["message-file-1"]
    assert [item["id"] for item in wildcard_results["items"]] == [literal_wildcard["id"]]
    assert filename_results["items"][0]["file"]["original_name"] == "filename-needle.txt"


def _create_clocked_text(client: TestClient, body: str, request_id: str) -> dict[str, object]:
    response = client.post(
        "/api/messages", json={"body": body, "client_request_id": request_id}
    )
    assert response.status_code == 200
    return response.json()


def _event_counts(settings: Settings, message_id: str) -> dict[str, int]:
    with Database(settings.database_path).connect() as connection:
        rows = connection.execute(
            "SELECT event_type, COUNT(*) FROM events WHERE entity_id = ? GROUP BY event_type",
            (message_id,),
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def test_delete_is_soft_idempotent_and_excluded_from_list_and_search(
    clocked_client: TestClient, settings: Settings
) -> None:
    message = _create_clocked_text(clocked_client, "撤销测试", "delete-1")

    deleted = clocked_client.delete(f"/api/messages/{message['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["id"] == message["id"]
    assert deleted.json()["deleted_at"] is not None

    repeated = clocked_client.delete(f"/api/messages/{message['id']}")
    assert repeated.status_code == 200
    assert repeated.json()["deleted_at"] == deleted.json()["deleted_at"]

    assert _event_counts(settings, str(message["id"])) == {
        "message.created": 1,
        "message.deleted": 1,
    }
    assert clocked_client.get("/api/messages").json()["items"] == []
    assert clocked_client.get("/api/search", params={"q": "撤销测试"}).json()["items"] == []


def test_unknown_message_delete_and_restore_return_404(clocked_client: TestClient) -> None:
    unknown_id = "0" * 45
    assert clocked_client.delete(f"/api/messages/{unknown_id}").status_code == 404
    assert clocked_client.post(f"/api/messages/{unknown_id}/restore").status_code == 404


def test_restore_at_zero_and_just_before_thirty_seconds_is_idempotent(
    clocked_client: TestClient, settings: Settings, clock
) -> None:
    message = _create_clocked_text(clocked_client, "恢复测试", "restore-1")

    clocked_client.delete(f"/api/messages/{message['id']}")
    restored = clocked_client.post(f"/api/messages/{message['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None

    repeated = clocked_client.post(f"/api/messages/{message['id']}/restore")
    assert repeated.status_code == 200
    assert repeated.json()["deleted_at"] is None
    assert _event_counts(settings, str(message["id"])) == {
        "message.created": 1,
        "message.deleted": 1,
        "message.restored": 1,
    }

    clocked_client.delete(f"/api/messages/{message['id']}")
    clock.advance(seconds=29.999)
    assert clocked_client.post(f"/api/messages/{message['id']}/restore").status_code == 200
    assert _event_counts(settings, str(message["id"])) == {
        "message.created": 1,
        "message.deleted": 2,
        "message.restored": 2,
    }
    ids = [item["id"] for item in clocked_client.get("/api/messages").json()["items"]]
    assert ids == [message["id"]]


@pytest.mark.parametrize("elapsed", [30, 30.001, 31])
def test_restore_at_or_after_thirty_seconds_returns_409(
    clocked_client: TestClient, settings: Settings, clock, elapsed: float
) -> None:
    message = _create_clocked_text(clocked_client, "过期测试", "restore-expired")

    deleted = clocked_client.delete(f"/api/messages/{message['id']}").json()
    clock.advance(seconds=elapsed)

    response = clocked_client.post(f"/api/messages/{message['id']}/restore")
    assert response.status_code == 409
    assert _event_counts(settings, str(message["id"])) == {
        "message.created": 1,
        "message.deleted": 1,
    }
    with Database(settings.database_path).connect() as connection:
        row = connection.execute(
            "SELECT deleted_at FROM messages WHERE id = ?", (message["id"],)
        ).fetchone()
    assert row["deleted_at"] == deleted["deleted_at"]
    assert clocked_client.get("/api/messages").json()["items"] == []


def test_delete_restore_and_purge_endpoints_require_session(client: TestClient) -> None:
    assert client.delete("/api/messages/some-id").status_code == 401
    assert client.post("/api/messages/some-id/restore").status_code == 401
    assert client.post("/api/maintenance/purge").status_code == 401
