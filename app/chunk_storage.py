from __future__ import annotations

import asyncio
import os
import re
import stat
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from .storage import PendingFile, sanitize_filename
from .upload_repository import PartRecord


UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class ChunkSizeMismatch(ValueError):
    pass


class ChunkDigestMismatch(ValueError):
    pass


class PartConflict(ValueError):
    pass


class PartIntegrityError(ValueError):
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

    def _new_incoming_path(self, upload_id: str, part_index: int) -> Path:
        _validate_key(upload_id, part_index)
        return self._session_dir(upload_id) / f"incoming-{part_index:06d}-{uuid4().hex}"

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
        incoming = self._new_incoming_path(upload_id, part_index)
        confirmed = self.part_path(upload_id, part_index)
        await asyncio.to_thread(incoming.parent.mkdir, parents=True, exist_ok=True)
        if incoming.parent.is_symlink():
            raise ValueError("Upload session cannot be a symbolic link")
        digest = sha256()
        written = 0
        output = None
        failure: BaseException | None = None
        result: StoredPart | None = None

        def write_and_hash(piece: bytes) -> None:
            output.write(piece)
            digest.update(piece)

        def confirmed_digest() -> tuple[int, str]:
            existing_digest = sha256()
            existing_size = 0
            descriptor = os.open(confirmed, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source:
                while piece := source.read(self.buffer_size):
                    existing_size += len(piece)
                    existing_digest.update(piece)
            return existing_size, existing_digest.hexdigest()

        try:
            output = await asyncio.to_thread(incoming.open, "xb")
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("Upload chunks must be bytes")
                for offset in range(0, len(chunk), self.buffer_size):
                    piece = chunk[offset : offset + self.buffer_size]
                    if written + len(piece) > expected_size:
                        raise ChunkSizeMismatch(
                            f"Expected {expected_size} bytes, received more data"
                        )
                    await asyncio.to_thread(write_and_hash, piece)
                    written += len(piece)
                    if on_bytes is not None:
                        await on_bytes(len(piece))
            await asyncio.to_thread(output.flush)
            await asyncio.to_thread(os.fsync, output.fileno())
            await asyncio.to_thread(output.close)
            output = None
            if written != expected_size:
                raise ChunkSizeMismatch(f"Expected {expected_size} bytes, received {written}")
            actual_sha256 = digest.hexdigest()
            if actual_sha256 != expected_sha256:
                raise ChunkDigestMismatch("Chunk SHA-256 mismatch")
            try:
                await asyncio.to_thread(os.link, incoming, confirmed)
            except FileExistsError:
                try:
                    existing_size, existing_sha256 = await asyncio.to_thread(confirmed_digest)
                except OSError as exc:
                    raise PartConflict(
                        f"Confirmed part {part_index} cannot be verified"
                    ) from exc
                if (existing_size, existing_sha256) != (written, actual_sha256):
                    raise PartConflict(
                        f"Confirmed part {part_index} has different content"
                    )
            result = StoredPart(confirmed, written, actual_sha256)
        except BaseException as exc:
            failure = exc
        if output is not None:
            try:
                await asyncio.to_thread(output.close)
            except BaseException as exc:
                if failure is None:
                    failure = exc
        try:
            await asyncio.to_thread(incoming.unlink, missing_ok=True)
        except BaseException as exc:
            if failure is None:
                failure = exc
        if failure is not None:
            raise failure
        if result is None:
            raise RuntimeError("Part write ended without a result")
        return result

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
            raise PartIntegrityError("Upload part indexes must be contiguous and unique")
        expected_size = int(session["size_bytes"])
        expected_start = 0
        for part in ordered:
            range_size = part.end_byte - part.start_byte + 1
            if part.upload_id != upload_id:
                raise PartIntegrityError("Part belongs to a different upload")
            if part.start_byte != expected_start or part.end_byte < part.start_byte:
                raise PartIntegrityError("Upload part ranges must be precisely contiguous")
            if part.size_bytes != range_size:
                raise PartIntegrityError("Upload part size does not match its byte range")
            expected_start = part.end_byte + 1
        if expected_start != expected_size:
            raise PartIntegrityError("Upload parts do not cover the complete file range")
        try:
            with temporary_path.open("xb") as output:
                for part in ordered:
                    source_path = self.part_path(upload_id, part.part_index)
                    if source_path.is_symlink():
                        raise PartIntegrityError("Upload part cannot be a symbolic link")
                    part_digest = sha256()
                    part_written = 0
                    try:
                        with source_path.open("rb") as source:
                            while chunk := source.read(self.buffer_size):
                                output.write(chunk)
                                digest.update(chunk)
                                part_digest.update(chunk)
                                part_written += len(chunk)
                                written += len(chunk)
                    except FileNotFoundError as exc:
                        raise PartIntegrityError("Stored upload part is missing") from exc
                    if part_written != part.size_bytes:
                        raise PartIntegrityError("Stored part size differs from its record")
                    if part_digest.hexdigest() != part.sha256:
                        raise PartIntegrityError("Stored part SHA-256 differs from its record")
                output.flush()
                os.fsync(output.fileno())
            if written != expected_size:
                raise PartIntegrityError(
                    f"Expected {expected_size} assembled bytes, received {written}"
                )
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

        safe_name = sanitize_filename(str(session["original_name"]))
        safe_name = " ".join(safe_name.split()) or "unnamed-file"
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
        """Remove stale writers while holding the upload lock.

        This batch cleanup is only safe for cancellation, recovery, or maintenance
        paths that hold the upload lock and have established that no writer is active.
        Active writers clean only their own UUID-derived incoming path.
        """
        _validate_key(upload_id, part_index)
        session_dir = self._session_dir(upload_id)
        if not session_dir.exists():
            return
        prefix = f"incoming-{part_index:06d}-"
        for candidate in session_dir.iterdir():
            if not candidate.name.startswith(prefix) or candidate.name == prefix:
                continue
            try:
                mode = candidate.stat(follow_symlinks=False).st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISREG(mode):
                candidate.unlink(missing_ok=True)

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
