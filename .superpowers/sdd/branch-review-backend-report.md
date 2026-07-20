# Backend Branch Review Report

Date: 2026-07-20
Baseline: `58c9b5b` (`docs(test): use binary unit for large upload marker`)

## Review Findings

### A. Cancellable Completion

- RED: completion held the per-upload lock during whole-file assembly, so cancellation waited for hashing and could lose the race to publication.
- GREEN: completion now persists `verifying/assembling` under the lock, broadcasts that mutation, assembles outside the lock, and reacquires the lock before publication. It rereads durable state and discards the pending final when cancellation or expiry has won.
- Coverage: `test_cancel_preempts_assembly_and_prevents_publication` verifies prompt cancellation, timely `verifying` delivery, no message, and no published or pending file.

### B. Durable Same-Process Retry

- RED: retrying complete against `assembling`, `assembled`, `file_published`, or `published` returned a conflict and required a process restart.
- GREEN: complete and startup recovery share one durable publication continuation path. Interrupted assembly resets and restarts once; later publication states resume without reassembly and converge on one file and one message.
- Coverage: `test_complete_retry_resets_assembling_and_restarts_once`, `test_complete_retry_continues_durable_publication_state`, and `test_complete_retry_finishes_published_state_idempotently` cover all four states.

### C. Confirmed-Part Repair

- RED: a missing or damaged confirmed part left its database row intact, causing every completion retry to fail against the same part.
- GREEN: `PartIntegrityError` identifies `part_index` and `reason`. Completion discards the identified part under the upload lock, removes its row and recomputes `confirmed_bytes` atomically, records a failed-state event, and allows that part to be resumed and retransmitted.
- Coverage: chunk-storage tests assert structured integrity details; `test_complete_invalidates_damaged_confirmed_part_for_reupload` verifies corrupt and missing part repair through successful completion.

### D. Cancellation Cleanup

- RED: cleanup failure changed an already durable cancellation into HTTP 507, leaving clients uncertain about the terminal state.
- GREEN: cancellation remains successful after durable mutation, cleanup failures produce warning logs, and periodic maintenance retries residual cleanup for cancelled and expired sessions.
- Coverage: `test_cancel_cleanup_failure_returns_cancelled_and_maintenance_retries` verifies one cancellation event, warning context, successful response, and later cleanup.

### E. Recovery Events

- RED: startup reconciliation and publication repair could change durable upload state without creating corresponding state events.
- GREEN: confirmed-part reconciliation, assembly reset, publication failure, and published-state repair create events in the same database transaction as their state changes. Lifespan receives the resulting mutations in event-sequence order.
- Coverage: `test_startup_recovery_broadcasts_state_mutations_in_sequence` verifies ordered broadcasts and exact recovered payloads.

## Design Decisions

- A process-local, thread-safe completing set prevents duplicate complete operations while allowing cancellation to use the per-upload lock during assembly.
- Publication continuation lives in `_continue_publication_locked()` and is shared by request retry and startup recovery.
- Durable cancellation is the authoritative result; filesystem cleanup is an eventually consistent side effect.
- Recovery events are committed with their state transitions so observers can reconstruct the same durable timeline.
- The lifecycle design now explicitly permits `verifying -> cancelled`.

## Verification

- Focused backend regression suite:
  - `python3 -m pytest -q tests/test_chunk_storage.py tests/test_upload_repository.py tests/test_resumable_upload_api.py`
  - Result: `119 passed, 1 warning in 11.23s`.
- Full default project suite:
  - `python3 -m pytest -q`
  - Result: `504 passed, 1 deselected, 1 warning in 186.28s`.
- The warning is the existing `StarletteDeprecationWarning` from FastAPI TestClient's httpx integration.

## Residual Considerations

- The process-local completing guard coordinates threads in one application process. Durable publication state remains the cross-process recovery mechanism.
- Cancellation and integrity repair intentionally prioritize durable state; repeated maintenance handles filesystem cleanup failures.

## Follow-up Review Remediation

Baseline: `e227557` (`fix(upload): harden completion recovery`)

### Rename And State-Write Boundary

- RED: fault injection after successful rename left durable publication state at `assembled`; DELETE broadcast cancellation and removed chunks while leaving the final file behind.
- GREEN: terminal cleanup now accepts `assembled` and `file_published`, reconstructs the upload-scoped final name, validates upload ID and path constraints, and verifies durable size and SHA-256 before deleting the exact final name. A same-path replacement with a different digest is preserved.
- Recovery: cancellation remains successful when the first cleanup attempt fails; periodic maintenance and startup recovery retry both durable publication states until verified final and chunk residue converge.

### Complete Residual Cleanup

- RED: a transient `cleanup_session()` failure after successful completion left the resumable directory permanently because maintenance excluded complete sessions.
- GREEN: startup recovery cleans complete-session residue. Periodic maintenance scans only valid non-symlink resumable directories and selects matching complete sessions, preserving the permanent message and final file.

### Follow-up Tests

- `test_cancel_removes_verified_final_after_publish_state_write_failure`
- `test_cancel_preserves_unverified_final_after_publish_state_write_failure`
- `test_restart_finishes_cancelled_publication_cleanup`
- `test_maintenance_retries_complete_session_chunk_cleanup`
- `test_startup_retries_complete_session_chunk_cleanup`

### Follow-up Verification

- Initial fault-injection RED:
  - `python3 -m pytest -q tests/test_resumable_upload_api.py::test_cancel_removes_verified_final_after_publish_state_write_failure tests/test_resumable_upload_api.py::test_cancel_preserves_unverified_final_after_publish_state_write_failure tests/test_resumable_upload_api.py::test_maintenance_retries_complete_session_chunk_cleanup`
  - Result: `2 failed, 1 passed, 1 warning in 0.72s`; verified final cleanup and complete residual cleanup failed, while digest-mismatched replacement preservation already held.
- Expanded recovery GREEN:
  - Targeted startup, maintenance, cancellation, and integrity cases.
  - Result: `6 passed, 1 warning in 1.06s`.
- Focused backend regression suite:
  - `python3 -m pytest -q tests/test_chunk_storage.py tests/test_upload_repository.py tests/test_resumable_upload_api.py`
  - Result: `124 passed, 1 warning in 11.41s`.
- Full default project suite:
  - `python3 -m pytest -q`
  - Result: `509 passed, 1 deselected, 1 warning in 210.26s`.
- Python compilation and patch validation:
  - `python3 -m compileall -q app server.py tests`
  - `git diff --check`
  - Result: exit code 0 with no output.
