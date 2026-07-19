# Resumable Unified Transfer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one transfer timeline for text and resumable file tasks, supporting 512MB files, nine-file concurrency, refresh recovery, and cross-device status synchronization.

**Architecture:** Keep permanent messages and temporary upload sessions separate in SQLite, then merge them by `upload_id` and `client_request_id` in the frontend timeline. A focused repository owns durable transitions, chunk storage owns isolated streaming files, and an upload service coordinates idempotent API operations, whole-file verification, recoverable publication, events, and maintenance.

**Tech Stack:** Python 3, FastAPI 0.139, SQLite, filesystem storage, native ES Modules, Web Crypto, IndexedDB, QuickJS frontend tests, Playwright Chromium E2E, pytest.

## Global Constraints

- Accept files from 1 byte through 512MB inclusive; reject larger or empty files before receiving content.
- Use 8MB chunks by default and permit exactly one in-flight chunk per file.
- Permit at most 9 uploading files per source coordinator; preserve overflow as queued tasks.
- Keep `POST /api/upload` as the documented legacy whole-file route; the new Transfer UI uses only `/api/uploads*`.
- Require the existing signed session for every resumable HTTP endpoint and the existing WebSocket connection.
- Use the source device for pause and resume authorization; every connected device may cancel.
- Expire inactive upload sessions after 24 hours.
- Extend `expires_at` only when session creation succeeds, a chunk is successfully confirmed, or an upload-status/publication-state transition succeeds. Reads, rejected chunks, in-flight progress, failed retries, and idempotent replays preserve the prior expiry.
- Broadcast progress at most four times per second per upload session; state and terminal events bypass progress throttling.
- Stream request bodies, part assembly, and SHA-256 calculation with bounded buffers; never buffer a complete file in memory.
- Compute the complete-file SHA-256 on the server while assembling ordered confirmed parts.
- Keep incomplete and recoverable publication files unavailable through download routes.
- Keep runtime dependencies unchanged; use the Python standard library and browser platform APIs.
- Preserve CSP compliance: no inline script/style and no `element.style` assignments.
- Keep every interactive target at least 44 by 44 CSS pixels, throttle live-region announcements, and honor `prefers-reduced-motion`.
- Generate 40MB and sparse 512MB test files at test time; keep large fixtures outside version control.
- Mark the sparse 512MB verification with `@pytest.mark.large` and exclude it from the default suite.
- Use TDD and commit each independently testable task with the exact path set listed in that task.

## Exact File Map

| Path | Change | Responsibility |
|---|---|---|
| `app/config.py` | Modify | Resumable limits, expiry, reserve, concurrency, and event cadence settings. |
| `app/database.py` | Modify | `upload_sessions` and `upload_parts` schema plus additive migration columns/indexes. |
| `app/repository.py` | Modify | Add originating `upload_id` to permanent message payloads without changing legacy message behavior. |
| `app/upload_repository.py` | Create | Upload DTOs, transition rules, idempotency, confirmed-part persistence, publication state, expiry, and ordered upload events. |
| `app/chunk_storage.py` | Create | Safe resumable paths, streamed incoming-part validation, atomic part commit, assembly, SHA-256, and cleanup. |
| `app/upload_service.py` | Create | Authorization, range validation, per-upload serialization, controls, completion, cancellation, recovery, and maintenance orchestration. |
| `app/events.py` | Modify | Per-upload progress coalescing at a 250ms minimum interval. |
| `app/main.py` | Modify | Pydantic request models, `/api/uploads*` routes, capacity guards, lifespan recovery, and event publication. |
| `web/js/config.js` | Modify | 8MB, 512MB, nine-file, retry, speed-window, and announcement constants. |
| `web/js/api.js` | Modify | Resumable session, part XHR, control, completion, and reconciliation clients. |
| `web/js/upload-persistence.js` | Create | IndexedDB task metadata and authorized file-handle persistence. |
| `web/js/upload-coordinator.js` | Create | Scheduling, hashing, retries, controls, identity verification, speed/ETA, reconciliation, and immutable snapshots. |
| `web/js/composer.js` | Modify | Delegate selection, drop, and paste files to the coordinator; retain text submission. |
| `web/js/timeline.js` | Modify | Merge permanent messages and active upload projections and replace cards by stable upload identity. |
| `web/js/app.js` | Modify | Construct coordinator, reconcile before timeline restoration, route WebSocket upload events, and resume after re-authentication. |
| `web/index.html` | Modify | Aggregate controls, full-surface drop overlay, upload live region, and composer wiring. |
| `web/styles.css` | Modify | Unified file cards, progress states, responsive controls, 44px targets, and reduced motion. |
| `tests/test_session.py` | Modify | Environment/default validation for resumable settings. |
| `tests/test_database.py` | Modify | Schema and additive migration verification. |
| `tests/test_upload_repository.py` | Create | State machine, idempotency, expiry, part confirmation, and publication-state tests. |
| `tests/test_chunk_storage.py` | Create | Stream/range/digest/replay/path/assembly tests. |
| `tests/test_resumable_upload_api.py` | Create | Authenticated API, controls, races, completion, restart, limits, and legacy regression. |
| `tests/test_events.py` | Modify | Upload event ordering and progress throttling tests. |
| `tests/test_frontend_contract.py` | Modify | QuickJS coordinator, persistence, timeline/composer, refresh, and accessibility contracts. |
| `tests/test_browser_e2e.py` | Modify | Nine-file scheduling, controls, refresh/reselect, cross-device, mobile, and 40MB browser coverage. |
| `tests/test_large_upload.py` | Create | Sparse 512MB API upload, server SHA-256, and bounded-memory verification. |
| `pytest.ini` | Create | Register the `large` marker and omit it from default runs. |
| `README.md` | Modify | Resumable API, legacy route status, settings, recovery, and large-test commands. |

## Concurrency And Lifecycle Contract

- Session creation commits `queued`; accepting a request body does not change that state.
- `UploadService.begin_part()` validates `queued` or `uploading` under the upload lock and returns a `PartLease`; the body streams outside the lock.
- The first successful `confirm_part()` atomically commits part metadata and transitions `queued -> uploading` in the same transaction.
- A pause racing with an already-started part commits `paused` immediately. That leased part may finish and become confirmed; confirmation observes `paused`, updates bytes and expiry, and leaves the state `paused`. No later part may begin until resume.
- A cancel racing with an in-flight part commits `cancelled`; that part's confirmation is rejected, its incoming or committed race artifact is removed, and no new part is accepted.
- Completion transitions `uploading -> verifying` only with contiguous full coverage. Pause and resume are rejected while verifying.
- Cancellation may win while `verifying`; completion rechecks durable state after assembly, discards the unpublished assembly when it sees `cancelled`, and returns 409. Once finalization commits `complete`, cancellation returns 409.
- Cancellation of `complete` returns HTTP 409. Repeated cancellation of `cancelled` is idempotent and preserves expiry.
- Completion replay after `complete` returns the same permanent message. Conflicting create or part replay returns HTTP 409.
- Publication uses durable states `none -> assembling -> assembled -> file_published -> published`. Startup recovery resumes from the recorded state and never exposes the final file before the message/database transaction reaches `published`.

---

### Task 1: Resumable Configuration And Database Schema

**Files:**
- Modify: `app/config.py`
- Modify: `app/database.py`
- Modify: `tests/test_session.py`
- Modify: `tests/test_database.py`

**Interfaces:**
- Produces `Settings.upload_chunk_size_bytes: int = 8 * 1024 * 1024`.
- Produces `Settings.upload_session_ttl_seconds: int = 24 * 60 * 60`.
- Produces `Settings.upload_storage_reserve_bytes: int = 256 * 1024 * 1024`.
- Produces `Settings.max_active_upload_sessions: int = 128`.
- Produces `Settings.max_concurrent_chunk_handlers: int = 16`.
- Produces `Settings.upload_progress_interval_seconds: float = 0.25`.
- Produces tables `upload_sessions` and `upload_parts`, with durable `publication_state` and foreign-key cascade.

- [ ] **Step 1: Write failing settings and schema tests**

Add these concrete assertions:

