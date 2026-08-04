let latestFreshnessSnapshot = null;
let currentSelectedChatId = null;
let groupSwitchRequestId = 0;
let activeGroupSwitchController = null;
let promptCenterRequestId = 0;
const PROMPT_API_ACTIONS = ['/draft', '/validate', '/test', '/publish', '/history', '/rollback'];
let promptCenterState = {
  items: [], selected: null, validated: false, tested: false, chatId: null,
};
let hasDeferredMessageRefresh = false;
let recoveryRefreshPromise = null;
let pendingPositionsFragment = null;
const POSITION_SNAPSHOT_RETRY_DELAYS = [1000, 2000, 4000];
let positionSnapshotRetryTimer = null;
let positionSnapshotRetryToken = 0;
let positionSnapshotRetryAttempt = 0;
const exchangePositionTabRequests = new WeakMap();
const STRATEGY_RECORD_FILTER_KEY = 'telegram-workbench:strategy-filter';
const STRATEGY_RECORD_GROUP_KEY = 'telegram-workbench:strategy-group';
const STRATEGY_RECORD_SCROLL_KEY = 'telegram-workbench:strategy-scroll';
const EXCHANGE_POSITION_VIEW_KEY = 'telegram-workbench:exchange-position-view';
const EXCHANGE_POSITION_TAB_KEY = 'telegram-workbench:exchange-position-tab';
const EXCHANGE_POSITION_TABS = [
  'positions',
  'open-orders',
  'order-history',
  'position-history',
];
let strategyRecordRequestId = 0;
let strategyRecordHasPendingChanges = false;
let lastSuccessfulStrategyRecordAt = null;
let lastSuccessfulStrategyRecordSelection = null;

const workbenchLoadState = {
  strategies: { key: null, promise: null },
  positions: { key: null, promise: null },
  activity: { key: null, promise: null },
  groups: { key: null, promise: null },
  more: { key: null, promise: null },
  'management-batches': { key: null, promise: null },
};

const MESSAGE_TOP_THRESHOLD = 24;
const MESSAGE_LOAD_MORE_THRESHOLD = 320;

function beginGroupSwitchRequest() {
  if (activeGroupSwitchController) activeGroupSwitchController.abort();
  activeGroupSwitchController = new AbortController();
  return activeGroupSwitchController;
}

function handleGroupDetailCompanionError(error, requestId) {
  if (error?.name === 'AbortError' || requestId !== groupSwitchRequestId) return;
  setAiStatus('消息加载失败，请重试。', true);
}

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

async function fetchDetailPanel(chatId, options = {}) {
  const url = `/groups/${chatId}/detail?_t=${Date.now()}`;
  const response = await fetch(url, {
    cache: 'no-store',
    signal: options.signal,
  });
  if (!response.ok) throw new Error(`detail request failed: ${response.status}`);
  const html = await response.text();
  const parser = new DOMParser();
  const doc = parser.parseFromString(html, 'text/html');
  const fragment = doc.querySelector('.strategy-detail-shell');
  if (!fragment) throw new Error('detail response missing strategy-detail-shell');
  return fragment;
}

