from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from app.database import Database
from app.storage import PendingFile
from app.upload_repository import (
    PartLease,
    PartRecord,
    UploadCapacityExceeded,
    UploadConflict,
    UploadCreate,
    UploadRepository,
    UploadStateConflict,
)


@pytest.fixture
def repository(tmp_path: Path) -> UploadRepository:
    database = Database(tmp_path / "uploads.sqlite3")
    database.initialize()
    return UploadRepository(database)


@pytest.fixture
def upload_command() -> UploadCreate:
    return UploadCreate(
        client_request_id="request-1",
        original_name="notes.txt",
        mime_type="text/plain",
        size_bytes=8,
        last_modified_ms=1_700_000_000_000,
        sample_sha256=sha256(b"sample").hexdigest(),
        chunk_size_bytes=4,
        source_device_id="device-1",
        source_device_name="Work computer",
    )


def part(session: dict[str, object], index: int, body: bytes, now: datetime) -> PartRecord:
    start = index * 4
    return PartRecord(
        str(session["upload_id"]), index, start, start + len(body) - 1,
        len(body), sha256(body).hexdigest(), now.isoformat(),
    )


def confirm(repository: UploadRepository, session: dict[str, object], index: int, body: bytes, now: datetime) -> dict[str, object]:
    record = part(session, index, body, now)
    lease = repository.begin_part(
        record.upload_id, record.part_index, record.start_byte, record.end_byte,
        record.size_bytes, record.sha256,
    )
    assert isinstance(lease, PartLease)
    return repository.confirm_part(lease, record, now, 86_400)


def test_create_payload_id_and_metadata_replay(repository, upload_command, clock) -> None:
    session, created = repository.create_or_get(upload_command, clock(), 86_400, 128)
    replay, replay_created = repository.create_or_get(upload_command, clock(), 86_400, 128)
    assert created is True
    assert replay_created is False
    assert replay == session
    assert re.fullmatch(r"[0-9a-f]{32}", str(session["upload_id"]))
    assert "id" not in session
    assert set(session) == {
        "upload_id", "client_request_id", "source_device_id", "original_name",
        "source_device_name", "mime_type", "size_bytes", "last_modified_ms", "sample_sha256",
        "chunk_size_bytes", "status", "confirmed_parts", "confirmed_bytes",
        "file_sha256", "message_id", "error_code", "publication_state",
        "created_at", "updated_at", "expires_at",
    }


