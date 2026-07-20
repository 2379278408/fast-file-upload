from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


class EventWriter:
    def __init__(self, retention_limit: int) -> None:
        if retention_limit < 1:
            raise ValueError("retention_limit must be at least 1")
        self.retention_limit = retention_limit

    def append(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        entity_id: str,
        payload: dict[str, object],
        created_at: str | None = None,
    ) -> dict[str, object]:
        timestamp = created_at or datetime.now(timezone.utc).isoformat()
        insertion = connection.execute(
            "INSERT INTO events (event_type, entity_id, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (event_type, entity_id, json.dumps(payload), timestamp),
        )
        self.trim(connection)
        return {
            "sequence": int(insertion.lastrowid),
            "event_type": event_type,
            "entity_id": entity_id,
            "payload": payload,
            "created_at": timestamp,
        }

    def trim(self, connection: sqlite3.Connection) -> int:
        cutoff = connection.execute(
            "SELECT sequence FROM events ORDER BY sequence DESC LIMIT 1 OFFSET ?",
            (self.retention_limit - 1,),
        ).fetchone()
        if cutoff is None:
            return 0
        deleted = connection.execute(
            "DELETE FROM events WHERE sequence < ?", (int(cutoff[0]),)
        )
        return int(deleted.rowcount)
