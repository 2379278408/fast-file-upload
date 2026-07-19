### Task 5: Completion, Recoverable Publication, And Startup Recovery

**Files:**
- Modify: `app/upload_service.py`
- Modify: `app/main.py`
- Modify: `app/repository.py`
- Modify: `app/upload_repository.py`
- Modify: `tests/test_upload_repository.py`
- Modify: `tests/test_resumable_upload_api.py`

**Interfaces:**
- Produces `async UploadService.complete(upload_id: str, device: SessionData, now: datetime) -> dict[str, object]`, returning a mutation envelope whose `result` is the permanent file-message DTO and whose `events` contains only events committed by that completion. Replay returns the same `result` with empty `events` and `changed=False`.
- Produces `async UploadService.recover(now: datetime) -> list[dict[str, object]]`.
- Produces `async UploadService.expire(now: datetime) -> list[dict[str, object]]`.
- Produces `POST /api/uploads/{upload_id}/complete` returning the existing permanent file-message DTO.
- Preserves the legacy `process_upload()` and `POST /api/upload` behavior.

- [ ] **Step 1: Write failing completion, replay, SHA-256, and recovery tests**

```python
def test_complete_computes_server_hash_and_returns_one_permanent_message(settings: Settings) -> None:
    client = authenticated_client(settings, device_id="source")
    content = b"abcdefgh"
    upload = create_upload(client, request_id="complete-1", content=content, chunk_size=4)
    put_part(client, upload, 0, content[:4])
    put_part(client, upload, 1, content[4:])
    first = client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})
    replay = client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["file"]["sha256"] == sha256(content).hexdigest()
    assert first.json()["upload_id"] == upload["upload_id"]
    assert client.get("/api/messages?limit=50").json()["items"].count(first.json()) == 1


def test_restart_recovers_file_published_session_without_duplicate_message(settings: Settings) -> None:
    content = b"recover-me"
    upload_id = "d" * 32
    now = "2026-07-19T00:00:00+00:00"
    settings.upload_dir.mkdir(parents=True)
    (settings.upload_dir / f"{upload_id}_report.txt").write_bytes(content)
    database = Database(settings.database_path)
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO upload_sessions "
            "(id, client_request_id, source_device_id, source_device_name, original_name, mime_type, size_bytes, "
            "last_modified_ms, sample_sha256, chunk_size_bytes, status, confirmed_bytes, "
            "file_sha256, message_id, error_code, publication_state, created_at, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
            (
                upload_id, "recover-request", "source", "Source device", "report.txt", "text/plain", len(content),
                1_784_412_345_000, sha256(b"sample").hexdigest(), 8 * 1024 * 1024,
                "verifying", len(content), sha256(content).hexdigest(), "file_published",
                now, now, "2026-07-20T00:00:00+00:00",
            ),
        )
    with authenticated_client(settings, app=create_app(settings), device_id="source") as client:
        active = client.get("/api/uploads/active").json()["items"]
        messages = client.get("/api/messages?limit=50").json()["items"]
    assert active == []
    assert len([item for item in messages if item.get("upload_id")]) == 1
```

Also inject failures after assembly, after final rename, and during database finalization; verify the file stays unavailable and a restart converges to one complete message.

- [ ] **Step 2: Run completion and recovery tests and verify failure**

Run: `python3 -m pytest -q tests/test_resumable_upload_api.py tests/test_upload_repository.py -k "complete or publication or recover"`

Expected: FAIL because the completion route and publication recovery are absent.

- [ ] **Step 3: Implement durable publication phases and server SHA-256**

Implement completion in this exact order under the upload lock around each persisted phase:

