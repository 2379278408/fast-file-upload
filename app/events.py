from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from starlette.websockets import WebSocket, WebSocketDisconnect


logger = logging.getLogger("transfer.events")


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
