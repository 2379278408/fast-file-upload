import { MAX_ACTIVE_UPLOADS, MAX_UPLOAD_SIZE_BYTES, UPLOAD_CHUNK_SIZE_BYTES, UPLOAD_ETA_MIN_SAMPLE_MS, UPLOAD_RETRY_DELAYS, UPLOAD_SPEED_WINDOW_MS } from './config.js';

const IDENTITY_SAMPLE_BYTES = 64 * 1024;
const IDENTITY_VERSION = 'sample-identity-v1';
const LIVE_MILESTONES = [25, 50, 75, 100];
const UPLOAD_ANNOUNCEMENT_INTERVAL_MS = 1000;
const TERMINAL_UPLOAD_STATUSES = new Set(['complete', 'completed', 'cancelled', 'expired']);
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
  chunkSize = UPLOAD_CHUNK_SIZE_BYTES,
  onAnnounce = null,
  onCompleted = null,
}) {
  const tasks = [];
  const listeners = new Set();
  let destroyed = false;
  let pumping = false;
  let generation = 0;
  let persistenceClosed = false;
  let resolveDestroy;
  const destroyPromise = new Promise(resolve => { resolveDestroy = resolve; });
  const pendingWorkers = new Set();

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
    for (const listener of listeners) {
      if (!isCurrent(token)) break;
      listener(value);
    }
  };
  const persist = (task, token = generation) => {
    if (!isCurrent(token)) return Promise.resolve();
    return raceWithDestroy(persistence.put(persistedTask(task))).catch(() => {});
  };
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
    const start = partIndex * chunkSize;
    return total + Math.max(0, Math.min(chunkSize, task.sizeBytes - start));
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
      raceWithDestroy(persistence.remove(task.uploadId)).catch(() => {});
      return;
    }
    task.status = status;
  };
  const findTask = uploadId => tasks.find(task => task.uploadId === uploadId || task.clientRequestId === uploadId);
  const nextMissingPart = task => {
    const count = Math.ceil(task.sizeBytes / chunkSize);
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
    if (!isCurrent(token) || task.status !== 'queued' || !task.file || !task.isSourceDevice) return;
    const partIndex = nextMissingPart(task);
    if (partIndex === null) {
      task.status = 'completing';
      notify(token);
      try {
        const message = await raceWithDestroy(api.completeUpload(task.uploadId));
        if (!isCurrent(token)) return;
        if (message && typeof onCompleted === 'function') onCompleted(message);
        task.status = 'completed';
        task.progressPercent = 100;
        task.etaSeconds = 0;
        await raceWithDestroy(persistence.remove(task.uploadId));
        if (!isCurrent(token)) return;
      } catch (error) {
        if (!isCurrent(token)) return;
        task.status = 'failed';
        task.errorCode = 'complete-failed';
        task.errorMessage = error.message;
        persist(task, token);
      }
      notify(token);
      return;
    }

    task.status = 'uploading';
    task.controller = abortControllerFactory();
    if (!task.samples.length) task.samples = [{ time: now(), bytes: task.confirmedBytes }];
    const start = partIndex * chunkSize;
    const endExclusive = Math.min(task.sizeBytes, start + chunkSize);
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
            notify(token);
          },
          task.controller.signal,
        ));
        if (!isCurrent(token)) return;
        applyServerState(task, response);
        if (task.status === 'uploading') task.status = 'queued';
        task.errorCode = null;
        task.errorMessage = null;
        persist(task, token);
        notify(token);
        break;
      } catch (error) {
        if (!isCurrent(token)) return;
        task.inFlightBytes = 0;
        if (task.status === 'paused' || task.status === 'cancelled' || error.name === 'AbortError') break;
        if (attempt >= UPLOAD_RETRY_DELAYS.length) {
          task.status = 'failed';
          task.errorCode = 'upload-failed';
          task.errorMessage = error.message;
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
        const ready = tasks.filter(task => task.status === 'queued' && task.file
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
      createdAt: now(), samples: [], controller: null, worker: null,
      sessionReady: false,
      serverSequence: null, serverUpdatedAt: null, serverVersion: null,
    };
  }

  async function prepare(task, token) {
    if (!isCurrent(token)) return;
    if (task.sizeBytes > MAX_UPLOAD_SIZE_BYTES) {
      task.status = 'failed';
      task.errorCode = 'file-too-large';
      task.errorMessage = '文件大小超过 512 MiB 限制';
      notify(token);
      return;
    }
    try {
      task.identity = await raceWithDestroy(sampleFileIdentity(task.file, cryptoObject));
      if (!isCurrent(token) || task.status === 'cancelled') return;
      const response = await raceWithDestroy(api.createUploadSession({
        clientRequestId: task.clientRequestId,
        name: task.name,
        sizeBytes: task.sizeBytes,
        mimeType: task.mimeType,
        lastModified: task.identity.lastModified,
        chunkSize,
        sampleSha256: task.identity.sampleSha256,
      }));
      if (!isCurrent(token)) return;
      task.uploadId = response.upload_id || response.uploadId || task.uploadId;
      task.sessionReady = true;
      task.sourceDeviceId = response.source_device_id || response.sourceDeviceId || null;
      applyServerState(task, response, { authoritative: true });
      if (task.status === 'cancelled') {
        await raceWithDestroy(api.cancelUpload(task.uploadId)).catch(() => {});
        if (!isCurrent(token)) return;
        await raceWithDestroy(persistence.remove(task.uploadId)).catch(() => {});
        if (!isCurrent(token)) return;
        return;
      }
      task.status = task.status === 'paused' || response.status === 'paused' ? 'paused' : 'queued';
      if (task.status === 'paused') {
        await raceWithDestroy(api.controlUpload(task.uploadId, 'pause')).catch(() => {});
      }
      if (!isCurrent(token)) return;
      await persist(task, token);
      if (!isCurrent(token)) return;
    } catch (error) {
      if (!isCurrent(token)) return;
      task.status = 'failed';
      task.errorCode = 'session-create-failed';
      task.errorMessage = error.message;
    }
    notify(token);
    pump();
  }

  function restoredTask(record) {
    return {
      ...record, file: null, fileHandle: record.fileHandle || null,
      identity: record.identity || null, confirmedParts: record.confirmedParts || [],
      confirmedBytes: record.confirmedBytes || 0, inFlightBytes: 0,
      progressPercent: record.sizeBytes ? (record.confirmedBytes || 0) * 100 / record.sizeBytes : 0,
      speedBytesPerSecond: 0, etaSeconds: null, samples: [], controller: null, worker: null,
      isSourceDevice: Boolean(record.isSourceDevice), sessionReady: true,
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
      createdAt: data.created_at || data.createdAt || now(), samples: [], controller: null,
      worker: null, sessionReady: true,
      serverSequence: null, serverUpdatedAt: null, serverVersion: null,
    };
  }

  async function coordinateSourceFile(task, token) {
    if (!task.isSourceDevice || TERMINAL_UPLOAD_STATUSES.has(task.status)) return;
    if (!task.file && task.fileHandle && typeof task.fileHandle.queryPermission === 'function') {
      try {
        const permission = await raceWithDestroy(task.fileHandle.queryPermission({ mode: 'read' }));
        if (!isCurrent(token)) return;
        if (permission === 'granted') {
          const file = await raceWithDestroy(task.fileHandle.getFile());
          if (!isCurrent(token)) return;
          if (await raceWithDestroy(matchesFileIdentity(file, task.identity, cryptoObject))) task.file = file;
        }
      } catch {}
    }
    if (!isCurrent(token)) return;
    if (!task.file) {
      task.status = 'paused';
      task.errorCode = 'reselect_required';
      task.errorMessage = '请重新选择原文件以继续上传';
      return;
    }
    task.status = 'queued';
    task.errorCode = null;
    task.errorMessage = null;
    if (api.controlUpload) await raceWithDestroy(api.controlUpload(task.uploadId, 'resume')).catch(() => {});
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
        prepare(task, token);
      });
      notify(token);
      return snapshot();
    },
    pause(uploadId) {
      if (destroyed) return;
      const task = findTask(uploadId);
      if (!task || ['completed', 'cancelled'].includes(task.status)) return;
      if (!task.isSourceDevice) {
        task.errorCode = 'source_device_required';
        task.errorMessage = '源设备控制暂停和继续';
        notify();
        return;
      }
      task.status = 'paused';
      task.inFlightBytes = 0;
      task.controller?.abort();
      persist(task);
      if (task.sessionReady) raceWithDestroy(api.controlUpload(task.uploadId, 'pause')).catch(() => {});
      notify();
    },
    resume(uploadId) {
      if (destroyed) return;
      const task = findTask(uploadId);
      if (!task) return;
      if (!task.isSourceDevice) {
        task.errorCode = 'source_device_required';
        task.errorMessage = '源设备控制暂停和继续';
        notify();
        return;
      }
      if (!task.file) {
        task.status = 'paused';
        task.errorCode = 'reselect_required';
        task.errorMessage = '请重新选择原文件以继续上传';
        notify();
        return;
      }
      task.status = 'queued';
      task.errorCode = null;
      task.errorMessage = null;
      if (task.sessionReady) raceWithDestroy(api.controlUpload(task.uploadId, 'resume')).catch(() => {});
      persist(task);
      notify();
      pump();
    },
    async reselect(uploadId, file) {
      const token = generation;
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      const task = findTask(uploadId);
      if (!task || !task.isSourceDevice || TERMINAL_UPLOAD_STATUSES.has(task.status)) return false;
      const matches = await raceWithDestroy(matchesFileIdentity(file, task.identity, cryptoObject));
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      if (!matches) {
        task.status = 'paused';
        task.errorCode = 'file_mismatch';
        task.errorMessage = '所选文件与原文件不一致';
        persist(task, token);
        notify(token);
        return false;
      }
      let shouldResumeServer = true;
      if (api.getUploadSession) {
        const remote = await raceWithDestroy(api.getUploadSession(task.uploadId));
        if (!isCurrent(token)) throw coordinatorDestroyedError();
        applyServerState(task, remote, { authoritative: true });
        shouldResumeServer = normalizedStatus(remote.status) === 'paused';
        if (TERMINAL_UPLOAD_STATUSES.has(normalizedStatus(remote.status))) {
          applyRemoteStatus(task, remote);
          notify(token);
          return false;
        }
      }
      task.file = file;
      task.status = 'queued';
      task.errorCode = null;
      task.errorMessage = null;
      if (shouldResumeServer && api.controlUpload) {
        await raceWithDestroy(api.controlUpload(task.uploadId, 'resume')).catch(() => {});
      }
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      await persist(task, token);
      notify(token);
      pump();
      return true;
    },
    cancel(uploadId) {
      if (destroyed) return;
      const task = findTask(uploadId);
      if (!task) return;
      task.status = 'cancelled';
      task.inFlightBytes = 0;
      task.controller?.abort();
      if (task.sessionReady) raceWithDestroy(api.cancelUpload(task.uploadId)).catch(() => {});
      raceWithDestroy(persistence.remove(task.uploadId)).catch(() => {});
      notify();
    },
    async retry(uploadId) {
      const token = generation;
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      const task = findTask(uploadId);
      if (!task || !task.file || !task.isSourceDevice) return;
      try {
        const response = await raceWithDestroy(api.getUploadSession(task.uploadId));
        if (!isCurrent(token)) throw coordinatorDestroyedError();
        if (task.worker || task.inFlightBytes || task.status === 'uploading') return;
        applyServerState(task, response, { authoritative: true });
        task.status = 'queued';
        task.errorCode = null;
        task.errorMessage = null;
        persist(task, token);
        notify(token);
        pump();
      } catch (error) {
        if (error.name === 'CoordinatorDestroyedError') throw error;
        if (!isCurrent(token)) throw coordinatorDestroyedError();
        task.errorMessage = error.message;
        notify();
      }
    },
    prioritize(uploadId) {
      if (destroyed) return;
      const index = tasks.findIndex(task => task.uploadId === uploadId || task.clientRequestId === uploadId);
      if (index < 1 || tasks[index].status !== 'queued') return;
      const [task] = tasks.splice(index, 1);
      const firstQueued = tasks.findIndex(item => item.status === 'queued');
      tasks.splice(firstQueued < 0 ? tasks.length : firstQueued, 0, task);
      notify();
      pump();
    },
    pauseAll() { tasks.forEach(task => coordinator.pause(task.uploadId)); },
    resumeAll() { tasks.forEach(task => coordinator.resume(task.uploadId)); },
    cancelAll() { tasks.forEach(task => coordinator.cancel(task.uploadId)); },
    async reconcile() {
      const token = generation;
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      const remote = api.listActiveUploads ? await raceWithDestroy(api.listActiveUploads()) : [];
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      const records = await raceWithDestroy(persistence.getAll());
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      const remoteById = new Map(remote.map(item => {
        const data = eventData(item);
        return [data.upload_id || data.uploadId, data];
      }));

      for (const record of records) {
        if (!isCurrent(token)) throw coordinatorDestroyedError();
        let task = findTask(record.uploadId);
        if (!task) {
          task = restoredTask(record);
          tasks.push(task);
        }
        const data = remoteById.get(record.uploadId);
        if (data) {
          remoteById.delete(record.uploadId);
          task.clientRequestId = data.client_request_id || data.clientRequestId || task.clientRequestId;
          task.name = data.original_name || data.name || task.name;
          task.sizeBytes = data.size_bytes ?? data.sizeBytes ?? task.sizeBytes;
          task.mimeType = data.mime_type || data.mimeType || task.mimeType;
          task.sourceDeviceId = data.source_device_id || data.sourceDeviceId || task.sourceDeviceId;
          applyServerState(task, data, { authoritative: true });
          applyRemoteStatus(task, data);
          await coordinateSourceFile(task, token);
          if (!isCurrent(token)) throw coordinatorDestroyedError();
          await persist(task, token);
          continue;
        }
        if (api.getUploadSession) {
          try {
            const finalData = await raceWithDestroy(api.getUploadSession(record.uploadId));
            if (!isCurrent(token)) throw coordinatorDestroyedError();
            if (TERMINAL_UPLOAD_STATUSES.has(normalizedStatus(finalData.status))) {
              applyServerState(task, finalData, { authoritative: true });
              applyRemoteStatus(task, finalData);
              await raceWithDestroy(persistence.remove(task.uploadId)).catch(() => {});
              continue;
            }
          } catch (error) {
            if (error.name === 'CoordinatorDestroyedError') throw error;
          }
        }
        await coordinateSourceFile(task, token);
        await persist(task, token);
      }

      remoteById.forEach(data => coordinator.applyRemoteEvent({ event_type: 'upload.created', payload: data }));
      if (!isCurrent(token)) throw coordinatorDestroyedError();
      notify(token);
      pump();
      return snapshot();
    },
    applyRemoteEvent(event) {
      if (destroyed) return false;
      const data = eventData(event);
      const upsertRemote = payload => {
        const remoteUploadId = payload.upload_id || payload.uploadId;
        const remoteClientRequestId = payload.client_request_id || payload.clientRequestId;
        let task = findTask(remoteUploadId) || findTask(remoteClientRequestId);
        if (!task) {
          task = observerTask(payload);
          tasks.push(task);
        } else if (!isFreshEvent(task, payload)) {
          return true;
        }
        if (remoteUploadId) task.uploadId = remoteUploadId;
        if (remoteClientRequestId) task.clientRequestId = remoteClientRequestId;
        applyServerState(task, payload);
        applyRemoteStatus(task, payload);
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
      listener(snapshot());
      return () => listeners.delete(listener);
    },
    getSnapshot: snapshot,
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