```python
async def complete(self, upload_id: str, device: SessionData, now: datetime) -> dict[str, object]:
    async with self.upload_locks.hold(upload_id):
        session = self.repository.get(upload_id)
        if session is None:
            raise UploadNotFound(upload_id)
        if session["status"] == "complete":
            return {"result": self.repository.get_completed_message(upload_id), "events": [], "changed": False}
        session = self.repository.begin_completion(
            upload_id, now, self.settings.upload_session_ttl_seconds
        )
        parts = self.repository.list_parts(upload_id)

    pending = await asyncio.to_thread(self.chunks.assemble, session, parts)
    async with self.upload_locks.hold(upload_id):
        current = self.repository.get(upload_id)
        if current is None or current["status"] == "cancelled":
            await asyncio.to_thread(self.chunks.discard_assembled, upload_id)
            raise UploadStateConflict("Upload was cancelled during verification")
        self.repository.set_publication_state(
            upload_id, "assembled", pending.sha256, now,
            self.settings.upload_session_ttl_seconds,
        )
        await asyncio.to_thread(self.storage.publish, pending)
        self.repository.set_publication_state(
            upload_id, "file_published", pending.sha256, now,
            self.settings.upload_session_ttl_seconds,
        )
        mutation = self.repository.finalize_publication(upload_id, pending, now)
        return {**mutation, "result": self.repository.get_completed_message(upload_id)}
```

The route calls `mutation = await upload_service.complete(upload_id, session, request.app.state.clock())`, broadcasts only `mutation["events"]`, and returns `mutation["result"]`. `complete()` sends assembly, hashing, and publication filesystem work through `asyncio.to_thread()`. Validate part coverage and total bytes before assembly and again against assembled byte count. The server-computed `pending.sha256` is authoritative. Add `ChunkStorage.discard_assembled(upload_id: str) -> None` for interrupted assembly recovery.

- [ ] **Step 4: Implement restart reconciliation and 24-hour expiry**

`recover()` must process durable states as follows:

| Durable state | Recovery action |
|---|---|
| `assembling` | Remove incomplete `final.uploading`, reset to `uploading`, preserve confirmed parts. |
| `assembled` | Validate assembled size/hash, publish final file, persist `file_published`. |
| `file_published` | Recreate `PendingFile` from the session and atomically finalize the message/database state. |
| `published` with incomplete status | Load the linked message and set `complete` idempotently. |
| confirmed database part missing on disk | Remove its part row, recompute confirmed bytes, set `failed` with `missing_part`. |
| unconfirmed incoming file | Remove it. |

`expire()` considers every nonterminal session where `expires_at <= now`. It acquires the upload lock and transitions `queued`, `uploading`, `paused`, and `failed` to `expired`, emits `upload.expired`, then cleans temporary data after the transaction. For `verifying`, it first resumes `assembled` or `file_published` recovery; an unrecoverable or still-`assembling` session transitions to `expired` only after recovery has released its filesystem work. The periodic worker calls both existing purge maintenance and upload expiry without coupling their failure handling.

- [ ] **Step 5: Wire lifespan recovery and completion route**

Construct `UploadRepository`, `ChunkStorage`, and `UploadService` in `create_app()`, expose them on `app.state`, call `upload_service.recover()` before serving, and call `upload_service.expire(clock())` in each maintenance pass. Broadcast returned mutation events through the existing hub.

Extend `MessageRepository._message_payload()` in `app/repository.py` with a lookup by `upload_sessions.message_id`; include `upload_id` when linked and `None` for legacy/text messages. This keeps existing list/search/WebSocket permanent message DTOs consistent with completion responses.

- [ ] **Step 6: Run completion, restart, and legacy regression tests**

Run: `python3 -m pytest -q tests/test_resumable_upload_api.py tests/test_upload_repository.py tests/test_files.py -k "resumable or complete or recover or upload_creates_one_file_message_with_hash or retry_returns_existing"`

Expected: PASS; legacy `POST /api/upload` still creates the existing DTO.

```bash
git add app/upload_service.py app/main.py app/repository.py app/upload_repository.py tests/test_upload_repository.py tests/test_resumable_upload_api.py
git commit -m "feat(upload): add recoverable verified publication"
```

---
