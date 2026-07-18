# Transfer Assistant Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair all confirmed release-blocking defects and close the associated correctness, security, performance, accessibility, and test gaps.

**Architecture:** Keep the existing FastAPI, SQLite, filesystem, and ES module architecture. Centralize frontend/backend contracts, make repository mutations return committed events, model deletion as transactional state transitions, and add bounded background recovery and resource controls.

**Tech Stack:** Python 3.11, FastAPI 0.139, standard-library sqlite3, vanilla JavaScript ES modules, pytest.

## Global Constraints

- No new runtime dependencies.
- `UPLOAD_TOKEN` is mandatory at startup.
- Session Cookie remains HttpOnly, SameSite=Strict, Secure on HTTPS, with 30-day expiry.
- Data API, download, and WebSocket access require a valid session.
- Text length is 1-10000 characters; device name length is 1-40 characters; page limit is at most 50.
- Soft deletion has one exact 30-second restore window.
- Do not commit or push.
- Each task uses failing tests first, focused verification, full regression, and independent review.

---

### Task 1: Frontend Boot and DTO Contract

**Files:**
- Modify: `web/js/app.js`
- Modify: `web/js/timeline.js`
- Modify: `web/js/library.js`
- Modify: `web/index.html`
- Test: `tests/test_frontend_contract.py`

**Interfaces:**
- Message DTO uses `id`, `kind`, `body`, timestamps, and optional nested `file`.
- Timeline `upsert(message)` merges by `message.id` without changing event sequence.

- [ ] Add a failing executable module test proving `app.js` loads without unresolved identifiers.
- [ ] Add failing tests that feed real `/api/messages` and `/api/files` DTO fixtures into timeline and library rendering.
- [ ] Remove the legacy upload/preview/file-list implementation from `app.js` and duplicate markup from `index.html`.
- [ ] Update timeline and library to consume `id` and nested `file` consistently.
- [ ] Bind preview, copy, delete, and locate actions through module listeners.
- [ ] Run `python3 -m pytest -q tests/test_frontend_contract.py tests/test_app.py` and then `python3 -m pytest -q`.
- [ ] Request independent task review and resolve every Critical or Important finding.

### Task 2: Events, Paging, Batch Operations, and Upload Cancellation

**Files:**
- Modify: `app/repository.py`
- Modify: `app/main.py`
- Modify: `web/js/api.js`
- Modify: `web/js/timeline.js`
- Modify: `web/js/composer.js`
- Modify: `web/js/library.js`
- Test: `tests/test_events.py`
- Test: `tests/test_files.py`
- Test: `tests/test_frontend_contract.py`

**Interfaces:**
- Repository mutations return `{result, event}` where `event` is absent for idempotent no-ops.
- Batch request body uses `message_ids`.
- Timeline paging sends `before=<cursor>` and observes a top sentinel.

- [ ] Add failing tests for structured event payloads, idempotent no-broadcast behavior, and replay of more than 500 events.
- [ ] Add failing frontend tests for `before`, top paging, `message_ids`, delete response handling, and XHR abort rejection.
- [ ] Return committed events from repository mutations and broadcast only those events.
- [ ] Loop WebSocket replay to a stable sequence snapshot.
- [ ] Correct paging and batch request/response contracts.
- [ ] Implement `xhr.onabort` with `AbortError` and prove the next queued upload starts.
- [ ] Remove synthetic sequence increments from local upserts.
- [ ] Run focused event/file/frontend tests and the full suite.
- [ ] Request independent task review and resolve every Critical or Important finding.

### Task 3: Authentication, Validation, CSP, and Session Lifecycle

**Files:**
- Modify: `app/config.py`
- Modify: `app/auth.py`
- Modify: `app/main.py`
- Modify: `server.py`
- Modify: `web/js/app.js`
- Modify: `web/js/library.js`
- Test: `tests/test_session.py`
- Test: `tests/test_app.py`
- Test: `tests/test_frontend_contract.py`

**Interfaces:**
- Settings creation raises a clear configuration error when `UPLOAD_TOKEN` is empty.
- All protected requests require `transfer_session`.
- WebSocket close code for invalid session remains 4401.

