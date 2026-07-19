# 可恢复统一文件传输需求

## Introduction

本功能将传输页面改造为类似微信文件传输助手的统一时间线。文本消息、待传文件、上传进度、失败恢复和已完成文件共享同一内容流。系统通过可恢复分片上传支持单文件 512MB、最多 9 个文件并行、跨刷新恢复和跨设备实时状态同步。

## Glossary

- **传输时间线**：按创建时间展示文本消息、活动上传和已完成文件消息的统一界面。
- **上传任务**：一个本地文件从加入队列到完成、取消或过期的生命周期实体。
- **上传会话**：服务端持久化的可恢复上传记录。
- **源设备**：持有原始本地文件并负责发送分片的设备。
- **观察设备**：同一用户会话下查看上传状态的其他已连接设备。
- **分片**：上传任务拆分出的固定大小数据块，默认大小为 8MB。
- **已确认分片**：服务端完成大小与 SHA-256 校验并持久化元数据的分片。
- **永久文件消息**：服务端完成整体校验和原子发布后的文件时间线记录。
- **活动任务**：状态为 queued、uploading、paused、verifying 或 failed 的上传任务。

## Requirements

### Requirement 1: Unified Transfer Timeline

**User Story:** AS a user, I want text and file transfers in one timeline, so that I can understand and control transfers from one workspace.

#### Acceptance Criteria

1. WHEN the user adds a file, the transfer workspace SHALL create a file card in the transfer timeline before the first file chunk is sent.
2. WHILE an upload task is active, the transfer timeline SHALL update the existing file card in place.
3. WHEN an upload completes, the transfer timeline SHALL replace the active upload projection with the corresponding permanent file message without creating a duplicate card.
4. WHEN the user drags files over the transfer workspace, the transfer workspace SHALL display one drop target covering the timeline surface.
5. WHEN the user selects files, drops files, or pastes file content, the transfer workspace SHALL add the resulting files to the same upload scheduler.

### Requirement 2: Multi-File Scheduling

**User Story:** AS a user, I want to transfer many files together, so that I can send a complete batch without repeated selection.

#### Acceptance Criteria

1. WHEN the user adds more than 9 files, the upload scheduler SHALL upload at most 9 files concurrently and keep the remaining files queued.
2. WHILE a file is uploading, the upload scheduler SHALL send at most one chunk for that file at a time.
3. WHEN an active upload reaches a terminal or paused state, the upload scheduler SHALL start the next eligible queued file.
4. WHEN the user prioritizes a queued file, the upload scheduler SHALL move the selected file ahead of other queued files while preserving active uploads.
5. WHILE active tasks exist, the transfer workspace SHALL display aggregate task counts and controls for pause all, resume all, and cancel all.

### Requirement 3: Large And Resumable Uploads

**User Story:** AS a user, I want reliable large-file uploads, so that a 40MB or larger transfer survives network and request interruptions.

#### Acceptance Criteria

1. WHEN a file size is between 1 byte and 512MB inclusive, the upload system SHALL accept the file when the configured extension policy and storage capacity allow the upload.
2. WHEN a file size exceeds 512MB, the upload system SHALL reject the task before sending file content and display the configured size limit.
3. WHEN an upload starts, the upload coordinator SHALL divide the file into 8MB chunks, with the final chunk containing the remaining bytes.
4. WHEN the server confirms a chunk, the upload session SHALL persist the chunk index, byte count, and SHA-256 digest.
5. IF a network request fails, the upload coordinator SHALL retry only unconfirmed chunks using bounded exponential backoff.
6. WHEN the upload coordinator resumes a task, the upload coordinator SHALL query the server and send only missing chunks.

### Requirement 4: Task Controls And State

**User Story:** AS a user, I want task-level and batch controls, so that I can manage bandwidth and recover failures.

#### Acceptance Criteria

1. WHEN the source device pauses an upload task, the upload coordinator SHALL stop scheduling new chunks after the current request is aborted or completed.
2. WHEN the source device resumes a paused or failed task, the upload coordinator SHALL continue from server-confirmed chunks.
3. WHEN any connected device cancels an upload task, the upload system SHALL stop accepting new chunks and schedule temporary data cleanup.
4. WHILE a task is queued, uploading, paused, verifying, failed, complete, or cancelled, the file card SHALL display the corresponding textual state.
5. WHILE a task is uploading, the source device SHALL display percentage, confirmed bytes, total bytes, current speed, and estimated remaining time.
6. WHILE progress samples cover less than 2 seconds, the file card SHALL display the estimated remaining time as calculating.

### Requirement 5: Cross-Refresh Recovery

**User Story:** AS a user, I want uploads to survive a page refresh, so that completed chunks are preserved.

#### Acceptance Criteria

1. WHEN the transfer page loads, the client SHALL retrieve active upload sessions before reconciling local upload tasks.
2. WHERE a persisted file handle remains authorized, WHEN the page restores an upload task, the client SHALL continue missing chunks automatically.
3. WHERE a persisted file handle is unavailable, WHEN the page restores an upload task, the client SHALL request the original file from the user.
4. WHEN the user reselects an original file, the client SHALL verify file name, size, last-modified time, and sampled content digest before resuming.
5. IF the reselected file identity differs from the upload session identity, the client SHALL keep the task paused and display a file mismatch error.

### Requirement 6: Cross-Device Synchronization

