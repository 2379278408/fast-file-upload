import { request as apiRequest, unlock as apiUnlock, logout as apiLogout, getSession, ApiError, connectEvents, getLastSequence } from './api.js';
import * as resumableApi from './api.js';
import { createTimeline } from './timeline.js';
import { createComposer } from './composer.js';
import { createUploadCoordinator } from './upload-coordinator.js';
import { createUploadPersistence } from './upload-persistence.js';
import { createLibrary } from './library.js';
import { createNavigation } from './navigation.js';
import { TOAST_DURATION_MS } from './config.js';

const DEVICE_ID_KEY = 'device-id';
const DEVICE_NAME_KEY = 'device-name';
const APP_INSTANCE_KEY = '__personalTransferAppInstance';

window[APP_INSTANCE_KEY]?.destroy?.();

const appListenerCleanups = [];
let appDestroyed = false;
let resumePromise = null;

function listen(target, type, listener, options) {
  if (!target?.addEventListener) return;
  target.addEventListener(type, listener, options);
  appListenerCleanups.push(() => target.removeEventListener(type, listener, options));
}

export function createLiveRegionAnnouncer(region) {
  let sequence = 0;
  return message => {
    if (!region || !message) return false;
    const announcement = document.createElement('span');
    announcement.dataset.announcementSequence = String(++sequence);
    announcement.textContent = message;
    region.replaceChildren(announcement);
    return true;
  };
}

export function captureTimelinePosition(container) {
  if (!container) return null;
  const scrollTop = container.scrollTop;
  const containerRect = container.getBoundingClientRect();
  const messages = Array.from(container.querySelectorAll('.timeline-message'));
  const anchor = messages.find(message => {
    const rect = message.getBoundingClientRect();
    return rect.bottom > containerRect.top && rect.top < containerRect.bottom;
  }) || messages[messages.length - 1] || null;
  const anchorRect = anchor ? anchor.getBoundingClientRect() : null;
  return {
    scrollTop,
    messageId: anchor ? anchor.dataset.messageId : null,
    anchorOffset: anchorRect ? anchorRect.top - containerRect.top : null,
  };
}

export async function restoreTimelinePosition(container, snapshot, timeline) {
  if (!container || !snapshot) return false;
  let anchor = snapshot.messageId
    ? container.querySelector(`[data-message-id="${snapshot.messageId}"]`)
    : null;
  if (!anchor && snapshot.messageId && timeline) {
    try {
      if (typeof timeline.ensureMessageLoaded === 'function') {
        await timeline.ensureMessageLoaded(snapshot.messageId);
      } else if (typeof timeline.loadUntil === 'function') {
        await timeline.loadUntil(snapshot.messageId, { focus: false });
      }
    } catch {
      // The saved scrollTop remains the recovery path for paging failures.
    }
    anchor = container.querySelector(`[data-message-id="${snapshot.messageId}"]`);
  }
  if (anchor && Number.isFinite(snapshot.anchorOffset)) {
    const containerTop = container.getBoundingClientRect().top;
    const currentOffset = anchor.getBoundingClientRect().top - containerTop;
    container.scrollTop += currentOffset - snapshot.anchorOffset;
    return true;
  }
  container.scrollTop = snapshot.scrollTop;
  return false;
}

function getOrCreateDeviceId() {
  let id = localStorage.getItem(DEVICE_ID_KEY);
  if (!id) {
    id = generateUUID();
    localStorage.setItem(DEVICE_ID_KEY, id);
  }
  return id;
}

function generateUUID() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

function getDeviceName() {
  return localStorage.getItem(DEVICE_NAME_KEY) || '';
}

function setDeviceName(name) {
  localStorage.setItem(DEVICE_NAME_KEY, name);
  if (routeSource) routeSource.textContent = name || '本机';
  if (connectionDevice) connectionDevice.textContent = name || '-';
}

const sessionExpiredOverlay = document.getElementById('sessionExpired');
const unlockForm = document.getElementById('unlockForm');
const accessTokenInput = document.getElementById('accessToken');
const deviceNameInput = document.getElementById('deviceName');
const unlockError = document.querySelector('.unlock-error');
const unlockSubmit = document.getElementById('unlockSubmit');
const mainContent = document.getElementById('mainContent');
const libraryView = document.getElementById('libraryView');
const skipLink = document.getElementById('skipLink');

