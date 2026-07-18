import { UNDO_WINDOW_MS, HIGHLIGHT_DURATION_MS } from './config.js';

export function createTimeline({ container, newMessageButton, api, onRestore }) {
  const messages = new Map();
  const appliedSequences = new Set();
  let lastSequence = 0;
  let nextBefore = null;
  let exhausted = false;
  let loading = false;
  let loadingPromise = null;
  let observer = null;
  let atBottom = true;
  let newCount = 0;

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

  if (container) {
    container.addEventListener('scroll', () => {
      atBottom = isNearBottom();
      if (atBottom) {
        newCount = 0;
        updateNewButton();
      }
    });
  }

  if (newMessageButton) {
    newMessageButton.hidden = true;
    newMessageButton.addEventListener('click', () => {
      scrollToBottom(true);
    });
  }

  function getLocalDate(ts) {
    const d = new Date(ts);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
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
    copyBtn.addEventListener('click', async () => {
      const text = msg.file
        ? `${location.origin}${msg.file.download_url || `/download/${msg.file.id}`}`
        : (msg.body || '');
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        // fallback: select text
      }
    });
    actions.append(copyBtn);

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'btn btn-soft timeline-delete-btn';
    deleteBtn.dataset.timelineAction = 'delete';
    deleteBtn.type = 'button';
    deleteBtn.textContent = '删除';
    deleteBtn.addEventListener('click', async () => {
      const label = msg.file ? (msg.file.name || '这个文件') : '这条消息';
      if (!confirm(`确定删除${label}吗？`)) return;
      try {
        const deleted = await api(`/api/messages/${encodeURIComponent(msg.id)}`, {
          method: 'DELETE',
        });
        messages.delete(msg.id);
        const rendered = container && container.querySelector(`[data-message-id="${msg.id}"]`);
        if (rendered) rendered.remove();
        showUndo({ ...msg, ...(deleted || {}) });
      } catch {
        // The event stream keeps the timeline consistent after request failures.
      }
    });
    actions.append(deleteBtn);
    el.append(actions);

    return el;
  }

  function showUndo(message) {
    if (!container) return;
    const existing = container.querySelector(
      `.timeline-undo[data-undo-message-id="${message.id}"]`
    );
    if (existing) existing.remove();

    const notice = document.createElement('div');
    notice.className = 'timeline-undo';
    notice.dataset.undoMessageId = message.id;
    const label = document.createElement('span');
    label.textContent = '已删除，可在 30 秒内撤销';
    const undoButton = document.createElement('button');
    undoButton.className = 'btn btn-soft timeline-undo-btn';
    undoButton.type = 'button';
    undoButton.textContent = '撤销';
    notice.append(label, undoButton);
    container.append(notice);

    const deletedAt = Date.parse(message.deleted_at || '');
    const undoMilliseconds = Number.isFinite(deletedAt)
      ? Math.max(0, deletedAt + UNDO_WINDOW_MS - Date.now())
      : UNDO_WINDOW_MS;
    const expiryTimer = setTimeout(() => notice.remove(), undoMilliseconds);
    undoButton.addEventListener('click', async () => {
      undoButton.disabled = true;
      try {
        const restored = await api(
          `/api/messages/${encodeURIComponent(message.id)}/restore`,
          { method: 'POST' }
        );
        clearTimeout(expiryTimer);
        notice.remove();
        upsertMessage(restored);
      } catch {
        undoButton.disabled = false;
      }
    });
  }

  function insertMessageSorted(el) {
    const children = Array.from(container.querySelectorAll('.timeline-message'));
    const createdAt = el.dataset.createdAt || '';
    const msgId = el.dataset.messageId || '';
    let inserted = false;
    for (const child of children) {
      const childCreatedAt = child.dataset.createdAt || '';
      const childMsgId = child.dataset.messageId || '';
      if (createdAt < childCreatedAt || (createdAt === childCreatedAt && msgId < childMsgId)) {
        container.insertBefore(el, child);
        inserted = true;
        break;
      }
    }
    if (!inserted) {
      container.append(el);
    }
  }

  function insertDateSeparator(dateStr) {
    const existing = container.querySelector(`.timeline-date-separator[data-date="${dateStr}"]`);
    if (existing) return;
    const sep = createDateSeparator(dateStr);
    sep.dataset.date = dateStr;
    const separators = Array.from(container.querySelectorAll('.timeline-date-separator'));
    let inserted = false;
    for (const existingSep of separators) {
      if (dateStr < (existingSep.dataset.date || '')) {
        container.insertBefore(sep, existingSep);
        inserted = true;
        break;
      }
    }
    if (!inserted) {
      container.append(sep);
    }
  }

  function mergeEvent(event) {
    if (appliedSequences.has(event.sequence)) return false;
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
        messages.set(event.entity_id, msg);
        if (container) {
          const existing = container.querySelector(`[data-message-id="${event.entity_id}"]`);
          if (!existing) {
            const el = renderMessage(msg);
            el.dataset.sequence = String(event.sequence);
            const dateStr = getLocalDate(msg.created_at || Date.now());
            insertDateSeparator(dateStr);
            insertMessageSorted(el);
            if (!atBottom) {
              newCount++;
              updateNewButton();
            }
          }
        }
        break;
      }
      case 'message.deleted': {
        messages.delete(event.entity_id);
        if (container) {
          const el = container.querySelector(`[data-message-id="${event.entity_id}"]`);
          if (el) {
            const dateStr = el.dataset.createdAt
              ? getLocalDate(el.dataset.createdAt)
              : null;
            el.remove();
            if (dateStr) {
              const remaining = container.querySelector(
                `.timeline-message[data-created-at^="${dateStr}"]`
              );
              if (!remaining) {
                const sep = container.querySelector(
                  `.timeline-date-separator[data-date="${dateStr}"]`
                );
                if (sep) sep.remove();
              }
            }
          }
        }
        break;
      }
      case 'message.restored': {
        const msg = { ...payload, id: event.entity_id };
        messages.set(event.entity_id, msg);
        if (container) {
          const undoNotice = container.querySelector(`.timeline-undo[data-undo-message-id="${event.entity_id}"]`);
          if (undoNotice) undoNotice.remove();

          const existing = container.querySelector(`[data-message-id="${event.entity_id}"]`);
          if (existing) {
            const replacement = renderMessage(msg);
            replacement.dataset.sequence = existing.dataset.sequence;
            existing.replaceWith(replacement);
          } else {
            const el = renderMessage(msg);
            el.dataset.sequence = String(event.sequence);
            const dateStr = getLocalDate(msg.created_at || Date.now());
            insertDateSeparator(dateStr);
            insertMessageSorted(el);
          }
        }
        break;
      }
      case 'file.finalized': {
        const fileId = event.entity_id;
        for (const [msgId, msg] of messages) {
          if (msg.file && msg.file.id === fileId) {
            msg.file = { ...msg.file, ...payload, id: fileId };
            if (container) {
              const el = container.querySelector(`[data-message-id="${msgId}"]`);
              if (el) {
                const replacement = renderMessage(msg);
                replacement.dataset.sequence = el.dataset.sequence;
                el.replaceWith(replacement);
              }
            }
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
    if (!container) return false;
    const el = container.querySelector(`[data-message-id="${messageId}"]`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('timeline-message-highlight');
      setTimeout(() => el.classList.remove('timeline-message-highlight'), HIGHLIGHT_DURATION_MS);
      return true;
    }
    return false;
  }

  async function loadInitial() {
    if (!container) return;
    container.innerHTML = '';
    messages.clear();
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

    await loadOlder();
    scrollToBottom(false);

    if (onRestore) await onRestore();
  }

  function loadOlder() {
    if (loadingPromise) return loadingPromise;
    if (exhausted) return Promise.resolve(false);
    loading = true;
    loadingPromise = (async () => {
      try {
        const url = nextBefore
          ? `/api/messages?limit=50&before=${encodeURIComponent(nextBefore)}`
          : '/api/messages?limit=50';
        const data = await api(url);
        if (!data) return false;
        const items = data.items || [];
        if (items.length === 0) {
          nextBefore = null;
          exhausted = true;
          return false;
        }
        nextBefore = data.next_before || null;
        exhausted = !nextBefore;
        const prevScrollHeight = container ? container.scrollHeight : 0;
        const pageNodes = [];
        const pageDates = new Set();

        for (const msg of items) {
          if (messages.has(msg.id)) continue;
          messages.set(msg.id, msg);
          if (container) {
            const el = renderMessage(msg);
            const dateStr = getLocalDate(msg.created_at || Date.now());
            if (
              !pageDates.has(dateStr)
              && !container.querySelector(`.timeline-date-separator[data-date="${dateStr}"]`)
            ) {
              const separator = createDateSeparator(dateStr);
              separator.dataset.date = dateStr;
              pageNodes.push(separator);
              pageDates.add(dateStr);
            }
            pageNodes.push(el);
          }
        }

        if (container) {
          for (const node of pageNodes.reverse()) {
            container.insertBefore(node, container.children[1] || null);
          }
          const newScrollHeight = container.scrollHeight;
          container.scrollTop += newScrollHeight - prevScrollHeight;
        }
        return true;
      } catch {
        window.dispatchEvent(new CustomEvent('timeline-error', { detail: { message: '加载历史消息失败，请稍后重试。' } }));
        return false;
      } finally {
        loading = false;
        loadingPromise = null;
      }
    })();
    return loadingPromise;
  }

  function hasMessageElement(messageId) {
    return Boolean(
      container && container.querySelector(`[data-message-id="${messageId}"]`)
    );
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

  function upsertMessage(message) {
    if (!message || !message.id) return;
    const existingMessage = messages.get(message.id);
    const merged = {
      ...existingMessage,
      ...message,
      file: message.file || (existingMessage && existingMessage.file) || null,
    };
    messages.set(message.id, merged);
    if (!container) return;

    const existingElement = container.querySelector(`[data-message-id="${message.id}"]`);
    const element = renderMessage(merged);
    if (existingElement) {
      element.dataset.sequence = existingElement.dataset.sequence;
      existingElement.replaceWith(element);
      return;
    }

    insertDateSeparator(getLocalDate(merged.created_at || Date.now()));
    insertMessageSorted(element);
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
  };
}
