import { RECONNECT_DELAYS } from './config.js';

export class ApiError extends Error {
  constructor(status, body) {
    super(`API error ${status}`);
    this.status = status;
    this.body = body;
  }
}
const LAST_SEQ_KEY = 'transfer-last-sequence';
const LEGACY_CURSOR_IGNORED_KEY = 'transfer-local-cursor-ignored-v1';

function normalizeSequence(value) {
  const sequence = Number(value);
  return Number.isSafeInteger(sequence) && sequence >= 0 ? sequence : 0;
}

function ignoreLegacyCursorOnce() {
  try {
    if (sessionStorage.getItem(LEGACY_CURSOR_IGNORED_KEY)) return;
    localStorage.getItem(LAST_SEQ_KEY);
    sessionStorage.setItem(LEGACY_CURSOR_IGNORED_KEY, '1');
  } catch {}
}

function storeLastSequence(sequence) {
  try { sessionStorage.setItem(LAST_SEQ_KEY, String(sequence)); } catch {}
}

export function connectEvents({ after, onEvent, onStatus }) {
  let attempt = 0, stopped = false, socket, reconnectTimer = null;
  let lastAppliedSequence = normalizeSequence(after());
  const clearReconnectTimer = () => {
    if (reconnectTimer === null) return;
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  };
  const open = () => {
    if (stopped) return;
    clearReconnectTimer();
    onStatus(attempt ? 'reconnecting' : 'connecting');
    socket = new WebSocket(`${location.origin.replace(/^http/, 'ws')}/api/events?after=${lastAppliedSequence}`);
    socket.onopen = () => {
      if (stopped) return;
      clearReconnectTimer();
      attempt = 0;
      onStatus('connected');
    };
    socket.onmessage = message => {
      const event = JSON.parse(message.data);
      const applied = onEvent(event);
      if (applied === true && event.sequence !== undefined) {
        const sequence = normalizeSequence(event.sequence);
        if (sequence > lastAppliedSequence) {
          lastAppliedSequence = sequence;
          storeLastSequence(sequence);
        }
      }
    };
    socket.onclose = event => {
      if (stopped) return;
      if (event.code === 4401) {
        clearReconnectTimer();
        return onStatus('closed');
      }
      onStatus('reconnecting');
      clearReconnectTimer();
      const delay = RECONNECT_DELAYS[Math.min(attempt++, RECONNECT_DELAYS.length - 1)];
      const timer = window.setTimeout(() => {
        if (reconnectTimer !== timer) return;
        reconnectTimer = null;
        open();
      }, delay);
      reconnectTimer = timer;
    };
  };
  open();
  return {
    close() {
      stopped = true;
      clearReconnectTimer();
      socket?.close();
    },
  };
}

export function getLastSequence() {
  ignoreLegacyCursorOnce();
  try { return normalizeSequence(sessionStorage.getItem(LAST_SEQ_KEY)); } catch { return 0; }
}

export async function request(path, options = {}) {
  const { responseType, ...fetchOptions } = options;
  const response = await fetch(path, { credentials: 'same-origin', ...fetchOptions });
  if (response.status === 401) {
    window.dispatchEvent(new CustomEvent('session-expired', { detail: { path } }));
    throw new ApiError(401, { detail: 'Session expired' });
  }
  if (!response.ok) {
    let body;
    try {
      body = await response.json();
    } catch {
      body = { detail: 'Request failed' };
    }
    throw new ApiError(response.status, body);
  }
  if (response.status === 204) return null;
  if (responseType === 'blob') return response.blob();
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) return response.json();
  return response.text();
}

export async function getSession() {
  return request('/api/session');
}

export async function unlock(accessToken, deviceId, deviceName) {
  return request('/api/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      access_token: accessToken,
      device_id: deviceId,
      device_name: deviceName,
    }),
  });
}

export async function logout() {
  const result = await request('/api/session', { method: 'DELETE' });
  window.dispatchEvent(new CustomEvent('session-expired', { detail: { reason: 'logout' } }));
  return result;
}