let savedTimelinePosition = null;
let previouslyFocusedElement = null;
let previousFocusTarget = null;
let outsideDialogInertStates = null;
let isUnlocked = false;

function describeFocusTarget(element) {
  if (!element) return null;
  if (element.id) return { type: 'id', id: element.id };
  const message = typeof element.closest === 'function'
    ? element.closest('.timeline-message')
    : null;
  if (message && message.dataset.messageId) {
    return {
      type: 'timeline-action',
      messageId: message.dataset.messageId,
      action: element.dataset.timelineAction || null,
    };
  }
  if (element.dataset && element.dataset.messageId && element.dataset.fileAction) {
    return {
      type: 'library-action',
      messageId: element.dataset.messageId,
      action: element.dataset.fileAction,
    };
  }
  return null;
}

function findEquivalentFocusTarget(target) {
  if (!target) return null;
  if (target.type === 'id') return document.getElementById(target.id);
  if (target.type === 'timeline-action' && timelineContainer) {
    const message = timelineContainer.querySelector(
      `[data-message-id="${target.messageId}"]`
    );
    if (!message) return timelineContainer;
    if (!target.action) return timelineContainer;
    return message.querySelector(`[data-timeline-action="${target.action}"]`) || timelineContainer;
  }
  if (target.type === 'library-action' && libraryView) {
    const candidates = Array.from(
      libraryView.querySelectorAll(`[data-message-id="${target.messageId}"]`)
    );
    return candidates.find(element => element.dataset.fileAction === target.action) || libraryView;
  }
  return null;
}

function restorePreviousFocus() {
  const equivalent = findEquivalentFocusTarget(previousFocusTarget);
  if (equivalent && typeof equivalent.focus === 'function') {
    equivalent.focus();
    if (document.activeElement === equivalent) {
      previouslyFocusedElement = null;
      previousFocusTarget = null;
      return;
    }
  }
  if (
    previouslyFocusedElement
    && previouslyFocusedElement.isConnected
    && typeof previouslyFocusedElement.focus === 'function'
  ) {
    previouslyFocusedElement.focus();
    if (document.activeElement === previouslyFocusedElement) {
      previouslyFocusedElement = null;
      previousFocusTarget = null;
      return;
    }
  }
  if (mainContent && typeof mainContent.focus === 'function') {
    mainContent.focus();
  }
  previouslyFocusedElement = null;
  previousFocusTarget = null;
}

function lockOutsideDialog() {
  if (outsideDialogInertStates || !document.body) return;
  outsideDialogInertStates = new Map();
  for (const element of Array.from(document.body.children)) {
    if (element === sessionExpiredOverlay) continue;
    outsideDialogInertStates.set(element, element.inert);
    element.inert = true;
  }
}

function unlockOutsideDialog() {
  if (!outsideDialogInertStates) return;
  for (const [element, wasInert] of outsideDialogInertStates) {
    element.inert = wasInert;
  }
  outsideDialogInertStates = null;
}

function showLockOverlay() {
  if (appDestroyed) return;
  closeEventConnection();
  isUnlocked = false;
  if (!sessionExpiredOverlay) return;
  if (sessionExpiredOverlay.classList.contains('is-hidden')) {
    savedTimelinePosition = captureTimelinePosition(timelineContainer);
    previouslyFocusedElement = document.activeElement;
    previousFocusTarget = describeFocusTarget(previouslyFocusedElement);
  }
  lockOutsideDialog();
  sessionExpiredOverlay.classList.remove('is-hidden');
  if (deviceNameInput) {
    const stored = getDeviceName();
    if (stored) deviceNameInput.value = stored;
  }
  if (accessTokenInput) accessTokenInput.value = '';
  if (unlockError) unlockError.classList.remove('visible');
  if (accessTokenInput) accessTokenInput.focus();
}

function hideLockOverlay() {
  if (appDestroyed) return;
  if (!sessionExpiredOverlay) return;
  sessionExpiredOverlay.classList.add('is-hidden');
  unlockOutsideDialog();
  isUnlocked = true;
  restorePreviousFocus();
}

