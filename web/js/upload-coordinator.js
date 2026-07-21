import { MAX_ACTIVE_UPLOADS, MAX_UPLOAD_SIZE_BYTES, UPLOAD_ETA_MIN_SAMPLE_MS, UPLOAD_RETRY_DELAYS, UPLOAD_SPEED_WINDOW_MS } from './config.js';

const IDENTITY_SAMPLE_BYTES = 64 * 1024;
const IDENTITY_VERSION = 'sample-identity-v1';
const LIVE_MILESTONES = [25, 50, 75, 100];
const UPLOAD_ANNOUNCEMENT_INTERVAL_MS = 1000;
const TERMINAL_UPLOAD_STATUSES = new Set(['complete', 'completed', 'cancelled', 'expired']);
const SERVER_OWNED_UPLOAD_STATUSES = new Set(['verifying']);
const BATCH_CONTROL_STATUSES = {
  pause: new Set(['queued', 'uploading']),
  resume: new Set(['paused', 'failed']),
  cancel: new Set(['preparing', 'queued', 'uploading', 'paused', 'verifying', 'completing', 'failed']),
};
const UPLOAD_STATUS_LABELS = {
  preparing: '正在准备', queued: '等待上传', uploading: '上传中', paused: '已暂停',
  verifying: '正在校验', completing: '正在校验', failed: '上传失败',
  complete: '已完成', completed: '已完成', cancelled: '已取消', expired: '已过期',
};

function utf8Bytes(value) {
  const encoded = unescape(encodeURIComponent(String(value)));
  const bytes = new Uint8Array(encoded.length);
  for (let index = 0; index < encoded.length; index += 1) bytes[index] = encoded.charCodeAt(index);
  return bytes;
}

function concatenate(parts) {
  const length = parts.reduce((total, part) => total + part.byteLength, 0);
  const result = new Uint8Array(length);
  let offset = 0;
  parts.forEach(part => {
    result.set(part, offset);
    offset += part.byteLength;
  });
  return result;
}

function toHex(buffer) {
  return Array.from(new Uint8Array(buffer), byte => byte.toString(16).padStart(2, '0')).join('');
}

function identityOffsets(size) {
  const sampleSize = Math.min(size, IDENTITY_SAMPLE_BYTES);
  const offsets = [0, Math.max(0, Math.floor((size - sampleSize) / 2)), Math.max(0, size - sampleSize)];
  return Array.from(new Set(offsets));
}

export async function sampleFileIdentity(file, cryptoObject = crypto) {
  const sampleSize = Math.min(file.size, IDENTITY_SAMPLE_BYTES);
  const parts = [utf8Bytes(`${IDENTITY_VERSION}\n${file.name}\n${file.size}\n${file.lastModified}\n`)];
  for (const offset of identityOffsets(file.size)) {
    parts.push(utf8Bytes(`${offset}\n`));
    parts.push(new Uint8Array(await file.slice(offset, offset + sampleSize).arrayBuffer()));
  }
  const digest = await cryptoObject.subtle.digest('SHA-256', concatenate(parts));
  return {
    name: file.name,
    size: file.size,
    lastModified: file.lastModified,
    sampleSha256: toHex(digest),
  };
}

export async function matchesFileIdentity(file, identity, cryptoObject = crypto) {
  if (!identity || file.name !== identity.name || file.size !== identity.size
      || file.lastModified !== identity.lastModified) return false;
  const sampled = await sampleFileIdentity(file, cryptoObject);
  return sampled.sampleSha256 === identity.sampleSha256;
}

function serverParts(response, fallback) {
  const value = response && (response.confirmed_parts || response.confirmedParts);
  return Array.isArray(value) ? Array.from(new Set(value)).sort((left, right) => left - right) : fallback;
}

function unionParts(current, incoming) {
  return Array.from(new Set([...current, ...incoming])).sort((left, right) => left - right);
}

function eventData(event) {
  let payload = event && event.payload;
  if (typeof payload === 'string') {
    try { payload = JSON.parse(payload); } catch { payload = {}; }
  }
  const data = event && (event.upload || event.data) || payload || event || {};
  return {
    ...data,
    sequence: data.sequence ?? event?.sequence,
    updated_at: data.updated_at ?? event?.updated_at ?? event?.created_at,
  };
}

function normalizedStatus(status) {
  return status === 'complete' ? 'completed' : status;
}

function normalizeCreatedAt(value, fallback) {
  const candidate = value ?? fallback;
  const numeric = typeof candidate === 'string' && /^\d+$/.test(candidate)
    ? Number(candidate)
    : candidate;
  const timestamp = new Date(numeric).getTime();
  const fallbackTimestamp = new Date(fallback).getTime();
  const resolvedTimestamp = Number.isFinite(timestamp)
    ? timestamp
    : (Number.isFinite(fallbackTimestamp) ? fallbackTimestamp : Date.now());
  return new Date(resolvedTimestamp).toISOString();
}

function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.keys(value).forEach(key => deepFreeze(value[key]));
  return Object.freeze(value);
}

function coordinatorDestroyedError() {
  const error = new Error('Upload coordinator is destroyed');
  error.name = 'CoordinatorDestroyedError';
  return error;
}

function publicTask(task) {
  return deepFreeze({
    uploadId: task.uploadId,
    clientRequestId: task.clientRequestId,
    file: null,
    fileHandle: null,
    identity: task.identity ? { ...task.identity } : null,
    name: task.name,
    sizeBytes: task.sizeBytes,
    mimeType: task.mimeType,
    status: task.status,
    confirmedParts: [...task.confirmedParts],
    confirmedBytes: task.confirmedBytes,
    inFlightBytes: task.inFlightBytes,
    progressPercent: task.progressPercent,
    speedBytesPerSecond: task.speedBytesPerSecond,
    etaSeconds: task.etaSeconds,
    sourceDeviceId: task.sourceDeviceId,
    isSourceDevice: task.isSourceDevice,
    errorCode: task.errorCode,
    errorMessage: task.errorMessage,
    createdAt: task.createdAt,
    pendingAction: task.pendingAction,
  });
}

