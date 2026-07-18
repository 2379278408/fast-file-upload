from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from secrets import compare_digest
from time import monotonic, time
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator
from starlette.websockets import WebSocketDisconnect

from .auth import (
    SESSION_COOKIE,
    LoginRateLimiter,
    SessionData,
    decode_session,
    encode_session,
    require_session,
)
from .config import SESSION_DAYS, Settings
from .database import Database
from .events import EventHub
from .repository import (
    BatchDownloadSourceMissing,
    BatchDownloadTooLarge,
    IdempotencyConflict,
    MessageRepository,
    NoDownloadableFiles,
    RestoreWindowExpired,
    utc_now,
)
from .storage import FILE_ID_PATTERN, FileStorage


logger = logging.getLogger("transfer.upload")

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
MESSAGE_ID_PATTERN = r"^[0-9]{13}[0-9a-f]{32}$"
Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=IDENTIFIER_PATTERN),
]
MessageId = Annotated[
    str,
    StringConstraints(min_length=45, max_length=45, pattern=MESSAGE_ID_PATTERN),
]


class SessionRequest(BaseModel):
    access_token: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    device_id: Identifier
    device_name: Annotated[str, StringConstraints(min_length=1, max_length=40)]

    @field_validator("device_name")
    @classmethod
    def validate_device_name(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 40:
            raise ValueError("device_name must contain between 1 and 40 characters")
        return value


class TextMessageRequest(BaseModel):
    body: Annotated[str, StringConstraints(min_length=1, max_length=10_000)]
    client_request_id: Identifier


class BatchRequest(BaseModel):
    message_ids: Annotated[list[MessageId], Field(min_length=1, max_length=50)]


class _LockEntry:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class _KeyedLockCapacityExceeded(RuntimeError):
    pass


class _KeyedLockPool:
    def __init__(self, max_entries: int = 1024) -> None:
        if max_entries < 1:
            raise ValueError("Keyed lock capacity must be at least 1")
        self._entries: dict[str, _LockEntry] = {}
        self._max_entries = max_entries
        self._loop: asyncio.AbstractEventLoop | None = None
        self._binding_guard = threading.Lock()

    def _bind_running_loop(self) -> None:
        loop = asyncio.get_running_loop()
        with self._binding_guard:
            if self._loop is None:
                self._loop = loop
            elif self._loop is not loop:
                raise RuntimeError(
                    "Keyed lock pool active entries are bound to a single event loop"
                )

    @asynccontextmanager
    async def hold(self, key: str):
        self._bind_running_loop()
        entry = self._entries.get(key)
        if entry is None:
            if len(self._entries) >= self._max_entries:
                raise _KeyedLockCapacityExceeded("Keyed lock capacity exceeded")
            entry = _LockEntry()
            self._entries[key] = entry
        entry.users += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            entry.users -= 1
            if entry.users == 0 and self._entries.get(key) is entry:
                del self._entries[key]
                if not self._entries:
                    with self._binding_guard:
                        self._loop = None

    async def run(self, key: str, operation, *args):
        async with self.hold(key):
            operation_task = asyncio.create_task(asyncio.to_thread(operation, *args))
            try:
                return await asyncio.shield(operation_task)
            except asyncio.CancelledError:
                try:
                    await operation_task
                except BaseException:
                    pass
                raise

    @property
    def size(self) -> int:
        return len(self._entries)

    def users_for(self, key: str) -> int:
        entry = self._entries.get(key)
        return entry.users if entry is not None else 0


class _TemporaryPathRegistry:
    def __init__(self) -> None:
        self._paths: set[Path] = set()
        self._guard = threading.Lock()

    def create(self) -> Path:
        temporary = tempfile.NamedTemporaryFile(
            prefix="transfer-", suffix=".zip", delete=False
        )
        path = Path(temporary.name)
        temporary.close()
        with self._guard:
            self._paths.add(path)
        return path

    def cleanup(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        with self._guard:
            self._paths.discard(path)

    @property
    def paths(self) -> list[Path]:
        with self._guard:
            return sorted(self._paths)

    @property
    def size(self) -> int:
        with self._guard:
            return len(self._paths)


class _AsyncOnceCleanup:
    def __init__(self, cleanup: Callable[[], Awaitable[None]]) -> None:
        self._cleanup = cleanup
        self._task: asyncio.Task[None] | None = None

    async def __call__(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._cleanup())
        await asyncio.shield(self._task)


class _CleanupStreamingResponse(StreamingResponse):
    def __init__(
        self,
        *args,
        cleanup: _AsyncOnceCleanup,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._cleanup = cleanup

    async def _run_cleanup(self) -> None:
        await self._cleanup()

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    await close()
            finally:
                await self._run_cleanup()


def create_app(settings: Settings) -> FastAPI:
    database = Database(settings.database_path)
    database.initialize()
    messages = MessageRepository(database)
    storage = FileStorage(settings.upload_dir, settings.max_upload_size, settings.allowed_extensions)
    zip_temp_paths = _TemporaryPathRegistry()
    zip_cleanup_tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI) -> AsyncIterator[None]:
        recovered = await asyncio.to_thread(messages.recover_upload_reservations, storage)
        if recovered:
            logger.info("Recovered %d interrupted uploads", len(recovered))
        result = await asyncio.to_thread(messages.import_legacy_files, storage)
        if result.imported or result.skipped or result.failed:
            logger.info(
                "Legacy migration: imported=%d skipped=%d failed=%d",
                result.imported,
                result.skipped,
                result.failed,
            )
        stop_maintenance = asyncio.Event()

        async def run_purge() -> None:
            recovered_claims = await asyncio.to_thread(
                messages.recover_purge_claims,
                storage,
                lifespan_app.state.clock(),
                settings.purge_claim_lease_seconds,
            )
            await broadcast_mutation(recovered_claims)
            mutation = await asyncio.to_thread(
                messages.purge_expired_files,
                storage,
                lifespan_app.state.clock(),
                settings.undo_seconds,
            )
            await broadcast_mutation(mutation)

        async def maintenance_worker() -> None:
            while not stop_maintenance.is_set():
                try:
                    await asyncio.wait_for(
                        stop_maintenance.wait(),
                        timeout=settings.maintenance_interval_seconds,
                    )
                except TimeoutError:
                    try:
                        await run_purge()
                    except Exception:
                        logger.exception("Periodic purge failed")

        await run_purge()
        maintenance_task = asyncio.create_task(maintenance_worker())
        lifespan_app.state.maintenance_task = maintenance_task
        try:
            yield
        finally:
            stop_maintenance.set()
            await maintenance_task
            if zip_cleanup_tasks:
                await asyncio.gather(*tuple(zip_cleanup_tasks), return_exceptions=True)

    app = FastAPI(title=settings.app_title, description="A polished file upload service with optional governance controls", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.messages = messages
    app.state.storage = storage
    app.state.zip_temp_paths = zip_temp_paths
    app.state.zip_cleanup_tasks = zip_cleanup_tasks
    app.state.clock = utc_now
    hub = EventHub()
    hub.set_fetch_missing(messages.events_after)
    app.state.hub = hub
    login_limiter = LoginRateLimiter(
        settings.login_rate_limit_count,
        settings.login_rate_limit_window_seconds,
        settings.login_rate_limit_max_clients,
    )
    app.state.login_limiter = login_limiter
    rate_buckets: dict[tuple[str, str], list[float]] = {}
    RATE_BUCKETS_MAX = 10_000

    async def broadcast_mutation(mutation: dict[str, object]) -> None:
        events = mutation.get("events")
        if not isinstance(events, list):
            event = mutation.get("event")
            events = [event] if isinstance(event, dict) else []
        for event in events:
            if isinstance(event, dict):
                await hub.broadcast(event)

    def track_zip_cleanup(task: asyncio.Task[None]) -> None:
        zip_cleanup_tasks.add(task)
        task.add_done_callback(zip_cleanup_tasks.discard)

    async def cleanup_zip_after_build(
        build_task: asyncio.Task[object], zip_path: Path
    ) -> None:
        try:
            await build_task
        except BaseException:
            pass
        await asyncio.to_thread(zip_temp_paths.cleanup, zip_path)

    async def create_zip_path() -> Path:
        create_task = asyncio.create_task(asyncio.to_thread(zip_temp_paths.create))
        try:
            return await asyncio.shield(create_task)
        except asyncio.CancelledError:
            async def cleanup_created_path() -> None:
                try:
                    path = await create_task
                except BaseException:
                    return
                await asyncio.to_thread(zip_temp_paths.cleanup, path)

            track_zip_cleanup(asyncio.create_task(cleanup_created_path()))
            raise

    async def stream_zip(
        zip_path: Path, cleanup: _AsyncOnceCleanup
    ) -> AsyncIterator[bytes]:
        source = None
        try:
            source = await asyncio.to_thread(zip_path.open, "rb")
            while True:
                chunk = await asyncio.to_thread(source.read, 64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                if source is not None:
                    await asyncio.to_thread(source.close)
            finally:
                await cleanup()

    async def cleanup_response_zip(zip_path: Path) -> None:
        cleanup_task = asyncio.current_task()
        if cleanup_task is not None:
            track_zip_cleanup(cleanup_task)
        await asyncio.to_thread(zip_temp_paths.cleanup, zip_path)

    @app.middleware("http")
    async def add_security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'",
        )
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

    audit_path = settings.upload_dir / ".audit.jsonl"
    web_dir = Path(__file__).resolve().parent.parent / "web"
    web_root = web_dir / "index.html"
    upload_locks = _KeyedLockPool(settings.client_request_lock_capacity)
    app.state.upload_locks = upload_locks

    def record_audit(action: str, file_id: str, name: str, size_bytes: int, **extra: object) -> None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "file_id": file_id,
            "name": name,
            "size_bytes": size_bytes,
            **extra,
        }
        with audit_path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_audit_events(limit: int = 200) -> list[dict[str, object]]:
        """Read the last `limit` audit events from the log file.

        Uses backward seeking in 8KB chunks. Complexity is O(limit * chunk_size)
        in the worst case, but performs well for the default 200-line limit.
        """
        if not audit_path.exists():
            return []
        with audit_path.open("rb") as source:
            source.seek(0, 2)
            position = source.tell()
            tail = b""
            while position > 0 and tail.count(b"\n") <= limit:
                chunk_size = min(8192, position)
                position -= chunk_size
                source.seek(position)
                tail = source.read(chunk_size) + tail
        lines = tail.splitlines()[-limit:]
        events: list[dict[str, object]] = []
        for line in lines:
            try:
                events.append(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return events

    def enforce_rate_limit(request: Request) -> None:
        if settings.rate_limit_count <= 0:
            return
        now = monotonic()
        window_start = now - settings.rate_limit_window_seconds
        client = request.client.host if request.client else "unknown"
        bucket_key = (client, request.url.path)
        bucket = [item for item in rate_buckets.get(bucket_key, []) if item >= window_start]
        if len(bucket) >= settings.rate_limit_count:
            rate_buckets[bucket_key] = bucket
            raise HTTPException(status_code=429, detail="Too many requests")
        bucket.append(now)
        rate_buckets[bucket_key] = bucket
        if len(rate_buckets) > RATE_BUCKETS_MAX:
            stale_keys = [k for k, v in rate_buckets.items() if not v or v[-1] < window_start]
            for k in stale_keys[:len(stale_keys) // 2]:
                del rate_buckets[k]

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        if not web_root.exists():
            return HTMLResponse("<h1>Missing web/index.html</h1>", status_code=500)
        return HTMLResponse(web_root.read_text(encoding="utf-8"))

    @app.get("/api/health")
    async def health(
        _: SessionData = Depends(require_session),
    ) -> dict[str, str | int | bool]:
        stats = await asyncio.to_thread(storage.stats)
        return {
            "ok": True,
            "protected": bool(settings.auth_token),
            **stats,
        }

    @app.post("/api/session")
    async def create_session(
        payload: SessionRequest, request: Request, response: Response
    ) -> SessionData:
        client = request.client.host if request.client else "unknown"
        if compare_digest(payload.access_token, settings.auth_token):
            login_limiter.reset(client)
        else:
            if login_limiter.record_failure(client):
                raise HTTPException(status_code=429, detail="Too many login failures")
            raise HTTPException(status_code=401, detail="Invalid access token")
        max_age = SESSION_DAYS * 86400
        session = SessionData(
            device_id=payload.device_id,
            device_name=payload.device_name,
            expires_at=int(time()) + max_age,
        )
        response.set_cookie(
            SESSION_COOKIE,
            encode_session(session, settings.session_secret),
            max_age=max_age,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
        )
        return session

    @app.get("/api/session")
    async def get_session(session: SessionData = Depends(require_session)) -> SessionData:
        return session

    @app.post("/api/messages")
    async def create_text_message(
        payload: TextMessageRequest,
        session: SessionData = Depends(require_session),
    ) -> dict[str, object]:
        try:
            mutation = await upload_locks.run(
                payload.client_request_id,
                messages.create_text,
                payload.body,
                payload.client_request_id,
                session,
            )
        except _KeyedLockCapacityExceeded as error:
            raise HTTPException(
                status_code=503, detail="Too many active client requests"
            ) from error
        except IdempotencyConflict as error:
            raise HTTPException(
                status_code=409,
                detail="client_request_id was already used for another operation",
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        await broadcast_mutation(mutation)
        result = mutation["result"]
        if not isinstance(result, dict):
            raise RuntimeError("Message mutation returned an invalid result")
        return result

    @app.get("/api/messages")
    async def list_messages(
        before: MessageId | None = None,
        limit: int = Query(default=50, ge=1, le=50),
        _: SessionData = Depends(require_session),
    ) -> dict[str, object]:
        return await asyncio.to_thread(messages.list_messages, before, limit)

    @app.get("/api/search")
    async def search_messages(
        q: Annotated[str, Query(min_length=1, max_length=1000)],
        cursor: MessageId | None = None,
        limit: int = Query(default=50, ge=1, le=50),
        _: SessionData = Depends(require_session),
    ) -> dict[str, object]:
        return await asyncio.to_thread(messages.search, q, cursor, limit)

    @app.delete("/api/messages/{message_id}")
    async def delete_message(
        message_id: MessageId,
        request: Request,
        _: SessionData = Depends(require_session),
    ) -> dict[str, object]:
        mutation = await asyncio.to_thread(
            messages.soft_delete, message_id, request.app.state.clock()
        )
        if mutation is None:
            raise HTTPException(status_code=404, detail="Message not found")
        await broadcast_mutation(mutation)
        result = mutation["result"]
        if not isinstance(result, dict):
            raise RuntimeError("Delete mutation returned an invalid result")
        return result

    @app.post("/api/messages/{message_id}/restore")
    async def restore_message(
        message_id: MessageId,
        request: Request,
        _: SessionData = Depends(require_session),
    ) -> dict[str, object]:
        try:
            mutation = await asyncio.to_thread(
                messages.restore,
                message_id,
                request.app.state.clock(),
                settings.undo_seconds,
            )
        except RestoreWindowExpired as error:
            raise HTTPException(status_code=409, detail="Restore window expired") from error
        if mutation is None:
            raise HTTPException(status_code=404, detail="Message not found")
        await broadcast_mutation(mutation)
        result = mutation["result"]
        if not isinstance(result, dict):
            raise RuntimeError("Restore mutation returned an invalid result")
        return result

    @app.post("/api/maintenance/purge")
    async def purge_expired_files(
        request: Request, _: SessionData = Depends(require_session)
    ) -> dict[str, object]:
        mutation = await asyncio.to_thread(
            messages.purge_expired_files,
            storage,
            request.app.state.clock(),
            settings.undo_seconds,
        )
        await broadcast_mutation(mutation)
        return {"purged": mutation["result"]}

    @app.delete("/api/session")
    async def delete_session(request: Request, response: Response) -> dict[str, bool]:
        response.delete_cookie(
            SESSION_COOKIE,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
        )
        return {"ok": True}

    @app.get("/api/files")
    async def list_file_messages(
        cursor: MessageId | None = None,
        limit: int = Query(default=50, ge=1, le=50),
        type: Literal["image", "document"] | None = None,
        device_id: Identifier | None = None,
        from_date: date | None = Query(default=None, alias="from"),
        to: date | None = None,
        q: str | None = None,
        _: SessionData = Depends(require_session),
    ) -> dict[str, object]:
        if from_date is not None and to is not None and from_date > to:
            raise HTTPException(status_code=422, detail="from must not be after to")
        to_exclusive = (to + timedelta(days=1)).isoformat() if to else None
        return await asyncio.to_thread(
            messages.list_file_messages,
            cursor,
            limit,
            type,
            device_id,
            from_date.isoformat() if from_date else None,
            to_exclusive,
            q,
        )

    @app.post("/api/files/batch-download")
    async def batch_download(
        payload: BatchRequest,
        _: SessionData = Depends(require_session),
    ) -> StreamingResponse:
        zip_path = await create_zip_path()
        build_task = asyncio.create_task(
            asyncio.to_thread(
                messages.build_batch_download_zip,
                payload.message_ids,
                storage,
                settings.max_batch_download_total_bytes,
                zip_path,
            )
        )
        try:
            await asyncio.shield(build_task)
        except asyncio.CancelledError:
            cleanup_task = asyncio.create_task(
                cleanup_zip_after_build(build_task, zip_path)
            )
            track_zip_cleanup(cleanup_task)
            raise
        except BatchDownloadSourceMissing as error:
            await asyncio.to_thread(zip_temp_paths.cleanup, zip_path)
            raise HTTPException(status_code=404, detail=str(error)) from error
        except BatchDownloadTooLarge as error:
            await asyncio.to_thread(zip_temp_paths.cleanup, zip_path)
            raise HTTPException(status_code=413, detail=str(error)) from error
        except NoDownloadableFiles as error:
            await asyncio.to_thread(zip_temp_paths.cleanup, zip_path)
            raise HTTPException(
                status_code=404, detail="No downloadable files found"
            ) from error
        except BaseException:
            await asyncio.to_thread(zip_temp_paths.cleanup, zip_path)
            raise
        cleanup = _AsyncOnceCleanup(lambda: cleanup_response_zip(zip_path))
        return _CleanupStreamingResponse(
            stream_zip(zip_path, cleanup),
            cleanup=cleanup,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="files.zip"',
            },
        )

    @app.post("/api/messages/batch-delete")
    async def batch_delete(
        payload: BatchRequest,
        _: SessionData = Depends(require_session),
    ) -> dict[str, object]:
        mutation = await asyncio.to_thread(messages.batch_soft_delete, payload.message_ids)
        await broadcast_mutation(mutation)
        return {"deleted": int(mutation["result"]), "deleted_ids": mutation.get("deleted_ids", [])}

    @app.get("/api/storage")
    async def storage_audit(
        _: SessionData = Depends(require_session),
    ) -> dict[str, object]:
        return await asyncio.to_thread(messages.storage_audit, settings.upload_dir)

    @app.get("/api/audit")
    async def audit(_: SessionData = Depends(require_session)) -> dict[str, object]:
        return {"events": await asyncio.to_thread(read_audit_events)}

    @app.get("/api/admin/summary")
    async def admin_summary(_: SessionData = Depends(require_session)) -> dict[str, object]:
        return await asyncio.to_thread(storage.admin_summary)

    def process_upload(
        file: UploadFile, client_request_id: str, session: SessionData
    ) -> tuple[dict[str, object], dict[str, object] | None]:
        def execute() -> tuple[dict[str, object], dict[str, object] | None]:
            existing = messages.get_message_by_client_request_id(client_request_id)
            if existing is not None:
                if existing["kind"] not in {"file", "image"}:
                    raise HTTPException(
                        status_code=409,
                        detail="client_request_id was already used for another operation",
                    )
                messages.delete_upload_reservation(client_request_id)
                return existing, None

            reservation = messages.get_upload_reservation(client_request_id)
            if reservation is not None:
                recovered = storage.pending_from_reservation(reservation)
                if recovered.final_path.is_file():
                    try:
                        mutation = messages.create_file_message(
                            recovered, client_request_id, session
                        )
                    except IdempotencyConflict as error:
                        raise HTTPException(
                            status_code=409,
                            detail="client_request_id was already used for another operation",
                        ) from error
                    message = mutation["result"]
                    if not isinstance(message, dict):
                        raise RuntimeError("Upload mutation returned an invalid result")
                    try:
                        record_audit(
                            "upload",
                            recovered.file_id,
                            recovered.original_name,
                            recovered.size_bytes,
                        )
                    except OSError:
                        logger.exception(
                            "Failed to write audit log for upload %s", recovered.file_id
                        )
                    return message, mutation
                storage.discard(recovered)
                messages.delete_upload_reservation(client_request_id)

            pending = storage.stage_upload(file)
            try:
                if not messages.create_upload_reservation(
                    pending, client_request_id, session
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Upload with this client_request_id is already in progress",
                    )
                storage.publish(pending)
                mutation = messages.create_file_message(
                    pending, client_request_id, session
                )
                message = mutation["result"]
                if not isinstance(message, dict):
                    raise RuntimeError("Upload mutation returned an invalid result")
            except Exception as error:
                storage.discard(pending)
                try:
                    messages.record_upload_compensation(pending, type(error).__name__)
                except Exception:
                    logger.exception(
                        "Failed to record upload compensation for %s", pending.file_id
                    )
                try:
                    record_audit(
                        "upload.discarded",
                        pending.file_id,
                        pending.original_name,
                        pending.size_bytes,
                        reason=type(error).__name__,
                    )
                except OSError:
                    logger.exception(
                        "Failed to write audit log for discarded upload %s",
                        pending.file_id,
                    )
                if isinstance(error, IdempotencyConflict):
                    raise HTTPException(
                        status_code=409,
                        detail="client_request_id was already used for another operation",
                    ) from error
                raise

            try:
                record_audit(
                    "upload",
                    pending.file_id,
                    pending.original_name,
                    pending.size_bytes,
                )
            except OSError:
                logger.exception(
                    "Failed to write audit log for upload %s", pending.file_id
                )
            return message, mutation

        return execute()

    @app.post("/api/upload")
    async def upload(
        file: UploadFile,
        client_request_id: Annotated[
            str,
            Form(min_length=1, max_length=128, pattern=IDENTIFIER_PATTERN),
        ],
        session: SessionData = Depends(require_session),
        __: None = Depends(enforce_rate_limit),
    ) -> dict[str, object]:
        try:
            message, mutation = await upload_locks.run(
                client_request_id,
                process_upload,
                file,
                client_request_id,
                session,
            )
        except _KeyedLockCapacityExceeded as error:
            raise HTTPException(
                status_code=503, detail="Too many active client requests"
            ) from error
        if mutation is not None:
            await broadcast_mutation(mutation)
        return message

    @app.get("/download/{file_id}")
    async def download(file_id: str, _: SessionData = Depends(require_session)) -> FileResponse:
        if not FILE_ID_PATTERN.fullmatch(file_id):
            raise HTTPException(status_code=400, detail="Invalid file id")
        state = messages.file_download_state(file_id)
        if state is None:
            raise HTTPException(status_code=404, detail="File not found")
        if state["purged_at"] is not None or state["message_deleted_at"] is not None:
            raise HTTPException(status_code=410, detail="File has been deleted")
        try:
            path = storage.path_for(str(state["storage_name"]))
        except ValueError as error:
            raise HTTPException(status_code=404, detail="File not found") from error
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Source file is missing")
        return FileResponse(str(path), filename=str(state["original_name"]))

    @app.delete("/api/files/{file_id}")
    async def delete(
        file_id: str,
        request: Request,
        _: SessionData = Depends(require_session),
        __: None = Depends(enforce_rate_limit),
    ) -> dict[str, object]:
        if not FILE_ID_PATTERN.fullmatch(file_id):
            raise HTTPException(status_code=400, detail="Invalid file id")
        message = await asyncio.to_thread(messages.get_message_by_file_id, file_id)
        if message is None:
            raise HTTPException(status_code=404, detail="Message not found")
        mutation = await asyncio.to_thread(
            messages.soft_delete, str(message["id"]), request.app.state.clock()
        )
        if mutation is None:
            raise HTTPException(status_code=404, detail="Message not found")
        file_payload = message.get("file")
        if not isinstance(file_payload, dict):
            raise RuntimeError("File message returned an invalid file payload")
        try:
            record_audit(
                "delete",
                file_id,
                str(file_payload["original_name"]),
                int(file_payload["size_bytes"]),
            )
        except OSError:
            logger.exception("Failed to write audit log for deleted file %s", file_id)
        await broadcast_mutation(mutation)
        result = mutation["result"]
        if not isinstance(result, dict):
            raise RuntimeError("Delete mutation returned an invalid result")
        return result

    @app.websocket("/api/events")
    async def events_ws(websocket: WebSocket) -> None:
        settings = websocket.app.state.settings
        origin = websocket.headers.get("origin", "")
        host = websocket.headers.get("host", "")
        if origin:
            parsed_origin = urlparse(origin)
            origin_host = parsed_origin.netloc or parsed_origin.hostname or ""
            if origin_host and origin_host != host and "*" not in settings.allowed_origins:
                if origin_host not in settings.allowed_origins:
                    await websocket.accept()
                    await websocket.close(code=1008)
                    return

        cookie_value = websocket.cookies.get(SESSION_COOKIE)
        session = decode_session(cookie_value, settings.session_secret) if cookie_value else None
        if session is None:
            await websocket.accept()
            await websocket.close(code=4401)
            return

        raw_after = websocket.query_params.get("after", "0")
        try:
            after = int(raw_after)
            if after < 0 or str(after) != raw_after:
                raise ValueError
        except (TypeError, ValueError):
            await websocket.accept()
            await websocket.close(code=1008)
            return
        await websocket.accept()
        latest = messages.latest_sequence()
        if after > latest:
            after = 0
        connection = hub.connect(websocket, after)
        try:
            replay_target = latest
            replay_cursor = after
            while replay_cursor < replay_target:
                replay = [
                    event
                    for event in await asyncio.to_thread(messages.events_after, replay_cursor)
                    if int(event["sequence"]) <= replay_target
                ]
                if not replay:
                    break
                for event in replay:
                    await connection.send_replay(event)
                replay_cursor = int(replay[-1]["sequence"])
            await connection.finish_replay(replay_target)
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(websocket)

    @app.api_route(
        "/api/{resource_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def missing_api_resource(
        resource_path: str, _: SessionData = Depends(require_session)
    ) -> None:
        raise HTTPException(status_code=404, detail="Not Found")

    @app.get("/{asset_path:path}")
    async def static_asset(asset_path: str) -> FileResponse:
        asset = (web_dir / asset_path).resolve()
        if web_dir.resolve() not in asset.parents or not asset.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(str(asset))

    return app
