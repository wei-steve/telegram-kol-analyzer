function escapeHtml(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function getConversationKey() {
  const chatIdInput = document.querySelector('[name="chat_id"]');
  const chatId = chatIdInput ? chatIdInput.value : '0';
  return `telegram-workbench:${chatId}:current_group`;
}

function getPromptKey(chatId = null) {
  const resolvedChatId = chatId || document.querySelector('[name="chat_id"]')?.value || '0';
  return `telegram-workbench:prompt:${resolvedChatId}`;
}

function loadGroupPrompt(chatId = null) {
  try {
    return window.localStorage.getItem(getPromptKey(chatId)) || '';
  } catch {
    return '';
  }
}

function saveGroupPrompt(value, chatId = null) {
  try {
    window.localStorage.setItem(getPromptKey(chatId), value || '');
  } catch {
    // ignore storage failures in local-only mode
  }
}

function applyGroupPromptToEditor(chatId = null) {
  const promptInput = document.querySelector('[data-group-prompt]');
  if (!promptInput) {
    return;
  }
  promptInput.value = loadGroupPrompt(chatId);
}

function loadConversationHistory() {
  try {
    const raw = window.localStorage.getItem(getConversationKey());
    const history = raw ? JSON.parse(raw) : [];
    const { history: migratedHistory, changed } = migrateConversationHistory(history);
    if (changed) {
      saveConversationHistory(migratedHistory);
    }
    return migratedHistory;
  } catch {
    return [];
  }
}

function migrateConversationHistory(history) {
  if (!Array.isArray(history)) {
    return { history: [], changed: true };
  }

  let changed = false;
  const migratedHistory = history.map((entry) => {
    const normalizedAnswer = normalizeAiAnswerText(entry.answer || '');
    const normalizedSources = isImageInputErrorText(normalizedAnswer) ? [] : (entry.sources || []);
    if (normalizedAnswer !== (entry.answer || '')) {
      changed = true;
    }
    if (normalizedSources !== (entry.sources || [])) {
      changed = true;
    }
    return {
      ...entry,
      answer: normalizedAnswer,
      sources: isImageInputErrorText(normalizedAnswer) ? [] : (entry.sources || []),
    };
  });

  return { history: migratedHistory, changed };
}

function saveConversationHistory(history) {
  try {
    window.localStorage.setItem(getConversationKey(), JSON.stringify(history));
  } catch {
    // ignore storage failures in local-only mode
  }
}

function clearConversationHistory() {
  try {
    window.localStorage.removeItem(getConversationKey());
  } catch {
    // ignore storage failures in local-only mode
  }
}

function normalizeAiAnswerText(answer) {
  const text = String(answer || '');
  if (isImageInputErrorText(text)) {
    return '当前模型不支持直接图片理解，本次分析会优先基于文字消息与 OCR 内容。';
  }
  return text;
}

function isImageInputErrorText(answer) {
  const lowered = String(answer || '').toLowerCase();
  return lowered.includes('does not support image input');
}

function renderConversationHistory() {
  const container = document.querySelector('[data-ai-history]');
  if (!container) {
    return;
  }
  const history = loadConversationHistory();
  if (history.length === 0) {
    container.innerHTML = '<div class="history-empty">No messages yet.</div>';
    return;
  }
  container.innerHTML = history
    .map((entry) => {
      const normalizedAnswer = normalizeAiAnswerText(entry.answer || '');
      const shouldShowSources = !isImageInputErrorText(normalizedAnswer);
      return `
      <article class="history-turn">
        <div class="history-question-block">
          ${renderHistoryTimestamp(entry.createdAt)}
          <div class="history-content">${escapeHtml(entry.question || '')}</div>
        </div>
        <div class="history-answer-block">
          <div class="history-content">${renderCitations(normalizedAnswer, shouldShowSources ? (entry.sources || []) : [])}</div>
          ${shouldShowSources ? renderHistorySources(entry.sources || []) : ''}
        </div>
      </article>
    `;
    })
    .join('');
  bindCitationClicks(container);
  scrollAiHistoryToLatest();
}

function renderHistoryTimestamp(value) {
  if (!value) {
    return '';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  const hour = String(date.getHours()).padStart(2, '0');
  const minute = String(date.getMinutes()).padStart(2, '0');
  return `<div class="history-timestamp">${year}-${month}-${day} ${hour}:${minute}</div>`;
}

function renderHistorySources(sources) {
  if (!sources || sources.length === 0) {
    return '';
  }
  return `
    <details class="history-sources" open>
      <summary>Sources (${sources.length})</summary>
      <div class="history-sources-list">${renderSourceList(sources)}</div>
    </details>
  `;
}

function renderCitations(answer, sources) {
  const sourceMap = new Map((sources || []).map((source) => [String(source.index), source]));
  const escapedAnswer = escapeHtml(answer || '');
  return escapedAnswer.replace(/\[(\d+)\]/g, (_, index) => {
    const source = sourceMap.get(index);
    if (!source || !source.raw_message_id) {
      return `[${index}]`;
    }
    return `<button type="button" class="citation-link" data-target-id="message-${source.raw_message_id}">[${index}]</button>`;
  });
}

function renderSourceList(sources) {
  if (!sources || sources.length === 0) {
    return '';
  }
  return sources
    .map((source) => {
      const targetId = source.raw_message_id ? `message-${source.raw_message_id}` : '';
      const button = targetId
        ? `<button type="button" class="source-jump" data-target-id="${targetId}">${escapeHtml(source.label)}</button>`
        : `<span>${escapeHtml(source.label)}</span>`;
      return `<div class="source-item">${button}</div>`;
    })
    .join('');
}

function bindCitationClicks(container) {
  container.querySelectorAll('[data-target-id]').forEach((element) => {
    element.addEventListener('click', () => {
      const target = document.getElementById(element.dataset.targetId);
      if (!target) {
        return;
      }
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      target.classList.add('message-card-highlight');
      window.setTimeout(() => target.classList.remove('message-card-highlight'), 1500);
    });
  });
}

function getAiHistoryScrollContainer() {
  return document.querySelector('[data-ai-history-scroll]');
}

function scrollAiHistoryToLatest() {
  const container = getAiHistoryScrollContainer();
  if (!container) {
    return;
  }
  container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' });
}

function setAiStatus(message, isError = false) {
  const status = document.querySelector('[data-ai-status]');
  if (!status) {
    return;
  }
  status.textContent = message || '';
  status.classList.toggle('is-error', Boolean(message) && isError);
  status.classList.toggle('is-active', Boolean(message));
}

function getMessagePanel() {
  return document.querySelector('[data-messages-panel]');
}

function getMessageFilterState(panel = getMessagePanel()) {
  const filterForm = panel ? panel.querySelector('[data-message-filters]') : null;
  if (!panel || !filterForm) {
    return { chatId: 0, searchText: '', senderName: '' };
  }
  const searchInput = filterForm.querySelector('[name="search_text"]');
  const senderInput = filterForm.querySelector('[name="sender_name"]');
  return {
    chatId: Number(panel.dataset.chatId || '0'),
    searchText: searchInput ? searchInput.value.trim() : '',
    senderName: senderInput ? senderInput.value.trim() : '',
  };
}

function getLatestMessageId(panel = getMessagePanel()) {
  if (!panel) {
    return 0;
  }
  return Number(panel.dataset.latestMessageId || '0');
}

function buildMessagesUrl(chatId, options = {}) {
  const params = new URLSearchParams();
  if (options.beforeMessageId) {
    params.set('before_message_id', String(options.beforeMessageId));
  }
  if (options.searchText) {
    params.set('search_text', options.searchText);
  }
  if (options.senderName) {
    params.set('sender_name', options.senderName);
  }
  const query = params.toString();
  return query ? `/groups/${chatId}/messages?${query}` : `/groups/${chatId}/messages`;
}

async function fetchMessagePanel(chatId, options = {}) {
  const response = await fetch(buildMessagesUrl(chatId, options));
  const html = await response.text();
  const parser = new DOMParser();
  const doc = parser.parseFromString(html, 'text/html');
  return doc.querySelector('[data-messages-panel]');
}

function bindMessagePanelControls(panel = getMessagePanel()) {
  if (!panel) {
    return;
  }
  const filterForm = panel.querySelector('[data-message-filters]');
  if (filterForm) {
    filterForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const { chatId, searchText, senderName } = getMessageFilterState(panel);
      const nextPanel = await fetchMessagePanel(chatId, { searchText, senderName });
      const currentPanel = getMessagePanel();
      if (currentPanel && nextPanel) {
        currentPanel.replaceWith(nextPanel);
        bindMessagePanelControls(nextPanel);
      }
    });
  }

  const clearButton = panel.querySelector('[data-clear-message-filters]');
  if (clearButton && filterForm) {
    clearButton.addEventListener('click', async () => {
      const searchInput = filterForm.querySelector('[name="search_text"]');
      const senderInput = filterForm.querySelector('[name="sender_name"]');
      if (searchInput) {
        searchInput.value = '';
      }
      if (senderInput) {
        senderInput.value = '';
      }
      const { chatId } = getMessageFilterState(panel);
      const nextPanel = await fetchMessagePanel(chatId);
      const currentPanel = getMessagePanel();
      if (currentPanel && nextPanel) {
        currentPanel.replaceWith(nextPanel);
        bindMessagePanelControls(nextPanel);
      }
    });
  }

  const loadMoreButton = panel.querySelector('[data-load-more]');
  if (loadMoreButton) {
    loadMoreButton.addEventListener('click', async () => {
      const { chatId, searchText, senderName } = getMessageFilterState(panel);
      const nextPanel = await fetchMessagePanel(chatId, {
        beforeMessageId: Number(loadMoreButton.dataset.beforeMessageId),
        searchText,
        senderName,
      });
      const nextList = nextPanel ? nextPanel.querySelector('[data-message-list]') : null;
      const currentList = panel.querySelector('[data-message-list]');
      if (currentList && nextList) {
        currentList.insertAdjacentHTML('beforeend', nextList.innerHTML);
      }
      const nextLoadMore = nextPanel ? nextPanel.querySelector('[data-load-more]') : null;
      const currentFooter = panel.querySelector('.message-list-footer');
      const nextFooter = nextPanel ? nextPanel.querySelector('.message-list-footer') : null;
      if (currentFooter && nextFooter) {
        currentFooter.replaceWith(nextFooter);
      }
      bindMessagePanelControls(panel);
    });
  }

  const refreshButton = panel.querySelector('[data-refresh-now]');
  if (refreshButton) {
    refreshButton.addEventListener('click', async () => {
      refreshButton.disabled = true;
      setAiStatus('Refreshing Telegram data...');
      try {
        const response = await fetch('/api/refresh', { method: 'POST' });
        const payload = await response.json();
        if (!response.ok) {
          const detail = payload && typeof payload.detail === 'string'
            ? payload.detail
            : 'Refresh failed.';
          setAiStatus(detail, true);
          return;
        }
        await refreshCurrentGroupPanel();
        setAiStatus(`Refresh complete. Inserted ${payload.inserted_messages || 0} new message(s).`);
      } catch {
        setAiStatus('Refresh failed. Please check Telegram credentials and try again.', true);
      } finally {
        refreshButton.disabled = false;
      }
    });
  }
}

