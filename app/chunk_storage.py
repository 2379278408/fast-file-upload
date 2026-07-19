from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .storage import PendingFile, sanitize_filename
from .upload_repository import PartRecord


UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class ChunkSizeMismatch(ValueError):
    pass


class ChunkDigestMismatch(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredPart:
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class StorageReconcileResult:
    missing_confirmed: set[tuple[str, int]]
    orphan_sessions: set[str]


def _validate_key(upload_id: str, part_index: int) -> None:
    if (
        not isinstance(upload_id, str)
        or UPLOAD_ID_PATTERN.fullmatch(upload_id) is None
        or not isinstance(part_index, int)
        or isinstance(part_index, bool)
        or part_index < 0
    ):
        raise ValueError("Invalid upload storage key")


class ChunkStorage:
    def __init__(self, upload_dir: Path, buffer_size: int = 64 * 1024) -> None:
        if buffer_size <= 0:
            raise ValueError("buffer_size must be positive")
        self.upload_dir = Path(upload_dir)
        self.buffer_size = buffer_size
        self.resumable_dir = self.upload_dir / ".resumable"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        if self.resumable_dir.is_symlink():
            raise ValueError("Resumable storage cannot be a symbolic link")
        self.resumable_dir.mkdir(exist_ok=True)

    def _session_dir(self, upload_id: str) -> Path:
        _validate_key(upload_id, 0)
        session_dir = self.resumable_dir / upload_id
        if session_dir.is_symlink():
            raise ValueError("Upload session cannot be a symbolic link")
        return session_dir

    def part_path(self, upload_id: str, part_index: int) -> Path:
        _validate_key(upload_id, part_index)
        return self._session_dir(upload_id) / f"part-{part_index:06d}"

    def incoming_path(self, upload_id: str, part_index: int) -> Path:
        _validate_key(upload_id, part_index)
        return self._session_dir(upload_id) / f"incoming-{part_index:06d}"

    async def write_part(
        self,
        upload_id: str,
        part_index: int,
        chunks: AsyncIterator[bytes],
        expected_size: int,
        expected_sha256: str,
        on_bytes: Callable[[int], Awaitable[None]] | None = None,
    ) -> StoredPart:
        if expected_size < 0:
            raise ValueError("expected_size must be non-negative")
        incoming = self.incoming_path(upload_id, part_index)
        confirmed = self.part_path(upload_id, part_index)
        incoming.parent.mkdir(parents=True, exist_ok=True)
        if incoming.parent.is_symlink():
            raise ValueError("Upload session cannot be a symbolic link")
        digest = sha256()
        written = 0
        output = None
        try:
            output = incoming.open("xb")
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("Upload chunks must be bytes")
                await asyncio.to_thread(output.write, chunk)
                written += len(chunk)
                digest.update(chunk)
                if on_bytes is not None:
                    await on_bytes(len(chunk))
            await asyncio.to_thread(output.flush)
            await asyncio.to_thread(os.fsync, output.fileno())
            await asyncio.to_thread(output.close)
            output = None
            if written != expected_size:
                raise ChunkSizeMismatch(f"Expected {expected_size} bytes, received {written}")
            actual_sha256 = digest.hexdigest()
            if actual_sha256 != expected_sha256:
                raise ChunkDigestMismatch("Chunk SHA-256 mismatch")
            await asyncio.to_thread(incoming.replace, confirmed)
            return StoredPart(confirmed, written, actual_sha256)
        finally:
            if output is not None:
                await asyncio.to_thread(output.close)
            if incoming.exists() or incoming.is_symlink():
                await asyncio.to_thread(incoming.unlink, missing_ok=True)

    def assemble(
        self, session: Mapping[str, object], parts: Sequence[PartRecord]
    ) -> PendingFile:
        upload_id = str(session["upload_id"])
        session_dir = self._session_dir(upload_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = session_dir / "final.uploading"
        digest = sha256()
        written = 0
        ordered = sorted(parts, key=lambda part: part.part_index)
        if [part.part_index for part in ordered] != list(range(len(ordered))):
            raise ValueError("Upload parts must be contiguous and unique")
        try:
            with temporary_path.open("xb") as output:
                for part in ordered:
                    if part.upload_id != upload_id:
                        raise ValueError("Part belongs to a different upload")
                    source_path = self.part_path(upload_id, part.part_index)
                    if source_path.is_symlink():
                        raise ValueError("Upload part cannot be a symbolic link")
                    with source_path.open("rb") as source:
                        while chunk := source.read(self.buffer_size):
                            output.write(chunk)
                            digest.update(chunk)
                            written += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            expected_size = int(session["size_bytes"])
            if written != expected_size:
                raise ChunkSizeMismatch(
                    f"Expected {expected_size} assembled bytes, received {written}"
                )
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

        safe_name = sanitize_filename(str(session["original_name"]))
        return PendingFile(
            file_id=upload_id,
            original_name=safe_name,
            storage_name=f"{upload_id}_{safe_name}",
            temporary_path=temporary_path,
            final_path=self.upload_dir / f"{upload_id}_{safe_name}",
            mime_type=str(session["mime_type"]),
            extension=Path(str(session["original_name"])).suffix.lower(),
            size_bytes=written,
            sha256=digest.hexdigest(),
        )

    def discard_incoming(self, upload_id: str, part_index: int) -> None:
        self.incoming_path(upload_id, part_index).unlink(missing_ok=True)

    def discard_part(self, upload_id: str, part_index: int) -> None:
        self.part_path(upload_id, part_index).unlink(missing_ok=True)

    def cleanup_session(self, upload_id: str) -> None:
        session_dir = self._session_dir(upload_id)
        if not session_dir.exists():
            return
        for child in session_dir.iterdir():
            if child.is_dir() and not child.is_symlink():
                raise ValueError("Unexpected directory in upload session")
            child.unlink()
        session_dir.rmdir()

    def reconcile(
        self,
        session_ids: set[str],
        confirmed: set[tuple[str, int]],
    ) -> StorageReconcileResult:
        for upload_id in session_ids:
            _validate_key(upload_id, 0)
        for upload_id, part_index in confirmed:
            _validate_key(upload_id, part_index)

        orphan_sessions: set[str] = set()
        for session_dir in self.resumable_dir.iterdir():
            if session_dir.is_symlink() or not session_dir.is_dir():
                continue
            if UPLOAD_ID_PATTERN.fullmatch(session_dir.name) is None:
                continue
            if session_dir.name not in session_ids:
                orphan_sessions.add(session_dir.name)
            for incoming in session_dir.glob("incoming-*"):
                if incoming.is_file() or incoming.is_symlink():
                    incoming.unlink(missing_ok=True)

        missing_confirmed = {
            key for key in confirmed if not self.part_path(*key).is_file()
            or self.part_path(*key).is_symlink()
        }
        return StorageReconcileResult(missing_confirmed, orphan_sessions)
