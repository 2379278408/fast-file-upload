#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os

import uvicorn

from app import create_app
from app.config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fast file upload server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8083")), help="Server port")
    parser.add_argument(
        "--dir",
        dest="upload_dir",
        type=str,
        default=os.environ.get("UPLOAD_DIR", "./uploads"),
        help="Upload directory path",
    )
    parser.add_argument(
        "--max-upload-size-mb",
        type=int,
        default=None,
        help="Optional per-file size limit in MB. Falls back to MAX_UPLOAD_SIZE_MB or 512.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    max_size = None if args.max_upload_size_mb is None else args.max_upload_size_mb * 1024 * 1024
    settings = Settings.from_env(args.upload_dir, max_size)
    app = create_app(settings)
    print(f"Starting server on port {args.port}")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
