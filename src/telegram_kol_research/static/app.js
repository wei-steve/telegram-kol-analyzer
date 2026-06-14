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
    container.innerHTML = '<div class="history-empty">还没有分析报告。输入一个问题后，这里会生成该群的研究卡片。</div>';
    return;
  }
  container.innerHTML = history
    .map((entry) => {
      const normalizedAnswer = normalizeAiAnswerText(entry.answer || '');
      const shouldShowSources = !isImageInputErrorText(normalizedAnswer);
      return `
      <article class="history-turn ai-report-card">
        <div class="history-question-block ai-report-meta">
          <span class="ai-report-label">提问</span>
          <div class="history-content">${escapeHtml(entry.question || '')}</div>
          ${renderHistoryTimestamp(entry.createdAt)}
        </div>
        <div class="history-answer-block ai-report-body">
          <div class="ai-report-kicker">AI 研究结论</div>
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
    <details class="history-sources">
      <summary>引用消息 ${sources.length} 条</summary>
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

function scrollMessagePanelToTop(panel = getMessagePanel()) {
  if (!panel) {
    return;
  }
  panel.scrollTo({ top: 0, behavior: 'auto' });
}

function resetInitialMessagePanelScroll() {
  scrollMessagePanelToTop();
  window.requestAnimationFrame(() => {
    scrollMessagePanelToTop();
  });
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

function getSelectedChatId() {
  const panel = getMessagePanel();
  if (panel && panel.dataset.chatId) {
    return Number(panel.dataset.chatId || '0');
  }
  const chatIdInput = document.querySelector('[name="chat_id"]');
  return Number(chatIdInput ? chatIdInput.value : '0');
}

async function refreshGroupList() {
  const selectedChatId = getSelectedChatId();
  const response = await fetch('/groups?selected_chat_id=' + selectedChatId);
  const html = await response.text();
  const parser = new DOMParser();
  const doc = parser.parseFromString(html, 'text/html');
  const nextList = doc.querySelector('[data-groups-list]');
  const currentList = document.querySelector('[data-groups-list]');
  if (!nextList || !currentList) {
    return;
  }
  currentList.replaceWith(nextList);
  bindGroupLinks();
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
        scrollMessagePanelToTop(nextPanel);
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
        scrollMessagePanelToTop(nextPanel);
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
        await refreshGroupList();
        setAiStatus(`Refresh complete. Inserted ${payload.inserted_messages || 0} new message(s).`);
      } catch {
        setAiStatus('Refresh failed. Please check Telegram credentials and try again.', true);
      } finally {
        refreshButton.disabled = false;
      }
    });
  }

  panel.querySelectorAll('[data-recognize-message]').forEach((button) => {
    button.addEventListener('click', async () => {
      const rawMessageId = Number(button.dataset.rawMessageId || '0');
      const status = button
        .closest('.message-actions')
        ?.querySelector('[data-message-recognition-status]');
      button.disabled = true;
      if (status) {
        status.textContent = '正在识别...';
        status.classList.remove('is-error');
      }
      try {
        const response = await fetch(`/api/messages/${rawMessageId}/recognize`, {
          method: 'POST',
        });
        const payload = await response.json();
        if (!response.ok) {
          if (status) {
            status.textContent = payload.detail || '识别失败';
            status.classList.add('is-error');
          }
          return;
        }
        if (status) {
          status.textContent = `识别完成：${payload.status}`;
        }
        await refreshSelectedGroupPanel();
      } catch {
        if (status) {
          status.textContent = '识别失败，请检查服务状态。';
          status.classList.add('is-error');
        }
      } finally {
        button.disabled = false;
      }
    });
  });
}

async function refreshSelectedGroupPanel() {
  const currentPanel = getMessagePanel();
  if (!currentPanel) {
    return;
  }
  const { chatId, searchText, senderName } = getMessageFilterState(currentPanel);
  const nextPanel = await fetchMessagePanel(chatId, { searchText, senderName });
  if (!nextPanel) {
    return;
  }
  currentPanel.replaceWith(nextPanel);
  bindMessagePanelControls(nextPanel);
  scrollMessagePanelToTop(nextPanel);
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
        scrollMessagePanelToTop(nextPanel);
      }
      document.querySelectorAll('.kol-strategy-row').forEach((row) => row.classList.remove('is-active'));
      const row = element.closest('.kol-strategy-row');
      if (row) {
        row.classList.add('is-active');
      }
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

function bindGroupAutomationToggles() {
  document.querySelectorAll('[data-toggle-group-automation]').forEach((button) => {
    button.addEventListener('click', async (event) => {
      event.stopPropagation();
      const setting = button.dataset.setting || '';
      const chatId = Number(button.dataset.chatId || '0');
      const chatTitle = button.dataset.chatTitle || String(chatId);
      const nextEnabled = button.dataset.enabled !== 'true';
      const payload = { chat_title: chatTitle };
      payload[setting] = nextEnabled;
      button.disabled = true;
      button.classList.add('is-updating');
      try {
        const response = await fetch(`/api/groups/${chatId}/automation`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const result = await response.json();
        if (!response.ok) {
          setRecoveryStatus(result.detail || '群组开关保存失败', true);
          return;
        }
        const resolvedEnabled = Boolean(result[setting]);
        button.dataset.enabled = resolvedEnabled ? 'true' : 'false';
        button.classList.toggle('is-enabled', resolvedEnabled);
        setRecoveryStatus('群组开关已保存');
      } catch {
        setRecoveryStatus('群组开关保存失败，请检查服务状态。', true);
      } finally {
        button.disabled = false;
        button.classList.remove('is-updating');
      }
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

function bindDashboardTabs() {
  const buttons = document.querySelectorAll('[data-dashboard-tab]');
  const panels = document.querySelectorAll('[data-dashboard-panel]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const tab = button.dataset.dashboardTab || 'main';
      buttons.forEach((item) => item.classList.toggle('is-active', item === button));
      panels.forEach((panel) => {
        panel.classList.toggle('is-active', panel.dataset.dashboardPanel === tab);
      });
    });
  });
}

function bindAiRecognitionPromptForm() {
  const form = document.querySelector('[data-ai-recognition-prompt-form]');
  if (!form) {
    return;
  }
  const input = form.querySelector('[data-ai-recognition-prompt-input]');
  const status = form.querySelector('[data-ai-recognition-save-status]');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const submitButton = form.querySelector('button[type="submit"]');
    if (submitButton) {
      submitButton.disabled = true;
    }
    if (status) {
      status.textContent = '正在保存...';
      status.classList.remove('is-error');
    }
    try {
      const response = await fetch('/api/ai-recognition-config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          recognition_prompt: input ? input.value : '',
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        if (status) {
          status.textContent = payload.detail || '保存失败';
          status.classList.add('is-error');
        }
        return;
      }
      if (status) {
        status.textContent = '提示词已保存';
      }
    } catch {
      if (status) {
        status.textContent = '保存失败，请检查服务状态。';
        status.classList.add('is-error');
      }
    } finally {
      if (submitButton) {
        submitButton.disabled = false;
      }
    }
  });
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

function setRecoveryStatus(message, isError = false) {
  const status = document.querySelector('[data-recovery-status]');
  if (!status) {
    return;
  }
  status.textContent = message;
  status.classList.toggle('is-error', Boolean(isError));
}

function formatRecoveryActionCounts(actionCounts) {
  return Object.entries(actionCounts || {})
    .map(([action, count]) => `${action}: ${count}`)
    .join(', ');
}

function bindRecoveryScanButton() {
  const button = document.querySelector('[data-run-recovery-scan]');
  if (!button) {
    return;
  }
  button.addEventListener('click', async () => {
    button.disabled = true;
    setRecoveryStatus('正在运行恢复扫描...');
    try {
      const response = await fetch('/api/recovery-dry-run', { method: 'POST' });
      const payload = await response.json();
      if (!response.ok) {
        const detail = payload && typeof payload.detail === 'string'
          ? payload.detail
          : '恢复扫描失败，请检查服务日志。';
        setRecoveryStatus(detail, true);
        return;
      }
      const actionCounts = formatRecoveryActionCounts(payload.action_counts);
      const suffix = actionCounts ? `；${actionCounts}` : '';
      setRecoveryStatus(
        `扫描完成：候选 ${payload.total_candidates}，写入 ${payload.persisted_decisions}${suffix}`
      );
      window.setTimeout(() => window.location.reload(), 700);
    } catch {
      setRecoveryStatus('恢复扫描失败，请检查网络或服务状态。', true);
    } finally {
      button.disabled = false;
    }
  });
}

function bindRecoveryReviewButtons() {
  document.querySelectorAll('[data-review-recovery]').forEach((button) => {
    button.addEventListener('click', async () => {
      const reviewStatus = button.dataset.reviewStatus || '';
      const label = reviewStatus === 'approved_for_order' ? '同意补挂单' : '忽略';
      button.disabled = true;
      setRecoveryStatus(`正在记录“${label}”...`);
      try {
        const response = await fetch('/api/recovery-decisions/review', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            chat_id: Number(button.dataset.chatId || 0),
            message_id: Number(button.dataset.messageId || 0),
            symbol: button.dataset.symbol || '',
            side: button.dataset.side || '',
            review_status: reviewStatus,
          }),
        });
        const payload = await response.json();
        if (!response.ok) {
          const detail = payload && typeof payload.detail === 'string'
            ? payload.detail
            : '审核记录失败，请检查服务日志。';
          setRecoveryStatus(detail, true);
          return;
        }
        setRecoveryStatus(`已记录：${label}`);
        window.setTimeout(() => window.location.reload(), 500);
      } catch {
        setRecoveryStatus('审核记录失败，请检查网络或服务状态。', true);
      } finally {
        button.disabled = false;
      }
    });
  });
}

function setRecoveryOrderConfirmStatus(button, message, isError = false) {
  const row = button.closest('.recovery-decision-row');
  const status = row ? row.querySelector('[data-recovery-order-confirm-status]') : null;
  if (!status) {
    return;
  }
  status.textContent = message || '';
  status.classList.toggle('is-error', Boolean(isError));
  status.classList.toggle('is-ready', Boolean(message) && !isError);
}

function formatRecoveryOrderConfirmation(payload) {
  if (payload.ready_for_live_order) {
    return '可进入真实下单前确认';
  }
  const reasonCodes = Array.isArray(payload.reason_codes) ? payload.reason_codes.join(', ') : '';
  return reasonCodes ? `仍有阻断：${reasonCodes}` : '仍有阻断，请检查订单草稿。';
}

function bindRecoveryOrderConfirmationButtons() {
  document.querySelectorAll('[data-confirm-recovery-order]').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      setRecoveryOrderConfirmStatus(button, '正在最终确认...');
      try {
        const response = await fetch('/api/recovery-order-confirm-dry-run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            chat_id: Number(button.dataset.chatId || 0),
            message_id: Number(button.dataset.messageId || 0),
            symbol: button.dataset.symbol || '',
            side: button.dataset.side || '',
          }),
        });
        const payload = await response.json();
        if (!response.ok) {
          const detail = payload && typeof payload.detail === 'string'
            ? payload.detail
            : '最终确认失败，请检查服务日志。';
          setRecoveryOrderConfirmStatus(button, detail, true);
          return;
        }
        setRecoveryOrderConfirmStatus(
          button,
          formatRecoveryOrderConfirmation(payload),
          !payload.ready_for_live_order
        );
      } catch {
        setRecoveryOrderConfirmStatus(button, '最终确认失败，请检查网络或服务状态。', true);
      } finally {
        button.disabled = false;
      }
    });
  });
}

function setRecoverySubmitGateStatus(button, message, isError = false) {
  const row = button.closest('.recovery-decision-row');
  const status = row ? row.querySelector('[data-recovery-submit-gate-status]') : null;
  if (!status) {
    return;
  }
  status.textContent = message || '';
  status.classList.toggle('is-error', Boolean(isError));
  status.classList.toggle('is-ready', Boolean(message) && !isError);
}

function formatRecoverySubmitGate(payload) {
  if (payload.would_submit) {
    return '模拟通过，可进入真实提交实现';
  }
  const reasonCodes = Array.isArray(payload.reason_codes) ? payload.reason_codes.join(', ') : '';
  return reasonCodes ? `模拟阻断：${reasonCodes}` : '模拟阻断，请检查闸门结果。';
}

function bindRecoverySubmitGateButtons() {
  document.querySelectorAll('[data-simulate-recovery-submit]').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      setRecoverySubmitGateStatus(button, '正在模拟提交闸门...');
      try {
        const response = await fetch('/api/recovery-live-submit-gate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            chat_id: Number(button.dataset.chatId || 0),
            message_id: Number(button.dataset.messageId || 0),
            symbol: button.dataset.symbol || '',
            side: button.dataset.side || '',
          }),
        });
        const payload = await response.json();
        if (!response.ok) {
          const detail = payload && typeof payload.detail === 'string'
            ? payload.detail
            : '模拟提交失败，请检查服务日志。';
          setRecoverySubmitGateStatus(button, detail, true);
          return;
        }
        setRecoverySubmitGateStatus(
          button,
          formatRecoverySubmitGate(payload),
          !payload.would_submit
        );
      } catch {
        setRecoverySubmitGateStatus(button, '模拟提交失败，请检查网络或服务状态。', true);
      } finally {
        button.disabled = false;
      }
    });
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
  scrollMessagePanelToTop(nextPanel);
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
      await refreshGroupList();
      const currentChatId = Number(currentPanel.dataset.chatId || '0');
      if (Number(payload.chat_id || 0) !== currentChatId) {
        return;
      }
      await refreshCurrentGroupPanel();
    });
    source.onerror = () => {
      setAiStatus('实时连接中断，已退回轮询刷新。', true);
      source.close();
    };
    return;
  }
}

function startPollingUpdates() {
  window.setInterval(async () => {
    await refreshCurrentGroupPanel();
    await refreshGroupList();
  }, 15000);
}

window.addEventListener('DOMContentLoaded', () => {
  const form = document.querySelector('[data-ai-form]');
  if (form) {
    form.addEventListener('submit', submitAiQuestion);
  }
  bindGroupLinks();
  bindGroupAutomationToggles();
  bindMessagePanelControls();
  bindDashboardTabs();
  bindAiRecognitionPromptForm();
  bindGroupPromptEditor();
  bindClearAiHistory();
  bindRecoveryScanButton();
  bindRecoveryReviewButtons();
  bindRecoveryOrderConfirmationButtons();
  bindRecoverySubmitGateButtons();
  renderConversationHistory();
  setAiStatus('Ready to analyze the current group.');
  resetInitialMessagePanelScroll();
  connectLiveUpdates();
  startPollingUpdates();
});
