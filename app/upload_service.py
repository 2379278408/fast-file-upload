from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from .auth import SessionData
from .chunk_storage import ChunkStorage, PartIntegrityError
from .config import Settings
from .storage import FileStorage, PendingFile, sanitize_filename
from .upload_repository import (
    PartLease,
    PartRecord,
    UploadCreate,
    UploadNotFound,
    UploadRepository,
    UploadStateConflict,
)


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CONTENT_RANGE_PATTERN = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")


class UploadLockPool(Protocol):
    def hold(self, key: str): ...
    def reserve(self, key: str): ...


class InvalidUploadPart(ValueError):
    pass


class UploadTooLarge(ValueError):
    pass


class UploadStorageExceeded(RuntimeError):
    pass


def parse_content_range(
    value: str, expected_total: int, part_index: int, chunk_size: int
) -> tuple[int, int, int]:
    if part_index < 0 or chunk_size <= 0:
        raise InvalidUploadPart("Invalid upload part index")
    match = CONTENT_RANGE_PATTERN.fullmatch(value)
    if match is None:
        raise InvalidUploadPart("Invalid Content-Range")
    start, end, total = (int(item) for item in match.groups())
    expected_start = part_index * chunk_size
    if expected_start >= expected_total:
        raise InvalidUploadPart("Upload part index is outside the file")
    expected_end = min(expected_total - 1, expected_start + chunk_size - 1)
    if total != expected_total or start != expected_start or end != expected_end:
        raise InvalidUploadPart("Content-Range does not match the upload part")
    size = end - start + 1
    if size <= 0:
        raise InvalidUploadPart("Invalid Content-Range size")
    return start, end, size


