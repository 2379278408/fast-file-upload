from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.auth import SessionData
from app.events import EventHub
from app.main import create_app


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


def create_text(client: TestClient, body: str, request_id: str) -> dict[str, object]:
    response = client.post(
        "/api/messages",
        json={"body": body, "client_request_id": request_id},
    )
    assert response.status_code == 200
    return response.json()


def test_unauthenticated_websocket_closed_with_4401(client: TestClient) -> None:
    with pytest.raises(Exception) as exc_info:
        with client.websocket_connect("/api/events") as ws:
            ws.receive_json()
    error = exc_info.value
    assert getattr(error, "code", None) == 4401 or (
        len(getattr(error, "args", ())) > 0 and 4401 in error.args
    )


def test_two_clients_receive_created_event_and_replay(client: TestClient) -> None:
    unlock(client)
    with (
        client.websocket_connect("/api/events?after=0") as first,
        client.websocket_connect("/api/events?after=0") as second,
    ):
        assert first.receive_json()["event_type"] == "ready"
        assert second.receive_json()["event_type"] == "ready"
        created = create_text(client, "同步", "ws-1")
        event_a = first.receive_json()
        event_b = second.receive_json()
        assert event_a == event_b
        assert event_a["event_type"] == "message.created"
        assert event_a["entity_id"] == created["id"]

    with client.websocket_connect(f"/api/events?after={event_a['sequence'] - 1}") as replay:
        assert replay.receive_json()["sequence"] == event_a["sequence"]


def test_events_are_sequenced_and_deduplicated(client: TestClient) -> None:
    unlock(client)
    with client.websocket_connect("/api/events?after=0") as ws:
        assert ws.receive_json()["event_type"] == "ready"

        messages = []
        for i in range(3):
            messages.append(create_text(client, f"msg-{i}", f"req-{i}"))

        events = [ws.receive_json() for _ in range(3)]

    sequences = [e["sequence"] for e in events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)

    pairs = [(e["event_type"], e["entity_id"]) for e in events]
    assert len(set(pairs)) == len(pairs)


def test_delete_and_restore_broadcast_final_state(client: TestClient) -> None:
    unlock(client)
    with client.websocket_connect("/api/events?after=0") as ws:
        assert ws.receive_json()["event_type"] == "ready"

        message = create_text(client, "delete-me", "del-1")
        created_event = ws.receive_json()
        assert created_event["event_type"] == "message.created"

        client.delete(f"/api/messages/{message['id']}")
        deleted_event = ws.receive_json()
        assert deleted_event["event_type"] == "message.deleted"
        assert deleted_event["entity_id"] == message["id"]

        client.post(f"/api/messages/{message['id']}/restore")
        restored_event = ws.receive_json()
        assert restored_event["event_type"] == "message.restored"
        assert restored_event["entity_id"] == message["id"]


def test_ready_event_sends_latest_sequence(client: TestClient) -> None:
    unlock(client)

    with client.websocket_connect("/api/events?after=0") as ws:
        ready = ws.receive_json()
        assert ready["event_type"] == "ready"
        assert ready["sequence"] == 0

    create_text(client, "first", "ready-1")

    with client.websocket_connect("/api/events?after=0") as ws:
        replay = ws.receive_json()
        assert replay["event_type"] == "message.created"
        ready = ws.receive_json()
        assert ready["event_type"] == "ready"
        assert ready["sequence"] >= 1


def test_mutations_broadcast_committed_structured_events_only(client: TestClient) -> None:
    unlock(client)
    broadcasts: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        broadcasts.append(event)

    client.app.state.hub.broadcast = capture
    created = create_text(client, "structured", "structured-1")
    repeated = create_text(client, "structured", "structured-1")

    assert repeated == created
    assert len(broadcasts) == 1
    assert broadcasts[0]["event_type"] == "message.created"
    assert broadcasts[0]["entity_id"] == created["id"]
    assert broadcasts[0]["payload"] == created

    stored = client.app.state.messages.events_after(0)
    assert stored == broadcasts
    assert isinstance(stored[0]["payload"], dict)

    deleted = client.delete(f"/api/messages/{created['id']}")
    repeated_delete = client.delete(f"/api/messages/{created['id']}")
    assert deleted.status_code == repeated_delete.status_code == 200
    assert [event["event_type"] for event in broadcasts] == [
        "message.created",
        "message.deleted",
    ]
    assert broadcasts[-1]["payload"] == deleted.json()