if (sessionExpiredOverlay) {
  listen(sessionExpiredOverlay, 'keydown', event => {
    if (event.key !== 'Tab') return;
    const focusable = [accessTokenInput, deviceNameInput, unlockSubmit]
      .filter(element => element && !element.disabled && !element.hidden);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
}

if (skipLink && mainContent) {
  listen(skipLink, 'click', event => {
    event.preventDefault();
    mainContent.focus();
  });
}

async function checkSession() {
  if (appDestroyed) return;
  try {
    await getSession();
    if (appDestroyed) return;
    isUnlocked = true;
    const storedName = getDeviceName();
    if (routeSource && storedName) routeSource.textContent = storedName;
    if (connectionDevice) connectionDevice.textContent = storedName || '-';
    await restoreUploads();
    if (appDestroyed) return;
    await refreshAll();
    if (appDestroyed) return;
    await timeline.loadInitial();
    if (appDestroyed) return;
    renderUploadSnapshots();
    hideLockOverlay();
    startEventConnection();
  } catch {
    if (!appDestroyed) showLockOverlay();
  }
}

if (unlockForm) {
  listen(unlockForm, 'submit', async (event) => {
    event.preventDefault();
    const token = (accessTokenInput ? accessTokenInput.value : '').trim();
    const name = (deviceNameInput ? deviceNameInput.value : '').trim();
    if (!token) {
      if (unlockError) {
        unlockError.textContent = '请输入访问令牌';
        unlockError.classList.add('visible');
      }
      return;
    }
    if (name.length < 1 || name.length > 40) {
      if (unlockError) {
        unlockError.textContent = '设备名称需要 1-40 个字符';
        unlockError.classList.add('visible');
      }
      return;
    }
    const deviceId = getOrCreateDeviceId();
    try {
      await apiUnlock(token, deviceId, name);
      if (appDestroyed) return;
      setDeviceName(name);
      await restoreUploads();
      if (appDestroyed) return;
      await refreshAll();
      if (appDestroyed) return;
      await timeline.loadInitial();
      if (appDestroyed) return;
      renderUploadSnapshots();
      hideLockOverlay();
      startEventConnection();
    } catch (err) {
      if (appDestroyed) return;
      const message = err instanceof ApiError ? (err.body && err.body.detail || '验证失败') : '网络错误';
      if (unlockError) {
        unlockError.textContent = message;
        unlockError.classList.add('visible');
      }
    }
  });
}

listen(window, 'session-expired', closeEventConnection);
listen(window, 'session-expired', showLockOverlay);
listen(window, 'session-logout', async () => {
  closeEventConnection();
  try { await apiLogout(); } finally { if (!appDestroyed) showLockOverlay(); }
});

const timelineContainer = document.getElementById('timelineContainer');
const newMessageButton = document.getElementById('newMessageButton');
const uploadLiveRegion = document.getElementById('uploadLiveRegion');
const announceUpload = createLiveRegionAnnouncer(uploadLiveRegion);
const uploadPersistence = createUploadPersistence({ indexedDB: window.indexedDB });
const uploadCoordinator = createUploadCoordinator({
  api: resumableApi,
  persistence: uploadPersistence,
  onAnnounce: announceUpload,
  onCompleted(message) {
    timeline.upsert?.(message);
  },
});
let uploadCoordinatorStarted = false;

async function restoreUploads({ throwOnError = false } = {}) {
  if (appDestroyed) return;
  try {
    if (!uploadCoordinatorStarted) {
      await uploadCoordinator.start();
      uploadCoordinatorStarted = true;
    } else {
      await uploadCoordinator.reconcile();
    }
    if (appDestroyed) return;
  } catch (error) {
    if (!appDestroyed) showToast(error.message || '上传任务恢复失败', 'error');
    if (throwOnError) throw error;
  }
}

function renderUploadSnapshots() {
  uploadCoordinator.getSnapshot().forEach(task => timeline.upsertUpload?.(task));
}

const timeline = createTimeline({
  container: timelineContainer,
  newMessageButton: newMessageButton,
  api: (url, options) => apiRequest(url, options),
  async onRestore() {
    if (appDestroyed) return;
    const snapshot = savedTimelinePosition;
    try {
      await restoreTimelinePosition(timelineContainer, snapshot, timeline);
    } finally {
      if (!appDestroyed && savedTimelinePosition === snapshot) savedTimelinePosition = null;
    }
  },
  async onUploadAction({ action, uploadId }) {
    if (action === 'reselect') {
      pendingReselectUploadId = uploadId;
      if (uploadReselectInput) {
        uploadReselectInput.value = '';
        uploadReselectInput.click();
      }
      return;
    }
    const handler = uploadCoordinator[action];
    if (typeof handler !== 'function') return;
    try {
      await handler.call(uploadCoordinator, uploadId);
    } catch (error) {
      if (!appDestroyed) showToast(error.message || '上传操作失败', 'error');
    }
  },
});

const composerForm = document.getElementById('composerForm');
const composerTextarea = document.getElementById('composerTextarea');
const composerFileInput = document.getElementById('composerFileInput');
const composerDropTarget = document.getElementById('composerDropTarget');
const transferPage = document.getElementById('transferPage');
const composerAttachBtn = document.getElementById('composerAttachBtn');
const routeSource = document.getElementById('routeSource');
const routeTarget = document.getElementById('routeTarget');

const composer = createComposer({
  form: composerForm,
  textarea: composerTextarea,
  fileInput: composerFileInput,
  dropTarget: transferPage || composerDropTarget,
  api: (url, opts) => apiRequest(url, opts),
  timeline,
  uploadCoordinator,
});

const uploadSummary = document.getElementById('uploadSummary');
const uploadSummaryText = document.getElementById('uploadSummaryText');
const pauseAllUploads = document.getElementById('pauseAllUploads');
const resumeAllUploads = document.getElementById('resumeAllUploads');
const cancelAllUploads = document.getElementById('cancelAllUploads');
const transferDropOverlay = document.getElementById('transferDropOverlay');
const uploadReselectInput = document.getElementById('uploadReselectInput');
let pendingReselectUploadId = null;

listen(uploadReselectInput, 'change', async () => {
  const uploadId = pendingReselectUploadId;
  const file = uploadReselectInput.files?.[0];
  pendingReselectUploadId = null;
  if (!uploadId || !file) return;
  try {
    await uploadCoordinator.reselect(uploadId, file);
  } catch (error) {
    if (!appDestroyed) showToast(error.message || '文件恢复失败', 'error');
  }
});

const unsubscribeUploadCoordinator = uploadCoordinator.subscribe((tasks, summary = {}) => {
  tasks.forEach(task => {
    timeline.upsertUpload?.(task);
  });
  const hasPendingControl = Boolean(summary.hasPendingControl);
  [pauseAllUploads, resumeAllUploads, cancelAllUploads].forEach(button => {
    if (!button) return;
    button.disabled = hasPendingControl;
    button.setAttribute('aria-disabled', String(hasPendingControl));
  });
  const activeStates = ['queued', 'uploading', 'paused', 'verifying', 'completing', 'failed', 'needs-file'];
  const activeTasks = tasks.filter(task => activeStates.includes(task.status));
  if (uploadSummary) uploadSummary.hidden = activeTasks.length === 0;
  if (uploadSummaryText) {
    const uploading = activeTasks.filter(task => task.status === 'uploading').length;
    const paused = activeTasks.filter(task => task.status === 'paused').length;
    uploadSummaryText.textContent = `${activeTasks.length} 个活动任务 · ${uploading} 个上传中 · ${paused} 个已暂停`;
  }
});
listen(pauseAllUploads, 'click', async () => {
  const summary = await uploadCoordinator.pauseAll();
  if (summary.failed && !appDestroyed) showToast(`${summary.failed} 个任务暂停失败`, 'error');
});
listen(resumeAllUploads, 'click', async () => {
  const summary = await uploadCoordinator.resumeAll();
  if (summary.failed && !appDestroyed) showToast(`${summary.failed} 个任务继续失败`, 'error');
});
listen(cancelAllUploads, 'click', async () => {
  const summary = await uploadCoordinator.cancelAll();
  if (summary.failed && !appDestroyed) showToast(`${summary.failed} 个任务取消失败`, 'error');
});

const library = createLibrary({
  root: document.getElementById('libraryView'),
  api: (url, options) => apiRequest(url, options),
  timeline,
  onAttach() {
    navigation.navigate('transfer');
    composerFileInput?.click();
  },
  async onLocate(messageId) {
    await navigation.navigate('transfer', { focus: false });
    timeline.focusMessage(messageId);
  },
});

if (composerAttachBtn && composerFileInput) {
  listen(composerAttachBtn, 'click', async () => {
    if (!await composer.pickFiles()) composerFileInput.click();
  });
}

if (transferPage && transferDropOverlay) {
  let dragDepth = 0;
  listen(transferPage, 'dragenter', event => {
    event.preventDefault();
    dragDepth += 1;
    transferDropOverlay.classList.add('is-visible');
    transferDropOverlay.setAttribute('aria-hidden', 'false');
  });
  listen(transferPage, 'dragover', event => {
    event.preventDefault();
  });
  listen(transferPage, 'dragleave', event => {
    event.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) {
      transferDropOverlay.classList.remove('is-visible');
      transferDropOverlay.setAttribute('aria-hidden', 'true');
    }
  });
  listen(transferPage, 'drop', event => {
      event.preventDefault();
      dragDepth = 0;
      transferDropOverlay.classList.remove('is-visible');
      transferDropOverlay.setAttribute('aria-hidden', 'true');
  });
}

const toastEl = document.getElementById('toast');
const metricCount = document.getElementById('metricCount');
const metricSize = document.getElementById('metricSize');
const metricLimit = document.getElementById('metricLimit');
const metricMode = document.getElementById('metricMode');
const modeState = document.getElementById('modeState');
const opsSummary = document.getElementById('opsSummary');
const largestFilesList = document.getElementById('largestFilesList');
const auditEventsList = document.getElementById('auditEventsList');

let healthState = null;
const refreshHealthBtn = document.getElementById('refreshHealthBtn');
const refreshOpsBtn = document.getElementById('refreshOpsBtn');

listen(refreshHealthBtn, 'click', loadHealth);
listen(refreshOpsBtn, 'click', loadOperations);

const railRefresh = document.getElementById('railRefresh');
const retryConnection = document.getElementById('retryConnection');
listen(railRefresh, 'click', () => { loadHealth(); startEventConnection(); });
listen(retryConnection, 'click', () => { startEventConnection(); });

async function loadHealth() {
  try {
    const nextHealthState = await apiRequest('/api/health');
    if (appDestroyed) return;
    healthState = nextHealthState;
    if (metricCount) metricCount.textContent = healthState.file_count ?? 0;
    if (metricSize) metricSize.textContent = healthState.total_size ?? '0 B';
    if (metricLimit) metricLimit.textContent = healthState.max_upload_size ?? '-';
    if (metricMode) metricMode.textContent = healthState.protected ? '受控' : '公开';
    if (modeState) modeState.textContent = healthState.protected ? '令牌访问' : '公开访问';
  } catch (error) {
    if (!appDestroyed) showToast('健康信息读取失败', 'error');
  }
}

async function loadFiles() {
  await library.load({});
}

async function loadOperations() {
  if (opsSummary) opsSummary.innerHTML = renderOpsLoading();
  if (largestFilesList) largestFilesList.textContent = '同步中...';
  if (auditEventsList) auditEventsList.textContent = '同步中...';
  try {
    const [summary, audit] = await Promise.all([
      apiRequest('/api/admin/summary'),
      apiRequest('/api/audit')
    ]);
    if (appDestroyed) return;
    renderOperations(summary, audit.events || []);
  } catch (error) {
    if (!appDestroyed) renderOpsError('运营视图读取失败，请检查服务状态。');
  }
}

function renderOpsLoading() {
  return ['总占用', '文件数', '旧文件', '大文件'].map((label) => (
    `<div class="mini-kpi"><span>${label}</span><strong>-</strong></div>`
  )).join('');
}

function renderOpsError(message) {
  if (opsSummary) opsSummary.innerHTML = `<div class="mini-kpi"><span>状态</span><strong>读取失败</strong></div>`;
  if (largestFilesList) largestFilesList.innerHTML = `<span class="ops-meta">${escapeHtml(message)}</span>`;
  if (auditEventsList) auditEventsList.innerHTML = `<span class="ops-meta">${escapeHtml(message)}</span>`;
}

function renderOperations(summary, events) {
  if (opsSummary) {
    opsSummary.innerHTML = `
      <div class="mini-kpi"><span>总占用</span><strong>${escapeHtml(summary.total_size || '0 B')}</strong></div>
      <div class="mini-kpi"><span>文件数</span><strong>${Number(summary.file_count || 0)}</strong></div>
      <div class="mini-kpi"><span>旧文件</span><strong>${Number(summary.stale_file_count || 0)}</strong></div>
      <div class="mini-kpi"><span>大文件</span><strong>${Number(summary.large_file_count || 0)}</strong></div>
    `;
  }
  if (largestFilesList) largestFilesList.innerHTML = renderLargestFiles(summary.largest_files || []);
  if (auditEventsList) auditEventsList.innerHTML = renderAuditEvents(events.slice(-6).reverse());
}

function renderLargestFiles(items) {
  if (!items.length) return '<div class="ops-meta">暂无文件。</div>';
  return items.map((file) => `
    <div class="ops-row">
      <div class="ops-main">
        <strong title="${escapeAttr(file.name)}">${escapeHtml(file.name)}</strong>
        <span class="ops-meta">sha256 ${escapeHtml(shortHash(file.sha256))}</span>
      </div>
      <span class="pill"><strong>${escapeHtml(file.size || formatBytes(file.size_bytes || 0))}</strong></span>
    </div>
  `).join('');
}

function renderAuditEvents(events) {
  if (!events.length) return '<div class="ops-meta">暂无审计事件。</div>';
  return events.map((event) => `
    <div class="ops-row">
      <div class="ops-main">
        <strong>${escapeHtml(event.action || 'event')} · ${escapeHtml(event.name || '-')}</strong>
        <span class="ops-meta">${escapeHtml(formatAuditTime(event.time))}</span>
      </div>
      <span class="pill"><strong>${escapeHtml(formatBytes(Number(event.size_bytes || 0)))}</strong></span>
    </div>
  `).join('');
}

function shortHash(value) {
  return value ? `${value.slice(0, 10)}...` : '-';
}

function parseError(text, fallback) {
  try {
    const payload = JSON.parse(text);
    return payload.detail || fallback;
  } catch (error) {
    return fallback;
  }
}

function showToast(message, type) {
  if (!toastEl) return;
  toastEl.textContent = message;
  toastEl.className = `toast ${type} show`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toastEl.classList.remove('show'), TOAST_DURATION_MS);
}

