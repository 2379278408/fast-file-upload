#!/usr/bin/env python3
"""
Pure FastAPI File Upload Server
------------------------------
Features:
- Streaming upload (Low memory usage for large files)
- Directory listing & Download
- Sanitized filenames
- CORS support
- No database, purely file-system based

Usage:
  python server.py [--port PORT] [--dir DIR]
"""

import os
import uuid
import re
import shutil
import argparse
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

# --- Configuration ---
# Defaults can be overridden by environment variables
DEFAULT_DIR = os.environ.get("UPLOAD_DIR", "./uploads")
DEFAULT_PORT = int(os.environ.get("PORT", "8083"))

# Parse CLI arguments if provided
parser = argparse.ArgumentParser(description="Simple File Upload Server")
parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Server port")
parser.add_argument("--dir", dest="upload_dir", type=str, default=DEFAULT_DIR, help="Upload directory path")
args = parser.parse_args()

UPLOAD_DIR = Path(args.upload_dir)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# --- App Setup ---
app = FastAPI(title="File Upload Server", description="A minimal file upload server using FastAPI")

# CORS Middleware (allow all origins by default for simplicity, restrict in prod)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Helpers ---

def fmt_size(n: int) -> str:
    """Format bytes into human-readable string."""
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024.0
    return f"{n:.1f} PB"

def sanitize(name: str) -> str:
    """Remove potentially malicious characters from filename."""
    # Allow alphanumeric, whitespace, dot, hyphen, underscore, Chinese chars
    name = re.sub(r'[^\w\s.\-()_\u4e00-\u9fff]', '', name.strip())
    return name or "unnamed_file"

# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the frontend interface."""
    html_path = Path(__file__).parent / "web" / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Error: index.html not found</h1>")
    return html_path.read_text(encoding="utf-8")

@app.post("/api/upload")
async def upload(file: UploadFile):
    """
    Upload a file.
    Returns JSON with file info and download link.
    """
    fid = uuid.uuid4().hex[:8]
    safe_name = sanitize(file.filename or "unnamed")
    
    # Format: {UUID}_{OriginalName} to prevent collision and ensure uniqueness
    dest = UPLOAD_DIR / f"{fid}_{safe_name}"

    # Stream file content to disk (avoids loading whole file into memory)
    # FastAPI uploads to SpooledTemporaryFile first, then we move it.
    file.file.seek(0)
    with open(dest, "wb") as f_out:
        shutil.copyfileobj(file.file, f_out, length=1024 * 1024 * 16) # 16MB buffer

    stat = dest.stat()
    return {
        "ok": True,
        "id": fid,
        "name": safe_name,
        "size": fmt_size(stat.st_size),
        "download_url": f"/download/{fid}"
    }

@app.get("/download/{fid}")
async def download(fid: str):
    """Download a file by its ID."""
    for f in UPLOAD_DIR.iterdir():
        if f.name.startswith(fid + "_"):
            original_name = f.name.split("_", 1)[1]
            return FileResponse(str(f), filename=original_name)
    raise HTTPException(404, "File not found")

@app.get("/api/files")
async def list_files():
    """List all uploaded files."""
    files = []
    for f in UPLOAD_DIR.iterdir():
        if f.is_file():
            s = f.stat()
            parts = f.name.split("_", 1)
            files.append({
                "id": parts[0],
                "name": parts[1] if len(parts) > 1 else f.name,
                "size": fmt_size(s.st_size),
                "date": datetime.fromtimestamp(s.st_mtime).strftime("%Y-%m-%d %H:%M")
            })
    # Sort newest first
    return sorted(files, key=lambda x: x["date"], reverse=True)

@app.delete("/api/files/{fid}")
async def delete_file(fid: str):
    """Delete a file by its ID."""
    for f in UPLOAD_DIR.iterdir():
        if f.name.startswith(fid + "_"):
            f.unlink()
            return {"ok": True}
    raise HTTPException(404, "File not found")

# --- Main Entry ---

if __name__ == "__main__":
    import uvicorn
    print(f"🚀 Starting server on port {args.port}")
    print(f"📂 Upload dir: {UPLOAD_DIR.absolute()}")
    uvicorn.run("server:app", host="0.0.0.0", port=args.port, log_level="info")
