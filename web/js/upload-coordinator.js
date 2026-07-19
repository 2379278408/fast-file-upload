import { MAX_ACTIVE_UPLOADS, MAX_UPLOAD_SIZE_BYTES, UPLOAD_CHUNK_SIZE_BYTES, UPLOAD_ETA_MIN_SAMPLE_MS, UPLOAD_RETRY_DELAYS, UPLOAD_SPEED_WINDOW_MS } from './config.js';

const IDENTITY_SAMPLE_BYTES = 64 * 1024;
const IDENTITY_VERSION = 'sample-identity-v1';

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

function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.keys(value).forEach(key => deepFreeze(value[key]));
  return Object.freeze(value);
}

function publicTask(task) {
  return deepFreeze({
    uploadId: task.uploadId,
    clientRequestId: task.clientRequestId,
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
}) {
  const tasks = [];
  const listeners = new Set();
  let destroyed = false;
  let pumping = false;
  let generation = 0;
  let persistenceClosed = false;

  const snapshot = () => Object.freeze(tasks.map(publicTask));
  const isCurrent = token => !destroyed && token === generation;
  const notify = (token = generation) => {
    if (!isCurrent(token)) return;
    const value = snapshot();
    for (const listener of listeners) {
      if (!isCurrent(token)) break;
      listener(value);
    }
  };
  const persist = (task, token = generation) => {
    if (!isCurrent(token)) return Promise.resolve();
    return persistence.put(persistedTask(task)).catch(() => {});
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
  const findTask = uploadId => tasks.find(task => task.uploadId === uploadId || task.clientRequestId === uploadId);
  const nextMissingPart = task => {
    const count = Math.ceil(task.sizeBytes / chunkSize);
    for (let index = 0; index < count; index += 1) {
      if (!task.confirmedParts.includes(index)) return index;
    }
    return null;
  };

  async function hashPart(blob, token) {
    const bytes = await blob.arrayBuffer();
    if (!isCurrent(token)) return null;
    const digest = await cryptoObject.subtle.digest('SHA-256', bytes);
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
        await api.completeUpload(task.uploadId);
        if (!isCurrent(token)) return;
        task.status = 'completed';
        task.progressPercent = 100;
        task.etaSeconds = 0;
        await persistence.remove(task.uploadId);
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
        const response = await api.uploadPart(
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
        );
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
        await delay(UPLOAD_RETRY_DELAYS[attempt]);
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
          task.worker = uploadOne(task, token).finally(() => {
            if (!isCurrent(token)) return;
            task.worker = null;
            pump();
          });
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
      task.identity = await sampleFileIdentity(task.file, cryptoObject);
      if (!isCurrent(token) || task.status === 'cancelled') return;
      const response = await api.createUploadSession({
        clientRequestId: task.clientRequestId,
        name: task.name,
        sizeBytes: task.sizeBytes,
        mimeType: task.mimeType,
        lastModified: task.identity.lastModified,
        chunkSize,
        sampleSha256: task.identity.sampleSha256,
      });
      if (!isCurrent(token)) return;
      task.uploadId = response.upload_id || response.uploadId || task.uploadId;
      task.sessionReady = true;
      task.sourceDeviceId = response.source_device_id || response.sourceDeviceId || null;
      applyServerState(task, response, { authoritative: true });
      if (task.status === 'cancelled') {
        await api.cancelUpload(task.uploadId).catch(() => {});
        if (!isCurrent(token)) return;
        await persistence.remove(task.uploadId).catch(() => {});
        if (!isCurrent(token)) return;
        return;
      }
      task.status = task.status === 'paused' || response.status === 'paused' ? 'paused' : 'queued';
      if (task.status === 'paused') await api.controlUpload(task.uploadId, 'pause').catch(() => {});
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

  async function restore(record, token) {
    if (!isCurrent(token)) return;
    const task = {
      ...record, file: null, fileHandle: record.fileHandle || null,
      identity: record.identity || null, confirmedParts: record.confirmedParts || [],
      confirmedBytes: record.confirmedBytes || 0, inFlightBytes: 0,
      progressPercent: record.sizeBytes ? (record.confirmedBytes || 0) * 100 / record.sizeBytes : 0,
      speedBytesPerSecond: 0, etaSeconds: null, samples: [], controller: null, worker: null,
      isSourceDevice: Boolean(record.isSourceDevice),
      sessionReady: true,
    };
    if (task.fileHandle && task.isSourceDevice) {
      try {
        const file = await task.fileHandle.getFile();
        if (!isCurrent(token)) return;
        const matches = await matchesFileIdentity(file, task.identity, cryptoObject);
        if (!isCurrent(token)) return;
        if (matches) task.file = file;
      } catch {}
    }
    if (!task.file && task.isSourceDevice && !['completed', 'cancelled'].includes(task.status)) task.status = 'needs-file';
    if (isCurrent(token)) tasks.push(task);
  }

  const coordinator = {
    async start() {
      const token = generation;
      if (!isCurrent(token)) return snapshot();
      const records = await persistence.getAll();
      if (!isCurrent(token)) return snapshot();
      for (const record of records) {
        await restore(record, token);
        if (!isCurrent(token)) return snapshot();
      }
      await coordinator.reconcile();
      if (!isCurrent(token)) return snapshot();
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
      task.status = 'paused';
      task.inFlightBytes = 0;
      task.controller?.abort();
      persist(task);
      if (task.sessionReady) api.controlUpload(task.uploadId, 'pause').catch(() => {});
      notify();
    },
    resume(uploadId) {
      if (destroyed) return;
      const task = findTask(uploadId);
      if (!task || !task.file || !task.isSourceDevice) return;
      task.status = 'queued';
      task.errorCode = null;
      task.errorMessage = null;
      if (task.sessionReady) api.controlUpload(task.uploadId, 'resume').catch(() => {});
      persist(task);
      notify();
      pump();
    },
    cancel(uploadId) {
      if (destroyed) return;
      const task = findTask(uploadId);
      if (!task) return;
      task.status = 'cancelled';
      task.inFlightBytes = 0;
      task.controller?.abort();
      if (task.sessionReady) api.cancelUpload(task.uploadId).catch(() => {});
      persistence.remove(task.uploadId).catch(() => {});
      notify();
    },
    async retry(uploadId) {
      const token = generation;
      if (!isCurrent(token)) return;
      const task = findTask(uploadId);
      if (!task || !task.file || !task.isSourceDevice) return;
      try {
        const response = await api.getUploadSession(task.uploadId);
        if (!isCurrent(token)) return;
        if (task.worker || task.inFlightBytes || task.status === 'uploading') return;
        applyServerState(task, response, { authoritative: true });
        task.status = 'queued';
        task.errorCode = null;
        task.errorMessage = null;
        persist(task, token);
        notify(token);
        pump();
      } catch (error) {
        if (!isCurrent(token)) return;
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
      if (!isCurrent(token)) return snapshot();
      const remote = await api.listActiveUploads?.() || [];
      if (!isCurrent(token)) return snapshot();
      remote.forEach(event => {
        if (!isCurrent(token)) return;
        const data = event.upload || event.data || event;
        const task = findTask(data.upload_id || data.uploadId);
        if (!task) {
          coordinator.applyRemoteEvent(data);
          return;
        }
        if (!task.worker && !task.inFlightBytes && task.status !== 'uploading') {
          applyServerState(task, data, { authoritative: true });
          if (data.status && task.status !== 'paused') task.status = data.status;
          persist(task, token);
          notify(token);
        }
      });
      return snapshot();
    },
    applyRemoteEvent(event) {
      if (destroyed) return false;
      const data = event.upload || event.data || event;
      let task = findTask(data.upload_id || data.uploadId);
      if (!task) {
        task = {
          uploadId: data.upload_id || data.uploadId,
          clientRequestId: data.client_request_id || data.clientRequestId || null,
          file: null, fileHandle: null, identity: null,
          name: data.name, sizeBytes: data.size_bytes ?? data.sizeBytes ?? 0,
          mimeType: data.mime_type || data.mimeType || '', status: data.status || 'queued',
          confirmedParts: [], confirmedBytes: 0, inFlightBytes: 0, progressPercent: 0,
          speedBytesPerSecond: 0, etaSeconds: null,
          sourceDeviceId: data.source_device_id || data.sourceDeviceId || null,
          isSourceDevice: Boolean(data.is_source_device ?? data.isSourceDevice),
          errorCode: null, errorMessage: null,
          createdAt: data.created_at || data.createdAt || now(), samples: [], controller: null,
          worker: null, sessionReady: true,
          serverSequence: null, serverUpdatedAt: null, serverVersion: null,
        };
        tasks.push(task);
      } else if (!isFreshEvent(task, data)) {
        return false;
      }
      applyServerState(task, data);
      if (data.status && task.status !== 'paused') task.status = data.status;
      notify();
      return true;
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
      tasks.forEach(task => task.controller?.abort());
      listeners.clear();
      if (!persistenceClosed) {
        persistenceClosed = true;
        persistence.close();
      }
    },
  };
  return coordinator;
}
