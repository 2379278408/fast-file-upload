export class ApiError extends Error {
  constructor(status, body) {
    super(`API error ${status}`);
    this.status = status;
    this.body = body;
  }
}

const reconnectDelays = [1000, 2000, 4000, 8000, 16000, 30000];
const LAST_SEQ_KEY = 'transfer-last-sequence';

export function connectEvents({ after, onEvent, onStatus }) {
  let attempt = 0, stopped = false, socket, reconnectTimer = null;
  const clearReconnectTimer = () => {
    if (reconnectTimer === null) return;
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  };
  const open = () => {
    if (stopped) return;
    clearReconnectTimer();
    onStatus(attempt ? 'reconnecting' : 'connecting');
    const seq = after();
    socket = new WebSocket(`${location.origin.replace(/^http/, 'ws')}/api/events?after=${seq}`);
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
        try { localStorage.setItem(LAST_SEQ_KEY, String(event.sequence)); } catch {}
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
      const delay = reconnectDelays[Math.min(attempt++, reconnectDelays.length - 1)];
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
  try { return Number(localStorage.getItem(LAST_SEQ_KEY) || 0) || 0; } catch { return 0; }
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

export function uploadFile(file, clientRequestId, onProgress, signal) {
  return new Promise((resolve, reject) => {
    const abortError = () => {
      const error = new Error('Upload aborted');
      error.name = 'AbortError';
      return error;
    };
    const formData = new FormData();
    formData.append('file', file);
    formData.append('client_request_id', clientRequestId);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    xhr.withCredentials = true;

    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      if (onProgress) onProgress(event.loaded, event.total);
    };

    xhr.onload = () => {
      if (xhr.status === 200) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch {
          resolve(null);
        }
      } else if (xhr.status === 401) {
        window.dispatchEvent(new CustomEvent('session-expired', { detail: { path: '/api/upload' } }));
        reject(new ApiError(401, { detail: 'Session expired' }));
      } else {
        let body;
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          body = { detail: 'Upload failed' };
        }
        reject(new ApiError(xhr.status, body));
      }
    };

    xhr.onerror = () => reject(new Error('网络错误'));
    xhr.onabort = () => reject(abortError());
    if (signal) {
      if (signal.aborted) {
        reject(abortError());
        xhr.abort();
        return;
      }
      signal.addEventListener('abort', () => xhr.abort(), { once: true });
    }
    xhr.send(formData);
  });
}
