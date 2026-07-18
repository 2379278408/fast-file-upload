# Task 5 Async Lock and ZIP Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the ZIP response-start cleanup gap and replace executor-consuming keyed lock waits with a cancellation-safe async keyed lock.

**Architecture:** `_CleanupStreamingResponse` owns an async idempotent cleanup callback for its registered ZIP path and invokes it from `__call__` regardless of stream startup. `_KeyedLockPool` manages per-key `asyncio.Lock` entries on one event loop; synchronous protected operations enter the async lock before dispatching only the operation to `asyncio.to_thread`.

**Tech Stack:** Python 3.11, asyncio, FastAPI/Starlette, pytest.

## Global Constraints

- Follow strict RED then GREEN.
- Add no runtime dependency.
- Keep the working tree uncommitted and unpushed.
- Run focused tests, the four-file focused suite, and the full suite.

---

### Task 1: Response-Owned ZIP Cleanup

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_files.py`

**Interfaces:**
- `_CleanupStreamingResponse(..., cleanup: Callable[[], Awaitable[None]])`
- The batch-download route supplies a callback bound to `zip_temp_paths` and `zip_path`.

- [x] Add a deterministic ASGI test whose `send()` raises on `http.response.start`, then assert the path is absent and registry size is zero.
- [x] Run the single test and record the expected RED leak.
- [x] Make response `__call__` close the iterator and invoke its owned cleanup callback in nested `finally` blocks.
- [x] Keep registry cleanup idempotent and preserve build-cancellation cleanup ordering.
- [x] Run all batch-download tests and record GREEN.

### Task 2: Pure Async Keyed Lock

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_files.py`

**Interfaces:**
- `_KeyedLockPool.hold(key: str)` is an async context manager.
- `_KeyedLockPool.run(key, operation, *args)` acquires asynchronously, then dispatches the synchronous operation to a worker.
- The pool binds lazily to the first running event loop and raises a clear `RuntimeError` on cross-loop use.

- [x] Convert existing lock tests to async scenarios covering same-key serialization, different-key parallelism, exception release, cancellation release, high-cardinality reclamation, capacity rejection, and loop affinity.
- [x] Add a deterministic same-key saturation test with more waiters than the default executor capacity; while they wait, prove an unrelated `asyncio.to_thread` health/admin-style operation completes.
- [x] Run keyed-lock tests and record RED against the thread-backed implementation.
- [x] Replace threading lock entries with `asyncio.Lock` entries and cancellation-safe user accounting.
- [x] Move upload lock acquisition into the async route; dispatch only `process_upload` after the lock is held.
- [x] Run keyed-lock and cross-operation tests and record GREEN.

### Task 3: Report and Verification

**Files:**
- Modify: `.superpowers/sdd/hardening-task-5-report.md`

- [x] Update prior ZIP and keyed-lock descriptions so the report has one current implementation narrative.
- [x] Run `pytest -q tests/test_files.py -k 'batch_download or keyed_lock or concurrent_cross_operation'`.
- [x] Run `pytest -q tests/test_database.py tests/test_files.py tests/test_messages.py tests/test_app.py`.
- [x] Run `pytest -q`.
- [x] Run `git diff --check && python3 -m compileall -q app tests server.py`.
- [x] Record exact RED/GREEN and final verification results in the report.
