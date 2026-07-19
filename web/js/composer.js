import { sendText } from './api.js';
import { MAX_TEXT_LENGTH, TOAST_DURATION_MS } from './config.js';

function generateUUID() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

export function createComposer({ form, textarea, fileInput, dropTarget, api, timeline, uploadCoordinator }) {

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

  function enqueueFiles(files) {
    return uploadCoordinator.enqueueFiles(Array.from(files));
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
    enqueueFiles: files => uploadCoordinator.enqueueFiles(files),
  };
}