**User Story:** AS a user, I want every connected device to see current transfer status, so that the transfer timeline remains consistent.

#### Acceptance Criteria

1. WHEN an upload session is created, the event service SHALL broadcast an upload-created event to connected devices.
2. WHILE bytes arrive, the event service SHALL broadcast confirmed or in-flight aggregate progress at most four times per second per upload session.
3. WHEN an upload state changes, the event service SHALL broadcast the resulting state and confirmed byte count.
4. WHILE an observing device displays an upload task, the observing device SHALL present progress and state as read-only transfer information.
5. WHEN an observing device cancels an upload task, the upload service SHALL apply the cancellation to the shared upload session.
6. WHEN an observing device requests pause or resume, the interface SHALL explain that the source device controls file transmission.

### Requirement 7: Integrity And Atomic Publication

**User Story:** AS a user, I want completed transfers to be verified, so that downloaded files match the selected local files.

#### Acceptance Criteria

1. WHEN the server receives a chunk, the upload service SHALL stream the request into isolated temporary storage without buffering the complete file in memory.
2. WHEN a chunk request ends, the upload service SHALL compare the received byte range, byte count, and SHA-256 digest with the request metadata.
3. IF chunk validation fails, the upload service SHALL discard the incomplete chunk and preserve previously confirmed chunks.
4. WHEN the client requests completion, the upload service SHALL verify chunk coverage, total bytes, and the complete-file SHA-256 digest.
5. WHEN complete-file verification succeeds, the upload service SHALL publish the final file and permanent message atomically from the user's perspective.
6. IF publication or database persistence fails, the upload service SHALL keep the final file unavailable and preserve a recoverable upload session.

### Requirement 8: Idempotency And Recovery

**User Story:** AS a user, I want retries to be safe, so that interrupted requests do not duplicate files or timeline entries.

#### Acceptance Criteria

1. WHEN the client repeats upload-session creation with the same client request identifier and metadata, the upload service SHALL return the existing upload session.
2. WHEN the client repeats an identical confirmed chunk, the upload service SHALL return the existing chunk result.
3. IF a repeated chunk index contains different bytes or digest metadata, the upload service SHALL reject the request with status 409.
4. WHEN the client repeats a completion request, the upload service SHALL return the same permanent file message.
5. WHEN the service starts, the recovery process SHALL reconcile upload-session records, confirmed chunk records, temporary files, and published files.
6. WHEN an inactive upload session reaches 24 hours, the maintenance process SHALL expire the session and clean its temporary data.

### Requirement 9: Security And Resource Protection

**User Story:** AS an operator, I want bounded upload resources, so that concurrent transfers do not exhaust the service.

#### Acceptance Criteria

1. WHILE an upload API is accessed, the upload service SHALL require the existing signed session authentication.
2. WHEN a client references an upload session, the upload service SHALL authorize the request against the shared personal workspace and record the source device.
3. WHEN the service receives a chunk index or byte range, the upload service SHALL validate the values against declared file size and chunk size.
4. WHEN available storage cannot accommodate the declared file plus configured safety reserve, the upload service SHALL reject session creation with a storage-capacity error.
5. WHILE upload requests execute, the service SHALL enforce configured active-session, chunk-request, and rate limits.
6. WHEN temporary file paths are constructed, the upload service SHALL derive paths from server-generated upload identifiers and validated chunk indexes.

### Requirement 10: Error Feedback And Accessibility

**User Story:** AS a user, I want clear and accessible transfer feedback, so that I can recover from errors on desktop and mobile.

#### Acceptance Criteria

1. IF a file is empty, oversized, disallowed, mismatched, storage-blocked, network-failed, or integrity-failed, the file card SHALL display a distinct actionable error message.
2. WHEN a recoverable error occurs, the file card SHALL expose the applicable retry, resume, reselect, or cancel action.
3. WHILE drag-and-drop is available, the transfer workspace SHALL provide an equivalent keyboard-operable file selection action.
4. WHILE task controls are visible, each interactive target SHALL measure at least 44 by 44 CSS pixels.
5. WHEN upload status changes are announced, the interface SHALL use a throttled live-region summary instead of announcing every progress sample.
6. WHILE reduced-motion preference is active, the interface SHALL update progress and state without nonessential movement animation.

### Requirement 11: Compatibility And Verification

**User Story:** AS a maintainer, I want regression coverage and a controlled migration, so that existing transfer behavior remains reliable.

#### Acceptance Criteria

1. WHILE existing API consumers migrate, the service SHALL retain the documented `POST /api/upload` endpoint as a legacy whole-file upload path.
2. WHEN the new transfer interface uploads files, the interface SHALL use only the resumable upload APIs.
3. WHEN a 40MB file is uploaded through a test environment with constrained individual request size, the resumable upload path SHALL complete successfully.
4. WHEN a sparse 512MB test file is uploaded, the upload path SHALL complete and produce a matching SHA-256 digest without storing a repository fixture.
5. WHEN more than 9 files are added, automated browser tests SHALL verify 9 active uploads and queued overflow tasks.
6. WHEN network interruption, page refresh, session re-authentication, or service restart occurs, automated tests SHALL verify that confirmed chunks are not retransmitted.
7. WHEN the full regression suite runs, existing session, WebSocket, timeline, file library, download, soft-delete, restore, and purge behavior SHALL remain verified.
