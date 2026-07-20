from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.main import create_app


class MutableClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        upload_dir=tmp_path / "uploads",
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt", ".md"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
        upload_chunk_size_bytes=1024,
        event_retention_limit=100,
    )


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def clocked_client(settings: Settings, clock: MutableClock) -> TestClient:
    app = create_app(settings)
    app.state.clock = clock
    client = TestClient(app)
    response = client.post(
        "/api/session",
        json={
            "access_token": settings.auth_token,
            "device_id": "test-device",
            "device_name": "Test device",
        },
    )
    assert response.status_code == 200
    return client
