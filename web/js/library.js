export function createLibrary({ root, api, timeline }) {
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

  if (batchDownloadBtn) batchDownloadBtn.addEventListener('click', batchDownload);
  if (batchCopyBtn) batchCopyBtn.addEventListener('click', copySelected);
  if (batchDeleteBtn) batchDeleteBtn.addEventListener('click', batchDelete);
  if (selectVisibleBtn) selectVisibleBtn.addEventListener('click', selectVisibleFiles);
  if (clearSelectionBtn) clearSelectionBtn.addEventListener('click', clearSelection);
  if (gridViewBtn) gridViewBtn.addEventListener('click', () => setViewMode('grid'));
  if (listViewBtn) listViewBtn.addEventListener('click', () => setViewMode('list'));
  for (const button of [gridViewBtn, listViewBtn]) {
    if (button) button.addEventListener('keydown', event => handleViewKeydown(event, button));
  }
  if (clearFiltersBtn) clearFiltersBtn.addEventListener('click', clearFilters);
  if (loadMoreBtn) loadMoreBtn.addEventListener('click', loadMore);

  if (fileListEl) {
    fileListEl.addEventListener('click', handleFileAction);
    fileListEl.addEventListener('change', (event) => {
      const checkbox = event.target.closest('[data-select-message]');
      if (checkbox) toggleFileSelection(checkbox.dataset.selectMessage, checkbox.checked);
    });
  }
  if (previewModal) previewModal.addEventListener('click', (event) => {
    if (event.target === previewModal) closePreview();
  });
  if (closePreviewBtn) closePreviewBtn.addEventListener('click', closePreview);
  if (previewCopyBtn) previewCopyBtn.addEventListener('click', () => {
    if (currentPreview) copyLink(currentPreview);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && previewModal && !previewModal.hidden) closePreview();
  });

  if (searchInput) searchInput.addEventListener('input', debounce(reloadFromStart, 300));
  if (typeSelect) typeSelect.addEventListener('change', reloadFromStart);
  if (deviceInput) deviceInput.addEventListener('input', reloadFromStart);
  if (dateFromInput) dateFromInput.addEventListener('change', reloadFromStart);
  if (dateToInput) dateToInput.addEventListener('change', reloadFromStart);

  function debounce(fn, ms) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
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
    const button = event.target.closest('[data-file-action]');
    if (!button) return;
    const message = getMessage(button.dataset.messageId);
    if (!message) return;
    const action = button.dataset.fileAction;
    if (action === 'preview') openPreview(message, button);
    if (action === 'copy') await copyLink(message);
    if (action === 'delete') await deleteMessage(message);
    if (action === 'locate') await openMessage(message.id);
  }

  function openPreview(message, trigger) {
    const file = message.file;
    if (!file || !file.is_previewable || !previewModal) return;
    currentPreview = message;
    lastPreviewTrigger = trigger || null;
    if (previewImage) {
      previewImage.src = file.download_url;
      previewImage.alt = file.name || '图片预览';
    }
    if (previewTitle) previewTitle.textContent = file.name || '图片预览';
    if (previewSize) previewSize.textContent = file.size || '-';
    if (previewDate) previewDate.textContent = file.created_at || message.created_at || '-';
    if (previewType) previewType.textContent = file.extension || file.mime_type || '-';
    if (previewDownloadBtn) previewDownloadBtn.href = file.download_url;
    previewModal.hidden = false;
    previewModal.classList.add('open');
    if (closePreviewBtn) closePreviewBtn.focus();
  }

  function closePreview() {
    if (!previewModal) return;
    previewModal.hidden = true;
    previewModal.classList.remove('open');
    if (previewImage) previewImage.removeAttribute('src');
    currentPreview = null;
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
    const remaining = Math.max(0, deletedAt + 30000 - Date.now());

    toastEl.textContent = '文件已删除';
    const undoBtn = document.createElement('button');
    undoBtn.className = 'btn btn-soft';
    undoBtn.type = 'button';
    undoBtn.textContent = ' · 撤销';
    undoBtn.addEventListener('click', async () => {
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
    const remaining = 30000;

    if (!deletedIds.length) {
      showToast(`已删除 ${count} 个文件`, 'info');
      return;
    }

    toastEl.textContent = `已删除 ${count} 个文件`;
    const undoBtn = document.createElement('button');
    undoBtn.className = 'btn btn-soft';
    undoBtn.type = 'button';
    undoBtn.textContent = ' · 撤销';
    undoBtn.addEventListener('click', async () => {
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
      fileListEl.innerHTML = '<div class="empty"><div><strong>还没有文件</strong><p>把文件拖进上传区，完成后会显示在这里。</p></div></div>';
      return;
    }

    fileListEl.className = `file-list ${viewMode}-mode`;
    fileListEl.innerHTML = items.map((message) => renderFileCard(message)).join('');
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
              <input type="checkbox" data-select-message="${escapeAttr(message.id)}" ${selectedIds.has(message.id) ? 'checked' : ''}>
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

  function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value || '';
    return div.innerHTML;
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
    showToast.timer = window.setTimeout(() => toastEl.classList.remove('show'), 2600);
  }

  return {
    load,
    loadMore,
    applyEvent,
    clearSelection,
    openMessage,
    getFiles: () => filesState,
    reloadFromStart,
  };
}
