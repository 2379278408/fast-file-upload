import { UNDO_WINDOW_MS, HIGHLIGHT_DURATION_MS } from './config.js';

export function createTimeline({ container, newMessageButton, api, onRestore, onUploadAction }) {
  const messages = new Map();
  const uploads = new Map();
  const messageByUploadId = new Map();
  const messageByClientRequestId = new Map();
  const messageElements = new Map();
  const uploadElements = new Map();
  const undoNotices = new Map();
  const undoMessages = new Map();
  const undoTimers = new Map();
  const timers = new Set();
  const appliedSequences = new Set();
  let lastSequence = 0;
  let nextBefore = null;
  let exhausted = false;
  let loading = false;
  let loadingPromise = null;
  let loadGeneration = 0;
  let observer = null;
  let atBottom = true;
  let newCount = 0;
  let destroyed = false;

  const SCROLL_THRESHOLD = 48;
  const HTTP = 'http:';
  const HTTPS = 'https:';

  function isNearBottom() {
    if (!container) return true;
    return container.scrollHeight - container.scrollTop - container.clientHeight <= SCROLL_THRESHOLD;
  }

  function scrollToBottom(smooth) {
    if (!container) return;
    container.scrollTo({
      top: container.scrollHeight,
      behavior: smooth ? 'smooth' : 'instant',
    });
    newCount = 0;
    updateNewButton();
  }

  function updateNewButton() {
    if (!newMessageButton) return;
    if (newCount > 0 && !isNearBottom()) {
      newMessageButton.textContent = `${newCount} 条新消息`;
      newMessageButton.hidden = false;
    } else {
      newMessageButton.hidden = true;
    }
  }

  function handleScroll() {
    atBottom = isNearBottom();
    if (atBottom) {
      newCount = 0;
      updateNewButton();
    }
  }

  function handleNewMessageClick() {
    scrollToBottom(true);
  }

  if (container) container.addEventListener('scroll', handleScroll);
  if (container) container.addEventListener('click', handleContainerClick);
  if (newMessageButton) {
    newMessageButton.hidden = true;
    newMessageButton.addEventListener('click', handleNewMessageClick);
  }

  function getLocalDate(ts) {
    const timestamp = safeTimestamp(ts);
    const d = new Date(timestamp);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }

  function safeTimestamp(value) {
    const numeric = typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : value;
    const timestamp = new Date(numeric).getTime();
    return Number.isFinite(timestamp) ? timestamp : 0;
  }

  const _escSpan = document.createElement('span');
  function escapeHtml(value) {
    _escSpan.textContent = value || '';
    return _escSpan.innerHTML;
  }

  function appendTextWithLinks(node, body) {
    const pattern = /https?:\/\/[^\s]+/g;
    let offset = 0;
    for (const match of body.matchAll(pattern)) {
      if (match.index > offset) {
        node.append(document.createTextNode(body.slice(offset, match.index)));
      }
      const link = document.createElement('a');
      link.href = match[0];
      link.textContent = match[0];
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      node.append(link);
      offset = match.index + match[0].length;
    }
    if (offset < body.length) {
      node.append(document.createTextNode(body.slice(offset)));
    }
  }

  function createDateSeparator(dateStr) {
    const sep = document.createElement('div');
    sep.className = 'timeline-date-separator';
    sep.setAttribute('role', 'separator');
    const label = document.createElement('span');
    label.className = 'timeline-date-label';
    label.textContent = dateStr;
    sep.append(label);
    return sep;
  }

  function renderMessage(msg) {
    const el = document.createElement('div');
    el.className = 'timeline-message';
    el.dataset.messageId = msg.id;
    el.dataset.createdAt = msg.created_at || '';
    if (msg.upload_id) el.dataset.uploadStatus = 'complete';

    const body = document.createElement('div');
    body.className = 'timeline-message-body';
    appendTextWithLinks(body, msg.body || '');
    el.append(body);

    if (msg.file) {
      const fileCard = document.createElement('div');
      fileCard.className = 'timeline-file-card';
      const fileLink = document.createElement('a');
      fileLink.href = msg.file.download_url || `/download/${msg.file.id}`;
      fileLink.textContent = msg.file.name || 'file';
      fileLink.target = '_blank';
      fileLink.rel = 'noopener noreferrer';
      fileCard.append(fileLink);
      if (msg.file.size) {
        const sizeSpan = document.createElement('span');
        sizeSpan.className = 'timeline-file-size';
        sizeSpan.textContent = msg.file.size;
        fileCard.append(sizeSpan);
      }
      el.append(fileCard);
    }

    const actions = document.createElement('div');
    actions.className = 'timeline-message-actions';
    const copyBtn = document.createElement('button');
    copyBtn.className = 'btn btn-soft timeline-copy-btn';
    copyBtn.dataset.timelineAction = 'copy';
    copyBtn.type = 'button';
    copyBtn.textContent = '复制';
    actions.append(copyBtn);

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'btn btn-soft timeline-delete-btn';
    deleteBtn.dataset.timelineAction = 'delete';
    deleteBtn.type = 'button';
    deleteBtn.textContent = '删除';
    actions.append(deleteBtn);
    el.append(actions);

    return el;
  }

  const uploadStatusLabels = {
    queued: '等待上传',
    uploading: '上传中',
    paused: '已暂停',
    verifying: '正在校验',
    completing: '正在校验',
    failed: '上传失败',
    complete: '已完成',
    completed: '已完成',
    cancelled: '已取消',
    expired: '已过期',
    'needs-file': '等待重新选择原文件',
  };

  function formatBytes(size) {
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = Number(size) || 0;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    const digits = unit === 0 ? 0 : 1;
    return `${value.toFixed(digits)} ${units[unit]}`;
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds)) return '计算中';
    if (seconds < 60) return `${Math.ceil(seconds)} 秒`;
    return `${Math.ceil(seconds / 60)} 分钟`;
  }

  function uploadActions(upload) {
    if (upload.pendingAction) return [];
    const actions = [];
    if (upload.isSourceDevice !== false) {
      if (['queued', 'uploading'].includes(upload.status)) actions.push(['pause', '暂停']);
      if (upload.errorCode === 'reselect_required' || upload.errorCode === 'file_mismatch' || upload.status === 'needs-file') {
        actions.push(['reselect', '重新选择原文件']);
      } else if (upload.status === 'paused') {
        actions.push(['resume', '继续']);
      }
      if (upload.status === 'failed') actions.push(['retry', '重试']);
      if (upload.status === 'queued') actions.push(['prioritize', '优先上传']);
    }
    if (!['complete', 'completed', 'cancelled', 'expired'].includes(upload.status)) {
      actions.push(['cancel', '取消']);
    }
    return actions;
  }

  function renderUpload(upload) {
    const element = document.createElement('article');
    element.className = `timeline-message timeline-upload-card upload-card upload-card-${upload.status || 'queued'}`;
    element.dataset.uploadId = upload.uploadId;
    element.dataset.clientRequestId = upload.clientRequestId || '';
    element.dataset.createdAt = upload.createdAt || '';
    element.dataset.uploadStatus = upload.status === 'completed'
      ? 'complete'
      : (upload.status || 'queued');

    const heading = document.createElement('div');
    heading.className = 'upload-card-heading';
    const name = document.createElement('strong');
    name.className = 'upload-card-name';
    name.textContent = upload.name || '未命名文件';
    const status = document.createElement('span');
    status.className = 'upload-card-status';
    const pendingLabels = { pause: '正在暂停', resume: '正在继续', cancel: '正在取消' };
    status.textContent = upload.pendingAction
      ? pendingLabels[upload.pendingAction]
      : (upload.errorCode === 'reselect_required'
      ? '需要重新选择原文件'
      : (upload.errorCode === 'file_mismatch' ? '文件不匹配' : (uploadStatusLabels[upload.status] || upload.status || '等待上传')));
    heading.append(name, status);
    element.append(heading);

    const progress = document.createElement('progress');
    progress.className = 'upload-card-progress';
    progress.max = 100;
    progress.value = Math.max(0, Math.min(100, Math.round(upload.progressPercent || 0)));
    progress.setAttribute('aria-label', `${name.textContent}上传进度`);
    element.append(progress);

    const metrics = document.createElement('p');
    metrics.className = 'upload-card-metrics';
    const confirmed = upload.confirmedBytes || 0;
    const total = upload.sizeBytes || 0;
    const parts = [`${Math.round(upload.progressPercent || 0)}%`, `${formatBytes(confirmed)} / ${formatBytes(total)}`];
    if (upload.status === 'uploading' && upload.isSourceDevice !== false) {
      if (upload.etaSeconds === null || upload.etaSeconds === undefined) {
        parts.push('计算中');
      } else {
        parts.push(`${formatBytes(upload.speedBytesPerSecond || 0)}/s`);
        parts.push(`剩余 ${formatDuration(upload.etaSeconds)}`);
      }
    }
    metrics.textContent = parts.join(' · ');
    element.append(metrics);

    if (upload.errorMessage) {
      const error = document.createElement('p');
      error.className = 'upload-card-error';
      error.textContent = upload.errorMessage;
      element.append(error);
    }

    const actions = document.createElement('div');
    actions.className = 'upload-card-actions';
    uploadActions(upload).forEach(([action, label]) => {
      const button = document.createElement('button');
      button.className = `upload-card-action ${action === 'cancel' ? 'btn btn-danger' : 'btn btn-soft'}`;
      button.type = 'button';
      button.dataset.uploadAction = action;
      button.textContent = label;
      actions.append(button);
    });
    element.append(actions);
    return element;
  }

  function upsertUpload(snapshot) {
    if (destroyed || !snapshot || !snapshot.uploadId) return false;
    const previous = uploads.get(snapshot.uploadId);
    const upload = { ...previous, ...snapshot };
    const permanentMessage = findMessageForUpload(upload);
    if (permanentMessage) {
      removeUpload(upload.uploadId);
      return false;
    }
    uploads.set(upload.uploadId, upload);
    if (!container) return true;
    const existing = uploadElements.get(upload.uploadId);
    const element = renderUpload(upload);
    if (existing) existing.remove();
    uploadElements.set(upload.uploadId, element);
    container.append(element);
    rebuildProjection();
    return true;
  }

  function removeUpload(uploadId, { rebuild = true } = {}) {
    if (destroyed) return null;
    const upload = uploads.get(uploadId);
    uploads.delete(uploadId);
    const element = uploadElements.get(uploadId);
    uploadElements.delete(uploadId);
    if (element) element.remove();
    if (rebuild && container) rebuildProjection();
    return upload;
  }

  function getUpload(uploadId) {
    return uploads.get(uploadId) || null;
  }

  function findUploadForMessage(message) {
    if (message.upload_id && uploads.has(message.upload_id)) return uploads.get(message.upload_id);
    if (!message.client_request_id) return null;
    return Array.from(uploads.values()).find(
      upload => upload.clientRequestId === message.client_request_id
    ) || null;
  }

  function findMessageForUpload(upload) {
    const messageId = messageByUploadId.get(upload.uploadId)
      || messageByClientRequestId.get(upload.clientRequestId);
    return messageId ? messages.get(messageId) || null : null;
  }

  function unindexMessage(message) {
    if (!message) return;
    if (message.upload_id && messageByUploadId.get(message.upload_id) === message.id) {
      messageByUploadId.delete(message.upload_id);
    }
    if (
      message.client_request_id
      && messageByClientRequestId.get(message.client_request_id) === message.id
    ) {
      messageByClientRequestId.delete(message.client_request_id);
    }
  }

  function indexMessage(message) {
    if (message.upload_id) messageByUploadId.set(message.upload_id, message.id);
    if (message.client_request_id) {
      messageByClientRequestId.set(message.client_request_id, message.id);
    }
  }

  function removeMessage(messageId, { rebuild = true } = {}) {
    if (destroyed) return null;
    const message = messages.get(messageId);
    unindexMessage(message);
    messages.delete(messageId);
    const element = messageElements.get(messageId);
    messageElements.delete(messageId);
    if (element) element.remove();
    if (rebuild && container) rebuildProjection();
    return message;
  }

  function showUndo(message) {
    if (destroyed || !container) return;
    const existing = undoNotices.get(message.id);
    if (existing) existing.remove();
    const existingTimer = undoTimers.get(message.id);
    if (existingTimer) {
      clearTimeout(existingTimer);
      timers.delete(existingTimer);
    }

    const notice = document.createElement('div');
    notice.className = 'timeline-undo';
    notice.dataset.undoMessageId = message.id;
    const label = document.createElement('span');
    label.textContent = '已删除，可在 30 秒内撤销';
    const undoButton = document.createElement('button');
    undoButton.className = 'btn btn-soft timeline-undo-btn';
    undoButton.type = 'button';
    undoButton.dataset.timelineAction = 'undo';
    undoButton.textContent = '撤销';
    notice.append(label, undoButton);
    container.append(notice);
    undoNotices.set(message.id, notice);
    undoMessages.set(message.id, message);

    const deletedAt = Date.parse(message.deleted_at || '');
    const undoMilliseconds = Number.isFinite(deletedAt)
      ? Math.max(0, deletedAt + UNDO_WINDOW_MS - Date.now())
      : UNDO_WINDOW_MS;
    const expiryTimer = setTimeout(() => {
      timers.delete(expiryTimer);
      undoTimers.delete(message.id);
      undoNotices.delete(message.id);
      undoMessages.delete(message.id);
      notice.remove();
    }, undoMilliseconds);
    timers.add(expiryTimer);
    undoTimers.set(message.id, expiryTimer);
  }

  async function copyMessage(message) {
    const text = message.file
      ? `${location.origin}${message.file.download_url || `/download/${message.file.id}`}`
      : (message.body || '');
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard failures leave the timeline unchanged.
    }
  }

  async function deleteMessage(message) {
    const label = message.file ? (message.file.name || '这个文件') : '这条消息';
    if (!confirm(`确定删除${label}吗？`)) return;
    try {
      const deleted = await api(`/api/messages/${encodeURIComponent(message.id)}`, {
        method: 'DELETE',
      });
      if (destroyed) return;
      removeMessage(message.id);
      showUndo({ ...message, ...(deleted || {}) });
    } catch {
      // The event stream keeps the timeline consistent after request failures.
    }
  }

  async function restoreMessage(messageId, button) {
    const message = undoMessages.get(messageId);
    if (!message) return;
    button.disabled = true;
    try {
      const restored = await api(
        `/api/messages/${encodeURIComponent(messageId)}/restore`,
        { method: 'POST' }
      );
      if (destroyed) return;
      const expiryTimer = undoTimers.get(messageId);
      if (expiryTimer) {
        clearTimeout(expiryTimer);
        timers.delete(expiryTimer);
      }
      undoTimers.delete(messageId);
      undoNotices.get(messageId)?.remove();
      undoNotices.delete(messageId);
      undoMessages.delete(messageId);
      upsertMessage(restored);
    } catch {
      if (!destroyed) button.disabled = false;
    }
  }

  function handleContainerClick(event) {
    if (destroyed) return;
    const target = event.target;
    if (!target) return;

    const uploadButton = closestWithDataset(target, 'uploadAction');
    if (uploadButton) {
      const uploadElement = closestWithDataset(uploadButton, 'uploadId');
      if (uploadElement) {
        onUploadAction?.({
          action: uploadButton.dataset.uploadAction,
          uploadId: uploadElement.dataset.uploadId,
        });
      }
      return;
    }

    const actionButton = closestWithDataset(target, 'timelineAction');
    if (!actionButton) return;
    const action = actionButton.dataset.timelineAction;
    if (action === 'undo') {
      const notice = closestWithDataset(actionButton, 'undoMessageId');
      if (notice) restoreMessage(notice.dataset.undoMessageId, actionButton);
      return;
    }

    const messageElement = closestWithDataset(actionButton, 'messageId');
    const message = messageElement && messages.get(messageElement.dataset.messageId);
    if (!message) return;
    if (action === 'copy') copyMessage(message);
    if (action === 'delete') deleteMessage(message);
  }

  function closestWithDataset(element, key) {
    let current = element;
    while (current) {
      if (current.dataset?.[key]) return current;
      if (current === container) break;
      current = current.parentNode;
    }
    return null;
  }

  function stableElementId(element) {
    if (element.dataset.messageId) return `message:${element.dataset.messageId}`;
    return `upload:${element.dataset.uploadId || ''}`;
  }

  function compareProjectedElements(left, right) {
    const leftTimestamp = safeTimestamp(left.dataset.createdAt || '');
    const rightTimestamp = safeTimestamp(right.dataset.createdAt || '');
    if (leftTimestamp !== rightTimestamp) return leftTimestamp - rightTimestamp;
    const leftId = stableElementId(left);
    const rightId = stableElementId(right);
    if (leftId < rightId) return -1;
    if (leftId > rightId) return 1;
    return 0;
  }

  function rebuildProjection() {
    if (destroyed || !container) return;
    // This static lookup also ensures a failed DOM application leaves the event retryable.
    container.querySelector('.timeline-sentinel');
    const elements = [
      ...messageElements.values(),
      ...uploadElements.values(),
    ].sort(compareProjectedElements);
    const notices = Array.from(undoNotices.values());

    container.querySelectorAll('.timeline-date-separator').forEach(separator => separator.remove());
    elements.forEach(element => element.remove());
    notices.forEach(notice => notice.remove());

    let previousDate = null;
    for (const element of elements) {
      const date = getLocalDate(element.dataset.createdAt || Date.now());
      if (date !== previousDate) {
        const separator = createDateSeparator(date);
        separator.dataset.date = date;
        container.append(separator);
        previousDate = date;
      }
      container.append(element);
    }
    notices.forEach(notice => container.append(notice));
  }

  function mergeEvent(event) {
    if (destroyed || !event) return false;
    if (appliedSequences.has(event.sequence)) return true;
    let payload = event.payload;
    if (typeof payload === 'string') {
      try {
        payload = JSON.parse(payload);
      } catch {
        payload = {};
      }
    }
    payload = payload && typeof payload === 'object' ? payload : {};

    switch (event.event_type) {
      case 'message.created': {
        const msg = { ...payload, id: event.entity_id };
        const existed = messages.has(event.entity_id);
        upsertMessage(msg);
        const existing = messageElements.get(event.entity_id);
        if (existing) existing.dataset.sequence = String(event.sequence);
        if (!existed && !atBottom) {
          newCount++;
          updateNewButton();
        }
        break;
      }
      case 'message.deleted': {
        removeMessage(event.entity_id);
        break;
      }
      case 'message.restored': {
        const msg = { ...payload, id: event.entity_id };
        const undoNotice = undoNotices.get(event.entity_id);
        if (undoNotice) {
          undoNotices.delete(event.entity_id);
          undoNotice.remove();
        }
        const undoTimer = undoTimers.get(event.entity_id);
        if (undoTimer) {
          clearTimeout(undoTimer);
          timers.delete(undoTimer);
          undoTimers.delete(event.entity_id);
        }
        upsertMessage(msg);
        const restoredElement = messageElements.get(event.entity_id);
        if (restoredElement) restoredElement.dataset.sequence = String(event.sequence);
        break;
      }
      case 'file.finalized': {
        const fileId = event.entity_id;
        for (const [msgId, msg] of messages) {
          if (msg.file && msg.file.id === fileId) {
            msg.file = { ...msg.file, ...payload, id: fileId };
            upsertMessage({ ...msg, id: msgId });
          }
        }
        break;
      }
    }
    appliedSequences.add(event.sequence);
    lastSequence = Math.max(lastSequence, event.sequence);
    return true;
  }

  function focusMessage(messageId) {
    if (destroyed || !container) return false;
    const el = messageElements.get(messageId);
    if (el) {
      let scrollBehavior = 'smooth';
      try {
        if (
          typeof window !== 'undefined'
          && typeof window.matchMedia === 'function'
          && window.matchMedia('(prefers-reduced-motion: reduce)').matches
        ) {
          scrollBehavior = 'auto';
        }
      } catch {
        scrollBehavior = 'smooth';
      }
      el.scrollIntoView({ behavior: scrollBehavior, block: 'center' });
      el.classList.add('timeline-message-highlight');
      el.setAttribute('tabindex', '-1');
      el.focus({ preventScroll: true });
      const timer = setTimeout(() => {
        timers.delete(timer);
        el.classList.remove('timeline-message-highlight');
      }, HIGHLIGHT_DURATION_MS);
      timers.add(timer);
      return true;
    }
    return false;
  }

  async function loadInitial({ throwOnError = false } = {}) {
    const generation = ++loadGeneration;
    if (destroyed || !container) return;
    loading = false;
    loadingPromise = null;
    observer?.disconnect();
    container.innerHTML = '';
    messages.clear();
    uploads.clear();
    messageByUploadId.clear();
    messageByClientRequestId.clear();
    messageElements.clear();
    uploadElements.clear();
    undoNotices.clear();
    undoMessages.clear();
    undoTimers.clear();
    timers.forEach(timer => clearTimeout(timer));
    timers.clear();
    appliedSequences.clear();
    lastSequence = 0;
    nextBefore = null;
    exhausted = false;
    newCount = 0;
    updateNewButton();

    const sentinel = document.createElement('div');
    sentinel.className = 'timeline-sentinel';
    sentinel.setAttribute('aria-hidden', 'true');
    container.append(sentinel);
    if (observer) observer.observe(sentinel);

    await loadOlder({ throwOnError });
    if (destroyed || generation !== loadGeneration) return;
    scrollToBottom(false);

    if (onRestore) await onRestore();
  }

  function loadOlder({ throwOnError = false } = {}) {
    if (destroyed) return Promise.resolve(false);
    if (loadingPromise) return loadingPromise;
    if (exhausted) return Promise.resolve(false);
    const generation = loadGeneration;
    const cursor = nextBefore;
    const anchor = captureVisibleAnchor();
    const previousScrollHeight = container ? container.scrollHeight : 0;
    const previousScrollTop = container ? container.scrollTop : 0;
    loading = true;
    const operationPromise = (async () => {
      try {
        const url = cursor
          ? `/api/messages?limit=50&before=${encodeURIComponent(cursor)}`
          : '/api/messages?limit=50';
        const data = await api(url);
        if (destroyed || generation !== loadGeneration || !data) return false;
        const items = data.items || [];
        if (items.length === 0) {
          nextBefore = null;
          exhausted = true;
          return false;
        }
        nextBefore = data.next_before || null;
        exhausted = !nextBefore;
        for (const msg of items) {
          upsertMessage(msg, { rebuild: false });
        }

        if (container) {
          rebuildProjection();
          restorePagingPosition(anchor, previousScrollHeight, previousScrollTop);
        }
        return true;
      } catch (error) {
        if (!destroyed && generation === loadGeneration) {
          window.dispatchEvent(new CustomEvent('timeline-error', { detail: { message: '加载历史消息失败，请稍后重试。' } }));
        }
        if (throwOnError) throw error;
        return false;
      }
    })();
    const requestPromise = operationPromise.finally(() => {
      if (loadingPromise === requestPromise) {
        loading = false;
        loadingPromise = null;
      }
    });
    loadingPromise = requestPromise;
    return requestPromise;
  }

  function captureVisibleAnchor() {
    if (!container) return null;
    const containerRect = container.getBoundingClientRect?.() || { top: 0, bottom: Infinity };
    const elements = container.querySelectorAll('.timeline-message');
    for (const element of elements) {
      const rect = element.getBoundingClientRect?.();
      if (rect && rect.bottom > containerRect.top && rect.top < containerRect.bottom) {
        if (element.dataset.messageId) {
          return { type: 'message', id: element.dataset.messageId, top: rect.top };
        }
        if (element.dataset.uploadId) {
          return { type: 'upload', id: element.dataset.uploadId, top: rect.top };
        }
      }
    }
    return null;
  }

  function restorePagingPosition(anchor, previousScrollHeight, previousScrollTop) {
    const anchorElement = anchor && (
      anchor.type === 'message'
        ? messageElements.get(anchor.id)
        : uploadElements.get(anchor.id)
    );
    const currentTop = anchorElement?.getBoundingClientRect?.().top;
    if (typeof currentTop === 'number') {
      container.scrollTop = previousScrollTop + currentTop - anchor.top;
      return;
    }
    container.scrollTop = previousScrollTop + container.scrollHeight - previousScrollHeight;
  }

  function hasMessageElement(messageId) {
    return !destroyed && messageElements.has(messageId);
  }

  async function ensureMessageLoaded(messageId) {
    if (hasMessageElement(messageId)) return true;
    if (loadingPromise) {
      await loadingPromise;
      if (hasMessageElement(messageId)) return true;
    }
    while (!exhausted && nextBefore) {
      const loaded = await loadOlder();
      if (hasMessageElement(messageId)) return true;
      if (!loaded) break;
    }
    return false;
  }

  async function loadUntil(messageId, { focus = true } = {}) {
    const loaded = await ensureMessageLoaded(messageId);
    if (loaded && focus) focusMessage(messageId);
    return loaded;
  }

  // IntersectionObserver for infinite scroll (load older)
  if (container && typeof IntersectionObserver !== 'undefined') {
    observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            if (destroyed) return;
            loadOlder();
          }
        }
      },
      { root: container, threshold: 0.1 }
    );

  }

  function getLastSequence() {
    return lastSequence;
  }

  function setLastSequence(sequence) {
    lastSequence = sequence;
  }

  function upsertMessage(message, { rebuild = true } = {}) {
    if (destroyed || !message || !message.id) return false;
    const existingMessage = messages.get(message.id);
    const merged = {
      ...existingMessage,
      ...message,
      file: message.file || (existingMessage && existingMessage.file) || null,
    };
    unindexMessage(existingMessage);
    messages.set(message.id, merged);
    indexMessage(merged);
    if (!container) return true;

    const existingElement = messageElements.get(message.id);
    const element = renderMessage(merged);
    if (existingElement?.dataset.sequence) {
      element.dataset.sequence = existingElement.dataset.sequence;
    }
    if (existingElement) {
      existingElement.remove();
    }

    const matchingUpload = findUploadForMessage(merged);
    if (matchingUpload) {
      removeUpload(matchingUpload.uploadId, { rebuild: false });
    }

    messageElements.set(message.id, element);
    container.append(element);
    if (rebuild) rebuildProjection();
    return true;
  }

  function destroy() {
    loadGeneration += 1;
    if (destroyed) return;
    destroyed = true;
    container?.removeEventListener('scroll', handleScroll);
    container?.removeEventListener('click', handleContainerClick);
    newMessageButton?.removeEventListener('click', handleNewMessageClick);
    observer?.disconnect();
    observer = null;
    timers.forEach(timer => clearTimeout(timer));
    timers.clear();
    undoMessages.clear();
    loading = false;
    loadingPromise = null;
  }

  return {
    loadInitial,
    loadOlder,
    mergeEvent,
    focusMessage,
    ensureMessageLoaded,
    loadUntil,
    getLastSequence,
    setLastSequence,
    upsert: upsertMessage,
    upsertUpload,
    removeUpload,
    getUpload,
    destroy,
  };
}
