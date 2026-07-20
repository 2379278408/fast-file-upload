from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sqlite3
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from . import safe_fs
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
logger = logging.getLogger("transfer.upload")


class UploadLockPool(Protocol):
    def hold(self, key: str): ...
    def reserve(self, key: str): ...


class InvalidUploadPart(ValueError):
    pass


class UploadTooLarge(ValueError):
    pass


class UploadStorageExceeded(RuntimeError):
    pass


class UploadStorageCapacityExceeded(UploadStorageExceeded):
    def __init__(self, required_bytes: int, free_bytes: int) -> None:
        super().__init__("Insufficient storage capacity")
        self.required_bytes = required_bytes
        self.free_bytes = free_bytes


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
        self._completing: set[str] = set()
        self._completing_lock = threading.Lock()
        self._assembly_slots = asyncio.Semaphore(
            settings.max_concurrent_chunk_handlers
        )

    def _is_completing(self, upload_id: str) -> bool:
        with self._completing_lock:
            return upload_id in self._completing

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
        if session["publication_state"] not in {"assembled", "file_published"}:
            return
        upload_id = str(session["upload_id"])
        safe_name = sanitize_filename(str(session["original_name"]))
        storage_name = f"{upload_id}_{safe_name}"
        try:
            safe_fs.quarantine_verified_file(
                Path(self.settings.upload_dir),
                storage_name,
                expected_size=int(session["size_bytes"]),
                expected_sha256=str(session["file_sha256"]),
            )
        except OSError as error:
            logger.warning(
                "Final upload cleanup deferred upload_id=%s storage_name=%s "
                "isolated_name=%s",
                upload_id,
                storage_name,
                getattr(error, "isolated_name", None),
                exc_info=True,
            )

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
            return {"result": self._result(replay), "events": [], "changed": False}
        if command.size_bytes > self.settings.max_upload_size:
            raise UploadTooLarge("Upload exceeds the configured size limit")
        try:
            free_bytes = shutil.disk_usage(Path(self.settings.upload_dir)).free
        except OSError as error:
            raise UploadStorageExceeded("Storage capacity check failed") from error
        capacity_budget_bytes = free_bytes - self.settings.upload_storage_reserve_bytes
        extension = Path(sanitized_name).suffix.lower()
        if self.settings.allowed_extensions and extension not in self.settings.allowed_extensions:
            allowed = ", ".join(sorted(self.settings.allowed_extensions))
            raise ValueError(f"File type not allowed. Allowed types: {allowed}")
        mutation = self.repository.create_or_get(
            effective,
            now,
            self.settings.upload_session_ttl_seconds,
            self.settings.max_active_upload_sessions,
            include_event=True,
            capacity_budget_bytes=capacity_budget_bytes,
        )
        result = mutation["result"]
        mutation["result"] = self._result(result)
        return mutation

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
        mutation = self.repository.transition(
            upload_id,
            action,
            device.device_id,
            now,
            self.settings.upload_session_ttl_seconds,
            include_event=True,
        )
        mutation["result"] = self._result(mutation["result"])
        return mutation

    def cancel(
        self, upload_id: str, device: SessionData, now: datetime
    ) -> dict[str, object]:
        self._authorized(upload_id, device)
        mutation = self.repository.cancel(
            upload_id,
            now,
            self.settings.upload_session_ttl_seconds,
            include_event=True,
        )
        session = mutation["result"]
        try:
            self._discard_unpublished_final(session)
        except OSError as error:
            logger.warning(
                "Upload cancellation final cleanup failed upload_id=%s",
                upload_id,
                exc_info=error,
            )
        if self._writers.get(upload_id, 0) == 0:
            try:
                self.chunks.cleanup_session(upload_id)
            except OSError as error:
                logger.warning(
                    "Upload cancellation temporary cleanup failed upload_id=%s",
                    upload_id,
                    exc_info=error,
                )
        mutation["result"] = self._result(session)
        return mutation

    def get(self, upload_id: str, device: SessionData) -> dict[str, object]:
        return self._result(self._authorized(upload_id, device))

    def list_active(self, device: SessionData) -> list[dict[str, object]]:
        return [self._result(session) for session in self.repository.list_active()]

    async def complete(
        self,
        upload_id: str,
        device: SessionData,
        now: datetime,
        on_mutation: Callable[[dict[str, object]], Awaitable[None]] | None = None,
    ) -> dict[str, object]:
        with self._completing_lock:
            if upload_id in self._completing:
                raise UploadStateConflict("Upload completion is already in progress")
            self._completing.add(upload_id)
        try:
            phase_mutations: list[dict[str, object]] = []
            async with self.upload_locks.hold(upload_id):
                session = self._authorized(upload_id, device, source_only=True)
                if session["status"] == "complete":
                    return {
                        "result": self.repository.get_completed_message(upload_id),
                        "events": [],
                        "changed": False,
                    }
                if session["status"] == "verifying":
                    phase_mutations = self._continue_publication_locked(
                        upload_id, session, now
                    )
                    session = self.repository.get(upload_id)
                    if session is None:
                        raise UploadNotFound(upload_id)
                    if session["status"] == "complete":
                        return {
                            "result": self.repository.get_completed_message(upload_id),
                            "events": [
                                event
                                for mutation in phase_mutations
                                for event in mutation["events"]
                            ],
                            "changed": any(
                                bool(mutation["changed"])
                                for mutation in phase_mutations
                            ),
                        }
                try:
                    free_bytes = shutil.disk_usage(Path(self.settings.upload_dir)).free
                except OSError as error:
                    raise UploadStorageExceeded("Storage capacity check failed") from error
                required_bytes = int(session["size_bytes"]) + self.settings.upload_storage_reserve_bytes
                if free_bytes < required_bytes:
                    raise UploadStorageCapacityExceeded(required_bytes, free_bytes)
                state_mutation = self.repository.begin_completion(
                    upload_id,
                    now,
                    self.settings.upload_session_ttl_seconds,
                    include_event=True,
                )
                session = state_mutation["result"]
                parts = self.repository.list_parts(upload_id)
            if on_mutation is not None:
                for phase_mutation in phase_mutations:
                    await on_mutation(phase_mutation)
                await on_mutation(state_mutation)
            try:
                pending = await self._assemble(session, parts)
            except PartIntegrityError as error:
                async with self.upload_locks.hold(upload_id):
                    if error.part_index is None:
                        self.repository.fail(
                            upload_id,
                            "integrity_error",
                            now,
                            self.settings.upload_session_ttl_seconds,
                        )
                    else:
                        await asyncio.to_thread(
                            self.chunks.discard_part, upload_id, error.part_index
                        )
                        state_mutation = self.repository.invalidate_part(
                            upload_id,
                            error.part_index,
                            "missing_part"
                            if error.reason == "missing"
                            else "integrity_error",
                            now,
                            self.settings.upload_session_ttl_seconds,
                            include_event=True,
                        )
                if error.part_index is not None and on_mutation is not None:
                    await on_mutation(state_mutation)
                raise
            async with self.upload_locks.hold(upload_id):
                current = self.repository.get(upload_id)
                if current is None or current["status"] in {"cancelled", "expired"}:
                    await asyncio.to_thread(self.chunks.discard_assembled, upload_id)
                    raise UploadStateConflict("Upload was cancelled during verification")
                if (
                    current["status"] != "verifying"
                    or current["publication_state"] != "assembling"
                ):
                    await asyncio.to_thread(self.chunks.discard_assembled, upload_id)
                    raise UploadStateConflict(str(current["status"]))
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
                mutation = self.repository.finalize_publication(
                    upload_id, pending, now
                )
                if on_mutation is None:
                    mutation["events"] = [
                        *(
                            event
                            for phase_mutation in phase_mutations
                            for event in phase_mutation["events"]
                        ),
                        *state_mutation["events"],
                        *mutation["events"],
                    ]
                try:
                    await asyncio.to_thread(self.chunks.cleanup_session, upload_id)
                except OSError:
                    pass
                return self._completed_mutation(upload_id, mutation)
        finally:
            with self._completing_lock:
                self._completing.discard(upload_id)

    def _completed_mutation(
        self, upload_id: str, mutation: dict[str, object]
    ) -> dict[str, object]:
        return {
            **mutation,
            "result": self.repository.get_completed_message(upload_id),
        }

    def _continue_publication_locked(
        self, upload_id: str, session: dict[str, object], now: datetime
    ) -> list[dict[str, object]]:
        state = str(session["publication_state"])
        if state == "assembling":
            self.chunks.discard_assembled(upload_id)
            return [
                self.repository.reset_assembling(
                    upload_id, now, include_event=True
                )
            ]
        if state == "published":
            mutation = self.repository.finish_published(
                upload_id, now, include_event=True
            )
            return [self._completed_mutation(upload_id, mutation)]
        if state == "assembled":
            try:
                pending = self.chunks.pending_from_session(session, published=True)
            except PartIntegrityError:
                pending = self.chunks.pending_from_session(session, published=False)
                self.storage.publish(pending)
            session = self.repository.set_publication_state(
                upload_id,
                "file_published",
                pending.sha256,
                now,
                self.settings.upload_session_ttl_seconds,
            )
            state = "file_published"
        if state == "file_published":
            pending = self.chunks.pending_from_session(session, published=True)
            mutation = self.repository.finalize_publication(upload_id, pending, now)
            try:
                self.chunks.cleanup_session(upload_id)
            except OSError:
                pass
            return [self._completed_mutation(upload_id, mutation)]
        raise UploadStateConflict(f"Unknown publication state {state}")

    def _recover_locked(
        self, upload_id: str, now: datetime
    ) -> list[dict[str, object]]:
        mutations: list[dict[str, object]] = []
        original = self.repository.get(upload_id)
        if original is None:
            return []
        if self._is_completing(upload_id):
            return []
        if self._writers.get(upload_id, 0) > 0:
            return []
        confirmed_indexes = {
            part.part_index for part in self.repository.list_parts(upload_id)
        }
        missing = self.chunks.reconcile_session(upload_id, confirmed_indexes)
        mutations.extend(
            self.repository.reconcile_missing_parts(
                missing,
                now,
                self.settings.upload_session_ttl_seconds,
            )
        )
        original = self.repository.get(upload_id)
        if original is None:
            return []
        state = str(original["publication_state"])
        status = str(original["status"])
        if status == "complete":
            self.chunks.cleanup_session(upload_id)
            return mutations
        if status in {"cancelled", "expired"}:
            try:
                self._discard_unpublished_final(original)
                self.chunks.cleanup_session(upload_id)
            except OSError:
                pass
            return mutations
        if state in {"assembling", "published"}:
            mutations.extend(
                self._continue_publication_locked(upload_id, original, now)
            )
            return mutations
        try:
            if state in {"assembled", "file_published"}:
                mutations.extend(
                    self._continue_publication_locked(upload_id, original, now)
                )
                return mutations
        except PartIntegrityError:
            mutations.append(
                self.repository.fail(
                    upload_id,
                    "publication_error",
                    now,
                    self.settings.upload_session_ttl_seconds,
                    include_event=True,
                    reset_publication=True,
                )
            )
        except (OSError, UploadStateConflict):
            mutations.append(
                self.repository.fail(
                    upload_id,
                    "publication_error",
                    now,
                    self.settings.upload_session_ttl_seconds,
                    include_event=True,
                )
            )
        return mutations

    async def recover(self, now: datetime) -> list[dict[str, object]]:
        sessions = await asyncio.to_thread(self.repository.list_sessions)
        session_ids = {str(session["upload_id"]) for session in sessions}
        orphan_ids = await asyncio.to_thread(self.chunks.orphan_sessions, session_ids)
        for orphan_id in orphan_ids:
            await self._run_isolated(
                orphan_id,
                "recover_orphan_cleanup",
                self.chunks.cleanup_session,
                orphan_id,
            )
        mutations: list[dict[str, object]] = []
        for upload_id in sorted(session_ids):
            recovered = await self._run_isolated(
                upload_id, "recover", self._recover_locked, upload_id, now
            )
            if recovered is not None:
                mutations.extend(recovered)
        return mutations

    async def _run_isolated(
        self,
        upload_id: str,
        phase: str,
        operation: Callable[..., list[dict[str, object]] | None],
        *args: object,
    ) -> list[dict[str, object]] | None:
        try:
            async with self.upload_locks.hold(upload_id):
                task = asyncio.create_task(asyncio.to_thread(operation, *args))
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    try:
                        await task
                    except BaseException:
                        pass
                    raise
        except asyncio.CancelledError:
            raise
        except (
            InvalidUploadPart,
            UploadTooLarge,
            UploadStorageExceeded,
            UploadNotFound,
            UploadStateConflict,
            PartIntegrityError,
            OSError,
            sqlite3.Error,
        ):
            logger.exception(
                "Upload maintenance failed upload_id=%s phase=%s", upload_id, phase
            )
        except Exception:
            logger.exception(
                "Unexpected upload maintenance failure upload_id=%s phase=%s",
                upload_id,
                phase,
            )
        return None

    def _expire_locked(
        self, upload_id: str, now: datetime
    ) -> list[dict[str, object]]:
        if self._is_completing(upload_id):
            return []
        session = self.repository.get(upload_id)
        if session is None or session["status"] not in {
            "queued",
            "uploading",
            "paused",
            "verifying",
            "failed",
        }:
            return []
        if str(session["expires_at"]) > now.isoformat():
            return []
        if self._writers.get(upload_id, 0) > 0:
            return []
        mutations: list[dict[str, object]] = []
        if session["status"] == "verifying":
            mutations.extend(self._recover_locked(upload_id, now))
            current = self.repository.get(upload_id)
            if current is None or current["status"] == "complete":
                return mutations
        expired = self.repository.expire_one(upload_id, now, was_due=True)
        if expired is not None:
            mutations.append(expired)
            try:
                self.chunks.cleanup_session(upload_id)
            except OSError:
                pass
        return mutations

    async def expire(self, now: datetime) -> list[dict[str, object]]:
        upload_ids = await asyncio.to_thread(self.repository.expired_ids, now)
        mutations: list[dict[str, object]] = []
        for upload_id in sorted(upload_ids):
            expired = await self._run_isolated(
                upload_id, "expire", self._expire_locked, upload_id, now
            )
            if expired is not None:
                mutations.extend(expired)
        sessions = await asyncio.to_thread(self.repository.list_sessions)
        residual_ids = await asyncio.to_thread(self.chunks.orphan_sessions, set())
        cleanup_ids = {
            str(session["upload_id"])
            for session in sessions
            if session["status"] in {"cancelled", "expired"}
        }
        cleanup_ids.update(
            str(session["upload_id"])
            for session in sessions
            if session["status"] == "complete"
            and str(session["upload_id"]) in residual_ids
        )
        for upload_id in sorted(cleanup_ids):
            await self._run_isolated(
                upload_id,
                "terminal_cleanup",
                self._recover_locked,
                upload_id,
                now,
            )
        return mutations
