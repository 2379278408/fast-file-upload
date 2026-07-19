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

function publicTask(task) {
  return Object.freeze({
    uploadId: task.uploadId,
    clientRequestId: task.clientRequestId,
    file: task.file,
    fileHandle: task.fileHandle,
    identity: task.identity ? Object.freeze({ ...task.identity }) : null,
    name: task.name,
    sizeBytes: task.sizeBytes,
    mimeType: task.mimeType,
    status: task.status,
    confirmedParts: Object.freeze([...task.confirmedParts]),
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

  const snapshot = () => Object.freeze(tasks.map(publicTask));
  const notify = () => {
    const value = snapshot();
    listeners.forEach(listener => listener(value));
  };
  const persist = task => persistence.put(publicTask(task)).catch(() => {});
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
  const applyServerState = (task, response) => {
    task.confirmedParts = serverParts(response, task.confirmedParts);
    task.confirmedBytes = response && (response.confirmed_bytes ?? response.confirmedBytes) !== undefined
      ? (response.confirmed_bytes ?? response.confirmedBytes)
      : Math.min(task.sizeBytes, task.confirmedParts.length * chunkSize);
    task.inFlightBytes = 0;
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

  async function hashPart(blob) {
    return toHex(await cryptoObject.subtle.digest('SHA-256', await blob.arrayBuffer()));
  }

  async function uploadOne(task) {
    if (task.status !== 'queued' || !task.file || !task.isSourceDevice) return;
    const partIndex = nextMissingPart(task);
    if (partIndex === null) {
      task.status = 'completing';
      notify();
      try {
        await api.completeUpload(task.uploadId);
        task.status = 'completed';
        task.progressPercent = 100;
        task.etaSeconds = 0;
        await persistence.remove(task.uploadId);
      } catch (error) {
        task.status = 'failed';
        task.errorCode = 'complete-failed';
        task.errorMessage = error.message;
        persist(task);
      }
      notify();
      return;
    }

    task.status = 'uploading';
    task.controller = abortControllerFactory();
    if (!task.samples.length) task.samples = [{ time: now(), bytes: task.confirmedBytes }];
    const start = partIndex * chunkSize;
    const endExclusive = Math.min(task.sizeBytes, start + chunkSize);
    const blob = task.file.slice(start, endExclusive);
    let attempt = 0;
    notify();
    while (task.status === 'uploading') {
      try {
        const sha256 = await hashPart(blob);
        if (task.status !== 'uploading') break;
        const response = await api.uploadPart(
          task.uploadId,
          partIndex,
          blob,
          { start, end: endExclusive - 1, total: task.sizeBytes, sha256 },
          loaded => {
            if (task.status !== 'uploading') return;
            task.inFlightBytes = Math.min(blob.size ?? endExclusive - start, loaded);
            updateProgress(task);
            notify();
          },
          task.controller.signal,
        );
        applyServerState(task, response);
        if (task.status === 'uploading') task.status = 'queued';
        task.errorCode = null;
        task.errorMessage = null;
        persist(task);
        notify();
        break;
      } catch (error) {
        task.inFlightBytes = 0;
        if (task.status === 'paused' || task.status === 'cancelled' || error.name === 'AbortError') break;
        if (attempt >= UPLOAD_RETRY_DELAYS.length) {
          task.status = 'failed';
          task.errorCode = 'upload-failed';
          task.errorMessage = error.message;
          persist(task);
          notify();
          break;
        }
        await delay(UPLOAD_RETRY_DELAYS[attempt]);
        attempt += 1;
      }
    }
    task.controller = null;
  }

  function pump() {
    if (destroyed || pumping) return;
    pumping = true;
    Promise.resolve().then(async () => {
      while (!destroyed) {
        const active = tasks.filter(task => task.worker).length;
        const available = maxActive - active;
        if (available <= 0) break;
        const ready = tasks.filter(task => task.status === 'queued' && task.file
          && task.isSourceDevice && !task.worker).slice(0, available);
        if (!ready.length) break;
        ready.forEach(task => {
          task.worker = uploadOne(task).finally(() => {
            task.worker = null;
            pump();
          });
        });
        await Promise.resolve();
      }
      pumping = false;
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
    };
  }

  async function prepare(task) {
    if (task.sizeBytes > MAX_UPLOAD_SIZE_BYTES) {
      task.status = 'failed';
      task.errorCode = 'file-too-large';
      task.errorMessage = '文件大小超过 512 MiB 限制';
      notify();
      return;
    }
    try {
      task.identity = await sampleFileIdentity(task.file, cryptoObject);
      if (task.status === 'cancelled') return;
      const response = await api.createUploadSession({
        clientRequestId: task.clientRequestId,
        name: task.name,
        sizeBytes: task.sizeBytes,
        mimeType: task.mimeType,
        lastModified: task.identity.lastModified,
        chunkSize,
        sampleSha256: task.identity.sampleSha256,
      });
      task.uploadId = response.upload_id || response.uploadId || task.uploadId;
      task.sessionReady = true;
      task.sourceDeviceId = response.source_device_id || response.sourceDeviceId || null;
      applyServerState(task, response);
      if (task.status === 'cancelled') {
        await api.cancelUpload(task.uploadId).catch(() => {});
        await persistence.remove(task.uploadId).catch(() => {});
        return;
      }
      task.status = task.status === 'paused' || response.status === 'paused' ? 'paused' : 'queued';
      if (task.status === 'paused') await api.controlUpload(task.uploadId, 'pause').catch(() => {});
      await persist(task);
    } catch (error) {
      task.status = 'failed';
      task.errorCode = 'session-create-failed';
      task.errorMessage = error.message;
    }
    notify();
    pump();
  }

  async function restore(record) {
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
        if (await matchesFileIdentity(file, task.identity, cryptoObject)) task.file = file;
      } catch {}
    }
    if (!task.file && task.isSourceDevice && !['completed', 'cancelled'].includes(task.status)) task.status = 'needs-file';
    tasks.push(task);
  }

  const coordinator = {
    async start() {
      const records = await persistence.getAll();
      for (const record of records) await restore(record);
      await coordinator.reconcile();
      pump();
      notify();
      return snapshot();
    },
    enqueueFiles(files) {
      Array.from(files).forEach(item => {
        const file = item.file || item;
        const task = newTask(file, item.fileHandle);
        tasks.push(task);
        prepare(task);
      });
      notify();
      return snapshot();
    },
    pause(uploadId) {
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
      const task = findTask(uploadId);
      if (!task || !task.file || !task.isSourceDevice) return;
      try {
        applyServerState(task, await api.getUploadSession(task.uploadId));
        task.status = 'queued';
        task.errorCode = null;
        task.errorMessage = null;
        persist(task);
        notify();
        pump();
      } catch (error) {
        task.errorMessage = error.message;
        notify();
      }
    },
    prioritize(uploadId) {
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
      const remote = await api.listActiveUploads?.() || [];
      remote.forEach(event => coordinator.applyRemoteEvent(event));
      return snapshot();
    },
    applyRemoteEvent(event) {
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
        };
        tasks.push(task);
      }
      applyServerState(task, data);
      if (data.status && task.status !== 'paused') task.status = data.status;
      notify();
      return true;
    },
    subscribe(listener) {
      listeners.add(listener);
      listener(snapshot());
      return () => listeners.delete(listener);
    },
    getSnapshot: snapshot,
    destroy() {
      destroyed = true;
      tasks.forEach(task => task.controller?.abort());
      listeners.clear();
      persistence.close();
    },
  };
  return coordinator;
}
