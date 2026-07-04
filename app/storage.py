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
FILE_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")
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

    def delete_file(self, file_id: str) -> None:
        target = self.get_file(file_id)
        target.path.unlink()

    def prune_expired(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = time() - (retention_days * 24 * 60 * 60)
        removed = 0
        for item in self.list_files():
            if item.modified_at >= cutoff:
                continue
            item.path.unlink(missing_ok=True)
            removed += 1
        return removed

    def save_upload(self, upload: UploadFile) -> StoredFile:
        safe_name = sanitize_filename(upload.filename or "unnamed-file")
        extension = Path(safe_name).suffix.lower()
        if self.allowed_extensions and extension not in self.allowed_extensions:
            allowed = ", ".join(sorted(self.allowed_extensions))
            raise HTTPException(status_code=400, detail=f"File type not allowed. Allowed: {allowed}")

        file_id = uuid.uuid4().hex[:12]
        destination = self.upload_dir / f"{file_id}_{safe_name}"
        written = 0
        upload.file.seek(0)
        with destination.open("wb") as output:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > self.max_upload_size:
                    output.close()
                    destination.unlink(missing_ok=True)
                    limit_mb = self.max_upload_size // (1024 * 1024)
                    raise HTTPException(status_code=413, detail=f"File too large. Max {limit_mb} MB")
                output.write(chunk)

        if written == 0:
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Empty files are not allowed")

        stat = destination.stat()
        return StoredFile(
            file_id=file_id,
            display_name=safe_name,
            path=destination,
            size_bytes=stat.st_size,
            modified_at=stat.st_mtime,
        )

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