export async function sendText(body, clientRequestId) {
  return request('/api/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ body, client_request_id: clientRequestId }),
  });
}

function xhrJson({ method, path, body, headers = {}, onProgress, signal }) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let abortHandler = null;
    const abortError = () => {
      const error = new Error('Upload aborted');
      error.name = 'AbortError';
      return error;
    };
    const settle = (callback, value) => {
      if (settled) return;
      settled = true;
      if (signal && abortHandler && typeof signal.removeEventListener === 'function') {
        signal.removeEventListener('abort', abortHandler);
      }
      callback(value);
    };
    const resolveOnce = value => settle(resolve, value);
    const rejectOnce = error => settle(reject, error);
    const xhr = new XMLHttpRequest();
    xhr.open(method, path);
    xhr.withCredentials = true;
    Object.entries(headers).forEach(([name, value]) => xhr.setRequestHeader(name, value));

    xhr.upload.onprogress = (event) => {
      if (settled || !event.lengthComputable) return;
      if (onProgress) onProgress(event.loaded, event.total);
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolveOnce(JSON.parse(xhr.responseText));
        } catch {
          resolveOnce(null);
        }
      } else if (xhr.status === 401) {
        window.dispatchEvent(new CustomEvent('session-expired', { detail: { path } }));
        rejectOnce(new ApiError(401, { detail: 'Session expired' }));
      } else {
        let body;
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          body = { detail: 'Upload failed' };
        }
        rejectOnce(new ApiError(xhr.status, body));
      }
    };

    xhr.onerror = () => rejectOnce(new Error('网络错误'));
    xhr.onabort = () => rejectOnce(abortError());
    if (signal) {
      if (signal.aborted) {
        rejectOnce(abortError());
        xhr.abort();
        return;
      }
      abortHandler = () => xhr.abort();
      signal.addEventListener('abort', abortHandler, { once: true });
    }
    try {
      xhr.send(body);
    } catch (error) {
      rejectOnce(error);
    }
  });
}

export function uploadFile(file, clientRequestId, onProgress, signal) {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('client_request_id', clientRequestId);
  return xhrJson({ method: 'POST', path: '/api/upload', body: formData, onProgress, signal });
}

export function createUploadSession(metadata) {
  const body = {
    client_request_id: metadata.clientRequestId ?? metadata.client_request_id,
    name: metadata.name,
    size_bytes: metadata.sizeBytes ?? metadata.size_bytes,
    mime_type: metadata.mimeType ?? metadata.mime_type,
    last_modified_ms: metadata.lastModified ?? metadata.last_modified_ms,
    chunk_size_bytes: metadata.chunkSize ?? metadata.chunk_size_bytes,
    sample_sha256: metadata.sampleSha256 ?? metadata.sample_sha256,
  };
  return request('/api/uploads', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function listActiveUploads() {
  return request('/api/uploads/active');
}

export function getUploadSession(uploadId) {
  return request(`/api/uploads/${encodeURIComponent(uploadId)}`);
}

export function uploadPart(uploadId, partIndex, blob, metadata, onProgress, signal) {
  return xhrJson({
    method: 'PUT',
    path: `/api/uploads/${encodeURIComponent(uploadId)}/parts/${partIndex}`,
    body: blob,
    headers: {
      'Content-Type': 'application/octet-stream',
      'Content-Range': `bytes ${metadata.start}-${metadata.end}/${metadata.total}`,
      'X-Chunk-SHA256': metadata.sha256,
    },
    onProgress,
    signal,
  });
}

export function controlUpload(uploadId, action) {
  return request(`/api/uploads/${encodeURIComponent(uploadId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  });
}

export function cancelUpload(uploadId) {
  return request(`/api/uploads/${encodeURIComponent(uploadId)}`, { method: 'DELETE' });
}

export function completeUpload(uploadId) {
  return request(`/api/uploads/${encodeURIComponent(uploadId)}/complete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
}