function bindGroupLinks() {
  document.querySelectorAll('[data-group-link]').forEach((element) => {
    element.addEventListener('click', async () => {
      const chatId = Number(element.dataset.chatId);
      const nextPanel = await fetchMessagePanel(chatId);
      const currentPanel = document.querySelector('[data-messages-panel]');
      if (nextPanel && currentPanel) {
        currentPanel.replaceWith(nextPanel);
        bindMessagePanelControls(nextPanel);
      }
      document.querySelectorAll('[data-group-link]').forEach((button) => button.classList.remove('is-active'));
      element.classList.add('is-active');
      const chatIdInput = document.querySelector('[name="chat_id"]');
      if (chatIdInput) {
        chatIdInput.value = String(chatId);
      }
      setAiStatus('Group switched. Ask a new question or continue the conversation.');
      applyGroupPromptToEditor(String(chatId));
      renderConversationHistory();
    });
  });
}

function bindGroupPromptEditor() {
  const promptInput = document.querySelector('[data-group-prompt]');
  if (!promptInput) {
    return;
  }
  applyGroupPromptToEditor();
  const persist = () => {
    const chatId = document.querySelector('[name="chat_id"]')?.value || '0';
    saveGroupPrompt(promptInput.value, chatId);
  };
  promptInput.addEventListener('input', persist);
  promptInput.addEventListener('change', persist);
}