```python
def test_resumable_upload_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOAD_TOKEN", "secret")
    settings = Settings.from_env("uploads")
    assert settings.max_upload_size == 512 * 1024 * 1024
    assert settings.upload_chunk_size_bytes == 8 * 1024 * 1024
    assert settings.upload_session_ttl_seconds == 86_400
    assert settings.upload_storage_reserve_bytes == 256 * 1024 * 1024
    assert settings.max_active_upload_sessions == 128
    assert settings.max_concurrent_chunk_handlers == 16
    assert settings.upload_progress_interval_seconds == 0.25


def test_database_initializes_resumable_upload_schema(tmp_path: Path) -> None:
    database = Database(tmp_path / "timeline.sqlite3")
    database.initialize()
    with database.connect() as connection:
        sessions = {row[1] for row in connection.execute("PRAGMA table_info(upload_sessions)")}
        parts = {row[1] for row in connection.execute("PRAGMA table_info(upload_parts)")}
        foreign_keys = connection.execute("PRAGMA foreign_key_list(upload_parts)").fetchall()
    assert {
        "id", "client_request_id", "source_device_id", "source_device_name", "original_name", "mime_type",
        "size_bytes", "last_modified_ms", "sample_sha256", "chunk_size_bytes", "status",
        "confirmed_bytes", "file_sha256", "message_id", "error_code", "publication_state",
        "created_at", "updated_at", "expires_at",
    } <= sessions
    assert {"upload_id", "part_index", "start_byte", "end_byte", "size_bytes", "sha256", "created_at"} <= parts
    assert any(row[2] == "upload_sessions" and row[6] == "CASCADE" for row in foreign_keys)
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python3 -m pytest -q tests/test_session.py tests/test_database.py -k "resumable or upload_schema"`

Expected: FAIL because the six settings fields and `upload_sessions`/`upload_parts` tables do not exist.

- [ ] **Step 3: Add validated settings and environment parsing**

Add the exact dataclass fields and parse these exact variables:

```python
upload_chunk_size_bytes: int = 8 * 1024 * 1024
upload_session_ttl_seconds: int = 24 * 60 * 60
upload_storage_reserve_bytes: int = 256 * 1024 * 1024
max_active_upload_sessions: int = 128
max_concurrent_chunk_handlers: int = 16
upload_progress_interval_seconds: float = 0.25
```

```python
upload_chunk_size_bytes=int(os.environ.get("UPLOAD_CHUNK_SIZE_BYTES", str(8 * 1024 * 1024))),
upload_session_ttl_seconds=int(os.environ.get("UPLOAD_SESSION_TTL_SECONDS", str(24 * 60 * 60))),
upload_storage_reserve_bytes=int(os.environ.get("UPLOAD_STORAGE_RESERVE_BYTES", str(256 * 1024 * 1024))),
max_active_upload_sessions=int(os.environ.get("MAX_ACTIVE_UPLOAD_SESSIONS", "128")),
max_concurrent_chunk_handlers=int(os.environ.get("MAX_CONCURRENT_CHUNK_HANDLERS", "16")),
upload_progress_interval_seconds=float(os.environ.get("UPLOAD_PROGRESS_INTERVAL_SECONDS", "0.25")),
```

Validate chunk size, TTL, active-session count, chunk-handler count, and progress interval as positive integers/numbers; validate storage reserve as non-negative.

- [ ] **Step 4: Add the schema and additive initialization indexes**

Append these statements to `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS upload_sessions (
 id TEXT PRIMARY KEY, client_request_id TEXT NOT NULL UNIQUE,
 source_device_id TEXT NOT NULL, source_device_name TEXT NOT NULL,
 original_name TEXT NOT NULL,
 mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
 last_modified_ms INTEGER NOT NULL, sample_sha256 TEXT NOT NULL,
 chunk_size_bytes INTEGER NOT NULL CHECK(chunk_size_bytes > 0),
 status TEXT NOT NULL, confirmed_bytes INTEGER NOT NULL DEFAULT 0,
 file_sha256 TEXT, message_id TEXT UNIQUE REFERENCES messages(id),
 error_code TEXT, publication_state TEXT NOT NULL DEFAULT 'none',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS upload_sessions_active_expiry
 ON upload_sessions(status, expires_at);
CREATE TABLE IF NOT EXISTS upload_parts (
 upload_id TEXT NOT NULL REFERENCES upload_sessions(id) ON DELETE CASCADE,
 part_index INTEGER NOT NULL CHECK(part_index >= 0),
 start_byte INTEGER NOT NULL CHECK(start_byte >= 0),
 end_byte INTEGER NOT NULL CHECK(end_byte >= start_byte),
 size_bytes INTEGER NOT NULL CHECK(size_bytes > 0), sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(upload_id, part_index)
);
```

Use `_add_column()` for `publication_state`, `file_sha256`, `message_id`, `error_code`, and `source_device_name` so an existing development database upgrades additively. Backfill legacy `source_device_name` from `source_device_id`.

- [ ] **Step 5: Run focused tests and commit**

Run: `python3 -m pytest -q tests/test_session.py tests/test_database.py`

Expected: PASS.

```bash
git add app/config.py app/database.py tests/test_session.py tests/test_database.py
git commit -m "feat(upload): add resumable configuration and schema"
```

---

### Task 2: Upload Repository And State Machine

**Files:**
- Create: `app/upload_repository.py`
- Create: `tests/test_upload_repository.py`

**Interfaces:**
- Produces `UploadCreate(client_request_id: str, original_name: str, mime_type: str, size_bytes: int, last_modified_ms: int, sample_sha256: str, chunk_size_bytes: int, source_device_id: str, source_device_name: str)`.
- Produces `PartRecord(upload_id: str, part_index: int, start_byte: int, end_byte: int, size_bytes: int, sha256: str, created_at: str)`.
- Produces exceptions `UploadNotFound`, `UploadConflict`, `UploadStateConflict`, and `UploadCapacityExceeded`.
- Produces `UploadRepository.create_or_get(command: UploadCreate, now: datetime, ttl_seconds: int, max_active: int) -> tuple[dict[str, object], bool]`; new IDs use `uuid4().hex` so storage validation receives exactly 32 lowercase hexadecimal characters.
- Produces `UploadRepository.get(upload_id: str) -> dict[str, object] | None`, `list_active() -> list[dict[str, object]]`, and `list_parts(upload_id: str) -> list[PartRecord]`; every session payload exposes `upload_id`, never the database column name `id`.
- Session payloads use exact keys `upload_id`, `client_request_id`, `source_device_id`, `source_device_name`, `original_name`, `mime_type`, `size_bytes`, `last_modified_ms`, `sample_sha256`, `chunk_size_bytes`, `status`, `confirmed_parts`, `confirmed_bytes`, `file_sha256`, `message_id`, `error_code`, `publication_state`, `created_at`, `updated_at`, and `expires_at`.
- Produces `begin_part(upload_id: str, part_index: int, start_byte: int, end_byte: int, size_bytes: int, sha256: str) -> PartLease | dict[str, object]` and `confirm_part(lease: PartLease, part: PartRecord, now: datetime, ttl_seconds: int) -> dict[str, object]`. An identical confirmed replay returns the persisted session dictionary before reading the body; conflicting metadata raises `UploadConflict`.
- Produces `transition(upload_id: str, action: Literal["pause", "resume"], source_device_id: str, now: datetime, ttl_seconds: int) -> dict[str, object]`.
- Produces `cancel(upload_id: str, now: datetime, ttl_seconds: int) -> tuple[dict[str, object], bool]`.
- Produces `begin_completion(upload_id: str, now: datetime, ttl_seconds: int) -> dict[str, object]`.
- Produces `set_publication_state(upload_id: str, state: Literal["assembling", "assembled", "file_published"], file_sha256: str | None, now: datetime, ttl_seconds: int) -> dict[str, object]`.
- Produces `finalize_publication(upload_id: str, pending: PendingFile, now: datetime) -> dict[str, object]`; it derives permanent-message device metadata from the durable source-device columns so restart recovery needs no online `SessionData` object and idempotent replay cannot extend expiry.
- Produces `fail(upload_id: str, error_code: str, now: datetime, ttl_seconds: int) -> dict[str, object]` and `claim_expired(now: datetime, limit: int = 100) -> list[dict[str, object]]`.

