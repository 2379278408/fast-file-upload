import { UNDO_WINDOW_MS, TOAST_DURATION_MS } from './config.js';

export function createLibrary({ root, api, timeline, onAttach = () => {} }) {
  const fileListEl = root.querySelector('#fileList');
  const libraryCountEl = root.querySelector('#libraryCount');
  const imageCountEl = root.querySelector('#imageCount');
  const otherCountEl = root.querySelector('#otherCount');
  const selectedCountEl = root.querySelector('#selectedCount');
  const storageSummaryEl = root.querySelector('#storageSummary');

  const searchInput = root.querySelector('#librarySearch');
  const typeSelect = root.querySelector('#fileTypeFilter');
  const deviceInput = root.querySelector('#deviceFilter');
  const dateFromInput = root.querySelector('#dateFrom');
  const dateToInput = root.querySelector('#dateTo');
  const batchDownloadBtn = root.querySelector('#batchDownload');
  const batchCopyBtn = root.querySelector('#batchCopy');
  const batchDeleteBtn = root.querySelector('#batchDelete');
  const gridViewBtn = root.querySelector('#gridViewBtn');
  const listViewBtn = root.querySelector('#listViewBtn');
  const selectVisibleBtn = root.querySelector('#selectVisibleBtn');
  const clearSelectionBtn = root.querySelector('#clearSelectionBtn');
  const clearFiltersBtn = root.querySelector('#clearFiltersBtn');
  const loadMoreBtn = root.querySelector('#libraryLoadMore');
  const previewModal = document.getElementById('previewModal');
  const previewImage = document.getElementById('previewImage');
  const previewTitle = document.getElementById('previewTitle');
  const previewSize = document.getElementById('previewSize');
  const previewDate = document.getElementById('previewDate');
  const previewType = document.getElementById('previewType');
  const previewCopyBtn = document.getElementById('previewCopyBtn');
  const previewDownloadBtn = document.getElementById('previewDownloadBtn');
  const closePreviewBtn = document.getElementById('closePreviewBtn');

  let filesState = [];
  const selectedIds = new Set();
  let viewMode = 'grid';
  let loading = false;
  let reloadPending = false;
  let cursor = null;
  let hasMore = false;
  let currentPreview = null;
  let lastPreviewTrigger = null;
  let destroyed = false;
  const listenerCleanups = [];
  const pendingTimers = new Set();

  listen(batchDownloadBtn, 'click', batchDownload);
  listen(batchCopyBtn, 'click', copySelected);
  listen(batchDeleteBtn, 'click', batchDelete);
  listen(selectVisibleBtn, 'click', selectVisibleFiles);
  listen(clearSelectionBtn, 'click', clearSelection);
  listen(gridViewBtn, 'click', () => setViewMode('grid'));
  listen(listViewBtn, 'click', () => setViewMode('list'));
  for (const button of [gridViewBtn, listViewBtn]) {
    listen(button, 'keydown', event => handleViewKeydown(event, button));
  }
  listen(clearFiltersBtn, 'click', clearFilters);
  listen(loadMoreBtn, 'click', loadMore);

  if (fileListEl) {
    listen(fileListEl, 'click', handleFileAction);
    listen(fileListEl, 'change', (event) => {
      const checkbox = event.target.closest('[data-select-message]');
      if (checkbox) toggleFileSelection(checkbox.dataset.selectMessage, checkbox.checked);
    });
  }
  listen(previewModal, 'click', (event) => {
    if (event.target === previewModal) closePreview();
  });
  listen(closePreviewBtn, 'click', closePreview);
  listen(previewCopyBtn, 'click', () => {
    if (currentPreview) copyLink(currentPreview);
  });
  listen(document, 'keydown', (event) => {
    if (event.key === 'Escape' && previewModal && !previewModal.hidden) closePreview();
  });

  listen(searchInput, 'input', debounce(reloadFromStart, 300));
  listen(typeSelect, 'change', reloadFromStart);
  listen(deviceInput, 'input', reloadFromStart);
  listen(dateFromInput, 'change', reloadFromStart);
  listen(dateToInput, 'change', reloadFromStart);

  function listen(target, type, handler) {
    if (!target || destroyed) return;
    target.addEventListener(type, handler);
    listenerCleanups.push(() => target.removeEventListener(type, handler));
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    while (listenerCleanups.length) listenerCleanups.pop()();
    for (const timer of pendingTimers) clearTimeout(timer);
    pendingTimers.clear();
    reloadPending = false;
  }

  function debounce(fn, ms) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      pendingTimers.delete(timer);
      if (destroyed) return;
      timer = setTimeout(() => {
        pendingTimers.delete(timer);
        if (!destroyed) fn(...args);
      }, ms);
      pendingTimers.add(timer);
    };
  }

  function setViewMode(mode, focus = false) {
    viewMode = mode;
    if (gridViewBtn) gridViewBtn.classList.toggle('active', mode === 'grid');
    if (listViewBtn) listViewBtn.classList.toggle('active', mode === 'list');
    if (gridViewBtn) {
      gridViewBtn.setAttribute('aria-selected', String(mode === 'grid'));
      gridViewBtn.setAttribute('tabindex', mode === 'grid' ? '0' : '-1');
    }
    if (listViewBtn) {
      listViewBtn.setAttribute('aria-selected', String(mode === 'list'));
      listViewBtn.setAttribute('tabindex', mode === 'list' ? '0' : '-1');
    }
    const selectedButton = mode === 'grid' ? gridViewBtn : listViewBtn;
    if (focus && selectedButton) selectedButton.focus();
    renderFiles();
  }

  function handleViewKeydown(event, currentButton) {
    const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    let mode;
    if (event.key === 'Home') mode = 'grid';
    if (event.key === 'End') mode = 'list';
    if (event.key === 'ArrowRight') {
      mode = currentButton === gridViewBtn ? 'list' : 'grid';
    }
    if (event.key === 'ArrowLeft') {
      mode = currentButton === listViewBtn ? 'grid' : 'list';
    }
    setViewMode(mode, true);
  }

  function getSearchParams() {
    const params = {};
    const keyword = searchInput ? searchInput.value.trim() : '';
    if (keyword) params.q = keyword;
    const type = typeSelect ? typeSelect.value : 'all';
    if (type && type !== 'all') params.type = type;
    const device = deviceInput ? deviceInput.value.trim() : '';
    if (device) params.device_id = device;
    const from = dateFromInput ? dateFromInput.value : '';
    if (from) params.from = from;
    const to = dateToInput ? dateToInput.value : '';
    if (to) params.to = to;
    params.limit = 50;
    return params;
  }

  function clearFilters() {
    if (searchInput) searchInput.value = '';
    if (typeSelect) typeSelect.value = 'all';
    if (deviceInput) deviceInput.value = '';
    if (dateFromInput) dateFromInput.value = '';
    if (dateToInput) dateToInput.value = '';
    reloadFromStart();
  }

  function toggleFileSelection(id, checked) {
    if (checked) {
      selectedIds.add(id);
    } else {
      selectedIds.delete(id);
    }
    updateStats();
  }

  function selectVisibleFiles() {
    getFilteredItems().forEach((message) => selectedIds.add(message.id));
    renderFiles();
  }

  function clearSelection() {
    selectedIds.clear();
    if (fileListEl) {
      fileListEl.querySelectorAll('input[type="checkbox"]').forEach((cb) => { cb.checked = false; });
    }
    updateStats();
  }

  function pruneSelection() {
    const fileIds = new Set(filesState.map((message) => message.id));
    Array.from(selectedIds).forEach((id) => {
      if (!fileIds.has(id)) selectedIds.delete(id);
    });
  }

  async function openMessage(messageId) {
    if (!timeline) return;
    if (typeof timeline.focusMessage === 'function') {
      const found = timeline.focusMessage(messageId);
      if (!found && typeof timeline.ensureMessageLoaded === 'function') {
        await timeline.ensureMessageLoaded(messageId);
        timeline.focusMessage(messageId);
      } else if (!found && typeof timeline.loadUntil === 'function') {
        await timeline.loadUntil(messageId, { focus: false });
        timeline.focusMessage(messageId);
      }
    }
    location.hash = `message-${encodeURIComponent(messageId)}`;
  }

  function getMessage(messageId) {
    return filesState.find((message) => message.id === messageId);
  }

  async function handleFileAction(event) {
    const action = event.target.closest('[data-file-action]');
    if (!action) return;
    if (action.dataset.fileAction === 'empty-attach') {
      onAttach();
      return;
    }
    const message = getMessage(action.dataset.messageId);
    if (!message) return;
    const actionName = action.dataset.fileAction;
    if (actionName === 'preview') openPreview(message, action);
    if (actionName === 'copy') await copyLink(message);
    if (actionName === 'delete') await deleteMessage(message);
    if (actionName === 'locate') await openMessage(message.id);
  }

  function openPreview(message, trigger) {
    const file = message.file;
    if (!file || !file.is_previewable || !previewModal) return;
    currentPreview = message;
    lastPreviewTrigger = trigger || null;

    const isImage = file.mime_type && file.mime_type.startsWith('image/');

    if (previewImage) {
      if (isImage && file.download_url) {
        previewImage.src = file.download_url;
        previewImage.alt = file.name || '图片预览';
        previewImage.hidden = false;
      } else {
        previewImage.hidden = true;
        previewImage.removeAttribute('src');
      }
    }

    const placeholder = document.getElementById('previewPlaceholder');
    if (placeholder) {
      if (isImage) placeholder.classList.add('is-hidden');
      else placeholder.classList.remove('is-hidden');
    }

    if (previewTitle) previewTitle.textContent = file.name || '文件预览';
    const fileNameEl = document.getElementById('previewFileName');
    if (fileNameEl) fileNameEl.textContent = file.name || '-';
    if (previewSize) previewSize.textContent = file.size || '-';
    if (previewDate) previewDate.textContent = file.created_at || message.created_at || '-';
    if (previewType) previewType.textContent = file.extension || file.mime_type || '-';
    if (previewDownloadBtn) previewDownloadBtn.href = file.download_url;
    previewModal.hidden = false;
    previewModal.classList.add('open');
    if (closePreviewBtn) closePreviewBtn.focus();
    listen(document, 'keydown', trapPreviewFocus);
  }

  function trapPreviewFocus(event) {
    if (event.key !== 'Tab' || !previewModal || previewModal.hidden) return;
    const focusable = previewModal.querySelectorAll('button:not([hidden]), a[href]:not([hidden]), [tabindex]:not([tabindex="-1"])');
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function closePreview() {
    if (!previewModal) return;
    previewModal.hidden = true;
    previewModal.classList.remove('open');
    if (previewImage) previewImage.removeAttribute('src');
    currentPreview = null;
    document.removeEventListener('keydown', trapPreviewFocus);
    if (lastPreviewTrigger) lastPreviewTrigger.focus();
    lastPreviewTrigger = null;
  }

  async function copyLink(message) {
    const file = message.file;
    if (!file) return;
    const url = `${location.origin}${file.download_url}`;
    try {
      await navigator.clipboard.writeText(url);
      showToast('链接已复制', 'success');
    } catch {
      showToast(url, 'info');
    }
  }

  async function deleteMessage(message) {
    const file = message.file;
    if (!file || !confirm(`确定删除 ${file.name || '这个文件'} 吗？`)) return;
    try {
      const deleted = await api(`/api/messages/${encodeURIComponent(message.id)}`, { method: 'DELETE' });
      filesState = filesState.filter((item) => item.id !== message.id);
      selectedIds.delete(message.id);
      closePreview();
      renderFiles();
      showUndoToast(message.id, deleted);
    } catch {
      showToast('删除失败', 'error');
    }
  }

  function showUndoToast(messageId, deleted) {
    const toastEl = document.getElementById('toast');
    if (!toastEl) return;
    const deletedAt = deleted && deleted.deleted_at ? Date.parse(deleted.deleted_at) : Date.now();
    const remaining = Math.max(0, deletedAt + UNDO_WINDOW_MS - Date.now());

    toastEl.textContent = '文件已删除';
    const undoBtn = document.createElement('button');
    undoBtn.className = 'btn btn-soft';
    undoBtn.type = 'button';
    undoBtn.textContent = ' · 撤销';
    listen(undoBtn, 'click', async () => {
      undoBtn.disabled = true;
      try {
        await api(`/api/messages/${encodeURIComponent(messageId)}/restore`, { method: 'POST' });
        toastEl.classList.remove('show');
        reloadFromStart();
      } catch {
        undoBtn.disabled = false;
        showToast('撤销失败', 'error');
      }
    });
    toastEl.append(undoBtn);
    toastEl.className = 'toast info show';
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toastEl.classList.remove('show'), remaining);
  }

  async function load(opts) {
    if (loading) return;
    loading = true;
    updateLoadMoreState();
    try {
      const params = getSearchParams();
      if (opts && opts.cursor) {
        params.cursor = opts.cursor;
      }
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null && v !== '') qs.set(k, v);
      }
      const data = await api(`/api/files?${qs.toString()}`);
      if (!data) return;
      const items = data.items || [];
      if (opts && opts.cursor) {
        filesState = filesState.concat(items);
      } else {
        filesState = items;
      }
      cursor = data.next_cursor || null;
      hasMore = Boolean(cursor);
      updateLoadMoreState();
      pruneSelection();
      renderFiles();
      loadStorage();
    } catch (err) {
      if (fileListEl) {
        fileListEl.className = 'file-list grid-mode';
        fileListEl.innerHTML = '<div class="empty">文件列表读取失败，请检查服务状态。</div>';
      }
      updateStats();
    } finally {
      loading = false;
      updateLoadMoreState();
      if (reloadPending) {
        reloadPending = false;
        reloadFromStart();
      }
    }
  }

  function reloadFromStart() {
    cursor = null;
    hasMore = false;
    updateLoadMoreState();
    if (loading) {
      reloadPending = true;
      return Promise.resolve();
    }
    return load({});
  }

  function loadMore() {
    if (!hasMore || !cursor) return Promise.resolve(false);
    return load({ cursor });
  }

  function updateLoadMoreState() {
    if (!loadMoreBtn) return;
    loadMoreBtn.hidden = !hasMore;
    loadMoreBtn.disabled = loading;
    loadMoreBtn.textContent = loading ? '加载中…' : '加载更多';
  }

  async function loadStorage() {
    if (!storageSummaryEl) return;
    try {
      const data = await api('/api/storage');
      if (!data) return;
      const parts = [];
      if (data.file_count !== undefined) parts.push(`文件: ${data.file_count}`);
      if (data.total_size !== undefined) parts.push(`总大小: ${data.total_size}`);
      if (data.largest_files) {
        const top3 = data.largest_files.slice(0, 3).map((f) => f.name).join(', ');
        if (top3) parts.push(`最大: ${top3}`);
      }
      storageSummaryEl.textContent = parts.join(' | ') || '-';
    } catch {
      storageSummaryEl.textContent = '-';
    }
  }

  function applyEvent(event) {
    if (!event) return;
    switch (event.event_type) {
      case 'message.created':
      case 'message.restored': {
        const message = { ...event.payload, id: event.entity_id };
        if (!message.file) break;
        const existing = filesState.findIndex((item) => item.id === message.id);
        if (existing >= 0) {
          filesState[existing] = { ...filesState[existing], ...message };
        } else {
          filesState.unshift(message);
        }
        renderFiles();
        break;
      }
      case 'message.deleted': {
        filesState = filesState.filter((message) => message.id !== event.entity_id);
        selectedIds.delete(event.entity_id);
        renderFiles();
        break;
      }
      case 'file.finalized': {
        const fileId = event.entity_id;
        const payload = event.payload || {};
        filesState = filesState.map((message) => (
          message.file && message.file.id === fileId
            ? { ...message, file: { ...message.file, ...payload, id: fileId } }
            : message
        ));
        renderFiles();
        break;
      }
      case 'file.deleted': {
        const fileId = event.entity_id;
        const removed = filesState.filter((message) => message.file && message.file.id === fileId);
        filesState = filesState.filter((message) => !message.file || message.file.id !== fileId);
        removed.forEach((message) => selectedIds.delete(message.id));
        renderFiles();
        break;
      }
      case 'file.updated': {
        const fileId = event.entity_id;
        filesState = filesState.map((message) => (
          message.file && message.file.id === fileId
            ? { ...message, file: { ...message.file, ...(event.payload || {}) } }
            : message
        ));
        renderFiles();
        break;
      }
    }
  }

  async function batchDownload() {
    const selected = filesState.filter((message) => selectedIds.has(message.id));
    if (!selected.length) {
      showToast('先选择文件', 'info');
      return;
    }
    try {
      const blob = await api('/api/files/batch-download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message_ids: selected.map((message) => message.id) }),
        responseType: 'blob',
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `batch-${Date.now()}.zip`;
      a.click();
      URL.revokeObjectURL(url);
      showToast(`正在下载 ${selected.length} 个文件`, 'success');
    } catch {
      showToast('批量下载失败', 'error');
    }
  }

  async function batchDelete() {
    const selected = filesState.filter((message) => selectedIds.has(message.id));
    if (!selected.length) {
      showToast('先选择文件', 'info');
      return;
    }
    if (!confirm(`确定删除 ${selected.length} 个文件吗？`)) return;
    try {
      const result = await api('/api/messages/batch-delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message_ids: selected.map((message) => message.id) }),
      });
      const deleted = Number(result.deleted) || 0;
      const deletedIds = result.deleted_ids || [];
      selectedIds.clear();
      showBatchUndoToast(deletedIds, deleted);
      await reloadFromStart();
    } catch {
      showToast('批量删除失败', 'error');
    }
  }

  function showBatchUndoToast(deletedIds, count) {
    const toastEl = document.getElementById('toast');
    if (!toastEl) return;
    const remaining = UNDO_WINDOW_MS;

    if (!deletedIds.length) {
      showToast(`已删除 ${count} 个文件`, 'info');
      return;
    }

    toastEl.textContent = `已删除 ${count} 个文件`;
    const undoBtn = document.createElement('button');
    undoBtn.className = 'btn btn-soft';
    undoBtn.type = 'button';
    undoBtn.textContent = ' · 撤销';
    listen(undoBtn, 'click', async () => {
      undoBtn.disabled = true;
      let restored = 0;
      for (const id of deletedIds) {
        try {
          await api(`/api/messages/${encodeURIComponent(id)}/restore`, { method: 'POST' });
          restored++;
        } catch {
          // continue restoring others
        }
      }
      toastEl.classList.remove('show');
      showToast(`已恢复 ${restored} 个文件`, restored > 0 ? 'success' : 'error');
      reloadFromStart();
    });
    toastEl.append(undoBtn);

    toastEl.className = 'toast info show';
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toastEl.classList.remove('show'), remaining);
  }

  async function copySelected() {
    const selected = filesState.filter((message) => selectedIds.has(message.id));
    if (!selected.length) {
      showToast('先选择文件', 'info');
      return;
    }
    const urls = selected.map((message) => `${location.origin}${message.file.download_url}`);
    try {
      await navigator.clipboard.writeText(urls.join('\n'));
      showToast(`已复制 ${selected.length} 个链接`, 'success');
    } catch {
      showToast(urls.join('\n'), 'info');
    }
  }

  function getFilteredItems() {
    const type = typeSelect ? typeSelect.value : 'all';
    return filesState.filter((message) => {
      const file = message.file;
      if (!file) return false;
      if (type !== 'all' && file.extension !== type && file.media_kind !== type) return false;
      return true;
    });
  }

  function updateStats() {
    const items = getFilteredItems();
    const previewable = items.filter((message) => message.file && message.file.is_previewable);
    if (libraryCountEl) libraryCountEl.textContent = String(items.length);
    if (imageCountEl) imageCountEl.textContent = String(previewable.length);
    if (otherCountEl) otherCountEl.textContent = String(items.length - previewable.length);
    if (selectedCountEl) selectedCountEl.textContent = String(selectedIds.size);
    if (typeof window.__updateBatchToolbar === 'function') window.__updateBatchToolbar(selectedIds.size);
  }

  function renderFiles() {
    const items = getFilteredItems();
    updateStats();
    if (!fileListEl) return;

    if (!items.length) {
      fileListEl.className = 'file-list grid-mode';
      fileListEl.innerHTML = `
        <div class="empty">
          <div>
            <strong>还没有文件</strong>
            <p>选择文件后，它会显示在这里。</p>
            <button class="btn btn-primary" id="emptyFilesAction" type="button" data-file-action="empty-attach">选择文件</button>
          </div>
        </div>`;
      return;
    }

    if (viewMode === 'list') {
      fileListEl.className = 'file-list table-mode';
      fileListEl.innerHTML = renderFileTable(items);
    } else {
      fileListEl.className = 'file-list grid-mode';
      fileListEl.innerHTML = items.map((message) => renderFileCard(message)).join('');
    }
  }

  function renderFileTable(items) {
    const rows = items.map((message) => {
      const file = message.file;
      const isSelected = selectedIds.has(message.id);
      const icon = file.is_previewable
        ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8" cy="9" r="1.5"/><path d="m5 17 4-4 3 3 2-2 5 4"/></svg>'
        : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v5h5M10 13h5M10 17h5"/></svg>';
      return `
        <tr data-message-id="${escapeAttr(message.id)}" data-name="${escapeAttr(file.name)}" data-type="${file.is_previewable ? 'image' : 'document'}">
          <td><input class="check" type="checkbox" data-select-message="${escapeAttr(message.id)}" ${isSelected ? 'checked' : ''} aria-label="选择 ${escapeAttr(file.name)}"></td>
          <td>
            <div class="file-cell">
              <span class="file-icon">${icon}</span>
              <div class="file-name">
                <strong>${escapeHtml(file.name)}</strong>
                <span>${escapeHtml(file.size)} · ${escapeHtml(file.created_at || message.created_at || '')}</span>
              </div>
            </div>
          </td>
          <td class="mono">${escapeHtml(file.size)}</td>
          <td class="mono">${escapeHtml(file.created_at || message.created_at || '')}</td>
          <td><span class="status">可用</span></td>
          <td>
            <div class="row-actions">
              <button class="row-action" type="button" data-file-action="copy" data-message-id="${escapeAttr(message.id)}" aria-label="复制链接">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>
              </button>
              ${file.is_previewable ? `<button class="row-action" type="button" data-file-action="preview" data-message-id="${escapeAttr(message.id)}" aria-label="预览 ${escapeAttr(file.name)}">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z"/><circle cx="12" cy="12" r="2.5"/></svg>
              </button>` : ''}
            </div>
          </td>
        </tr>`;
    }).join('');

    return `
      <table class="file-table">
        <thead>
          <tr>
            <th class="col-check"><span class="visually-hidden">选择</span></th>
            <th>文件</th>
            <th>大小</th>
            <th>更新时间</th>
            <th>状态</th>
            <th class="col-actions"><span class="visually-hidden">操作</span></th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  function renderFileCard(message) {
    const file = message.file;
    const classes = `${viewMode === 'grid' ? 'file-card grid-card' : 'file-card list-card'}${selectedIds.has(message.id) ? ' selected' : ''}`;
    const media = file.is_previewable
      ? `<button class="media btn-plain" type="button" data-file-action="preview" data-message-id="${escapeAttr(message.id)}" aria-label="预览 ${escapeAttr(file.name)}"><img src="${escapeAttr(file.download_url)}" alt="${escapeAttr(file.name)}"></button>`
      : `<div class="media fallback">${documentBadge(file.extension)}</div>`;

    return `
      <article class="${classes}">
        ${media}
        <div class="card-body">
          <div class="card-top">
            <label class="select-line">
              <input type="checkbox" data-select-message="${escapeAttr(message.id)}" ${selectedIds.has(message.id) ? 'checked' : ''} aria-label="选择 ${escapeAttr(file.name)}">
              选择文件
            </label>
            <div class="file-tag">${escapeHtml(file.is_previewable ? 'image' : (file.extension || 'file').replace('.', '') || 'file')}</div>
            <div class="file-name" title="${escapeAttr(file.name)}">${escapeHtml(file.name)}</div>
            <div class="file-sub">
              <span>${escapeHtml(file.size)}</span>
              <span>${escapeHtml(file.created_at || message.created_at || '')}</span>
            </div>
            <div class="file-sub" title="SHA-256: ${escapeAttr(file.sha256 || '-')}">
              <span>sha256 ${escapeHtml(shortHash(file.sha256))}</span>
            </div>
          </div>
          ${viewMode === 'grid' ? `<div class="file-actions">${renderFileActionButtons(message)}</div>` : ''}
        </div>
        ${viewMode === 'list' ? `<div class="file-actions">${renderFileActionButtons(message)}</div>` : ''}
      </article>
    `;
  }

  function renderFileActionButtons(message) {
    const file = message.file;
    const previewButton = file.is_previewable
      ? `<button class="btn btn-soft" type="button" data-file-action="preview" data-message-id="${escapeAttr(message.id)}">预览</button>`
      : '';
    return `
      <button class="btn btn-soft" type="button" data-file-action="copy" data-message-id="${escapeAttr(message.id)}">复制链接</button>
      <a class="btn btn-primary" href="${escapeAttr(file.download_url)}">下载</a>
      ${previewButton}
      <button class="btn btn-soft" type="button" data-file-action="locate" data-message-id="${escapeAttr(message.id)}">定位消息</button>
      <button class="btn btn-danger" type="button" data-file-action="delete" data-message-id="${escapeAttr(message.id)}">删除</button>
    `;
  }

  function documentBadge(extension) {
    return `
      <div class="doc-badge" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
          <path d="M14 2v6h6"></path>
          <path d="M8 13h8"></path>
          <path d="M8 17h6"></path>
        </svg>
        <span>${escapeHtml((extension || 'file').replace('.', '') || 'file')}</span>
      </div>
    `;
  }

  function shortHash(value) {
    return value ? `${value.slice(0, 10)}...` : '-';
  }

  const _escDiv = document.createElement('div');
  function escapeHtml(value) {
    _escDiv.textContent = value || '';
    return _escDiv.innerHTML;
  }

  function escapeAttr(value) {
    return String(value || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function showToast(message, type) {
    const toastEl = document.getElementById('toast');
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.className = `toast ${type} show`;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toastEl.classList.remove('show'), TOAST_DURATION_MS);
  }

  return {
    load,
    loadMore,
    applyEvent,
    clearSelection,
    destroy,
    openMessage,
    getFiles: () => filesState,
    reloadFromStart,
  };
}