function bindClearAiHistory() {
  const clearButton = document.querySelector('[data-clear-ai-history]');
  if (!clearButton) {
    return;
  }
  clearButton.addEventListener('click', () => {
    clearConversationHistory();
    renderConversationHistory();
    setAiStatus('Current group conversation cleared.');
  });
}

async function submitAiQuestion(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const questionInput = form.querySelector('[name="question"]');
  const chatIdInput = form.querySelector('[name="chat_id"]');
  const groupPromptInput = document.querySelector('[data-group-prompt]');
  const question = questionInput ? questionInput.value.trim() : '';

  if (!question) {
    setAiStatus('Please enter a question before sending.', true);
    return;
  }

  setAiStatus('Analyzing the latest context...');
  const submitButton = form.querySelector('button[type="submit"]');
  if (submitButton) {
    submitButton.disabled = true;
  }
  if (questionInput) {
    questionInput.disabled = true;
  }

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question,
        chat_id: Number(chatIdInput.value),
        group_prompt: groupPromptInput ? groupPromptInput.value : '',
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      const detail = payload && typeof payload.detail === 'string'
        ? payload.detail
        : 'AI request failed. Please check the proxy connection and try again.';
      setAiStatus(detail, true);
      return;
    }
    const history = loadConversationHistory();
    const normalizedAnswer = normalizeAiAnswerText(payload.answer || '');
    const normalizedSources = isImageInputErrorText(normalizedAnswer) ? [] : (payload.sources || []);
    history.push({
      question,
      answer: normalizedAnswer,
      sources: normalizedSources,
      createdAt: new Date().toISOString(),
    });
    saveConversationHistory(history);
    renderConversationHistory();
    setAiStatus('Analysis added to the conversation.');
    questionInput.value = '';
  } catch {
    setAiStatus('AI request failed. Please check the proxy connection and try again.', true);
  } finally {
    if (submitButton) {
      submitButton.disabled = false;
    }
    if (questionInput) {
      questionInput.disabled = false;
      questionInput.focus();
    }
  }
}