- [ ] **Step 1: Write failing transition, race, and expiry tests**

Create repository fixtures around `Database`, then add:

```python
def test_first_confirmed_part_transitions_queued_to_uploading(repository, upload_command, clock) -> None:
    session, created = repository.create_or_get(upload_command, clock(), 86_400, 128)
    lease = repository.begin_part(str(session["upload_id"]), 0, 0, 3, 4, sha256(b"data").hexdigest())
    assert isinstance(lease, PartLease)
    confirmed = repository.confirm_part(
        lease,
        PartRecord(str(session["upload_id"]), 0, 0, 3, 4, sha256(b"data").hexdigest(), clock().isoformat()),
        clock(),
        86_400,
    )
    assert created is True
    assert confirmed["status"] == "uploading"
    assert confirmed["confirmed_bytes"] == 4


def test_pause_allows_started_part_to_confirm_and_preserves_paused(repository, upload_command, clock) -> None:
    session, _ = repository.create_or_get(upload_command, clock(), 86_400, 128)
    lease = repository.begin_part(str(session["upload_id"]), 0, 0, 3, 4, sha256(b"data").hexdigest())
    assert isinstance(lease, PartLease)
    paused = repository.transition(str(session["upload_id"]), "pause", upload_command.source_device_id, clock(), 86_400)
    confirmed = repository.confirm_part(
        lease,
        PartRecord(str(session["upload_id"]), 0, 0, 3, 4, sha256(b"data").hexdigest(), clock().isoformat()),
        clock(),
        86_400,
    )
    assert paused["status"] == "paused"
    assert confirmed["status"] == "paused"
    assert confirmed["confirmed_bytes"] == 4


def test_only_confirmation_and_state_change_extend_expiry(repository, upload_command, clock) -> None:
    session, _ = repository.create_or_get(upload_command, clock(), 86_400, 128)
    original_expiry = session["expires_at"]
    clock.advance(seconds=60)
    assert repository.get(str(session["upload_id"]))["expires_at"] == original_expiry
    resumed = repository.transition(str(session["upload_id"]), "pause", upload_command.source_device_id, clock(), 86_400)
    assert resumed["expires_at"] > original_expiry
```

Also test create metadata conflict, identical part replay, conflicting part replay, observing-device pause/resume rejection, cancel idempotency, complete cancellation conflict, contiguous completion coverage, and active-session capacity.

- [ ] **Step 2: Run repository tests and verify failure**

Run: `python3 -m pytest -q tests/test_upload_repository.py`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.upload_repository'`.

- [ ] **Step 3: Implement domain records, payload loading, and exact transition table**

Define these records and transitions:

```python
@dataclass(frozen=True, slots=True)
class UploadCreate:
    client_request_id: str
    original_name: str
    mime_type: str
    size_bytes: int
    last_modified_ms: int
    sample_sha256: str
    chunk_size_bytes: int
    source_device_id: str
    source_device_name: str


@dataclass(frozen=True, slots=True)
class PartLease:
    upload_id: str
    part_index: int
    start_byte: int
    end_byte: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PartRecord:
    upload_id: str
    part_index: int
    start_byte: int
    end_byte: int
    size_bytes: int
    sha256: str
    created_at: str


CONTROL_TRANSITIONS = {
    ("queued", "pause"): "paused",
    ("uploading", "pause"): "paused",
    ("failed", "resume"): "uploading",
    ("paused", "resume"): "uploading",
}
ACTIVE_STATUSES = ("queued", "uploading", "paused", "verifying", "failed")
```

`create_or_get()` must compare all client metadata on replay and reject keys already present in `messages` or legacy `upload_reservations`. The `POST /api/uploads` route must execute under the same `_KeyedLockPool` key used by text and legacy upload creation, making the cross-table check race-safe. `begin_part()` must accept only `queued` and `uploading`, compare an existing part's range, size, and digest, and return the persisted session for an identical replay. `confirm_part()` must insert with `(upload_id, part_index)` uniqueness, sum confirmed bytes in SQL, and retain `paused` when pause wins the race.

- [ ] **Step 4: Implement publication and event transactions**

Use one `BEGIN IMMEDIATE` transaction in `begin_completion()` to prove contiguous coverage and set `status='verifying', publication_state='assembling'`. Use one transaction in `finalize_publication()` to insert `files`, insert one `messages` row carrying `upload_id`, set `status='complete', publication_state='published', message_id=?, file_sha256=?`, and append ordered `upload.completed`, `message.created`, and `file.finalized` events.

Return publication/event transaction mutations in this exact shape:

```python
{
    "result": session_payload,
    "events": [event_payload],
    "changed": True,
}
```

For idempotent replay, return the persisted result with `events=[]` and `changed=False`; preserve `expires_at`.

- [ ] **Step 5: Run repository tests and commit**

Run: `python3 -m pytest -q tests/test_upload_repository.py tests/test_database.py`

Expected: PASS.

```bash
git add app/upload_repository.py tests/test_upload_repository.py
git commit -m "feat(upload): add durable upload state machine"
```

---

### Task 3: Isolated Chunk Storage And Whole-File Hashing

**Files:**
- Create: `app/chunk_storage.py`
- Create: `tests/test_chunk_storage.py`

**Interfaces:**
- Produces `ChunkStorage(upload_dir: Path, buffer_size: int = 64 * 1024)`.
- Produces `async ChunkStorage.write_part(upload_id: str, part_index: int, chunks: AsyncIterator[bytes], expected_size: int, expected_sha256: str, on_bytes: Callable[[int], Awaitable[None]] | None = None) -> StoredPart`.
- Produces `ChunkStorage.part_path(upload_id: str, part_index: int) -> Path` and `incoming_path(upload_id: str, part_index: int) -> Path`.
- Produces `ChunkStorage.assemble(session: Mapping[str, object], parts: Sequence[PartRecord]) -> PendingFile`.
- Produces `discard_incoming(upload_id: str, part_index: int)`, `discard_part(upload_id: str, part_index: int)`, `cleanup_session(upload_id: str)`, and `reconcile(session_ids: set[str], confirmed: set[tuple[str, int]]) -> StorageReconcileResult`.

- [ ] **Step 1: Write failing stream, path, digest, and assembly tests**

```python
def test_write_part_streams_and_atomically_confirms(tmp_path: Path) -> None:
    storage = ChunkStorage(tmp_path)

    async def chunks():
        yield b"ab"
        yield b"cd"

    stored = asyncio.run(storage.write_part("a" * 32, 0, chunks(), 4, sha256(b"abcd").hexdigest()))
    assert stored.size_bytes == 4
    assert stored.path.read_bytes() == b"abcd"
    assert not storage.incoming_path("a" * 32, 0).exists()


def test_digest_failure_removes_incoming_and_preserves_confirmed_parts(tmp_path: Path) -> None:
    storage = ChunkStorage(tmp_path)
    confirmed = storage.part_path("b" * 32, 0)
    confirmed.parent.mkdir(parents=True)
    confirmed.write_bytes(b"kept")

    async def chunks():
        yield b"wrong"

    with pytest.raises(ChunkDigestMismatch):
        asyncio.run(storage.write_part("b" * 32, 1, chunks(), 5, sha256(b"right").hexdigest()))
    assert confirmed.read_bytes() == b"kept"
    assert not storage.incoming_path("b" * 32, 1).exists()


def test_assemble_streams_ordered_parts_and_computes_server_sha256(tmp_path: Path) -> None:
    storage = ChunkStorage(tmp_path, buffer_size=2)
    upload_id = "c" * 32
    session = {
        "upload_id": upload_id,
        "size_bytes": 8,
        "original_name": "report.txt",
        "mime_type": "text/plain",
    }
    first_path = storage.part_path(upload_id, 0)
    first_path.parent.mkdir(parents=True)
    first_path.write_bytes(b"abcd")
    second_path = storage.part_path(upload_id, 1)
    second_path.write_bytes(b"efgh")
    parts = [
        PartRecord(upload_id, 0, 0, 3, 4, sha256(b"abcd").hexdigest(), "2026-07-19T00:00:00+00:00"),
        PartRecord(upload_id, 1, 4, 7, 4, sha256(b"efgh").hexdigest(), "2026-07-19T00:00:01+00:00"),
    ]
    pending = storage.assemble(session, parts)
    assert pending.temporary_path.read_bytes() == b"abcdefgh"
    assert pending.sha256 == sha256(b"abcdefgh").hexdigest()
```

