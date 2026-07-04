from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(slots=True)
class Settings:
    upload_dir: Path
    max_upload_size: int
    allowed_extensions: set[str]
    allowed_origins: list[str]
    auth_token: str | None
    rate_limit_count: int
    rate_limit_window_seconds: int
    retention_days: int
    app_title: str = "Fast File Upload"

    @classmethod
    def from_env(cls, upload_dir: str, max_upload_size: int | None = None) -> "Settings":
        raw_extensions = os.environ.get("ALLOWED_EXTENSIONS", "")
        allowed_extensions = {
            item.lower() if item.startswith(".") else f".{item.lower()}"
            for item in _parse_csv(raw_extensions)
        }
        raw_origins = os.environ.get("ALLOWED_ORIGINS", "*")
        allowed_origins = ["*"] if raw_origins.strip() == "*" else _parse_csv(raw_origins)
        resolved_max_size = max_upload_size or int(os.environ.get("MAX_UPLOAD_SIZE_MB", "512")) * 1024 * 1024
        auth_token = os.environ.get("UPLOAD_TOKEN") or None
        rate_limit_count = int(os.environ.get("RATE_LIMIT_COUNT", "0"))
        rate_limit_window_seconds = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))
        retention_days = int(os.environ.get("RETENTION_DAYS", "0"))
        return cls(
            upload_dir=Path(upload_dir),
            max_upload_size=resolved_max_size,
            allowed_extensions=allowed_extensions,
            allowed_origins=allowed_origins,
            auth_token=auth_token,
            rate_limit_count=rate_limit_count,
            rate_limit_window_seconds=rate_limit_window_seconds,
            retention_days=retention_days,
        )
