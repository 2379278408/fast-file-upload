import { sendText, uploadFile } from './api.js';
import { MAX_TEXT_LENGTH, TOAST_DURATION_MS } from './config.js';

function generateUUID() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

export function createComposer({ form, textarea, fileInput, dropTarget, queue, api, timeline }) {
  const uploadTasks = [];
  let processing = false;

  function showComposerError(message) {
    showToast(message, 'error');
  }

  function showToast(message, type) {
    const toastEl = document.getElementById('toast');
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.className = `toast ${type} show`;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toastEl.classList.remove('show'), TOAST_DURATION_MS);
  }

  async function submitText() {
    const text = textarea.value;
    if (!text.trim()) return;
    if (text.length > MAX_TEXT_LENGTH) return showComposerError('文本最多 10,000 个字符');
    try {
      const message = await sendText(text, generateUUID());
      textarea.value = '';
      timeline.upsert(message);
    } catch (error) {
      showComposerError(error.message);
    }
  }

  textarea.addEventListener('keydown', event => {
    if (event.isComposing) return;
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submitText(); }
  });

  if (form) {
    form.addEventListener('submit', event => {
      event.preventDefault();
      submitText();
    });
  }

  function createTask(file) {
    return {
      id: generateUUID(),
      file,
      status: 'queued',
      progress: 0,
      error: null,
      controller: new AbortController(),
      clientRequestId: generateUUID(),
    };
  }

  function renderQueue() {
    if (!queue) return;
    queue.innerHTML = uploadTasks.map(task => {
      const statusLabel = {
        queued: '等待中',
        uploading: '上传中',
        failed: '失败',
        complete: '完成',
        cancelled: '已取消',
      }[task.status] || task.status;

      const actions = [];
      if (task.status === 'failed') {
        actions.push(`<button class="btn btn-soft" type="button" data-action="retry" data-task-id="${task.id}">重试</button>`);
      }
      if (task.status === 'queued' || task.status === 'uploading') {
        actions.push(`<button class="btn btn-plain" type="button" data-action="cancel" data-task-id="${task.id}">取消</button>`);
      }

      return `
        <div class="queue-item" data-task-id="${task.id}">
          <div class="queue-row">
            <div>
              <div class="queue-name">${escapeHtml(task.file.name)}</div>
              <div class="queue-meta">${formatBytes(task.file.size)} · ${statusLabel}</div>
            </div>
            <div class="queue-status">
              ${actions.join('')}
              <strong>${task.progress}%</strong>
            </div>
          </div>
          <progress class="progress" max="100" value="${task.progress}" aria-label="上传进度">${task.progress}%</progress>
          ${task.error ? `<div class="queue-error">${escapeHtml(task.error)}</div>` : ''}
        </div>
      `;
    }).join('');
  }

  if (queue) {
    queue.addEventListener('click', event => {
      const btn = event.target.closest('[data-action]');
      if (!btn) return;
      const taskId = btn.dataset.taskId;
      const action = btn.dataset.action;
      if (action === 'retry') retryUpload(taskId);
      if (action === 'cancel') cancelUpload(taskId);
    });
  }

  function cancelUpload(taskId) {
    const task = uploadTasks.find(t => t.id === taskId);
    if (!task) return;
    if (task.status === 'uploading' && task.controller) {
      task.controller.abort();
    }
    task.status = 'cancelled';
    task.progress = 0;
    renderQueue();
  }

  function retryUpload(taskId) {
    const task = uploadTasks.find(t => t.id === taskId);
    if (!task || task.status !== 'failed') return;
    task.status = 'queued';
    task.progress = 0;
    task.error = null;
    task.controller = new AbortController();
    renderQueue();
    processQueue();
  }

  function enqueueFiles(files) {
    for (const file of files) {
      if (file.size <= 0) {
        showToast(`空文件已跳过：${file.name || '未命名文件'}`, 'error');
        continue;
      }
      uploadTasks.push(createTask(file));
    }
    renderQueue();
    processQueue();
  }

  async function processQueue() {
    if (processing) return;
    const next = uploadTasks.find(t => t.status === 'queued');
    if (!next) return;
    processing = true;
    try {
      next.status = 'uploading';
      renderQueue();
      const message = await uploadFile(
        next.file,
        next.clientRequestId,
        (loaded, total) => {
          next.progress = Math.round((loaded / total) * 100);
          renderQueue();
        },
        next.controller.signal,
      );
      next.status = 'complete';
      next.progress = 100;
      if (timeline && message) {
        timeline.upsert(message);
      }
    } catch (error) {
      if (error.name === 'AbortError') {
        // cancelled — status already set
      } else {
        next.status = 'failed';
        next.error = error.message || '上传失败';
      }
    } finally {
      renderQueue();
      processing = false;
      processQueue();
    }
  }

  // File input change
  if (fileInput) {
    fileInput.addEventListener('change', event => {
      enqueueFiles(Array.from(event.target.files));
      event.target.value = '';
    });
  }

  // Drag and drop on drop target
  if (dropTarget) {
    let dragCounter = 0;
    dropTarget.addEventListener('dragenter', event => {
      event.preventDefault();
      dragCounter++;
      dropTarget.classList.add('dragover');
    });

    dropTarget.addEventListener('dragover', event => {
      event.preventDefault();
    });

    dropTarget.addEventListener('dragleave', event => {
      event.preventDefault();
      dragCounter--;
      if (dragCounter <= 0) {
        dragCounter = 0;
        dropTarget.classList.remove('dragover');
      }
    });

    dropTarget.addEventListener('drop', event => {
      event.preventDefault();
      dragCounter = 0;
      dropTarget.classList.remove('dragover');
      enqueueFiles(Array.from(event.dataTransfer.files));
    });
  }

  // Paste images from clipboard
  document.addEventListener('paste', event => {
    const items = event.clipboardData && event.clipboardData.items;
    if (!items) return;
    const files = [];
    for (const item of items) {
      if (item.kind === 'file') {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    if (files.length) {
      event.preventDefault();
      enqueueFiles(files);
    }
  });

  return {
    enqueueFiles,
    cancelUpload,
    retryUpload,
    getUploadTasks: () => uploadTasks,
  };
}

const _escDiv = document.createElement('div');
function escapeHtml(value) {
  _escDiv.textContent = value || '';
  return _escDiv.innerHTML;
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