- [ ] **Step 2: Run storage tests and verify failure**

Run: `python3 -m pytest -q tests/test_chunk_storage.py`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.chunk_storage'`.

- [ ] **Step 3: Implement identifier-derived paths and streamed part commit**

Use strict server identifier and numeric index validation:

```python
UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

def _validate_key(upload_id: str, part_index: int) -> None:
    if UPLOAD_ID_PATTERN.fullmatch(upload_id) is None or part_index < 0:
        raise ValueError("Invalid upload storage key")
```

Write each request to `.resumable/<upload_id>/incoming-000000`, hash and count while streaming, reject size/digest mismatches, then use `Path.replace()` to commit `part-000000`. Call `await asyncio.to_thread(output.write, chunk)` for bounded writes and close/discard in `finally` on errors.

- [ ] **Step 4: Implement bounded assembly and reconciliation**

`assemble()` must open `.resumable/<upload_id>/final.uploading` in exclusive mode, copy every `part-*` in `part_index` order using `buffer_size`, update one server SHA-256, verify total bytes, and return this concrete `PendingFile` mapping:

```python
safe_name = sanitize_filename(str(session["original_name"]))
PendingFile(
    file_id=upload_id,
    original_name=safe_name,
    storage_name=f"{upload_id}_{safe_name}",
    temporary_path=temporary_path,
    final_path=self.upload_dir / f"{upload_id}_{safe_name}",
    mime_type=str(session["mime_type"]),
    extension=Path(str(session["original_name"])).suffix.lower(),
    size_bytes=written,
    sha256=digest.hexdigest(),
)
```

`reconcile()` removes unconfirmed `incoming-*`, reports missing confirmed parts, and reports orphan session directories for maintenance cleanup. It must never follow symlinks or accept a path component from a client filename.

- [ ] **Step 5: Run storage tests and commit**

Run: `python3 -m pytest -q tests/test_chunk_storage.py`

Expected: PASS, including bounded-write and path-validation cases.

```bash
git add app/chunk_storage.py tests/test_chunk_storage.py
git commit -m "feat(upload): add isolated resumable chunk storage"
```

---

### Task 4: Resumable Backend API And Shared Controls

**Files:**
- Create: `app/upload_service.py`
- Modify: `app/main.py`
- Create: `tests/test_resumable_upload_api.py`

**Interfaces:**
- Consumes `UploadRepository`, `ChunkStorage`, `SessionData`, and the existing `_KeyedLockPool`.
- Produces `UploadService.create(command: UploadCreate, device: SessionData, now: datetime) -> dict[str, object]`.
- Produces `async UploadService.put_part(upload_id: str, part_index: int, content_range: str, chunk_sha256: str, chunks: AsyncIterator[bytes], device: SessionData, now: datetime, on_bytes: Callable[[int], Awaitable[None]] | None = None) -> dict[str, object]`.
- Produces `UploadService.control(upload_id: str, action: Literal["pause", "resume"], device: SessionData, now: datetime) -> dict[str, object]`.
- Produces `UploadService.cancel(upload_id: str, device: SessionData, now: datetime) -> dict[str, object]`.
- Produces `UploadService.get(upload_id: str, device: SessionData) -> dict[str, object]` and `list_active(device: SessionData) -> list[dict[str, object]]`.
- Produces routes `POST /api/uploads`, `GET /api/uploads/active`, `GET /api/uploads/{upload_id}`, `PUT /api/uploads/{upload_id}/parts/{part_index}`, `PATCH /api/uploads/{upload_id}`, and `DELETE /api/uploads/{upload_id}`.

- [ ] **Step 1: Write failing create, chunk, control, and race API tests**

Add an authenticated helper that accepts a `device_id`, then add:

```python
def authenticated_client(
    settings: Settings,
    *,
    app: FastAPI | None = None,
    device_id: str = "source",
) -> TestClient:
    client = TestClient(app or create_app(settings))
    response = client.post("/api/session", json={
        "access_token": settings.auth_token,
        "device_id": device_id,
        "device_name": device_id,
    })
    assert response.status_code == 200
    return client


def create_upload(
    client: TestClient,
    request_id: str = "request-1",
    content: bytes = b"data",
    chunk_size: int = 8 * 1024 * 1024,
) -> dict[str, object]:
    response = client.post("/api/uploads", json={
        "client_request_id": request_id,
        "name": "report.txt",
        "size_bytes": len(content),
        "mime_type": "text/plain",
        "last_modified_ms": 1_784_412_345_000,
        "chunk_size_bytes": chunk_size,
        "sample_sha256": sha256(b"sample").hexdigest(),
    })
    assert response.status_code == 200
    return response.json()


def put_part(
    client: TestClient,
    upload: dict[str, object],
    part_index: int,
    content: bytes,
) -> dict[str, object]:
    chunk_size = int(upload["chunk_size_bytes"])
    start = part_index * chunk_size
    end = start + len(content) - 1
    response = client.put(
        f"/api/uploads/{upload['upload_id']}/parts/{part_index}",
        content=content,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Range": f"bytes {start}-{end}/{upload['size_bytes']}",
            "X-Chunk-SHA256": sha256(content).hexdigest(),
        },
    )
    assert response.status_code == 200
    return response.json()


def put_single_part(client: TestClient, upload: dict[str, object], content: bytes) -> dict[str, object]:
    return put_part(client, upload, 0, content)


def test_first_confirmed_chunk_changes_queued_to_uploading(settings: Settings) -> None:
    client = authenticated_client(settings, device_id="source")
    upload = create_upload(client)
    assert upload["status"] == "queued"
    response = client.put(
        f"/api/uploads/{upload['upload_id']}/parts/0",
        content=b"data",
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Range": "bytes 0-3/4",
            "X-Chunk-SHA256": sha256(b"data").hexdigest(),
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "uploading"
    assert response.json()["confirmed_parts"] == [0]


def test_complete_upload_cancellation_returns_409(settings: Settings) -> None:
    client = authenticated_client(settings, device_id="source")
    upload = create_upload(client)
    put_single_part(client, upload, b"data")
    assert client.post(f"/api/uploads/{upload['upload_id']}/complete", json={}).status_code == 200
    response = client.delete(f"/api/uploads/{upload['upload_id']}")
    assert response.status_code == 409
    assert response.json()["detail"] == "Completed uploads cannot be cancelled"
```

Also test signed-session rejection, metadata replay/conflict, invalid index/range/total/digest, identical/conflicting part replay, source-only pause/resume, observer cancellation, paused request rejection, and cancel idempotency.

- [ ] **Step 2: Run API tests and verify failure**

Run: `python3 -m pytest -q tests/test_resumable_upload_api.py -k "create or chunk or control or cancel"`

Expected: FAIL because `/api/uploads` is handled by the 404 fallback.

- [ ] **Step 3: Implement request models and exact range validation**

Add these models in `app/main.py`:

```python
class CreateUploadRequest(BaseModel):
    client_request_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER_PATTERN)
    name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)
    mime_type: str = Field(default="application/octet-stream", max_length=255)
    last_modified_ms: int = Field(ge=0)
    chunk_size_bytes: int = Field(gt=0)
    sample_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UploadControlRequest(BaseModel):
    action: Literal["pause", "resume"]
```

Implement `parse_content_range(value: str, expected_total: int, part_index: int, chunk_size: int) -> tuple[int, int, int]`. It must require `bytes start-end/total`, exact `total`, `start == part_index * chunk_size`, `end == min(total - 1, start + chunk_size - 1)`, and `size == end - start + 1`.

- [ ] **Step 4: Implement the service race boundary**

Use one keyed upload lock before and after streaming:

