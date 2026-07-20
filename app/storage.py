from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from time import time

from fastapi import HTTPException, UploadFile


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
FILE_ID_PATTERN = re.compile(r"^(?:[0-9a-f]{12}|[0-9a-f]{32})$")
MAX_DISPLAY_NAME_LENGTH = 120


def format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\s.\-()_\u4e00-\u9fff]", "", name.strip())
    cleaned = re.sub(r" *[\t\n\r\f\v]+ *", " ", cleaned)
    cleaned = cleaned or "unnamed-file"
    if len(cleaned) <= MAX_DISPLAY_NAME_LENGTH:
        return cleaned
    suffix = Path(cleaned).suffix
    stem_limit = MAX_DISPLAY_NAME_LENGTH - len(suffix)
    return f"{Path(cleaned).stem[:stem_limit]}{suffix}"


@dataclass(slots=True)
class StoredFile:
    file_id: str
    display_name: str
    path: Path
    size_bytes: int
    modified_at: float

    @property
    def modified_label(self) -> str:
        return datetime.fromtimestamp(self.modified_at, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    @property
    def extension(self) -> str:
        return self.path.suffix.lower()

    @property
    def media_kind(self) -> str:
        if self.extension in IMAGE_EXTENSIONS:
            return "image"
        return "document"

    @property
    def is_previewable(self) -> bool:
        return self.media_kind == "image"

    @property
    def checksum_sha256(self) -> str:
        digest = sha256()
        with self.path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def to_api(self) -> dict[str, str | int | float]:
        return {
            "id": self.file_id,
            "name": self.display_name,
            "size": format_size(self.size_bytes),
            "size_bytes": self.size_bytes,
            "date": self.modified_label,
            "modified_at": self.modified_at,
            "extension": self.extension,
            "media_kind": self.media_kind,
            "is_previewable": self.is_previewable,
            "sha256": self.checksum_sha256,
            "download_url": f"/download/{self.file_id}",
        }


@dataclass(frozen=True, slots=True)
class PendingFile:
    file_id: str
    original_name: str
    storage_name: str
    temporary_path: Path
    final_path: Path
    mime_type: str
    extension: str
    size_bytes: int
    sha256: str


class FileStorage:
    def __init__(self, upload_dir: Path, max_upload_size: int, allowed_extensions: set[str]) -> None:
        self.upload_dir = upload_dir
        self.max_upload_size = max_upload_size
        self.allowed_extensions = allowed_extensions
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    def list_files(self) -> list[StoredFile]:
        files: list[StoredFile] = []
        for path in self.upload_dir.iterdir():
            if not path.is_file():
                continue
            file_id, _, display_name = path.name.partition("_")
            if not FILE_ID_PATTERN.fullmatch(file_id) or not display_name:
                continue
            stat = path.stat()
            files.append(
                StoredFile(
                    file_id=file_id,
                    display_name=display_name or path.name,
                    path=path,
                    size_bytes=stat.st_size,
                    modified_at=stat.st_mtime,
                )
            )
        return sorted(files, key=lambda item: item.modified_at, reverse=True)

    def get_file(self, file_id: str) -> StoredFile:
        if not FILE_ID_PATTERN.fullmatch(file_id):
            raise HTTPException(status_code=400, detail="Invalid file id")
        for item in self.list_files():
            if item.file_id == file_id:
                return item
        raise HTTPException(status_code=404, detail="File not found")

    def path_for(self, storage_name: str) -> Path:
        safe_name = Path(storage_name).name
        if not safe_name or safe_name != storage_name:
            raise ValueError("Invalid storage name")
        return self.upload_dir / safe_name

    def delete_file(self, file_id: str) -> None:
        target = self.get_file(file_id)
        target.path.unlink()

    def purge_file(self, storage_name: str) -> None:
        target = self.upload_dir / Path(storage_name).name
        target.unlink(missing_ok=True)

    def has_published_file(self, storage_name: str) -> bool:
        return (self.upload_dir / Path(storage_name).name).is_file()

    def pending_from_reservation(self, reservation: dict[str, object]) -> PendingFile:
        file_id = str(reservation["file_id"])
        storage_name = Path(str(reservation["storage_name"])).name
        return PendingFile(
            file_id=file_id,
            original_name=str(reservation["original_name"]),
            storage_name=storage_name,
            temporary_path=self.upload_dir / f".{file_id}.uploading",
            final_path=self.upload_dir / storage_name,
            mime_type=str(reservation["mime_type"]),
            extension=str(reservation["extension"]),
            size_bytes=int(reservation["size_bytes"]),
            sha256=str(reservation["sha256"]),
        )

    def discard_orphaned_temporary_files(
        self, reserved_file_ids: set[str] | None = None
    ) -> None:
        protected_names = {
            f".{file_id}.uploading" for file_id in (reserved_file_ids or set())
        }
        for path in self.upload_dir.glob(".*.uploading"):
            if path.is_file() and path.name not in protected_names:
                path.unlink(missing_ok=True)

    def stage_upload(self, upload: UploadFile) -> PendingFile:
        safe_name = sanitize_filename(upload.filename or "unnamed-file")
        extension = Path(safe_name).suffix.lower()
        if self.allowed_extensions and extension not in self.allowed_extensions:
            allowed = ", ".join(sorted(self.allowed_extensions))
            raise HTTPException(status_code=400, detail=f"File type not allowed. Allowed: {allowed}")

        file_id = uuid.uuid4().hex
        final_path = self.upload_dir / f"{file_id}_{safe_name}"
        temporary_path = self.upload_dir / f".{file_id}.uploading"
        digest = sha256()
        written = 0
        upload.file.seek(0)
        try:
            with temporary_path.open("xb") as output:
                while True:
                    chunk = upload.file.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > self.max_upload_size:
                        limit_mb = self.max_upload_size // (1024 * 1024)
                        raise HTTPException(
                            status_code=413,
                            detail=f"File too large. Max {limit_mb} MB",
                        )
                    digest.update(chunk)
                    output.write(chunk)

            if written == 0:
                raise HTTPException(status_code=400, detail="Empty files are not allowed")
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

        return PendingFile(
            file_id=file_id,
            original_name=safe_name,
            storage_name=final_path.name,
            temporary_path=temporary_path,
            final_path=final_path,
            mime_type=upload.content_type or "application/octet-stream",
            extension=extension,
            size_bytes=written,
            sha256=digest.hexdigest(),
        )

    def publish(self, pending: PendingFile) -> None:
        pending.temporary_path.replace(pending.final_path)

    def discard(self, pending: PendingFile) -> None:
        pending.temporary_path.unlink(missing_ok=True)

    def stats(self) -> dict[str, str | int]:
        files = self.list_files()
        total_bytes = sum(item.size_bytes for item in files)
        return {
            "file_count": len(files),
            "total_bytes": total_bytes,
            "total_size": format_size(total_bytes),
            "max_upload_size": format_size(self.max_upload_size),
        }

    def admin_summary(self) -> dict[str, object]:
        files = self.list_files()
        now = time()
        stale_cutoff = now - (30 * 24 * 60 * 60)
        large_cutoff = self.max_upload_size * 0.5
        largest = sorted(files, key=lambda item: item.size_bytes, reverse=True)[:5]
        return {
            **self.stats(),
            "stale_file_count": sum(1 for item in files if item.modified_at < stale_cutoff),
            "large_file_count": sum(1 for item in files if item.size_bytes >= large_cutoff),
            "largest_files": [item.to_api() for item in largest],
        }
