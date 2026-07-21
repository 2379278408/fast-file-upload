# Frontend Upload Control Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make upload controls server-authoritative, migrate persistence keys atomically, normalize timeline timestamps, and carry production file handles through picker and drag/drop flows.

**Architecture:** Keep the coordinator as the upload state owner and add one asynchronous control path that marks transient pending actions, awaits the server, and recovers authoritative state on failure. Keep IndexedDB key changes inside one readwrite transaction, and implement File System Access as progressive enhancement at the composer boundary.

**Tech Stack:** Browser ES modules, IndexedDB, File System Access API, QuickJS contract tests, Playwright Chromium, pytest.

## Global Constraints

- Strict red-green-refactor TDD for every behavior group.
- Preserve the existing per-task reconcile revision guard introduced by commit `61ec8db`.
- Terminal statuses are `complete`, `completed`, `cancelled`, and `expired`; retry accepts only `failed`.
- Server responses and GET recovery are authoritative for status and confirmed parts.
- Keep source `File`, `FileSystemFileHandle`, identity, and source-device ownership on control failures.
- Create independent commits and never amend existing commits.

---

### Task 1: Server-authoritative controls

**Files:**
- Modify: `tests/test_frontend_contract.py`
- Modify: `web/js/upload-coordinator.js`
- Modify: `web/js/timeline.js`
- Modify: `web/js/app.js`

**Interfaces:**
- Produces: `pause(uploadId): Promise`, `resume(uploadId): Promise`, `cancel(uploadId): Promise`
- Produces: `pauseAll/resumeAll/cancelAll(): Promise<ControlSummary>` using `Promise.allSettled`
- Produces: public task field `pendingAction: string | null`

- [ ] Write contract tests proving pending actions are visible, success commits status, failure GET restores authoritative status/parts and rejects, app handlers await/catch, terminal guards converge, and retry accepts only failed tasks.
- [ ] Run focused tests and confirm failures against fire-and-forget controls.
- [ ] Add a shared asynchronous control operation with pre-await and post-await `touchTask`, authoritative recovery, persistence handling, and terminal validation.
- [ ] Make reselect/retry await resume before queueing and preserve authoritative state on failure.
- [ ] Run focused tests and confirm pass.

### Task 2: Timestamp normalization

**Files:**
- Modify: `tests/test_frontend_contract.py`
- Modify: `web/js/upload-coordinator.js`
- Modify: `web/js/timeline.js`

**Interfaces:**
- Produces: coordinator timestamps as valid ISO strings for new, restored, and remote upload tasks.
- Produces: timeline timestamp parsing with finite fallback and stable upload ordering.

- [ ] Write failing tests for numeric, numeric-string, ISO, and malformed legacy timestamps.
- [ ] Run focused tests and confirm mixed timestamps fail.
- [ ] Normalize at coordinator construction/restoration boundaries and use safe timeline epoch/date helpers.
- [ ] Run focused tests and confirm pass.

### Task 3: Atomic persistence ID migration

**Files:**
- Modify: `tests/test_frontend_contract.py`
- Modify: `web/js/upload-persistence.js`
- Modify: `web/js/upload-coordinator.js`

**Interfaces:**
- Produces: `persistence.migrate(previousUploadId, task): Promise`

- [ ] Write failing tests proving one readwrite transaction performs put-new/delete-old and rejects either request/transaction failure.
- [ ] Write a coordinator reload test proving prepare-time client records cannot become ghosts after a server ID differs.
- [ ] Run focused tests and confirm failures.
- [ ] Implement clone-safe record construction and atomic migration; await and surface migration failure from prepare.
- [ ] Run focused tests and confirm pass.

### Task 4: Production file handle acquisition

**Files:**
- Modify: `tests/test_frontend_contract.py`
- Modify: `tests/test_browser_e2e.py`
- Modify: `web/js/composer.js`
- Modify: `web/js/app.js`

**Interfaces:**
- Produces: `composer.pickFiles(): Promise<boolean>`
- Consumes: `showOpenFilePicker({ multiple: true })`
- Consumes: `DataTransferItem.getAsFileSystemHandle()` with per-item file fallback.

- [ ] Write contract tests for picker success, cancellation, unavailable/error fallback, drag handle success, drag fallback, and paste File fallback.
- [ ] Add Chromium coverage for handle persistence and granted-permission reload continuation where browser capabilities permit.
- [ ] Run focused tests and confirm failures.
- [ ] Implement progressive enhancement while preserving existing input, drag/drop, paste, CSP, and accessible button paths.
- [ ] Run focused contract and browser tests and confirm pass.

### Task 5: Verification and branch report

**Files:**
- Create: `.superpowers/sdd/branch-review-frontend-report.md`

- [ ] Run `pytest -q tests/test_frontend_contract.py`.
- [ ] Run `pytest -q tests/test_browser_e2e.py`.
- [ ] Run `pytest -q`.
- [ ] Run `python -m compileall -q app tests`.
- [ ] Run whitespace and diff checks with `git diff --check` and inspect the complete diff.
- [ ] Record findings 3/4/8/11/Minor, tests, UX decisions, and residual concerns in the branch report.
- [ ] Review status/diff/log, stage only intended files, and create a new commit without amend.

### Task 6: Authoritative retry state matrix

**Files:**
- Modify: `tests/test_frontend_contract.py`
- Modify: `web/js/upload-coordinator.js`