```python
async def put_part(self, upload_id, part_index, content_range, chunk_sha256, chunks, device, now, on_bytes=None):
    async with self.upload_locks.hold(upload_id):
        session = self._authorized(upload_id, device)
        start, end, size = parse_content_range(
            content_range, int(session["size_bytes"]), part_index, int(session["chunk_size_bytes"])
        )
        lease = self.repository.begin_part(
            upload_id, part_index, start, end, size, chunk_sha256
        )
        if isinstance(lease, dict):
            return lease

    stored = await self.chunks.write_part(
        upload_id, part_index, chunks, size, chunk_sha256, on_bytes
    )

    async with self.upload_locks.hold(upload_id):
        current = self.repository.get(upload_id)
        if current is None or current["status"] in {"cancelled", "expired"}:
            await asyncio.to_thread(self.chunks.discard_part, upload_id, part_index)
            raise UploadStateConflict("Upload no longer accepts chunks")
        return self.repository.confirm_part(
            lease,
            PartRecord(upload_id, part_index, start, end, stored.size_bytes, stored.sha256, now.isoformat()),
            now,
            self.settings.upload_session_ttl_seconds,
        )
```

Pause executes under the same lock and therefore may win while the body streams. Confirmation keeps `paused`. Cancel makes the post-stream check discard the race artifact.

- [ ] **Step 5: Add thin authenticated routes and error mapping**

The chunk route must stream raw bytes and avoid `await request.body()`:

```python
@app.put("/api/uploads/{upload_id}/parts/{part_index}")
async def put_upload_part(
    upload_id: str,
    part_index: int,
    request: Request,
    session: SessionData = Depends(require_session),
    _: None = Depends(enforce_rate_limit),
) -> dict[str, object]:
    return await upload_service.put_part(
        upload_id,
        part_index,
        request.headers.get("content-range", ""),
        request.headers.get("x-chunk-sha256", ""),
        request.stream(),
        session,
        request.app.state.clock(),
    )
```

Map not-found to 404, metadata/state/replay conflicts to 409, invalid range/digest to 400, oversized files to 413, active capacity to 429, and storage capacity to 507. Add `PATCH` to CORS `allow_methods`.

- [ ] **Step 6: Run API tests and commit**

Run: `python3 -m pytest -q tests/test_resumable_upload_api.py -k "create or chunk or control or cancel"`

Expected: PASS.

```bash
git add app/upload_service.py app/main.py tests/test_resumable_upload_api.py
git commit -m "feat(upload): expose resumable session and control APIs"
```

---

### Task 5: Completion, Recoverable Publication, And Startup Recovery

**Files:**
- Modify: `app/upload_service.py`
- Modify: `app/main.py`
- Modify: `app/repository.py`
- Modify: `app/upload_repository.py`
- Modify: `tests/test_upload_repository.py`
- Modify: `tests/test_resumable_upload_api.py`

**Interfaces:**
- Produces `async UploadService.complete(upload_id: str, device: SessionData, now: datetime) -> dict[str, object]`.
- Produces `UploadService.recover(now: datetime) -> list[dict[str, object]]`.
- Produces `UploadService.expire(now: datetime) -> list[dict[str, object]]`.
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
            return self.repository.get_completed_message(upload_id)
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
        return mutation["result"]
