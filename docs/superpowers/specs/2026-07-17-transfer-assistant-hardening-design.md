# Transfer Assistant Hardening Design

## Goal

Bring the current personal transfer assistant to a releasable state by repairing frontend runtime failures, enforcing one API data contract, making event delivery and deletion state transitions correct, and closing the identified security and resource-management gaps.

## Constraints

- Keep FastAPI, standard-library `sqlite3`, vanilla JavaScript ES modules, and the existing filesystem storage model.
- Do not add runtime dependencies.
- Require `UPLOAD_TOKEN` for every server start and require a signed session for all data APIs, downloads, and WebSocket connections.
- Preserve 30-day sessions, 30-second undo, 50-item page limits, and permanent retention until user deletion.
- Keep the current uncommitted worktree intact and do not commit or push.
- Every repair starts with a failing regression test and ends with focused tests, the full suite, and an independent review.

## Architecture

### Frontend Contract Boundary

`api.js` owns HTTP and WebSocket transport. API responses retain the backend message DTO: `id`, `kind`, message metadata, and optional nested `file`. Timeline, composer, and library consume that DTO directly. Client-created optimistic updates merge by message ID and never invent event sequence numbers.

The legacy upload, preview, and file-list code in `app.js` is removed. The page uses one composer upload queue and one library controller. DOM actions use module-scoped listeners or event delegation so the CSP can disallow inline scripts.

### Event Delivery

Repository mutations return the exact event they committed. Routes broadcast only that event and skip broadcasts for idempotent no-op requests. Event payloads are structured JSON objects containing the resulting message state. WebSocket replay loops in sequence order until it reaches a stable snapshot, so a gap larger than 500 events is delivered completely.

### Authentication

Startup validates that `UPLOAD_TOKEN` is present. `require_session` always validates the signed cookie. Login receives a dedicated bounded rate limit. Input models constrain device names, request IDs, message IDs, filters, and WebSocket cursors. Logout closes the browser connection immediately; server-side session revocation is kept out of scope because it requires a new persistent session model beyond the accepted signed-cookie design.

### Deletion State Machine

Messages move through active, soft-deleted, restore-eligible, purge-claimed, and purged states. Restore and purge use conditional database updates inside transactions. Purge first atomically claims eligible rows, then deletes files, then finalizes state; a failed file deletion releases or records the claim. The old file deletion route resolves the owning message and invokes soft deletion.

A lifespan worker runs startup recovery and periodic purge. Reservation recovery reconciles staged, published, and indexed upload states independently from a client retry.

### Storage and Resource Limits

Repository code receives the configured storage directory instead of deriving it from the database path. Batch ZIP generation enforces a total-byte limit, builds outside the event loop, and streams from a temporary archive. SQLite enables WAL, sets `busy_timeout`, and uses bounded transaction retries where contention is expected. Per-request upload locks are released after use.

### Verification

Backend tests cover auth startup, DTOs, replay beyond 500 events, idempotent broadcasts, concurrent restore/purge, custom upload directories, ZIP limits, reservation recovery, and lock cleanup. Frontend tests execute ES modules through an available JavaScript runtime or a small browser-compatible harness and verify startup, real DTO rendering, pagination, batch payloads, upload abort, reconnect, and session expiry. Static contract tests remain supplemental.

## Accepted Decisions

- Missing `UPLOAD_TOKEN` is a startup error in every environment.
- Existing public HTML and static assets remain accessible so the unlock screen can load.
- Runtime dependencies remain unchanged.
- The repair proceeds in ordered gates; each gate must pass review before the next begins.
