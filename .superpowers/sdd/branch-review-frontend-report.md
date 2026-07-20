# Branch Review Frontend Report

Date: 2026-07-20
Baseline: `61ec8db` (`fix(upload): preserve tasks across reconcile migration`)

## Review Findings

### Finding 3: Server-Authoritative Upload Controls

- RED: `pause`, `resume`, and `cancel` returned immediately, committed optimistic local states, swallowed API failures, and removed persistence before cancellation was confirmed.
- GREEN: all three controls return promises and expose a transient `pendingAction`. Pause and cancel abort the active XHR immediately while retaining the last confirmed status. Only a successful control response commits paused, queued, or cancelled state; only confirmed terminal state removes persistence. A failed control fetches the session, replaces status and confirmed parts with the authoritative snapshot, retains the source file, file handle, identity, and source ownership, and rejects to the app toast boundary.
- Batch controls use `Promise.allSettled` and return `{ action, total, succeeded, failed, results }`. Timeline and batch app handlers await completion and surface failures.

### Finding 4: Resume Gates For Reselect And Retry

- RED: reselect and retry queued and pumped locally before a remote paused session acknowledged resume; resume errors were swallowed. Retry could also be called outside failed state.
- GREEN: reselect and retry first apply a GET session snapshot. Remote paused or failed sessions must acknowledge resume before becoming queued or entering the pump. Remote complete, cancelled, or expired sessions converge locally and reject the operation. Resume failure restores the latest authoritative state and missing parts, keeps the source payload controls, and propagates the error. Retry accepts only a local failed task.

### Finding 8: Canonical Upload Timestamps

- RED: new tasks used numeric epoch values while remote tasks and completed messages used strings, allowing invalid dates and unstable mixed-type ordering.
- GREEN: every upload task source now emits ISO 8601 `createdAt`. Restore normalizes legacy numeric, numeric-string, and ISO records. Timeline parsing always produces a finite epoch fallback and retains deterministic timestamp-plus-stable-ID ordering. Completed message DTOs already carry ISO timestamps, so no backend change was required.

### Finding 11: Atomic Persistence ID Migration

- RED: a prepare-time control could persist the client request ID, then session creation could put a different server upload ID without deleting the old key, leaving a reload ghost.
- GREEN: persistence exposes `migrate(previousUploadId, task)`. It issues put-new and delete-old inside one IndexedDB readwrite transaction and resolves only after transaction commit. Request, abort, clone fallback, and transaction errors reject; failed migration is visible as a task error. Coordinator tests span pending prepare, client-key persistence, server-ID migration, and reload reconciliation with exactly one task.

### Minor: Production FileSystemHandle Acquisition

- RED: production attachment, drag, and paste paths delegated only plain `File` objects, so the existing handle persistence and granted-permission restore logic had no production source.
- GREEN: the attachment button progressively calls `showOpenFilePicker({ multiple: true })`, pairs every handle with `getFile()`, and sends both to the coordinator. Picker cancellation is quiet; unavailable or rejected capability falls back to the existing file input. Drag items use `getAsFileSystemHandle()` per item and fall back independently to `getAsFile()`; paste retains plain File behavior.
- Chromium coverage creates a real OPFS `FileSystemFileHandle`, traverses the production button and coordinator path, verifies the handle in IndexedDB, reloads, and completes automatically through granted permission.

### Reconcile Revision Safety

- Pending control transitions and every authoritative control result advance the existing per-task reconcile revision from `61ec8db`. A deferred reconcile test proves an older uploading snapshot cannot overwrite a pending pause or its confirmed paused result.

### Follow-up Review: Verifying And Batch Control Safety

- `verifying` is now preserved as a server-owned processing state during session preparation and source reconciliation. Source coordination skips handle permission checks, reselect prompts, resume PATCH requests, and upload pumping while the server completes verification.
- A refreshed source task without its file first pauses a remote `queued` or `uploading` session through the server transition, then requests reselection. A successful resume response with server status `uploading` becomes local scheduler status `queued`, allowing the upload worker to continue with missing parts.
- Reselect accepts local and remote `paused` or `failed` states. Retry accepts local and remote `failed` states. Both operations converge to the latest GET status and confirmed parts before rejecting an invalid transition.
- Batch controls use explicit transition sets matching the server contract: pause selects source `queued` and `uploading`; resume selects source `paused` and `failed`; cancel selects source and observer `queued`, `uploading`, `paused`, `verifying`, and `failed`. Skipped states stay outside the settled result and failure count.
- Coordinator subscriptions now include `{ hasPendingControl }`. A pending individual or batch control suppresses overlapping batch execution until every selected request settles.

### Follow-up Review 2: Retry Authority And Cancellation Races

- Retry now dispatches from a fresh server snapshot while retaining the local `failed` entry condition. Authoritative `queued` or `uploading` sessions adopt confirmed parts and enter the existing pump; `paused` or `failed` sessions await server resume; `verifying` remains read-only; terminal sessions converge locally and reject retry.
- A locally failed task whose server remains `uploading` resumes with one worker, skips the already confirmed part, uploads only the missing part, and publishes completion once.
- Cancellation now covers every locally actionable non-terminal phase: `preparing`, `queued`, `uploading`, `paused`, `verifying`, `completing`, and `failed`. Batch totals and settled results include source and observer tasks in those phases.
- Cancelling during deferred preparation records cancellation intent immediately. When session creation resolves, the coordinator deletes the new server session, removes both client-request and server-ID persistence, and never starts the upload pump.
- Cancelling during deferred completion deletes the server `verifying` session. A late complete response cannot publish a completed message or replace the confirmed cancelled state, and pending batch state clears after settlement.
- TDD RED coverage produced `8 failed, 2 passed`; the focused GREEN run produced `10 passed, 165 deselected, 1 warning in 0.35s`.

## UX Decisions

- Pending controls keep the last server-confirmed status and replace the card status text with `正在暂停`, `正在继续`, or `正在取消`.
- Card actions are hidden while one control is pending, preventing conflicting operations and duplicate requests.
- All three batch buttons use native `disabled` plus `aria-disabled` while any control is pending, then restore together after settlement.
- Individual failures use the existing toast boundary and retain an inline task error. Batch controls show the number of failed tasks while preserving per-task results for callers.
- Picker cancellation preserves the current composer without an error toast. Capability and permission failures return to the established accessible file input.

## Verification

- Frontend contract suite: `175 passed, 1 warning in 4.56s`.
- Browser E2E suite: `22 passed, 1 warning in 164.39s`.
- Focused production picker reload regression: `1 passed, 1 warning in 15.46s`.
- Default full suite: `569 passed, 1 deselected, 1 warning in 216.08s`.
- `python3 -m compileall -q app server.py tests`: passed. The environment has no `python` executable alias.
- `git diff --check`: passed.
- Local preview: root returned HTTP 200 through `https://8086-57e9f8b4df557af1.monkeycode-ai.online` using existing background terminal `term_1784550079788_23`.

## Residual Considerations

- File System Access remains a Chromium-oriented progressive enhancement. Other browsers continue through the file input and can resume after a manual reselect.
- IndexedDB implementations that cannot structured-clone a handle retain the existing metadata-only fallback. Real Chromium handle cloning and reload continuation are covered.
- When both a control request and its recovery GET fail, the card retains the last confirmed local snapshot and displays the control error; the rejected promise drives the toast and a later reconcile can recover.
- The first full browser run hit a Chromium OPFS fixture `FileSystemWritableFileStream` data-pipe `AbortError` before coordinator code executed. The focused OPFS regression and a complete browser rerun passed without product changes or relaxed assertions.
