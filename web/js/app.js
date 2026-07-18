import { request as apiRequest, unlock as apiUnlock, logout as apiLogout, getSession, ApiError, connectEvents, getLastSequence } from './api.js';
import { createTimeline } from './timeline.js';
import { createComposer } from './composer.js';
import { createLibrary } from './library.js';
import { TOAST_DURATION_MS } from './config.js';

const DEVICE_ID_KEY = 'device-id';
const DEVICE_NAME_KEY = 'device-name';

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
  if (!sessionExpiredOverlay) return;
  sessionExpiredOverlay.classList.add('is-hidden');
  unlockOutsideDialog();
  isUnlocked = true;
  restorePreviousFocus();
}

if (sessionExpiredOverlay) {
  sessionExpiredOverlay.addEventListener('keydown', event => {
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
  skipLink.addEventListener('click', event => {
    event.preventDefault();
    location.hash = 'mainContent';
    mainContent.focus();
  });
}

async function checkSession() {
  try {
    await getSession();
    isUnlocked = true;
    const storedName = getDeviceName();
    if (routeSource && storedName) routeSource.textContent = storedName;
    if (connectionDevice) connectionDevice.textContent = storedName || '-';
    await refreshAll();
    await timeline.loadInitial();
    hideLockOverlay();
    startEventConnection();
  } catch {
    showLockOverlay();
  }
}

if (unlockForm) {
  unlockForm.addEventListener('submit', async (event) => {
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
      setDeviceName(name);
      await refreshAll();
      await timeline.loadInitial();
      hideLockOverlay();
      startEventConnection();
    } catch (err) {
      const message = err instanceof ApiError ? (err.body && err.body.detail || '验证失败') : '网络错误';
      if (unlockError) {
        unlockError.textContent = message;
        unlockError.classList.add('visible');
      }
    }
  });
}

window.addEventListener('session-expired', closeEventConnection);
window.addEventListener('session-expired', showLockOverlay);
window.addEventListener('session-logout', async () => {
  closeEventConnection();
  try { await apiLogout(); } finally { showLockOverlay(); }
});

const timelineContainer = document.getElementById('timelineContainer');
const newMessageButton = document.getElementById('newMessageButton');

const timeline = createTimeline({
  container: timelineContainer,
  newMessageButton: newMessageButton,
  api: (url, options) => apiRequest(url, options),
  async onRestore() {
    const snapshot = savedTimelinePosition;
    try {
      await restoreTimelinePosition(timelineContainer, snapshot, timeline);
    } finally {
      if (savedTimelinePosition === snapshot) savedTimelinePosition = null;
    }
  },
});

const composerForm = document.getElementById('composerForm');
const composerTextarea = document.getElementById('composerTextarea');
const composerFileInput = document.getElementById('composerFileInput');
const composerDropTarget = document.getElementById('composerDropTarget');
const composerQueue = document.getElementById('composerQueue');
const composerAttachBtn = document.getElementById('composerAttachBtn');
const routeSource = document.getElementById('routeSource');
const routeTarget = document.getElementById('routeTarget');

const composer = createComposer({
  form: composerForm,
  textarea: composerTextarea,
  fileInput: composerFileInput,
  dropTarget: composerDropTarget,
  queue: composerQueue,
  api: (url, opts) => apiRequest(url, opts),
  timeline,
});

const library = createLibrary({
  root: document.getElementById('libraryView'),
  api: (url, options) => apiRequest(url, options),
  timeline,
});

if (composerAttachBtn && composerFileInput) {
  composerAttachBtn.addEventListener('click', () => composerFileInput.click());
}

if (timelineContainer) {
  ['dragenter', 'dragover'].forEach(name => {
    timelineContainer.addEventListener(name, event => {
      event.preventDefault();
      timelineContainer.classList.add('dragover');
    });
  });
  ['dragleave', 'drop'].forEach(name => {
    timelineContainer.addEventListener(name, event => {
      event.preventDefault();
      timelineContainer.classList.remove('dragover');
    });
  });
  timelineContainer.addEventListener('drop', event => {
    const files = Array.from(event.dataTransfer.files);
    if (files.length) composer.enqueueFiles(files);
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

if (refreshHealthBtn) refreshHealthBtn.addEventListener('click', loadHealth);
if (refreshOpsBtn) refreshOpsBtn.addEventListener('click', loadOperations);

const railRefresh = document.getElementById('railRefresh');
const retryConnection = document.getElementById('retryConnection');
if (railRefresh) railRefresh.addEventListener('click', () => { loadHealth(); startEventConnection(); });
if (retryConnection) retryConnection.addEventListener('click', () => { startEventConnection(); });

async function loadHealth() {
  try {
    healthState = await apiRequest('/api/health');
    if (metricCount) metricCount.textContent = healthState.file_count ?? 0;
    if (metricSize) metricSize.textContent = healthState.total_size ?? '0 B';
    if (metricLimit) metricLimit.textContent = healthState.max_upload_size ?? '-';
    if (metricMode) metricMode.textContent = healthState.protected ? '受控' : '公开';
    if (modeState) modeState.textContent = healthState.protected ? '令牌访问' : '公开访问';
  } catch (error) {
    showToast('健康信息读取失败', 'error');
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
    renderOperations(summary, audit.events || []);
  } catch (error) {
    renderOpsError('运营视图读取失败，请检查服务状态。');
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
  if (!connectionStatusEl) return;
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

function applyIncomingEvent(event) {
  if (!event) return false;
  timeline.mergeEvent(event);
  if (library && typeof library.applyEvent === 'function') {
    library.applyEvent(event);
  }
  return true;
}

function startEventConnection() {
  closeEventConnection();
  eventConnection = connectEvents({
    after: () => getLastSequence(),
    onEvent: applyIncomingEvent,
    onStatus: updateConnectionStatus,
  });
}

window.addEventListener('timeline-error', (event) => {
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
  themeToggle.addEventListener('click', () => {
    try {
      const current = document.documentElement.classList.contains('dark') ? 'dark' : 'light';
      const next = current === 'dark' ? 'light' : 'dark';
      localStorage.setItem(THEME_KEY, next);
      applyTheme(next);
    } catch { /* noop */ }
  });
}

try {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', event => {
    if (!localStorage.getItem(THEME_KEY)) {
      applyTheme(event.matches ? 'dark' : 'light');
    }
  });
} catch {
  // matchMedia not available (e.g. QuickJS test environment)
}

// Sidebar navigation
function setupNavigation() {
  const navButtons = document.querySelectorAll('[data-section]');
  if (!navButtons.length) return;

  const sectionPanels = {
    workspace: ['composerPanel', 'libraryView'],
    files: ['libraryView'],
    activity: ['timelinePanel'],
    devices: ['connectionPanel'],
    settings: ['operationsPanel']
  };

  function switchSection(section) {
    navButtons.forEach(b => {
      b.classList.remove('active');
      b.removeAttribute('aria-current');
    });
    document.querySelectorAll(`[data-section="${section}"]`).forEach(b => {
      b.classList.add('active');
      b.setAttribute('aria-current', 'page');
    });

    // Show/hide panels based on section
    const mainColumn = document.querySelector('.main-column');
    const rail = document.querySelector('.rail');
    if (mainColumn) {
      const panels = mainColumn.querySelectorAll(':scope > .panel');
      panels.forEach(p => {
        const id = p.id;
        const shouldShow = section === 'workspace' || (sectionPanels[section] && sectionPanels[section].includes(id));
        p.hidden = !shouldShow;
      });
    }
    if (rail) {
      const railPanels = rail.querySelectorAll(':scope > .panel');
      railPanels.forEach(p => {
        const id = p.id;
        const shouldShow = section === 'workspace' || (sectionPanels[section] && sectionPanels[section].includes(id));
        p.hidden = !shouldShow;
      });
    }
  }

  navButtons.forEach(btn => {
    btn.addEventListener('click', () => switchSection(btn.dataset.section));
  });
}
try { setupNavigation(); } catch { /* noop */ }

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
  filterToggleBtn.addEventListener('click', () => {
    const collapsed = filterGrid.classList.toggle('collapsed');
    filterToggleBtn.setAttribute('aria-expanded', String(!collapsed));
  });

  filterGrid.querySelectorAll('select, input').forEach(el => {
    el.addEventListener('change', updateClearFiltersVisibility);
    el.addEventListener('input', updateClearFiltersVisibility);
  });
}

if (clearFiltersBtn) {
  clearFiltersBtn.addEventListener('click', () => {
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

if (batchToolbarDownload) batchToolbarDownload.addEventListener('click', () => document.getElementById('batchDownload')?.click());
if (batchToolbarCopy) batchToolbarCopy.addEventListener('click', () => document.getElementById('batchCopy')?.click());
if (batchToolbarDelete) batchToolbarDelete.addEventListener('click', () => document.getElementById('batchDelete')?.click());
if (batchToolbarClear) batchToolbarClear.addEventListener('click', () => document.getElementById('clearSelectionBtn')?.click());

// Expose updateBatchToolbar for library module
window.__updateBatchToolbar = updateBatchToolbar;

// Timeline empty state
const timelineEmpty = document.getElementById('timelineEmpty');

function updateTimelineEmpty() {
  if (!timelineEmpty || !timelineContainer) return;
  const hasMessages = timelineContainer.querySelector('.timeline-message');
  timelineEmpty.hidden = !!hasMessages;
}

try {
  const timelineObserver = new MutationObserver(updateTimelineEmpty);
  if (timelineContainer) {
    timelineObserver.observe(timelineContainer, { childList: true, subtree: true });
    updateTimelineEmpty();
  }
} catch {
  // MutationObserver not available
}

checkSession();
