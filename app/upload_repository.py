from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from .database import Database
from .event_store import EventWriter
from .repository import MessageRepository
from .storage import PendingFile, sanitize_filename


class UploadNotFound(Exception):
    pass


class UploadConflict(Exception):
    pass


class UploadStateConflict(Exception):
    pass


class UploadCapacityExceeded(Exception):
    pass


class UploadStorageCommitmentExceeded(Exception):
    def __init__(self, required_bytes: int, budget_bytes: int) -> None:
        super().__init__("Insufficient storage capacity for active uploads")
        self.required_bytes = required_bytes
        self.budget_bytes = budget_bytes


@dataclass(frozen=True, slots=True)
class UploadCreate:
    client_request_id: str
    original_name: str
    mime_type: str
    size_bytes: int
    last_modified_ms: int
    sample_sha256: str
    chunk_size_bytes: int
    source_device_id: str
    source_device_name: str


@dataclass(frozen=True, slots=True)
class PartLease:
    upload_id: str
    part_index: int
    start_byte: int
    end_byte: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PartRecord:
    upload_id: str
    part_index: int
    start_byte: int
    end_byte: int
    size_bytes: int
    sha256: str
    created_at: str


CONTROL_TRANSITIONS = {
    ("queued", "pause"): "paused",
    ("uploading", "pause"): "paused",
    ("failed", "resume"): "uploading",
    ("paused", "resume"): "uploading",
}
ACTIVE_STATUSES = ("queued", "uploading", "paused", "verifying", "failed")
SESSION_KEYS = (
    "client_request_id", "source_device_id", "source_device_name", "original_name", "mime_type",
    "size_bytes", "last_modified_ms", "sample_sha256", "chunk_size_bytes",
    "status", "confirmed_bytes", "file_sha256", "message_id", "error_code",
    "publication_state", "created_at", "updated_at", "expires_at",
)
CREATE_METADATA_KEYS = (
    "client_request_id", "source_device_id", "source_device_name", "original_name", "mime_type",
    "size_bytes", "last_modified_ms", "sample_sha256", "chunk_size_bytes",
)