async function fetchStrategyMidPanel(chatId, filter, options = {}) {
  const url = `/groups/${chatId}/strategy-mid-panel?filter=${filter}&_t=${Date.now()}`;
  const response = await fetch(url, {
    cache: 'no-store',
    signal: options.signal,
  });
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

async function loadMoreMessages(panel) {
  const loadMoreButton = panel?.querySelector('[data-load-more]');
  if (!loadMoreButton || loadMoreButton.dataset.loading === 'true') return;

  const { chatId, searchText, senderName } = getMessageFilterState(panel);
  const beforeMessageId = Number(loadMoreButton.dataset.beforeMessageId || '0');
  if (!chatId || !beforeMessageId) return;

  loadMoreButton.dataset.loading = 'true';
  loadMoreButton.disabled = true;
  loadMoreButton.textContent = '加载中…';
  try {
    const nextPanel = await fetchMessagePanel(chatId, {
      beforeMessageId,
      searchText,
      senderName,
    });
    if (!panel.isConnected || Number(panel.dataset.chatId || '0') !== chatId) return;

    const nextList = nextPanel?.querySelector('[data-message-list]');
    const currentList = panel.querySelector('[data-message-list]');
    const currentFooter = panel.querySelector('[data-message-list-footer]');
    const nextFooter = nextPanel?.querySelector('[data-message-list-footer]');
    if (!currentList || !nextList || !currentFooter || !nextFooter) {
      throw new Error('message history response incomplete');
    }

    currentList.insertAdjacentHTML('beforeend', nextList.innerHTML);
    currentFooter.replaceWith(nextFooter);
    bindMessagePanelControls(panel);
  } catch {
    if (!panel.isConnected) return;
    loadMoreButton.dataset.loading = 'false';
    loadMoreButton.disabled = false;
    loadMoreButton.textContent = '加载失败，点击重试';
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
  if (scrollContainer && scrollContainer.dataset.historyScrollBound !== 'true') {
    scrollContainer.dataset.historyScrollBound = 'true';
    scrollContainer.addEventListener('scroll', () => {
      const remaining = (
        scrollContainer.scrollHeight
        - scrollContainer.scrollTop
        - scrollContainer.clientHeight
      );
      if (remaining <= MESSAGE_LOAD_MORE_THRESHOLD) loadMoreMessages(panel);
    }, { passive: true });
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
  if (filterForm && filterForm.dataset.messageFiltersBound !== 'true') {
    filterForm.dataset.messageFiltersBound = 'true';
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
  if (
    clearButton
    && filterForm
    && clearButton.dataset.clearMessageFiltersBound !== 'true'
  ) {
    clearButton.dataset.clearMessageFiltersBound = 'true';
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
  if (loadMoreButton && loadMoreButton.dataset.loadMoreBound !== 'true') {
    loadMoreButton.dataset.loadMoreBound = 'true';
    loadMoreButton.addEventListener('click', () => loadMoreMessages(panel));
  }

  const refreshButton = panel.querySelector('[data-refresh-now]');
  if (refreshButton && refreshButton.dataset.refreshNowBound !== 'true') {
    refreshButton.dataset.refreshNowBound = 'true';
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
    if (button.dataset.recognizeMessageBound === 'true') return;
    button.dataset.recognizeMessageBound = 'true';
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
  const activeView = document.querySelector('[data-trader-dashboard]')?.dataset.activeWorkbenchView || 'groups';
  const detailPanel = getDetailPanelForWorkbenchView(activeView);
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
      const controller = beginGroupSwitchRequest();
      hasDeferredMessageRefresh = false;
      const filterInput = document.querySelector('[data-strategy-filter-input]');
      const filter = filterInput ? filterInput.value : 'holding';
      const activeView = document.querySelector('[data-trader-dashboard]')?.dataset.activeWorkbenchView === 'activity'
        ? 'activity'
        : 'groups';
      const detailPanel = getDetailPanelForWorkbenchView(activeView);
      const strategyPanel = document.querySelector('[data-strategy-panel]');
      const detailPromise = activeView === 'groups'
        ? fetchDetailPanel(chatId, { signal: controller.signal })
        : null;
      if (detailPromise) detailPromise.catch(() => {});
      setAiStatus('');
      document.dispatchEvent(new CustomEvent('group-context-pending', { detail: { chatId } }));
      try {
        await loadVisibleGroupDestination({
          activeView,
          chatId,
          filter,
          detailPanel,
          strategyPanel,
          requestId,
          signal: controller.signal,
        });
        if (requestId !== groupSwitchRequestId) return;
        markWorkbenchLoaded(activeView, chatId);
        syncSelectedGroupState(chatId, { focus: true });
        if (detailPromise) {
          loadGroupDetailCompanion({
            chatId,
            detailPanel,
            requestId,
            detailPromise,
          }).catch((error) => handleGroupDetailCompanionError(error, requestId));
        }
        applyGroupPromptToEditor(String(chatId));
        renderConversationHistory();
        document.dispatchEvent(new CustomEvent('group-context-success', { detail: { chatId } }));
        refreshGroupList().catch(() => {});
      } catch (error) {
        controller.abort();
        if (error?.name === 'AbortError') return;
        if (requestId === groupSwitchRequestId) {
          setAiStatus('群组切换失败，请重试。', true);
          document.dispatchEvent(new CustomEvent('group-context-error', { detail: { chatId } }));
        }
      }
    });
  });
}

async function loadVisibleGroupDestination({
  activeView,
  chatId,
  filter,
  detailPanel,
  strategyPanel,
  requestId,
  signal,
}) {
  if (activeView === 'activity') {
    const nextContent = await fetchDetailPanel(chatId, { signal });
    if (requestId !== groupSwitchRequestId) return false;
    if (!detailPanel) throw new Error('missing detail panel');
    detailPanel.innerHTML = '';
    detailPanel.appendChild(nextContent);
    bindDetailPanelControls();
    bindWorkflowFilters();
    return true;
  }
  const nextStrategyContent = await fetchStrategyMidPanel(chatId, filter, { signal });
  if (requestId !== groupSwitchRequestId) return false;
  if (!strategyPanel) throw new Error('missing strategy panel');
  strategyPanel.innerHTML = '';
  strategyPanel.appendChild(nextStrategyContent);
  bindStrategyFilterBadges();
  bindWorkflowFilters();
  return true;
}

function getDetailPanelForWorkbenchView(view = null) {
  const dashboard = document.querySelector('[data-trader-dashboard]');
  const activeView = view || dashboard?.dataset.activeWorkbenchView || 'groups';
  const scopedPanel = document.querySelector(`[data-workbench-panel="${activeView}"] [data-detail-panel]`);
  if (scopedPanel) return scopedPanel;
  return document.querySelector('[data-workbench-panel].is-active [data-detail-panel]')
    || document.querySelector('[data-detail-panel]');
}

async function loadGroupDetailCompanion({
  chatId,
  detailPanel,
  requestId,
  detailPromise,
}) {
  if (!detailPanel) return;
  const nextContent = await detailPromise;
  if (requestId !== groupSwitchRequestId || getSelectedChatId() !== chatId) return;
  detailPanel.innerHTML = '';
  detailPanel.appendChild(nextContent);
  bindDetailPanelControls();
  bindWorkflowFilters();
  markWorkbenchLoaded('messages', chatId);
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
  if (view === 'activity' || view === 'groups' || view === 'management-batches') {
    return String(getSelectedChatId() || 0);
  }
  return 'global';
}

function markWorkbenchLoaded(view, key = workbenchLoadKey(view)) {
  if (workbenchLoadState[view]) {
    workbenchLoadState[view].key = String(key);
  }
}

function showWorkbenchLoadError(view, error, retryLoader = null) {
  const container = document.querySelector(`[data-lazy-workbench="${view}"]`);
  if (!container) return;
  if (view === 'more') {
    container.querySelectorAll('.workbench-load-error').forEach((notice) => notice.remove());
  } else {
    container.innerHTML = '';
  }
  container.setAttribute('aria-busy', 'false');
  const message = document.createElement('p');
  message.className = 'workbench-load-error';
  message.textContent = `加载失败：${error?.message || '请检查服务状态'}`;
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.className = 'secondary-button';
  retry.textContent = '重新加载';
  retry.addEventListener('click', () => {
    const load = retryLoader || (() => ensureWorkbenchViewLoaded(view, { force: true }));
    load();
  });
  container.append(message, retry);
}

function showActivityBootstrapError(error) {
  showWorkbenchLoadError('activity', error, retryActivityAfterGroups);
}

async function retryActivityAfterGroups() {
  const groupsLoaded = await ensureWorkbenchViewLoaded('groups', { force: true });
  if (!groupsLoaded || !getSelectedChatId()) {
    showActivityBootstrapError(new Error('群组加载失败或暂无可用群组'));
    return false;
  }
  return ensureWorkbenchViewLoaded('activity', { force: true });
}

function showDashboardPanelLoadError(tab, error) {
  const activeWorkbench = document.querySelector('[data-workbench-panel].is-active');
  const host = activeWorkbench?.querySelector('[data-lazy-workbench]') || activeWorkbench;
  if (!host) return;
  host.querySelector('[data-dashboard-load-error]')?.remove();
  const notice = document.createElement('aside');
  notice.className = 'workbench-load-error';
  notice.dataset.dashboardLoadError = '';
  notice.setAttribute('role', 'alert');
  const message = document.createElement('p');
  message.textContent = `设置加载失败：${error?.message || '请检查服务状态'}`;
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.className = 'secondary-button';
  retry.textContent = '重新加载';
  retry.addEventListener('click', () => {
    retryDashboardPanelLoad(tab).catch((retryError) => showDashboardPanelLoadError(tab, retryError));
  });
  notice.append(message, retry);
  host.prepend(notice);
}

async function retryDashboardPanelLoad(tab) {
  workbenchLoadState.more.key = null;
  const moreReloaded = await ensureWorkbenchViewLoaded('more', { force: true });
  if (!moreReloaded) {
    showDashboardPanelLoadError(tab, new Error('更多工具重新加载失败'));
    return false;
  }
  return openDashboardPanel(tab);
}

function clearDashboardPanelLoadError() {
  document.querySelectorAll('[data-dashboard-load-error]').forEach((notice) => notice.remove());
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

function strategyRecordStorageGet(key, fallback = '') {
  try {
    return window.localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function strategyRecordStorageSet(key, value) {
  try {
    window.localStorage.setItem(key, String(value));
  } catch {
    // The list remains usable when private browsing disables storage.
  }
}

function strategyRecordSelectionFromRoot(root = document.querySelector('[data-strategy-record-list]')) {
  const lazyContainer = document.querySelector('[data-lazy-workbench="strategies"]');
  const defaultUrl = lazyContainer?.dataset.strategyRecordsUrl || '/strategy-records?filter=needs_attention';
  const defaultFilter = new URLSearchParams(defaultUrl.split('?')[1] || '').get('filter') || 'needs_attention';
  const activeFilter = root?.querySelector('[data-strategy-record-filter][aria-current="page"]');
  const groupFilter = root?.querySelector('[data-strategy-group-filter]');
  return {
    filter: activeFilter?.dataset.strategyRecordFilter
      || strategyRecordStorageGet(STRATEGY_RECORD_FILTER_KEY, defaultFilter),
    group: groupFilter ? groupFilter.value : strategyRecordStorageGet(STRATEGY_RECORD_GROUP_KEY, ''),
    limit: root?.dataset.strategyRecordLimit || '100',
    page: root?.dataset.strategyRecordPage || '1',
  };
}

function currentStrategyRecordParams(selection = strategyRecordSelectionFromRoot()) {
  const params = new URLSearchParams({ filter: selection.filter, limit: selection.limit, page: selection.page || '1' });
  if (selection.group) params.set('chat_id', selection.group);
  return params;
}

function strategyRecordSelectionMatches(selection) {
  const current = strategyRecordSelectionFromRoot();
  return current.filter === selection.filter && current.group === selection.group && current.page === selection.page;
}

function applyStrategyRecordSelection(selection) {
  const root = document.querySelector('[data-strategy-record-list]');
  if (!root || !selection) return;
  root.querySelectorAll('[data-strategy-record-filter]').forEach((item) => {
    const selected = item.dataset.strategyRecordFilter === selection.filter;
    item.classList.toggle('is-active', selected);
    if (selected) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  const groupFilter = root.querySelector('[data-strategy-group-filter]');
  if (groupFilter) groupFilter.value = selection.group;
}

function commitSuccessfulStrategyRecordSelection(root) {
  const selection = strategyRecordSelectionFromRoot(root);
  lastSuccessfulStrategyRecordSelection = selection;
  strategyRecordStorageSet(STRATEGY_RECORD_FILTER_KEY, selection.filter);
  strategyRecordStorageSet(STRATEGY_RECORD_GROUP_KEY, selection.group);
}

function rollbackStrategyRecordSelection() {
  applyStrategyRecordSelection(lastSuccessfulStrategyRecordSelection);
}

function getStrategyRecordScrollTop() {
  const surface = document.querySelector('[data-strategy-record-scroll]');
  return Math.max(Number(surface?.scrollTop || 0), Number(window.scrollY || 0));
}

function saveStrategyRecordScrollPosition() {
  strategyRecordStorageSet(STRATEGY_RECORD_SCROLL_KEY, getStrategyRecordScrollTop());
}

function restoreStrategyRecordScrollPosition() {
  const top = Number(strategyRecordStorageGet(STRATEGY_RECORD_SCROLL_KEY, '0'));
  if (!Number.isFinite(top) || top < 0) return;
  window.requestAnimationFrame(() => {
    const surface = document.querySelector('[data-strategy-record-scroll]');
    if (surface) surface.scrollTop = top;
    window.scrollTo({ top, behavior: 'auto' });
  });
}

function resetStrategyRecordScrollPosition() {
  strategyRecordStorageSet(STRATEGY_RECORD_SCROLL_KEY, 0);
  window.requestAnimationFrame(() => {
    const surface = document.querySelector('[data-strategy-record-scroll]');
    if (surface) surface.scrollTop = 0;
    window.scrollTo({ top: 0, behavior: 'auto' });
  });
}

function formatStrategyRecordSuccessTime(value) {
  if (!value) return '尚未完成更新';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return `上次成功更新：${value}`;
  return `上次成功更新：${date.toLocaleString()}`;
}

function updateStrategyRecordStatus({ error = null } = {}) {
  const root = document.querySelector('[data-strategy-record-list]');
  const status = root?.querySelector('[data-strategy-record-status]');
  const timestamp = root?.querySelector('[data-strategy-record-last-success]');
  const retry = root?.querySelector('[data-strategy-record-retry]');
  if (!status || !timestamp || !retry) return;
  timestamp.textContent = error
    ? `数据可能不是最新：${error.message || '请检查服务状态'}。${formatStrategyRecordSuccessTime(lastSuccessfulStrategyRecordAt)}`
    : formatStrategyRecordSuccessTime(lastSuccessfulStrategyRecordAt);
  status.classList.toggle('is-error', Boolean(error));
  status.setAttribute('role', error ? 'alert' : 'status');
  retry.hidden = !error;
}

function updateStrategyRecordChangesBadge() {
  const badge = document.querySelector('[data-strategy-new-changes]');
  if (!badge) return;
  badge.hidden = !strategyRecordHasPendingChanges;
  badge.textContent = '有新变化，点击查看';
}

function noteStrategyRecordChanges() {
  strategyRecordHasPendingChanges = true;
  updateStrategyRecordChangesBadge();
}

function showStrategyRecordLoadError(error) {
  const container = document.querySelector('[data-lazy-workbench="strategies"]');
  const root = document.querySelector('[data-strategy-record-list]');
  if (root) {
    updateStrategyRecordStatus({ error });
    return;
  }
  if (!container) return;
  let notice = container.querySelector('[data-strategy-record-bootstrap-error]');
  if (!notice) {
    notice = document.createElement('aside');
    notice.dataset.strategyRecordBootstrapError = '';
    notice.className = 'workbench-load-error';
    notice.setAttribute('role', 'alert');
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'secondary-button';
    retry.dataset.strategyRecordRetry = '';
    retry.textContent = '重试';
    retry.addEventListener('click', () => loadStrategyRecords({
      force: true, revealChanges: true, scrollMode: 'restore',
    }));
    notice.append(document.createElement('span'), retry);
    container.appendChild(notice);
  }
  notice.querySelector('span').textContent = `加载失败：${error.message || '请检查服务状态'}`;
}

function replaceStrategyRecordList(fragment, { scrollMode }) {
  const container = document.querySelector('[data-lazy-workbench="strategies"]');
  const current = document.querySelector('[data-strategy-record-list]');
  if (container) {
    container.replaceChildren(fragment);
  } else if (current) {
    current.replaceWith(fragment);
  }
  bindStrategyRecordController();
  if (scrollMode === 'reset') {
    resetStrategyRecordScrollPosition();
  } else {
    restoreStrategyRecordScrollPosition();
  }
}

async function loadStrategyRecords({
  force = false,
  revealChanges = false,
  attemptedSelection = null,
  scrollMode = null,
} = {}) {
  const container = document.querySelector('[data-lazy-workbench="strategies"]');
  const current = document.querySelector('[data-strategy-record-list]');
  if (!container && !current) return false;
  const requestId = ++strategyRecordRequestId;
  const selection = attemptedSelection || strategyRecordSelectionFromRoot(current);
  const params = currentStrategyRecordParams(selection);
  const resolvedScrollMode = scrollMode || (current ? 'preserve' : 'restore');
  if (current && resolvedScrollMode === 'preserve') saveStrategyRecordScrollPosition();
  if (container) container.setAttribute('aria-busy', 'true');
  try {
    const response = await fetch(`/strategy-records?${params}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`请求失败 (${response.status})`);
    const html = await response.text();
    if (requestId !== strategyRecordRequestId) return false;
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const fragment = doc.querySelector('[data-strategy-record-list]');
    if (!fragment) throw new Error('返回内容不完整');
    lastSuccessfulStrategyRecordAt = new Date().toISOString();
    commitSuccessfulStrategyRecordSelection(fragment);
    if (revealChanges) strategyRecordHasPendingChanges = false;
    replaceStrategyRecordList(fragment, { scrollMode: resolvedScrollMode });
    updateStrategyRecordStatus();
    updateStrategyRecordChangesBadge();
    return true;
  } catch (error) {
    if (requestId !== strategyRecordRequestId) return false;
    if (strategyRecordSelectionMatches(selection)) {
      rollbackStrategyRecordSelection();
    }
    showStrategyRecordLoadError(error);
    return false;
  } finally {
    if (requestId === strategyRecordRequestId && container) {
      container.setAttribute('aria-busy', 'false');
    }
  }
}

function bindStrategyRecordController() {
  const root = document.querySelector('[data-strategy-record-list]');
  if (!root) return;
  if (!lastSuccessfulStrategyRecordAt) {
    lastSuccessfulStrategyRecordAt = root.querySelector('[data-last-success-at]')?.dataset.lastSuccessAt || null;
  }
  if (!lastSuccessfulStrategyRecordSelection) {
    commitSuccessfulStrategyRecordSelection(root);
  }
  const groupFilter = root.querySelector('[data-strategy-group-filter]');
  root.querySelectorAll('[data-strategy-record-filter]').forEach((filter) => {
    if (filter.dataset.strategyRecordFilterBound === 'true') return;
    filter.dataset.strategyRecordFilterBound = 'true';
    filter.addEventListener('click', (event) => {
      event.preventDefault();
      root.querySelectorAll('[data-strategy-record-filter]').forEach((item) => {
        const selected = item === filter;
        item.classList.toggle('is-active', selected);
        if (selected) item.setAttribute('aria-current', 'page');
        else item.removeAttribute('aria-current');
      });
      const attemptedSelection = { ...strategyRecordSelectionFromRoot(root), page: '1' };
      loadStrategyRecords({ force: true, attemptedSelection, scrollMode: 'reset' });
    });
  });
  if (groupFilter && groupFilter.dataset.strategyGroupFilterBound !== 'true') {
    groupFilter.dataset.strategyGroupFilterBound = 'true';
    groupFilter.addEventListener('change', () => {
      const attemptedSelection = { ...strategyRecordSelectionFromRoot(root), page: '1' };
      loadStrategyRecords({ force: true, attemptedSelection, scrollMode: 'reset' });
    });
  }
  root.querySelectorAll('[data-strategy-record-refresh], [data-strategy-record-retry], [data-strategy-new-changes]')
    .forEach((button) => {
      if (button.dataset.strategyRecordActionBound === 'true') return;
      button.dataset.strategyRecordActionBound = 'true';
      button.addEventListener('click', () => loadStrategyRecords({
        force: true, revealChanges: true, scrollMode: 'preserve',
      }));
    });
  root.querySelectorAll('[data-strategy-record-card]').forEach((card) => {
    if (card.dataset.strategyRecordNavigationBound === 'true') return;
    card.dataset.strategyRecordNavigationBound = 'true';
    card.addEventListener('click', saveStrategyRecordScrollPosition);
  });
  root.querySelectorAll('[data-strategy-record-next-page]').forEach((link) => {
    if (link.dataset.strategyRecordNextBound === 'true') return;
    link.dataset.strategyRecordNextBound = 'true';
    link.addEventListener('click', (event) => {
      event.preventDefault();
      const attemptedSelection = {
        ...strategyRecordSelectionFromRoot(root),
        page: link.dataset.strategyRecordNextPage,
      };
      loadStrategyRecords({ force: true, attemptedSelection, scrollMode: 'reset' });
    });
  });
  updateStrategyRecordStatus();
  updateStrategyRecordChangesBadge();
}

function commitPositionsPanel(fragment, { preserveSnapshotRetryBudget = false } = {}) {
  const container = document.querySelector('[data-lazy-workbench="positions"]');
  if (!container || !fragment) return false;
  const current = container.querySelector('[data-exchange-position-tabs]');
  const uiState = exchangePositionUiState(current);
  container.replaceChildren(fragment);
  pendingPositionsFragment = null;
  bindDashboardTabs();
  bindExchangePositionTabs();
  applyExchangePositionUiState(fragment, uiState);
  bindBoundPositionCloseButtons();
  bindDeepcoinPositionSync();
  bindLivePositionAttributionButtons();
  schedulePositionSnapshotRefresh(fragment, {
    preserveRetryBudget: preserveSnapshotRetryBudget,
  });
  return true;
}

function clearPendingPositionsRefreshNotice() {
  document.querySelector('[data-positions-refresh-notice]')?.remove();
}

function applyPendingPositionsRefresh() {
  if (!pendingPositionsFragment) return false;
  const fragment = pendingPositionsFragment;
  clearPendingPositionsRefreshNotice();
  return commitPositionsPanel(fragment);
}

function showPendingPositionsRefreshNotice() {
  const container = document.querySelector('[data-lazy-workbench="positions"]');
  if (!container || container.querySelector('[data-positions-refresh-notice]')) return;
  const notice = document.createElement('aside');
  notice.className = 'positions-refresh-notice';
  notice.dataset.positionsRefreshNotice = '';
  notice.setAttribute('role', 'status');
  notice.setAttribute('aria-live', 'polite');
  const message = document.createElement('span');
  message.textContent = '检测到新的持仓数据';
  const applyButton = document.createElement('button');
  applyButton.type = 'button';
  applyButton.className = 'secondary-button';
  applyButton.textContent = '点击更新';
  applyButton.addEventListener('click', applyPendingPositionsRefresh);
  notice.append(message, applyButton);
  container.prepend(notice);
}

function positionsPanelComparableMarkup(root) {
  const clone = root.cloneNode(true);
  setExchangePositionTab(clone, 'positions');
  setExchangePositionView(clone, 'list');
  clone.querySelectorAll('[data-exchange-position-panel]:not([data-exchange-position-panel="positions"])')
    .forEach((panel) => panel.remove());
  clone.querySelectorAll('details[open]').forEach((details) => {
    details.removeAttribute('open');
  });
  clone.querySelectorAll('.strategy-record-position-target').forEach((item) => {
    item.classList.remove('strategy-record-position-target');
  });
  return clone.outerHTML;
}

async function checkPositionsPanelForChanges({ applySnapshotRefresh = false } = {}) {
  const container = document.querySelector('[data-lazy-workbench="positions"]');
  const current = container?.querySelector('[data-exchange-position-tabs]');
  if (!container || !current) return false;
  try {
    const fragment = await fetchWorkbenchPartial('/positions-panel?initial=positions', '[data-exchange-position-tabs]');
    if (current !== container.querySelector('[data-exchange-position-tabs]')) return false;
    const uiState = exchangePositionUiState(current);
    applyExchangePositionUiState(fragment, uiState);
    if (
      positionsPanelComparableMarkup(current)
      === positionsPanelComparableMarkup(fragment)
    ) {
      pendingPositionsFragment = null;
      clearPendingPositionsRefreshNotice();
      return false;
    }
    if (
      applySnapshotRefresh
      && current.dataset.positionSnapshotState !== 'current'
    ) {
      clearPendingPositionsRefreshNotice();
      return commitPositionsPanel(fragment, {
        preserveSnapshotRetryBudget: true,
      });
    }
    pendingPositionsFragment = fragment;
    showPendingPositionsRefreshNotice();
    return true;
  } catch {
    return false;
  }
}

function cancelPositionSnapshotRefresh({ resetRetryBudget = true } = {}) {
  positionSnapshotRetryToken += 1;
  if (positionSnapshotRetryTimer !== null) {
    window.clearTimeout(positionSnapshotRetryTimer);
    positionSnapshotRetryTimer = null;
  }
  if (resetRetryBudget) positionSnapshotRetryAttempt = 0;
}

function schedulePositionSnapshotRefresh(
  root,
  { preserveRetryBudget = false } = {},
) {
  cancelPositionSnapshotRefresh({ resetRetryBudget: !preserveRetryBudget });
  if (!root || root.dataset.positionSnapshotState === 'current') return;
  if (positionSnapshotRetryAttempt >= POSITION_SNAPSHOT_RETRY_DELAYS.length) return;
  const token = positionSnapshotRetryToken;
  const delay = POSITION_SNAPSHOT_RETRY_DELAYS[positionSnapshotRetryAttempt];
  positionSnapshotRetryAttempt += 1;
  positionSnapshotRetryTimer = window.setTimeout(async () => {
    positionSnapshotRetryTimer = null;
    const activeView = document.querySelector('[data-trader-dashboard]')?.dataset.activeWorkbenchView;
    const current = document.querySelector('[data-lazy-workbench="positions"] [data-exchange-position-tabs]');
    if (
      token !== positionSnapshotRetryToken
      || activeView !== 'positions'
      || current !== root
    ) {
      return;
    }
    await checkPositionsPanelForChanges({ applySnapshotRefresh: true });
    if (token !== positionSnapshotRetryToken) return;
    const refreshed = document.querySelector('[data-lazy-workbench="positions"] [data-exchange-position-tabs]');
    if (!refreshed || refreshed.dataset.positionSnapshotState === 'current') return;
    schedulePositionSnapshotRefresh(refreshed, { preserveRetryBudget: true });
  }, delay);
}

async function loadPositionsPanel() {
  const fragment = await fetchWorkbenchPartial('/positions-panel?initial=positions', '[data-exchange-position-tabs]');
  commitPositionsPanel(fragment);
}

function persistedSelectedChatId() {
  try {
    return Number(window.localStorage.getItem('telegram-workbench:selected-group') || 0);
  } catch {
    return 0;
  }
}

async function loadGroupsPanel() {
  const container = document.querySelector('[data-lazy-workbench="groups"]');
  if (!container) return false;
  const requestedChatId = getSelectedChatId() || persistedSelectedChatId();
  const params = new URLSearchParams();
  if (requestedChatId) params.set('selected_chat_id', String(requestedChatId));
  const suffix = params.toString() ? `?${params}` : '';
  const fragment = await fetchWorkbenchPartial(`/groups${suffix}`, '.kol-strategy-list');
  container.querySelector('.kol-strategy-list')?.remove();
  container.querySelector('.workbench-loading')?.remove();
  container.querySelector('.workbench-load-error')?.remove();
  container.appendChild(fragment);
  bindGroupLinks();
  bindGroupAutomationToggles();
  const requestedExists = requestedChatId
    && document.querySelector(`[data-group-link][data-chat-id="${requestedChatId}"]`);
  const selectedLink = requestedExists || document.querySelector('[data-group-link]');
  const selectedChatId = Number(selectedLink?.dataset.chatId || 0);
  if (selectedChatId) {
    syncSelectedGroupState(selectedChatId);
    return await loadSelectedGroupDestination('groups');
  }
  return true;
}

async function loadMorePanel() {
  const container = document.querySelector('[data-lazy-workbench="more"]');
  if (!container) return;
  const response = await fetch(`/more-panel?_t=${Date.now()}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`请求失败 (${response.status})`);
  const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
  const dashboard = document.querySelector('[data-trader-dashboard]');
  if (!dashboard) throw new Error('工作台容器不完整');
  doc.querySelectorAll('[data-dashboard-panel]:not([data-workbench-panel])').forEach((panel) => {
    const name = panel.dataset.dashboardPanel;
    if (!document.querySelector(`[data-dashboard-panel="${name}"]`)) dashboard.appendChild(panel);
  });
  bindDashboardTabs();
  bindTradingSettingsForm();
  bindAiRecognitionPromptForm();
  bindAiPromptCenter();
  bindAiModelSelectionForm();
  container.querySelector('.workbench-load-error')?.remove();
}

function managementBatchValue(value) {
  return value === null || value === undefined || value === '' ? '-' : String(value);
}

function renderManagementBatchCard(batch) {
  const card = document.createElement('article');
  card.className = `management-batch-card management-batch-${String(batch.status || 'unknown')}`;
  const heading = document.createElement('h3');
  heading.textContent = `Batch #${managementBatchValue(batch.batch_id)} · ${managementBatchValue(batch.status)}`;
  const identity = document.createElement('p');
  identity.textContent = `群 ${managementBatchValue(batch.source?.chat_title)} (${managementBatchValue(batch.source?.chat_id)}) / 消息 #${managementBatchValue(batch.source?.message_id)} / lifecycle ${managementBatchValue(batch.lifecycle_id)} / binding ${managementBatchValue(batch.execution_binding_id)}`;
  const strategy = document.createElement('p');
  strategy.textContent = `strategy ${managementBatchValue(batch.strategy_instance_id)} · ${managementBatchValue(batch.intent)} → ${managementBatchValue(batch.effective_action)} · round ${managementBatchValue(batch.partial_round_before)} · fraction ${managementBatchValue(batch.effective_fraction)}`;
  const safety = document.createElement('p');
  safety.className = 'management-batch-safety';
  safety.textContent = [batch.mode_label, batch.safety_label].filter(Boolean).join(' · ');
  const reason = document.createElement('p');
  reason.textContent = `原因: ${managementBatchValue(batch.reason)}`;
  const targets = document.createElement('p');
  targets.textContent = `目标: ${(batch.targets || []).map((target) => `${managementBatchValue(target.pos_id)}=${managementBatchValue(target.size)}`).join(', ') || '-'}`;
  const timestamps = document.createElement('p');
  timestamps.textContent = `计划 ${managementBatchValue(batch.planned_at)} · 更新 ${managementBatchValue(batch.updated_at)} · 完成 ${managementBatchValue(batch.completed_at)}`;
  const legs = document.createElement('ul');
  legs.className = 'management-batch-legs';
  (batch.legs || []).forEach((leg) => {
    const item = document.createElement('li');
    item.textContent = `leg ${managementBatchValue(leg.leg_index)} / pos ${managementBatchValue(leg.pos_id)} / ${managementBatchValue(leg.status)} / ${managementBatchValue(leg.preflight_size)} → ${managementBatchValue(leg.planned_close_size)} / clOrdId ${managementBatchValue(leg.client_order_id)} / ordId ${managementBatchValue(leg.exchange_order_id)} / protection ${JSON.stringify(leg.old_protection || [])} → ${JSON.stringify(leg.planned_protection || [])}`;
    legs.appendChild(item);
  });
  card.append(heading, identity, strategy, safety, reason, targets, timestamps, legs);
  return card;
}

async function loadManagementBatches() {
  const container = document.querySelector('[data-lazy-workbench="management-batches"]');
  if (!container) return;
  const chatId = getSelectedChatId();
  if (!chatId) {
    container.innerHTML = '<p class="empty-state">请先选择群组</p>';
    return;
  }
  const params = new URLSearchParams({ chat_id: String(chatId) });
  const response = await fetch(`/api/management-batches?${params}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`请求失败 (${response.status})`);
  const payload = await response.json();
  container.innerHTML = '';
  const list = document.createElement('div');
  list.className = 'management-batch-list';
  (payload.batches || []).forEach((batch) => list.appendChild(renderManagementBatchCard(batch)));
  if (!list.childElementCount) {
    const empty = document.createElement('p');
    empty.className = 'empty-state';
    empty.textContent = '暂无策略管理批次';
    list.appendChild(empty);
  }
  container.appendChild(list);
}

async function loadSelectedGroupDestination(view) {
  const chatId = getSelectedChatId();
  if (!chatId) return false;
  const requestId = ++groupSwitchRequestId;
  const controller = beginGroupSwitchRequest();
  const filterInput = document.querySelector('[data-strategy-filter-input]');
  const filter = filterInput ? filterInput.value : 'holding';
  const legacyView = view === 'activity' ? 'activity' : 'strategies';
  const detailPanel = getDetailPanelForWorkbenchView(view);
  const detailPromise = view === 'groups'
    ? fetchDetailPanel(chatId, { signal: controller.signal })
    : null;
  if (detailPromise) detailPromise.catch(() => {});
  let committed = false;
  try {
    committed = await loadVisibleGroupDestination({
      activeView: legacyView,
      chatId,
      filter,
      detailPanel,
      strategyPanel: document.querySelector('[data-strategy-panel]'),
      requestId,
      signal: controller.signal,
    });
  } catch (error) {
    controller.abort();
    throw error;
  }
  if (!committed) return false;
  if (detailPromise) {
    loadGroupDetailCompanion({
      chatId,
      detailPanel,
      requestId,
      detailPromise,
    }).catch((error) => handleGroupDetailCompanionError(error, requestId));
  }
  return true;
}

async function ensureWorkbenchViewLoaded(view, options = {}) {
  const state = workbenchLoadState[view];
  if (!state) return false;
  const key = workbenchLoadKey(view);
  if (!options.force && state.key === key) return true;
  if (state.promise) return state.promise;
  const container = document.querySelector(`[data-lazy-workbench="${view}"]`);
  if (container) container.setAttribute('aria-busy', 'true');
  state.promise = (async () => {
    let loaded = true;
    if (view === 'strategies') {
      loaded = await loadStrategyRecords();
    } else if (view === 'positions') {
      await loadPositionsPanel();
    } else if (view === 'management-batches') {
      await loadManagementBatches();
    } else if (view === 'groups') {
      loaded = await loadGroupsPanel();
    } else if (view === 'activity') {
      const groupsLoaded = await ensureWorkbenchViewLoaded('groups');
      if (!groupsLoaded || !getSelectedChatId()) {
        showActivityBootstrapError(new Error('群组加载失败或暂无可用群组'));
        if (container) container.setAttribute('aria-busy', 'false');
        return false;
      }
      loaded = await loadSelectedGroupDestination(view);
    } else if (view === 'more') {
      await loadMorePanel();
    }
    if (!loaded) {
      if (container) container.setAttribute('aria-busy', 'false');
      if (view === 'activity') {
        showActivityBootstrapError(new Error('动态记录暂时无法加载'));
      }
      return false;
    }
    state.key = workbenchLoadKey(view);
    if (container) container.setAttribute('aria-busy', 'false');
    return true;
  })();
  try {
    return await state.promise;
  } catch (error) {
    showWorkbenchLoadError(view, error);
    if (view === 'activity') showActivityBootstrapError(error);
    return false;
  } finally {
    state.promise = null;
  }
}

async function focusRequestedPosition() {
  const params = new URLSearchParams(window.location.search);
  const requestedView = params.get('view');
  const posId = params.get('pos_id');
  if (requestedView !== 'positions' || !posId) return;

  setWorkbenchView('positions');
  const positionsLoaded = await ensureWorkbenchViewLoaded('positions');
  if (!positionsLoaded) return false;
  const selector = `[data-position-pos-id="${CSS.escape(posId)}"]`;
  const card = document.querySelector(selector);
  if (!card) return;
  card.classList.add('strategy-record-position-target');
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  card.focus({ preventScroll: true });
  window.setTimeout(() => {
    card.classList.remove('strategy-record-position-target');
  }, 5000);
  return true;
}

function setActiveDashboardPanel(tab) {
  const buttons = document.querySelectorAll('[data-dashboard-tab]');
  const panels = document.querySelectorAll('[data-dashboard-panel]');
  buttons.forEach((button) => {
    button.classList.toggle('is-active', Boolean(tab) && button.dataset.dashboardTab === tab);
  });
  panels.forEach((panel) => {
    panel.classList.toggle('is-active', Boolean(tab) && panel.dataset.dashboardPanel === tab);
  });
  if (tab === 'prompt') {
    loadAiPromptCenter();
  }
}

const WORKBENCH_VIEWS = ['strategies', 'positions', 'activity', 'groups', 'more'];

function setWorkbenchView(requestedView) {
  const dashboard = document.querySelector('[data-trader-dashboard]');
  const buttons = document.querySelectorAll('[data-workbench-view]');
  const panels = document.querySelectorAll('[data-workbench-panel]');
  if (!dashboard || !buttons.length || !panels.length) return;
  const view = WORKBENCH_VIEWS.includes(requestedView) ? requestedView : 'strategies';
  if (view !== 'positions') cancelPositionSnapshotRefresh();
  dashboard.dataset.activeWorkbenchView = view;
  dashboard.classList.remove(...WORKBENCH_VIEWS.map((item) => `mobile-view-${item}`));
  dashboard.classList.add(`mobile-view-${view}`);
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
    const isActive = panelView === view;
    panel.classList.toggle('is-active', isActive);
  });
  const dashboardPanel = view === 'positions' ? 'exchange-positions' : null;
  setActiveDashboardPanel(dashboardPanel);
  ensureWorkbenchViewLoaded(view);
}

async function openDashboardPanel(tab) {
  const dashboard = document.querySelector('[data-trader-dashboard]');
  if (!dashboard) return false;
  if (tab === 'main') {
    setWorkbenchView(dashboard.getAttribute('data-return-workbench-view') || 'strategies');
    return true;
  }
  if (tab === 'exchange-positions') {
    setWorkbenchView('positions');
    return true;
  }
  const moreLoaded = await ensureWorkbenchViewLoaded('more');
  const targetPanel = document.querySelector(`[data-dashboard-panel="${tab}"]`);
  if (!moreLoaded || !targetPanel) {
    showDashboardPanelLoadError(tab, new Error(
      moreLoaded ? '返回内容缺少目标面板' : '更多工具暂时无法加载',
    ));
    return false;
  }
  clearDashboardPanelLoadError();
  const currentView = dashboard.dataset.activeWorkbenchView;
  if (WORKBENCH_VIEWS.includes(currentView)) {
    dashboard.setAttribute('data-return-workbench-view', currentView);
  } else if (!dashboard.hasAttribute('data-return-workbench-view')) {
    dashboard.setAttribute('data-return-workbench-view', 'strategies');
  }
  dashboard.dataset.activeWorkbenchView = 'settings';
  document.querySelectorAll('[data-workbench-panel]').forEach((panel) => {
    panel.classList.remove('is-active');
  });
  document.querySelectorAll('[data-workbench-view]').forEach((button) => {
    button.classList.remove('is-active');
    button.removeAttribute('aria-current');
  });
  setActiveDashboardPanel(tab);
  return true;
}

function bindDashboardTabs() {
  const buttons = document.querySelectorAll('[data-dashboard-tab]');
  buttons.forEach((button) => {
    if (button.dataset.dashboardTabBound === 'true') return;
    button.dataset.dashboardTabBound = 'true';
    button.addEventListener('click', () => {
      const tab = button.dataset.dashboardTab || 'main';
      const menu = button.closest('details');
      openDashboardPanel(tab).then((opened) => {
        if (opened && menu) menu.open = false;
      }).catch((error) => showDashboardPanelLoadError(tab, error));
    });
  });
}

function bindWorkbenchNavigation() {
  const dashboard = document.querySelector('[data-trader-dashboard]');
  const buttons = document.querySelectorAll('[data-workbench-view]');
  const panels = document.querySelectorAll('[data-workbench-panel]');
  if (dashboard && dashboard.dataset.managementGroupRefreshBound !== 'true') {
    dashboard.dataset.managementGroupRefreshBound = 'true';
    document.addEventListener('group-context-success', () => {
      if (dashboard.dataset.activeWorkbenchView === 'management-batches') {
        ensureWorkbenchViewLoaded('management-batches', { force: true });
      }
    });
  }
  if (!dashboard || !buttons.length || !panels.length) {
    return;
  }

  buttons.forEach((button) => {
    if (button.dataset.workbenchViewBound === 'true') return;
    button.dataset.workbenchViewBound = 'true';
    button.addEventListener('click', () => {
      setWorkbenchView(button.dataset.workbenchView || 'strategies');
    });
  });
  document.querySelectorAll('[data-legacy-workbench-view]').forEach((button) => {
    if (button.dataset.legacyWorkbenchViewBound === 'true') return;
    button.dataset.legacyWorkbenchViewBound = 'true';
    button.addEventListener('click', () => {
      const legacyView = button.dataset.legacyWorkbenchView;
      if (legacyView !== 'management-batches') return;
      dashboard.dataset.activeWorkbenchView = legacyView;
      panels.forEach((panel) => panel.classList.toggle('is-active', panel.dataset.workbenchPanel === legacyView));
      buttons.forEach((item) => {
        item.classList.remove('is-active');
        item.removeAttribute('aria-current');
      });
      setActiveDashboardPanel(null);
      ensureWorkbenchViewLoaded(legacyView);
    });
  });
}

function scheduleInitialWorkbenchView() {
  const params = new URLSearchParams(window.location.search);
  const requestedView = params.get('view');
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(async () => {
      setWorkbenchView(requestedView || 'strategies');
      if (requestedView === 'positions') {
        await ensureWorkbenchViewLoaded('positions');
        await focusRequestedPosition();
      }
    });
  });
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
  // Legacy entry point retained while callers migrate to the unified workbench.
  bindWorkbenchNavigation();
}

function bindExchangePositionTabs() {
  document.querySelectorAll('[data-exchange-position-tabs]').forEach((root) => {
    const tabs = root.querySelectorAll('[data-exchange-position-tab]');
    const viewButtons = root.querySelectorAll('[data-exchange-view-mode]');
    tabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        const target = tab.dataset.exchangePositionTab;
        setExchangePositionTab(root, target);
        saveExchangePositionTab(target);
        loadExchangePositionTab(root, target);
      });
    });
    viewButtons.forEach((button) => {
      button.addEventListener('click', () => {
        const mode = button.dataset.exchangeViewMode || 'list';
        setExchangePositionView(root, mode);
        saveExchangePositionView(mode);
      });
    });
    restoreExchangePositionTab(root);
    restoreExchangePositionView(root);
    const restoredTab = exchangePositionTab();
    if (restoredTab !== 'positions' && root.querySelector?.(
      `[data-exchange-position-panel="${restoredTab}"]`,
    )) {
      loadExchangePositionTab(root, restoredTab);
    }
  });
}

async function loadExchangePositionTab(root, tab) {
  if (!root || tab === 'positions' || !EXCHANGE_POSITION_TABS.includes(tab)) {
    return tab === 'positions';
  }
  if (typeof root.querySelector !== 'function') return false;
  const selector = `[data-exchange-position-panel="${tab}"]`;
  const panel = root.querySelector(selector);
  if (!panel) return false;
  if (panel.dataset.exchangeTabLoaded === 'true') return true;

  let requests = exchangePositionTabRequests.get(root);
  if (!requests) {
    requests = new Map();
    exchangePositionTabRequests.set(root, requests);
  }
  if (requests.has(tab)) return requests.get(tab);

  panel.setAttribute('aria-busy', 'true');
  const loading = panel.querySelector('[data-exchange-tab-loading]');
  if (loading) loading.textContent = '正在加载 Deepcoin 数据...';
  const request = (async () => {
    try {
      const fragment = await fetchWorkbenchPartial(
        `/positions-panel/tabs/${encodeURIComponent(tab)}`,
        selector,
      );
      const current = root.querySelector(selector);
      if (!current) return false;
      current.replaceWith(fragment);
      setExchangePositionTab(root, exchangePositionTab());
      setExchangePositionView(root, exchangePositionViewMode());
      bindBoundPositionCloseButtons();
      bindLivePositionAttributionButtons();
      return true;
    } catch (error) {
      const current = root.querySelector(selector);
      if (current) {
        current.dataset.exchangeTabLoaded = 'false';
        current.removeAttribute('aria-busy');
        const notice = document.createElement('p');
        notice.className = 'exchange-empty';
        notice.dataset.exchangeTabLoading = '';
        notice.setAttribute('role', 'alert');
        notice.textContent = `加载失败：${error.message || '请重新点击分页重试'}`;
        current.replaceChildren(notice);
      }
      return false;
    } finally {
      requests.delete(tab);
    }
  })();
  requests.set(tab, request);
  return request;
}

function exchangePositionTab() {
  try {
    const tab = window.localStorage.getItem(EXCHANGE_POSITION_TAB_KEY);
    return EXCHANGE_POSITION_TABS.includes(tab) ? tab : 'positions';
  } catch {
    return 'positions';
  }
}

function saveExchangePositionTab(tab) {
  if (!EXCHANGE_POSITION_TABS.includes(tab)) return;
  try {
    window.localStorage.setItem(EXCHANGE_POSITION_TAB_KEY, tab);
  } catch {
    // Keep the active in-memory DOM state when browser storage is unavailable.
  }
}

function setExchangePositionTab(root, tab) {
  const tabs = Array.from(root.querySelectorAll('[data-exchange-position-tab]'));
  const availableTabs = tabs.map((item) => item.dataset.exchangePositionTab);
  const selectedTab = EXCHANGE_POSITION_TABS.includes(tab) && availableTabs.includes(tab)
    ? tab
    : 'positions';
  tabs.forEach((item) => {
    const isActive = item.dataset.exchangePositionTab === selectedTab;
    item.classList.toggle('is-active', isActive);
    item.setAttribute('aria-selected', isActive ? 'true' : 'false');
  });
  root.querySelectorAll('[data-exchange-position-panel]').forEach((panel) => {
    panel.classList.toggle(
      'is-active',
      panel.dataset.exchangePositionPanel === selectedTab,
    );
  });
}

function exchangePositionUiState(root) {
  const activeTab = root?.querySelector('[data-exchange-position-tab].is-active');
  const activeView = root?.querySelector('[data-exchange-view-mode].is-active');
  return {
    tab: activeTab?.dataset.exchangePositionTab || exchangePositionTab(),
    view: activeView?.dataset.exchangeViewMode || exchangePositionViewMode(),
  };
}

function applyExchangePositionUiState(root, state) {
  if (!root || !state) return;
  setExchangePositionTab(root, state.tab);
  setExchangePositionView(root, state.view);
}

function restoreExchangePositionTab(root) {
  setExchangePositionTab(root, exchangePositionTab());
}

function exchangePositionViewMode() {
  try {
    return window.localStorage.getItem(EXCHANGE_POSITION_VIEW_KEY) === 'grouped'
      ? 'grouped'
      : 'list';
  } catch {
    return 'list';
  }
}

function saveExchangePositionView(mode) {
  try {
    window.localStorage.setItem(
      EXCHANGE_POSITION_VIEW_KEY,
      mode === 'grouped' ? 'grouped' : 'list',
    );
  } catch {
    // Keep the active in-memory DOM state when browser storage is unavailable.
  }
}

function setExchangePositionView(root, mode) {
  const selectedMode = mode === 'grouped' ? 'grouped' : 'list';
  root.querySelectorAll('[data-exchange-view-mode]').forEach((button) => {
    const isActive = button.dataset.exchangeViewMode === selectedMode;
    button.classList.toggle('is-active', isActive);
    button.setAttribute('aria-selected', isActive ? 'true' : 'false');
  });
  root.querySelectorAll('[data-exchange-view-panel]').forEach((panel) => {
    panel.classList.toggle('is-active', panel.dataset.exchangeViewPanel === selectedMode);
  });
}

function restoreExchangePositionView(root) {
  setExchangePositionView(root, exchangePositionViewMode());
}

function initTradingSymbolSelector(form) {
  const selector = form.querySelector('[data-symbol-selector]');
  if (!selector) {
    return;
  }
  const allowedInput = selector.querySelector('[data-allowed-symbols-input]');
  const riskInput = selector.querySelector('[data-symbol-risk-input]');
  const thresholdsInput = selector.querySelector('[data-symbol-entry-thresholds-input]');
  const searchInput = selector.querySelector('[data-symbol-search]');
  const summary = selector.querySelector('[data-symbol-selector-summary]');
  const selectedList = selector.querySelector('[data-selected-symbol-list]');
  const symbolList = selector.querySelector('[data-symbol-selector-list]');
  const riskList = selector.querySelector('[data-selected-symbol-risk-list]');
  const state = {
    symbols: [],
    selected: new Set(parseSymbolList(allowedInput?.value || '')),
    riskBySymbol: parseSymbolRiskMap(riskInput?.value || '{}'),
    thresholdsBySymbol: parseSymbolEntryThresholdMap(thresholdsInput?.value || '{}'),
    query: '',
  };

  const ensureSymbolEntryThresholds = (symbol) => {
    if (!Object.prototype.hasOwnProperty.call(state.thresholdsBySymbol, symbol)) {
      state.thresholdsBySymbol[symbol] = {
        market_leg_threshold: '0',
        first_limit_offset: '0',
        second_limit_offset: '0',
      };
    }
    return state.thresholdsBySymbol[symbol];
  };

  const syncInputs = () => {
    const selectedSymbols = Array.from(state.selected).sort();
    if (allowedInput) {
      allowedInput.value = selectedSymbols.join(',');
    }
    const riskPayload = {};
    Object.entries(state.riskBySymbol).forEach(([symbol, rawValue]) => {
      const value = Number(rawValue);
      if (Number.isFinite(value) && value > 0) {
        riskPayload[symbol] = value;
      }
    });
    if (riskInput) {
      riskInput.value = JSON.stringify(riskPayload);
    }
    if (thresholdsInput) {
      thresholdsInput.value = JSON.stringify(state.thresholdsBySymbol);
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
      const row = document.createElement('section');
      row.className = 'symbol-entry-settings-card';
      const title = document.createElement('strong');
      title.className = 'symbol-entry-settings-title';
      title.textContent = symbol;
      const grid = document.createElement('div');
      grid.className = 'symbol-entry-settings-grid';

      const riskLabel = document.createElement('label');
      const riskText = document.createElement('span');
      riskText.textContent = '最大亏损 USDT';
      const riskControl = document.createElement('input');
      riskControl.type = 'number';
      riskControl.min = '1';
      riskControl.step = '1';
      riskControl.placeholder = '默认';
      riskControl.value = state.riskBySymbol[symbol] || '';
      riskControl.addEventListener('input', () => {
        const value = Number(riskControl.value);
        if (Number.isFinite(value) && value > 0) {
          state.riskBySymbol[symbol] = value;
        } else {
          delete state.riskBySymbol[symbol];
        }
        syncInputs();
      });
      riskLabel.append(riskText, riskControl);
      grid.appendChild(riskLabel);

      const thresholds = ensureSymbolEntryThresholds(symbol);
      [
        ['market_leg_threshold', '第一腿市价固定阈值'],
        ['first_limit_offset', '第一腿限价固定价差'],
        ['second_limit_offset', '第二腿限价固定价差'],
      ].forEach(([field, text]) => {
        const label = document.createElement('label');
        const labelText = document.createElement('span');
        labelText.textContent = text;
        const input = document.createElement('input');
        input.type = 'number';
        input.min = '0';
        input.step = 'any';
        input.value = thresholds[field] || '0';
        input.dataset.thresholdField = field;
        input.addEventListener('input', () => {
          thresholds[field] = String(input.value || '').trim() || '0';
          syncInputs();
        });
        label.append(labelText, input);
        grid.appendChild(label);
      });
      row.append(title, grid);
      riskList.appendChild(row);
    });
    syncInputs();
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
          ensureSymbolEntryThresholds(item.symbol);
        } else {
          state.selected.delete(item.symbol);
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

  state.selected.forEach((symbol) => ensureSymbolEntryThresholds(symbol));
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

function isNonnegativeDecimalText(value) {
  if (typeof value === 'boolean' || value === null || typeof value === 'object') {
    return false;
  }
  const text = String(value).trim();
  return /^(?:\d+(?:\.\d*)?|\.\d+)$/.test(text);
}

function normalizeEntryThresholdValue(value, fieldName) {
  const text = String(value ?? '').trim();
  if (!text) {
    return '0';
  }
  if (!isNonnegativeDecimalText(value)) {
    throw new Error(`${fieldName} 必须是非负小数`);
  }
  return text;
}

function parseSymbolEntryThresholdMap(value) {
  let parsed;
  try {
    parsed = JSON.parse(value || '{}');
  } catch {
    throw new Error('币种固定阈值配置格式无效');
  }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error('币种固定阈值配置必须是对象');
  }
  return Object.fromEntries(
    Object.entries(parsed).map(([rawSymbol, rawThresholds]) => {
      const symbol = String(rawSymbol || '').trim().toUpperCase();
      if (!symbol || !rawThresholds || Array.isArray(rawThresholds)
          || typeof rawThresholds !== 'object') {
        throw new Error('币种固定阈值配置格式无效');
      }
      return [
        symbol,
        {
          market_leg_threshold: normalizeEntryThresholdValue(
            rawThresholds.market_leg_threshold,
            `${symbol}.market_leg_threshold`,
          ),
          first_limit_offset: normalizeEntryThresholdValue(
            rawThresholds.first_limit_offset,
            `${symbol}.first_limit_offset`,
          ),
          second_limit_offset: normalizeEntryThresholdValue(
            rawThresholds.second_limit_offset,
            `${symbol}.second_limit_offset`,
          ),
        },
      ];
    }),
  );
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
    const formData = new FormData(form);
    let symbolEntryThresholds;
    try {
      symbolEntryThresholds = parseSymbolEntryThresholdMap(
        formData.get('symbol_entry_thresholds') || '{}',
      );
    } catch (error) {
      if (status) {
        status.textContent = error.message || '币种固定阈值配置无效';
        status.classList.add('is-error');
      }
      return;
    }
    const submitButton = form.querySelector('button[type="submit"]');
    if (submitButton) {
      submitButton.disabled = true;
    }
    if (status) {
      status.textContent = '正在保存...';
      status.classList.remove('is-error');
    }
    const numericValue = (name, fallback) => {
      const parsed = Number(formData.get(name));
      return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
    };
    const payload = {
      auto_trade_enabled: Boolean(form.querySelector('[name="auto_trade_enabled"]')?.checked),
      telegram_source_deletion_exit_enabled: Boolean(form.querySelector('[name="telegram_source_deletion_exit_enabled"]')?.checked),
      context_resolution_enabled: Boolean(form.querySelector('[name="context_resolution_enabled"]')?.checked),
      context_resolution_live_chat_ids: String(formData.get('context_resolution_live_chat_ids') || '')
        .split(',')
        .map((value) => Number(value.trim()))
        .filter((value) => Number.isSafeInteger(value) && value !== 0),
      management_execution_mode: String(formData.get('management_execution_mode') || 'disabled'),
      composite_management_v2_mode: String(formData.get('composite_management_v2_mode') || 'disabled'),
      default_max_loss_usdt: numericValue('default_max_loss_usdt', 20),
      daily_max_loss_usdt: numericValue('daily_max_loss_usdt', 500),
      max_concurrent_positions: numericValue('max_concurrent_positions', 4),
      max_market_entry_deviation_pct: numericValue('max_market_entry_deviation_pct', 0.15),
      nearby_entry_market_deviation_pct: numericValue('nearby_entry_market_deviation_pct', 0.15),
      min_ai_confidence: Number(formData.get('min_ai_confidence') || 0.75),
      allowed_symbols: String(formData.get('allowed_symbols') || 'BTC,ETH'),
      symbol_max_loss_usdt: parseSymbolRiskMap(formData.get('symbol_max_loss_usdt') || '{}'),
      symbol_entry_thresholds: symbolEntryThresholds,
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

function promptApiPath(promptKey, action = '', chatId = null) {
  const suffix = action ? `/${action}` : '';
  const query = chatId ? `?chat_id=${encodeURIComponent(chatId)}` : '';
  return `/api/ai-prompts/${encodeURIComponent(promptKey)}${suffix}${query}`;
}

async function promptApiRequest(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
  return payload;
}

function setPromptCenterStatus(message, isError = false) {
  const status = document.querySelector('[data-ai-prompt-status]');
  if (!status) return;
  status.textContent = message || '';
  status.classList.toggle('is-error', isError);
}

function selectedPromptChatId(item = promptCenterState.selected) {
  return item?.scope_chat_id || null;
}

async function loadAiPromptCenter() {
  const root = document.querySelector('[data-ai-prompt-center]');
  if (!root) return;
  const requestId = ++promptCenterRequestId;
  const chatId = getSelectedChatId();
  const query = chatId ? `?chat_id=${encodeURIComponent(chatId)}` : '';
  try {
    const payload = await promptApiRequest(`/api/ai-prompts${query}`);
    if (requestId !== promptCenterRequestId) return;
    promptCenterState.items = payload.items || [];
    promptCenterState.chatId = chatId || null;
    renderPromptRegistryList();
    const previousKey = promptCenterState.selected?.prompt_key;
    const next = promptCenterState.items.find((item) => item.prompt_key === previousKey)
      || promptCenterState.items[0];
    if (next) selectPromptDefinition(next.prompt_key, next.scope_chat_id);
  } catch (error) {
    setPromptCenterStatus(error.message || '提示词列表加载失败', true);
  }
}

function renderPromptRegistryList() {
  const list = document.querySelector('[data-ai-prompt-list]');
  const select = document.querySelector('[data-ai-prompt-mobile-select]');
  if (!list || !select) return;
  list.replaceChildren();
  select.replaceChildren();
  promptCenterState.items.forEach((item) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'prompt-registry-item';
    button.dataset.promptKey = item.prompt_key;
    if (item.scope_chat_id) button.dataset.chatId = String(item.scope_chat_id);
    const title = document.createElement('strong');
    title.textContent = item.display_name;
    const meta = document.createElement('small');
    meta.textContent = `${item.category} · v${item.active_version.version_number}${item.draft_version ? ' · 有草稿' : ''}`;
    button.append(title, meta);
    button.addEventListener('click', () => selectPromptDefinition(item.prompt_key, item.scope_chat_id));
    list.append(button);

    const option = document.createElement('option');
    option.value = `${item.prompt_key}|${item.scope_chat_id || ''}`;
    option.textContent = item.display_name;
    select.append(option);
  });
  const hasScoped = promptCenterState.items.some((item) => item.prompt_key === 'research.chat.group');
  if (promptCenterState.chatId && !hasScoped) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'prompt-registry-item';
    button.dataset.promptKey = 'research.chat.group';
    button.dataset.chatId = String(promptCenterState.chatId);
    const title = document.createElement('strong');
    title.textContent = '群组专属研究提示词';
    const meta = document.createElement('small');
    meta.textContent = 'research · 尚未创建';
    button.append(title, meta);
    button.addEventListener('click', () => selectPromptDefinition('research.chat.group', promptCenterState.chatId));
    list.append(button);
    const option = document.createElement('option');
    option.value = `research.chat.group|${promptCenterState.chatId}`;
    option.textContent = '群组专属研究提示词（尚未创建）';
    select.append(option);
  }
  if (!select.dataset.bound) {
    select.dataset.bound = 'true';
    select.addEventListener('change', () => {
      const [key, scopedChatId] = select.value.split('|');
      selectPromptDefinition(key, scopedChatId ? Number(scopedChatId) : null);
    });
  }
}

async function selectPromptDefinition(promptKey, chatId = null) {
  const requestId = ++promptCenterRequestId;
  try {
    const detail = await promptApiRequest(promptApiPath(promptKey, '', chatId));
    if (requestId !== promptCenterRequestId) return;
    promptCenterState.selected = detail;
    promptCenterState.validated = Boolean(detail.draft_version?.validation_result?.success);
    promptCenterState.tested = false;
    renderPromptDetail();
  } catch (error) {
    if (promptKey === 'research.chat.group' && chatId) {
      renderLegacyGroupPromptImport(chatId);
      return;
    }
    setPromptCenterStatus(error.message, true);
  }
}

function renderPromptDetail() {
  const detail = document.querySelector('[data-ai-prompt-detail]');
  const item = promptCenterState.selected;
  if (!detail || !item) return;
  detail.hidden = false;
  detail.querySelector('[data-ai-prompt-title]').textContent = item.display_name;
  detail.querySelector('[data-ai-prompt-description]').textContent = item.description;
  detail.querySelector('[data-ai-prompt-category]').textContent = item.category;
  detail.querySelector('[data-ai-prompt-active-badge]').textContent = `生效 v${item.active_version.version_number}`;
  detail.querySelector('[data-ai-prompt-draft-badge]').textContent = item.draft_version
    ? `草稿 v${item.draft_version.version_number}` : '无草稿';
  const editor = detail.querySelector('[data-ai-prompt-draft]');
  editor.readOnly = false;
  editor.value = item.draft_version?.content || item.active_version.content;
  detail.querySelector('[data-ai-prompt-change-note]').value = item.draft_version?.change_note || '';
  detail.querySelector('[data-ai-prompt-publish]').disabled = !promptCenterState.validated;
  detail.querySelector('[data-ai-prompt-import-legacy]').hidden = true;
  const isTrading = item.category === 'trading';
  const isVision = item.prompt_key === 'trading.analysis.mimo_vision';
  detail.querySelector('[data-ai-prompt-test]').disabled = !isTrading;
  detail.querySelector('[data-ai-prompt-test-controls]').hidden = !isTrading;
  detail.querySelectorAll('[data-ai-prompt-test-model]').forEach((input) => {
    input.disabled = !isTrading || (isVision && input.value === 'deepseek');
    input.checked = isTrading && (input.value === 'mimo' || !isVision);
  });
  detail.querySelectorAll('[data-ai-prompt-view]').forEach((button) => {
    button.classList.toggle('is-active', button.dataset.aiPromptView === 'draft');
  });
  document.querySelectorAll('[data-ai-prompt-list] [data-prompt-key]').forEach((button) => {
    button.classList.toggle('is-active', button.dataset.promptKey === item.prompt_key
      && Number(button.dataset.chatId || 0) === Number(item.scope_chat_id || 0));
  });
  const select = document.querySelector('[data-ai-prompt-mobile-select]');
  if (select) select.value = `${item.prompt_key}|${item.scope_chat_id || ''}`;
  setPromptCenterStatus(promptCenterState.validated ? '草稿已通过校验' : '');
}

function renderLegacyGroupPromptImport(chatId) {
  const detail = document.querySelector('[data-ai-prompt-detail]');
  if (!detail) return;
  promptCenterState.selected = { prompt_key: 'research.chat.group', scope_chat_id: chatId };
  detail.hidden = false;
  detail.querySelector('[data-ai-prompt-title]').textContent = '群组专属研究提示词';
  const legacy = loadGroupPrompt(chatId);
  detail.querySelector('[data-ai-prompt-description]').textContent = legacy
    ? '检测到浏览器旧版群组提示词，可导入服务器草稿。'
    : '为当前群组创建专属研究提示词；保存后仍需校验和发布。';
  detail.querySelector('[data-ai-prompt-draft]').value = legacy;
  const action = detail.querySelector('[data-ai-prompt-import-legacy]');
  action.hidden = false;
  action.textContent = legacy ? '导入为草稿' : '创建群组草稿';
  detail.querySelector('[data-ai-prompt-test]').disabled = true;
  detail.querySelector('[data-ai-prompt-test-controls]').hidden = true;
  setPromptCenterStatus(legacy ? '旧版提示词不会自动发布' : '尚未创建');
}

function promptDiffSummary(item) {
  const active = item?.active_version?.content || '';
  const draft = item?.draft_version?.content || '';
  return `当前生效 v${item.active_version.version_number} (${active.length} 字)\n`
    + `待发布草稿 v${item.draft_version?.version_number || '?'} (${draft.length} 字)\n\n`
    + '发布后下一次 AI 调用将使用草稿内容。';
}

function bindAiPromptCenter() {
  const root = document.querySelector('[data-ai-prompt-center]');
  if (!root || root.dataset.bound) return;
  root.dataset.bound = 'true';
  const editor = root.querySelector('[data-ai-prompt-draft]');
  editor.addEventListener('input', () => {
    promptCenterState.validated = false;
    promptCenterState.tested = false;
    root.querySelector('[data-ai-prompt-publish]').disabled = true;
    setPromptCenterStatus('内容已修改，请重新保存并校验');
  });
  root.querySelectorAll('[data-ai-prompt-view]').forEach((button) => {
    button.addEventListener('click', () => {
      const activeView = button.dataset.aiPromptView === 'active';
      editor.value = activeView
        ? promptCenterState.selected.active_version.content
        : (promptCenterState.selected.draft_version?.content || promptCenterState.selected.active_version.content);
      editor.readOnly = activeView;
      root.querySelectorAll('[data-ai-prompt-view]').forEach((item) => item.classList.toggle('is-active', item === button));
    });
  });
  root.querySelector('[data-ai-prompt-save-draft]').addEventListener('click', async () => {
    const item = promptCenterState.selected;
    try {
      const detail = await promptApiRequest(promptApiPath(item.prompt_key, 'draft', selectedPromptChatId(item)), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: editor.value, change_note: root.querySelector('[data-ai-prompt-change-note]').value,
          expected_active_version_id: item.active_version?.id,
          expected_draft_updated_at: item.draft_version?.updated_at }),
      });
      promptCenterState.selected = detail;
      promptCenterState.validated = false;
      renderPromptDetail();
      setPromptCenterStatus('草稿已保存，未发布');
    } catch (error) { setPromptCenterStatus(error.message, true); }
  });
  root.querySelector('[data-ai-prompt-validate]').addEventListener('click', async () => {
    const item = promptCenterState.selected;
    try {
      const result = await promptApiRequest(promptApiPath(item.prompt_key, 'validate', selectedPromptChatId(item)), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_draft_version_id: item.draft_version?.id,
          expected_active_version_id: item.active_version?.id }),
      });
      promptCenterState.validated = result.success;
      root.querySelector('[data-ai-prompt-publish]').disabled = !result.success;
      setPromptCenterStatus(result.success ? '校验通过' : result.errors.join('；'), !result.success);
    } catch (error) { setPromptCenterStatus(error.message, true); }
  });
  root.querySelector('[data-ai-prompt-test]').addEventListener('click', async () => {
    const item = promptCenterState.selected;
    const rawIds = root.querySelector('[data-ai-prompt-test-message-ids]').value.split(',')
      .map((value) => Number(value.trim())).filter(Boolean);
    const modelKinds = Array.from(root.querySelectorAll('[data-ai-prompt-test-model]:checked')).map((input) => input.value);
    try {
      const result = await promptApiRequest(promptApiPath(item.prompt_key, 'test'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ draft_version_id: item.draft_version?.id, raw_message_ids: rawIds, model_kinds: modelKinds }),
      });
      promptCenterState.tested = result.items.some((row) => !row.error_message);
      const output = root.querySelector('[data-ai-prompt-comparison]');
      output.textContent = JSON.stringify(result.items, null, 2);
      output.hidden = false;
      setPromptCenterStatus(promptCenterState.tested ? '历史消息测试完成' : '历史消息测试失败', !promptCenterState.tested);
    } catch (error) { setPromptCenterStatus(error.message, true); }
  });
  root.querySelector('[data-ai-prompt-publish]').addEventListener('click', async () => {
    const item = promptCenterState.selected;
    if (!window.confirm(`${promptDiffSummary(item)}\n\n确认发布？`)) return;
    try {
      const detail = await promptApiRequest(promptApiPath(item.prompt_key, 'publish', selectedPromptChatId(item)), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_draft_version_id: item.draft_version?.id }),
      });
      promptCenterState.selected = detail;
      promptCenterState.validated = false;
      renderPromptDetail();
      setPromptCenterStatus('已发布，新版本已生效');
    } catch (error) { setPromptCenterStatus(error.message, true); }
  });
  root.querySelector('[data-ai-prompt-history]').addEventListener('click', () => renderPromptHistory());
  root.querySelector('[data-ai-prompt-rollback]').addEventListener('click', () => {
    renderPromptHistory();
    setPromptCenterStatus('请在历史版本中选择要回滚的已发布版本');
  });
  root.querySelector('[data-ai-prompt-import-legacy]').addEventListener('click', async () => {
    const item = promptCenterState.selected;
    try {
      await promptApiRequest(promptApiPath(item.prompt_key, 'draft', item.scope_chat_id), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: editor.value,
          change_note: loadGroupPrompt(item.scope_chat_id)
            ? '从旧版浏览器群组提示词导入' : '创建群组专属研究提示词' }),
      });
      setPromptCenterStatus('已导入为草稿，未发布');
      loadAiPromptCenter();
    } catch (error) { setPromptCenterStatus(error.message, true); }
  });
  loadAiPromptCenter();
}

function renderPromptHistory() {
  const root = document.querySelector('[data-ai-prompt-center]');
  const container = root.querySelector('[data-ai-prompt-history-list]');
  const item = promptCenterState.selected;
  container.replaceChildren();
  (item.history || []).forEach((version) => {
    const row = document.createElement('div');
    const label = document.createElement('span');
    label.textContent = `v${version.version_number} · ${version.status} · ${version.change_note || '无说明'}`;
    const rollback = document.createElement('button');
    rollback.type = 'button';
    rollback.className = 'secondary-button';
    rollback.textContent = '回滚到此版本';
    rollback.disabled = !['published', 'superseded'].includes(version.status) || version.id === item.active_version.id;
    rollback.addEventListener('click', async () => {
      if (!window.confirm(`确认以 v${version.version_number} 创建新的生效版本？\n提示词回滚不会回滚应用代码。`)) return;
      try {
        const detail = await promptApiRequest(promptApiPath(item.prompt_key, 'rollback', selectedPromptChatId(item)), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source_version_id: version.id, expected_active_version_id: item.active_version.id,
            change_note: `Web 回滚到 v${version.version_number}` }),
        });
        promptCenterState.selected = detail;
        renderPromptDetail();
        renderPromptHistory();
      } catch (error) { setPromptCenterStatus(error.message, true); }
    });
    row.append(label, rollback);
    container.append(row);
  });
  container.hidden = false;
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
  const aiModels = collectAiModelConfigs();
  const activeTextModelId = value('[data-active-text-model-id]');
  const activeImageModelId = value('[data-active-image-model-id]');
  const activeTextModel = aiModels.find((model) => model.id === activeTextModelId) || null;
  const activeImageModel = aiModels.find((model) => model.id === activeImageModelId) || null;
  return {
    mode: 'ai_provider',
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

  if (globalChanged || selectedChanged) {
    noteStrategyRecordChanges();
  }
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
      updateStrategyRecordChangesBadge();
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
      noteStrategyRecordChanges();
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
        updateStrategyRecordChangesBadge();
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
        sseWasDisconnected = false;
        setAiStatus('实时连接已恢复，有新变化时可手动查看。');
        setMonitorStatus({
          state: 'monitoring',
          label: '监控中',
          detail: '实时事件连接已恢复',
        });
        noteStrategyRecordChanges();
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
    if (activeView === 'positions') {
      await checkPositionsPanelForChanges();
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
  const initialPositionsPanel = document.querySelector(
    '[data-lazy-workbench="positions"] [data-exchange-position-tabs]',
  );
  if (initialPositionsPanel) {
    markWorkbenchLoaded('positions');
    schedulePositionSnapshotRefresh(initialPositionsPanel);
  }
  scheduleInitialWorkbenchView();
  bindHomeEventFilters();
  bindGroupContext();
  bindWorkflowFilters();
  bindStrategyRecordController();
  if (document.querySelector('[data-strategy-record-list]')) {
    restoreStrategyRecordScrollPosition();
  }
  bindExchangePositionTabs();
  bindTradingSettingsForm();
  bindStrategyFilterBadges();
  bindAiRecognitionPromptForm();
  bindAiPromptCenter();
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
