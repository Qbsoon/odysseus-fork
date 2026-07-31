/**
 * ArrowUp on the composer recalls previous user messages from this chat.
 *
 * History recall only takes over the arrow keys once the caret sits on the
 * boundary row of the composer (first row for ArrowUp, last row for
 * ArrowDown); inside the text the keys keep their native caret movement.
 */

/**
 * User bubbles in the active chat surface (#chat-history), newest first, using
 * dataset.raw (same source as resend/regenerate in chat.js).
 *
 * @param {Document | Element} [root=document]
 * @returns {string[]}
 */
export function getUserMessagesFromChatHistory(root = document) {
  const chatBox =
    root && root.id === 'chat-history' && typeof root.querySelectorAll === 'function'
      ? root
      : (root.getElementById ? root.getElementById('chat-history') : null);
  if (!chatBox) return [];

  const users = chatBox.querySelectorAll('.msg-user');
  const prompts = [];
  for (let i = users.length - 1; i >= 0; i--) {
    const msg = users[i];
    const bodyEl = msg.querySelector('.body');
    const text = msg.dataset?.raw || (bodyEl ? bodyEl.textContent : '') || '';
    if (text) prompts.push(text);
  }
  return prompts;
}

/**
 * Last user bubble in the active chat surface (#chat-history).
 *
 * @param {Document | Element} [root=document]
 * @returns {string}
 */
export function getLastUserMessageFromChatHistory(root = document) {
  return getUserMessagesFromChatHistory(root)[0] || '';
}

/**
 * Calculate the row index of the caret in a textarea
 * to determine if the arrow keys should recall history or move the caret.
 *
 * @param {HTMLTextAreaElement | any} el
 * @returns {{ startRow: number, endRow: number, rows: number }}
 */
function caretRowInfo(el) {
  const value = String(el && el.value != null ? el.value : '');
  const start = Number.isFinite(el?.selectionStart) ? el.selectionStart : value.length;
  const end = Number.isFinite(el?.selectionEnd) ? el.selectionEnd : start;
  const rowOf = (pos) => value.slice(0, pos).split('\n').length - 1;
  return {
    startRow: rowOf(start),
    endRow: rowOf(end),
    rows: value.split('\n').length,
  };
}

/**
 * @param {HTMLTextAreaElement} composer
 * @param {() => string|string[]} getUserMessages
 * @param {{ autoResize?: (el: HTMLTextAreaElement) => void }} [options]
 * @returns {boolean} true when wired (or already wired)
 */
export function wireArrowUpRecall(composer, getUserMessages, options = {}) {
  if (!composer) return false;
  if (composer._arrowUpRecallWired) return true;
  composer._arrowUpRecallWired = true;

  const { autoResize } = options;
  let recallIndex = -1;
  let applyingRecall = false;
  let lastRecalledValue = '';
  let recallHistory = [];

  const readHistory = () => {
    const value = getUserMessages?.();
    if (Array.isArray(value)) return value.filter(Boolean);
    return value ? [value] : [];
  };
  const norm = (value) => String(value || '').replace(/\r\n/g, '\n').trimEnd();
  const debug = (...args) => {
    try {
      if (localStorage.getItem('odysseusArrowRecallDebug') === '1') {
        console.debug('[arrow-recall]', ...args);
      }
    } catch (_) {}
  };

  composer.addEventListener('input', () => {
    if (applyingRecall) return;
    if (norm(composer.value) === norm(lastRecalledValue)) return;
    recallIndex = -1;
    lastRecalledValue = '';
    recallHistory = [];
    try { delete composer.dataset.odysseusRecallIndex; } catch (_) {}
  });

  composer.addEventListener('keydown', (e) => {
    // Prompt history: ArrowUp walks older, ArrowDown walks newer/back to blank.
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    if (e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.isComposing) return;
    if (typeof window !== 'undefined' && window._ghostAutocomplete?.isActive?.()) return;

    // Ensures ArrowUp/ArrowDown only take over when the caret is on the first/last row of the composer.
    const rows = caretRowInfo(composer);
    if (e.key === 'ArrowUp' && (rows.startRow > 0 || rows.endRow > 0)) return;
    if (e.key === 'ArrowDown' &&
        (rows.startRow < rows.rows - 1 || rows.endRow < rows.rows - 1)) return;

    const freshHistory = readHistory();
    const history = freshHistory.length ? freshHistory : recallHistory;
    if (!history.length) {
      debug('skip:no-history', { value: composer.value });
      return;
    }

    const rawCurrentValue = String(composer.value || '');
    const currentValue = norm(rawCurrentValue);
    const recalledValue = norm(lastRecalledValue);
    let currentIndex = rawCurrentValue === ''
      ? -1
      : history.findIndex((item) => norm(item) === currentValue);
    if (currentIndex < 0 && currentValue && currentValue === recalledValue) {
      currentIndex = recallIndex;
    }
    if (currentIndex < 0 && currentValue) {
      const markedIndex = Number(composer.dataset?.odysseusRecallIndex);
      if (Number.isInteger(markedIndex) && markedIndex >= 0 && markedIndex < history.length) {
        currentIndex = markedIndex;
      }
    }
    if (rawCurrentValue !== '' && currentIndex < 0) {
      debug('skip:draft-in-progress', { value: composer.value });
      return;
    }
    e.preventDefault();
    e.stopPropagation?.();
    e.stopImmediatePropagation?.();
    if (e.key === 'ArrowDown') {
      if (currentIndex < 0) return;
      const nextIndex = currentIndex - 1;
      if (nextIndex < 0) {
        recallIndex = -1;
        recallHistory = history;
        applyingRecall = true;
        lastRecalledValue = '';
        try { delete composer.dataset.odysseusRecallIndex; } catch (_) {}
        composer.value = '';
        try { composer.selectionStart = composer.selectionEnd = 0; } catch (_) {}
        if (autoResize) autoResize(composer);
        debug('handled-down-clear', { historyLength: history.length });
        setTimeout(() => { applyingRecall = false; }, 0);
        return;
      }
      const recalled = history[nextIndex];
      recallIndex = nextIndex;
      recallHistory = history;
      applyingRecall = true;
      lastRecalledValue = recalled;
      try { composer.dataset.odysseusRecallIndex = String(nextIndex); } catch (_) {}
      composer.value = recalled;
      try { composer.selectionStart = composer.selectionEnd = recalled.length; } catch (_) {}
      if (autoResize) autoResize(composer);
      debug('handled-down', { nextIndex, recalled, historyLength: history.length });
      setTimeout(() => { applyingRecall = false; }, 0);
      return;
    }

    // ArrowUp owns prompt history in the chat composer once the caret sits on
    // the first row. If the current text is not already a recalled prompt,
    // start from newest instead of letting the browser move the caret inside the textarea.
    const nextIndex = currentIndex >= 0 ? Math.min(currentIndex + 1, history.length - 1) : 0;
    const recalled = history[nextIndex];
    if (!recalled) {
      debug('skip:no-recalled', { nextIndex, history });
      return;
    }

    recallIndex = nextIndex;
    recallHistory = history;
    applyingRecall = true;
    lastRecalledValue = recalled;
    try { composer.dataset.odysseusRecallIndex = String(nextIndex); } catch (_) {}
    composer.value = recalled;
    try {
      composer.selectionStart = composer.selectionEnd = recalled.length;
    } catch (_) {}
    if (autoResize) autoResize(composer);
    debug('handled', { nextIndex, recalled, historyLength: history.length });
    setTimeout(() => { applyingRecall = false; }, 0);
  }, true);

  return true;
}