- [ ] Add failing tests for missing-token startup, protected local requests, bounded identifiers, filter dates, batch IDs, and invalid WebSocket cursors.
- [ ] Require `UPLOAD_TOKEN` during settings construction and remove implicit local sessions.
- [ ] Add a dedicated default-on login rate limiter with bounded memory.
- [ ] Apply Pydantic constraints to every public input model and explicit WebSocket cursor validation.
- [ ] Close the frontend event connection on 401, logout, and lock transitions.
- [ ] Replace inline handlers with event delegation and remove `unsafe-inline` from `script-src`.
- [ ] Remove the blocked external font import or self-host an existing local font.
- [ ] Run session/app/frontend tests and the full suite.
- [ ] Request security review and independent task review; resolve every Critical or Important finding.

### Task 4: Transactional Delete, Restore, Purge, and Recovery

**Files:**
- Modify: `app/database.py`
- Modify: `app/repository.py`
- Modify: `app/main.py`
- Modify: `app/storage.py`
- Modify: `web/js/timeline.js`
- Test: `tests/test_messages.py`
- Test: `tests/test_files.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Restore succeeds only while `now < deleted_at + undo_seconds`.
- Purge atomically claims only rows where the restore deadline is reached.
- `DELETE /api/files/{file_id}` resolves to message soft deletion.

- [ ] Add deterministic boundary tests for just before, exactly at, and after 30 seconds.
- [ ] Add concurrent restore/purge tests proving one final state and one event.
- [ ] Add tests proving legacy file delete uses soft deletion and remains restorable.
- [ ] Add startup tests for reservation recovery without client retry and periodic expired-file purge.
- [ ] Introduce purge claim state and conditional transactional transitions.
- [ ] Reconcile reservations and orphan temporary/published files during lifespan startup.
- [ ] Run the maintenance worker with bounded interval and clean shutdown.
- [ ] Add timeline delete confirmation and 30-second restore action.
- [ ] Run message/file/event tests and the full suite.
- [ ] Request independent task review and resolve every Critical or Important finding.

### Task 5: Storage, ZIP, SQLite Contention, and Long-Running State

**Files:**
- Modify: `app/database.py`
- Modify: `app/repository.py`
- Modify: `app/main.py`
- Modify: `app/storage.py`
- Test: `tests/test_database.py`
- Test: `tests/test_files.py`

**Interfaces:**
- Batch ZIP receives the configured `FileStorage` and enforces a configured total-byte ceiling.
- SQLite connections use WAL and a busy timeout.

- [ ] Add failing tests for custom upload directories, missing source files, ZIP total-size rejection, and bounded memory behavior.
- [ ] Build ZIP files in a worker thread using a temporary file and stream the response.
- [ ] Enable WAL, set `busy_timeout`, and add bounded retries for lock contention.
- [ ] Release per-request upload locks after completion.
- [ ] Locate downloads by persisted `storage_name` instead of directory scans.
- [ ] Read audit log tails incrementally and move blocking filesystem work off the event loop.
- [ ] Classify supported image extensions as `kind="image"`.
- [ ] Reject cross-operation idempotency key collisions with 409.
- [ ] Run database/file tests and the full suite.
- [ ] Request independent task review and resolve every Critical or Important finding.

### Task 6: Accessibility, Documentation, Runtime Artifacts, and Final Verification

**Files:**
- Modify: `web/index.html`
- Modify: `web/styles.css`
- Modify: `web/js/app.js`
- Modify: `web/js/library.js`
- Modify: `README.md`
- Modify: `.gitignore`
- Test: `tests/test_frontend_contract.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Unlock overlay is an accessible modal dialog with focus containment.
- View controls expose correct tab state.
- Timeline scroll position uses its own container and message anchor.

- [ ] Add failing tests for dialog semantics, focus containment, visible skip link, tab state, keyboard help text, and timeline scroll restoration.
- [ ] Implement modal focus management and background inert state.
- [ ] Complete view/tab semantics and visible focused skip link.
- [ ] Preserve timeline container position across session unlock.
- [ ] Update README with configuration, startup, endpoints, recovery, and testing instructions.
- [ ] Ignore SQLite, upload, cache, and temporary ZIP runtime artifacts.
- [ ] Run the full test suite and Python compilation.
- [ ] Perform browser smoke verification against the local preview.
- [ ] Request a whole-branch code review and security review.
- [ ] Apply one consolidated fix wave for all remaining Critical and Important findings, rerun verification, and repeat review until approved.
