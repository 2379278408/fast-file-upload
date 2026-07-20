# Branch Review Resource Report

Date: 2026-07-20
Baseline: `6e0d875` (`fix(upload): anchor cleanup against filesystem races`)

## Review Findings

### A. Server-Owned Chunk Boundaries

- RED: resumable upload creation accepted a client-declared chunk size that could differ from the server setting, and configuration allowed impractically small parts or unboundedly large chunks.
- GREEN: `POST /api/uploads` requires `chunk_size_bytes` to equal `UPLOAD_CHUNK_SIZE_BYTES` and returns a clear 400 response containing the expected value. Configuration caps chunks at 64 MiB and rejects settings that would require more than 10,000 parts. The browser production contract remains 8 MiB, while test settings explicitly select smaller valid boundaries.
- Coverage: request mismatch, 64 MiB maximum, 10,000-part maximum, valid 8 MiB production configuration, and a 512 MiB upload declaration are covered.

### B. Atomic Storage Commitments

- RED: each upload admission checked only its own declared size against a pre-transaction disk snapshot. Concurrent sessions could individually pass while collectively committing more upload and assembly storage than available.
- GREEN: admission passes the current free-space budget minus the configured reserve into `UploadRepository.create_or_get()`. Its existing `BEGIN IMMEDIATE` transaction atomically sums active-session future commitments before inserting a new session. A new session commits `2 * size_bytes`; confirmed parts reduce future commitment byte-for-byte; durable assembly or publication states require no additional future allocation; terminal sessions release their commitment. Completion retains its actual free-space check immediately before assembly.
- Coverage: cumulative sessions, concurrent admission, confirmed-byte reduction, terminal release, publication-state release, exact 512 MiB upload plus assembly peak, and 507 mapping are covered.

### C. Bounded Event Replay And Authoritative Resync

- RED: the global `events` table grew without a bound, stale cursors could silently stall after retained events disappeared, and the browser advanced its cursor before asynchronous event application completed.
- GREEN: startup and periodic maintenance retain the latest `EVENT_RETENTION_LIMIT` events globally. Replay reads atomic floor/latest windows and emits `resync_required` for initial stale cursors and live retention gaps while preserving the subsequent `ready` control event. Browser event callbacks execute serially; a cursor is persisted only after successful asynchronous application. `resync_required` waits for authoritative active-upload, timeline, and file-library reconciliation. Reconciliation failure closes that connection generation and reconnects from the prior cursor.
- Coverage: transactional global trimming, initial stale cursor, live gap resync, ordered control events, asynchronous serialization, success-only cursor advancement, and failed-resync replay from the old cursor are covered.

## Protocol Decisions

- Chunk size remains explicit in the creation request so mismatched or outdated clients receive an actionable 400 response instead of silently uploading against another boundary.
- `resync_required.sequence` is the server replay target. The client commits it after all authoritative snapshots resolve, allowing live events queued above that target to continue in order.
- `ready` retains its existing meaning and is sent after replay or resync setup.
- Event retention applies to the whole event stream so every event type shares one sequence floor and one bounded global window.
- Storage commitment counts only future writes because confirmed chunks and durable assembly copies are already reflected in the free-space snapshot.

## Verification

- Focused resource and frontend contract suite:
  - `pytest -q tests/test_upload_repository.py tests/test_events.py tests/test_resumable_upload_api.py tests/test_frontend_contract.py`
  - Result: `275 passed, 1 warning in 17.00s`.
- Environment and part-boundary regression:
  - `pytest -q tests/test_session.py::test_resumable_upload_environment_overrides tests/test_resumable_upload_api.py::test_chunk_configuration_bounds_part_count_and_request_size`
  - Result: `2 passed, 1 warning in 0.07s`.
- Chromium browser E2E:
  - `pytest -q tests/test_browser_e2e.py`
  - Result: `21 passed, 1 warning in 171.15s`.
- Full default project suite:
  - `pytest -q`
  - Result: `531 passed, 1 deselected, 1 warning in 225.50s`.
- Python compilation and patch validation:
  - `python3 -m compileall -q app server.py tests`
  - `git diff --check`
  - Result: exit code 0 with no output.
- The warning is the existing `StarletteDeprecationWarning` from FastAPI TestClient's httpx integration.

## Residual Considerations

- Disk capacity can still change because of unrelated processes after admission. Chunk writes retain their filesystem error handling, and completion rechecks actual free space before creating the assembly copy.
- A pre-existing real-clock progress cadence test triggered its 5 ms scheduling tolerance once under combined-suite load. The affected production code was unchanged; ten isolated repetitions and the final focused and full suites passed.
- Retained event history is intentionally insufficient for stale clients. Correct recovery depends on the authoritative snapshot endpoints remaining available; failures preserve the old cursor and retry on a new WebSocket generation.