class UploadService:
    def __init__(
        self,
        repository: UploadRepository,
        chunks: ChunkStorage,
        settings: Settings,
        upload_locks: UploadLockPool,
        storage: FileStorage,
    ) -> None:
        self.repository = repository
        self.chunks = chunks
        self.settings = settings
        self.upload_locks = upload_locks
        self.storage = storage
        self._writers: dict[str, int] = {}
        self._assembly_slots = asyncio.Semaphore(
            settings.max_concurrent_chunk_handlers
        )

    async def _assemble(
        self, session: dict[str, object], parts: list[PartRecord]
    ) -> PendingFile:
        async with self._assembly_slots:
            operation = asyncio.create_task(
                asyncio.to_thread(self.chunks.assemble, session, parts)
            )
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                try:
                    await operation
                except BaseException:
                    pass
                raise

    def _discard_unpublished_final(self, session: dict[str, object]) -> None:
        if session["publication_state"] != "file_published":
            return
        upload_id = str(session["upload_id"])
        storage_name = (
            f"{upload_id}_{sanitize_filename(str(session['original_name']))}"
        )
        self.storage.purge_file(storage_name)

    def _result(self, session: dict[str, object]) -> dict[str, object]:
        result = dict(session)
        result["confirmed_parts"] = [
            part.part_index
            for part in self.repository.list_parts(str(session["upload_id"]))
        ]
        return result

    def _authorized(
        self, upload_id: str, device: SessionData, *, source_only: bool = False
    ) -> dict[str, object]:
        session = self.repository.get(upload_id)
        if session is None:
            raise UploadNotFound(upload_id)
        if source_only and session["source_device_id"] != device.device_id:
            raise UploadStateConflict("Only the source device can upload chunks")
        return session

    def create(
        self, command: UploadCreate, device: SessionData, now: datetime
    ) -> dict[str, object]:
        sanitized_name = sanitize_filename(command.original_name)
        if (
            not command.original_name.strip()
            or (
                sanitized_name == "unnamed-file"
                and command.original_name.strip() != "unnamed-file"
            )
        ):
            raise ValueError("Upload name must contain valid filename characters")
        effective = replace(
            command,
            original_name=sanitized_name,
            source_device_id=device.device_id,
            source_device_name=device.device_name,
        )
        replay = self.repository.get_by_client_request(effective)
        if replay is not None:
            return self._result(replay)
        if command.size_bytes > self.settings.max_upload_size:
            raise UploadTooLarge("Upload exceeds the configured size limit")
        try:
            free_bytes = shutil.disk_usage(Path(self.settings.upload_dir)).free
        except OSError as error:
            raise UploadStorageExceeded("Storage capacity check failed") from error
        if free_bytes - self.settings.upload_storage_reserve_bytes < command.size_bytes:
            raise UploadStorageExceeded("Insufficient storage capacity")
        extension = Path(sanitized_name).suffix.lower()
        if self.settings.allowed_extensions and extension not in self.settings.allowed_extensions:
            allowed = ", ".join(sorted(self.settings.allowed_extensions))
            raise ValueError(f"File type not allowed. Allowed types: {allowed}")
        session, _ = self.repository.create_or_get(
            effective,
            now,
            self.settings.upload_session_ttl_seconds,
            self.settings.max_active_upload_sessions,
        )
        return self._result(session)

    async def put_part(
        self,
        upload_id: str,
        part_index: int,
        content_range: str,
        chunk_sha256: str,
        chunks: AsyncIterator[bytes],
        device: SessionData,
        now: datetime,
        on_bytes: Callable[[int], Awaitable[None]] | None = None,
    ) -> dict[str, object]:
        if SHA256_PATTERN.fullmatch(chunk_sha256) is None:
            raise InvalidUploadPart("Invalid chunk SHA-256")
        async with self.upload_locks.reserve(upload_id) as reservation:
            writer_registered = False
            cancelled_conflict = False
            try:
                async with reservation.hold():
                    session = self._authorized(upload_id, device, source_only=True)
                    start, end, size = parse_content_range(
                        content_range,
                        int(session["size_bytes"]),
                        part_index,
                        int(session["chunk_size_bytes"]),
                    )
                    lease = self.repository.begin_part(
                        upload_id, part_index, start, end, size, chunk_sha256
                    )
                    if isinstance(lease, dict):
                        return self._result(lease)
                    self._writers[upload_id] = self._writers.get(upload_id, 0) + 1
                    writer_registered = True

                stored = await self.chunks.write_part(
                    upload_id, part_index, chunks, size, chunk_sha256, on_bytes
                )

                async with reservation.hold():
                    current = self.repository.get(upload_id)
                    if current is None or current["status"] in {"cancelled", "expired"}:
                        cancelled_conflict = True
                        try:
                            await asyncio.to_thread(
                                self.chunks.discard_part, upload_id, part_index
                            )
                        except OSError:
                            pass
                        raise UploadStateConflict("Upload no longer accepts chunks")
                    confirmed = self.repository.confirm_part(
                        lease,
                        PartRecord(
                            upload_id,
                            part_index,
                            start,
                            end,
                            stored.size_bytes,
                            stored.sha256,
                            now.isoformat(),
                        ),
                        now,
                        self.settings.upload_session_ttl_seconds,
                    )
                    return self._result(confirmed)
            except OSError as error:
                raise UploadStorageExceeded("Upload storage operation failed") from error
            finally:
                if writer_registered:
                    async with reservation.hold():
                        remaining = self._writers[upload_id] - 1
                        if remaining:
                            self._writers[upload_id] = remaining
                        else:
                            del self._writers[upload_id]
                            current = self.repository.get(upload_id)
                            if current is not None and current["status"] == "cancelled":
                                try:
                                    await asyncio.to_thread(
                                        self.chunks.cleanup_session, upload_id
                                    )
                                except OSError as error:
                                    if not cancelled_conflict:
                                        raise UploadStorageExceeded(
                                            "Upload cleanup failed"
                                        ) from error

    def control(
        self,
        upload_id: str,
        action: Literal["pause", "resume"],
        device: SessionData,
        now: datetime,
    ) -> dict[str, object]:
        self._authorized(upload_id, device)
        return self._result(
            self.repository.transition(
                upload_id,
                action,
                device.device_id,
                now,
                self.settings.upload_session_ttl_seconds,
            )
        )

    def cancel(
        self, upload_id: str, device: SessionData, now: datetime
    ) -> dict[str, object]:
        self._authorized(upload_id, device)
        session, _ = self.repository.cancel(
            upload_id, now, self.settings.upload_session_ttl_seconds
        )
        try:
            self._discard_unpublished_final(session)
        except OSError as error:
            raise UploadStorageExceeded("Upload cleanup failed") from error
        if self._writers.get(upload_id, 0) == 0:
            try:
                self.chunks.cleanup_session(upload_id)
            except OSError as error:
                raise UploadStorageExceeded("Upload cleanup failed") from error
        return self._result(session)

    def get(self, upload_id: str, device: SessionData) -> dict[str, object]:
        return self._result(self._authorized(upload_id, device))

    def list_active(self, device: SessionData) -> list[dict[str, object]]:
        return [self._result(session) for session in self.repository.list_active()]

    async def complete(
        self, upload_id: str, device: SessionData, now: datetime
    ) -> dict[str, object]:
        async with self.upload_locks.hold(upload_id):
            session = self._authorized(upload_id, device, source_only=True)
            if session["status"] == "complete":
                return self.repository.get_completed_message(upload_id)
            session = self.repository.begin_completion(
                upload_id, now, self.settings.upload_session_ttl_seconds
            )
            parts = self.repository.list_parts(upload_id)

        try:
            pending = await self._assemble(session, parts)
        except PartIntegrityError:
            async with self.upload_locks.hold(upload_id):
                current = self.repository.get(upload_id)
                if current is not None and current["status"] not in {
                    "cancelled",
                    "expired",
                }:
                    self.repository.fail(
                        upload_id,
                        "integrity_error",
                        now,
                        self.settings.upload_session_ttl_seconds,
                    )
            raise

        async with self.upload_locks.hold(upload_id):
            current = self.repository.get(upload_id)
            if current is None or current["status"] in {"cancelled", "expired"}:
                await asyncio.to_thread(self.chunks.discard_assembled, upload_id)
                raise UploadStateConflict("Upload was cancelled during verification")
            self.repository.set_publication_state(
                upload_id,
                "assembled",
                pending.sha256,
                now,
                self.settings.upload_session_ttl_seconds,
            )
            await asyncio.to_thread(self.storage.publish, pending)
            self.repository.set_publication_state(
                upload_id,
                "file_published",
                pending.sha256,
                now,
                self.settings.upload_session_ttl_seconds,
            )
            self.repository.finalize_publication(upload_id, pending, now)
            message = self.repository.get_completed_message(upload_id)
        try:
            await asyncio.to_thread(self.chunks.cleanup_session, upload_id)
        except OSError:
            pass
        return message

    def recover(self, now: datetime) -> list[dict[str, object]]:
        sessions = self.repository.list_sessions()
        session_ids = {str(session["upload_id"]) for session in sessions}
        reconciled = self.chunks.reconcile(
            session_ids, self.repository.confirmed_part_keys()
        )
        for orphan_id in reconciled.orphan_sessions:
            self.chunks.cleanup_session(orphan_id)
        self.repository.reconcile_missing_parts(
            reconciled.missing_confirmed,
            now,
            self.settings.upload_session_ttl_seconds,
        )

        mutations: list[dict[str, object]] = []
        for original in self.repository.list_sessions():
            upload_id = str(original["upload_id"])
            state = str(original["publication_state"])
            status = str(original["status"])
            if status == "complete":
                continue
            if status in {"cancelled", "expired"}:
                try:
                    self._discard_unpublished_final(original)
                    self.chunks.cleanup_session(upload_id)
                except OSError:
                    pass
                continue
            if state == "assembling":
                self.chunks.discard_assembled(upload_id)
                self.repository.reset_assembling(upload_id, now)
                continue
            if state == "published":
                self.repository.finish_published(upload_id, now)
                continue
            try:
                if state == "assembled":
                    try:
                        pending = self.chunks.pending_from_session(
                            original, published=True
                        )
                    except PartIntegrityError:
                        pending = self.chunks.pending_from_session(
                            original, published=False
                        )
                        self.storage.publish(pending)
                    original = self.repository.set_publication_state(
                        upload_id,
                        "file_published",
                        pending.sha256,
                        now,
                        self.settings.upload_session_ttl_seconds,
                    )
                    state = "file_published"
                if state == "file_published":
                    pending = self.chunks.pending_from_session(
                        original, published=True
                    )
                    mutation = self.repository.finalize_publication(
                        upload_id, pending, now
                    )
                    mutations.append(mutation)
                    try:
                        self.chunks.cleanup_session(upload_id)
                    except OSError:
                        pass
            except (OSError, PartIntegrityError, UploadStateConflict):
                self.repository.fail(
                    upload_id,
                    "publication_error",
                    now,
                    self.settings.upload_session_ttl_seconds,
                )
        return mutations

    def expire(self, now: datetime) -> list[dict[str, object]]:
        expired_before_recovery = self.repository.expired_ids(now)
        mutations = self.recover(now)
        mutations.extend(
            self.repository.expire_mutations(
                now, force_ids=expired_before_recovery
            )
        )
        for mutation in mutations:
            result = mutation["result"]
            if isinstance(result, dict) and result["status"] == "expired":
                upload_id = str(result["upload_id"])
                if self._writers.get(upload_id, 0) == 0:
                    try:
                        self.chunks.cleanup_session(upload_id)
                    except OSError:
                        pass
        return mutations
