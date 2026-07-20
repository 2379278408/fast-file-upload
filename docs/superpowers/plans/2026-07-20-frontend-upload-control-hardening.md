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
