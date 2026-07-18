from __future__ import annotations

import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


SESSION_DAYS = 30
_SESSION_SECRET_DOMAIN = b"personal-transfer-timeline/session-secret/v1\0"


class ConfigurationError(RuntimeError):
    """Raised when required server configuration is missing or invalid."""


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _session_secret(auth_token: str) -> str:
    configured = os.environ.get("SESSION_SECRET")
    if configured:
        return configured
    return sha256(_SESSION_SECRET_DOMAIN + auth_token.encode()).hexdigest()


@dataclass(slots=True)
class Settings:
    upload_dir: Path
    database_path: Path
    session_secret: str
    session_days: int
    undo_seconds: int
    max_upload_size: int
    allowed_extensions: set[str]
    allowed_origins: list[str]
    auth_token: str
    rate_limit_count: int
    rate_limit_window_seconds: int
    retention_days: int
    app_title: str = "Personal Transfer Timeline"
    max_batch_download_total_bytes: int = 1024 * 1024 * 1024
    client_request_lock_capacity: int = 1024
    login_rate_limit_count: int = 5
    login_rate_limit_window_seconds: int = 60
    login_rate_limit_max_clients: int = 1024
    maintenance_interval_seconds: float = 60.0
    purge_claim_lease_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.auth_token or not self.auth_token.strip():
            raise ConfigurationError(
                "UPLOAD_TOKEN is required and must contain a non-whitespace value"
            )
        if self.login_rate_limit_count < 1:
            raise ConfigurationError("LOGIN_RATE_LIMIT_COUNT must be at least 1")
        if self.login_rate_limit_window_seconds < 1:
            raise ConfigurationError("LOGIN_RATE_LIMIT_WINDOW_SECONDS must be at least 1")
        if self.login_rate_limit_max_clients < 1:
            raise ConfigurationError("LOGIN_RATE_LIMIT_MAX_CLIENTS must be at least 1")
        if self.maintenance_interval_seconds <= 0:
            raise ConfigurationError("MAINTENANCE_INTERVAL_SECONDS must be positive")
        if self.purge_claim_lease_seconds <= 0:
            raise ConfigurationError("PURGE_CLAIM_LEASE_SECONDS must be positive")
        if self.max_batch_download_total_bytes < 1:
            raise ConfigurationError("MAX_BATCH_DOWNLOAD_TOTAL_BYTES must be at least 1")
        if self.client_request_lock_capacity < 1:
            raise ConfigurationError("CLIENT_REQUEST_LOCK_CAPACITY must be at least 1")

    @classmethod
    def from_env(cls, upload_dir: str, max_upload_size: int | None = None) -> "Settings":
        resolved_upload_dir = Path(upload_dir)
        raw_extensions = os.environ.get("ALLOWED_EXTENSIONS", "")
        allowed_extensions = {
            item.lower() if item.startswith(".") else f".{item.lower()}"
            for item in _parse_csv(raw_extensions)
        }
        raw_origins = os.environ.get("ALLOWED_ORIGINS", "*")
        allowed_origins = ["*"] if raw_origins.strip() == "*" else _parse_csv(raw_origins)
        resolved_max_size = max_upload_size or int(os.environ.get("MAX_UPLOAD_SIZE_MB", "512")) * 1024 * 1024
        auth_token = os.environ.get("UPLOAD_TOKEN", "")
        rate_limit_count = int(os.environ.get("RATE_LIMIT_COUNT", "0"))
        rate_limit_window_seconds = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))
        retention_days = int(os.environ.get("RETENTION_DAYS", "0"))
        return cls(
            upload_dir=resolved_upload_dir,
            database_path=Path(
                os.environ.get(
                    "DATABASE_PATH", str(resolved_upload_dir.parent / "timeline.sqlite3")
                )
            ),
            session_secret=_session_secret(auth_token),
            session_days=SESSION_DAYS,
            undo_seconds=int(os.environ.get("UNDO_SECONDS", "30")),
            max_upload_size=resolved_max_size,
            allowed_extensions=allowed_extensions,
            allowed_origins=allowed_origins,
            auth_token=auth_token,
            rate_limit_count=rate_limit_count,
            rate_limit_window_seconds=rate_limit_window_seconds,
            retention_days=retention_days,
            login_rate_limit_count=int(os.environ.get("LOGIN_RATE_LIMIT_COUNT", "5")),
            login_rate_limit_window_seconds=int(
                os.environ.get("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "60")
            ),
            login_rate_limit_max_clients=int(
                os.environ.get("LOGIN_RATE_LIMIT_MAX_CLIENTS", "1024")
            ),
            maintenance_interval_seconds=float(
                os.environ.get("MAINTENANCE_INTERVAL_SECONDS", "60")
            ),
            purge_claim_lease_seconds=float(
                os.environ.get("PURGE_CLAIM_LEASE_SECONDS", "300")
            ),
            max_batch_download_total_bytes=int(
                os.environ.get("MAX_BATCH_DOWNLOAD_TOTAL_BYTES", str(1024 * 1024 * 1024))
            ),
            client_request_lock_capacity=int(
                os.environ.get("CLIENT_REQUEST_LOCK_CAPACITY", "1024")
            ),
        )
