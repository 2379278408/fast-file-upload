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
from .chunk_storage import ChunkStorage
from .config import Settings
from .storage import sanitize_filename
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
    ) -> None:
        self.repository = repository
        self.chunks = chunks
        self.settings = settings
        self.upload_locks = upload_locks
        self._writers: dict[str, int] = {}

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
