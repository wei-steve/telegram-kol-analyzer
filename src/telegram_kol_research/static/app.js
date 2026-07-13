let latestFreshnessSnapshot = null;
let currentSelectedChatId = null;
let groupSwitchRequestId = 0;
let hasDeferredMessageRefresh = false;
let recoveryRefreshPromise = null;

const workbenchLoadState = {
  home: { key: null, promise: null },
  positions: { key: null, promise: null },
  strategies: { key: null, promise: null },
  messages: { key: null, promise: null },
};

const MESSAGE_TOP_THRESHOLD = 24;

function setMutationBusy(control, busy) {
  if (!control) return;
  control.disabled = busy;
  control.setAttribute('aria-busy', busy ? 'true' : 'false');
}

function escapeHtml(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function getConversationKey() {
  const chatId = getSelectedChatId() || 0;
  return `telegram-workbench:${chatId}:current_group`;
}

function getPromptKey(chatId = null) {
  const resolvedChatId = chatId || getSelectedChatId() || '0';
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

function setMonitorStatus(status) {
  const badge = document.querySelector('[data-monitor-status]');
  if (!badge) {
    return;
  }
  const state = status && status.state ? status.state : 'disconnected';
  const label = status && status.label ? status.label : '已断开';
  badge.textContent = label;
  badge.dataset.monitorState = state;
  badge.title = status && status.detail ? status.detail : '';
  badge.classList.toggle('is-live', state === 'monitoring');
  badge.classList.toggle('is-idle', state === 'idle');
  badge.classList.toggle('is-disconnected', state === 'disconnected');
}

async function refreshMonitorStatus() {
  try {
    const response = await fetch('/api/monitor-status', { cache: 'no-store' });
    if (!response.ok) {
      throw new Error('monitor status request failed');
    }
    setMonitorStatus(await response.json());
  } catch {
    setMonitorStatus({
      state: 'disconnected',
      label: '已断开',
      detail: 'Web 服务连接失败，等待恢复',
    });
  }
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

function getMessageScrollContainer(panel = getMessagePanel()) {
  return panel ? panel.querySelector('[data-message-list]') : null;
}

function isMessagePanelAtTop(panel = getMessagePanel()) {
  const scrollContainer = getMessageScrollContainer(panel);
  return !scrollContainer || scrollContainer.scrollTop <= MESSAGE_TOP_THRESHOLD;
}

function setNewMessagesButtonVisible(panel = getMessagePanel(), visible = false) {
  const button = panel ? panel.querySelector('[data-new-messages-button]') : null;
  if (!button) {
    return;
  }
  button.hidden = !visible;
  button.classList.toggle('is-visible', Boolean(visible));
}

function setMessageCardCollapsed(card, collapsed) {
  if (!card) {
    return;
  }
  card.classList.toggle('is-message-collapsed', Boolean(collapsed));
  const toggle = card.querySelector('[data-message-card-toggle]');
  if (toggle) {
    toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  }
}

function setMessageCardsCollapsed(panel, collapsed) {
  if (!panel) {
    return;
  }
  panel.querySelectorAll('[data-message-card]').forEach((card) => {
    setMessageCardCollapsed(card, collapsed);
  });
}

function restoreDefaultMessageCardState(panel) {
  if (!panel) {
    return;
  }
  panel.querySelectorAll('[data-message-card]').forEach((card) => {
    setMessageCardCollapsed(card, card.dataset.messageDefaultExpanded !== 'true');
  });
}

function scrollMessagePanelToTop(panel = getMessagePanel()) {
  const scrollContainer = getMessageScrollContainer(panel);
  if (!scrollContainer) {
    return;
  }
  scrollContainer.scrollTo({ top: 0, behavior: 'auto' });
  setNewMessagesButtonVisible(panel, false);
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
  if (currentSelectedChatId) {
    return currentSelectedChatId;
  }
  const panel = getMessagePanel();
  if (panel && panel.dataset.chatId) {
    return Number(panel.dataset.chatId || '0');
  }
  const chatIdInput = document.querySelector('[name="chat_id"]');
  return Number(chatIdInput ? chatIdInput.value : '0');
}

function setSelectedChatId(chatId) {
  currentSelectedChatId = Number(chatId || 0);
  document.querySelectorAll('[name="chat_id"]').forEach((input) => {
    input.value = String(currentSelectedChatId);
  });
}

function syncSelectedGroupState(chatId, options = {}) {
  const selectedChatId = Number(chatId || 0);
  if (!selectedChatId) {
    return;
  }
  setSelectedChatId(selectedChatId);
  let selectedButton = null;
  document.querySelectorAll('[data-group-link]').forEach((button) => {
    const isSelected = Number(button.dataset.chatId || '0') === selectedChatId;
    button.setAttribute('aria-current', isSelected ? 'true' : 'false');
    const row = button.closest('.kol-strategy-row');
    if (row) {
      row.classList.toggle('is-active', isSelected);
    }
    if (isSelected) {
      selectedButton = button;
    }
  });
  const pickerOption = document.querySelector(`[data-group-picker-option][data-chat-id="${selectedChatId}"]`);
  document.querySelectorAll('[data-group-picker-option]').forEach((option) => {
    const selected = option === pickerOption;
    option.classList.toggle('is-selected', selected);
    option.setAttribute('aria-current', selected ? 'true' : 'false');
    const check = option.querySelector('.group-picker-check');
    if (check) check.textContent = selected ? '✓' : '';
  });
  const label = document.querySelector('[data-group-context-label]');
  const selectedName = pickerOption?.querySelector('strong')?.textContent?.trim();
  if (label && selectedName) label.textContent = selectedName;
  try { window.localStorage.setItem('telegram-workbench:selected-group', String(selectedChatId)); } catch {}
  if (options.focus && selectedButton) {
    selectedButton.focus({ preventScroll: true });
  }
}

function bindGroupPickerOptions(root, close) {
  root.querySelectorAll('[data-group-picker-option]').forEach((option) => {
    if (option.dataset.pickerBound === 'true') return;
    option.dataset.pickerBound = 'true';
    option.addEventListener('click', () => {
      const chatId = Number(option.dataset.chatId || 0);
      const status = root.querySelector('[data-group-context-status]');
      if (status) status.textContent = '正在切换…';
      const onSuccess = (event) => {
        if (Number(event.detail?.chatId) !== chatId) return;
        cleanup();
        if (status) status.textContent = '群组已切换';
        close();
      };
      const onError = (event) => {
        if (Number(event.detail?.chatId) !== chatId) return;
        cleanup();
        if (status) status.textContent = '切换失败，点击重试';
      };
      const cleanup = () => {
        document.removeEventListener('group-context-success', onSuccess);
        document.removeEventListener('group-context-error', onError);
      };
      document.addEventListener('group-context-success', onSuccess);
      document.addEventListener('group-context-error', onError);
      document.querySelector(`[data-group-link][data-chat-id="${chatId}"]`)?.click();
    });
  });
}

function refreshGroupPickerOptions() {
  const root = document.querySelector('[data-group-context]');
  const results = root?.querySelector('[data-group-picker-results]');
  if (!root || !results) return;
  const selectedChatId = getSelectedChatId();
  results.innerHTML = '';
  document.querySelectorAll('.kol-strategy-row [data-group-link]').forEach((groupLink) => {
    const option = document.createElement('button');
    const chatId = Number(groupLink.dataset.chatId || 0);
    const title = groupLink.querySelector('.kol-name-row strong')?.textContent?.trim() || String(chatId);
    const status = groupLink.querySelector('.kol-status-row')?.textContent?.replace(/\s+/g, ' ').trim() || '';
    option.type = 'button';
    option.className = `group-picker-option${chatId === selectedChatId ? ' is-selected' : ''}`;
    option.dataset.groupPickerOption = '';
    option.dataset.chatId = String(chatId);
    option.dataset.searchText = title.toLowerCase();
    option.setAttribute('aria-current', chatId === selectedChatId ? 'true' : 'false');
    const text = document.createElement('span');
    const strong = document.createElement('strong');
    const small = document.createElement('small');
    strong.textContent = title;
    small.textContent = status;
    text.append(strong, small);
    const check = document.createElement('span');
    check.className = 'group-picker-check';
    check.textContent = chatId === selectedChatId ? '✓' : '';
    option.append(text, check);
    results.appendChild(option);
  });
  bindGroupPickerOptions(root, () => {
    const picker = root.querySelector('[data-group-picker]');
    if (picker) picker.hidden = true;
  });
}

function bindGroupContext() {
  const root = document.querySelector('[data-group-context]');
  const picker = root?.querySelector('[data-group-picker]');
  const trigger = root?.querySelector('[data-group-context-trigger]');
  const search = root?.querySelector('[data-group-picker-search]');
  if (!root || !picker || !trigger) return;
  const close = () => { picker.hidden = true; trigger.focus({ preventScroll: true }); };
  trigger.addEventListener('click', () => { picker.hidden = false; search?.focus(); });
  root.querySelectorAll('[data-group-picker-close]').forEach((button) => button.addEventListener('click', close));
  bindGroupPickerOptions(root, close);
  search?.addEventListener('input', () => {
    const query = search.value.trim().toLowerCase();
    let visible = 0;
    root.querySelectorAll('[data-group-picker-option]').forEach((option) => {
      const matches = !query || (option.dataset.searchText || '').includes(query);
      option.hidden = !matches;
      if (matches) visible += 1;
    });
    const empty = root.querySelector('[data-group-picker-empty]');
    if (empty) empty.hidden = visible !== 0;
  });
  let persisted = 0;
  try { persisted = Number(window.localStorage.getItem('telegram-workbench:selected-group') || 0); } catch {}
  let initialSelectedChatId = Number(
    root.querySelector('[data-group-picker-option][aria-current="true"]')?.dataset.chatId || 0,
  );
  if (persisted && root.querySelector(`[data-group-picker-option][data-chat-id="${persisted}"]`)) {
    syncSelectedGroupState(persisted);
    initialSelectedChatId = persisted;
  }
  if (initialSelectedChatId) syncSelectedGroupState(initialSelectedChatId);
}

async function refreshGroupList() {
  const selectedChatId = getSelectedChatId();
  const url = `/groups?selected_chat_id=${selectedChatId}&_t=${Date.now()}`;
  const response = await fetch(url, { cache: 'no-store' });
  const html = await response.text();
  const parser = new DOMParser();
  const doc = parser.parseFromString(html, 'text/html');

  const nextKolList = doc.querySelector('.kol-strategy-list');
  const currentKolList = document.querySelector('.kol-strategy-list');
  if (nextKolList && currentKolList) {
    if (sidebarLooksLikeZeroRegression(currentKolList, nextKolList)) {
      bindGroupLinks();
      syncSelectedGroupState(selectedChatId);
      return;
    }
    currentKolList.replaceWith(nextKolList);
    bindGroupAutomationToggles();
  }

  bindGroupLinks();
  refreshGroupPickerOptions();
  const selectedStillExists = document.querySelector(`[data-group-link][data-chat-id="${selectedChatId}"]`);
  if (!selectedStillExists) {
    const fallback = document.querySelector('[data-group-link]');
    if (fallback) {
      fallback.click();
      return;
    }
  }
  syncSelectedGroupState(selectedChatId);
}

function sidebarStrategyCountTotal(list) {
  if (!list) return 0;
  return [...list.querySelectorAll('.kol-status-text')].reduce((total, element) => {
    const match = (element.textContent || '').match(/(\d+)/);
    return total + (match ? Number(match[1]) : 0);
  }, 0);
}

function sidebarLooksLikeZeroRegression(currentList, nextList) {
  return sidebarStrategyCountTotal(currentList) > 0 && sidebarStrategyCountTotal(nextList) === 0;
}

async function fetchMessagePanel(chatId, options = {}) {
  const url = buildMessagesUrl(chatId, options);
  const cacheBusted = url.includes('?') ? `${url}&_t=${Date.now()}` : `${url}?_t=${Date.now()}`;
  const response = await fetch(cacheBusted, { cache: 'no-store' });
  const html = await response.text();
  const parser = new DOMParser();
  const doc = parser.parseFromString(html, 'text/html');
  return doc.querySelector('[data-messages-panel]');
}

async function fetchDetailPanel(chatId) {
  const url = `/groups/${chatId}/detail?_t=${Date.now()}`;
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) throw new Error(`detail request failed: ${response.status}`);
  const html = await response.text();
  const parser = new DOMParser();
  const doc = parser.parseFromString(html, 'text/html');
  const fragment = doc.querySelector('.strategy-detail-shell');
  if (!fragment) throw new Error('detail response missing strategy-detail-shell');
  return fragment;
}

async function fetchStrategyMidPanel(chatId, filter) {
  const url = `/groups/${chatId}/strategy-mid-panel?filter=${filter}&_t=${Date.now()}`;
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) throw new Error(`strategy request failed: ${response.status}`);
  const html = await response.text();
  const parser = new DOMParser();
  const doc = parser.parseFromString(html, 'text/html');
  const fragment = doc.querySelector('.strategy-panel-content');
  if (!fragment) throw new Error('strategy response missing strategy-panel-content');
  return fragment;
}

async function refreshStrategyMidPanel() {
  const chatId = getSelectedChatId();
  if (!chatId) return;

  const filterInput = document.querySelector('[data-strategy-filter-input]');
  const filter = filterInput ? filterInput.value : 'holding';

  const strategyPanel = document.querySelector('[data-strategy-panel]');
  if (!strategyPanel) return;

  const nextContent = await fetchStrategyMidPanel(chatId, filter);
  if (!nextContent) return;

  strategyPanel.innerHTML = '';
  strategyPanel.appendChild(nextContent);
  bindStrategyFilterBadges();
  bindWorkflowFilters();
}

function bindStrategyFilterBadges() {
  document.querySelectorAll('[data-strategy-filter]').forEach((badge) => {
    badge.addEventListener('click', async () => {
      const filter = badge.dataset.strategyFilter;
      document.querySelectorAll('[data-strategy-filter]').forEach((b) => {
        b.classList.toggle('is-active-filter', b === badge);
      });
      const filterInput = document.querySelector('[data-strategy-filter-input]');
      if (filterInput) {
        filterInput.value = filter;
      }
      await refreshStrategyMidPanel();
    });
  });
}

function bindDetailPanelControls() {
  // Horizontal tab switching with lazy loading
  document.querySelectorAll('[data-detail-tab]').forEach((tab) => {
    tab.addEventListener('click', async () => {
      const targetPanel = tab.dataset.detailTab;
      // Update tab active states
      const tabsContainer = tab.closest('.detail-tabs');
      if (tabsContainer) {
        tabsContainer.querySelectorAll('[data-detail-tab]').forEach((t) => {
          t.classList.toggle('is-active', t === tab);
        });
      }
      // Show matching panel
      const shell = tab.closest('.strategy-detail-shell');
      if (!shell) return;
      const panel = shell.querySelector(`[data-detail-panel-content="${targetPanel}"]`);
      if (!panel) return;
      
      shell.querySelectorAll('[data-detail-panel-content]').forEach((p) => {
        p.classList.toggle('is-active', p === panel);
      });

      // Lazy-load tab content if needed
      if (panel.dataset.tabLazy !== undefined) {
        const chatId = getSelectedChatId();
        panel.innerHTML = '<p class="empty-state">加载中...</p>';
        try {
          let url;
          if (targetPanel === 'messages') {
            url = `/groups/${chatId}/detail/tab/messages`;
          } else {
            url = `/groups/${chatId}/detail/tab/${targetPanel}`;
          }
          const response = await fetch(url, { cache: 'no-store' });
          const html = await response.text();
          panel.innerHTML = html;
          delete panel.dataset.tabLazy;
          
          // Re-bind controls for newly loaded content
          if (targetPanel === 'messages') {
            const msgPanel = panel.querySelector('[data-messages-panel]');
            if (msgPanel) bindMessagePanelControls(msgPanel);
          }
          bindDetailPanelControls();
        } catch {
          panel.innerHTML = '<p class="empty-state">加载失败，请重试</p>';
        }
      }
    });
  });

  // Bind recovery scan button
  const scanBtn = document.querySelector('[data-run-recovery-scan]');
  if (scanBtn) {
    scanBtn.addEventListener('click', async () => {
      const chatId = getSelectedChatId();
      if (chatId) {
        setRecoveryStatus('正在扫描...');
        try {
          await fetch('/api/execution/sync-deepcoin', { method: 'POST' });
        } catch {
          // Keep the strategy panel refresh usable even when exchange sync is unavailable.
        }
        await refreshStrategyPanels(chatId);
        setRecoveryStatus('扫描完成');
      }
    });
  }

  // Bind recovery confirmation buttons (from pending tab)
  document.querySelectorAll('[data-confirm-recovery-order]').forEach((button) => {
    button.addEventListener('click', async () => {
      const chatId = Number(button.dataset.chatId || '0');
      const messageId = Number(button.dataset.messageId || '0');
      const symbol = button.dataset.symbol || '';
      const side = button.dataset.side || '';
      const status = button.parentElement.querySelector('[data-recovery-order-confirm-status]');
      button.disabled = true;
      if (status) { status.textContent = '确认中...'; status.classList.remove('is-error', 'is-ready'); }
      try {
        const response = await fetch('/api/recovery-order-confirm-dry-run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: chatId, message_id: messageId, symbol, side }),
        });
        const result = await response.json();
        if (!response.ok) {
          if (status) { status.textContent = result.detail || '确认失败'; status.classList.add('is-error'); }
          return;
        }
        if (status) { status.textContent = '已确认'; status.classList.add('is-ready'); }
      } catch {
        if (status) { status.textContent = '确认失败'; status.classList.add('is-error'); }
      } finally {
        button.disabled = false;
      }
    });
  });

  // Bind simulate submit buttons
  document.querySelectorAll('[data-simulate-recovery-submit]').forEach((button) => {
    button.addEventListener('click', async () => {
      const chatId = Number(button.dataset.chatId || '0');
      const messageId = Number(button.dataset.messageId || '0');
      const symbol = button.dataset.symbol || '';
      const side = button.dataset.side || '';
      const status = button.parentElement.querySelector('[data-recovery-submit-gate-status]');
      button.disabled = true;
      if (status) { status.textContent = '提交中...'; status.classList.remove('is-error', 'is-ready'); }
      try {
        const response = await fetch('/api/recovery-live-submit-gate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: chatId, message_id: messageId, symbol, side }),
        });
        const result = await response.json();
        if (!response.ok) {
          if (status) { status.textContent = result.detail || '提交失败'; status.classList.add('is-error'); }
          return;
        }
        if (status) { status.textContent = '已提交'; status.classList.add('is-ready'); }
      } catch {
        if (status) { status.textContent = '提交失败'; status.classList.add('is-error'); }
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll('[data-live-recovery-submit]').forEach((button) => {
    if (button.dataset.liveSubmitBound === 'true') {
      return;
    }
    button.dataset.liveSubmitBound = 'true';
    button.addEventListener('click', async () => {
      const chatId = Number(button.dataset.chatId || '0');
      const messageId = Number(button.dataset.messageId || '0');
      const symbol = button.dataset.symbol || '';
      const side = button.dataset.side || '';
      const status = button.parentElement.querySelector('[data-recovery-submit-gate-status]');
      if (!window.confirm(`确认实盘提交 ${symbol} ${side} 策略？`)) {
        return;
      }
      button.disabled = true;
      if (status) { status.textContent = '实盘提交中...'; status.classList.remove('is-error', 'is-ready'); }
      try {
        const response = await fetch('/api/recovery-live-submit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: chatId, message_id: messageId, symbol, side }),
        });
        const result = await response.json();
        if (!response.ok) {
          if (status) { status.textContent = result.detail || '实盘提交失败'; status.classList.add('is-error'); }
          return;
        }
        if (status) { status.textContent = `已提交 ${result.order_count || 0} 笔`; status.classList.add('is-ready'); }
      } catch {
        if (status) { status.textContent = '实盘提交失败，请检查服务状态'; status.classList.add('is-error'); }
      } finally {
        button.disabled = false;
      }
    });
  });

  // Bind message panel controls (always visible in lower section)
  const messagesPanel = document.querySelector('[data-messages-panel]');
  if (messagesPanel) {
    bindMessagePanelControls(messagesPanel);
  }
}

function bindMessagePanelControls(panel = getMessagePanel()) {
  if (!panel) {
    return;
  }

  const scrollContainer = getMessageScrollContainer(panel);
  if (scrollContainer && scrollContainer.dataset.messageScrollBound !== 'true') {
    scrollContainer.dataset.messageScrollBound = 'true';
    scrollContainer.addEventListener('scroll', () => {
      if (isMessagePanelAtTop(panel) && hasDeferredMessageRefresh) {
        hasDeferredMessageRefresh = false;
        setNewMessagesButtonVisible(panel, false);
        refreshCurrentGroupPanel({
          force: true,
          scrollToTopAfterRefresh: true,
          showStatus: false,
        });
      } else if (isMessagePanelAtTop(panel)) {
        setNewMessagesButtonVisible(panel, false);
      } else if (hasDeferredMessageRefresh) {
        setNewMessagesButtonVisible(panel, true);
      }
    });
  }

  const newMessagesButton = panel.querySelector('[data-new-messages-button]');
  if (newMessagesButton && newMessagesButton.dataset.newMessagesBound !== 'true') {
    newMessagesButton.dataset.newMessagesBound = 'true';
    newMessagesButton.addEventListener('click', async () => {
      newMessagesButton.disabled = true;
      try {
        hasDeferredMessageRefresh = false;
        setNewMessagesButtonVisible(panel, false);
        await refreshCurrentGroupPanel({
          force: true,
          scrollToTopAfterRefresh: true,
          showStatus: false,
        });
        await refreshGroupList();
      } finally {
        newMessagesButton.disabled = false;
      }
    });
  }

  panel.querySelectorAll('[data-message-card-toggle]').forEach((button) => {
    if (button.dataset.messageCardToggleBound === 'true') {
      return;
    }
    button.dataset.messageCardToggleBound = 'true';
    button.addEventListener('click', () => {
      const card = button.closest('[data-message-card]');
      if (!card) {
        return;
      }
      setMessageCardCollapsed(card, !card.classList.contains('is-message-collapsed'));
    });
  });

  const expandAllButton = panel.querySelector('[data-message-list-expand-all]');
  if (expandAllButton && expandAllButton.dataset.messageListControlBound !== 'true') {
    expandAllButton.dataset.messageListControlBound = 'true';
    expandAllButton.addEventListener('click', () => setMessageCardsCollapsed(panel, false));
  }

  const defaultButton = panel.querySelector('[data-message-list-default]');
  if (defaultButton && defaultButton.dataset.messageListControlBound !== 'true') {
    defaultButton.dataset.messageListControlBound = 'true';
    defaultButton.addEventListener('click', () => restoreDefaultMessageCardState(panel));
  }

  const collapseAllButton = panel.querySelector('[data-message-list-collapse-all]');
  if (collapseAllButton && collapseAllButton.dataset.messageListControlBound !== 'true') {
    collapseAllButton.dataset.messageListControlBound = 'true';
    collapseAllButton.addEventListener('click', () => setMessageCardsCollapsed(panel, true));
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
        await refreshCurrentGroupPanel({
          deferIfMessageListAwayFromTop: Number(payload.inserted_messages || 0) > 0,
        });
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
  const chatId = getSelectedChatId();
  if (!chatId) return;
  const currentMessagePanel = getMessagePanel();
  const currentScrollContainer = getMessageScrollContainer(currentMessagePanel);
  const previousMessageScrollTop = currentScrollContainer ? currentScrollContainer.scrollTop : 0;
  const filterState = getMessageFilterState(currentMessagePanel);

  try {
    const nextPanel = await fetchMessagePanel(chatId, {
      searchText: filterState.searchText,
      senderName: filterState.senderName,
    });
    if (nextPanel && currentMessagePanel) {
      currentMessagePanel.replaceWith(nextPanel);
      bindMessagePanelControls(nextPanel);
      const nextScrollContainer = getMessageScrollContainer(nextPanel);
      if (nextScrollContainer) {
        nextScrollContainer.scrollTop = previousMessageScrollTop;
      }
      if (isMessagePanelAtTop(nextPanel)) {
        setNewMessagesButtonVisible(nextPanel, false);
      }
      await refreshStrategyMidPanel();
      await refreshGroupList();
      return;
    }
  } catch {
    // Fall back to the full detail refresh path below.
  }

  await refreshCurrentGroupPanel({ force: true, preserveMessageScroll: true });
}

async function refreshStrategyPanels(chatId) {
  // Refresh the complete detail panel for the selected group
  const detailPanel = document.querySelector('[data-detail-panel]');
  if (!detailPanel || !chatId) return;
  try {
    const nextPanel = await fetchDetailPanel(chatId);
    if (nextPanel) {
      detailPanel.innerHTML = '';
      detailPanel.appendChild(nextPanel);
      bindDetailPanelControls();
    }
  } catch (e) {
    // Silently ignore fetch errors
  }
  // Also refresh group list to update position counts
  await refreshGroupList();
}

function bindGroupLinks() {
  document.querySelectorAll('[data-group-link]').forEach((element) => {
    if (element.dataset.groupLinkBound === 'true') {
      return;
    }
    element.dataset.groupLinkBound = 'true';
    element.addEventListener('click', async () => {
      const chatId = Number(element.dataset.chatId);
      const requestId = ++groupSwitchRequestId;
      hasDeferredMessageRefresh = false;
      const detailPanel = document.querySelector('[data-detail-panel]');
      const strategyPanel = document.querySelector('[data-strategy-panel]');
      const filterInput = document.querySelector('[data-strategy-filter-input]');
      const filter = filterInput ? filterInput.value : 'holding';
      const activeView = document.querySelector('[data-trader-dashboard]')?.dataset.activeWorkbenchView === 'messages'
        ? 'messages'
        : 'strategies';
      setAiStatus('');
      document.dispatchEvent(new CustomEvent('group-context-pending', { detail: { chatId } }));
      try {
        await loadVisibleGroupDestination({ activeView, chatId, filter, detailPanel, strategyPanel, requestId });
        if (requestId !== groupSwitchRequestId) return;
        markWorkbenchLoaded(activeView, chatId);
        syncSelectedGroupState(chatId, { focus: true });
        applyGroupPromptToEditor(String(chatId));
        renderConversationHistory();
        document.dispatchEvent(new CustomEvent('group-context-success', { detail: { chatId } }));
        refreshGroupList().catch(() => {});
      } catch (error) {
        if (requestId === groupSwitchRequestId) {
          setAiStatus('群组切换失败，请重试。', true);
          document.dispatchEvent(new CustomEvent('group-context-error', { detail: { chatId } }));
        }
      }
    });
  });
}

async function loadVisibleGroupDestination({ activeView, chatId, filter, detailPanel, strategyPanel, requestId }) {
  if (activeView === 'messages') {
    const nextContent = await fetchDetailPanel(chatId);
    if (requestId !== groupSwitchRequestId) return;
    if (!detailPanel) throw new Error('missing detail panel');
    detailPanel.innerHTML = '';
    detailPanel.appendChild(nextContent);
    bindDetailPanelControls();
    bindWorkflowFilters();
    return;
  }
  const nextStrategyContent = await fetchStrategyMidPanel(chatId, filter);
  if (requestId !== groupSwitchRequestId) return;
  if (!strategyPanel) throw new Error('missing strategy panel');
  strategyPanel.innerHTML = '';
  strategyPanel.appendChild(nextStrategyContent);
  bindStrategyFilterBadges();
  bindWorkflowFilters();
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
        await refreshMonitorStatus();
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

function workbenchLoadKey(view) {
  if (view === 'strategies' || view === 'messages') {
    return String(getSelectedChatId() || 0);
  }
  return 'global';
}

function markWorkbenchLoaded(view, key = workbenchLoadKey(view)) {
  if (workbenchLoadState[view]) {
    workbenchLoadState[view].key = String(key);
  }
}

function showWorkbenchLoadError(view, error) {
  const container = document.querySelector(`[data-lazy-workbench="${view}"]`);
  if (!container) return;
  container.innerHTML = '';
  container.setAttribute('aria-busy', 'false');
  const message = document.createElement('p');
  message.className = 'workbench-load-error';
  message.textContent = `加载失败：${error?.message || '请检查服务状态'}`;
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.className = 'secondary-button';
  retry.textContent = '重新加载';
  retry.addEventListener('click', () => ensureWorkbenchViewLoaded(view, { force: true }));
  container.append(message, retry);
}

async function fetchWorkbenchPartial(url, selector) {
  const response = await fetch(`${url}${url.includes('?') ? '&' : '?'}_t=${Date.now()}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`请求失败 (${response.status})`);
  const html = await response.text();
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const fragment = doc.querySelector(selector);
  if (!fragment) throw new Error('返回内容不完整');
  return fragment;
}

async function loadHomeDashboard() {
  const container = document.querySelector('[data-lazy-workbench="home"]');
  if (!container) return;
  const fragment = await fetchWorkbenchPartial('/home-dashboard', '[data-home-dashboard]');
  container.innerHTML = '';
  container.appendChild(fragment);
  bindHomeEventFilters();
  bindHomeEventNavigation();
}

async function loadPositionsPanel() {
  const container = document.querySelector('[data-lazy-workbench="positions"]');
  if (!container) return;
  const fragment = await fetchWorkbenchPartial('/positions-panel', '[data-exchange-position-tabs]');
  container.innerHTML = '';
  container.appendChild(fragment);
  bindDashboardTabs();
  bindExchangePositionTabs();
  bindBoundPositionCloseButtons();
  bindDeepcoinPositionSync();
  bindLivePositionAttributionButtons();
}

async function loadSelectedGroupDestination(view) {
  const chatId = getSelectedChatId();
  if (!chatId) return;
  const requestId = ++groupSwitchRequestId;
  const filterInput = document.querySelector('[data-strategy-filter-input]');
  const filter = filterInput ? filterInput.value : 'holding';
  await loadVisibleGroupDestination({
    activeView: view,
    chatId,
    filter,
    detailPanel: document.querySelector('[data-detail-panel]'),
    strategyPanel: document.querySelector('[data-strategy-panel]'),
    requestId,
  });
}

async function ensureWorkbenchViewLoaded(view, options = {}) {
  const state = workbenchLoadState[view];
  if (!state) return;
  const key = workbenchLoadKey(view);
  if (!options.force && state.key === key) return;
  if (state.promise) return state.promise;
  const container = document.querySelector(`[data-lazy-workbench="${view}"]`);
  if (container) container.setAttribute('aria-busy', 'true');
  state.promise = (async () => {
    if (view === 'home') {
      await loadHomeDashboard();
    } else if (view === 'positions') {
      await loadPositionsPanel();
    } else {
      await loadSelectedGroupDestination(view);
    }
    state.key = key;
    if (container) container.setAttribute('aria-busy', 'false');
  })().catch((error) => {
    showWorkbenchLoadError(view, error);
  }).finally(() => {
    state.promise = null;
  });
  return state.promise;
}

function bindDashboardTabs() {
  const buttons = document.querySelectorAll('[data-dashboard-tab]');
  const panels = document.querySelectorAll('[data-dashboard-panel]');
  buttons.forEach((button) => {
    if (button.dataset.dashboardTabBound === 'true') return;
    button.dataset.dashboardTabBound = 'true';
    button.addEventListener('click', () => {
      const tab = button.dataset.dashboardTab || 'main';
      buttons.forEach((item) => item.classList.toggle('is-active', item === button));
      panels.forEach((panel) => {
        panel.classList.toggle('is-active', panel.dataset.dashboardPanel === tab);
      });
      const menu = button.closest('details');
      if (menu) {
        menu.open = false;
      }
    });
  });
}

function bindWorkbenchNavigation() {
  const dashboard = document.querySelector('[data-trader-dashboard]');
  const buttons = document.querySelectorAll('[data-workbench-view]');
  const panels = document.querySelectorAll('[data-workbench-panel]');
  if (!dashboard || !buttons.length || !panels.length) {
    return;
  }

  const views = ['home', 'positions', 'strategies', 'messages', 'more'];
  const setWorkbenchView = (requestedView) => {
    const view = views.includes(requestedView) ? requestedView : 'home';
    const legacyView = view === 'home' ? 'overview' : view;
    dashboard.dataset.activeWorkbenchView = view;
    dashboard.classList.remove(...['overview', 'positions', 'strategies', 'messages', 'more'].map((item) => `mobile-view-${item}`));
    dashboard.classList.add(`mobile-view-${legacyView}`);
    buttons.forEach((button) => {
      const isActive = button.dataset.workbenchView === view;
      button.classList.toggle('is-active', isActive);
      if (isActive) {
        button.setAttribute('aria-current', 'page');
      } else {
        button.removeAttribute('aria-current');
      }
    });
    panels.forEach((panel) => {
      const panelView = panel.dataset.workbenchPanel;
      const isActive = panelView === view || (view === 'messages' && panelView === 'strategies');
      panel.classList.toggle('is-active', isActive);
    });
    if (view === 'positions') {
      document.querySelector('[data-dashboard-tab="exchange-positions"]')?.click();
    }
    ensureWorkbenchViewLoaded(view);
  };

  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      setWorkbenchView(button.dataset.workbenchView || 'home');
    });
  });
  setWorkbenchView('home');
}

function bindHomeEventNavigation() {
  document.querySelectorAll('[data-home-dashboard] [data-workbench-view]').forEach((button) => {
    if (button.dataset.homeWorkbenchNavigationBound === 'true') return;
    button.dataset.homeWorkbenchNavigationBound = 'true';
    button.addEventListener('click', () => {
      const destination = button.dataset.workbenchView || 'home';
      document.querySelector(`nav [data-workbench-view="${destination}"]`)?.click();
    });
  });
  document.querySelectorAll('[data-event-destination]').forEach((card) => {
    if (card.dataset.eventNavigationBound === 'true') return;
    card.dataset.eventNavigationBound = 'true';
    card.addEventListener('click', () => {
      const destination = card.dataset.eventDestination || 'home';
      document.querySelector(`[data-workbench-view="${destination}"]`)?.click();
      const chatId = Number(card.dataset.eventChatId || 0);
      if (chatId) {
        document.querySelector(`[data-group-link][data-chat-id="${chatId}"]`)?.click();
      }
    });
  });
}

function bindHomeEventFilters() {
  const filters = document.querySelectorAll('[data-home-event-filter]');
  const cards = document.querySelectorAll('[data-home-event-kind]');
  const empty = document.querySelector('[data-home-filter-empty]');
  const newEvents = document.querySelector('[data-new-home-events]');
  filters.forEach((filter) => {
    filter.addEventListener('click', () => {
      const kind = filter.dataset.homeEventFilter || 'all';
      let visibleCount = 0;
      filters.forEach((item) => item.classList.toggle('is-active', item === filter));
      cards.forEach((card) => {
        const visible = kind === 'all' || card.dataset.homeEventKind === kind;
        card.hidden = !visible;
        if (visible) visibleCount += 1;
      });
      if (empty) empty.hidden = visibleCount !== 0;
    });
  });
  newEvents?.addEventListener('click', () => window.location.reload());
}

function bindWorkflowFilters() {
  const strategyMap = {
    executing: 'holding',
    'pending-entry': 'pending',
    completed: 'exited',
  };
  document.querySelectorAll('[data-strategy-workflow-filter]').forEach((button) => {
    if (button.dataset.workflowFilterBound === 'true') return;
    button.dataset.workflowFilterBound = 'true';
    button.addEventListener('click', () => {
      const workflow = button.dataset.strategyWorkflowFilter;
      document.querySelectorAll('[data-strategy-workflow-filter]').forEach((item) => item.classList.toggle('is-active', item === button));
      const legacy = strategyMap[workflow];
      if (legacy) document.querySelector(`[data-strategy-filter="${legacy}"]`)?.click();
    });
  });
  document.querySelectorAll('[data-message-workflow-filter]').forEach((button) => {
    if (button.dataset.workflowFilterBound === 'true') return;
    button.dataset.workflowFilterBound = 'true';
    button.addEventListener('click', () => {
      const filter = button.dataset.messageWorkflowFilter || 'all';
      document.querySelectorAll('[data-message-workflow-filter]').forEach((item) => item.classList.toggle('is-active', item === button));
      document.querySelectorAll('[data-message-card]').forEach((card) => {
        const matches = filter === 'all'
          || card.dataset.messageKind === filter
          || (filter === 'media' && card.dataset.messageHasMedia === 'true');
        card.hidden = !matches;
      });
    });
  });
}

function bindMobileWorkNavigation() {
  // Legacy entry point retained for [data-mobile-work-view] compatibility:
  // setMobileWorkView('overview') and [data-dashboard-tab="exchange-positions"]
  bindWorkbenchNavigation();
}

function bindExchangePositionTabs() {
  document.querySelectorAll('[data-exchange-position-tabs]').forEach((root) => {
    const tabs = root.querySelectorAll('[data-exchange-position-tab]');
    const panels = root.querySelectorAll('[data-exchange-position-panel]');
    const viewButtons = root.querySelectorAll('[data-exchange-view-mode]');
    const viewPanels = root.querySelectorAll('[data-exchange-view-panel]');
    tabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        const target = tab.dataset.exchangePositionTab;
        tabs.forEach((item) => {
          const isActive = item === tab;
          item.classList.toggle('is-active', isActive);
          item.setAttribute('aria-selected', isActive ? 'true' : 'false');
        });
        panels.forEach((panel) => {
          panel.classList.toggle('is-active', panel.dataset.exchangePositionPanel === target);
        });
      });
    });
    viewButtons.forEach((button) => {
      button.addEventListener('click', () => {
        const mode = button.dataset.exchangeViewMode || 'list';
        viewButtons.forEach((item) => {
          const isActive = item === button;
          item.classList.toggle('is-active', isActive);
          item.setAttribute('aria-selected', isActive ? 'true' : 'false');
        });
        viewPanels.forEach((panel) => {
          panel.classList.toggle('is-active', panel.dataset.exchangeViewPanel === mode);
        });
      });
    });
  });
}

function initTradingSymbolSelector(form) {
  const selector = form.querySelector('[data-symbol-selector]');
  if (!selector) {
    return;
  }
  const allowedInput = selector.querySelector('[data-allowed-symbols-input]');
  const riskInput = selector.querySelector('[data-symbol-risk-input]');
  const searchInput = selector.querySelector('[data-symbol-search]');
  const summary = selector.querySelector('[data-symbol-selector-summary]');
  const selectedList = selector.querySelector('[data-selected-symbol-list]');
  const symbolList = selector.querySelector('[data-symbol-selector-list]');
  const riskList = selector.querySelector('[data-selected-symbol-risk-list]');
  const state = {
    symbols: [],
    selected: new Set(parseSymbolList(allowedInput?.value || '')),
    riskBySymbol: parseSymbolRiskMap(riskInput?.value || '{}'),
    query: '',
  };

  const syncInputs = () => {
    const selectedSymbols = Array.from(state.selected).sort();
    if (allowedInput) {
      allowedInput.value = selectedSymbols.join(',');
    }
    const riskPayload = {};
    selectedSymbols.forEach((symbol) => {
      const value = Number(state.riskBySymbol[symbol]);
      if (Number.isFinite(value) && value > 0) {
        riskPayload[symbol] = value;
      }
    });
    if (riskInput) {
      riskInput.value = JSON.stringify(riskPayload);
    }
  };

  const renderRiskRows = () => {
    if (!riskList) {
      return;
    }
    const selectedSymbols = Array.from(state.selected).sort();
    riskList.innerHTML = '';
    if (!selectedSymbols.length) {
      const empty = document.createElement('div');
      empty.className = 'symbol-risk-empty';
      empty.textContent = '未选择交易币种';
      riskList.appendChild(empty);
      return;
    }
    selectedSymbols.forEach((symbol) => {
      const row = document.createElement('label');
      row.className = 'symbol-risk-row';
      const label = document.createElement('span');
      label.textContent = `${symbol} 最大亏损 USDT`;
      const input = document.createElement('input');
      input.type = 'number';
      input.min = '1';
      input.step = '1';
      input.placeholder = '默认';
      input.value = state.riskBySymbol[symbol] || '';
      input.addEventListener('input', () => {
        const value = Number(input.value);
        if (Number.isFinite(value) && value > 0) {
          state.riskBySymbol[symbol] = value;
        } else {
          delete state.riskBySymbol[symbol];
        }
        syncInputs();
      });
      row.append(label, input);
      riskList.appendChild(row);
    });
  };

  const renderSelectedSymbols = () => {
    if (!selectedList) {
      return;
    }
    const selectedSymbols = Array.from(state.selected).sort();
    selectedList.innerHTML = '';
    if (!selectedSymbols.length) {
      const empty = document.createElement('span');
      empty.className = 'selected-symbol-empty';
      empty.textContent = '当前未选择币种';
      selectedList.appendChild(empty);
      return;
    }
    selectedSymbols.forEach((symbol) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'selected-symbol-chip';
      chip.textContent = symbol;
      chip.title = `移除 ${symbol}`;
      chip.addEventListener('click', () => {
        state.selected.delete(symbol);
        delete state.riskBySymbol[symbol];
        syncInputs();
        renderSymbols();
        renderSelectedSymbols();
        renderRiskRows();
      });
      selectedList.appendChild(chip);
    });
  };

  const renderSymbols = () => {
    if (!symbolList) {
      return;
    }
    const query = state.query.trim().toUpperCase();
    const visible = state.symbols
      .filter((item) => !query || item.symbol.includes(query) || item.instrument_id.includes(query))
      .slice(0, 80);
    symbolList.innerHTML = '';
    visible.forEach((item) => {
      const label = document.createElement('label');
      label.className = 'symbol-option-row';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = state.selected.has(item.symbol);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) {
          state.selected.add(item.symbol);
        } else {
          state.selected.delete(item.symbol);
          delete state.riskBySymbol[item.symbol];
        }
        syncInputs();
        renderSymbols();
        renderSelectedSymbols();
        renderRiskRows();
      });
      const symbol = document.createElement('strong');
      symbol.textContent = item.symbol;
      const instrument = document.createElement('span');
      instrument.textContent = item.instrument_id;
      label.append(checkbox, symbol, instrument);
      symbolList.appendChild(label);
    });
    if (summary) {
      summary.textContent = `已选 ${state.selected.size} 个，显示 ${visible.length} / ${state.symbols.length} 个`;
    }
  };

  if (searchInput) {
    searchInput.addEventListener('input', () => {
      state.query = searchInput.value || '';
      renderSymbols();
    });
  }

  syncInputs();
  renderSelectedSymbols();
  renderRiskRows();
  fetch('/api/trading-settings/symbols')
    .then((response) => response.json())
    .then((payload) => {
      const symbols = Array.isArray(payload.symbols) ? payload.symbols : [];
      state.symbols = symbols.map((item) => ({
        symbol: String(item.symbol || '').toUpperCase(),
        instrument_id: String(item.instrument_id || '').toUpperCase(),
      })).filter((item) => item.symbol);
      symbols.forEach((item) => {
        const symbol = String(item.symbol || '').toUpperCase();
        if (item.selected) {
          state.selected.add(symbol);
        }
        if (item.max_loss_usdt !== null && item.max_loss_usdt !== undefined) {
          const value = Number(item.max_loss_usdt);
          if (Number.isFinite(value) && value > 0) {
            state.riskBySymbol[symbol] = value;
          }
        }
      });
      syncInputs();
      renderSymbols();
      renderSelectedSymbols();
      renderRiskRows();
    })
    .catch(() => {
      if (summary) {
        summary.textContent = '交易所币种加载失败，仍可保存当前已选币种';
        summary.classList.add('is-error');
      }
      state.symbols = Array.from(state.selected).sort().map((symbol) => ({
        symbol,
        instrument_id: `${symbol}-USDT-SWAP`,
      }));
      renderSymbols();
      renderSelectedSymbols();
    });
}

function parseSymbolList(value) {
  return String(value || '')
    .split(',')
    .map((item) => item.trim().toUpperCase())
    .filter(Boolean);
}

function parseSymbolRiskMap(value) {
  try {
    const parsed = JSON.parse(value || '{}');
    return Object.fromEntries(
      Object.entries(parsed)
        .map(([symbol, loss]) => [String(symbol).toUpperCase(), Number(loss)])
        .filter(([, loss]) => Number.isFinite(loss) && loss > 0)
    );
  } catch {
    return {};
  }
}

function bindTradingSettingsForm() {
  const form = document.querySelector('[data-trading-settings-form]');
  if (!form) {
    return;
  }
  initTradingSymbolSelector(form);
  const status = form.querySelector('[data-trading-settings-save-status]');
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
    const formData = new FormData(form);
    const numericValue = (name, fallback) => {
      const parsed = Number(formData.get(name));
      return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
    };
    const payload = {
      auto_trade_enabled: Boolean(form.querySelector('[name="auto_trade_enabled"]')?.checked),
      default_max_loss_usdt: numericValue('default_max_loss_usdt', 20),
      daily_max_loss_usdt: numericValue('daily_max_loss_usdt', 500),
      max_concurrent_positions: numericValue('max_concurrent_positions', 3),
      max_market_entry_deviation_pct: numericValue('max_market_entry_deviation_pct', 0.15),
      nearby_entry_market_deviation_pct: numericValue('nearby_entry_market_deviation_pct', 0.15),
      min_ai_confidence: Number(formData.get('min_ai_confidence') || 0.75),
      allowed_symbols: String(formData.get('allowed_symbols') || 'BTC,ETH'),
      symbol_max_loss_usdt: parseSymbolRiskMap(formData.get('symbol_max_loss_usdt') || '{}'),
      entry_range_order_style: String(formData.get('entry_range_order_style') || 'conservative'),
      take_profit_allocations: String(formData.get('take_profit_allocations') || '50,30,20'),
      move_stop_to_breakeven_after_tp1: Boolean(form.querySelector('[name="move_stop_to_breakeven_after_tp1"]')?.checked),
      allow_vision_auto_trade: Boolean(form.querySelector('[name="allow_vision_auto_trade"]')?.checked),
    };
    try {
      const response = await fetch('/api/trading-settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok) {
        if (status) {
          status.textContent = result.detail || '保存失败';
          status.classList.add('is-error');
        }
        return;
      }
      if (status) {
        status.textContent = `已保存，默认单笔最大亏损 ${result.default_max_loss_usdt} USDT`;
      }
    } catch {
      if (status) {
        status.textContent = '保存失败，请检查服务状态';
        status.classList.add('is-error');
      }
    } finally {
      if (submitButton) {
        submitButton.disabled = false;
      }
    }
  });
}

function bindAiRecognitionPromptForm() {
  const form = document.querySelector('[data-ai-recognition-prompt-form]');
  if (!form) {
    bindAiRecognitionConfigForm();
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
        body: JSON.stringify(buildAiRecognitionConfigPayload()),
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
  bindAiRecognitionConfigForm();
}

function bindAiRecognitionConfigForm() {
  const form = document.querySelector('[data-ai-recognition-config-form]');
  if (!form) {
    return;
  }
  bindAiProviderPresetButtons(form);
  bindAiProviderKeyInputs(form);
  const status = form.querySelector('[data-ai-config-save-status]');
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
        body: JSON.stringify(buildAiRecognitionConfigPayload()),
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
        status.textContent = 'AI 配置已保存';
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

function bindAiModelSelectionForm() {
  const form = document.querySelector('[data-ai-model-selection-form]');
  if (!form) {
    return;
  }
  const status = form.querySelector('[data-ai-model-selection-save-status]');
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
        body: JSON.stringify(buildAiRecognitionConfigPayload()),
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
        status.textContent = 'AI 模型选择已保存';
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

function bindAiProviderPresetButtons(form) {
  const selectorForTarget = {
    text: {
      baseUrl: '[data-ai-text-base-url]',
      apiKey: '[data-ai-text-api-key]',
      model: '[data-ai-text-model]',
    },
    image: {
      baseUrl: '[data-ai-image-base-url]',
      apiKey: '[data-ai-image-api-key]',
      model: '[data-ai-image-model]',
    },
  };
  form.querySelectorAll('[data-ai-provider-preset]').forEach((button) => {
    button.addEventListener('click', () => {
      const target = button.dataset.aiProviderTarget;
      const selectors = selectorForTarget[target];
      if (!selectors) {
        return;
      }
      const baseUrlInput = form.querySelector(selectors.baseUrl);
      const apiKeyInput = form.querySelector(selectors.apiKey);
      const modelInput = form.querySelector(selectors.model);
      cacheAiProviderKey({
        target,
        baseUrl: baseUrlInput?.value || '',
        model: modelInput?.value || '',
        apiKey: apiKeyInput?.value || '',
      });

      const nextBaseUrl = button.dataset.aiProviderBaseUrl || '';
      const nextModel = button.dataset.aiProviderModel || '';
      if (baseUrlInput) {
        baseUrlInput.value = nextBaseUrl;
      }
      if (apiKeyInput) {
        apiKeyInput.value = loadCachedAiProviderKey({
          target,
          baseUrl: nextBaseUrl,
          model: nextModel,
        });
      }
      if (modelInput) {
        modelInput.value = nextModel;
      }
    });
  });
}

function bindAiProviderKeyInputs(form) {
  form.querySelectorAll('[data-ai-text-api-key], [data-ai-image-api-key]').forEach((input) => {
    input.addEventListener('change', () => cacheCurrentAiProviderKeys(form));
  });
}

function cacheCurrentAiProviderKeys(form) {
  [
    {
      target: 'text',
      baseUrl: form.querySelector('[data-ai-text-base-url]')?.value || '',
      apiKey: form.querySelector('[data-ai-text-api-key]')?.value || '',
      model: form.querySelector('[data-ai-text-model]')?.value || '',
    },
    {
      target: 'image',
      baseUrl: form.querySelector('[data-ai-image-base-url]')?.value || '',
      apiKey: form.querySelector('[data-ai-image-api-key]')?.value || '',
      model: form.querySelector('[data-ai-image-model]')?.value || '',
    },
  ].forEach(cacheAiProviderKey);
}

function cacheAiProviderKey({ target, baseUrl, model, apiKey }) {
  const storageKey = getAiProviderKeyStorageKey({ target, baseUrl, model });
  if (!storageKey) {
    return;
  }
  try {
    if (apiKey) {
      window.localStorage.setItem(storageKey, apiKey);
    } else {
      window.localStorage.removeItem(storageKey);
    }
  } catch {
    // ignore local storage failures in browser privacy modes
  }
}

function loadCachedAiProviderKey({ target, baseUrl, model }) {
  const storageKey = getAiProviderKeyStorageKey({ target, baseUrl, model });
  if (!storageKey) {
    return '';
  }
  try {
    return window.localStorage.getItem(storageKey) || '';
  } catch {
    return '';
  }
}

function getAiProviderKeyStorageKey({ target, baseUrl, model }) {
  const normalizedTarget = String(target || '').trim().toLowerCase();
  const normalizedBaseUrl = String(baseUrl || '').trim().replace(/\/+$/, '');
  const normalizedModel = String(model || '').trim();
  if (!normalizedTarget || !normalizedBaseUrl || !normalizedModel) {
    return '';
  }
  return `telegram-workbench:ai-provider-key:${normalizedTarget}:${normalizedBaseUrl}:${normalizedModel}`;
}

function buildAiRecognitionConfigPayload() {
  const value = (selector) => document.querySelector(selector)?.value || '';
  const promptValues = collectAiPromptValues();
  const aiModels = collectAiModelConfigs();
  const activeTextModelId = value('[data-active-text-model-id]');
  const activeImageModelId = value('[data-active-image-model-id]');
  const activeTextModel = aiModels.find((model) => model.id === activeTextModelId) || null;
  const activeImageModel = aiModels.find((model) => model.id === activeImageModelId) || null;
  return {
    mode: 'ai_provider',
    recognition_prompt: promptValues.recognition_prompt || value('[data-ai-recognition-prompt-input]'),
    lifecycle_event_prompt: promptValues.lifecycle_event_prompt || value('[data-ai-lifecycle-event-prompt-input]'),
    mimo_direct_prompt: promptValues.mimo_direct_prompt || value('[data-ai-mimo-direct-prompt-input]'),
    active_text_model_id: activeTextModelId,
    active_image_model_id: activeImageModelId,
    ai_models: aiModels,
    text_provider: modelConfigToProvider(activeTextModel),
    image_provider: modelConfigToProvider(activeImageModel),
  };
}

function collectAiPromptValues() {
  return Array.from(document.querySelectorAll('[data-ai-prompt-input]')).reduce((prompts, input) => {
    const key = input.getAttribute('data-ai-prompt-input') || input.name;
    if (key) {
      prompts[key] = input.value || '';
    }
    return prompts;
  }, {});
}

function collectAiModelConfigs() {
  return Array.from(document.querySelectorAll('[data-ai-model-row]'))
    .map((row) => {
      const rowValue = (selector) => row.querySelector(selector)?.value || '';
      const parsedTimeout = Number(rowValue('[data-ai-model-timeout]'));
      return {
        id: rowValue('[data-ai-model-id]') || rowValue('[data-ai-model-name]'),
        label: rowValue('[data-ai-model-label]'),
        base_url: rowValue('[data-ai-model-base-url]'),
        api_key: rowValue('[data-ai-model-api-key]'),
        model: rowValue('[data-ai-model-name]'),
        timeout_seconds: Number.isFinite(parsedTimeout) && parsedTimeout > 0 ? parsedTimeout : 60,
        supports_text: Boolean(row.querySelector('[data-ai-model-supports-text]')?.checked),
        supports_image: Boolean(row.querySelector('[data-ai-model-supports-image]')?.checked),
      };
    })
    .filter((model) => model.id || model.model || model.base_url);
}

function modelConfigToProvider(model) {
  if (!model) {
    return {
      base_url: '',
      api_key: '',
      model: '',
      timeout_seconds: 60,
    };
  }
  return {
    base_url: model.base_url,
    api_key: model.api_key,
    model: model.model,
    timeout_seconds: model.timeout_seconds,
  };
}

function buildLegacyAiRecognitionConfigPayload() {
  const value = (selector) => document.querySelector(selector)?.value || '';
  const numericValue = (selector, fallback) => {
    const parsed = Number(value(selector));
    return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
  };
  return {
    mode: 'ai_provider',
    recognition_prompt: value('[data-ai-recognition-prompt-input]'),
    lifecycle_event_prompt: value('[data-ai-lifecycle-event-prompt-input]'),
    mimo_direct_prompt: value('[data-ai-mimo-direct-prompt-input]'),
    text_provider: {
      base_url: value('[data-ai-text-base-url]'),
      api_key: value('[data-ai-text-api-key]'),
      model: value('[data-ai-text-model]'),
      timeout_seconds: numericValue('[data-ai-text-timeout]', 60),
    },
    image_provider: {
      base_url: value('[data-ai-image-base-url]'),
      api_key: value('[data-ai-image-api-key]'),
      model: value('[data-ai-image-model]'),
      timeout_seconds: numericValue('[data-ai-image-timeout]', 60),
    },
  };
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
  const chatId = getSelectedChatId();
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
        chat_id: Number(chatId),
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

async function refreshCurrentGroupPanel(options = {}) {
  const chatId = getSelectedChatId();
  if (!chatId) return;

  const detailPanel = document.querySelector('[data-detail-panel]');
  if (!detailPanel) return;

  const currentMessagePanel = getMessagePanel();
  if (
    options.deferIfMessageListAwayFromTop &&
    currentMessagePanel &&
    !isMessagePanelAtTop(currentMessagePanel)
  ) {
    hasDeferredMessageRefresh = true;
    setNewMessagesButtonVisible(currentMessagePanel, true);
    return;
  }
  hasDeferredMessageRefresh = false;

  const currentScrollContainer = getMessageScrollContainer(currentMessagePanel);
  const previousMessageScrollTop = currentScrollContainer ? currentScrollContainer.scrollTop : 0;

  const nextContent = await fetchDetailPanel(chatId);
  if (!nextContent) return;

  detailPanel.innerHTML = '';
  detailPanel.appendChild(nextContent);
  bindDetailPanelControls();
  bindWorkflowFilters();

  const nextMessagePanel = getMessagePanel();
  if (options.scrollToTopAfterRefresh) {
    scrollMessagePanelToTop(nextMessagePanel);
    hasDeferredMessageRefresh = false;
  } else if (nextMessagePanel && options.preserveMessageScroll !== false) {
    const nextScrollContainer = getMessageScrollContainer(nextMessagePanel);
    if (nextScrollContainer) {
      nextScrollContainer.scrollTop = previousMessageScrollTop;
    }
  }
  if (nextMessagePanel && isMessagePanelAtTop(nextMessagePanel)) {
    setNewMessagesButtonVisible(nextMessagePanel, false);
  }

  // Also refresh the strategy mid panel
  await refreshStrategyMidPanel();

  if (options.showStatus !== false) {
    setAiStatus('Panel refreshed.');
  }
}

async function fetchFreshnessSnapshot() {
  const selectedChatId = getSelectedChatId();
  const url = selectedChatId ? `/api/freshness?chat_id=${selectedChatId}` : '/api/freshness';
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error('freshness request failed');
  }
  return response.json();
}

function snapshotKey(snapshot, scope) {
  const item = snapshot && snapshot[scope] ? snapshot[scope] : {};
  return [
    item.raw_message_id || 0,
    item.message_id || 0,
    item.message_count || 0,
    item.created_at || '',
    item.posted_at || '',
  ].join(':');
}

function hasNewerSelectedMessage(snapshot, previousSnapshot) {
  const current = snapshot && snapshot.selected ? snapshot.selected : {};
  const previous = previousSnapshot && previousSnapshot.selected ? previousSnapshot.selected : {};
  const currentMessageId = Number(current.message_id || 0);
  const previousMessageId = Number(previous.message_id || 0);
  const currentRawMessageId = Number(current.raw_message_id || 0);
  const previousRawMessageId = Number(previous.raw_message_id || 0);
  return currentMessageId > previousMessageId || currentRawMessageId > previousRawMessageId;
}

async function refreshFromDatabaseChanges() {
  let snapshot = null;
  try {
    snapshot = await fetchFreshnessSnapshot();
  } catch {
    setMonitorStatus({
      state: 'disconnected',
      label: '已断开',
      detail: 'Web 服务连接失败，等待恢复',
    });
    return;
  }

  if (!latestFreshnessSnapshot) {
    latestFreshnessSnapshot = snapshot;
    return;
  }

  const globalChanged =
    snapshotKey(snapshot, 'global') !== snapshotKey(latestFreshnessSnapshot, 'global');
  const selectedChanged =
    snapshotKey(snapshot, 'selected') !== snapshotKey(latestFreshnessSnapshot, 'selected');
  const selectedHasNewerMessage = hasNewerSelectedMessage(snapshot, latestFreshnessSnapshot);
  latestFreshnessSnapshot = snapshot;

  if (globalChanged) {
    await refreshGroupList();
  }
  if (selectedChanged) {
    const activeView = document.querySelector('[data-trader-dashboard]')?.dataset.activeWorkbenchView;
    if (activeView === 'messages') {
      await refreshCurrentGroupPanel({
        force: true,
        deferIfMessageListAwayFromTop: selectedHasNewerMessage,
      });
      markWorkbenchLoaded('messages');
      markWorkbenchLoaded('strategies');
    } else if (activeView === 'strategies') {
      await refreshStrategyMidPanel();
      markWorkbenchLoaded('strategies');
    }
  }
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
      if (!payload) return;
      const homePending = document.querySelector('[data-new-home-events]');
      if (homePending) {
        const count = Number(homePending.dataset.count || 0) + 1;
        homePending.dataset.count = String(count);
        homePending.textContent = `有 ${count} 条新动态`;
        homePending.hidden = false;
      }
      await refreshGroupList();
      const currentChatId = getSelectedChatId();
      if (Number(payload.chat_id || 0) !== currentChatId) {
        return;
      }
      const activeView = document.querySelector('[data-trader-dashboard]')?.dataset.activeWorkbenchView;
      if (activeView === 'messages') {
        await refreshCurrentGroupPanel({ deferIfMessageListAwayFromTop: true });
        markWorkbenchLoaded('messages');
        markWorkbenchLoaded('strategies');
      } else if (activeView === 'strategies') {
        await refreshStrategyMidPanel();
        markWorkbenchLoaded('strategies');
      }
    });
    let sseWasDisconnected = false;
    source.onerror = () => {
      sseWasDisconnected = true;
      setAiStatus('实时连接中断，自动重连中...', true);
      setMonitorStatus({
        state: 'reconnecting',
        label: '重连中',
        detail: '实时事件连接中断，浏览器将自动重连',
      });
      // Do NOT call source.close() — let the browser's built-in
      // EventSource reconnection handle it with exponential backoff.
    };
    source.onopen = () => {
      if (sseWasDisconnected) {
        // Only reload on reconnection, not on initial connect
        setAiStatus('实时连接已恢复，刷新界面...');
        window.location.reload();
      }
    };
    return;
  }
}

function startPollingUpdates() {
  window.setInterval(async () => {
    await refreshMonitorStatus();
    await refreshFromDatabaseChanges();
  }, 5000);
}

function scheduleRecoveryRefresh() {
  if (recoveryRefreshPromise) return recoveryRefreshPromise;
  recoveryRefreshPromise = (async () => {
    await refreshMonitorStatus();
    await refreshFromDatabaseChanges();
    const activeView = document.querySelector('[data-trader-dashboard]')?.dataset.activeWorkbenchView;
    if (activeView === 'home' || activeView === 'positions') {
      await ensureWorkbenchViewLoaded(activeView, { force: true });
    }
  })().finally(() => {
    recoveryRefreshPromise = null;
  });
  return recoveryRefreshPromise;
}

function requestLiveActionConfirmation(button) {
  const actionLabel = button.dataset.liveActionLabel || '执行此操作';
  const symbol = button.dataset.liveActionSymbol;
  const side = button.dataset.liveActionSide;
  const size = button.dataset.liveActionSize;
  const groupLabel = button.dataset.liveActionGroupLabel;
  const confirmationNote = button.dataset.liveActionConfirmationNote
    || '这只会更新项目状态，不会向 DeepCoin 下单。';
  const context = [
    actionLabel,
    symbol,
    side === 'long' ? '多' : side === 'short' ? '空' : side,
    size ? `数量 ${size}` : '',
    groupLabel,
  ].filter(Boolean).join(' · ');
  const dialog = document.querySelector('[data-live-action-confirm]');

  if (!dialog || typeof dialog.showModal !== 'function') {
    return Promise.resolve(window.confirm(`确认${context}？${confirmationNote}`));
  }

  const contextElement = dialog.querySelector('[data-live-action-confirm-context]');
  if (contextElement) {
    contextElement.textContent = `即将${context}。`;
  }
  const noteElement = dialog.querySelector('.live-action-confirm-note');
  if (noteElement) {
    noteElement.textContent = confirmationNote;
  }
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
    try {
      dialog.returnValue = '';
      dialog.showModal();
    } catch {
      resolve(window.confirm(`确认${context}？${confirmationNote}`));
    }
  });
}

function bindBoundPositionCloseButtons() {
  document.querySelectorAll('[data-close-bound-position]').forEach((button) => {
    button.addEventListener('click', async () => {
      const posId = button.dataset.posId;
      const card = button.closest('.exchange-position-card');
      const status = card ? card.querySelector('[data-close-bound-position-status]') : null;
      if (!posId) {
        return;
      }
      const confirmed = await requestLiveActionConfirmation(button);
      if (!confirmed) {
        return;
      }
      setMutationBusy(button, true);
      if (status) {
        status.textContent = '正在提交市价全平...';
        status.classList.remove('is-error');
      }
      try {
        const response = await fetch('/api/execution/close-bound-position', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pos_id: posId }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.detail || 'bound position close failed');
        }
        if (status) {
          status.textContent = '市价全平已提交，正在刷新...';
        }
        window.setTimeout(() => window.location.reload(), 400);
      } catch (error) {
        setMutationBusy(button, false);
        if (status) {
          status.textContent = error.message || '市价全平提交失败';
          status.classList.add('is-error');
        }
      }
    });
  });
}

function bindManualCloseButtons() {
  document.querySelectorAll('[data-manual-close-lifecycle]').forEach((button) => {
    button.addEventListener('click', async () => {
      const lifecycleId = button.dataset.lifecycleId;
      const card = button.closest('[data-execution-card]');
      const status = card ? card.querySelector('[data-manual-close-status]') : null;
      if (!lifecycleId) {
        return;
      }
      const confirmed = await requestLiveActionConfirmation(button);
      if (!confirmed) {
        return;
      }
      button.disabled = true;
      if (status) {
        status.textContent = '正在标记...';
        status.classList.remove('is-error');
      }
      try {
        const response = await fetch(`/api/strategy-lifecycles/${lifecycleId}/manual-close`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ note: 'web_manual_close' }),
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(payload.detail || 'manual close failed');
        }
        if (status) {
          status.textContent = '已标记，正在刷新...';
        }
        window.location.reload();
      } catch (error) {
        button.disabled = false;
        if (status) {
          status.textContent = error.message || '标记失败';
          status.classList.add('is-error');
        }
      }
    });
  });
}

function bindDeepcoinPositionSync() {
  const button = document.querySelector('[data-sync-deepcoin-positions]');
  const status = document.querySelector('[data-sync-deepcoin-status]');
  if (!button) {
    return;
  }
  button.addEventListener('click', async () => {
    button.disabled = true;
    if (status) {
      status.textContent = '正在同步...';
      status.classList.remove('is-error');
    }
    try {
      const response = await fetch('/api/execution/sync-deepcoin', { method: 'POST' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || 'sync failed');
      }
      if (status) {
        status.textContent = `已同步，手动归档 ${payload.manually_closed || 0} 笔`;
      }
      window.setTimeout(() => window.location.reload(), 500);
    } catch (error) {
      button.disabled = false;
      if (status) {
        status.textContent = error.message || '同步失败';
        status.classList.add('is-error');
      }
    }
  });
}

function initLogViewer() {
  const viewer = document.querySelector('[data-log-viewer]');
  if (!viewer) {
    return;
  }

  const levelFilter = viewer.querySelector('[data-log-level-filter]');
  const refreshButton = viewer.querySelector('[data-log-refresh]');
  const status = viewer.querySelector('[data-log-status]');
  const container = viewer.querySelector('[data-log-list]');
  const pageSize = 100;
  let offset = 0;
  let hasMore = true;
  let loading = false;
  let requestVersion = 0;
  let reloadPending = false;

  const setStatus = (message, isError = false) => {
    status.textContent = message;
    status.classList.toggle('is-error', isError);
  };
  const appendLogEntry = (entry) => {
    const article = document.createElement('article');
    article.className = `log-entry log-entry--${entry.level.toLowerCase()}`;
    const meta = document.createElement('div');
    meta.className = 'log-entry-meta';
    meta.textContent = `${entry.timestamp} ${entry.level} ${entry.logger}`;
    const message = document.createElement('pre');
    message.className = 'log-entry-message';
    message.textContent = entry.message;
    article.append(meta, message);
    container.append(article);
  };
  const loadEntries = async () => {
    if (loading || !hasMore) {
      return;
    }
    loading = true;
    const activeRequestVersion = requestVersion;
    setStatus('正在加载…');
    const params = new URLSearchParams({ offset: String(offset), limit: String(pageSize) });
    if (levelFilter.value) {
      params.set('level', levelFilter.value);
    }
    try {
      const response = await fetch(`/api/logs?${params}`);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || '加载日志失败');
      }
      if (activeRequestVersion !== requestVersion) {
        return;
      }
      const entries = payload.items || [];
      entries.forEach(appendLogEntry);
      offset = payload.next_offset;
      hasMore = payload.has_more;
      if (!entries.length && offset === 0) {
        container.textContent = '暂无匹配的日志。';
      }
      setStatus(hasMore ? '继续向下滚动以加载更多。' : '已加载全部日志。');
    } catch (error) {
      setStatus(error.message || '加载日志失败', true);
    } finally {
      loading = false;
      if (reloadPending) {
        reloadPending = false;
        loadEntries();
      }
    }
  };
  const resetAndLoad = () => {
    requestVersion += 1;
    offset = 0;
    hasMore = true;
    container.replaceChildren();
    if (loading) {
      reloadPending = true;
      return Promise.resolve();
    }
    return loadEntries();
  };

  levelFilter.addEventListener('change', resetAndLoad);
  refreshButton.addEventListener('click', resetAndLoad);
  container.addEventListener('scroll', () => {
    if (container.scrollTop + container.clientHeight >= container.scrollHeight - 80) {
      loadEntries();
    }
  }, { passive: true });
  resetAndLoad();
}

function bindLivePositionAttributionButtons() {
  document.querySelectorAll('[data-bind-live-position]').forEach((button) => {
    button.addEventListener('click', async () => {
      const posId = button.dataset.posId;
      const lifecycleId = button.dataset.lifecycleId;
      const block = button.closest('.execution-attribution');
      const status = block ? block.querySelector('[data-bind-live-position-status]') : null;
      if (!posId || !lifecycleId) {
        return;
      }
      const confirmed = await requestLiveActionConfirmation(button);
      if (!confirmed) {
        return;
      }
      setMutationBusy(button, true);
      if (status) {
        status.textContent = '正在绑定...';
        status.classList.remove('is-error');
      }
      try {
        const response = await fetch('/api/execution/bind-live-position', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pos_id: posId, lifecycle_id: lifecycleId }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.detail || 'bind failed');
        }
        if (status) {
          status.textContent = '已绑定，正在刷新...';
        }
        window.setTimeout(() => window.location.reload(), 400);
      } catch (error) {
        setMutationBusy(button, false);
        if (status) {
          status.textContent = error.message || '绑定失败';
          status.classList.add('is-error');
        }
      }
    });
  });
}

window.addEventListener('DOMContentLoaded', () => {
  const form = document.querySelector('[data-ai-form]');
  if (form) {
    form.addEventListener('submit', submitAiQuestion);
  }
  bindGroupLinks();
  bindGroupAutomationToggles();
  bindDetailPanelControls();
  bindDashboardTabs();
  bindMobileWorkNavigation();
  bindHomeEventFilters();
  bindGroupContext();
  bindWorkflowFilters();
  bindExchangePositionTabs();
  bindTradingSettingsForm();
  bindStrategyFilterBadges();
  bindAiRecognitionPromptForm();
  bindAiModelSelectionForm();
  bindGroupPromptEditor();
  bindClearAiHistory();
  bindBoundPositionCloseButtons();
  bindManualCloseButtons();
  bindDeepcoinPositionSync();
  initLogViewer();
  bindLivePositionAttributionButtons();
  renderConversationHistory();
  setAiStatus('');
  resetInitialMessagePanelScroll();
  connectLiveUpdates();
  refreshMonitorStatus();
  refreshFromDatabaseChanges();
  startPollingUpdates();

  // ── Refresh immediately when the tab gains focus (catch up) ──────
  window.addEventListener('focus', () => {
    scheduleRecoveryRefresh();
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      scheduleRecoveryRefresh();
    }
  });
});
