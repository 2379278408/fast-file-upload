from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import threading
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass

from fastapi import HTTPException, Request


SESSION_COOKIE = "transfer_session"


class LoginRateLimiter:
    def __init__(
        self, max_failures: int, window_seconds: int, max_clients: int
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def tracked_clients(self) -> int:
        with self._lock:
            return len(self._failures)

    def _prune(self, client: str, now: float) -> deque[float]:
        failures = self._failures.get(client, deque())
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if failures:
            self._failures[client] = failures
            self._failures.move_to_end(client)
        else:
            self._failures.pop(client, None)
        return failures

    def is_limited(self, client: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            return len(self._prune(client, current)) >= self.max_failures

    def record_failure(self, client: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            failures = self._prune(client, current)
            if len(failures) >= self.max_failures:
                return True
            if client not in self._failures and len(self._failures) >= self.max_clients:
                self._failures.popitem(last=False)
            failures.append(current)
            self._failures[client] = failures
            self._failures.move_to_end(client)
            return False

    def reset(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


@dataclass(frozen=True, slots=True)
class SessionData:
    device_id: str
    device_name: str
    expires_at: int


def encode_session(data: SessionData, secret: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(asdict(data), separators=(",", ":"), ensure_ascii=False).encode()
    ).decode()
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def decode_session(value: str, secret: str, now: int | None = None) -> SessionData | None:
    try:
        payload, signature = value.rsplit(".", 1)
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        raw = json.loads(base64.urlsafe_b64decode(payload.encode()))
        data = SessionData(**raw)
        current_time = int(time.time()) if now is None else now
        return data if data.expires_at > current_time else None
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        binascii.Error,
    ):
        return None


def require_session(request: Request) -> SessionData:
    settings = request.app.state.settings
    value = request.cookies.get(SESSION_COOKIE)
    data = decode_session(value, settings.session_secret) if value else None
    if data is None:
        raise HTTPException(status_code=401, detail="Session required")
    return data