async function refreshCurrentGroupPanel() {
  const currentPanel = getMessagePanel();
  if (!currentPanel) {
    return;
  }
  const { chatId, searchText, senderName } = getMessageFilterState(currentPanel);
  const currentLatestMessageId = getLatestMessageId(currentPanel);
  const nextPanel = await fetchMessagePanel(chatId, { searchText, senderName });
  if (!nextPanel) {
    return;
  }
  const nextLatestMessageId = getLatestMessageId(nextPanel);
  if (nextLatestMessageId <= currentLatestMessageId) {
    return;
  }
  currentPanel.replaceWith(nextPanel);
  bindMessagePanelControls(nextPanel);
  setAiStatus('New group messages loaded automatically.');
}

function connectLiveUpdates() {
  if (window.EventSource) {
    const source = new EventSource('/api/events');
    source.addEventListener('message', async (event) => {
      let payload = null;
      try {
        payload = JSON.parse(event.data || '{}');
      } catch {
        payload = null;
      }
      const currentPanel = getMessagePanel();
      if (!currentPanel || !payload) {
        return;
      }
      const currentChatId = Number(currentPanel.dataset.chatId || '0');
      if (Number(payload.chat_id || 0) !== currentChatId) {
        return;
      }
      await refreshCurrentGroupPanel();
    });
    source.onerror = () => {
      setAiStatus('实时连接中断，已退回轮询刷新。', true);
      source.close();
      window.setInterval(refreshCurrentGroupPanel, 15000);
    };
    return;
  }

  window.setInterval(refreshCurrentGroupPanel, 15000);
}

window.addEventListener('DOMContentLoaded', () => {
  const form = document.querySelector('[data-ai-form]');
  if (form) {
    form.addEventListener('submit', submitAiQuestion);
  }
  bindGroupLinks();
  bindMessagePanelControls();
  bindGroupPromptEditor();
  bindClearAiHistory();
  renderConversationHistory();
  setAiStatus('Ready to analyze the current group.');
  connectLiveUpdates();
});
