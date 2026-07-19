from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
import logging
from time import monotonic
from typing import Awaitable

from starlette.websockets import WebSocket, WebSocketDisconnect


logger = logging.getLogger("transfer.events")


class UploadProgressPublisher:
    BLOCKED_STATUSES = frozenset(
        {"paused", "verifying", "failed", "complete", "cancelled", "expired"}
    )

    def __init__(
        self,
        interval_seconds: float,
        persist: Callable[[str, dict[str, object]], dict[str, object]],
        broadcast: Callable[[dict[str, object]], Awaitable[None]],
        monotonic_clock: Callable[[], float] = monotonic,
        max_pending_uploads: int = 10_000,
        status_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.persist = persist
        self.broadcast = broadcast
        self.monotonic_clock = monotonic_clock
        self.max_pending_uploads = max_pending_uploads
        self.status_lookup = status_lookup
        self._last_sent: dict[str, float] = {}
        self._pending: dict[str, dict[str, object]] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._generation: dict[str, int] = {}
        self._terminal: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def publish(
        self, upload_id: str, payload: dict[str, object], force: bool = False
    ) -> None:
        generation: int | None = None
        async with self._lock:
            if self._closed:
                raise RuntimeError("Upload progress publisher is closed")
            if not self._ensure_capacity(upload_id):
                return
            if not self._accepts_progress(upload_id):
                return
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
            if elapsed >= self.interval_seconds:
                self._last_sent[upload_id] = now
                generation = self._generation.get(upload_id, 0)
            else:
                if upload_id not in self._pending and len(self._pending) >= self.max_pending_uploads:
                    return
                self._pending[upload_id] = payload
                if upload_id not in self._timers:
                    delay = self.interval_seconds - elapsed
                    generation = self._generation.get(upload_id, 0)
                    timer = asyncio.create_task(
                        self._flush_after(upload_id, generation, delay)
                    )
                    self._timers[upload_id] = timer
                    self._track(timer)
                generation = None
        if generation is not None:
            await self._send_if_current(upload_id, generation, payload)

    def _accepts_progress(self, upload_id: str) -> bool:
        if upload_id in self._terminal:
            return False
        if self.status_lookup is None:
            return True
        status = self.status_lookup(upload_id)
        if status is None or status in self.BLOCKED_STATUSES:
            self._terminal.add(upload_id)
            return False
        return True

    def _ensure_capacity(self, upload_id: str) -> bool:
        state_keys = (
            set(self._last_sent)
            | set(self._pending)
            | set(self._timers)
            | set(self._generation)
            | self._terminal
        )
        if upload_id in state_keys or len(state_keys) < self.max_pending_uploads:
            return True
        inactive = [
            key for key in state_keys if key not in self._pending and key not in self._timers
        ]
        if not inactive:
            return False
        victim = min(inactive, key=lambda key: self._last_sent.get(key, float("-inf")))
        self._last_sent.pop(victim, None)
        self._generation.pop(victim, None)
        self._terminal.discard(victim)
        return True

    def _track(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)

        def completed(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            if done.cancelled():
                return
            try:
                done.exception()
            except asyncio.CancelledError:
                pass

        task.add_done_callback(completed)

    async def _flush_after(
        self, upload_id: str, generation: int, delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                if self._generation.get(upload_id, 0) != generation:
                    return
                payload = self._pending.pop(upload_id, None)
                if payload is not None and not self._closed and self._accepts_progress(upload_id):
                    self._last_sent[upload_id] = self.monotonic_clock()
            if payload is not None:
                flush = asyncio.create_task(
                    self._send_if_current(upload_id, generation, payload)
                )
                self._track(flush)
                try:
                    await asyncio.shield(flush)
                except asyncio.CancelledError:
                    await flush
                    raise
        except asyncio.CancelledError:
            raise
        finally:
            async with self._lock:
                if self._timers.get(upload_id) is asyncio.current_task():
                    self._timers.pop(upload_id, None)

    async def _send_if_current(
        self,
        upload_id: str,
        generation: int,
        payload: dict[str, object],
        *,
        allow_closed: bool = False,
    ) -> None:
        async with self._send_lock:
            async with self._lock:
                if self._generation.get(upload_id, 0) != generation:
                    return
                if (self._closed and not allow_closed) or not self._accepts_progress(upload_id):
                    return
            event = self.persist(upload_id, payload)
            try:
                await self.broadcast(event)
            except Exception:
                logger.exception("Failed to broadcast upload progress event")

    async def discard(self, upload_id: str, *, terminal: bool = False) -> None:
        async with self._lock:
            if not self._ensure_capacity(upload_id):
                return
            self._generation[upload_id] = self._generation.get(upload_id, 0) + 1
            self._pending.pop(upload_id, None)
            timer = self._timers.pop(upload_id, None)
            if terminal:
                self._terminal.add(upload_id)
        if timer is not None:
            timer.cancel()
            with suppress(asyncio.CancelledError):
                await timer
        async with self._send_lock:
            pass

    async def mark_terminal(self, upload_id: str) -> None:
        await self.discard(upload_id, terminal=True)

    async def reset(self, upload_id: str) -> None:
        await self.discard(upload_id)
        async with self._lock:
            self._terminal.discard(upload_id)
            self._last_sent.pop(upload_id, None)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            timers = tuple(self._timers.values())
            self._timers.clear()
            pending = tuple(self._pending.items())
            self._pending.clear()
            generations: dict[str, int] = {}
            for upload_id, _ in pending:
                generation = self._generation.get(upload_id, 0) + 1
                self._generation[upload_id] = generation
                generations[upload_id] = generation
            for timer in timers:
                timer.cancel()
        for timer in timers:
            with suppress(asyncio.CancelledError):
                await timer
        for upload_id, payload in pending:
            await self._send_if_current(
                upload_id,
                generations[upload_id],
                payload,
                allow_closed=True,
            )
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            self._last_sent.clear()
            self._generation.clear()
            self._terminal.clear()


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
