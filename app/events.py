from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from collections.abc import Callable
from time import monotonic
from typing import Awaitable

from starlette.websockets import WebSocket, WebSocketDisconnect


logger = logging.getLogger("transfer.events")


class UploadProgressPublisher:
    def __init__(
        self,
        interval_seconds: float,
        persist: Callable[[str, dict[str, object]], dict[str, object]],
        broadcast: Callable[[dict[str, object]], Awaitable[None]],
        monotonic_clock: Callable[[], float] = monotonic,
        max_pending_uploads: int = 10_000,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.persist = persist
        self.broadcast = broadcast
        self.monotonic_clock = monotonic_clock
        self.max_pending_uploads = max_pending_uploads
        self._last_sent: dict[str, float] = {}
        self._pending: dict[str, dict[str, object]] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def publish(
        self, upload_id: str, payload: dict[str, object], force: bool = False
    ) -> None:
        event: dict[str, object] | None = None
        async with self._lock:
            if self._closed:
                raise RuntimeError("Upload progress publisher is closed")
            now = self.monotonic_clock()
            if (
                upload_id not in self._last_sent
                and len(self._last_sent) >= self.max_pending_uploads
            ):
                inactive = [
                    key for key in self._last_sent if key not in self._pending
                ]
                if not inactive:
                    return
                oldest = min(inactive, key=self._last_sent.__getitem__)
                self._last_sent.pop(oldest, None)
            elapsed = now - self._last_sent.get(upload_id, float("-inf"))
            if force or elapsed >= self.interval_seconds:
                self._pending.pop(upload_id, None)
                timer = self._timers.pop(upload_id, None)
                if timer is not None:
                    timer.cancel()
                self._last_sent[upload_id] = now
                event = self.persist(upload_id, payload)
            else:
                if upload_id not in self._pending and len(self._pending) >= self.max_pending_uploads:
                    return
                self._pending[upload_id] = payload
                if upload_id not in self._timers:
                    delay = self.interval_seconds - elapsed
                    self._timers[upload_id] = asyncio.create_task(
                        self._flush_after(upload_id, delay)
                    )
        if event is not None:
            await self.broadcast(event)

    async def _flush_after(self, upload_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            event: dict[str, object] | None = None
            async with self._lock:
                payload = self._pending.pop(upload_id, None)
                self._timers.pop(upload_id, None)
                if payload is not None and not self._closed:
                    self._last_sent[upload_id] = self.monotonic_clock()
                    event = self.persist(upload_id, payload)
            if event is not None:
                await self.broadcast(event)
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        events: list[dict[str, object]] = []
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            timers = tuple(self._timers.values())
            self._timers.clear()
            for timer in timers:
                timer.cancel()
            for upload_id, payload in self._pending.items():
                events.append(self.persist(upload_id, payload))
            self._pending.clear()
            self._last_sent.clear()
        for timer in timers:
            with suppress(asyncio.CancelledError):
                await timer
        for event in events:
            await self.broadcast(event)


class EventConnection:
    def __init__(
        self,
        websocket: WebSocket,
        after: int,
        fetch_missing: Callable[[int, int], list[dict[str, object]]] | None = None,
    ) -> None:
        self.websocket = websocket
        self.last_sequence = after
        self.replaying = True
        self.pending: dict[int, dict[str, object]] = {}
        self.send_lock = asyncio.Lock()
        self._fetch_missing = fetch_missing
        self._pending_limit = 200

    async def send_replay(self, event: dict[str, object]) -> None:
        sequence = int(event["sequence"])
        async with self.send_lock:
            if sequence <= self.last_sequence:
                return
            await self.websocket.send_json(event)
            self.last_sequence = sequence
            self.pending.pop(sequence, None)

    async def queue_live(self, event: dict[str, object]) -> None:
        sequence = int(event["sequence"])
        async with self.send_lock:
            if sequence <= self.last_sequence:
                return
            self.pending[sequence] = event
            if len(self.pending) > self._pending_limit:
                overflow = sorted(self.pending.keys())[: len(self.pending) - self._pending_limit]
                for key in overflow:
                    del self.pending[key]
                logger.warning("Pending overflow; dropped %d stale events", len(overflow))
            if not self.replaying:
                await self._backfill_and_flush()

    async def finish_replay(self, replay_target: int) -> None:
        async with self.send_lock:
            self.last_sequence = max(self.last_sequence, replay_target)
            self.pending = {
                sequence: event
                for sequence, event in self.pending.items()
                if sequence > self.last_sequence
            }
            self.replaying = False
            await self._flush_pending()
            await self.websocket.send_json(
                {"event_type": "ready", "sequence": self.last_sequence}
            )

    async def _backfill_and_flush(self) -> None:
        gap_start = self.last_sequence + 1
        if gap_start not in self.pending and self._fetch_missing is not None:
            try:
                missing = await asyncio.to_thread(
                    self._fetch_missing, self.last_sequence, self._pending_limit
                )
                for evt in missing:
                    seq = int(evt.get("sequence", 0))
                    if seq > self.last_sequence and seq not in self.pending:
                        self.pending[seq] = evt
            except Exception:
                logger.exception("Failed to backfill missing events")
        await self._flush_pending()

    async def _flush_pending(self) -> None:
        while self.last_sequence + 1 in self.pending:
            sequence = self.last_sequence + 1
            await self.websocket.send_json(self.pending.pop(sequence))
            self.last_sequence = sequence


class EventHub:
    def __init__(self) -> None:
        self.connections: dict[WebSocket, EventConnection] = {}
        self._fetch_missing: Callable[[int, int], list[dict[str, object]]] | None = None

    def set_fetch_missing(
        self, fetcher: Callable[[int, int], list[dict[str, object]]]
    ) -> None:
        self._fetch_missing = fetcher

    def connect(self, websocket: WebSocket, after: int = 0) -> EventConnection:
        connection = EventConnection(websocket, after, self._fetch_missing)
        self.connections[websocket] = connection
        return connection

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.pop(websocket, None)

    async def broadcast(self, event: dict[str, object]) -> None:
        failed: list[WebSocket] = []
        for websocket, connection in tuple(self.connections.items()):
            try:
                await connection.queue_live(event)
            except (WebSocketDisconnect, Exception):
                failed.append(websocket)
        for websocket in failed:
            self.connections.pop(websocket, None)
