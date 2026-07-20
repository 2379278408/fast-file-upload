# Branch Review Resource Report

Date: 2026-07-20
Baseline: `ab830c2` (`fix(resource): bound upload and event commitments`)

## Review Findings

### A. Server-Owned Chunk Boundaries

- RED: upload creation required clients to know the server setting in advance, and browser slicing remained coupled to its build-time 8 MiB default.
- GREEN: `POST /api/uploads` accepts an omitted `chunk_size_bytes`, resolves the configured server value, and still returns an actionable 400 for an explicit mismatch. The response value is authoritative for each browser task's slicing, missing-part math, confirmed-byte projection, and persisted resume state. Configuration caps chunks at 64 MiB and 10,000 parts while allowing a chunk to exceed a smaller maximum file size.
- Coverage: omitted and mismatched request values, 64 MiB and 10,000-part limits, chunk larger than maximum upload size, and real coordinator slicing with a non-8 MiB response boundary are covered.

### B. Atomic Storage Commitments

- RED: each upload admission checked only its own declared size against a pre-transaction disk snapshot. Concurrent sessions could individually pass while collectively committing more upload and assembly storage than available.
- GREEN: admission passes the current free-space budget minus the configured reserve into `UploadRepository.create_or_get()`. Its `BEGIN IMMEDIATE` transaction atomically sums active-session future commitments before inserting a new session. A new session commits `2 * size_bytes`; confirmed parts reduce future commitment byte-for-byte; durable assembly or publication states require no additional future allocation; terminal sessions release their commitment. A publication recovery failure that requires reassembly atomically resets publication state, digest, and final message fields, restoring `remaining + assembly` commitment. Retryable publication I/O failures preserve their durable continuation state.
- Coverage: cumulative and concurrent admission, idempotent replay, confirmed-byte reduction, terminal release, every reassembly publication state, exact 512 MiB upload plus assembly peak, and 507 mapping are covered.

### C. Bounded Event Replay And Authoritative Resync

- RED: the global `events` table grew without a bound, stale cursors could silently stall after retained events disappeared, and the browser advanced its cursor before asynchronous event application completed.
- GREEN: a shared injected event writer trims within every append transaction, making `EVENT_RETENTION_LIMIT` a write-time hard upper bound across message, file, upload, and progress events. Replay emits a common `resync_required` control shape with `target_sequence` and `reset_cursor`; database resets can move a successfully reconciled browser cursor downward. Browser event callbacks execute through one queue across WebSocket generations, revalidate ownership after each await, and persist a cursor only after successful application. Authoritative timeline loads use strict error propagation. Active-upload reconcile replaces stale observers and persistence records while a live revision barrier protects observer and source-device updates newer than the snapshot start.
- Coverage: all event append paths, concurrent writes, initial stale and ahead-of-database cursors, real commit-before-broadcast live gaps, cross-generation serialization, stale completion, downward reset, strict snapshot failure, active replace, observer/source snapshot races, and browser pagination after cursor reset are covered.

## Protocol Decisions

- Chunk size is optional in the creation request and authoritative in the response. Explicit mismatches retain the actionable 400 contract.
- `resync_required.target_sequence` is the authoritative reconciliation target. `reset_cursor` grants downward movement only for an ahead-of-database cursor.
- `ready` retains its existing meaning and is sent after replay or resync setup.
- Event retention applies transactionally to the whole event stream so every event type shares one sequence floor and one hard-bounded global window.
- Storage commitment counts only future writes because confirmed chunks and durable assembly copies are already reflected in the free-space snapshot.

## Verification

- Focused backend/frontend contract suite: `292 passed, 1 warning in 18.88s`.
- Browser E2E suite: `21 passed, 1 warning in 172.33s`.
- Default full suite: `547 passed, 1 deselected, 1 warning in 203.07s`.
- `python3 -m compileall -q app server.py tests`: passed.
- `git diff --check`: passed.
- Local preview: root returned HTTP 200 through `https://8086-57e9f8b4df557af1.monkeycode-ai.online` (background terminal `term_1784550079788_23`).

## Residual Considerations

- Disk capacity can still change because of unrelated processes after admission. Chunk writes retain their filesystem error handling, and completion rechecks actual free space before creating the assembly copy.
- A pre-existing real-clock progress cadence test triggered its 5 ms scheduling tolerance once under combined-suite load. The affected production code was unchanged; ten isolated repetitions and the final focused and full suites passed.
- Retained event history is intentionally insufficient for stale clients. Correct recovery depends on the authoritative snapshot endpoints remaining available; strict failures preserve the old cursor and retry on a new WebSocket generation.
