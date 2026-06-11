# File Upload Panel (Pure Version)

Minimalist file upload server built with Python FastAPI and vanilla Frontend.

**Key Features:**
- **Streaming Upload**: Low memory usage, supports large files.
- **No Dependencies**: Only FastAPI and Uvicorn required (plus Python 3.8+).
- **Drag & Drop**: Modern UI with progress bar.
- **Management**: List, download (with original filename), delete.
- **Configurable**: Environment variables and CLI arguments.

## Structure

```text
upload-panel-clean/
├── server.py         # Backend API (Python/FastAPI)
├── web/
│   └── index.html    # Frontend UI
└── uploads/          # Upload directory (created automatically)
```

## Usage

### 1. Install

```bash
pip install fastapi uvicorn
```

### 2. Run

```bash
python server.py
# Or custom port/dir:
python server.py --port 3000 --dir /data/my-files
```

### 3. Access

Open browser: `http://localhost:8083`

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `UPLOAD_DIR` | Directory to store files | `./uploads` |
| `PORT` | Server port | `8083` |

## API Documentation

Once running, visit `/docs` for Swagger UI.

- `POST /api/upload`: Upload file
- `GET /api/files`: List files
- `GET /download/{id}`: Download file
- `DELETE /api/files/{id}`: Delete file

## Security Notes

- This is a lightweight server for internal/trusted use.
- No authentication built-in. If exposing to public, place behind Nginx with Basic Auth or IP whitelist.
- Filenames are sanitized to prevent path traversal.
- Stored as `{UUID}_{OriginalName}` to avoid collisions.
