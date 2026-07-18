from __future__ import annotations

import base64
import hashlib
import hmac
import sys
from pathlib import Path
from time import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth import LoginRateLimiter, SessionData, decode_session, encode_session
from app.config import ConfigurationError, Settings
from app.main import create_app
import server
from server import build_parser


@pytest.fixture
def protected_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        upload_dir=tmp_path / "uploads",
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
    )
    return TestClient(create_app(settings))


def test_session_codec_rejects_tampering_and_expiry() -> None:
    data = SessionData(device_id="browser-01", device_name="工作电脑", expires_at=200)
    encoded = encode_session(data, "secret")

    assert decode_session(encoded, "secret", now=199) == data
    assert decode_session(encoded, "wrong-secret", now=199) is None
    assert decode_session(f"{encoded[:-1]}0", "secret", now=199) is None
    assert decode_session(encoded, "secret", now=200) is None
    assert decode_session("malformed", "secret", now=199) is None


@pytest.mark.parametrize(
    "value",
    [
        "!!!!.deadbeef",
        "bm90LWpzb24=.deadbeef",
        "e30=.deadbeef",
    ],
)
def test_session_codec_rejects_malformed_payloads(value: str) -> None:
    assert decode_session(value, "secret") is None


def test_session_codec_rejects_signed_non_utf8_payload() -> None:
    payload = base64.urlsafe_b64encode(b"\xff").decode()
    signature = hmac.new(b"secret", payload.encode(), hashlib.sha256).hexdigest()

    assert decode_session(f"{payload}.{signature}", "secret") is None


def test_valid_token_sets_30_day_http_only_session(protected_client: TestClient) -> None:
    response = protected_client.post(
        "/api/session",
        json={
            "access_token": "secret-token",
            "device_id": "browser-01",
            "device_name": "工作电脑",
        },
    )

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "transfer_session=" in cookie
    assert "Max-Age=2592000" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert protected_client.get("/api/session").json()["device_name"] == "工作电脑"


def test_https_session_cookie_is_secure_on_create_and_delete(tmp_path: Path) -> None:
    settings = Settings(
        upload_dir=tmp_path / "uploads",
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=1,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
    )
    client = TestClient(create_app(settings), base_url="https://testserver")

    login = client.post(
        "/api/session",
        json={
            "access_token": "secret-token",
            "device_id": "browser-01",
            "device_name": "工作电脑",
        },
    )
    logout = client.delete("/api/session")

    assert "Secure" in login.headers["set-cookie"]
    assert "Max-Age=2592000" in login.headers["set-cookie"]
    assert "Secure" in logout.headers["set-cookie"]


def test_settings_derive_stable_secret_from_upload_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("UPLOAD_TOKEN", "secret-token")
    expected = hashlib.sha256(
        b"personal-transfer-timeline/session-secret/v1\0secret-token"
    ).hexdigest()

    first = Settings.from_env("uploads")
    second = Settings.from_env("other-uploads")

    assert first.session_secret == expected
    assert second.session_secret == expected


@pytest.mark.parametrize("token", [None, "", "   "])
def test_settings_reject_missing_or_blank_upload_token_in_every_environment(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    if token is None:
        monkeypatch.delenv("UPLOAD_TOKEN", raising=False)
    else:
        monkeypatch.setenv("UPLOAD_TOKEN", token)

    with pytest.raises(ConfigurationError, match="UPLOAD_TOKEN"):
        Settings.from_env("uploads")


@pytest.mark.parametrize("token", ["", "   "])
def test_direct_settings_construction_rejects_blank_upload_token(
    tmp_path: Path, token: str
) -> None:
    with pytest.raises(ConfigurationError, match="UPLOAD_TOKEN"):
        Settings(
            upload_dir=tmp_path / "uploads",
            database_path=tmp_path / "timeline.sqlite3",
            session_secret="test-session-secret",
            session_days=30,
            undo_seconds=30,
            max_upload_size=2048,
            allowed_extensions={".txt"},
            allowed_origins=["*"],
            auth_token=token,
            rate_limit_count=0,
            rate_limit_window_seconds=60,
            retention_days=0,
        )


def test_server_main_stops_before_uvicorn_when_upload_token_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UPLOAD_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["server.py"])
    monkeypatch.setattr(
        server.uvicorn,
        "run",
        lambda *args, **kwargs: pytest.fail("uvicorn must not start"),
    )

    with pytest.raises(ConfigurationError, match="UPLOAD_TOKEN"):
        server.main()


def test_login_rate_limiter_is_bounded_and_success_resets_failures() -> None:
    limiter = LoginRateLimiter(max_failures=2, window_seconds=60, max_clients=2)

    limiter.record_failure("client-1", now=1)
    limiter.record_failure("client-1", now=2)
    assert limiter.is_limited("client-1", now=3) is True

    limiter.reset("client-1")
    assert limiter.is_limited("client-1", now=3) is False

    limiter.record_failure("client-2", now=4)
    limiter.record_failure("client-3", now=5)
    assert limiter.tracked_clients == 2


