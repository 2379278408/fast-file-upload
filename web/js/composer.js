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
  let destroyed = false;
  let dragCounter = 0;

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
    if (destroyed) return;
    const text = textarea.value;
    if (!text.trim()) return;
    if (text.length > MAX_TEXT_LENGTH) return showComposerError('文本最多 10,000 个字符');
    try {
      const message = await sendText(text, generateUUID());
      if (destroyed) return;
      textarea.value = '';
      timeline.upsert(message);
    } catch (error) {
      if (destroyed) return;
      showComposerError(error.message);
    }
  }

  function handleKeydown(event) {
    if (event.isComposing) return;
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submitText(); }
  }

  function handleSubmit(event) {
    event.preventDefault();
    submitText();
  }

  textarea.addEventListener('keydown', handleKeydown);

  if (form) form.addEventListener('submit', handleSubmit);

  function enqueueFiles(files) {
    if (destroyed) return undefined;
    return uploadCoordinator.enqueueFiles(Array.from(files));
  }

  function handleFileChange(event) {
    enqueueFiles(Array.from(event.target.files));
    event.target.value = '';
  }

  function handleDragEnter(event) {
    event.preventDefault();
    dragCounter++;
    dropTarget.classList.add('dragover');
  }

  function handleDragOver(event) {
    event.preventDefault();
  }

  function handleDragLeave(event) {
    event.preventDefault();
    dragCounter--;
    if (dragCounter <= 0) {
      dragCounter = 0;
      dropTarget.classList.remove('dragover');
    }
  }

  function handleDrop(event) {
    event.preventDefault();
    dragCounter = 0;
    dropTarget.classList.remove('dragover');
    enqueueFiles(Array.from(event.dataTransfer.files));
  }

  function handlePaste(event) {
    if (destroyed) return;
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
  }

  // File input change
  if (fileInput) fileInput.addEventListener('change', handleFileChange);

  // Drag and drop on drop target
  if (dropTarget) {
    dropTarget.addEventListener('dragenter', handleDragEnter);
    dropTarget.addEventListener('dragover', handleDragOver);
    dropTarget.addEventListener('dragleave', handleDragLeave);
    dropTarget.addEventListener('drop', handleDrop);
  }

  // Paste images from clipboard
  document.addEventListener('paste', handlePaste);

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    textarea.removeEventListener('keydown', handleKeydown);
    form?.removeEventListener('submit', handleSubmit);
    fileInput?.removeEventListener('change', handleFileChange);
    dropTarget?.removeEventListener('dragenter', handleDragEnter);
    dropTarget?.removeEventListener('dragover', handleDragOver);
    dropTarget?.removeEventListener('dragleave', handleDragLeave);
    dropTarget?.removeEventListener('drop', handleDrop);
    document.removeEventListener('paste', handlePaste);
    dragCounter = 0;
    dropTarget?.classList.remove('dragover');
    window.clearTimeout(showToast.timer);
    showToast.timer = null;
  }

  return {
    enqueueFiles,
    destroy,
  };
}