def test_create_metadata_and_cross_table_conflicts(repository, upload_command, clock) -> None:
    repository.create_or_get(upload_command, clock(), 86_400, 128)
    with pytest.raises(UploadConflict):
        repository.create_or_get(replace(upload_command, size_bytes=9), clock(), 86_400, 128)

    for table, values in (
        ("messages", ("message-1", "text", "x", "request-message", "d", "D", clock().isoformat())),
        ("upload_reservations", ("request-legacy", "file-1", "x.txt", "file-1_x.txt", "text/plain", ".txt", 1, "a", clock().isoformat())),
    ):
        command = replace(upload_command, client_request_id=values[3] if table == "messages" else values[0])
        with repository.db.connect() as connection:
            if table == "messages":
                connection.execute("INSERT INTO messages (id, kind, body, client_request_id, device_id, device_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", values)
            else:
                connection.execute("INSERT INTO upload_reservations (client_request_id, file_id, original_name, storage_name, mime_type, extension, size_bytes, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
            connection.commit()
        with pytest.raises(UploadConflict):
            repository.create_or_get(command, clock(), 86_400, 128)


def test_first_confirmed_part_transitions_queued_to_uploading(repository, upload_command, clock) -> None:
    session, created = repository.create_or_get(upload_command, clock(), 86_400, 128)
    confirmed = confirm(repository, session, 0, b"data", clock())
    assert created is True
    assert confirmed["status"] == "uploading"
    assert confirmed["confirmed_bytes"] == 4


def test_part_replay_is_idempotent_and_conflict_is_rejected(repository, upload_command, clock) -> None:
    session, _ = repository.create_or_get(upload_command, clock(), 86_400, 128)
    confirmed = confirm(repository, session, 0, b"data", clock())
    expiry = confirmed["expires_at"]
    clock.advance(seconds=60)
    record = part(session, 0, b"data", clock())
    replay = repository.begin_part(record.upload_id, 0, 0, 3, 4, record.sha256)
    assert isinstance(replay, dict)
    assert replay["expires_at"] == expiry
    with pytest.raises(UploadConflict):
        repository.begin_part(record.upload_id, 0, 0, 3, 4, sha256(b"nope").hexdigest())


@pytest.mark.parametrize(
    "field,value",
    (("start_byte", 1), ("end_byte", 4), ("size_bytes", 3), ("sha256", "0" * 64)),
)
def test_confirm_rejects_lease_record_mismatch(repository, upload_command, clock, field: str, value: object) -> None:
    session, _ = repository.create_or_get(upload_command, clock(), 86_400, 128)
    record = part(session, 0, b"data", clock())
    lease = repository.begin_part(record.upload_id, 0, 0, 3, 4, record.sha256)
    assert isinstance(lease, PartLease)
    with pytest.raises(UploadConflict):
        repository.confirm_part(lease, replace(record, **{field: value}), clock(), 86_400)


def test_confirm_rejects_directly_constructed_out_of_range_lease(repository, upload_command, clock) -> None:
    session, _ = repository.create_or_get(upload_command, clock(), 86_400, 128)
    digest = sha256(b"data").hexdigest()
    lease = PartLease(str(session["upload_id"]), 0, 8, 11, 4, digest)
    record = PartRecord(str(session["upload_id"]), 0, 8, 11, 4, digest, clock().isoformat())
    with pytest.raises(UploadConflict):
        repository.confirm_part(lease, record, clock(), 86_400)


def test_pause_allows_started_part_to_confirm_and_preserves_paused(repository, upload_command, clock) -> None:
    session, _ = repository.create_or_get(upload_command, clock(), 86_400, 128)
    record = part(session, 0, b"data", clock())
    lease = repository.begin_part(record.upload_id, 0, 0, 3, 4, record.sha256)
    assert isinstance(lease, PartLease)
    paused = repository.transition(record.upload_id, "pause", upload_command.source_device_id, clock(), 86_400)
    confirmed = repository.confirm_part(lease, record, clock(), 86_400)
    assert paused["status"] == "paused"
    assert confirmed["status"] == "paused"
    assert confirmed["confirmed_bytes"] == 4


def test_control_ownership_cancel_race_and_expiry(repository, upload_command, clock) -> None:
    session, _ = repository.create_or_get(upload_command, clock(), 86_400, 128)
    original_expiry = session["expires_at"]
    clock.advance(seconds=60)
    assert repository.get(str(session["upload_id"]))["expires_at"] == original_expiry
    with pytest.raises(UploadConflict):
        repository.transition(str(session["upload_id"]), "pause", "observer", clock(), 86_400)
    paused = repository.transition(str(session["upload_id"]), "pause", upload_command.source_device_id, clock(), 86_400)
    assert paused["expires_at"] > original_expiry
    cancelled, changed = repository.cancel(str(session["upload_id"]), clock(), 86_400)
    replay, replay_changed = repository.cancel(str(session["upload_id"]), clock(), 86_400)
    assert (cancelled["status"], changed) == ("cancelled", True)
    assert replay_changed is False
    assert replay["expires_at"] == cancelled["expires_at"]
    with pytest.raises(UploadStateConflict):
        repository.transition(str(session["upload_id"]), "resume", upload_command.source_device_id, clock(), 86_400)


def test_completion_requires_contiguous_coverage_and_complete_cannot_cancel(repository, upload_command, clock) -> None:
    session, _ = repository.create_or_get(upload_command, clock(), 86_400, 128)
    confirm(repository, session, 1, b"more", clock())
    with pytest.raises(UploadStateConflict):
        repository.begin_completion(str(session["upload_id"]), clock(), 86_400)
    confirm(repository, session, 0, b"data", clock())
    completing = repository.begin_completion(str(session["upload_id"]), clock(), 86_400)
    assert completing["status"] == "verifying"
    assert completing["publication_state"] == "assembling"


def test_paused_upload_cannot_begin_completion(repository, upload_command, clock) -> None:
    session, _ = repository.create_or_get(upload_command, clock(), 86_400, 128)
    confirm(repository, session, 0, b"data", clock())
    confirm(repository, session, 1, b"more", clock())
    repository.transition(
        str(session["upload_id"]), "pause", upload_command.source_device_id,
        clock(), 86_400,
    )
    with pytest.raises(UploadStateConflict):
        repository.begin_completion(str(session["upload_id"]), clock(), 86_400)


def test_capacity_counts_only_active_sessions(repository, upload_command, clock) -> None:
    repository.create_or_get(upload_command, clock(), 86_400, 1)
    with pytest.raises(UploadCapacityExceeded):
        repository.create_or_get(replace(upload_command, client_request_id="request-2"), clock(), 86_400, 1)


def test_finalize_publication_uses_durable_device_data(repository, upload_command, clock, tmp_path: Path) -> None:
    upload_command = replace(upload_command, original_name="notes?.txt")
    session, _ = repository.create_or_get(upload_command, clock(), 86_400, 128)
    confirm(repository, session, 0, b"data", clock())
    confirm(repository, session, 1, b"more", clock())
    repository.begin_completion(str(session["upload_id"]), clock(), 86_400)
    digest = sha256(b"datamore").hexdigest()
    repository.set_publication_state(str(session["upload_id"]), "assembled", digest, clock(), 86_400)
    repository.set_publication_state(str(session["upload_id"]), "file_published", digest, clock(), 86_400)
    pending = PendingFile("file-1", "notes.txt", "file-1_notes.txt", tmp_path / "part", tmp_path / "final", "text/plain", ".txt", 8, digest)
    result = repository.finalize_publication(str(session["upload_id"]), pending, clock())
    assert result["changed"] is True
    assert [event["event_type"] for event in result["events"]] == ["upload.completed", "message.created", "file.finalized"]
    assert result["result"]["status"] == "complete"
    replay = repository.finalize_publication(str(session["upload_id"]), pending, clock())
    assert replay == {"result": result["result"], "events": [], "changed": False}
    with repository.db.connect() as connection:
        message = connection.execute("SELECT device_name FROM messages WHERE id = ?", (result["result"]["message_id"],)).fetchone()
    assert message["device_name"] == upload_command.source_device_name


@pytest.mark.parametrize(
    "change",
    (
        {"sha256": "0" * 64},
        {"original_name": "other.txt"},
        {"mime_type": "application/octet-stream"},
        {"size_bytes": 7},
    ),
)
def test_finalize_rejects_pending_metadata_conflict(repository, upload_command, clock, tmp_path: Path, change: dict[str, object]) -> None:
    session, _ = repository.create_or_get(upload_command, clock(), 86_400, 128)
    confirm(repository, session, 0, b"data", clock())
    confirm(repository, session, 1, b"more", clock())
    repository.begin_completion(str(session["upload_id"]), clock(), 86_400)
    digest = sha256(b"datamore").hexdigest()
    repository.set_publication_state(str(session["upload_id"]), "assembled", digest, clock(), 86_400)
    repository.set_publication_state(str(session["upload_id"]), "file_published", digest, clock(), 86_400)
    pending = PendingFile("file-1", "notes.txt", "file-1_notes.txt", tmp_path / "part", tmp_path / "final", "text/plain", ".txt", 8, digest)
    with pytest.raises((UploadConflict, UploadStateConflict)):
        repository.finalize_publication(str(session["upload_id"]), replace(pending, **change), clock())


def test_invalid_publication_state_raises_domain_conflict(repository, upload_command, clock) -> None:
    session, _ = repository.create_or_get(upload_command, clock(), 86_400, 128)
    with pytest.raises(UploadStateConflict):
        repository.set_publication_state(str(session["upload_id"]), "invalid", None, clock(), 86_400)  # type: ignore[arg-type]


def test_claim_expired_marks_active_rows_failed(repository, upload_command, clock) -> None:
    session, _ = repository.create_or_get(upload_command, clock(), 1, 128)
    clock.advance(seconds=2)
    claimed = repository.claim_expired(clock())
    assert [item["upload_id"] for item in claimed] == [session["upload_id"]]
    assert claimed[0]["status"] == "expired"
    assert repository.claim_expired(clock()) == []


def test_expired_upload_rejects_cancel_and_fail_without_extending_expiry(repository, upload_command, clock) -> None:
    session, _ = repository.create_or_get(upload_command, clock(), 1, 128)
    clock.advance(seconds=2)
    expired = repository.claim_expired(clock())[0]
    expiry = expired["expires_at"]
    with pytest.raises(UploadStateConflict):
        repository.cancel(str(session["upload_id"]), clock(), 86_400)
    with pytest.raises(UploadStateConflict):
        repository.fail(str(session["upload_id"]), "retry_failed", clock(), 86_400)
    assert repository.get(str(session["upload_id"]))["expires_at"] == expiry
