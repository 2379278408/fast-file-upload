# Task 6 Report: Ordered Upload Events And Resource Limits

## Status

Completed.

## TDD Evidence

- Added upload progress cadence and upload/message sequence tests first.
- Focused test collection failed because `UploadProgressPublisher` did not exist.
- Implemented the minimum event publisher and lifecycle integration, then reran focused tests to green.
- Added close-flush, expiry immutability, storage reserve, chunk capacity, and normalized rate-key regressions.

## Implementation

- Added `UploadProgressPublisher` with per-upload 250ms coalescing, forced boundary publication, bounded keyed state, and safe close flushing.
- Persisted upload progress directly to the shared `events` table without mutating upload session timestamps or expiry.
- Added ordered `upload.created`, `upload.progress`, `upload.state_changed`, `upload.completed`, `upload.cancelled`, and `upload.expired` events using the existing global event sequence.
- Preserved Task 4 writer lifecycle, lock reservations, bounded keyed locks, and assembly behavior.
- Preserved Task 5 exact completion and expiry mutation event lists.
- Enforced storage reserve before create and completion, active-session capacity, and one application-wide chunk-handler semaphore with immediate 503 rejection.
- Normalized rate-limit keys through FastAPI route templates and bounded stale/old rate buckets.

## Verification

- `python3 -m pytest -q tests/test_events.py tests/test_resumable_upload_api.py tests/test_upload_repository.py tests/test_files.py`
- Result: 170 passed, 1 third-party Starlette/httpx deprecation warning.
- `git diff --check`
- Result: clean.

## Considerations

- The remaining warning originates from `fastapi.testclient` compatibility code and is unrelated to Task 6.

## Important Follow-up

- Removed forced progress publication when a part completes; in-flight and confirmed progress now share the per-upload cadence.
- Added atomic `discard`, `mark_terminal`, and `reset` lifecycle APIs with generation invalidation and bounded terminal state.
- Added session-status validation before delayed persistence so stale and terminal progress is dropped.
- Serialized progress persistence and broadcast under one send lock; boundary discard waits the send barrier before state mutation proceeds.
- Tracked timer and flush tasks, contained broadcast failures, preserved direct cancellation propagation, and made close await all active work.
- Added real-timer cadence, rolling-window, discard race, terminal lateness, concurrent sequence, broadcast failure, cancellation, part cadence, and cancellation-boundary tests.
- Follow-up verification: `180 passed` across events, resumable API, upload repository, and files; `git diff --check` clean.