function formatBytes(size) {
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = size;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

function formatAuditTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}

const _escDiv = document.createElement('div');
function escapeHtml(value) {
  _escDiv.textContent = value || '';
  return _escDiv.innerHTML;
}

function escapeAttr(value) {
  return String(value || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function refreshAll() {
  await Promise.all([loadHealth(), loadFiles(), loadOperations()]);
}

const connectionStatusEl = document.getElementById('connectionStatus');
const connectionTitle = document.getElementById('connectionTitle');
const connectionDetail = document.getElementById('connectionDetail');
const connectionDevice = document.getElementById('connectionDevice');
const connectionEvents = document.getElementById('connectionEvents');
const connectionError = document.getElementById('connectionError');
const connectionPanel = document.getElementById('connectionPanel');
const latencyValue = document.getElementById('latencyValue');
let eventConnection = null;

function closeEventConnection() {
  if (eventConnection) eventConnection.close();
  eventConnection = null;
}

function updateConnectionStatus(status) {
  if (appDestroyed || !connectionStatusEl) return;
  const labels = {
    connecting: '连接中…',
    connected: '已连接',
    reconnecting: '重新连接中…',
    closed: '连接已关闭',
  };
  connectionStatusEl.textContent = labels[status] || status;

  // Update connection panel
  if (connectionTitle) {
    const panelLabels = {
      connecting: '正在连接…',
      connected: '安全连接已建立',
      reconnecting: '重新连接中…',
      closed: '连接已断开',
    };
    connectionTitle.textContent = panelLabels[status] || status;
  }
  if (connectionPanel) {
    connectionPanel.dataset.connection = status === 'closed' ? 'offline' : 'online';
  }
  if (connectionEvents) {
    connectionEvents.textContent = status === 'connected' ? '已订阅' : '未订阅';
  }
  if (connectionError) {
    connectionError.hidden = status !== 'closed';
  }

  if (status === 'closed') {
    showLockOverlay();
  }
}

async function reconcileAuthoritativeSnapshot() {
  if (appDestroyed) throw new Error('Application is destroyed');
  await restoreUploads({ throwOnError: true });
  await Promise.all([
    timeline.loadInitial({ throwOnError: true }),
    library.reconcileAuthoritative(),
  ]);
  if (appDestroyed) throw new Error('Application is destroyed');
  renderUploadSnapshots();
}

async function applyIncomingEvent(event) {
  if (appDestroyed || !event) return false;
  if (event.event_type === 'resync_required') {
    await reconcileAuthoritativeSnapshot();
    return true;
  }
  if (event.event_type === 'ready') return true;
  if (event.event_type?.startsWith('upload.')) {
    return uploadCoordinator.applyRemoteEvent(event);
  }
  if (event.event_type?.startsWith('message.')) {
    const applied = timeline.mergeEvent(event);
    if (library && typeof library.applyEvent === 'function') library.applyEvent(event);
    return applied;
  }
  if (event.event_type?.startsWith('file.')) {
    const timelineApplied = timeline.mergeEvent(event);
    const libraryApplied = library && typeof library.applyEvent === 'function'
      ? library.applyEvent(event)
      : false;
    return event.event_type === 'file.finalized'
      ? timelineApplied === true && libraryApplied === true
      : libraryApplied === true;
  }
  return false;
}

function startEventConnection() {
  if (appDestroyed) return;
  closeEventConnection();
  eventConnection = connectEvents({
    after: () => getLastSequence(),
    onEvent: applyIncomingEvent,
    onStatus: updateConnectionStatus,
  });
}

listen(window, 'timeline-error', (event) => {
  showToast(event.detail?.message || '操作失败', 'error');
});

// Dark mode toggle
const themeToggle = document.getElementById('themeToggle');
const themeIconLight = document.getElementById('themeIconLight');
const themeIconDark = document.getElementById('themeIconDark');
const THEME_KEY = 'theme-preference';

function getThemePreference() {
  const stored = localStorage.getItem(THEME_KEY);
  if (stored) return stored;
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  } catch {
    return 'light';
  }
}

function applyTheme(theme) {
  try {
    document.documentElement.classList.toggle('dark', theme === 'dark');
    if (themeIconLight) themeIconLight.classList.toggle('is-hidden', theme === 'dark');
    if (themeIconDark) themeIconDark.classList.toggle('is-hidden', theme !== 'dark');
  } catch {
    // DOM not available (e.g. QuickJS test environment)
  }
}

try { applyTheme(getThemePreference()); } catch { /* noop */ }

if (themeToggle) {
  listen(themeToggle, 'click', () => {
    try {
      const current = document.documentElement.classList.contains('dark') ? 'dark' : 'light';
      const next = current === 'dark' ? 'light' : 'dark';
      localStorage.setItem(THEME_KEY, next);
      applyTheme(next);
    } catch { /* noop */ }
  });
}

listen(document.getElementById('manageThemeToggle'), 'click', () => themeToggle?.click());
listen(document.getElementById('logoutButton'), 'click', () => {
  window.dispatchEvent(new CustomEvent('session-logout'));
});

try {
  const colorScheme = window.matchMedia('(prefers-color-scheme: dark)');
  listen(colorScheme, 'change', event => {
    if (!localStorage.getItem(THEME_KEY)) {
      applyTheme(event.matches ? 'dark' : 'light');
    }
  });
} catch {
  // matchMedia not available (e.g. QuickJS test environment)
}

const navigation = createNavigation({
  windowObject: window,
  documentObject: document,
  onRouteChange(route) {
    if (route !== 'files') {
      library.clearSelection?.();
    }
  },
});
navigation.start();

// Library filter toggle
const filterToggleBtn = document.getElementById('filterToggleBtn');
const filterGrid = document.getElementById('filterGrid');
const clearFiltersBtn = document.getElementById('clearFiltersBtn');

function updateClearFiltersVisibility() {
  if (!clearFiltersBtn || !filterGrid) return;
  const hasActiveFilter = filterGrid.querySelector('select').value !== 'all'
    || filterGrid.querySelector('input[type="text"]')?.value
    || filterGrid.querySelector('input[type="date"]')?.value;
  clearFiltersBtn.hidden = !hasActiveFilter;
}

if (filterToggleBtn && filterGrid) {
  listen(filterToggleBtn, 'click', () => {
    const collapsed = filterGrid.classList.toggle('collapsed');
    filterToggleBtn.setAttribute('aria-expanded', String(!collapsed));
  });

  filterGrid.querySelectorAll('select, input').forEach(el => {
    listen(el, 'change', updateClearFiltersVisibility);
    listen(el, 'input', updateClearFiltersVisibility);
  });
}

if (clearFiltersBtn) {
  listen(clearFiltersBtn, 'click', () => {
    if (filterGrid) {
      filterGrid.querySelectorAll('select').forEach(s => s.value = 'all');
      filterGrid.querySelectorAll('input[type="date"], input[type="text"]').forEach(i => i.value = '');
    }
    const searchInput = document.getElementById('librarySearch');
    if (searchInput) searchInput.value = '';
    clearFiltersBtn.hidden = true;
    if (typeof library?.reloadFromStart === 'function') library.reloadFromStart();
  });
}

// Batch toolbar
const batchToolbar = document.getElementById('batchToolbar');
const batchToolbarCount = document.getElementById('batchToolbarCount');
const batchToolbarDownload = document.getElementById('batchToolbarDownload');
const batchToolbarCopy = document.getElementById('batchToolbarCopy');
const batchToolbarDelete = document.getElementById('batchToolbarDelete');
const batchToolbarClear = document.getElementById('batchToolbarClear');

function updateBatchToolbar(count) {
  if (!batchToolbar) return;
  if (count > 0) {
    batchToolbar.classList.add('visible');
    if (batchToolbarCount) batchToolbarCount.textContent = count;
  } else {
    batchToolbar.classList.remove('visible');
  }
}

listen(batchToolbarDownload, 'click', () => document.getElementById('batchDownload')?.click());
listen(batchToolbarCopy, 'click', () => document.getElementById('batchCopy')?.click());
listen(batchToolbarDelete, 'click', () => document.getElementById('batchDelete')?.click());
listen(batchToolbarClear, 'click', () => document.getElementById('clearSelectionBtn')?.click());

// Expose updateBatchToolbar for library module
window.__updateBatchToolbar = updateBatchToolbar;

// Timeline empty state
const timelineEmpty = document.getElementById('timelineEmpty');

function updateTimelineEmpty() {
  if (!timelineEmpty || !timelineContainer) return;
  const hasMessages = timelineContainer.querySelector('.timeline-message');
  timelineEmpty.hidden = !!hasMessages;
}

let timelineObserver = null;
try {
  timelineObserver = new MutationObserver(updateTimelineEmpty);
  if (timelineContainer) {
    timelineObserver.observe(timelineContainer, { childList: true, subtree: true });
    updateTimelineEmpty();
  }
} catch {
  // MutationObserver not available
}

export function destroyApp() {
  if (appDestroyed) return;
  appDestroyed = true;
  closeEventConnection();
  appListenerCleanups.splice(0).forEach(cleanup => cleanup());
  unsubscribeUploadCoordinator();
  composer.destroy?.();
  timeline.destroy?.();
  library.destroy?.();
  navigation.destroy?.();
  uploadCoordinator.destroy?.();
  timelineObserver?.disconnect();
  timelineObserver = null;
  window.clearTimeout(showToast.timer);
  if (window.__updateBatchToolbar === updateBatchToolbar) {
    window.__updateBatchToolbar = null;
  }
  if (window[APP_INSTANCE_KEY] === appInstance) {
    window[APP_INSTANCE_KEY] = null;
  }
}

function handlePageHide(event) {
  if (event.persisted) closeEventConnection();
  else destroyApp();
}

function resumeFromBFCache() {
  if (appDestroyed) return Promise.resolve(false);
  if (resumePromise) return resumePromise;
  resumePromise = (async () => {
    try {
      await getSession();
      if (appDestroyed) return false;
      isUnlocked = true;
      await restoreUploads();
      if (appDestroyed) return false;
      await timeline.loadInitial();
      if (appDestroyed) return false;
      renderUploadSnapshots();
      hideLockOverlay();
      startEventConnection();
      return true;
    } catch {
      if (!appDestroyed) showLockOverlay();
      return false;
    } finally {
      resumePromise = null;
    }
  })();
  return resumePromise;
}

function handlePageShow(event) {
  if (event.persisted) resumeFromBFCache();
}

const appInstance = { destroy: destroyApp };
window[APP_INSTANCE_KEY] = appInstance;
listen(window, 'pagehide', handlePageHide);
listen(window, 'pageshow', handlePageShow);

checkSession();