**Interfaces:**
- Refines: `retry(uploadId): Promise<PublicUploadTask>`
- Consumes: authoritative `getUploadSession(uploadId)` status and confirmed-part snapshot.

- [x] Write failing contract coverage for a locally failed upload whose server status remains `uploading`, proving retry adopts missing parts, runs one worker, uploads only the missing part, and completes.
- [x] Add matrix assertions for direct pump from `queued`/`uploading`, awaited resume from `paused`/`failed`, read-only rejection for `verifying`, and convergence plus rejection for terminal states.
- [x] Run the focused tests and confirm the current failed-only server gate fails.
- [x] Implement authoritative retry dispatch without creating a second worker.
- [x] Run focused retry tests and confirm pass.

### Task 7: Cancellation across local transition phases

**Files:**
- Modify: `tests/test_frontend_contract.py`
- Modify: `web/js/upload-coordinator.js`

**Interfaces:**
- Refines: `cancel(uploadId): Promise<PublicUploadTask>`
- Refines: `cancelAll(): Promise<ControlSummary>`
- Adds internal cancellation intent that survives deferred session creation and deferred completion.

- [x] Write a failing deferred-create test proving cancel settles only after a successful create response is immediately deleted and persistence is removed, with no upload worker.
- [x] Write a failing deferred-complete test proving cancel preempts local `completing`, deletes the server `verifying` session, suppresses completion publication, and clears pending state.
- [x] Assert `cancelAll` includes `preparing` and `completing`, reports exact totals/results, and cannot remain pending.
- [x] Run focused cancellation tests and confirm failure against the current transition set and completion race.
- [x] Implement the minimal cancellation-intent coordination and extend the batch cancel set.
- [x] Run focused cancellation tests and confirm pass.

### Task 8: Review verification and independent commit

**Files:**
- Modify: `.superpowers/sdd/branch-review-frontend-report.md`

- [x] Run frontend contract, browser E2E, default full suite, compileall, and `git diff --check`.
- [x] Update the review report with retry matrix, preparing/completing cancellation semantics, exact test results, and residual concerns.
- [x] Inspect status, diff, and log; stage only intended files and create one new commit without amend.

### Task 9: Lost create response cancellation recovery

**Files:**
- Modify: `tests/test_frontend_contract.py`
- Modify: `web/js/upload-coordinator.js`

**Interfaces:**
- Refines: `cancel(uploadId): Promise<PublicUploadTask>`
- Reuses: `api.createUploadSession(metadata)` with the original `clientRequestId` and identical file metadata.
- Reuses: `persistence.migrate(previousUploadId, task)` for atomic client-ID to server-ID adoption.

- [x] Add a contract test whose first create call registers a server session and rejects its response, then cancel retries the same metadata, atomically migrates to the returned server ID, DELETEs the session, removes persistence only after confirmation, and resolves `cancelled` without pumping.
- [x] Run the focused test and confirm the current implementation rejects without issuing the idempotent create lookup.
- [x] Keep stable create metadata on the task and add one authoritative session-resolution path shared by prepare and cancellation.
- [x] Make preparing cancellation retry create after an ambiguous create failure, migrate persistence before DELETE, and commit terminal state only after DELETE succeeds.
- [x] Run the focused test and confirm pass.

### Task 10: Persistent cancellation intent across recovery

**Files:**
- Modify: `tests/test_frontend_contract.py`
- Modify: `web/js/upload-coordinator.js`

**Interfaces:**
- Refines: `applyRemoteEvent(event): boolean`
- Refines: `reconcile(): Promise<PublicUploadTask[]>`
- Preserves: source `file`, `fileHandle`, client-key persistence, and `cancelRequested` while server identity remains unresolved.

- [x] Add a contract test where the initial create and cancellation lookup both fail after server registration; assert cancel rejects, the task remains actionable and non-terminal, persistence and source payload remain, and cancellation intent survives.
- [x] Extend the test with a matching `upload.created` event and active reconcile response; assert server ID adoption, atomic migration, authoritative DELETE, no upload pump, and final persistence removal.
- [x] Add a second retry-cancel assertion for recovery without an event, proving the next cancel reuses the same metadata and completes DELETE.
- [x] Run the focused tests and confirm the current terminal/status guards or rejected prepare promise prevent recovery.
- [x] Implement a retryable cancellation state and one adoption-and-cancel scheduler used by event, reconcile, and repeated cancel.
- [x] Run focused recovery tests and confirm pass.

### Task 11: Batch settlement and final verification

**Files:**
- Modify: `tests/test_frontend_contract.py`
- Modify: `.superpowers/sdd/branch-review-frontend-report.md`

- [x] Add batch coverage mixing ambiguous preparing cancellation recovery with a normal cancellable task; assert exact `total`, `succeeded`, `failed`, per-task results, and cleared pending state.
- [x] Run focused RED then GREEN tests and record exact evidence.
- [ ] Run `pytest -q tests/test_frontend_contract.py`, `pytest -q tests/test_browser_e2e.py`, `pytest -q`, `python3 -m compileall -q app server.py tests`, and `git diff --check`.
- [ ] Update the frontend review report with root cause, authoritative create recovery semantics, exact test results, and residual concerns.
- [ ] Inspect status, complete diff, and recent log; stage only intended files and create one independent commit without amend.