def test_login_endpoint_limits_failures_and_success_resets(
    tmp_path: Path,
) -> None:
    settings = Settings(
        upload_dir=tmp_path / "uploads",
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
        login_rate_limit_count=2,
    )
    client = TestClient(create_app(settings))
    payload = {
        "device_id": "browser-01",
        "device_name": "Work computer",
    }

    for token in ("wrong-1", "wrong-2"):
        assert client.post(
            "/api/session", json={**payload, "access_token": token}
        ).status_code == 401
    assert client.post(
        "/api/session", json={**payload, "access_token": "wrong-3"}
    ).status_code == 429

    assert client.post(
        "/api/session", json={**payload, "access_token": "secret-token"}
    ).status_code == 200
    assert client.post(
        "/api/session", json={**payload, "access_token": "wrong-again"}
    ).status_code == 401


def test_invalid_token_and_missing_session_remain_locked(protected_client: TestClient) -> None:
    response = protected_client.post(
        "/api/session",
        json={
            "access_token": "wrong",
            "device_id": "browser-01",
            "device_name": "手机",
        },
    )

    assert response.status_code == 401
    assert protected_client.get("/api/files").status_code == 401
    assert protected_client.get("/api/messages").status_code == 401
    assert protected_client.get("/api/health").status_code == 401


@pytest.mark.parametrize("device_name", ["", "   ", "x" * 41])
def test_device_name_validation_returns_422(
    protected_client: TestClient, device_name: str
) -> None:
    response = protected_client.post(
        "/api/session",
        json={
            "access_token": "secret-token",
            "device_id": "browser-01",
            "device_name": device_name,
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "device_id",
    ["", "x" * 129, "contains spaces", "slash/not-allowed"],
)
def test_device_id_validation_returns_422(
    protected_client: TestClient, device_id: str
) -> None:
    response = protected_client.post(
        "/api/session",
        json={
            "access_token": "secret-token",
            "device_id": device_id,
            "device_name": "Work computer",
        },
    )

    assert response.status_code == 422


def test_expired_cookie_is_rejected(protected_client: TestClient) -> None:
    expired = SessionData(
        device_id="browser-01",
        device_name="工作电脑",
        expires_at=int(time()) - 1,
    )
    value = encode_session(expired, "test-session-secret")
    protected_client.cookies.set("transfer_session", value)

    response = protected_client.get("/api/files")

    assert response.status_code == 401
    assert response.json() == {"detail": "Session required"}


def test_logout_clears_cookie_and_locks_resources(protected_client: TestClient) -> None:
    login = protected_client.post(
        "/api/session",
        json={
            "access_token": "secret-token",
            "device_id": "browser-01",
            "device_name": "工作电脑",
        },
    )
    assert login.status_code == 200

    logout = protected_client.delete("/api/session")

    assert logout.status_code == 200
    assert "transfer_session=" in logout.headers["set-cookie"]
    assert "Max-Age=0" in logout.headers["set-cookie"]
    assert protected_client.get("/api/files").status_code == 401


def test_server_host_defaults_to_loopback_and_accepts_override() -> None:
    parser = build_parser()

    assert parser.parse_args([]).host == "127.0.0.1"
    assert parser.parse_args(["--host", "0.0.0.0"]).host == "0.0.0.0"


@pytest.mark.parametrize("after", ["-1", "1.5", "abc", ""])
def test_unauthenticated_websocket_always_closes_4401_before_cursor_validation(
    protected_client: TestClient, after: str
) -> None:
    with pytest.raises(WebSocketDisconnect) as caught:
        with protected_client.websocket_connect(f"/api/events?after={after}") as websocket:
            websocket.receive_text()

    assert caught.value.code == 4401


@pytest.mark.parametrize("after", ["-1", "1.5", "abc", ""])
def test_authenticated_websocket_rejects_invalid_after_cursor_with_1008(
    protected_client: TestClient, after: str
) -> None:
    login = protected_client.post(
        "/api/session",
        json={
            "access_token": "secret-token",
            "device_id": "browser-01",
            "device_name": "Work computer",
        },
    )
    assert login.status_code == 200

    with pytest.raises(WebSocketDisconnect) as caught:
        with protected_client.websocket_connect(f"/api/events?after={after}") as websocket:
            websocket.receive_text()

    assert caught.value.code == 1008


def test_websocket_invalid_session_close_code_remains_4401(
    protected_client: TestClient,
) -> None:
    with pytest.raises(WebSocketDisconnect) as caught:
        with protected_client.websocket_connect("/api/events?after=0") as websocket:
            websocket.receive_text()

    assert caught.value.code == 4401
