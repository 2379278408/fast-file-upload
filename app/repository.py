from __future__ import annotations

import json
import logging
import mimetypes
import sqlite3
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .auth import SessionData
from .database import Database
from .storage import IMAGE_EXTENSIONS, FileStorage, PendingFile, StoredFile, format_size


logger = logging.getLogger("transfer.migration")


@dataclass(slots=True)
class MigrationResult:
    imported: int = 0
    skipped: int = 0
    failed: int = 0


class RestoreWindowExpired(Exception):
    def __init__(self, message_id: str) -> None:
        super().__init__(f"Restore window expired for message {message_id}")
        self.message_id = message_id


class IdempotencyConflict(Exception):
    pass


class BatchDownloadTooLarge(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"Batch download exceeds the {limit}-byte limit")
        self.limit = limit


class BatchDownloadSourceMissing(Exception):
    def __init__(self, display_name: str) -> None:
        super().__init__(f"Source file is missing: {display_name}")
        self.display_name = display_name


class NoDownloadableFiles(Exception):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_sortable_id(now: datetime | None = None) -> str:
    timestamp = now or utc_now()
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    milliseconds = int(timestamp.timestamp() * 1000)
    return f"{milliseconds:013d}{uuid4().hex}"


class MessageRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _message_payload(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": row["id"],
            "kind": row["kind"],
            "body": row["body"],
            "file_id": row["file_id"],
            "client_request_id": row["client_request_id"],
            "device_id": row["device_id"],
            "device_name": row["device_name"],
            "created_at": row["created_at"],
            "deleted_at": row["deleted_at"],
            "file": None,
        }
        if row["file_id"] is not None:
            file_row = connection.execute(
                "SELECT * FROM files WHERE id = ?", (row["file_id"],)
            ).fetchone()
            if file_row is not None:
                file_payload = dict(file_row)
                extension = file_row["extension"]
                file_payload.update(
                    {
                        "name": file_row["original_name"],
                        "size": format_size(file_row["size_bytes"]),
                        "media_kind": "image"
                        if extension in IMAGE_EXTENSIONS
                        else "document",
                        "is_previewable": extension in IMAGE_EXTENSIONS,
                        "download_url": f"/download/{file_row['id']}",
                    }
                )
                payload["file"] = file_payload
        return payload

    def _append_event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        entity_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        created_at = utc_now().isoformat()
        insertion = connection.execute(
            "INSERT INTO events (event_type, entity_id, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (event_type, entity_id, json.dumps(payload), created_at),
        )
        return {
            "sequence": int(insertion.lastrowid),
            "event_type": event_type,
            "entity_id": entity_id,
            "payload": payload,
            "created_at": created_at,
        }

    def latest_sequence(self) -> int:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT MAX(sequence) FROM events"
            ).fetchone()
            return int(row[0]) if row is not None and row[0] is not None else 0

    def events_after(self, sequence: int, limit: int = 500) -> list[dict[str, object]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT sequence, event_type, entity_id, payload, created_at "
                "FROM events WHERE sequence > ? ORDER BY sequence ASC LIMIT ?",
                (sequence, limit),
            ).fetchall()
            return [
                {
                    "sequence": row["sequence"],
                    "event_type": row["event_type"],
                    "entity_id": row["entity_id"],
                    "payload": json.loads(row["payload"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def get_message(
        self, message_id: str, connection: sqlite3.Connection | None = None
    ) -> dict[str, object] | None:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            return self._message_payload(connection, row) if row is not None else None

        with self.db.connect() as owned_connection:
            row = owned_connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            return (
                self._message_payload(owned_connection, row) if row is not None else None
            )

    def create_text(
        self, body: str, client_request_id: str, device: SessionData
    ) -> dict[str, object]:
        normalized = body.strip()
        if not normalized or len(body) > 10_000:
            raise ValueError("Text must be between 1 and 10000 characters")

        message_id = new_sortable_id()
        created_at = utc_now().isoformat()
        with self.db.transaction() as connection:
            insertion = connection.execute(
                "INSERT INTO messages "
                "(id, kind, body, file_id, client_request_id, device_id, device_name, "
                "created_at, deleted_at) VALUES (?, 'text', ?, NULL, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(client_request_id) DO NOTHING",
                (
                    message_id,
                    body,
                    client_request_id,
                    device.device_id,
                    device.device_name,
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM messages WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Created message could not be loaded")
            if row["kind"] != "text":
                raise IdempotencyConflict(client_request_id)
            result = self._message_payload(connection, row)
            event = (
                self._append_event(
                    connection, "message.created", str(result["id"]), result
                )
                if insertion.rowcount == 1
                else None
            )
            return {"result": result, "event": event}

    def soft_delete(self, message_id: str, now: datetime) -> dict[str, object] | None:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                return None
            changed = row["deleted_at"] is None
            if changed:
                connection.execute(
                    "UPDATE messages SET deleted_at = ? WHERE id = ?",
                    (now.isoformat(), message_id),
                )
                row = connection.execute(
                    "SELECT * FROM messages WHERE id = ?", (message_id,)
                ).fetchone()
            result = self._message_payload(connection, row)
            event = (
                self._append_event(connection, "message.deleted", message_id, result)
                if changed
                else None
            )
            return {"result": result, "event": event}

    def restore(
        self, message_id: str, now: datetime, undo_seconds: int
    ) -> dict[str, object] | None:
        cutoff = now - timedelta(seconds=undo_seconds)
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                return None
            if row["deleted_at"] is None:
                return {
                    "result": self._message_payload(connection, row),
                    "event": None,
                }
            restored = connection.execute(
                "UPDATE messages SET deleted_at = NULL "
                "WHERE id = ? AND deleted_at IS NOT NULL AND deleted_at > ? "
                "AND (file_id IS NULL OR EXISTS ("
                "SELECT 1 FROM files WHERE files.id = messages.file_id "
                "AND files.purge_state = 'active' AND files.purged_at IS NULL))",
                (message_id, cutoff.isoformat()),
            )
            if restored.rowcount != 1:
                raise RestoreWindowExpired(message_id)
            result = self.get_message(message_id, connection)
            if result is None:
                raise RuntimeError("Restored message could not be loaded")
            event = self._append_event(
                connection, "message.restored", message_id, result
            )
            return {"result": result, "event": event}

    def _claim_expired_files(
        self, cutoff: datetime, claimed_at: datetime, claim_token: str
    ) -> list[dict[str, object]]:
        with self.db.transaction(immediate=True) as connection:
            rows = connection.execute(
                "UPDATE files SET purge_state = 'claimed', purge_claimed_at = ?, "
                "purge_claim_token = ? "
                "WHERE purge_state = 'active' AND purged_at IS NULL AND id IN ("
                "SELECT f.id FROM files AS f "
                "JOIN messages AS m ON m.file_id = f.id "
                "WHERE m.kind IN ('file', 'image') AND m.deleted_at IS NOT NULL "
                "AND m.deleted_at <= ?) "
                "RETURNING id AS file_id, storage_name, purge_claim_token",
                (claimed_at.isoformat(), claim_token, cutoff.isoformat()),
            ).fetchall()
        return sorted((dict(row) for row in rows), key=lambda row: str(row["file_id"]))

    def purge_expired_files(
        self, storage: FileStorage, now: datetime, undo_seconds: int
    ) -> dict[str, object]:
        cutoff = now - timedelta(seconds=undo_seconds)
        claim_token = uuid4().hex
        rows = self._claim_expired_files(cutoff, now, claim_token)
        purged: list[str] = []
        events: list[dict[str, object]] = []
        for row in rows:
            file_id = str(row["file_id"])
            row_claim_token = str(row["purge_claim_token"])
            try:
                storage.purge_file(str(row["storage_name"]))
            except Exception as exc:
                self._release_purge_claim(file_id, row_claim_token, exc)
                continue
            try:
                event = self._finalize_purge_claim(file_id, row_claim_token, now)
            except Exception as exc:
                self._record_purge_failure("purge.finalize_failed", file_id, exc)
                continue
            if event is None:
                continue
            events.append(event)
            purged.append(file_id)
        return {
            "result": purged,
            "event": events[0] if events else None,
            "events": events,
        }

    def recover_purge_claims(
        self, storage: FileStorage, now: datetime, lease_seconds: float
    ) -> dict[str, object]:
        stale_before = now - timedelta(seconds=lease_seconds)
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM files WHERE purge_state = 'claimed' "
                "AND purged_at IS NULL AND purge_claimed_at < ? "
                "AND purge_claim_token IS NOT NULL ORDER BY id",
                (stale_before.isoformat(),),
            ).fetchall()
        events: list[dict[str, object]] = []
        recovered: list[str] = []
        for row in rows:
            file_id = str(row["id"])
            claim_token = self._take_over_stale_purge_claim(
                file_id,
                str(row["purge_claim_token"]),
                stale_before,
                now,
            )
            if claim_token is None:
                continue
            try:
                storage.purge_file(str(row["storage_name"]))
            except Exception as exc:
                self._record_purge_failure("purge.recovery_failed", file_id, exc)
                continue
            try:
                event = self._finalize_purge_claim(file_id, claim_token, now)
            except Exception as exc:
                self._record_purge_failure("purge.finalize_failed", file_id, exc)
                continue
            if event is None:
                continue
            events.append(event)
            recovered.append(file_id)
        return {
            "result": recovered,
            "event": events[0] if events else None,
            "events": events,
        }

    def _take_over_stale_purge_claim(
        self,
        file_id: str,
        previous_token: str,
        stale_before: datetime,
        claimed_at: datetime,
    ) -> str | None:
        recovery_token = uuid4().hex
        with self.db.transaction(immediate=True) as connection:
            taken = connection.execute(
                "UPDATE files SET purge_claim_token = ?, purge_claimed_at = ? "
                "WHERE id = ? AND purge_state = 'claimed' AND purge_claim_token = ? "
                "AND purged_at IS NULL AND purge_claimed_at < ?",
                (
                    recovery_token,
                    claimed_at.isoformat(),
                    file_id,
                    previous_token,
                    stale_before.isoformat(),
                ),
            )
        return recovery_token if taken.rowcount == 1 else None

    def _finalize_purge_claim(
        self, file_id: str, claim_token: str, purged_at: datetime
    ) -> dict[str, object] | None:
        with self.db.transaction(immediate=True) as connection:
            finalized = connection.execute(
                "UPDATE files SET purged_at = ?, purge_state = 'purged', "
                "purge_claimed_at = NULL, purge_claim_token = NULL WHERE id = ? "
                "AND purge_state = 'claimed' AND purge_claim_token = ? "
                "AND purged_at IS NULL",
                (purged_at.isoformat(), file_id, claim_token),
            )
            if finalized.rowcount != 1:
                return None
            file_row = connection.execute(
                "SELECT * FROM files WHERE id = ?", (file_id,)
            ).fetchone()
            if file_row is None:
                raise RuntimeError("Purged file could not be loaded")
            return self._append_event(
                connection, "file.purged", file_id, self._file_payload(file_row)
            )

    def _release_purge_claim(
        self, file_id: str, claim_token: str, exc: BaseException | None = None
    ) -> None:
        try:
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE files SET purge_state = 'active', purge_claimed_at = NULL, "
                    "purge_claim_token = NULL WHERE id = ? AND purge_state = 'claimed' "
                    "AND purge_claim_token = ? AND purged_at IS NULL",
                    (file_id, claim_token),
                )
                if exc is not None:
                    connection.execute(
                        "INSERT INTO audit_events (action, entity_id, detail, created_at) "
                        "VALUES ('purge.failed', ?, ?, ?)",
                        (file_id, type(exc).__name__, utc_now().isoformat()),
                    )
        except Exception:
            logger.exception("Failed to record purge failure for %s", file_id)

    def _record_purge_failure(
        self, action: str, file_id: str, exc: BaseException
    ) -> None:
        try:
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO audit_events (action, entity_id, detail, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (action, file_id, type(exc).__name__, utc_now().isoformat()),
                )
        except Exception:
            logger.exception("Failed to record purge failure for %s", file_id)

    def file_download_state(self, file_id: str) -> dict[str, object] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT f.purged_at AS purged_at, f.purge_state AS purge_state, "
                "f.storage_name AS storage_name, f.original_name AS original_name, "
                "m.deleted_at AS message_deleted_at "
                "FROM files AS f LEFT JOIN messages AS m ON m.file_id = f.id "
                "WHERE f.id = ?",
                (file_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def mark_file_purged(self, file_id: str) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE files SET purged_at = ? WHERE id = ? AND purged_at IS NULL",
                (utc_now().isoformat(), file_id),
            )

    def get_message_by_client_request_id(
        self, client_request_id: str
    ) -> dict[str, object] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
            return self._message_payload(connection, row) if row is not None else None

    def get_message_by_file_id(self, file_id: str) -> dict[str, object] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE file_id = ?", (file_id,)
            ).fetchone()
            return self._message_payload(connection, row) if row is not None else None

    def get_upload_reservation(self, client_request_id: str) -> dict[str, object] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM upload_reservations WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def create_upload_reservation(
        self, pending: PendingFile, client_request_id: str, device: SessionData
    ) -> bool:
        owner_token = uuid4().hex
        with self.db.transaction() as connection:
            insertion = connection.execute(
                "INSERT INTO upload_reservations "
                "(client_request_id, file_id, original_name, storage_name, mime_type, "
                "extension, size_bytes, sha256, created_at, device_id, device_name, "
                "reservation_state, owner_token, leased_until) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, NULL) "
                "ON CONFLICT(client_request_id) DO NOTHING",
                (
                    client_request_id,
                    pending.file_id,
                    pending.original_name,
                    pending.storage_name,
                    pending.mime_type,
                    pending.extension,
                    pending.size_bytes,
                    pending.sha256,
                    utc_now().isoformat(),
                    device.device_id,
                    device.device_name,
                    owner_token,
                ),
            )
            return insertion.rowcount == 1

    def recover_upload_reservations(
        self, storage: FileStorage, lease_seconds: float = 300.0
    ) -> list[dict[str, object]]:
        stale_cutoff = utc_now() - timedelta(seconds=lease_seconds)
        with self.db.connect() as connection:
            reservations = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM upload_reservations "
                    "WHERE reservation_state = 'staged' "
                    "AND (leased_until IS NULL OR leased_until < ?) "
                    "ORDER BY created_at, client_request_id",
                    (stale_cutoff.isoformat(),),
                ).fetchall()
            ]
        mutations: list[dict[str, object]] = []
        for reservation in reservations:
            request_id = str(reservation["client_request_id"])
            existing = self.get_message_by_client_request_id(request_id)
            if existing is not None:
                self.delete_upload_reservation(request_id)
                continue
            recovery_token = uuid4().hex
            if not self._claim_reservation(request_id, str(reservation.get("owner_token", "")), recovery_token):
                continue
            pending = storage.pending_from_reservation(reservation)
            if not pending.final_path.is_file() and pending.temporary_path.is_file():
                try:
                    storage.publish(pending)
                except OSError as exc:
                    self._record_upload_recovery_failure(pending.file_id, exc)
                    continue
            if not pending.final_path.is_file():
                self._record_audit_event(
                    "upload.recovery_missing", pending.file_id, "reserved file missing"
                )
                self.delete_upload_reservation(request_id)
                continue
            device = SessionData(
                device_id=str(reservation.get("device_id") or "recovery"),
                device_name=str(reservation.get("device_name") or "Recovered upload"),
                expires_at=0,
            )
            try:
                mutations.append(self.create_file_message(pending, request_id, device))
            except (OSError, sqlite3.Error) as exc:
                self._record_upload_recovery_failure(pending.file_id, exc)
        with self.db.connect() as connection:
            reserved_file_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT file_id FROM upload_reservations"
                ).fetchall()
            }
        storage.discard_orphaned_temporary_files(reserved_file_ids)
        return mutations

    def _claim_reservation(
        self, client_request_id: str, previous_owner: str, new_owner: str
    ) -> bool:
        with self.db.transaction() as connection:
            result = connection.execute(
                "UPDATE upload_reservations SET owner_token = ?, "
                "reservation_state = 'recovery', leased_until = ? "
                "WHERE client_request_id = ? AND reservation_state = 'staged' "
                "AND (owner_token = ? OR leased_until IS NULL OR leased_until < ?)",
                (
                    new_owner,
                    utc_now().isoformat(),
                    client_request_id,
                    previous_owner,
                    (utc_now() - timedelta(seconds=300)).isoformat(),
                ),
            )
            return result.rowcount == 1

    def _record_upload_recovery_failure(
        self, file_id: str, exc: BaseException
    ) -> None:
        self._record_audit_event("upload.recovery_failed", file_id, type(exc).__name__)

    def _record_audit_event(self, action: str, entity_id: str, detail: str) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_events (action, entity_id, detail, created_at) "
                "VALUES (?, ?, ?, ?)",
                (action, entity_id, detail, utc_now().isoformat()),
            )

    def delete_upload_reservation(self, client_request_id: str) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM upload_reservations WHERE client_request_id = ?",
                (client_request_id,),
            )

    def create_file_message(
        self, pending: PendingFile, client_request_id: str, device: SessionData
    ) -> dict[str, object]:
        with self.db.transaction() as connection:
            message_id = new_sortable_id()
            created_at = utc_now().isoformat()
            message_kind = "image" if pending.extension in IMAGE_EXTENSIONS else "file"
            connection.execute(
                "INSERT INTO files "
                "(id, original_name, storage_name, mime_type, extension, size_bytes, "
                "sha256, created_at, purged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    pending.file_id,
                    pending.original_name,
                    pending.storage_name,
                    pending.mime_type,
                    pending.extension,
                    pending.size_bytes,
                    pending.sha256,
                    created_at,
                ),
            )
            insertion = connection.execute(
                "INSERT INTO messages "
                "(id, kind, body, file_id, client_request_id, device_id, device_name, "
                "created_at, deleted_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(client_request_id) DO NOTHING",
                (
                    message_id,
                    message_kind,
                    pending.file_id,
                    client_request_id,
                    device.device_id,
                    device.device_name,
                    created_at,
                ),
            )
            if insertion.rowcount != 1:
                connection.execute("DELETE FROM files WHERE id = ?", (pending.file_id,))
            connection.execute(
                "DELETE FROM upload_reservations WHERE client_request_id = ?",
                (client_request_id,),
            )

            row = connection.execute(
                "SELECT * FROM messages WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Created file message could not be loaded")
            if row["kind"] not in {"file", "image"}:
                raise IdempotencyConflict(client_request_id)
            result = self._message_payload(connection, row)
            if insertion.rowcount == 1:
                message_event = self._append_event(
                    connection, "message.created", str(result["id"]), result
                )
                file_payload = result.get("file")
                file_event = self._append_event(
                    connection,
                    "file.finalized",
                    pending.file_id,
                    file_payload if isinstance(file_payload, dict) else {},
                )
                events = [message_event, file_event]
            else:
                message_event = None
                events = []
            return {"result": result, "event": message_event, "events": events}

    def import_legacy_files(self, storage: FileStorage) -> MigrationResult:
        result = MigrationResult()
        for stored in self._legacy_candidates(storage):
            storage_name = stored.path.name
            if self._migration_recorded(storage_name):
                result.skipped += 1
                continue
            try:
                self._import_one(stored)
            except (OSError, sqlite3.Error) as exc:
                result.failed += 1
                self._record_migration_failure(storage_name, exc)
            else:
                result.imported += 1
        self._record_migration_completed(result)
        return result

    def _legacy_candidates(self, storage: FileStorage) -> list[StoredFile]:
        database_path = self.db.path.resolve()
        database_files = {
            database_path,
            Path(f"{database_path}-wal"),
            Path(f"{database_path}-shm"),
        }
        candidates: list[StoredFile] = []
        for stored in storage.list_files():
            name = stored.path.name
            if name == ".audit.jsonl" or name.endswith(".uploading"):
                continue
            if stored.path.resolve() in database_files:
                continue
            candidates.append(stored)
        return candidates

    def _migration_recorded(self, storage_name: str) -> bool:
        with self.db.connect() as connection:
            imported = connection.execute(
                "SELECT 1 FROM migration_imports WHERE storage_name = ?",
                (storage_name,),
            ).fetchone()
            if imported is not None:
                return True
            reserved = connection.execute(
                "SELECT 1 FROM upload_reservations WHERE storage_name = ?",
                (storage_name,),
            ).fetchone()
            if reserved is not None:
                return True
            indexed = connection.execute(
                "SELECT 1 FROM files WHERE storage_name = ?", (storage_name,)
            ).fetchone()
            return indexed is not None

    def _import_one(self, stored: StoredFile) -> None:
        checksum = stored.checksum_sha256
        created_stamp = datetime.fromtimestamp(stored.modified_at, timezone.utc)
        created_at = created_stamp.isoformat()
        message_id = new_sortable_id(created_stamp)
        mime_type = (
            mimetypes.guess_type(stored.display_name)[0] or "application/octet-stream"
        )
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO files "
                "(id, original_name, storage_name, mime_type, extension, size_bytes, "
                "sha256, created_at, purged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    stored.file_id,
                    stored.display_name,
                    stored.path.name,
                    mime_type,
                    stored.extension,
                    stored.size_bytes,
                    checksum,
                    created_at,
                ),
            )
            connection.execute(
                "INSERT INTO messages "
                "(id, kind, body, file_id, client_request_id, device_id, device_name, "
                "created_at, deleted_at) VALUES (?, ?, NULL, ?, ?, 'migration', "
                "'Legacy import', ?, NULL)",
                (
                    message_id,
                    "image" if stored.extension in IMAGE_EXTENSIONS else "file",
                    stored.file_id,
                    f"legacy-import-{stored.path.name}",
                    created_at,
                ),
            )
            connection.execute(
                "INSERT INTO migration_imports "
                "(storage_name, file_id, message_id, imported_at) VALUES (?, ?, ?, ?)",
                (stored.path.name, stored.file_id, message_id, utc_now().isoformat()),
            )
            message_row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            file_row = connection.execute(
                "SELECT * FROM files WHERE id = ?", (stored.file_id,)
            ).fetchone()
            if message_row is None or file_row is None:
                raise RuntimeError("Imported message could not be loaded")
            self._append_event(
                connection,
                "message.created",
                message_id,
                self._message_payload(connection, message_row),
            )
            self._append_event(
                connection,
                "file.finalized",
                stored.file_id,
                self._file_payload(file_row),
            )

    def _record_migration_failure(self, storage_name: str, exc: BaseException) -> None:
        try:
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO audit_events (action, entity_id, detail, created_at) "
                    "VALUES ('migration.failed', ?, ?, ?)",
                    (storage_name, type(exc).__name__, utc_now().isoformat()),
                )
        except Exception:
            logger.exception("Failed to record migration failure for %s", storage_name)

    def _record_migration_completed(self, result: MigrationResult) -> None:
        detail = json.dumps(
            {
                "imported": result.imported,
                "skipped": result.skipped,
                "failed": result.failed,
            }
        )
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_events (action, entity_id, detail, created_at) "
                "VALUES ('migration.completed', NULL, ?, ?)",
                (detail, utc_now().isoformat()),
            )

    def record_upload_compensation(self, pending: PendingFile, detail: str) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_events (action, entity_id, detail, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("upload.discarded", pending.file_id, detail, utc_now().isoformat()),
            )

    def _file_payload(self, file_row: sqlite3.Row) -> dict[str, object]:
        extension = file_row["extension"]
        return {
            "id": file_row["id"],
            "original_name": file_row["original_name"],
            "storage_name": file_row["storage_name"],
            "mime_type": file_row["mime_type"],
            "extension": extension,
            "size_bytes": file_row["size_bytes"],
            "sha256": file_row["sha256"],
            "created_at": file_row["created_at"],
            "purged_at": file_row["purged_at"],
            "name": file_row["original_name"],
            "size": format_size(file_row["size_bytes"]),
            "media_kind": "image" if extension in IMAGE_EXTENSIONS else "document",
            "is_previewable": extension in IMAGE_EXTENSIONS,
            "download_url": f"/download/{file_row['id']}",
        }

    def _batch_load_files(
        self, connection: sqlite3.Connection, file_ids: list[str]
    ) -> dict[str, dict[str, object]]:
        if not file_ids:
            return {}
        placeholders = ",".join("?" for _ in file_ids)
        rows = connection.execute(
            f"SELECT * FROM files WHERE id IN ({placeholders})", file_ids
        ).fetchall()
        return {row["id"]: self._file_payload(row) for row in rows}

    def list_file_messages(
        self,
        cursor: str | None,
        limit: int,
        file_type: str | None,
        device_id: str | None,
        date_from: str | None,
        date_to: str | None,
        query: str | None = None,
    ) -> dict[str, object]:
        self._validate_limit(limit)
        clauses: list[str] = [
            "m.file_id IS NOT NULL",
            "m.deleted_at IS NULL",
            "f.purged_at IS NULL",
        ]
        values: list[object] = []
        if query:
            pattern = f"%{self._escape_like(query.strip())}%"
            clauses.append("f.original_name LIKE ? ESCAPE '\\'")
            values.append(pattern)
        if file_type == "image":
            clauses.append("f.extension IN ({})".format(
                ",".join("?" for _ in IMAGE_EXTENSIONS)
            ))
            values.extend(IMAGE_EXTENSIONS)
        elif file_type == "document":
            clauses.append("f.extension NOT IN ({})".format(
                ",".join("?" for _ in IMAGE_EXTENSIONS)
            ))
            values.extend(IMAGE_EXTENSIONS)
        if device_id:
            clauses.append("m.device_id = ?")
            values.append(device_id)
        if date_from:
            clauses.append("m.created_at >= ?")
            values.append(date_from)
        if date_to:
            clauses.append("m.created_at < ?")
            values.append(date_to)

        where = " AND ".join(clauses)
        cursor_clause = ""
        cursor_values: list[object] = []
        if cursor:
            cursor_clause = (
                " AND (m.created_at, m.id) < "
                "(SELECT created_at, id FROM messages WHERE id = ?)"
            )
            cursor_values = [cursor]

        all_values = values + cursor_values + [limit + 1]
        with self.db.connect() as connection:
            rows = connection.execute(
                f"SELECT m.* FROM messages AS m "
                f"JOIN files AS f ON f.id = m.file_id "
                f"WHERE {where}{cursor_clause} "
                f"ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
                all_values,
            ).fetchall()

            has_more = len(rows) > limit
            page = rows[:limit]

            file_ids = [row["file_id"] for row in page if row["file_id"] is not None]
            file_cache = self._batch_load_files(connection, file_ids)

            items: list[dict[str, object]] = []
            for row in reversed(page):
                payload: dict[str, object] = {
                    "id": row["id"],
                    "kind": row["kind"],
                    "body": row["body"],
                    "file_id": row["file_id"],
                    "client_request_id": row["client_request_id"],
                    "device_id": row["device_id"],
                    "device_name": row["device_name"],
                    "created_at": row["created_at"],
                    "deleted_at": row["deleted_at"],
                    "file": None,
                }
                if row["file_id"] is not None:
                    payload["file"] = file_cache.get(row["file_id"])
                items.append(payload)

            return {
                "items": items,
                "next_cursor": page[-1]["id"] if has_more else None,
            }

    def build_batch_download_zip(
        self,
        message_ids: list[str],
        storage: FileStorage,
        max_total_bytes: int,
        zip_path: Path,
    ) -> list[str]:
        included: list[tuple[str, str, str]] = []
        with self.db.connect() as connection:
            for mid in message_ids:
                row = connection.execute(
                    "SELECT m.deleted_at, f.storage_name, f.original_name, f.purged_at "
                    "FROM messages AS m JOIN files AS f ON f.id = m.file_id "
                    "WHERE m.id = ?",
                    (mid,),
                ).fetchone()
                if row is None:
                    continue
                if row["deleted_at"] is not None or row["purged_at"] is not None:
                    continue
                included.append(
                    (mid, str(row["storage_name"]), str(row["original_name"]))
                )

        if not included:
            raise NoDownloadableFiles()

        name_counts: dict[str, int] = defaultdict(int)
        deduped: list[tuple[Path, str]] = []
        total_bytes = 0
        for mid, storage_name, original_name in included:
            file_path = storage.path_for(storage_name)
            display_source = Path(original_name).name or "download"
            if not file_path.is_file():
                raise BatchDownloadSourceMissing(display_source)
            total_bytes += file_path.stat().st_size
            if total_bytes > max_total_bytes:
                raise BatchDownloadTooLarge(max_total_bytes)
            count = name_counts[display_source]
            name_counts[display_source] = count + 1
            if count > 0:
                stem = Path(display_source).stem
                suffix = Path(display_source).suffix
                display_name = f"{stem} ({count + 1}){suffix}"
            else:
                display_name = display_source
            deduped.append((file_path, display_name))

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file_path, display_name in deduped:
                try:
                    archive.write(file_path, display_name)
                except FileNotFoundError as error:
                    raise BatchDownloadSourceMissing(display_name) from error
        return [name for _, name in deduped]

    def batch_soft_delete(self, message_ids: list[str]) -> dict[str, object]:
        now = utc_now()
        deleted_count = 0
        deleted_ids: list[str] = []
        events: list[dict[str, object]] = []
        with self.db.transaction() as connection:
            for mid in message_ids:
                row = connection.execute(
                    "SELECT * FROM messages WHERE id = ?", (mid,)
                ).fetchone()
                if row is None:
                    continue
                if row["deleted_at"] is None:
                    connection.execute(
                        "UPDATE messages SET deleted_at = ? WHERE id = ?",
                        (now.isoformat(), mid),
                    )
                    updated = connection.execute(
                        "SELECT * FROM messages WHERE id = ?", (mid,)
                    ).fetchone()
                    if updated is None:
                        raise RuntimeError("Deleted message could not be loaded")
                    events.append(
                        self._append_event(
                            connection,
                            "message.deleted",
                            mid,
                            self._message_payload(connection, updated),
                        )
                    )
                    deleted_count += 1
                    deleted_ids.append(mid)
        return {"result": deleted_count, "event": events[0] if events else None, "events": events, "deleted_ids": deleted_ids}

    def storage_audit(self, upload_dir: Path) -> dict[str, object]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(size_bytes), 0) AS total "
                "FROM files WHERE purged_at IS NULL"
            ).fetchone()
            file_count = int(row["cnt"])
            total_bytes = int(row["total"])

            largest_rows = connection.execute(
                "SELECT * FROM files WHERE purged_at IS NULL "
                "ORDER BY size_bytes DESC LIMIT 5"
            ).fetchall()
            largest_files = [self._file_payload(r) for r in largest_rows]

            audit_rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT 200"
            ).fetchall()
            audit_events = [dict(r) for r in audit_rows]

        return {
            "file_count": file_count,
            "total_bytes": total_bytes,
            "total_size": format_size(total_bytes),
            "largest_files": largest_files,
            "audit_events": audit_events,
        }

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")

    def list_messages(self, before: str | None, limit: int) -> dict[str, object]:
        self._validate_limit(limit)
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT m.* FROM messages AS m "
                "WHERE m.deleted_at IS NULL "
                "AND (? IS NULL OR (m.created_at, m.id) < "
                "(SELECT created_at, id FROM messages WHERE id = ?)) "
                "ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
                (before, before, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            file_ids = [row["file_id"] for row in page if row["file_id"] is not None]
            file_cache = self._batch_load_files(connection, file_ids)
            items = [self._build_message_payload(row, file_cache) for row in reversed(page)]
            return {
                "items": items,
                "next_before": page[-1]["id"] if has_more else None,
            }

    def _build_message_payload(
        self, row: sqlite3.Row, file_cache: dict[str, dict[str, object]]
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": row["id"],
            "kind": row["kind"],
            "body": row["body"],
            "file_id": row["file_id"],
            "client_request_id": row["client_request_id"],
            "device_id": row["device_id"],
            "device_name": row["device_name"],
            "created_at": row["created_at"],
            "deleted_at": row["deleted_at"],
            "file": None,
        }
        if row["file_id"] is not None:
            payload["file"] = file_cache.get(row["file_id"])
        return payload

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def search(
        self, query: str, cursor: str | None, limit: int
    ) -> dict[str, object]:
        self._validate_limit(limit)
        normalized = query.strip()
        if not normalized:
            return {"items": [], "next_cursor": None}

        pattern = f"%{self._escape_like(normalized)}%"
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT m.* FROM messages AS m "
                "LEFT JOIN files AS f ON f.id = m.file_id "
                "WHERE m.deleted_at IS NULL "
                "AND (m.body LIKE ? ESCAPE '\\' OR f.original_name LIKE ? ESCAPE '\\') "
                "AND (? IS NULL OR (m.created_at, m.id) < "
                "(SELECT created_at, id FROM messages WHERE id = ?)) "
                "ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
                (pattern, pattern, cursor, cursor, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            file_ids = [row["file_id"] for row in page if row["file_id"] is not None]
            file_cache = self._batch_load_files(connection, file_ids)
            items = [self._build_message_payload(row, file_cache) for row in reversed(page)]
            return {
                "items": items,
                "next_cursor": page[-1]["id"] if has_more else None,
            }