function persistedTask(task) {
  return {
    uploadId: task.uploadId,
    clientRequestId: task.clientRequestId,
    fileHandle: task.fileHandle,
    identity: task.identity ? { ...task.identity } : null,
    name: task.name,
    sizeBytes: task.sizeBytes,
    mimeType: task.mimeType,
    status: task.status,
    confirmedParts: [...task.confirmedParts],
    confirmedBytes: task.confirmedBytes,
    sourceDeviceId: task.sourceDeviceId,
    isSourceDevice: task.isSourceDevice,
    errorCode: task.errorCode,
    errorMessage: task.errorMessage,
    createdAt: task.createdAt,
    serverSequence: task.serverSequence,
    serverUpdatedAt: task.serverUpdatedAt,
    serverVersion: task.serverVersion,
    chunkSize: task.chunkSize,
    sessionReady: task.sessionReady,
    cancelRequested: task.cancelRequested,
  };
}

export function createUploadCoordinator({
  api,
  persistence,
  cryptoObject = crypto,
  now = () => Date.now(),
  delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)),
  abortControllerFactory = () => new AbortController(),
  maxActive = MAX_ACTIVE_UPLOADS,
  chunkSize = null,
  onAnnounce = null,
  onCompleted = null,
}) {
  const tasks = [];
  const listeners = new Set();
  let destroyed = false;
  let pumping = false;
  let generation = 0;
  let persistenceClosed = false;
  let liveRevision = 0;
  let taskRevision = 0;
  let resolveDestroy;
  const destroyPromise = new Promise(resolve => { resolveDestroy = resolve; });
  const pendingWorkers = new Set();
  const controlSummary = () => Object.freeze({
    hasPendingControl: tasks.some(task => Boolean(task.pendingAction)),
  });

  const announceTask = task => {
    let stateAnnouncement = null;
    if (task.announcedStatus === undefined) {
      task.announcedStatus = task.status;
    } else if (task.announcedStatus !== task.status) {
      task.announcedStatus = task.status;
      stateAnnouncement = `${task.name} ${UPLOAD_STATUS_LABELS[task.status] || task.status}`;
    }
    const milestone = LIVE_MILESTONES.filter(value => task.progressPercent >= value).pop() || 0;
    const lastMilestone = task.announcedMilestone || 0;
    const lastAt = task.progressAnnouncementAt ?? Number.NEGATIVE_INFINITY;
    const currentTime = now();
    let progressAnnouncement = null;
    if (milestone > lastMilestone && currentTime - lastAt >= UPLOAD_ANNOUNCEMENT_INTERVAL_MS) {
      task.announcedMilestone = milestone;
      task.progressAnnouncementAt = currentTime;
      progressAnnouncement = `上传进度 ${milestone}%`;
    }
    const announcement = [stateAnnouncement, progressAnnouncement].filter(Boolean).join('，');
    if (!announcement) return null;
    return stateAnnouncement ? announcement : `${task.name} ${announcement}`;
  };

  const snapshot = () => Object.freeze(tasks.map(publicTask));
  const isCurrent = token => !destroyed && token === generation;
  const raceWithDestroy = promise => {
    if (destroyed) return Promise.reject(coordinatorDestroyedError());
    return Promise.race([
      Promise.resolve(promise),
      destroyPromise.then(error => { throw error; }),
    ]);
  };
  const notify = (token = generation) => {
    if (!isCurrent(token)) return;
    if (typeof onAnnounce === 'function') {
      const announcement = tasks.map(announceTask).filter(Boolean).join('；');
      if (announcement) {
        try { onAnnounce(announcement); } catch {}
      }
    }
    const value = snapshot();
    const summary = controlSummary();
    for (const listener of listeners) {
      if (!isCurrent(token)) break;
      listener(value, summary);
    }
  };
  const persist = (task, token = generation) => {
    if (!isCurrent(token)) return Promise.resolve();
    return raceWithDestroy(persistence.put(persistedTask(task))).catch(() => {});
  };
  const touchTask = task => { task.reconcileVersion = ++taskRevision; };
  const taskBaseline = task => ({
    uploadId: task.uploadId,
    clientRequestId: task.clientRequestId,
    reconcileVersion: task.reconcileVersion,
  });
  const matchesBaseline = (task, baseline) => Boolean(baseline)
    && task.uploadId === baseline.uploadId
    && task.clientRequestId === baseline.clientRequestId
    && task.reconcileVersion === baseline.reconcileVersion;
  const updateProgress = task => {
    const transferred = Math.min(task.sizeBytes, task.confirmedBytes + task.inFlightBytes);
    task.progressPercent = task.sizeBytes ? Math.min(100, transferred * 100 / task.sizeBytes) : 100;
    const currentTime = now();
    task.samples.push({ time: currentTime, bytes: transferred });
    task.samples = task.samples.filter(sample => currentTime - sample.time <= UPLOAD_SPEED_WINDOW_MS);
    if (task.samples.length < 2) {
      task.speedBytesPerSecond = 0;
      task.etaSeconds = null;
      return;
    }
    const first = task.samples[0];
    const elapsed = currentTime - first.time;
    const bytes = transferred - first.bytes;
    task.speedBytesPerSecond = elapsed > 0 ? Math.max(0, bytes * 1000 / elapsed) : 0;
    task.etaSeconds = elapsed >= UPLOAD_ETA_MIN_SAMPLE_MS && task.speedBytesPerSecond > 0
      ? Math.max(0, (task.sizeBytes - transferred) / task.speedBytesPerSecond)
      : null;
  };
  const confirmedPartBytes = (task, parts) => parts.reduce((total, partIndex) => {
    const start = partIndex * task.chunkSize;
    return total + Math.max(0, Math.min(task.chunkSize, task.sizeBytes - start));
  }, 0);
  const recordServerRevision = (task, response) => {
    if (!response) return;
    const sequence = response.sequence ?? response.server_sequence ?? response.serverSequence;
    const updatedAt = response.updated_at ?? response.updatedAt;
    const version = response.version ?? response.server_version ?? response.serverVersion;
    if (sequence !== undefined && (task.serverSequence === null || task.serverSequence === undefined
        || Number(sequence) > Number(task.serverSequence))) task.serverSequence = sequence;
    if (updatedAt !== undefined && (!task.serverUpdatedAt || String(updatedAt) > String(task.serverUpdatedAt))) {
      task.serverUpdatedAt = updatedAt;
    }
    if (version !== undefined && (task.serverVersion === null || task.serverVersion === undefined
        || Number(version) > Number(task.serverVersion))) task.serverVersion = version;
  };
  const isFreshEvent = (task, response) => {
    const incomingStatus = normalizedStatus(response.status);
    if (TERMINAL_UPLOAD_STATUSES.has(task.status) && !TERMINAL_UPLOAD_STATUSES.has(incomingStatus)) return false;
    if (!TERMINAL_UPLOAD_STATUSES.has(task.status) && TERMINAL_UPLOAD_STATUSES.has(incomingStatus)) return true;
    const sequence = response.sequence ?? response.server_sequence ?? response.serverSequence;
    if (sequence !== undefined && task.serverSequence !== null && task.serverSequence !== undefined) {
      return Number(sequence) > Number(task.serverSequence);
    }
    const updatedAt = response.updated_at ?? response.updatedAt;
    if (updatedAt !== undefined && task.serverUpdatedAt) return String(updatedAt) > String(task.serverUpdatedAt);
    const version = response.version ?? response.server_version ?? response.serverVersion;
    if (version !== undefined && task.serverVersion !== null && task.serverVersion !== undefined) {
      return Number(version) > Number(task.serverVersion);
    }
    return true;
  };
  const applyServerState = (task, response, { authoritative = false } = {}) => {
    const responseChunkSize = response && (response.chunk_size_bytes ?? response.chunkSize);
    if (responseChunkSize !== undefined && responseChunkSize !== null) {
      const resolvedChunkSize = Number(responseChunkSize);
      if (!Number.isInteger(resolvedChunkSize) || resolvedChunkSize < 1) {
        throw new Error('Upload session returned an invalid chunk size');
      }
      task.chunkSize = resolvedChunkSize;
    }
    if (!Number.isInteger(task.chunkSize) || task.chunkSize < 1) {
      throw new Error('Upload session did not return a chunk size');
    }
    const incomingParts = serverParts(response, []);
    task.confirmedParts = authoritative ? incomingParts : unionParts(task.confirmedParts, incomingParts);
    const serverBytes = response && (response.confirmed_bytes ?? response.confirmedBytes) !== undefined
      ? (response.confirmed_bytes ?? response.confirmedBytes)
      : 0;
    const partBytes = confirmedPartBytes(task, task.confirmedParts);
    task.confirmedBytes = authoritative
      ? Math.max(serverBytes, partBytes)
      : Math.max(task.confirmedBytes, serverBytes, partBytes);
    task.inFlightBytes = 0;
    recordServerRevision(task, response);
    updateProgress(task);
    touchTask(task);
  };

  const applyRemoteStatus = (task, data) => {
    const status = normalizedStatus(data.status);
    if (!status) return;
    if (TERMINAL_UPLOAD_STATUSES.has(status)) {
      task.controller?.abort();
      task.inFlightBytes = 0;
      task.status = status;
      task.errorCode = null;
      task.errorMessage = null;
      touchTask(task);
      raceWithDestroy(persistence.remove(task.uploadId)).catch(() => {});
      return;
    }
    task.status = status;
    if (SERVER_OWNED_UPLOAD_STATUSES.has(status)) {
      task.errorCode = null;
      task.errorMessage = null;
    }
    touchTask(task);
  };
  const findTask = uploadId => tasks.find(task => task.uploadId === uploadId || task.clientRequestId === uploadId);
  const operationError = message => new Error(message);
  const persistStrict = (task, token = generation) => {
    if (!isCurrent(token)) throw coordinatorDestroyedError();
    return raceWithDestroy(persistence.put(persistedTask(task)));
  };
  const createMetadataFor = task => {
    if (task.createMetadata) return task.createMetadata;
    if (!task.identity) throw task.prepareError || operationError('上传文件身份尚未生成');
    task.createMetadata = {
      clientRequestId: task.clientRequestId,
      name: task.name,
      sizeBytes: task.sizeBytes,
      mimeType: task.mimeType,
      lastModified: task.identity.lastModified,
      sampleSha256: task.identity.sampleSha256,
    };
    if (task.chunkSize !== null) task.createMetadata.chunkSize = task.chunkSize;
    return task.createMetadata;
  };
  const adoptServerSession = async (task, response, token = generation) => {
    if (!isCurrent(token)) throw coordinatorDestroyedError();
    const remoteUploadId = response?.upload_id || response?.uploadId;
    if (!remoteUploadId) throw operationError('服务端未返回上传会话 ID');
    const previousUploadId = task.uploadId;
    const previousSessionReady = task.sessionReady;
    task.uploadId = remoteUploadId;
    task.sessionReady = true;
    task.prepareError = null;
    task.sourceDeviceId = response.source_device_id || response.sourceDeviceId || task.sourceDeviceId;
    applyServerState(task, response, { authoritative: true });
    applyRemoteStatus(task, response);
    touchTask(task);
    try {
      if (previousUploadId !== task.uploadId && persistence.migrate) {
        await raceWithDestroy(persistence.migrate(previousUploadId, persistedTask(task)));
      } else {
        await persistStrict(task, token);
      }
    } catch (error) {
      task.uploadId = previousUploadId;
      task.sessionReady = previousSessionReady;
      touchTask(task);
      throw error;
    }
    if (!isCurrent(token)) throw coordinatorDestroyedError();
    return task;
  };
  const resolveSessionForCancel = async (task, token = generation) => {
    if (task.sessionAdoptionPromise) await raceWithDestroy(task.sessionAdoptionPromise);
    if (task.sessionReady) return task;
    if (task.preparePromise) await raceWithDestroy(task.preparePromise);
    if (task.sessionAdoptionPromise) await raceWithDestroy(task.sessionAdoptionPromise);
    if (task.sessionReady) return task;
    let response;
    try {
      response = await raceWithDestroy(api.createUploadSession(createMetadataFor(task)));
    } catch (error) {
      if (task.sessionAdoptionPromise) await raceWithDestroy(task.sessionAdoptionPromise);
      if (task.sessionReady) return task;
      throw error;
    }
    if (task.sessionAdoptionPromise) await raceWithDestroy(task.sessionAdoptionPromise);
    if (task.sessionReady) return task;
    return adoptServerSession(task, response, token);
  };
  const removeTaskPersistence = async (task, token = generation) => {
    const uploadIds = Array.from(new Set([task.uploadId, task.clientRequestId].filter(Boolean)));
    for (const uploadId of uploadIds) {
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      await raceWithDestroy(persistence.remove(uploadId));
    }
  };
  const recoverAuthoritativeState = async (task, token, actionError) => {
    try {
      const remote = await raceWithDestroy(api.getUploadSession(task.uploadId));
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      applyServerState(task, remote, { authoritative: true });
      applyRemoteStatus(task, remote);
      task.errorCode = `${task.pendingAction || 'control'}-failed`;
      task.errorMessage = actionError.message;
      touchTask(task);
      if (TERMINAL_UPLOAD_STATUSES.has(task.status)) {
        await raceWithDestroy(persistence.remove(task.uploadId));
      } else {
        await persistStrict(task, token);
      }
    } catch (recoveryError) {
      if (recoveryError.name === 'CoordinatorDestroyedError') throw recoveryError;
      task.errorCode = `${task.pendingAction || 'control'}-failed`;
      task.errorMessage = actionError.message;
      touchTask(task);
    }
  };
  const runControl = (task, action, token = generation) => {
    if (!isCurrent(token)) return Promise.reject(coordinatorDestroyedError());
    if (TERMINAL_UPLOAD_STATUSES.has(normalizedStatus(task.status))
        && !(action === 'cancel' && task.cancelRequested && !task.sessionReady)) {
      return Promise.reject(operationError('上传任务已结束'));
    }
    if (task.actionPromise) {
      return task.pendingAction === action
        ? task.actionPromise
        : Promise.reject(operationError(`正在执行${task.pendingAction}`));
    }
    const operation = (async () => {
      task.pendingAction = action;
      task.errorCode = null;
      task.errorMessage = null;
      if (action === 'cancel') task.cancelRequested = true;
      touchTask(task);
      if (action === 'pause' || action === 'cancel') {
        task.inFlightBytes = 0;
        task.controller?.abort();
      }
      notify(token);
      await persistStrict(task, token);
      if (action === 'cancel') {
        await resolveSessionForCancel(task, token);
      } else if (!task.sessionReady && task.preparePromise) {
        await raceWithDestroy(task.preparePromise);
      }
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      if (task.prepareError) throw task.prepareError;
      if (!task.sessionReady) throw operationError(task.errorMessage || '上传会话尚未创建');

      const response = action === 'cancel'
        ? await raceWithDestroy(api.cancelUpload(task.uploadId))
        : await raceWithDestroy(api.controlUpload(task.uploadId, action));
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      if (response && (response.chunk_size_bytes !== undefined || response.chunkSize !== undefined
          || response.confirmed_parts !== undefined || response.confirmedParts !== undefined
          || response.confirmed_bytes !== undefined || response.confirmedBytes !== undefined)) {
        applyServerState(task, response, { authoritative: true });
      }
      const responseStatus = normalizedStatus(response?.status);
      if (TERMINAL_UPLOAD_STATUSES.has(responseStatus)) {
        applyRemoteStatus(task, response);
      } else {
        task.status = action === 'resume' && responseStatus === 'uploading'
          ? 'queued'
          : (responseStatus || (action === 'pause' ? 'paused' : (action === 'cancel' ? 'cancelled' : 'queued')));
        touchTask(task);
      }
      task.errorCode = null;
      task.errorMessage = null;
      task.pendingAction = null;
      touchTask(task);
      if (TERMINAL_UPLOAD_STATUSES.has(normalizedStatus(task.status))) {
        await removeTaskPersistence(task, token);
      } else {
        await persistStrict(task, token);
      }
      notify(token);
      if (action === 'resume') pump();
      return publicTask(task);
    })().catch(async error => {
      if (error.name === 'CoordinatorDestroyedError') throw error;
      if (action === 'cancel' && !task.sessionReady) {
        if (TERMINAL_UPLOAD_STATUSES.has(normalizedStatus(task.status))) task.status = 'failed';
        task.errorCode = 'cancel-failed';
        task.errorMessage = error.message;
        touchTask(task);
        await persistStrict(task, token);
      } else if (task.prepareError !== error) {
        await recoverAuthoritativeState(task, token, error);
      } else {
        task.errorCode = 'session-create-failed';
        task.errorMessage = error.message;
        touchTask(task);
      }
      task.pendingAction = null;
      touchTask(task);
      notify(token);
      throw error;
    }).finally(() => {
      task.actionPromise = null;
    });
    task.actionPromise = operation;
    return operation;
  };
  const adoptRemoteSession = (task, data, token = generation) => {
    if (task.sessionAdoptionPromise) return task.sessionAdoptionPromise;
    const operation = adoptServerSession(task, data, token).finally(() => {
      if (task.sessionAdoptionPromise === operation) task.sessionAdoptionPromise = null;
    });
    task.sessionAdoptionPromise = operation;
    return operation;
  };
  const scheduleAuthoritativeCancel = (task, data, token = generation) => {
    adoptRemoteSession(task, data, token)
      .then(() => {
        if (!isCurrent(token) || !task.cancelRequested) return null;
        return runControl(task, 'cancel', token);
      })
      .catch(error => {
        if (!isCurrent(token) || error.name === 'CoordinatorDestroyedError') return;
        task.errorCode = 'cancel-failed';
        task.errorMessage = error.message;
        touchTask(task);
        persist(task, token);
        notify(token);
      });
  };
  const nextMissingPart = task => {
    const count = Math.ceil(task.sizeBytes / task.chunkSize);
    for (let index = 0; index < count; index += 1) {
      if (!task.confirmedParts.includes(index)) return index;
    }
    return null;
  };

  async function hashPart(blob, token) {
    const bytes = await raceWithDestroy(blob.arrayBuffer());
    if (!isCurrent(token)) return null;
    const digest = await raceWithDestroy(cryptoObject.subtle.digest('SHA-256', bytes));
    if (!isCurrent(token)) return null;
    return toHex(digest);
  }

  async function uploadOne(task, token) {
    if (!isCurrent(token) || task.status !== 'queued' || task.cancelRequested
        || !task.file || !task.isSourceDevice) return;
    const partIndex = nextMissingPart(task);
    if (partIndex === null) {
      task.status = 'completing';
      touchTask(task);
      notify(token);
      try {
        const message = await raceWithDestroy(api.completeUpload(task.uploadId));
        if (!isCurrent(token)) return;
        if (task.cancelRequested) return;
        if (message && typeof onCompleted === 'function') onCompleted(message);
        task.status = 'completed';
        task.progressPercent = 100;
        task.etaSeconds = 0;
        touchTask(task);
        await raceWithDestroy(persistence.remove(task.uploadId));
        if (!isCurrent(token)) return;
      } catch (error) {
        if (!isCurrent(token)) return;
        if (task.cancelRequested) return;
        task.status = 'failed';
        task.errorCode = 'complete-failed';
        task.errorMessage = error.message;
        touchTask(task);
        persist(task, token);
      }
      notify(token);
      return;
    }

    task.status = 'uploading';
    touchTask(task);
    task.controller = abortControllerFactory();
    if (!task.samples.length) task.samples = [{ time: now(), bytes: task.confirmedBytes }];
    const start = partIndex * task.chunkSize;
    const endExclusive = Math.min(task.sizeBytes, start + task.chunkSize);
    const blob = task.file.slice(start, endExclusive);
    let attempt = 0;
    notify(token);
    while (isCurrent(token) && task.status === 'uploading') {
      try {
        const sha256 = await hashPart(blob, token);
        if (!isCurrent(token) || task.status !== 'uploading') break;
        const response = await raceWithDestroy(api.uploadPart(
          task.uploadId,
          partIndex,
          blob,
          { start, end: endExclusive - 1, total: task.sizeBytes, sha256 },
          loaded => {
            if (!isCurrent(token) || task.status !== 'uploading') return;
            task.inFlightBytes = Math.min(blob.size ?? endExclusive - start, loaded);
            updateProgress(task);
            touchTask(task);
            notify(token);
          },
          task.controller.signal,
        ));
        if (!isCurrent(token)) return;
        applyServerState(task, response);
        if (task.status === 'uploading') task.status = 'queued';
        task.errorCode = null;
        task.errorMessage = null;
        touchTask(task);
        persist(task, token);
        notify(token);
        break;
      } catch (error) {
        if (!isCurrent(token)) return;
        task.inFlightBytes = 0;
        touchTask(task);
        if (task.status === 'paused' || task.status === 'cancelled' || error.name === 'AbortError') break;
        if (attempt >= UPLOAD_RETRY_DELAYS.length) {
          task.status = 'failed';
          task.errorCode = 'upload-failed';
          task.errorMessage = error.message;
          touchTask(task);
          persist(task, token);
          notify(token);
          break;
        }
        await raceWithDestroy(delay(UPLOAD_RETRY_DELAYS[attempt]));
        if (!isCurrent(token)) return;
        attempt += 1;
      }
    }
    if (isCurrent(token)) task.controller = null;
  }

  function pump() {
    if (destroyed || pumping) return;
    const token = generation;
    pumping = true;
    Promise.resolve().then(async () => {
      if (!isCurrent(token)) return;
      while (isCurrent(token)) {
        const active = tasks.filter(task => task.worker).length;
        const available = maxActive - active;
        if (available <= 0) break;
        const ready = tasks.filter(task => task.status === 'queued' && !task.cancelRequested
          && !task.pendingAction && task.file
          && task.isSourceDevice && !task.worker).slice(0, available);
        if (!ready.length) break;
        ready.forEach(task => {
          const worker = uploadOne(task, token).finally(() => {
            task.worker = null;
            pendingWorkers.delete(worker);
            if (!isCurrent(token)) return;
            pump();
          });
          task.worker = worker;
          pendingWorkers.add(worker);
        });
        await Promise.resolve();
        if (!isCurrent(token)) return;
      }
      if (isCurrent(token)) pumping = false;
    });
  }

  function newTask(file, fileHandle) {
    const clientRequestId = cryptoObject.randomUUID();
    return {
      uploadId: clientRequestId, clientRequestId, file, fileHandle: fileHandle || null,
      identity: null, name: file.name, sizeBytes: file.size, mimeType: file.type || '',
      status: 'preparing', confirmedParts: [], confirmedBytes: 0, inFlightBytes: 0,
      progressPercent: 0, speedBytesPerSecond: 0, etaSeconds: null,
      sourceDeviceId: null, isSourceDevice: true, errorCode: null, errorMessage: null,
      createdAt: normalizeCreatedAt(now(), Date.now()), samples: [], controller: null, worker: null,
      pendingAction: null, actionPromise: null, cancelRequested: false,
      preparePromise: null, prepareError: null, createMetadata: null,
      sessionAdoptionPromise: null,
      sessionReady: false,
      chunkSize: Number.isInteger(chunkSize) && chunkSize > 0 ? chunkSize : null,
      liveRevision: 0,
      reconcileVersion: 0,
      serverSequence: null, serverUpdatedAt: null, serverVersion: null,
    };
  }

  async function prepare(task, token) {
    if (!isCurrent(token)) return;
    if (task.sizeBytes > MAX_UPLOAD_SIZE_BYTES) {
      task.status = 'failed';
      task.errorCode = 'file-too-large';
      task.errorMessage = '文件大小超过 512 MiB 限制';
      touchTask(task);
      notify(token);
      return;
    }
    try {
      task.identity = await raceWithDestroy(sampleFileIdentity(task.file, cryptoObject));
      touchTask(task);
      if (!isCurrent(token)) return;
      const response = await raceWithDestroy(api.createUploadSession(createMetadataFor(task)));
      if (!isCurrent(token)) return;
      await adoptServerSession(task, response, token);
      if (task.cancelRequested) {
        return;
      }
      const responseStatus = normalizedStatus(response.status);
      task.status = SERVER_OWNED_UPLOAD_STATUSES.has(responseStatus)
        ? responseStatus
        : (task.status === 'paused' || responseStatus === 'paused' ? 'paused' : 'queued');
      touchTask(task);
      if (task.status === 'paused') {
        await raceWithDestroy(api.controlUpload(task.uploadId, 'pause')).catch(() => {});
      }
      if (!isCurrent(token)) return;
      await persist(task, token);
      if (!isCurrent(token)) return;
    } catch (error) {
      if (!isCurrent(token)) return;
      task.prepareError = error;
      task.status = 'failed';
      task.errorCode = 'session-create-failed';
      task.errorMessage = error.message;
      touchTask(task);
    }
    notify(token);
    pump();
  }

  function restoredTask(record) {
    return {
      ...record, file: null, fileHandle: record.fileHandle || null,
      createdAt: normalizeCreatedAt(record.createdAt, now()),
      identity: record.identity || null, confirmedParts: record.confirmedParts || [],
      confirmedBytes: record.confirmedBytes || 0, inFlightBytes: 0,
      progressPercent: record.sizeBytes ? (record.confirmedBytes || 0) * 100 / record.sizeBytes : 0,
      speedBytesPerSecond: 0, etaSeconds: null, samples: [], controller: null, worker: null,
      isSourceDevice: Boolean(record.isSourceDevice),
      sessionReady: record.sessionReady ?? (record.uploadId !== record.clientRequestId),
      chunkSize: record.chunkSize ?? record.chunk_size_bytes ?? chunkSize,
      liveRevision: record.liveRevision ?? 0,
      reconcileVersion: 0,
      pendingAction: null, actionPromise: null, cancelRequested: Boolean(record.cancelRequested),
      preparePromise: Promise.resolve(), prepareError: null, createMetadata: null,
      sessionAdoptionPromise: null,
      serverSequence: record.serverSequence ?? null,
      serverUpdatedAt: record.serverUpdatedAt ?? null,
      serverVersion: record.serverVersion ?? null,
    };
  }

  function observerTask(data) {
    const identity = data.sample_sha256 || data.sampleSha256 ? {
      name: data.original_name || data.name,
      size: data.size_bytes ?? data.sizeBytes ?? 0,
      lastModified: data.last_modified_ms ?? data.lastModified,
      sampleSha256: data.sample_sha256 || data.sampleSha256,
    } : null;
    return {
      uploadId: data.upload_id || data.uploadId,
      clientRequestId: data.client_request_id || data.clientRequestId || null,
      file: null, fileHandle: null, identity,
      name: data.original_name || data.name || '未命名文件',
      sizeBytes: data.size_bytes ?? data.sizeBytes ?? 0,
      mimeType: data.mime_type || data.mimeType || '', status: normalizedStatus(data.status) || 'queued',
      confirmedParts: [], confirmedBytes: 0, inFlightBytes: 0, progressPercent: 0,
      speedBytesPerSecond: 0, etaSeconds: null,
      sourceDeviceId: data.source_device_id || data.sourceDeviceId || null,
      isSourceDevice: false, errorCode: null, errorMessage: null,
      createdAt: normalizeCreatedAt(data.created_at || data.createdAt, now()), samples: [], controller: null,
      worker: null, sessionReady: true,
      chunkSize: data.chunk_size_bytes ?? data.chunkSize ?? chunkSize,
      liveRevision: 0,
      reconcileVersion: 0,
      pendingAction: null, actionPromise: null, cancelRequested: false,
      preparePromise: Promise.resolve(), prepareError: null, createMetadata: null,
      sessionAdoptionPromise: null,
      serverSequence: null, serverUpdatedAt: null, serverVersion: null,
    };
  }

  async function coordinateSourceFile(task, token) {
    if (!task.isSourceDevice || TERMINAL_UPLOAD_STATUSES.has(task.status)) return;
    if (SERVER_OWNED_UPLOAD_STATUSES.has(normalizedStatus(task.status))) return;
    if (!task.file && task.fileHandle && typeof task.fileHandle.queryPermission === 'function') {
      try {
        const permission = await raceWithDestroy(task.fileHandle.queryPermission({ mode: 'read' }));
        if (!isCurrent(token)) return;
        if (permission === 'granted') {
          const file = await raceWithDestroy(task.fileHandle.getFile());
          if (!isCurrent(token)) return;
          if (await raceWithDestroy(matchesFileIdentity(file, task.identity, cryptoObject))) {
            task.file = file;
            touchTask(task);
          }
        }
      } catch {}
    }
    if (!isCurrent(token)) return;
    if (!task.file) {
      if (BATCH_CONTROL_STATUSES.pause.has(normalizedStatus(task.status))) {
        await runControl(task, 'pause', token);
      }
      if (!isCurrent(token) || SERVER_OWNED_UPLOAD_STATUSES.has(normalizedStatus(task.status))
          || TERMINAL_UPLOAD_STATUSES.has(normalizedStatus(task.status))) return;
      task.status = 'paused';
      task.errorCode = 'reselect_required';
      task.errorMessage = '请重新选择原文件以继续上传';
      touchTask(task);
      return;
    }
    if (task.status === 'paused' || task.status === 'failed') {
      await runControl(task, 'resume', token);
      return;
    }
    task.status = 'queued';
    task.errorCode = null;
    task.errorMessage = null;
    touchTask(task);
  }

  async function settleControls(action, selectedTasks) {
    if (controlSummary().hasPendingControl) {
      return { action, total: 0, succeeded: 0, failed: 0, results: [] };
    }
    const results = await Promise.allSettled(
      selectedTasks.map(task => coordinator[action](task.uploadId)),
    );
    const succeeded = results.filter(result => result.status === 'fulfilled').length;
    return {
      action,
      total: results.length,
      succeeded,
      failed: results.length - succeeded,
      results,
    };
  }

  const coordinator = {
    async start() {
      const token = generation;
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      await coordinator.reconcile();
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      pump();
      notify(token);
      return snapshot();
    },
    enqueueFiles(files) {
      if (destroyed) return snapshot();
      const token = generation;
      Array.from(files).forEach(item => {
        const file = item.file || item;
        const task = newTask(file, item.fileHandle);
        tasks.push(task);
        task.preparePromise = prepare(task, token);
      });
      notify(token);
      return snapshot();
    },
    async pause(uploadId) {
      if (destroyed) throw coordinatorDestroyedError();
      const task = findTask(uploadId);
      if (!task) throw operationError('上传任务不存在');
      if (!task.isSourceDevice) {
        task.errorCode = 'source_device_required';
        task.errorMessage = '源设备控制暂停和继续';
        touchTask(task);
        notify();
        throw operationError(task.errorMessage);
      }
      return runControl(task, 'pause');
    },
    async resume(uploadId) {
      if (destroyed) throw coordinatorDestroyedError();
      const task = findTask(uploadId);
      if (!task) throw operationError('上传任务不存在');
      if (!task.isSourceDevice) {
        task.errorCode = 'source_device_required';
        task.errorMessage = '源设备控制暂停和继续';
        touchTask(task);
        notify();
        throw operationError(task.errorMessage);
      }
      if (!task.file) {
        task.status = 'paused';
        task.errorCode = 'reselect_required';
        task.errorMessage = '请重新选择原文件以继续上传';
        touchTask(task);
        notify();
        throw operationError(task.errorMessage);
      }
      return runControl(task, 'resume');
    },
    async reselect(uploadId, file) {
      const token = generation;
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      const task = findTask(uploadId);
      if (!task || !task.isSourceDevice) return false;
      if (TERMINAL_UPLOAD_STATUSES.has(normalizedStatus(task.status))) throw operationError('上传任务已结束');
      if (!BATCH_CONTROL_STATUSES.resume.has(normalizedStatus(task.status))) {
        throw operationError('当前状态无法重新选择文件');
      }
      const matches = await raceWithDestroy(matchesFileIdentity(file, task.identity, cryptoObject));
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      if (!matches) {
        task.status = 'paused';
        task.errorCode = 'file_mismatch';
        task.errorMessage = '所选文件与原文件不一致';
        touchTask(task);
        persist(task, token);
        notify(token);
        return false;
      }
      const remote = await raceWithDestroy(api.getUploadSession(task.uploadId));
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      applyServerState(task, remote, { authoritative: true });
      applyRemoteStatus(task, remote);
      const remoteStatus = normalizedStatus(remote.status);
      if (!BATCH_CONTROL_STATUSES.resume.has(remoteStatus)) {
        if (!TERMINAL_UPLOAD_STATUSES.has(remoteStatus)) await persist(task, token);
        notify(token);
        if (TERMINAL_UPLOAD_STATUSES.has(remoteStatus)) throw operationError('上传任务已结束');
        throw operationError('服务端当前状态无法重新选择文件');
      }
      task.file = file;
      touchTask(task);
      await runControl(task, 'resume', token);
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      await persist(task, token);
      notify(token);
      pump();
      return true;
    },
    async cancel(uploadId) {
      if (destroyed) throw coordinatorDestroyedError();
      const task = findTask(uploadId);
      if (!task) throw operationError('上传任务不存在');
      return runControl(task, 'cancel');
    },
    async retry(uploadId) {
      const token = generation;
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      const task = findTask(uploadId);
      if (!task || !task.file || !task.isSourceDevice) throw operationError('上传任务无法重试');
      if (task.status !== 'failed') throw operationError('仅失败任务可以重试');
      const response = await raceWithDestroy(api.getUploadSession(task.uploadId));
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      if (task.worker || task.inFlightBytes || task.status === 'uploading') throw operationError('上传任务仍在运行');
      applyServerState(task, response, { authoritative: true });
      const remoteStatus = normalizedStatus(response.status);
      applyRemoteStatus(task, response);
      if (TERMINAL_UPLOAD_STATUSES.has(remoteStatus)) {
        notify(token);
        throw operationError('上传任务已结束');
      }
      if (SERVER_OWNED_UPLOAD_STATUSES.has(remoteStatus)) {
        await persist(task, token);
        notify(token);
        throw operationError('服务端正在校验，无法重试');
      }
      if (remoteStatus === 'queued' || remoteStatus === 'uploading') {
        task.status = 'queued';
        task.errorCode = null;
        task.errorMessage = null;
        touchTask(task);
        await persistStrict(task, token);
        notify(token);
        pump();
        return publicTask(task);
      }
      if (!BATCH_CONTROL_STATUSES.resume.has(remoteStatus)) {
        await persist(task, token);
        notify(token);
        throw operationError('服务端当前状态无法重试');
      }
      await runControl(task, 'resume', token);
      return publicTask(task);
    },
    prioritize(uploadId) {
      if (destroyed) return;
      const index = tasks.findIndex(task => task.uploadId === uploadId || task.clientRequestId === uploadId);
      if (index < 1 || tasks[index].status !== 'queued' || tasks[index].cancelRequested) return;
      const [task] = tasks.splice(index, 1);
      const firstQueued = tasks.findIndex(item => item.status === 'queued');
      tasks.splice(firstQueued < 0 ? tasks.length : firstQueued, 0, task);
      notify();
      pump();
    },
    async pauseAll() {
      return settleControls('pause', tasks.filter(task => task.isSourceDevice
        && BATCH_CONTROL_STATUSES.pause.has(normalizedStatus(task.status))));
    },
    async resumeAll() {
      return settleControls('resume', tasks.filter(task => task.isSourceDevice
        && BATCH_CONTROL_STATUSES.resume.has(normalizedStatus(task.status))));
    },
    async cancelAll() {
      return settleControls('cancel', tasks.filter(task => BATCH_CONTROL_STATUSES.cancel.has(normalizedStatus(task.status))));
    },
    async reconcile() {
      const token = generation;
      const baselines = new Map(tasks.map(task => [task, taskBaseline(task)]));
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      const remote = api.listActiveUploads ? await raceWithDestroy(api.listActiveUploads()) : [];
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      const records = await raceWithDestroy(persistence.getAll());
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      const remoteById = new Map(remote.map(item => {
        const data = eventData(item);
        return [data.upload_id || data.uploadId, data];
      }));
      const remoteByClientRequestId = new Map(remote.map(item => {
        const data = eventData(item);
        return [data.client_request_id || data.clientRequestId, data];
      }).filter(([clientRequestId]) => Boolean(clientRequestId)));
      const activeUploadIds = new Set(remoteById.keys());

      for (const record of records) {
        if (!isCurrent(token)) throw coordinatorDestroyedError();
        let task = findTask(record.uploadId);
        if (!task) {
          task = restoredTask(record);
          tasks.push(task);
          baselines.set(task, taskBaseline(task));
        }
        const data = remoteById.get(record.uploadId)
          || remoteByClientRequestId.get(record.clientRequestId);
        if (data) {
          remoteById.delete(data.upload_id || data.uploadId);
          if (matchesBaseline(task, baselines.get(task))) {
            task.clientRequestId = data.client_request_id || data.clientRequestId || task.clientRequestId;
            task.name = data.original_name || data.name || task.name;
            task.sizeBytes = data.size_bytes ?? data.sizeBytes ?? task.sizeBytes;
            task.mimeType = data.mime_type || data.mimeType || task.mimeType;
            task.sourceDeviceId = data.source_device_id || data.sourceDeviceId || task.sourceDeviceId;
            if (task.cancelRequested) {
              await adoptRemoteSession(task, data, token);
              await runControl(task, 'cancel', token);
              continue;
            }
            applyServerState(task, data, { authoritative: true });
            applyRemoteStatus(task, data);
            await coordinateSourceFile(task, token);
          }
          if (!isCurrent(token)) throw coordinatorDestroyedError();
          await persist(task, token);
          continue;
        }
      }

      remoteById.forEach(data => {
        const existing = findTask(data.upload_id || data.uploadId)
          || findTask(data.client_request_id || data.clientRequestId);
        if (!existing || matchesBaseline(existing, baselines.get(existing))) {
          coordinator.applyRemoteEvent({ event_type: 'upload.created', payload: data });
        }
      });
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      for (let index = tasks.length - 1; index >= 0; index -= 1) {
        const task = tasks[index];
        if (!task.sessionReady || activeUploadIds.has(task.uploadId)
            || !matchesBaseline(task, baselines.get(task))) continue;
        task.controller?.abort();
        tasks.splice(index, 1);
        await raceWithDestroy(persistence.remove(task.uploadId)).catch(() => {});
        if (!isCurrent(token)) throw coordinatorDestroyedError();
      }
      notify(token);
      pump();
      return snapshot();
    },
    applyRemoteEvent(event) {
      if (destroyed) return false;
      const data = eventData(event);
      const upsertRemote = payload => {
        const eventRevision = ++liveRevision;
        const remoteUploadId = payload.upload_id || payload.uploadId;
        const remoteClientRequestId = payload.client_request_id || payload.clientRequestId;
        let task = findTask(remoteUploadId) || findTask(remoteClientRequestId);
        if (!task) {
          task = observerTask(payload);
          tasks.push(task);
        } else if (!task.cancelRequested && !isFreshEvent(task, payload)) {
          return true;
        }
        const remoteStatus = normalizedStatus(payload.status);
        if (task.cancelRequested && TERMINAL_UPLOAD_STATUSES.has(remoteStatus)) {
          if (remoteUploadId) task.uploadId = remoteUploadId;
          if (remoteClientRequestId) task.clientRequestId = remoteClientRequestId;
          applyServerState(task, payload);
          applyRemoteStatus(task, payload);
          task.liveRevision = eventRevision;
          notify();
          return true;
        }
        if (task.cancelRequested && remoteUploadId) {
          task.liveRevision = eventRevision;
          scheduleAuthoritativeCancel(task, payload);
          notify();
          return true;
        }
        if (remoteUploadId) task.uploadId = remoteUploadId;
        if (remoteClientRequestId) task.clientRequestId = remoteClientRequestId;
        applyServerState(task, payload);
        applyRemoteStatus(task, payload);
        task.liveRevision = eventRevision;
        notify();
        return true;
      };
      const mergeRemoteProgress = payload => upsertRemote(payload);
      const mergeRemoteState = payload => upsertRemote(payload);
      const completeRemote = payload => upsertRemote({ ...payload, status: 'completed' });
      const terminalRemote = (payload, status) => upsertRemote({ ...payload, status });
      const UPLOAD_EVENT_HANDLERS = {
        'upload.created': payload => upsertRemote(payload),
        'upload.progress': payload => mergeRemoteProgress(payload),
        'upload.state_changed': payload => mergeRemoteState(payload),
        'upload.completed': payload => completeRemote(payload),
        'upload.cancelled': payload => terminalRemote(payload, 'cancelled'),
        'upload.expired': payload => terminalRemote(payload, 'expired'),
      };
      const handler = UPLOAD_EVENT_HANDLERS[event?.event_type] || UPLOAD_EVENT_HANDLERS['upload.created'];
      return handler(data);
    },
    subscribe(listener) {
      if (destroyed) return () => {};
      listeners.add(listener);
      listener(snapshot(), controlSummary());
      return () => listeners.delete(listener);
    },
    getSnapshot: snapshot,
    getSummary: controlSummary,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      generation += 1;
      resolveDestroy(coordinatorDestroyedError());
      tasks.forEach(task => task.controller?.abort());
      tasks.forEach(task => {
        task.file = null;
        task.fileHandle = null;
        task.controller = null;
      });
      listeners.clear();
      if (!persistenceClosed) {
        persistenceClosed = true;
        persistence.close();
      }
    },
  };
  return coordinator;
}