def test_legacy_file_delete_broadcasts_committed_event_when_audit_file_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlock(client)
    uploaded = client.post(
        "/api/upload",
        data={"client_request_id": "legacy-audit-failure"},
        files={"file": ("audit.txt", b"audit", "text/plain")},
    ).json()
    broadcasts: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        broadcasts.append(event)

    client.app.state.hub.broadcast = capture
    original_open = Path.open

    def fail_audit_open(self: Path, *args: object, **kwargs: object):
        if self.name == ".audit.jsonl" and args and args[0] == "a":
            raise OSError("forced audit failure")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_audit_open)
    deleted = client.delete(f"/api/files/{uploaded['file_id']}")

    assert deleted.status_code == 200
    assert deleted.json()["deleted_at"] is not None
    assert [event["event_type"] for event in broadcasts] == ["message.deleted"]


def test_websocket_replays_more_than_single_event_page(client: TestClient) -> None:
    unlock(client)
    with client.app.state.database.transaction() as connection:
        connection.executemany(
            "INSERT INTO events (event_type, entity_id, payload, created_at) "
            "VALUES ('message.created', ?, ?, '2026-07-17T00:00:00+00:00')",
            [
                (f"message-{index}", '{"id":"message-%d"}' % index)
                for index in range(501)
            ],
        )

    with client.websocket_connect("/api/events?after=0") as ws:
        replayed = [ws.receive_json() for _ in range(501)]
        assert replayed[-1]["event_type"] == "message.created"
        ready = ws.receive_json()

    assert [event["sequence"] for event in replayed] == list(range(1, 502))
    assert all(isinstance(event["payload"], dict) for event in replayed)
    assert ready == {"event_type": "ready", "sequence": 501}


def test_live_event_during_replay_is_buffered_before_monotonic_ready(
    client: TestClient,
) -> None:
    unlock(client)
    created = create_text(client, "replay", "replay-race-1")
    hub = client.app.state.hub
    repository = client.app.state.messages
    original_connect = hub.connect
    injected = False

    def connect_with_injection(websocket, after=0):
        connection = original_connect(websocket, after)
        original_send_replay = connection.send_replay

        async def send_replay_and_inject(event):
            nonlocal injected
            await original_send_replay(event)
            if not injected:
                injected = True
                mutation = repository.create_text(
                    "live",
                    "replay-race-2",
                    SessionData(
                        device_id="browser-01",
                        device_name="Work computer",
                        expires_at=2_000_000_000,
                    ),
                )
                await hub.broadcast(mutation["event"])

        connection.send_replay = send_replay_and_inject
        return connection

    hub.connect = connect_with_injection
    with client.websocket_connect("/api/events?after=0") as ws:
        replay = ws.receive_json()
        live = ws.receive_json()
        ready = ws.receive_json()

    assert replay["entity_id"] == created["id"]
    assert [replay["sequence"], live["sequence"], ready["sequence"]] == [1, 2, 2]
    assert [replay["event_type"], live["event_type"], ready["event_type"]] == [
        "message.created",
        "message.created",
        "ready",
    ]


def test_event_hub_serializes_and_orders_reversed_live_broadcasts() -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.active_sends = 0
            self.max_active_sends = 0

        async def send_json(self, event: dict[str, object]) -> None:
            self.active_sends += 1
            self.max_active_sends = max(self.max_active_sends, self.active_sends)
            await asyncio.sleep(0)
            self.sent.append(event)
            self.active_sends -= 1

    async def scenario() -> FakeWebSocket:
        hub = EventHub()
        websocket = FakeWebSocket()
        connection = hub.connect(websocket, after=0)
        await connection.finish_replay(0)
        second = {
            "sequence": 2,
            "event_type": "message.created",
            "entity_id": "second",
            "payload": {},
            "created_at": "2026-07-17T00:00:00+00:00",
        }
        first = {**second, "sequence": 1, "entity_id": "first"}
        await asyncio.gather(hub.broadcast(second), hub.broadcast(first))
        return websocket

    websocket = asyncio.run(scenario())
    assert websocket.max_active_sends == 1
    assert [event["sequence"] for event in websocket.sent] == [0, 1, 2]
    assert websocket.sent[0]["event_type"] == "ready"