class UploadRepository:
    def __init__(self, db: Database, events: EventWriter | None = None) -> None:
        self.db = db
        self.events = events or EventWriter(10_000)

    @staticmethod
    def _expiry(now: datetime, ttl_seconds: int) -> str:
        return (now + timedelta(seconds=ttl_seconds)).isoformat()

    @staticmethod
    def _session(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
        count = connection.execute(
            "SELECT COUNT(*) FROM upload_parts WHERE upload_id = ?", (row["id"],)
        ).fetchone()[0]
        payload = {key: row[key] for key in SESSION_KEYS}
        return {"upload_id": row["id"], **payload, "confirmed_parts": int(count)}

    def _load(self, connection: sqlite3.Connection, upload_id: str) -> dict[str, object]:
        row = connection.execute(
            "SELECT * FROM upload_sessions WHERE id = ?", (upload_id,)
        ).fetchone()
        if row is None:
            raise UploadNotFound(upload_id)
        return self._session(connection, row)

    def create_or_get(
        self, command: UploadCreate, now: datetime, ttl_seconds: int, max_active: int,
        *, include_event: bool = False, capacity_budget_bytes: int | None = None,
    ) -> tuple[dict[str, object], bool] | dict[str, object]:
        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM upload_sessions WHERE client_request_id = ?",
                (command.client_request_id,),
            ).fetchone()
            if existing is not None:
                if any(existing[key] != getattr(command, key) for key in CREATE_METADATA_KEYS):
                    raise UploadConflict(command.client_request_id)
                session = self._session(connection, existing)
                if include_event:
                    return {"result": session, "events": [], "changed": False}
                return session, False
            if connection.execute(
                "SELECT 1 FROM messages WHERE client_request_id = ? UNION ALL "
                "SELECT 1 FROM upload_reservations WHERE client_request_id = ? LIMIT 1",
                (command.client_request_id, command.client_request_id),
            ).fetchone() is not None:
                raise UploadConflict(command.client_request_id)
            placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
            active = connection.execute(
                f"SELECT COUNT(*) FROM upload_sessions WHERE status IN ({placeholders})",
                ACTIVE_STATUSES,
            ).fetchone()[0]
            if int(active) >= max_active:
                raise UploadCapacityExceeded(max_active)
            if capacity_budget_bytes is not None:
                placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
                committed = connection.execute(
                    f"SELECT COALESCE(SUM(CASE "
                    "WHEN publication_state IN ('assembled', 'file_published', 'published') THEN 0 "
                    "ELSE (2 * size_bytes) - confirmed_bytes END), 0) "
                    f"FROM upload_sessions WHERE status IN ({placeholders})",
                    ACTIVE_STATUSES,
                ).fetchone()[0]
                required = int(committed) + (2 * command.size_bytes)
                if required > capacity_budget_bytes:
                    raise UploadStorageCommitmentExceeded(
                        required, capacity_budget_bytes
                    )
            upload_id = uuid4().hex
            timestamp = now.isoformat()
            connection.execute(
                "INSERT INTO upload_sessions "
                "(id, client_request_id, source_device_id, source_device_name, original_name, mime_type, "
                "size_bytes, last_modified_ms, sample_sha256, chunk_size_bytes, status, "
                "created_at, updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
                (upload_id, command.client_request_id, command.source_device_id,
                 command.source_device_name, command.original_name, command.mime_type, command.size_bytes,
                 command.last_modified_ms, command.sample_sha256,
                 command.chunk_size_bytes, timestamp, timestamp,
                 self._expiry(now, ttl_seconds)),
            )
            session = self._load(connection, upload_id)
            event = self._append_event(
                connection, "upload.created", upload_id, session, timestamp
            )
            if include_event:
                return {"result": session, "events": [event], "changed": True}
            return session, True

    def get_by_client_request(
        self, command: UploadCreate
    ) -> dict[str, object] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM upload_sessions WHERE client_request_id = ?",
                (command.client_request_id,),
            ).fetchone()
            if row is None:
                return None
            if any(row[key] != getattr(command, key) for key in CREATE_METADATA_KEYS):
                raise UploadConflict(command.client_request_id)
            return self._session(connection, row)

    def get(self, upload_id: str) -> dict[str, object] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM upload_sessions WHERE id = ?", (upload_id,)
            ).fetchone()
            return self._session(connection, row) if row is not None else None

    def list_active(self) -> list[dict[str, object]]:
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        with self.db.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM upload_sessions WHERE status IN ({placeholders}) ORDER BY created_at, id",
                ACTIVE_STATUSES,
            ).fetchall()
            return [self._session(connection, row) for row in rows]

    def list_sessions(self) -> list[dict[str, object]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM upload_sessions ORDER BY created_at, id"
            ).fetchall()
            return [self._session(connection, row) for row in rows]

    def confirmed_part_keys(self) -> set[tuple[str, int]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT upload_id, part_index FROM upload_parts"
            ).fetchall()
            return {(str(row["upload_id"]), int(row["part_index"])) for row in rows}

    def list_parts(self, upload_id: str) -> list[PartRecord]:
        with self.db.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM upload_sessions WHERE id = ?", (upload_id,)
            ).fetchone() is None:
                raise UploadNotFound(upload_id)
            rows = connection.execute(
                "SELECT * FROM upload_parts WHERE upload_id = ? ORDER BY part_index", (upload_id,)
            ).fetchall()
            return [PartRecord(**dict(row)) for row in rows]

    def begin_part(
        self, upload_id: str, part_index: int, start_byte: int, end_byte: int,
        size_bytes: int, sha256: str,
    ) -> PartLease | dict[str, object]:
        with self.db.transaction() as connection:
            session = self._load(connection, upload_id)
            existing = connection.execute(
                "SELECT * FROM upload_parts WHERE upload_id = ? AND part_index = ?",
                (upload_id, part_index),
            ).fetchone()
            if existing is not None:
                expected = (start_byte, end_byte, size_bytes, sha256)
                actual = tuple(existing[key] for key in ("start_byte", "end_byte", "size_bytes", "sha256"))
                if actual != expected:
                    raise UploadConflict(f"Conflicting part {part_index}")
                return session
            if session["status"] not in {"queued", "uploading"}:
                raise UploadStateConflict(str(session["status"]))
            if part_index < 0 or start_byte < 0 or end_byte < start_byte or size_bytes <= 0:
                raise UploadConflict(f"Invalid part {part_index}")
            if end_byte - start_byte + 1 != size_bytes or end_byte >= int(session["size_bytes"]):
                raise UploadConflict(f"Invalid range for part {part_index}")
            return PartLease(upload_id, part_index, start_byte, end_byte, size_bytes, sha256)

    def confirm_part(
        self, lease: PartLease, part: PartRecord, now: datetime, ttl_seconds: int
    ) -> dict[str, object]:
        if lease != PartLease(
            part.upload_id, part.part_index, part.start_byte, part.end_byte,
            part.size_bytes, part.sha256,
        ):
            raise UploadConflict("Part lease does not match record")
        with self.db.transaction() as connection:
            session = self._load(connection, lease.upload_id)
            if (
                part.part_index < 0
                or part.start_byte < 0
                or part.end_byte < part.start_byte
                or part.size_bytes <= 0
                or part.end_byte - part.start_byte + 1 != part.size_bytes
                or part.end_byte >= int(session["size_bytes"])
            ):
                raise UploadConflict(f"Invalid range for part {part.part_index}")
            existing = connection.execute(
                "SELECT * FROM upload_parts WHERE upload_id = ? AND part_index = ?",
                (lease.upload_id, lease.part_index),
            ).fetchone()
            if existing is not None:
                expected = (part.start_byte, part.end_byte, part.size_bytes, part.sha256)
                actual = tuple(existing[key] for key in ("start_byte", "end_byte", "size_bytes", "sha256"))
                if actual != expected:
                    raise UploadConflict(f"Conflicting part {part.part_index}")
                return session
            if session["status"] not in {"queued", "uploading", "paused"}:
                raise UploadStateConflict(str(session["status"]))
            connection.execute(
                "INSERT INTO upload_parts (upload_id, part_index, start_byte, end_byte, size_bytes, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (part.upload_id, part.part_index, part.start_byte, part.end_byte,
                 part.size_bytes, part.sha256, part.created_at),
            )
            confirmed_bytes = connection.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM upload_parts WHERE upload_id = ?",
                (lease.upload_id,),
            ).fetchone()[0]
            status = "uploading" if session["status"] == "queued" else session["status"]
            connection.execute(
                "UPDATE upload_sessions SET status = ?, confirmed_bytes = ?, updated_at = ?, expires_at = ? WHERE id = ?",
                (status, confirmed_bytes, now.isoformat(), self._expiry(now, ttl_seconds), lease.upload_id),
            )
            return self._load(connection, lease.upload_id)

    def transition(
        self, upload_id: str, action: Literal["pause", "resume"],
        source_device_id: str, now: datetime, ttl_seconds: int, *, include_event: bool = False,
    ) -> dict[str, object]:
        with self.db.transaction() as connection:
            session = self._load(connection, upload_id)
            if session["source_device_id"] != source_device_id:
                raise UploadConflict("Only the source device can control an upload")
            target = CONTROL_TRANSITIONS.get((str(session["status"]), action))
            if target is None:
                raise UploadStateConflict(f"Cannot {action} {session['status']}")
            connection.execute(
                "UPDATE upload_sessions SET status = ?, error_code = NULL, updated_at = ?, expires_at = ? WHERE id = ?",
                (target, now.isoformat(), self._expiry(now, ttl_seconds), upload_id),
            )
            result = self._load(connection, upload_id)
            if not include_event:
                return result
            event = self._append_event(
                connection, "upload.state_changed", upload_id, result, now.isoformat()
            )
            return {"result": result, "events": [event], "changed": True}

    def cancel(
        self, upload_id: str, now: datetime, ttl_seconds: int, *, include_event: bool = False
    ) -> tuple[dict[str, object], bool] | dict[str, object]:
        with self.db.transaction() as connection:
            session = self._load(connection, upload_id)
            if session["status"] == "cancelled":
                if include_event:
                    return {"result": session, "events": [], "changed": False}
                return session, False
            if session["status"] in {"complete", "expired"}:
                raise UploadStateConflict(f"{session['status']} uploads cannot be cancelled")
            connection.execute(
                "UPDATE upload_sessions SET status = 'cancelled', updated_at = ?, expires_at = ? WHERE id = ?",
                (now.isoformat(), self._expiry(now, ttl_seconds), upload_id),
            )
            result = self._load(connection, upload_id)
            event = self._append_event(
                connection, "upload.cancelled", upload_id, result, now.isoformat()
            )
            if include_event:
                return {"result": result, "events": [event], "changed": True}
            return result, True

    def begin_completion(
        self, upload_id: str, now: datetime, ttl_seconds: int, *, include_event: bool = False
    ) -> dict[str, object]:
        with self.db.transaction() as connection:
            session = self._load(connection, upload_id)
            if session["status"] != "uploading":
                raise UploadStateConflict(str(session["status"]))
            rows = connection.execute(
                "SELECT start_byte, end_byte, size_bytes FROM upload_parts WHERE upload_id = ? ORDER BY start_byte",
                (upload_id,),
            ).fetchall()
            cursor = 0
            for row in rows:
                if row["start_byte"] != cursor or row["end_byte"] - row["start_byte"] + 1 != row["size_bytes"]:
                    raise UploadStateConflict("Upload parts are not contiguous")
                cursor = row["end_byte"] + 1
            if cursor != session["size_bytes"]:
                raise UploadStateConflict("Upload is incomplete")
            connection.execute(
                "UPDATE upload_sessions SET status = 'verifying', publication_state = 'assembling', updated_at = ?, expires_at = ? WHERE id = ?",
                (now.isoformat(), self._expiry(now, ttl_seconds), upload_id),
            )
            result = self._load(connection, upload_id)
            if not include_event:
                return result
            event = self._append_event(
                connection, "upload.state_changed", upload_id, result, now.isoformat()
            )
            return {"result": result, "events": [event], "changed": True}

    def set_publication_state(
        self, upload_id: str, state: Literal["assembling", "assembled", "file_published"],
        file_sha256: str | None, now: datetime, ttl_seconds: int,
    ) -> dict[str, object]:
        order = {"assembling": 0, "assembled": 1, "file_published": 2}
        with self.db.transaction() as connection:
            session = self._load(connection, upload_id)
            current = str(session["publication_state"])
            if state not in order:
                raise UploadStateConflict(f"Unknown publication state {state}")
            if current == state and session["file_sha256"] == file_sha256:
                return session
            if session["status"] != "verifying" or current not in order or order[state] != order[current] + 1:
                raise UploadStateConflict(f"Cannot transition publication from {current} to {state}")
            connection.execute(
                "UPDATE upload_sessions SET publication_state = ?, file_sha256 = COALESCE(?, file_sha256), updated_at = ?, expires_at = ? WHERE id = ?",
                (state, file_sha256, now.isoformat(), self._expiry(now, ttl_seconds), upload_id),
            )
            return self._load(connection, upload_id)

    def _append_event(
        self, connection: sqlite3.Connection, event_type: str, entity_id: str,
        payload: dict[str, object], created_at: str,
    ) -> dict[str, object]:
        return self.events.append(
            connection, event_type, entity_id, payload, created_at
        )

    def persist_progress(
        self, upload_id: str, payload: dict[str, object]
    ) -> dict[str, object]:
        created_at = str(payload["updated_at"])
        with self.db.transaction() as connection:
            self._load(connection, upload_id)
            return self._append_event(
                connection, "upload.progress", upload_id, payload, created_at
            )

    def finalize_publication(
        self, upload_id: str, pending: PendingFile, now: datetime
    ) -> dict[str, object]:
        with self.db.transaction() as connection:
            session = self._load(connection, upload_id)
            if session["status"] == "complete":
                return {"result": session, "events": [], "changed": False}
            if session["status"] != "verifying" or session["publication_state"] != "file_published":
                raise UploadStateConflict(str(session["status"]))
            if session["file_sha256"] is None:
                raise UploadStateConflict("Published upload has no durable digest")
            expected_metadata = (
                sanitize_filename(str(session["original_name"])),
                session["mime_type"],
                session["size_bytes"],
                session["file_sha256"],
            )
            pending_metadata = (
                pending.original_name, pending.mime_type, pending.size_bytes, pending.sha256,
            )
            if pending_metadata != expected_metadata:
                raise UploadConflict("Published file metadata differs from upload")
            timestamp = now.isoformat()
            message_id = uuid4().hex
            connection.execute(
                "INSERT INTO files (id, original_name, storage_name, mime_type, extension, size_bytes, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pending.file_id, pending.original_name, pending.storage_name, pending.mime_type,
                 pending.extension, pending.size_bytes, pending.sha256, timestamp),
            )
            device_id = str(session["source_device_id"])
            device_name = str(session["source_device_name"])
            connection.execute(
                "INSERT INTO messages (id, kind, body, file_id, client_request_id, device_id, device_name, created_at) VALUES (?, 'file', NULL, ?, ?, ?, ?, ?)",
                (message_id, pending.file_id, session["client_request_id"], device_id, device_name, timestamp),
            )
            connection.execute(
                "UPDATE upload_sessions SET status = 'complete', publication_state = 'published', message_id = ?, file_sha256 = ?, error_code = NULL, updated_at = ? WHERE id = ?",
                (message_id, pending.sha256, timestamp, upload_id),
            )
            completed_session = self._load(connection, upload_id)
            message_row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            message_payload = MessageRepository(self.db, self.events)._message_payload(
                connection, message_row
            )
            file_payload = {"id": pending.file_id, "original_name": pending.original_name,
                            "storage_name": pending.storage_name, "mime_type": pending.mime_type,
                            "extension": pending.extension, "size_bytes": pending.size_bytes,
                            "sha256": pending.sha256, "created_at": timestamp}
            events = [
                self._append_event(connection, "upload.completed", upload_id, completed_session, timestamp),
                self._append_event(connection, "message.created", message_id, message_payload, timestamp),
                self._append_event(connection, "file.finalized", pending.file_id, file_payload, timestamp),
            ]
            return {"result": completed_session, "events": events, "changed": True}

    def get_completed_message(self, upload_id: str) -> dict[str, object]:
        with self.db.connect() as connection:
            session = self._load(connection, upload_id)
            if session["status"] != "complete" or session["message_id"] is None:
                raise UploadStateConflict(str(session["status"]))
            message = MessageRepository(self.db, self.events).get_message(
                str(session["message_id"]), connection
            )
            if message is None:
                raise UploadStateConflict("Completed upload message is missing")
            return message

    def reset_assembling(
        self, upload_id: str, now: datetime, *, include_event: bool = False
    ) -> dict[str, object]:
        with self.db.transaction() as connection:
            session = self._load(connection, upload_id)
            changed = False
            if session["status"] == "verifying" and session["publication_state"] == "assembling":
                connection.execute(
                    "UPDATE upload_sessions SET status = 'uploading', publication_state = 'none', "
                    "file_sha256 = NULL, updated_at = ? WHERE id = ?",
                    (now.isoformat(), upload_id),
                )
                changed = True
            result = self._load(connection, upload_id)
            if not include_event:
                return result
            if not changed:
                return {"result": result, "events": [], "changed": False}
            event = self._append_event(
                connection,
                "upload.state_changed",
                upload_id,
                result,
                now.isoformat(),
            )
            return {"result": result, "events": [event], "changed": True}

    def reconcile_missing_parts(
        self, missing: set[tuple[str, int]], now: datetime, ttl_seconds: int
    ) -> list[dict[str, object]]:
        if not missing:
            return []
        changed: list[dict[str, object]] = []
        with self.db.transaction() as connection:
            for upload_id in sorted({key[0] for key in missing}):
                session = self._load(connection, upload_id)
                if session["publication_state"] in {
                    "assembled",
                    "file_published",
                    "published",
                }:
                    continue
                indexes = [key[1] for key in missing if key[0] == upload_id]
                placeholders = ", ".join("?" for _ in indexes)
                connection.execute(
                    f"DELETE FROM upload_parts WHERE upload_id = ? AND part_index IN ({placeholders})",
                    (upload_id, *indexes),
                )
                confirmed_bytes = connection.execute(
                    "SELECT COALESCE(SUM(size_bytes), 0) FROM upload_parts WHERE upload_id = ?",
                    (upload_id,),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE upload_sessions SET status = 'failed', error_code = 'missing_part', "
                    "publication_state = 'none', file_sha256 = NULL, confirmed_bytes = ?, "
                    "updated_at = ?, expires_at = ? WHERE id = ? AND status NOT IN ('complete', 'cancelled', 'expired')",
                    (confirmed_bytes, now.isoformat(), self._expiry(now, ttl_seconds), upload_id),
                )
                result = self._load(connection, upload_id)
                event = self._append_event(
                    connection,
                    "upload.state_changed",
                    upload_id,
                    result,
                    now.isoformat(),
                )
                changed.append(
                    {"result": result, "events": [event], "changed": True}
                )
        return changed

    def invalidate_part(
        self,
        upload_id: str,
        part_index: int,
        error_code: str,
        now: datetime,
        ttl_seconds: int,
        *,
        include_event: bool = False,
    ) -> dict[str, object]:
        with self.db.transaction() as connection:
            session = self._load(connection, upload_id)
            if session["status"] in {"complete", "cancelled", "expired"}:
                raise UploadStateConflict(str(session["status"]))
            connection.execute(
                "DELETE FROM upload_parts WHERE upload_id = ? AND part_index = ?",
                (upload_id, part_index),
            )
            confirmed_bytes = connection.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM upload_parts WHERE upload_id = ?",
                (upload_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE upload_sessions SET status = 'failed', error_code = ?, "
                "publication_state = 'none', file_sha256 = NULL, confirmed_bytes = ?, "
                "updated_at = ?, expires_at = ? WHERE id = ?",
                (
                    error_code,
                    confirmed_bytes,
                    now.isoformat(),
                    self._expiry(now, ttl_seconds),
                    upload_id,
                ),
            )
            result = self._load(connection, upload_id)
            if not include_event:
                return result
            event = self._append_event(
                connection,
                "upload.state_changed",
                upload_id,
                result,
                now.isoformat(),
            )
            return {"result": result, "events": [event], "changed": True}

    def expired_ids(self, now: datetime, limit: int = 100) -> set[str]:
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        with self.db.connect() as connection:
            rows = connection.execute(
                f"SELECT id FROM upload_sessions WHERE status IN ({placeholders}) "
                "AND expires_at <= ? ORDER BY expires_at, id LIMIT ?",
                (*ACTIVE_STATUSES, now.isoformat(), limit),
            ).fetchall()
            return {str(row["id"]) for row in rows}

    def expire_one(
        self, upload_id: str, now: datetime, *, was_due: bool = False
    ) -> dict[str, object] | None:
        with self.db.transaction() as connection:
            session = self._load(connection, upload_id)
            if session["status"] not in ACTIVE_STATUSES:
                return None
            if not was_due and str(session["expires_at"]) > now.isoformat():
                return None
            connection.execute(
                "UPDATE upload_sessions SET status = 'expired', error_code = 'expired', "
                "updated_at = ? WHERE id = ?",
                (now.isoformat(), upload_id),
            )
            result = self._load(connection, upload_id)
            event = self._append_event(
                connection,
                "upload.expired",
                upload_id,
                result,
                now.isoformat(),
            )
            return {"result": result, "events": [event], "changed": True}

    def finish_published(
        self, upload_id: str, now: datetime, *, include_event: bool = False
    ) -> dict[str, object]:
        with self.db.transaction() as connection:
            session = self._load(connection, upload_id)
            changed = False
            if session["publication_state"] == "published" and session["message_id"] is not None:
                if connection.execute(
                    "SELECT 1 FROM messages WHERE id = ?", (session["message_id"],)
                ).fetchone() is None:
                    raise UploadStateConflict("Published upload message is missing")
                connection.execute(
                    "UPDATE upload_sessions SET status = 'complete', error_code = NULL, updated_at = ? WHERE id = ?",
                    (now.isoformat(), upload_id),
                )
                changed = session["status"] != "complete"
            result = self._load(connection, upload_id)
            if not include_event:
                return result
            if not changed:
                return {"result": result, "events": [], "changed": False}
            event = self._append_event(
                connection,
                "upload.state_changed",
                upload_id,
                result,
                now.isoformat(),
            )
            return {"result": result, "events": [event], "changed": True}

    def fail(
        self,
        upload_id: str,
        error_code: str,
        now: datetime,
        ttl_seconds: int,
        *,
        include_event: bool = False,
        reset_publication: bool = False,
    ) -> dict[str, object]:
        with self.db.transaction() as connection:
            session = self._load(connection, upload_id)
            if session["status"] in {"complete", "cancelled", "expired"}:
                raise UploadStateConflict(str(session["status"]))
            publication_is_reset = (
                session["publication_state"] == "none"
                and session["file_sha256"] is None
                and session["message_id"] is None
            )
            if (
                session["status"] == "failed"
                and session["error_code"] == error_code
                and (not reset_publication or publication_is_reset)
            ):
                if include_event:
                    return {"result": session, "events": [], "changed": False}
                return session
            if reset_publication:
                connection.execute(
                    "UPDATE upload_sessions SET status = 'failed', error_code = ?, "
                    "publication_state = 'none', file_sha256 = NULL, message_id = NULL, "
                    "updated_at = ?, expires_at = ? WHERE id = ?",
                    (
                        error_code,
                        now.isoformat(),
                        self._expiry(now, ttl_seconds),
                        upload_id,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE upload_sessions SET status = 'failed', error_code = ?, updated_at = ?, expires_at = ? WHERE id = ?",
                    (error_code, now.isoformat(), self._expiry(now, ttl_seconds), upload_id),
                )
            result = self._load(connection, upload_id)
            if not include_event:
                return result
            event = self._append_event(
                connection,
                "upload.state_changed",
                upload_id,
                result,
                now.isoformat(),
            )
            return {"result": result, "events": [event], "changed": True}

    def claim_expired(self, now: datetime, limit: int = 100) -> list[dict[str, object]]:
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        with self.db.transaction() as connection:
            rows = connection.execute(
                f"SELECT id FROM upload_sessions WHERE status IN ({placeholders}) AND expires_at <= ? ORDER BY expires_at, id LIMIT ?",
                (*ACTIVE_STATUSES, now.isoformat(), limit),
            ).fetchall()
            upload_ids = [str(row["id"]) for row in rows]
            if upload_ids:
                ids = ", ".join("?" for _ in upload_ids)
                connection.execute(
                    f"UPDATE upload_sessions SET status = 'expired', error_code = 'expired', updated_at = ? WHERE id IN ({ids})",
                    (now.isoformat(), *upload_ids),
                )
            return [self._load(connection, upload_id) for upload_id in upload_ids]

    def expire_mutations(
        self, now: datetime, limit: int = 100, force_ids: set[str] | None = None
    ) -> list[dict[str, object]]:
        with self.db.transaction() as connection:
            placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
            forced = sorted(force_ids or set())
            forced_clause = ""
            parameters: tuple[object, ...]
            if forced:
                forced_placeholders = ", ".join("?" for _ in forced)
                forced_clause = f" OR id IN ({forced_placeholders})"
                parameters = (*ACTIVE_STATUSES, now.isoformat(), *forced, limit)
            else:
                parameters = (*ACTIVE_STATUSES, now.isoformat(), limit)
            rows = connection.execute(
                f"SELECT id FROM upload_sessions WHERE status IN ({placeholders}) "
                f"AND (expires_at <= ?{forced_clause}) ORDER BY expires_at, id LIMIT ?",
                parameters,
            ).fetchall()
            upload_ids = [str(row["id"]) for row in rows]
            if upload_ids:
                ids = ", ".join("?" for _ in upload_ids)
                connection.execute(
                    f"UPDATE upload_sessions SET status = 'expired', error_code = 'expired', "
                    f"updated_at = ? WHERE id IN ({ids})",
                    (now.isoformat(), *upload_ids),
                )
            mutations: list[dict[str, object]] = []
            for upload_id in upload_ids:
                result = self._load(connection, upload_id)
                upload_id = str(result["upload_id"])
                event = self._append_event(
                    connection, "upload.expired", upload_id, result, now.isoformat()
                )
                mutations.append({"result": result, "events": [event], "changed": True})
        return mutations
