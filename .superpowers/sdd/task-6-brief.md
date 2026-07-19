### Task 6: Ordered Upload Events And Resource Limits

**Files:**
- Modify: `app/events.py`
- Modify: `app/main.py`
- Modify: `app/upload_repository.py`
- Modify: `app/upload_service.py`
- Modify: `tests/test_events.py`
- Modify: `tests/test_resumable_upload_api.py`

**Interfaces:**
- Produces `UploadProgressPublisher(interval_seconds: float, persist: Callable[[str, dict[str, object]], dict[str, object]], broadcast: Callable[[dict[str, object]], Awaitable[None]], status_lookup: Callable[[str], str | None] | None = None)`.
- Produces `async publish(upload_id: str, payload: dict[str, object], force: bool = False) -> None`, `async discard(upload_id: str, terminal: bool = False) -> None`, `async mark_terminal(upload_id: str) -> None`, `async reset(upload_id: str) -> None`, and `async close() -> None`.
- Produces ordered event types `upload.created`, `upload.progress`, `upload.state_changed`, `upload.completed`, `upload.cancelled`, and `upload.expired`.
- Produces one application-wide `asyncio.Semaphore(settings.max_concurrent_chunk_handlers)` around chunk handlers.

- [ ] **Step 1: Write failing event cadence, ordering, capacity, and storage tests**

```python
def test_progress_publisher_emits_at_most_four_per_second() -> None:
    sent: list[dict[str, object]] = []
    ticks = iter([0.0, 0.05, 0.10, 0.24, 0.25])

    def persist(upload_id: str, payload: dict[str, object]) -> dict[str, object]:
        return {
            "sequence": len(sent) + 1,
            "event_type": "upload.progress",
            "entity_id": upload_id,
            "payload": payload,
        }

    async def broadcast(event: dict[str, object]) -> None:
        sent.append(event)

    publisher = UploadProgressPublisher(
        interval_seconds=0.25,
        persist=persist,
        broadcast=broadcast,
        monotonic_clock=lambda: next(ticks),
    )

    async def scenario() -> None:
        for in_flight_bytes in [1, 2, 3, 4, 5]:
            await publisher.publish(
                "a" * 32,
                {"in_flight_bytes": in_flight_bytes},
            )

    asyncio.run(scenario())
    assert [event["payload"]["in_flight_bytes"] for event in sent] == [1, 5]


def test_upload_events_share_strict_sequence_with_messages(settings: Settings) -> None:
    client = authenticated_client(settings, device_id="source")
    upload = create_upload(client)
    put_single_part(client, upload, b"data")
    client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})
    events = MessageRepository(Database(settings.database_path)).events_after(0)
    sequences = [int(event["sequence"]) for event in events]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert {"upload.created", "upload.state_changed", "upload.completed"} <= {str(event["event_type"]) for event in events}
```

Also mock `shutil.disk_usage()` to force reserve failure before session creation, set `max_active_upload_sessions=1`, set `max_concurrent_chunk_handlers=1`, and assert excess work receives deterministic 507, 429, or 503 responses without temporary files.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python3 -m pytest -q tests/test_events.py tests/test_resumable_upload_api.py -k "upload_events or progress_publisher or storage_reserve or active_capacity or chunk_capacity"`

Expected: FAIL because upload progress throttling and the resource guards are absent.

- [ ] **Step 3: Implement coalesced progress without extending session expiry**

Keep one latest pending payload and one timer per `upload_id`. Persist and broadcast immediately when 250ms elapsed; otherwise replace the pending payload. All progress, including part completion, remains on the normal cadence; the compatibility `force` argument does not bypass it. Before state or terminal mutations, `discard()` invalidates the upload generation, cancels and awaits its timer/flush, clears pending progress, and blocks terminal uploads. Timer flush validates generation and current session status. A bounded serial send lock preserves persist/broadcast sequence order and contains broadcast failures. `close()` safely flushes current non-terminal pending values and awaits tracked timer/flush tasks. Event persistence inserts only into `events`; it must not update `upload_sessions.updated_at` or `expires_at`.

Progress payload uses this exact shape:

```python
{
    "upload_id": upload_id,
    "status": session["status"],
    "confirmed_bytes": session["confirmed_bytes"],
    "in_flight_bytes": in_flight_bytes,
    "total_bytes": session["size_bytes"],
    "source_device_id": session["source_device_id"],
    "updated_at": now.isoformat(),
}
```

- [ ] **Step 4: Enforce storage, session, chunk, and rate bounds**

Before creation, require:

```python
free_bytes = shutil.disk_usage(self.settings.upload_dir).free
required_bytes = command.size_bytes + self.settings.upload_storage_reserve_bytes
if free_bytes < required_bytes:
    raise UploadStorageCapacityExceeded(required_bytes, free_bytes)
```

Count only active statuses against `max_active_upload_sessions`. Acquire the chunk semaphore without unbounded waiting and return 503 `Too many concurrent chunk uploads` when full. Reuse the existing limiter with normalized keys: all `PUT /api/uploads/{upload_id}/parts/{part_index}` requests share `/api/uploads/:upload_id/parts/:part_index`, while create, control, cancel, and complete use their route templates. This prevents each part index from receiving an independent bucket. Use bounded keyed-lock capacity already provided by `_KeyedLockPool`.

- [ ] **Step 5: Run event/resource tests and commit**

Run: `python3 -m pytest -q tests/test_events.py tests/test_resumable_upload_api.py`

Expected: PASS with strictly increasing event sequences and no more than four progress events per second per upload.

```bash
git add app/events.py app/main.py app/upload_repository.py app/upload_service.py tests/test_events.py tests/test_resumable_upload_api.py
git commit -m "feat(upload): bound resources and publish ordered progress"
```

---