```

The route calls `await upload_service.complete(upload_id, session, request.app.state.clock())`. `complete()` sends assembly, hashing, and publication filesystem work through `asyncio.to_thread()`. Validate part coverage and total bytes before assembly and again against assembled byte count. The server-computed `pending.sha256` is authoritative. Add `ChunkStorage.discard_assembled(upload_id: str) -> None` for the cancellation race.

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

### Task 6: Ordered Upload Events And Resource Limits

**Files:**
- Modify: `app/events.py`
- Modify: `app/main.py`
- Modify: `app/upload_repository.py`
- Modify: `app/upload_service.py`
- Modify: `tests/test_events.py`
- Modify: `tests/test_resumable_upload_api.py`

**Interfaces:**
- Produces `UploadProgressPublisher(interval_seconds: float, persist: Callable[[str, dict[str, object]], dict[str, object]], broadcast: Callable[[dict[str, object]], Awaitable[None]])`.
- Produces `async publish(upload_id: str, payload: dict[str, object], force: bool = False) -> None` and `async close() -> None`.
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

Keep one latest pending payload and one timer per `upload_id`. Persist and broadcast immediately when 250ms elapsed; otherwise replace the pending payload. `force=True` flushes immediately for state/terminal boundaries. `close()` flushes pending values and cancels timers. Event persistence inserts only into `events`; it must not update `upload_sessions.updated_at` or `expires_at`.

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

### Task 7: Frontend Resumable API, Persistence, And Coordinator

**Files:**
- Modify: `web/js/config.js`
- Modify: `web/js/api.js`
- Create: `web/js/upload-persistence.js`
- Create: `web/js/upload-coordinator.js`
- Modify: `tests/test_frontend_contract.py`

**Interfaces:**
- Produces constants `UPLOAD_CHUNK_SIZE_BYTES = 8 * 1024 * 1024`, `MAX_UPLOAD_SIZE_BYTES = 512 * 1024 * 1024`, `MAX_ACTIVE_UPLOADS = 9`, `UPLOAD_RETRY_DELAYS = [500, 1000, 2000, 4000, 8000]`, `UPLOAD_SPEED_WINDOW_MS = 5000`, and `UPLOAD_ETA_MIN_SAMPLE_MS = 2000`.
- Produces API functions `createUploadSession(metadata)`, `listActiveUploads()`, `getUploadSession(uploadId)`, `uploadPart(uploadId, partIndex, blob, metadata, onProgress, signal)`, `controlUpload(uploadId, action)`, `cancelUpload(uploadId)`, and `completeUpload(uploadId)`.
- Produces `createUploadPersistence({ indexedDB }) -> { put(task), getAll(), remove(uploadId), close() }`.
- Produces `sampleFileIdentity(file, cryptoObject = crypto) -> Promise<{ name: string, size: number, lastModified: number, sampleSha256: string }>` and `matchesFileIdentity(file, identity, cryptoObject = crypto) -> Promise<boolean>`.
- Produces `createUploadCoordinator({ api, persistence, cryptoObject, now, delay, maxActive, chunkSize })`.
- Coordinator returns `{ start(), enqueueFiles(files), pause(uploadId), resume(uploadId), cancel(uploadId), retry(uploadId), prioritize(uploadId), pauseAll(), resumeAll(), cancelAll(), reconcile(), applyRemoteEvent(event), subscribe(listener), getSnapshot(), destroy() }`.

- [ ] **Step 1: Write failing API, identity, scheduling, and one-part-per-file tests**

Add QuickJS module tests with deterministic files and API promises:

```python
def test_upload_coordinator_limits_active_files_and_one_part_per_file() -> None:
    context = create_js_context()
    context.eval("""
      globalThis.__modules['./api.js'] = {};
      globalThis.__modules['./upload-persistence.js'] = {};
      globalThis.activeParts = {};
      globalThis.peakFiles = 0;
      globalThis.duplicatePart = false;
      globalThis.uuidIndex = 0;
      globalThis.cryptoObject = {
        randomUUID: () => `00000000-0000-4000-8000-${String(++uuidIndex).padStart(12, '0')}`,
        subtle: { digest: () => Promise.resolve(new Uint8Array(32).buffer) },
      };
      globalThis.api = {
        createUploadSession: metadata => Promise.resolve({
          upload_id: metadata.clientRequestId, status: 'queued', confirmed_parts: [],
          confirmed_bytes: 0, chunk_size_bytes: 4,
        }),
        uploadPart: (id, index) => {
          activeParts[id] = (activeParts[id] || 0) + 1;
          duplicatePart = duplicatePart || activeParts[id] > 1;
          peakFiles = Math.max(peakFiles, Object.keys(activeParts).filter(key => activeParts[key] > 0).length);
          return Promise.resolve({ status: 'uploading', confirmed_parts: [index], confirmed_bytes: 4 })
            .finally(() => { activeParts[id] -= 1; });
        },
        completeUpload: id => Promise.resolve({ id: `message-${id}`, upload_id: id }),
        getUploadSession: id => Promise.resolve({ upload_id: id, confirmed_parts: [] }),
      };
      globalThis.persistence = { put: () => Promise.resolve(), getAll: () => Promise.resolve([]), remove: () => Promise.resolve(), close: () => {} };
      globalThis.files = Array.from({ length: 11 }, (_, index) => ({
        name: `file-${index}.txt`, size: 4, lastModified: index,
        slice: () => ({ size: 4, arrayBuffer: () => Promise.resolve(new Uint8Array([1, 2, 3, 4]).buffer) }),
      }));
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval("""
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({
        api, persistence, cryptoObject, now: () => Date.now(),
        delay: () => Promise.resolve(), maxActive: 9, chunkSize: 4,
      });
      coordinator.enqueueFiles(files);
    """)
    drain_jobs(context)
    assert context.eval("peakFiles") == 9
    assert context.eval("duplicatePart") is False


def test_file_identity_requires_metadata_and_sample_digest() -> None:
    context = create_js_context()
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    result = context.eval("typeof __modules['./upload-coordinator.js'].matchesFileIdentity")
    assert result == "function"
```

Also assert oversized files become local failed cards before `createUploadSession`, priority changes queued order only, pause prevents a next part, retry sends only missing indexes, and ETA stays `null` until at least two seconds of samples exist.

- [ ] **Step 2: Run frontend upload tests and verify failure**

Run: `python3 -m pytest -q tests/test_frontend_contract.py -k "upload_coordinator or file_identity or resumable_api"`

Expected: FAIL because `upload-coordinator.js`, `upload-persistence.js`, and resumable API exports do not exist.

- [ ] **Step 3: Implement the raw part XHR and API wrappers**

`uploadPart()` must send the blob directly and expose local bytes:

```javascript
export function uploadPart(uploadId, partIndex, blob, metadata, onProgress, signal) {
  return xhrJson({
    method: 'PUT',
    path: `/api/uploads/${encodeURIComponent(uploadId)}/parts/${partIndex}`,
    body: blob,
    headers: {
      'Content-Type': 'application/octet-stream',
      'Content-Range': `bytes ${metadata.start}-${metadata.end}/${metadata.total}`,
      'X-Chunk-SHA256': metadata.sha256,
    },
    onProgress,
    signal,
  });
}
```

Keep the current `uploadFile()` export for existing consumers and tests. Factor shared XHR status handling into `xhrJson()` while preserving the `session-expired` event on 401.

- [ ] **Step 4: Implement IndexedDB persistence and sampled identity**

Use database `personal-transfer-timeline`, version `1`, object store `upload-tasks`, key path `uploadId`. Persist server metadata, identity metadata, status, confirmed parts, and a `FileSystemFileHandle` only when structured cloning succeeds. Store no `File` or `Blob` fallback.

Sample at most three 64KiB ranges: start, centered middle, and end. Hash a byte sequence containing UTF-8 name, size, lastModified, each range offset, and sampled bytes. `matchesFileIdentity()` compares name, size, lastModified, then the sampled SHA-256.

- [ ] **Step 5: Implement immutable coordinator scheduling and bounded retry**

Use this public task snapshot shape consistently:

```javascript
{
  uploadId, clientRequestId, file, fileHandle, identity,
  name, sizeBytes, mimeType, status, confirmedParts, confirmedBytes,
  inFlightBytes, progressPercent, speedBytesPerSecond, etaSeconds,
  sourceDeviceId, isSourceDevice, errorCode, errorMessage, createdAt,
}
```

`pump()` selects at most nine `queued` or resumable source tasks. Each task has one `AbortController` and awaits one `uploadPart()` before selecting the next missing index. Network errors retry the same unconfirmed index with `[500, 1000, 2000, 4000, 8000]`; exhaustion changes the task to `failed`. A pause sets local intent before aborting; either an abort or a successful response ends the current step, and no subsequent part is selected while paused. Every listener receives `Object.freeze()` snapshots and cannot mutate coordinator state.

- [ ] **Step 6: Run frontend API/coordinator tests and commit**

Run: `python3 -m pytest -q tests/test_frontend_contract.py -k "upload_coordinator or file_identity or resumable_api or persistence"`

Expected: PASS.

```bash
git add web/js/config.js web/js/api.js web/js/upload-persistence.js web/js/upload-coordinator.js tests/test_frontend_contract.py
git commit -m "feat(upload): add browser resumable coordinator"
```

---

### Task 8: Unified Timeline Projection And Composer Surface

**Files:**
- Modify: `web/js/composer.js`
- Modify: `web/js/timeline.js`
- Modify: `web/js/app.js`
- Modify: `web/index.html`
- Modify: `web/styles.css`
- Modify: `tests/test_frontend_contract.py`

**Interfaces:**
- Changes `createComposer({ form, textarea, fileInput, dropTarget, api, timeline })` to `createComposer({ form, textarea, fileInput, dropTarget, api, timeline, uploadCoordinator })`.
- Changes `createTimeline({ container, newMessageButton, api, onRestore })` to `createTimeline({ container, newMessageButton, api, onRestore, onUploadAction })`.
- Timeline additionally returns `upsertUpload(snapshot)`, `removeUpload(uploadId)`, and `getUpload(uploadId)`.
- Consumes coordinator actions `pause`, `resume`, `cancel`, `retry`, `prioritize`, and all batch-control methods.
- Produces `#uploadSummary`, `#pauseAllUploads`, `#resumeAllUploads`, `#cancelAllUploads`, `#transferDropOverlay`, and `#uploadLiveRegion`.

- [ ] **Step 1: Write failing unified projection and composer delegation tests**

```python
def test_composer_delegates_select_drop_and_paste_to_one_coordinator() -> None:
    source = read_web("js/composer.js")
    assert "uploadCoordinator.enqueueFiles" in source
    assert "uploadFile(" not in source
    assert "uploadTasks" not in source
    assert "renderQueue" not in source


def test_timeline_upload_projection_replaces_card_without_duplicate() -> None:
    context = create_js_context()
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval("""
      globalThis.container = document.createElement('div');
      globalThis.timeline = __modules['./timeline.js'].createTimeline({
        container, newMessageButton: null, api: () => Promise.resolve({ items: [] }),
        onRestore: () => {}, onUploadAction: () => {},
      });
      timeline.upsertUpload({ uploadId: 'upload-1', clientRequestId: 'request-1', name: 'a.txt', sizeBytes: 4, status: 'uploading', confirmedBytes: 2, progressPercent: 50 });
      timeline.upsert({ id: 'message-1', upload_id: 'upload-1', client_request_id: 'request-1', created_at: '2026-07-19T00:00:00Z', file: { id: 'upload-1', name: 'a.txt', size: '4 B', download_url: '/download/upload-1' } });
    """)
    assert context.eval("container.querySelectorAll('[data-upload-id=\"upload-1\"]').length") == 1
    assert context.eval("container.querySelectorAll('[data-message-id=\"message-1\"]').length") == 1
```

Also assert all eight textual states render, observer cards omit pause/resume actions, source uploading cards show percent/bytes/speed/ETA, and batch summary hides with zero active tasks.

- [ ] **Step 2: Run timeline/composer tests and verify failure**

Run: `python3 -m pytest -q tests/test_frontend_contract.py -k "composer_delegates or upload_projection or upload_card or upload_summary"`

Expected: FAIL because composer owns a separate queue and timeline has no upload projection.

- [ ] **Step 3: Remove the separate queue ownership and unify file entry points**

Keep text submission unchanged. File input `change`, full-surface `drop`, and clipboard file extraction each call:

```javascript
uploadCoordinator.enqueueFiles(Array.from(files));
```

Delete composer-local task creation, sequential queue processing, progress rendering, cancel, and retry state. Return `{ enqueueFiles: files => uploadCoordinator.enqueueFiles(files) }` only for compatibility with app-level trusted picker actions.

- [ ] **Step 4: Render active uploads and replace by stable identity**

Store uploads in a second `Map` keyed by `uploadId`. Render each active card with `data-upload-id`, status text, progress, error guidance, and action buttons. `upsertMessage()` and `message.created` handling must read `message.upload_id`; when present, remove the active upload node before inserting/replacing the permanent message node. Preserve the original upload card's timeline position by calling `replaceWith(messageElement)` when it exists.

Use this action dispatch shape:

```javascript
onUploadAction({
  action: button.dataset.uploadAction,
  uploadId: button.closest('[data-upload-id]').dataset.uploadId,
});
```

- [ ] **Step 5: Add aggregate controls, full-surface overlay, and responsive card CSS**

Add markup inside `transferPage`:

```html
<section class="upload-summary" id="uploadSummary" aria-label="上传任务" hidden>
  <p id="uploadSummaryText">0 个活动任务</p>
  <div class="upload-summary-actions">
    <button class="btn btn-soft" id="pauseAllUploads" type="button">全部暂停</button>
    <button class="btn btn-soft" id="resumeAllUploads" type="button">全部继续</button>
    <button class="btn btn-danger" id="cancelAllUploads" type="button">全部取消</button>
  </div>
</section>
<div class="transfer-drop-overlay" id="transferDropOverlay" aria-hidden="true">释放文件以添加到传输时间线</div>
<div class="visually-hidden" id="uploadLiveRegion" aria-live="polite" aria-atomic="true"></div>
```

Use a progress element or CSS class buckets (`progress-0` through `progress-100` in 5% increments) so CSP remains compatible without inline style writes.

- [ ] **Step 6: Run unified UI tests and commit**

Run: `python3 -m pytest -q tests/test_frontend_contract.py -k "composer or timeline or upload_card or upload_summary or drop_overlay"`

Expected: PASS.

```bash
git add web/js/composer.js web/js/timeline.js web/js/app.js web/index.html web/styles.css tests/test_frontend_contract.py
git commit -m "feat(ui): merge uploads into transfer timeline"
```

---

### Task 9: Refresh Recovery, Cross-Device Updates, And Accessibility

**Files:**
- Modify: `web/js/upload-coordinator.js`
- Modify: `web/js/upload-persistence.js`
- Modify: `web/js/timeline.js`
- Modify: `web/js/app.js`
- Modify: `web/index.html`
- Modify: `web/styles.css`
- Modify: `tests/test_frontend_contract.py`
- Modify: `tests/test_browser_e2e.py`

**Interfaces:**
- Consumes server `GET /api/uploads/active` before local reconciliation.
- Consumes WebSocket upload events through `uploadCoordinator.applyRemoteEvent(event)`.
- Produces coordinator method `reselect(uploadId: string, file: File) -> Promise<boolean>`.
- Produces action guidance `source_device_required` for observer pause/resume attempts.
- Produces coarse live announcements for state changes and 25%, 50%, 75%, and 100% milestones.

- [ ] **Step 1: Write failing refresh, observer, reselect, and accessibility tests**

```python
def test_app_reconciles_active_uploads_before_timeline_restore() -> None:
    source = read_web("js/app.js")
    reconcile_index = source.index("await uploadCoordinator.reconcile()")
    timeline_index = source.index("await timeline.loadInitial()")
    assert reconcile_index < timeline_index
    assert "uploadCoordinator.applyRemoteEvent(event)" in source


def test_upload_controls_have_touch_targets_and_reduced_motion() -> None:
    css = read_web("styles.css")
    assert ".upload-card-action" in css
    assert "min-width: 44px" in css
    assert "min-height: 44px" in css
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".timeline-upload-card" in reduced
    assert "transition: none" in reduced


def test_live_region_announces_state_and_coarse_milestones_only() -> None:
    source = read_web("js/upload-coordinator.js")
    assert "LIVE_MILESTONES" in source
    assert "[25, 50, 75, 100]" in source
    assert "UPLOAD_ANNOUNCEMENT_INTERVAL_MS" in source
```

Add browser cases that refresh after one confirmed part, reselect the same file, reject a mismatched file, open a second authenticated page as observer, and verify remote cancellation reaches both pages.

- [ ] **Step 2: Run focused recovery/accessibility tests and verify failure**

Run: `python3 -m pytest -q tests/test_frontend_contract.py tests/test_browser_e2e.py -k "reconcile or reselect or observer or upload_controls or live_region"`

Expected: FAIL because startup ordering, reselect verification, remote upload events, and upload accessibility contracts are incomplete.

- [ ] **Step 3: Reconcile server sessions before local records**

`reconcile()` must:

1. Fetch server active sessions first.
2. Load IndexedDB records second.
3. Merge by `uploadId`, trusting server status and confirmed parts.
4. Call `fileHandle.queryPermission({ mode: 'read' })`; call `getFile()` only when permission is `granted`.
5. Auto-resume a source task with an authorized matching handle.
6. Keep a source task `paused` with `errorCode='reselect_required'` when no authorized handle exists.
7. Render server-only tasks as observer snapshots with `file=null` and `isSourceDevice=false`.
8. Remove local records for server terminal sessions after applying their final event/message.

After a 401 unlock succeeds, call `reconcile()` again before pumping queued chunks. Confirmed indexes returned by the server are always removed from the local send set.

- [ ] **Step 4: Implement exact reselect verification and cross-device event application**

`reselect(uploadId, file)` computes the sampled identity and requires exact equality for `name`, `size`, `lastModified`, and `sampleSha256`. A mismatch keeps `paused` and sets `file_mismatch`. A match stores the new file reference, clears the recoverable error, calls source-authorized resume, and sends only server-missing indexes.

Handle events with these mappings:

```javascript
const UPLOAD_EVENT_HANDLERS = {
  'upload.created': payload => upsertRemote(payload),
  'upload.progress': payload => mergeRemoteProgress(payload),
  'upload.state_changed': payload => mergeRemoteState(payload),
  'upload.completed': payload => completeRemote(payload),
  'upload.cancelled': payload => terminalRemote(payload, 'cancelled'),
  'upload.expired': payload => terminalRemote(payload, 'expired'),
};
```

Observer pause/resume buttons remain absent. When an observer invokes an action through another surface, show `源设备控制暂停和继续`; observer cancel remains enabled.

- [ ] **Step 5: Implement throttled announcements and responsive accessibility**

Track the last announced state, milestone, and timestamp per upload. Announce state transitions immediately and visual progress milestones at 25% increments, with at least 1000ms between progress announcements. Use textual `计算中` while speed samples span less than 2000ms. Add visible keyboard file selection equivalent, 44px targets, focus-visible styles, mobile card wrapping, and reduced-motion overrides for progress/card transitions.

- [ ] **Step 6: Run contract and browser recovery tests and commit**

Run: `python3 -m pytest -q tests/test_frontend_contract.py tests/test_browser_e2e.py -k "reconcile or refresh or reselect or observer or accessibility or live_region or reduced_motion"`

Expected: PASS with confirmed chunks omitted after refresh and session re-authentication.

```bash
git add web/js/upload-coordinator.js web/js/upload-persistence.js web/js/timeline.js web/js/app.js web/index.html web/styles.css tests/test_frontend_contract.py tests/test_browser_e2e.py
git commit -m "feat(upload): recover and synchronize transfer tasks"
```

---

### Task 10: 40MB And 512MB End-To-End Verification, Documentation, And Full Regression

**Files:**
- Modify: `tests/test_browser_e2e.py`
- Create: `tests/test_large_upload.py`
- Create: `pytest.ini`
- Modify: `README.md`

**Interfaces:**
- Consumes the complete `/api/uploads*` protocol and Transfer UI.
- Produces default 40MB constrained-request coverage.
- Produces separately selected sparse 512MB server SHA-256 and bounded-memory coverage.
- Documents migration criteria while preserving `POST /api/upload`.

- [ ] **Step 1: Add failing 40MB browser and nine-file overflow E2E tests**

Generate content during the test and use Playwright's file input:

```python
def test_resumable_40mb_upload_uses_bounded_parts_and_completes(browser_session: BrowserSession, tmp_path: Path) -> None:
    source = tmp_path / "forty-megabytes.bin"
    block = bytes(range(256)) * 4096
    with source.open("wb") as output:
        for _ in range(40):
            output.write(block)

    page = browser_session.page
    _open_locked_application(browser_session)
    _unlock(page)
    requests: list[int] = []
    page.on(
        "request",
        lambda request: requests.append(len(request.post_data_buffer or b""))
        if "/parts/" in request.url else None,
    )
    page.locator("#composerFileInput").set_input_files(str(source))
    expect(page.locator('[data-upload-status="complete"]')).to_be_visible(timeout=120_000)
    assert requests
    assert max(requests) <= 8 * 1024 * 1024
    _assert_browser_clean(browser_session)


def test_eleven_files_show_nine_uploading_and_two_queued(browser_session: BrowserSession, tmp_path: Path) -> None:
    paths = create_test_files(tmp_path, count=11, size_bytes=16 * 1024 * 1024)
    page = browser_session.page
    _open_locked_application(browser_session)
    _unlock(page)
    install_slow_part_responses(page)
    page.locator("#composerFileInput").set_input_files([str(path) for path in paths])
    expect(page.locator('[data-upload-status="uploading"]')).to_have_count(9)
    expect(page.locator('[data-upload-status="queued"]')).to_have_count(2)


def create_test_files(tmp_path: Path, count: int, size_bytes: int) -> list[Path]:
    paths: list[Path] = []
    block = b"transfer-e2e" * 1024
    for index in range(count):
        path = tmp_path / f"file-{index}.bin"
        with path.open("wb") as output:
            remaining = size_bytes
            while remaining:
                chunk = block[:min(len(block), remaining)]
                output.write(chunk)
                remaining -= len(chunk)
        paths.append(path)
    return paths


def install_slow_part_responses(page: Page) -> None:
    def slow_part(route: Route) -> None:
        time.sleep(0.25)
        route.continue_()

    page.route("**/api/uploads/*/parts/*", slow_part)
```

Import `Page` and `Route` from the existing Playwright sync API imports. Each generated file uses deterministic bytes and is removed by `tmp_path` cleanup.

- [ ] **Step 2: Run 40MB and scheduler E2E tests and verify failure**

Run: `python3 -m pytest -q tests/test_browser_e2e.py -k "40mb or eleven_files"`

Expected: FAIL until the final resumable UI path completes and exposes the expected card states.

- [ ] **Step 3: Add the separately marked sparse 512MB verification**

Create marker configuration:

```ini
[pytest]
addopts = -m "not large"
markers =
    large: resource-intensive sparse 512MB resumable upload verification
```

Create the sparse file and upload one 8MB range at a time without a repository fixture:

```python
def authenticated_large_client(settings: Settings) -> TestClient:
    client = TestClient(create_app(settings))
    response = client.post("/api/session", json={
        "access_token": settings.auth_token,
        "device_id": "large-test",
        "device_name": "Large test",
    })
    assert response.status_code == 200
    return client


def create_upload_for_path(client: TestClient, path: Path, chunk_size: int) -> dict[str, object]:
    response = client.post("/api/uploads", json={
        "client_request_id": "large-upload-request",
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "mime_type": "application/octet-stream",
        "last_modified_ms": int(path.stat().st_mtime * 1000),
        "chunk_size_bytes": chunk_size,
        "sample_sha256": sha256(path.name.encode()).hexdigest(),
    })
    assert response.status_code == 200
    return response.json()


def put_large_part(client: TestClient, upload: dict[str, object], part_index: int, chunk: bytes) -> None:
    chunk_size = int(upload["chunk_size_bytes"])
    start = part_index * chunk_size
    end = start + len(chunk) - 1
    response = client.put(
        f"/api/uploads/{upload['upload_id']}/parts/{part_index}",
        content=chunk,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Range": f"bytes {start}-{end}/{upload['size_bytes']}",
            "X-Chunk-SHA256": sha256(chunk).hexdigest(),
        },
    )
    assert response.status_code == 200


@pytest.mark.large
def test_sparse_512mb_upload_completes_with_server_sha256(settings: Settings, tmp_path: Path) -> None:
    size = 512 * 1024 * 1024
    source = tmp_path / "sparse-512mb.bin"
    with source.open("wb") as output:
        output.seek(size - 1)
        output.write(b"\0")

    expected = sha256()
    client = authenticated_large_client(replace(settings, max_upload_size=size))
    upload = create_upload_for_path(client, source, chunk_size=8 * 1024 * 1024)
    tracemalloc.start()
    with source.open("rb") as input_file:
        for part_index, chunk in enumerate(iter(lambda: input_file.read(8 * 1024 * 1024), b"")):
            expected.update(chunk)
            put_large_part(client, upload, part_index, chunk)
    response = client.post(f"/api/uploads/{upload['upload_id']}/complete", json={})
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert response.status_code == 200
    assert response.json()["file"]["sha256"] == expected.hexdigest()
    assert peak < 40 * 1024 * 1024
```

The test-local helpers use path metadata and exact range/digest headers, so the large test remains independently executable.

- [ ] **Step 4: Run the separate 512MB test**

Run: `python3 -m pytest -q -m large tests/test_large_upload.py`

Expected: PASS; final SHA-256 matches and traced Python peak memory stays below 40MB.

- [ ] **Step 5: Update API, configuration, recovery, and migration documentation**

Document all `/api/uploads*` methods and headers, source-device control semantics, 24-hour expiry, 8MB default chunks, nine-file browser concurrency, storage reserve/capacity settings, WebSocket event names, refresh/reselect behavior, normal test command, and separate large-test command. Label `POST /api/upload` as the legacy whole-file route and state its removal criteria: all documented consumers use `/api/uploads*` and one release has passed with resumable telemetry and regression coverage.

- [ ] **Step 6: Run the complete default verification matrix**

Run:

```bash
python3 -m pytest -q
python3 -m pytest -q tests/test_frontend_contract.py
python3 -m pytest -q tests/test_browser_e2e.py
python3 -m compileall -q app server.py tests
git diff --check
```

Expected: default pytest excludes only the `large` marker; all remaining tests pass, browser tests report no page/console/CSP errors, compile check exits 0, and `git diff --check` produces no output.

- [ ] **Step 7: Commit end-to-end coverage and documentation**

```bash
git add tests/test_browser_e2e.py tests/test_large_upload.py pytest.ini README.md
git commit -m "test(upload): verify resumable large-file workflow"
```

---

## Final Review Checklist

| Requirement | Implemented and verified by |
|---|---|
| 1. Unified Transfer Timeline | Tasks 8, 9, and 10 |
| 2. Multi-File Scheduling | Tasks 7, 8, and 10 |
| 3. Large And Resumable Uploads | Tasks 1, 3, 4, 7, and 10 |
| 4. Task Controls And State | Tasks 2, 4, 7, and 8 |
| 5. Cross-Refresh Recovery | Tasks 5, 7, and 9 |
| 6. Cross-Device Synchronization | Tasks 6 and 9 |
| 7. Integrity And Atomic Publication | Tasks 3 and 5 |
| 8. Idempotency And Recovery | Tasks 2, 4, and 5 |
| 9. Security And Resource Protection | Tasks 1, 3, 4, and 6 |
| 10. Error Feedback And Accessibility | Tasks 8 and 9 |
| 11. Compatibility And Verification | Tasks 4, 5, and 10 |

- [ ] Every acceptance criterion in Requirements 1 through 11 maps to at least one task and executable test above.
- [ ] Every created or modified path appears in the Exact File Map and in one or more task `Files` blocks.
- [ ] Every cross-task function name, parameter, return type, status, event name, field name, and DOM ID is consistent.
- [ ] The first confirmed part owns `queued -> uploading`; request start alone preserves `queued`.
- [ ] A started part may confirm after pause while the durable status remains `paused`; cancel rejects its confirmation.
- [ ] Only successful part confirmation and successful state changes renew the 24-hour expiry after creation.
- [ ] Complete cancellation returns 409 and completion replay returns the same permanent message.
- [ ] Whole-file SHA-256 is computed by the server during bounded assembly.
- [ ] Publication recovery covers `assembling`, `assembled`, `file_published`, and `published` without exposing incomplete files.
- [ ] Runtime dependencies remain unchanged and browser code uses native Web Crypto and IndexedDB.
- [ ] The 512MB test is marked `large`, omitted from default verification, and run explicitly.
- [ ] Legacy upload, sessions, WebSocket replay, timeline, file library, download, batch download, soft delete, restore, purge, CSP, mobile, and accessibility regressions remain covered.
