# Resumable Unified Transfer Design

**Date:** 2026-07-19

## Goal

Replace the separated upload queue and timeline panels with one file-transfer-assistant timeline. Support reliable files up to 512MB, at most 9 concurrent file uploads, queued overflow, cross-refresh recovery, and real-time state synchronization across connected devices.

## Confirmed Product Direction

- Each selected, dropped, or pasted file becomes a timeline card immediately.
- The card updates in place through queued, uploading, paused, verifying, complete, failed, and cancelled states.
- The completed card becomes the permanent file message without duplication.
- The entire timeline accepts drag-and-drop.
- Individual tasks and the active-task summary expose pause, resume, and cancel controls.
- The source device controls data transmission; all devices can observe progress and cancel a shared task.
- File System Access handles provide automatic refresh recovery where available; reselecting the original file is the fallback.

## Confirmed Technical Direction

- Implement a native FastAPI and SQLite resumable upload protocol.
- Use 8MB chunks and one in-flight chunk per file.
- Allow up to 9 files to upload concurrently.
- Persist upload sessions and confirmed part metadata in SQLite.
- Stream chunks and final assembly with bounded memory.
- Verify each chunk and the complete file with SHA-256.
- Publish the final file and permanent message atomically from the user's perspective.
- Broadcast upload lifecycle and throttled progress through the existing ordered WebSocket event stream.
- Expire idle temporary upload sessions after 24 hours.
- Retain the documented whole-file `POST /api/upload` route as a legacy compatibility path while the new interface uses resumable APIs.

## Canonical Specification

The EARS requirements and full technical design are maintained in:

- `.monkeycode/specs/2026-07-19-resumable-unified-transfer/requirements.md`
- `.monkeycode/specs/2026-07-19-resumable-unified-transfer/design.md`

Those documents define API contracts, state transitions, data models, correctness properties, recovery, resource protection, error handling, accessibility, and test strategy.

## Acceptance Baseline

- A 40MB file completes under constrained individual-request size and simulated slow network.
- A generated sparse 512MB file completes with matching SHA-256.
- Nine files upload concurrently and overflow files remain queued.
- Pause, resume, cancellation, network interruption, page refresh, re-authentication, and service restart preserve confirmed chunks.
- Source and observing devices show consistent progress and final state.
- The Transfer route displays one unified timeline and no separate upload queue panel.
- Existing authentication, WebSocket replay, file library, downloads, soft deletion, restoration, purge, and CSP behavior remain covered by regression tests.
