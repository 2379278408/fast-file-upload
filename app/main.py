from __future__ import annotations

import json
from secrets import compare_digest
from pathlib import Path
from time import monotonic
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from .config import Settings
from .storage import FileStorage


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title=settings.app_title, description="A polished file upload service with optional governance controls")
    rate_buckets: dict[tuple[str, str], list[float]] = {}

    @app.middleware("http")
    async def add_security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'",
        )
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

    storage = FileStorage(settings.upload_dir, settings.max_upload_size, settings.allowed_extensions)
    audit_path = settings.upload_dir / ".audit.jsonl"
    web_dir = Path(__file__).resolve().parent.parent / "web"
    web_root = web_dir / "index.html"

    def record_audit(action: str, file_id: str, name: str, size_bytes: int) -> None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "file_id": file_id,
            "name": name,
            "size_bytes": size_bytes,
        }
        with audit_path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_audit_events(limit: int = 200) -> list[dict[str, object]]:
        if not audit_path.exists():
            return []
        lines = audit_path.read_text(encoding="utf-8").splitlines()[-limit:]
        events: list[dict[str, object]] = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def require_token(
        authorization: str | None = Header(default=None),
        x_upload_token: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ) -> None:
        if not settings.auth_token:
            return
        candidate = x_upload_token or token
        if authorization and authorization.lower().startswith("bearer "):
            candidate = authorization[7:].strip()
        if candidate and compare_digest(candidate, settings.auth_token):
            return
        raise HTTPException(status_code=401, detail="Missing or invalid upload token")

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

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        if not web_root.exists():
            return HTMLResponse("<h1>Missing web/index.html</h1>", status_code=500)
        return HTMLResponse(web_root.read_text(encoding="utf-8"))

    @app.get("/api/health")
    async def health() -> dict[str, str | int | bool]:
        storage.prune_expired(settings.retention_days)
        stats = storage.stats()
        return {
            "ok": True,
            "protected": bool(settings.auth_token),
            **stats,
        }

    @app.get("/api/files")
    async def list_files(_: None = Depends(require_token)) -> dict[str, object]:
        storage.prune_expired(settings.retention_days)
        files = [item.to_api() for item in storage.list_files()]
        return {"items": files, "stats": storage.stats()}

    @app.get("/api/audit")
    async def audit(_: None = Depends(require_token)) -> dict[str, object]:
        return {"events": read_audit_events()}

    @app.get("/api/admin/summary")
    async def admin_summary(_: None = Depends(require_token)) -> dict[str, object]:
        storage.prune_expired(settings.retention_days)
        return storage.admin_summary()

    @app.post("/api/upload")
    async def upload(
        file: UploadFile,
        _: None = Depends(require_token),
        __: None = Depends(enforce_rate_limit),
    ) -> dict[str, object]:
        storage.prune_expired(settings.retention_days)
        stored = storage.save_upload(file)
        record_audit("upload", stored.file_id, stored.display_name, stored.size_bytes)
        return {"ok": True, **stored.to_api()}

    @app.get("/download/{file_id}")
    async def download(file_id: str, _: None = Depends(require_token)) -> FileResponse:
        stored = storage.get_file(file_id)
        return FileResponse(str(stored.path), filename=stored.display_name)

    @app.delete("/api/files/{file_id}")
    async def delete(
        file_id: str,
        _: None = Depends(require_token),
        __: None = Depends(enforce_rate_limit),
    ) -> dict[str, bool]:
        stored = storage.get_file(file_id)
        storage.delete_file(file_id)
        record_audit("delete", stored.file_id, stored.display_name, stored.size_bytes)
        return {"ok": True}

    @app.get("/{asset_path:path}")
    async def static_asset(asset_path: str) -> FileResponse:
        asset = (web_dir / asset_path).resolve()
        if web_dir.resolve() not in asset.parents or not asset.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(str(asset))

    return app
