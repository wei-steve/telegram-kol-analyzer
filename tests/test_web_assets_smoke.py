import shutil
import subprocess
import textwrap

import pytest
from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app


def test_static_assets_are_served(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.css")

    assert response.status_code == 200


def test_trading_settings_js_submits_adjacent_entry_modes(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    source = client.get("/static/app.js").text

    assert "entry_message_assembly_v2_mode: String(formData.get('entry_message_assembly_v2_mode')" in source
    assert "entry_revision_v2_mode: String(formData.get('entry_revision_v2_mode')" in source


def test_trading_symbol_assets_preserve_decimal_threshold_state(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    js = client.get("/static/app.js").text
    css = client.get("/static/app.css").text
    selector_start = js.index("function initTradingSymbolSelector")
    selector_end = js.index("\nfunction parseSymbolList", selector_start)
    selector_block = js[selector_start:selector_end]
    fetch_start = selector_block.index("fetch('/api/trading-settings/symbols')")
    fetch_block = selector_block[fetch_start:]

    assert "thresholdsBySymbol" in selector_block
    assert "data-symbol-entry-thresholds-input" in selector_block
    assert "ensureSymbolEntryThresholds" in selector_block
    assert "market_leg_threshold" in selector_block
    assert "first_limit_offset" in selector_block
    assert "second_limit_offset" in selector_block
    assert "第一腿市价固定阈值" in selector_block
    assert "第一腿限价固定价差" in selector_block
    assert "第二腿限价固定价差" in selector_block
    assert "input.min = '0';" in selector_block
    assert "input.step = 'any';" in selector_block
    assert "delete state.thresholdsBySymbol" not in selector_block
    assert selector_block.count("delete state.riskBySymbol") == 1
    assert "Object.entries(state.riskBySymbol).forEach" in selector_block
    assert "item.entry_thresholds" not in fetch_block
    assert "if (item.selected)" not in fetch_block
    assert "item.max_loss_usdt" not in fetch_block
    assert ".value = JSON.stringify(state.thresholdsBySymbol)" in selector_block
    assert "symbolEntryThresholds = parseSymbolEntryThresholdMap" in js
    assert "symbol_entry_thresholds: symbolEntryThresholds" in js
    assert "max_market_entry_deviation_pct: numericValue" in js
    assert "entry_range_order_style: String" in js
    assert ".symbol-entry-settings-grid" in css


def test_symbol_threshold_parser_preserves_small_decimal_strings(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for the threshold parser behavior test")
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    parser_start = js.index("function isNonnegativeDecimalText")
    parser_end = js.index("\nfunction bindTradingSettingsForm", parser_start)
    parser_source = js[parser_start:parser_end]
    harness = textwrap.dedent(
        f"""
        {parser_source}
        const parsed = parseSymbolEntryThresholdMap(JSON.stringify({{
          pepe: {{
            market_leg_threshold: '0.000003',
            first_limit_offset: '0.000001',
            second_limit_offset: '',
          }},
        }}));
        if (parsed.PEPE.market_leg_threshold !== '0.000003') process.exit(1);
        if (parsed.PEPE.first_limit_offset !== '0.000001') process.exit(2);
        if (parsed.PEPE.second_limit_offset !== '0') process.exit(3);
        try {{
          parseSymbolEntryThresholdMap(JSON.stringify({{
            BTC: {{
              market_leg_threshold: '-1',
              first_limit_offset: '0',
              second_limit_offset: '0',
            }},
          }}));
          process.exit(4);
        }} catch (error) {{
          if (!String(error.message).includes('market_leg_threshold')) process.exit(5);
        }}
        """
    )

    subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)


def test_deepcoin_history_assets_are_scoped_to_the_history_panel(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    css = client.get("/static/app.css").text
    history_css = css[
        css.index("/* DeepCoin historical-position treatment"):
        css.index(".exchange-position-actions")
    ]

    assert ".exchange-tab.is-active::after" in css
    assert ".exchange-view-toolbar" in css
    assert ".exchange-tab-refresh-controls" in css
    assert ".exchange-tab-refresh-status" in css
    assert "background: #3b82f6;" in css
    assert "background: #f97316;" in css
    assert "[data-exchange-history-panel]" in css
    assert ".exchange-tab-strip:has(~ .exchange-tab-panels [data-exchange-history-panel].is-active)" in css
    assert ".deepcoin-history-position" in css
    assert "background: var(--surface-panel);" in history_css
    assert "color: var(--color-text);" in history_css
    assert "#ffffff" not in history_css
    assert ".deepcoin-history-times dd" in css
    assert ".deepcoin-history-metric-missing" in css
    assert ".deepcoin-history-pnl-negative" in css
    assert "[data-exchange-history-panel].is-active .exchange-group-header" in css
    assert ".exchange-position-card" in css


def test_management_batch_assets_only_load_read_only_api(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    js = client.get("/static/app.js").text
    css = client.get("/static/app.css").text
    assert "loadManagementBatches" in js
    assert "/api/management-batches?" in js
    management_slice = js[js.find("loadManagementBatches"):js.find("loadManagementBatches") + 2200]
    assert "chat_id" in management_slice
    assert "getSelectedChatId" in management_slice
    assert "view === 'management-batches'" in js
    assert "group-context-success" in js
    assert "ensureWorkbenchViewLoaded('management-batches', { force: true })" in js
    assert "view === 'activity' || view === 'groups' || view === 'management-batches'" in js
    assert "method: 'POST'" not in management_slice
    assert "management-batch-card" in css
    for forbidden in ("retryManagementBatch", "closeManagementBatch", "cancelManagementBatch"):
        assert forbidden not in js


def test_app_js_includes_conversation_history_migration_for_legacy_image_errors(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "migrateConversationHistory" in response.text
    assert "saveConversationHistory(migratedHistory);" in response.text
    assert "normalizeAiAnswerText(entry.answer || '')" in response.text
    assert "sources: isImageInputErrorText(normalizedAnswer) ? [] : (entry.sources || [])" in response.text


def test_app_js_includes_ai_history_timestamps_for_saved_and_rendered_turns(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "renderHistoryTimestamp" in response.text
    assert "createdAt: new Date().toISOString()" in response.text
    assert "${renderHistoryTimestamp(entry.createdAt)}" in response.text


def test_app_js_collects_separate_ai_model_configs_and_active_selection(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "bindAiModelSelectionForm" in response.text
    assert "collectAiModelConfigs" in response.text
    assert "active_text_model_id" in response.text
    assert "active_image_model_id" in response.text
    assert "data-ai-model-api-key" in response.text
    assert "modelConfigToProvider(activeTextModel)" in response.text


def test_app_js_refreshes_group_list_after_live_or_manual_updates(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "refreshGroupList" in response.text
    assert "/groups?selected_chat_id" in response.text
    assert "await refreshGroupList();" in response.text


def test_app_js_polls_for_updates_even_when_sse_stays_quiet(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "startPollingUpdates" in response.text
    assert "window.setInterval" in response.text
    assert "connectLiveUpdates();" in response.text
    assert "startPollingUpdates();" in response.text


def test_focus_recovery_checks_positions_without_replacing_visible_panel(tmp_path):
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text

    assert "async function checkPositionsPanelForChanges(" in js

    recovery_start = js.index("function scheduleRecoveryRefresh")
    recovery_end = js.index("\nfunction ", recovery_start + 1)
    recovery_block = js[recovery_start:recovery_end]
    check_start = js.index("async function checkPositionsPanelForChanges")
    check_end = js.index("\nfunction ", check_start + 1)
    check_block = js[check_start:check_end]

    assert "await refreshMonitorStatus();" in recovery_block
    assert "await refreshFromDatabaseChanges();" in recovery_block
    assert "await checkPositionsPanelForChanges();" in recovery_block
    assert "ensureWorkbenchViewLoaded(activeView, { force: true })" not in recovery_block
    assert "pendingPositionsFragment = fragment;" in check_block
    assert "showPendingPositionsRefreshNotice();" in check_block
    assert "container.innerHTML" not in check_block
    assert "setAttribute('aria-busy'" not in check_block


def test_silent_positions_check_discards_snapshot_after_visible_panel_changes(tmp_path):
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    check_start = js.index("async function checkPositionsPanelForChanges")
    check_end = js.index("\nfunction ", check_start + 1)
    check_block = js[check_start:check_end]

    stale_guard = (
        "if (current !== container.querySelector('[data-exchange-position-tabs]')) "
        "return false;"
    )
    assert stale_guard in check_block
    assert check_block.index(stale_guard) < check_block.index(
        "pendingPositionsFragment = fragment;"
    )


def test_position_snapshot_assets_use_bounded_automatic_refresh(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    js = client.get("/static/app.js").text
    css = client.get("/static/app.css").text

    assert "function schedulePositionSnapshotRefresh" in js
    assert "function cancelPositionSnapshotRefresh" in js
    assert "const POSITION_SNAPSHOT_RETRY_DELAYS = [1000, 2000, 4000];" in js
    assert "applySnapshotRefresh: true" in js
    assert "dataset.positionSnapshotState" in js
    assert "cancelPositionSnapshotRefresh();" in js
    assert ".position-snapshot-status" in css
    assert ".position-snapshot-status--stale" in css
    assert ".position-snapshot-status--error" in css

    if shutil.which("node") is None:
        pytest.skip("Node.js is required for the bounded retry behavior test")
    refresh_start = js.index("function cancelPositionSnapshotRefresh")
    refresh_end = js.index("\nasync function loadPositionsPanel", refresh_start)
    refresh_source = js[refresh_start:refresh_end]
    harness = textwrap.dedent(
        f"""
        const POSITION_SNAPSHOT_RETRY_DELAYS = [1000, 2000, 4000];
        let positionSnapshotRetryTimer = null;
        let positionSnapshotRetryToken = 0;
        let positionSnapshotRetryAttempt = 0;
        const root = {{ dataset: {{ positionSnapshotState: 'error' }} }};
        const dashboard = {{ dataset: {{ activeWorkbenchView: 'positions' }} }};
        const window = {{
          setTimeout: (fn) => setTimeout(fn, 0),
          clearTimeout: (timer) => clearTimeout(timer),
        }};
        const document = {{
          querySelector: (selector) => selector.includes('trader-dashboard') ? dashboard : root,
        }};
        let calls = 0;
        async function checkPositionsPanelForChanges() {{
          calls += 1;
          root.dataset.positionSnapshotState = calls % 2 ? 'refreshing' : 'error';
          schedulePositionSnapshotRefresh(root, {{ preserveRetryBudget: true }});
          return true;
        }}
        {refresh_source}
        schedulePositionSnapshotRefresh(root);
        setTimeout(() => {{
          if (calls !== 3) process.exit(1);
          process.exit(0);
        }}, 100);
        """
    )
    subprocess.run(
        ["node", "-e", harness],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )


def test_positions_refresh_normalizes_and_restores_in_memory_ui_state(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for the positions UI-state behavior test")

    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    assert "function exchangePositionUiState(root)" in js
    assert "function applyExchangePositionUiState(root, state)" in js

    commit_start = js.index("function commitPositionsPanel")
    commit_end = js.index("\nfunction ", commit_start + 1)
    commit_block = js[commit_start:commit_end]
    check_start = js.index("async function checkPositionsPanelForChanges")
    check_end = js.index("\nasync function loadPositionsPanel", check_start + 1)
    check_block = js[check_start:check_end]

    assert "const uiState = exchangePositionUiState(current);" in commit_block
    assert commit_block.index("const uiState = exchangePositionUiState(current);") < (
        commit_block.index("container.replaceChildren(fragment);")
    )
    assert commit_block.index("bindExchangePositionTabs();") < commit_block.index(
        "applyExchangePositionUiState(fragment, uiState);"
    )
    assert "const uiState = exchangePositionUiState(current);" in check_block
    assert "applyExchangePositionUiState(fragment, uiState);" in check_block
    assert check_block.index("applyExchangePositionUiState(fragment, uiState);") < (
        check_block.index("positionsPanelComparableMarkup(current)")
    )

    functions_start = js.index("function setExchangePositionTab")
    functions_end = js.index("\nfunction restoreExchangePositionTab", functions_start)
    view_start = js.index("function setExchangePositionView")
    view_end = js.index("\nfunction restoreExchangePositionView", view_start)
    harness = textwrap.dedent(
        """
        global.window = {
          localStorage: {
            getItem: () => { throw new Error('storage unavailable'); },
            setItem: () => { throw new Error('storage unavailable'); },
          },
        };
        function syncExchangeTabRefreshControls() {}
        const EXCHANGE_POSITION_TABS = [
          'positions', 'open-orders', 'order-history', 'position-history',
        ];

        class FakeClassList {
          constructor(active = false) { this.values = new Set(active ? ['is-active'] : []); }
          toggle(name, enabled) {
            if (enabled) this.values.add(name);
            else this.values.delete(name);
          }
          contains(name) { return this.values.has(name); }
        }

        class FakeElement {
          constructor(dataset, active = false) {
            this.dataset = dataset;
            this.classList = new FakeClassList(active);
            this.attributes = {};
          }
          setAttribute(name, value) { this.attributes[name] = value; }
        }

        class FakeRoot {
          constructor() {
            const tabs = ['positions', 'open-orders', 'order-history', 'position-history'];
            this.tabs = tabs.map((name, index) => new FakeElement(
              { exchangePositionTab: name }, index === 0,
            ));
            this.tabPanels = tabs.map((name, index) => new FakeElement(
              { exchangePositionPanel: name }, index === 0,
            ));
            const views = ['list', 'grouped'];
            this.viewButtons = views.map((name, index) => new FakeElement(
              { exchangeViewMode: name }, index === 0,
            ));
            this.viewPanels = views.map((name, index) => new FakeElement(
              { exchangeViewPanel: name }, index === 0,
            ));
          }
          querySelectorAll(selector) {
            if (selector === '[data-exchange-position-tab]') return this.tabs;
            if (selector === '[data-exchange-position-panel]') return this.tabPanels;
            if (selector === '[data-exchange-view-mode]') return this.viewButtons;
            if (selector === '[data-exchange-view-panel]') return this.viewPanels;
            return [];
          }
          querySelector(selector) {
            const items = this.querySelectorAll(selector.replace('.is-active', ''));
            return items.find((item) => item.classList.contains('is-active')) || null;
          }
          get outerHTML() {
            const state = exchangePositionUiState(this);
            return `${state.tab}:${state.view}`;
          }
        }

        const current = new FakeRoot();
        setExchangePositionTab(current, 'order-history');
        setExchangePositionView(current, 'grouped');
        const fragment = new FakeRoot();
        if (current.outerHTML === fragment.outerHTML) {
          throw new Error('test setup should begin with different UI state');
        }

        const state = exchangePositionUiState(current);
        applyExchangePositionUiState(fragment, state);
        if (fragment.outerHTML !== 'order-history:grouped') {
          throw new Error(`UI state was not restored: ${fragment.outerHTML}`);
        }
        if (current.outerHTML !== fragment.outerHTML) {
          throw new Error('equivalent data should compare equally after UI normalization');
        }
        """
    )
    result = subprocess.run(
        [
            "node",
            "-e",
            "\n".join(
                [
                    harness,
                    js[functions_start:functions_end],
                    js[view_start:view_end],
                ]
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_positions_change_comparison_ignores_browsing_only_dom_state(tmp_path):
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    comparable_start = js.index("function positionsPanelComparableMarkup")
    comparable_end = js.index("\nfunction ", comparable_start + 1)
    comparable_block = js[comparable_start:comparable_end]
    check_start = js.index("async function checkPositionsPanelForChanges")
    check_end = js.index("\nasync function loadPositionsPanel", check_start + 1)
    check_block = js[check_start:check_end]

    assert "root.cloneNode(true)" in comparable_block
    assert "setExchangePositionTab(clone, 'positions');" in comparable_block
    assert "setExchangePositionView(clone, 'list');" in comparable_block
    assert "clone.querySelectorAll('details[open]')" in comparable_block
    assert "details.removeAttribute('open')" in comparable_block
    assert (
        "clone.querySelectorAll('[data-exchange-position-panel]:not("
        "[data-exchange-position-panel=\"positions\"])')"
    ) in comparable_block
    assert "panel.remove()" in comparable_block
    assert "current.outerHTML === fragment.outerHTML" not in check_block
    assert check_block.index("positionsPanelComparableMarkup(current)") < (
        check_block.index("positionsPanelComparableMarkup(fragment)")
    )
    assert check_block.index("positionsPanelComparableMarkup(fragment)") < (
        check_block.index("pendingPositionsFragment = fragment;")
    )


def test_app_js_restores_exchange_position_view_after_partial_reload(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    js = client.get("/static/app.js").text

    assert "const EXCHANGE_POSITION_VIEW_KEY" in js
    assert "function restoreExchangePositionView(root)" in js
    assert "restoreExchangePositionView(root);" in js
    assert "saveExchangePositionView(mode);" in js


def test_app_js_restores_exchange_position_tab_after_partial_reload(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    js = client.get("/static/app.js").text
    bind_start = js.index("function bindExchangePositionTabs")
    bind_end = js.index("\nfunction ", bind_start + 1)
    bind_block = js[bind_start:bind_end]
    load_start = js.index("async function loadPositionsPanel")
    load_end = js.index("\nfunction ", load_start + 1)
    load_block = js[load_start:load_end]
    commit_start = js.index("function commitPositionsPanel")
    commit_end = js.index("\nfunction ", commit_start + 1)
    commit_block = js[commit_start:commit_end]

    assert "const EXCHANGE_POSITION_TAB_KEY" in js
    assert "const EXCHANGE_POSITION_TABS = [" in js
    for tab in ("positions", "open-orders", "order-history", "position-history"):
        assert f"'{tab}'" in js
    assert "function exchangePositionTab()" in js
    assert "function saveExchangePositionTab(tab)" in js
    assert "function setExchangePositionTab(root, tab)" in js
    assert "function restoreExchangePositionTab(root)" in js
    assert "saveExchangePositionTab(target);" in bind_block
    assert "restoreExchangePositionTab(root);" in bind_block
    assert "return 'positions';" in js
    assert "commitPositionsPanel(fragment);" in load_block
    assert commit_block.index("container.replaceChildren(fragment);") < commit_block.index(
        "bindExchangePositionTabs();"
    )


def test_app_js_lazy_loads_exchange_position_tabs(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    js = client.get("/static/app.js").text

    assert "function loadExchangePositionTab(root, tab, { force = false } = {})" in js
    assert "/positions-panel/tabs/${encodeURIComponent(tab)}" in js
    assert "panel.dataset.exchangeTabLoaded === 'true'" in js
    assert "loadExchangePositionTab(root, target)" in js
    assert "/positions-panel?initial=positions" in js


def test_app_js_loads_more_history_positions_without_a_second_exchange_snapshot(tmp_path):
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text

    assert "function loadMoreHistoryPositions(root)" in js
    assert "browse_token" in js
    assert "data-history-load-more" in js


def test_app_js_applies_and_clears_history_position_date_filters(tmp_path):
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text

    assert "function reloadHistoryPositionsWithFilter(root)" in js
    assert "data-history-filter-apply" in js
    assert "data-history-filter-clear" in js


def test_app_js_merges_history_continuation_into_grouped_view(tmp_path):
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text

    assert "function appendHistoryGroups" in js
    assert "dataset.exchangeGroupName" in js


def test_workbench_partial_asset_version_mismatch_reloads_once_without_returning_fragment(
    tmp_path,
):
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for the asset-version behavior test")
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    assert "class WorkbenchAssetVersionMismatchError" in js
    functions_start = js.index("class WorkbenchAssetVersionMismatchError")
    functions_end = js.index("\nfunction strategyRecordStorageGet", functions_start)
    harness = textwrap.dedent(
        """
        const storage = new Map();
        let reloadCalls = 0;
        global.window = {
          sessionStorage: {
            getItem: (key) => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, value),
          },
          location: { reload: () => { reloadCalls += 1; } },
        };
        global.document = {
          documentElement: { dataset: { workbenchAssetVersion: '100' } },
          querySelector: () => null,
        };
        global.fetch = async () => ({
          ok: true,
          status: 200,
          headers: { get: (name) => name === 'X-Workbench-Asset-Version' ? '101' : null },
          text: async () => '<section data-target></section>',
        });
        global.DOMParser = class {
          parseFromString() { return { querySelector: () => ({ dataset: {} }) }; }
        };

        for (let attempt = 0; attempt < 2; attempt += 1) {
          try {
            await fetchWorkbenchPartial('/positions-panel', '[data-target]');
            throw new Error('mismatched fragment was returned');
          } catch (error) {
            if (!(error instanceof WorkbenchAssetVersionMismatchError)) throw error;
          }
        }
        if (reloadCalls !== 1) throw new Error(`expected one reload, got ${reloadCalls}`);
        """
    )
    result = subprocess.run(
        ["node", "-e", "\n".join((js[functions_start:functions_end], harness))],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_exchange_position_tab_manual_refresh_is_single_flight_and_preserves_data(
    tmp_path,
):
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for the browser-state behavior test")

    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    constants_start = js.index("const exchangePositionTabRequests")
    constants_end = js.index("\nlet strategyRecordRequestId", constants_start)
    functions_start = js.index("function exchangePositionTabLabel")
    functions_end = js.index("\nfunction boundedContractSpecStatusText", functions_start)
    harness = textwrap.dedent(
        """
        const storage = new Map([
          [EXCHANGE_POSITION_TAB_KEY, 'open-orders'],
          [EXCHANGE_POSITION_VIEW_KEY, 'list'],
        ]);
        global.window = {
          localStorage: {
            getItem: (key) => storage.has(key) ? storage.get(key) : null,
            setItem: (key, value) => storage.set(key, value),
          },
        };

        class FakeClassList {
          constructor(initial = []) { this.values = new Set(initial); }
          toggle(name, enabled) {
            if (enabled) this.values.add(name);
            else this.values.delete(name);
          }
          contains(name) { return this.values.has(name); }
        }

        class FakeElement {
          constructor(dataset = {}, active = false) {
            this.dataset = { ...dataset };
            this.classList = new FakeClassList(active ? ['is-active'] : []);
            this.attributes = {};
            this.listeners = {};
            this.textContent = '';
            this.disabled = false;
            this.hidden = false;
          }
          addEventListener(name, listener) { this.listeners[name] = listener; }
          click() { this.listeners.click?.(); }
          setAttribute(name, value) { this.attributes[name] = String(value); }
          removeAttribute(name) { delete this.attributes[name]; }
        }

        class FakePanel extends FakeElement {
          constructor(root, tab, loaded = true, count = '2', capturedAt = '') {
            super({
              exchangePositionPanel: tab,
              exchangeTabLoaded: loaded ? 'true' : 'false',
              exchangeTabItemCount: count,
              ...(capturedAt ? { exchangeTabCapturedAt: capturedAt } : {}),
            }, tab === 'open-orders');
            this.root = root;
            this.tab = tab;
            this.loading = new FakeElement();
            this.retryButton = null;
          }
          querySelector(selector) {
            if (selector === '[data-exchange-tab-loading]') return this.loading;
            return null;
          }
          querySelectorAll(selector) {
            if (selector === '[data-exchange-tab-retry]' && this.retryButton) {
              return [this.retryButton];
            }
            return [];
          }
          replaceWith(fragment) {
            fragment.root = this.root;
            fragment.tab = this.tab;
            this.root.panels.set(this.tab, fragment);
            this.root.currentPanel = fragment;
          }
          replaceChildren(child) {
            this.children = [child];
          }
        }

        class FakeRoot {
          constructor() {
            this.dataset = {};
            this.tabs = ['positions', 'open-orders', 'order-history', 'position-history']
              .map((tab) => {
                const item = new FakeElement({
                  exchangePositionTab: tab,
                  exchangePositionLabel: ({
                    positions: '持仓',
                    'open-orders': '当前委托',
                    'order-history': '历史委托',
                    'position-history': '历史仓位',
                  })[tab],
                }, tab === 'open-orders');
                item.textContent = item.dataset.exchangePositionLabel;
                return item;
              });
            this.panels = new Map([
              ['positions', new FakePanel(this, 'positions')],
              ['open-orders', new FakePanel(this, 'open-orders')],
              ['order-history', new FakePanel(this, 'order-history')],
              ['position-history', new FakePanel(this, 'position-history')],
            ]);
            this.currentPanel = this.panels.get('open-orders');
            this.refreshControls = new FakeElement();
            this.refreshButton = new FakeElement();
            this.refreshStatus = new FakeElement();
            this.viewButtons = [new FakeElement({ exchangeViewMode: 'list' }, true)];
          }
          querySelectorAll(selector) {
            if (selector === '[data-exchange-position-tab]') return this.tabs;
            if (selector === '[data-exchange-position-panel]') return [...this.panels.values()];
            if (selector === '[data-exchange-view-mode]') return this.viewButtons;
            if (selector === '[data-exchange-view-panel]') return [];
            if (selector === '[data-exchange-tab-retry]') {
              return [...this.panels.values()]
                .flatMap((panel) => panel.retryButton ? [panel.retryButton] : []);
            }
            return [];
          }
          querySelector(selector) {
            const panel = selector.match(/^\\[data-exchange-position-panel="(.+)"\\]$/);
            if (panel) return this.panels.get(panel[1]) || null;
            const tab = selector.match(/^\\[data-exchange-position-tab="(.+)"\\]$/);
            if (tab) return this.tabs.find((item) => item.dataset.exchangePositionTab === tab[1]) || null;
            if (selector === '[data-exchange-position-tab].is-active') {
              return this.tabs.find((item) => item.classList.contains('is-active')) || null;
            }
            if (selector === '[data-exchange-view-mode].is-active') return this.viewButtons[0];
            if (selector === '[data-exchange-tab-refresh-controls]') return this.refreshControls;
            if (selector === '[data-exchange-tab-refresh]') return this.refreshButton;
            if (selector === '[data-exchange-tab-refresh-status]') return this.refreshStatus;
            return null;
          }
        }

        global.document = { querySelectorAll: () => [] };
        function bindBoundPositionCloseButtons() {}
        function bindLivePositionAttributionButtons() {}

        const root = new FakeRoot();
        setExchangePositionTab(root, 'positions');
        if (!root.refreshControls.hidden || !root.refreshButton.disabled) {
          throw new Error('positions tab left the manual refresh control available');
        }
        setExchangePositionTab(root, 'open-orders');
        if (root.refreshControls.hidden || root.refreshButton.textContent !== '刷新当前委托') {
          throw new Error('open-orders tab did not expose the correct refresh control');
        }
        let fetchCount = 0;
        const pendingFetches = [];
        var fetchWorkbenchPartial = () => {
          fetchCount += 1;
          return new Promise((resolve) => { pendingFetches.push(resolve); });
        };

        await loadExchangePositionTab(root, 'open-orders');
        if (fetchCount !== 0) throw new Error('normal load bypassed the cache');

        const first = loadExchangePositionTab(root, 'open-orders', { force: true });
        const second = loadExchangePositionTab(root, 'open-orders', { force: true });
        if (first !== second) throw new Error('refresh did not reuse the in-flight promise');
        await new Promise((resolve) => setTimeout(resolve, 0));
        if (fetchCount !== 1) throw new Error(`expected one refresh, got ${fetchCount}`);
        if (!root.refreshButton.disabled || root.refreshButton.textContent !== '刷新中…') {
          throw new Error('refresh button did not enter busy state');
        }

        root.panels.get('order-history').dataset.exchangeTabLoaded = 'false';
        setExchangePositionTab(root, 'order-history');
        const historyRequest = loadExchangePositionTab(root, 'order-history');
        await new Promise((resolve) => setTimeout(resolve, 0));
        if (fetchCount !== 2) throw new Error('second tab did not begin its own request');
        setExchangePositionTab(root, 'open-orders');
        if (!root.refreshButton.disabled || root.refreshButton.textContent !== '刷新中…') {
          throw new Error('active tab lost its busy state while another tab loaded');
        }

        const refreshed = new FakePanel(
          root,
          'open-orders',
          true,
          '5',
          '2026-08-10T00:05:06+00:00',
        );
        pendingFetches[0](refreshed);
        await Promise.all([first, second]);
        if (root.currentPanel !== refreshed) throw new Error('fresh partial not committed');
        if (root.refreshButton.disabled) throw new Error('refresh button stayed disabled');
        if (root.tabs[1].textContent !== '当前委托(5)') throw new Error('count not updated');
        if (!root.refreshStatus.textContent.includes('00:05:06 UTC')) {
          throw new Error('capture timestamp not reported');
        }
        pendingFetches[1](new FakePanel(
          root,
          'order-history',
          true,
          '3',
          '2026-08-10T00:05:07+00:00',
        ));
        await historyRequest;
        setExchangePositionTab(root, 'open-orders');

        const beforeFailure = root.panels.get('open-orders');
        fetchWorkbenchPartial = async () => { throw new Error('network unavailable'); };
        await loadExchangePositionTab(root, 'open-orders', { force: true });
        if (root.panels.get('open-orders') !== beforeFailure) throw new Error('refresh failure discarded old data');
        if (root.panels.get('open-orders').dataset.exchangeTabLoaded !== 'true') {
          throw new Error('refresh failure made loaded panel retry-only');
        }
        if (root.refreshButton.disabled) throw new Error('failed refresh left button disabled');
        if (!root.refreshStatus.textContent.includes('刷新失败，当前展示上次成功数据')) {
          throw new Error('failed refresh did not explain preserved data');
        }

        const retryPanel = new FakePanel(root, 'order-history', false, '0');
        const retry = new FakeElement({ exchangeTabRetry: 'order-history' });
        retryPanel.retryButton = retry;
        root.panels.set('order-history', retryPanel);
        let retryFetchCount = 0;
        fetchWorkbenchPartial = async () => {
          retryFetchCount += 1;
          return new FakePanel(root, 'order-history', true, '1', '2026-08-10T00:06:07+00:00');
        };
        bindExchangeTabRetryControls(root);
        retry.click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        if (retryFetchCount !== 1) throw new Error('retry control did not force a reload');
        """
    )
    result = subprocess.run(
        [
            "node",
            "-e",
            "\n".join(
                (
                    js[constants_start:constants_end],
                    js[functions_start:functions_end],
                    harness,
                )
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_exchange_position_tab_persists_across_dom_replacement(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for the browser-state behavior test")

    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    constants_start = js.index("const EXCHANGE_POSITION_TAB_KEY")
    constants_end = js.index("\nlet strategyRecordRequestId", constants_start)
    functions_start = js.index("function exchangePositionTabLabel")
    functions_end = js.index("\nfunction exchangePositionViewMode", functions_start)
    harness = textwrap.dedent(
        """
        const storage = new Map();
        global.window = {
          localStorage: {
            getItem: (key) => storage.has(key) ? storage.get(key) : null,
            setItem: (key, value) => storage.set(key, value),
          },
        };

        class FakeClassList {
          constructor(initial = []) { this.values = new Set(initial); }
          toggle(name, enabled) {
            if (enabled) this.values.add(name);
            else this.values.delete(name);
          }
          contains(name) { return this.values.has(name); }
        }

        class FakeElement {
          constructor(dataset, active = false) {
            this.dataset = dataset;
            this.classList = new FakeClassList(active ? ['is-active'] : []);
            this.attributes = {};
            this.listeners = {};
          }
          addEventListener(name, listener) { this.listeners[name] = listener; }
          setAttribute(name, value) { this.attributes[name] = value; }
          click() { this.listeners.click(); }
        }

        class FakeRoot {
          constructor(names) {
            this.tabs = names.map((name, index) => new FakeElement(
              { exchangePositionTab: name },
              index === 0,
            ));
            this.panels = names.map((name, index) => new FakeElement(
              { exchangePositionPanel: name },
              index === 0,
            ));
          }
          querySelectorAll(selector) {
            if (selector === '[data-exchange-position-tab]') return this.tabs;
            if (selector === '[data-exchange-position-panel]') return this.panels;
            if (selector === '[data-exchange-view-mode]') return [];
            return [];
          }
        }

        let roots = [];
        global.document = {
          querySelectorAll: (selector) => selector === '[data-exchange-position-tabs]'
            ? roots
            : [],
        };
        function restoreExchangePositionView() {}

        function assertSelected(root, expected) {
          for (const tab of root.tabs) {
            const selected = tab.dataset.exchangePositionTab === expected;
            if (tab.classList.contains('is-active') !== selected) {
              throw new Error(`wrong active tab for ${tab.dataset.exchangePositionTab}`);
            }
            if (tab.attributes['aria-selected'] !== String(selected)) {
              throw new Error(`wrong aria-selected for ${tab.dataset.exchangePositionTab}`);
            }
          }
          for (const panel of root.panels) {
            const selected = panel.dataset.exchangePositionPanel === expected;
            if (panel.classList.contains('is-active') !== selected) {
              throw new Error(`wrong active panel for ${panel.dataset.exchangePositionPanel}`);
            }
          }
        }

        const allTabs = ['positions', 'open-orders', 'order-history', 'position-history'];
        const firstRoot = new FakeRoot(allTabs);
        roots = [firstRoot];
        bindExchangePositionTabs();
        firstRoot.tabs[2].click();
        if (storage.get(EXCHANGE_POSITION_TAB_KEY) !== 'order-history') {
          throw new Error('clicked tab was not persisted');
        }
        assertSelected(firstRoot, 'order-history');

        const refreshedRoot = new FakeRoot(allTabs);
        roots = [refreshedRoot];
        bindExchangePositionTabs();
        assertSelected(refreshedRoot, 'order-history');

        storage.set(EXCHANGE_POSITION_TAB_KEY, 'unsupported');
        const invalidRoot = new FakeRoot(allTabs);
        roots = [invalidRoot];
        bindExchangePositionTabs();
        assertSelected(invalidRoot, 'positions');

        storage.set(EXCHANGE_POSITION_TAB_KEY, 'order-history');
        const incompleteRoot = new FakeRoot(['positions', 'open-orders']);
        roots = [incompleteRoot];
        bindExchangePositionTabs();
        assertSelected(incompleteRoot, 'positions');

        window.localStorage.getItem = () => { throw new Error('storage blocked'); };
        window.localStorage.setItem = () => { throw new Error('storage blocked'); };
        if (exchangePositionTab() !== 'positions') {
          throw new Error('blocked storage did not fall back to positions');
        }
        saveExchangePositionTab('position-history');
        """
    )
    result = subprocess.run(
        ["node", "-e", "\n".join((
            js[constants_start:constants_end],
            js[functions_start:functions_end],
            harness,
        ))],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_app_js_defaults_message_panel_to_latest_messages_at_top(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "function scrollMessagePanelToTop" in response.text
    assert "scrollContainer.scrollTo({ top: 0, behavior: 'auto' });" in response.text
    assert "function resetInitialMessagePanelScroll" in response.text
    assert "window.requestAnimationFrame" in response.text
    assert "resetInitialMessagePanelScroll();" in response.text
    assert "scrollMessagePanelToTop();" in response.text
    assert "scrollMessagePanelToBottom" not in response.text


def test_app_js_refreshes_message_panel_after_manual_recognition_without_jumping(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "async function refreshSelectedGroupPanel" in response.text
    assert "const previousMessageScrollTop = currentScrollContainer ? currentScrollContainer.scrollTop : 0;" in response.text
    assert "const nextPanel = await fetchMessagePanel(chatId, {" in response.text
    assert "currentMessagePanel.replaceWith(nextPanel);" in response.text
    assert "nextScrollContainer.scrollTop = previousMessageScrollTop;" in response.text
    assert "await refreshStrategyMidPanel();" in response.text
    assert "await refreshGroupList();" in response.text


def test_app_js_strategy_refresh_syncs_deepcoin_before_reloading_panels(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "await fetch('/api/execution/sync-deepcoin', { method: 'POST' });" in response.text
    assert "await refreshStrategyPanels(chatId);" in response.text


def test_app_js_refreshes_sidebar_when_switching_groups(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "syncSelectedGroupState(chatId, { focus: true });" in response.text
    assert "refreshGroupList().catch(() => {" in response.text


def test_app_js_loads_restored_group_destination_after_groups_panel_bootstraps(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    js = client.get("/static/app.js").text
    panel_slice = js[
        js.index("async function loadGroupsPanel"):
        js.index("async function loadMorePanel")
    ]

    assert "syncSelectedGroupState(selectedChatId);" in panel_slice
    assert "await loadSelectedGroupDestination('groups');" in panel_slice


def test_app_js_ignores_zero_sidebar_regression_from_partial_refresh(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "function sidebarStrategyCountTotal" in response.text
    assert "function sidebarLooksLikeZeroRegression" in response.text
    assert "sidebarLooksLikeZeroRegression(currentKolList, nextKolList)" in response.text


def test_app_js_appends_loaded_history_below_current_messages(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "currentList.insertAdjacentHTML('beforeend', nextList.innerHTML);" in response.text
    assert "currentList.insertAdjacentHTML('afterbegin', nextList.innerHTML);" not in response.text


def test_app_js_loads_older_messages_once_near_scroll_boundary(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    js = client.get("/static/app.js").text

    assert "const MESSAGE_LOAD_MORE_THRESHOLD = 320;" in js
    assert "async function loadMoreMessages(panel)" in js
    assert "loadMoreButton.dataset.loading === 'true'" in js
    assert "const remaining =" in js
    assert "scrollContainer.scrollHeight" in js
    assert "remaining <= MESSAGE_LOAD_MORE_THRESHOLD" in js
    assert "loadMoreButton.addEventListener('click', () => loadMoreMessages(panel));" in js
    assert "加载失败，点击重试" in js
    assert "currentList.insertAdjacentHTML('beforeend', nextList.innerHTML);" in js
    assert "filterForm.dataset.messageFiltersBound" in js
    assert "button.dataset.recognizeMessageBound" in js


def test_app_css_keeps_message_header_sticky(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.css")

    assert response.status_code == 200
    assert ".messages-panel-header" in response.text
    assert "position: sticky" in response.text


def test_app_css_collapses_empty_message_history_footer(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    css = client.get("/static/app.css").text

    assert ".message-list-footer:empty" in css
    assert "min-height: 0" in css


def test_app_css_keeps_panels_from_forcing_mobile_horizontal_scroll(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.css")

    assert response.status_code == 200
    assert ".panel" in response.text
    assert "box-sizing: border-box" in response.text
    assert "min-width: 0" in response.text
    assert ".message-card" in response.text
    assert "overflow-wrap: anywhere" in response.text


def test_app_css_allows_strategy_summary_prices_to_wrap_in_mid_panel(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.css")

    assert response.status_code == 200
    assert "grid-template-columns: 14px minmax(0, 1fr);" in response.text
    assert "grid-column: 1 / -1;" in response.text
    assert "grid-template-columns: repeat(auto-fit, minmax(128px, 1fr));" in response.text
    assert ".strategy-card-summary-grid strong" in response.text
    assert "overflow-wrap: anywhere" in response.text
    assert "word-break: break-word" in response.text


def test_app_css_allows_trading_settings_tab_to_scroll(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.css")

    assert response.status_code == 200
    assert '.dashboard-tab-panel[data-dashboard-panel="trading-settings"].is-active' in response.text
    assert "overflow-y: auto" in response.text


def test_app_css_includes_mobile_work_mode_navigation(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.css")

    assert response.status_code == 200
    css = response.text
    nav_selector = "[data-mobile-work-nav]"
    mobile_media = "@media (max-width: 760px)"

    mobile_start = css.rindex(mobile_media)
    default_nav_start = css.index(nav_selector)
    default_nav_end = css.index("}", default_nav_start)
    assert default_nav_start < mobile_start
    assert "display: none" in css[default_nav_start:default_nav_end]

    mobile_nav_start = css.index(nav_selector, mobile_start)
    mobile_nav_end = css.index("}", mobile_nav_start)
    assert "display: grid" in css[mobile_nav_start:mobile_nav_end]
    assert "env(safe-area-inset-bottom" in css[mobile_start:]

    mobile_layout_start = css.index(".trader-layout", mobile_start)
    mobile_layout_end = css.index("}", mobile_layout_start)
    mobile_layout = css[mobile_layout_start:mobile_layout_end]
    assert "padding: 8px 10px calc(68px + env(safe-area-inset-bottom))" in mobile_layout
    assert "height: 100dvh" not in mobile_layout
    assert "min-height: 100dvh" not in mobile_layout
    assert "overflow-y: auto" not in mobile_layout


def test_app_css_includes_mobile_first_workbench_visual_contract(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.css")

    assert response.status_code == 200
    css = response.text
    assert "--color-profit" in css
    assert ".desktop-workbench-nav" in css
    assert ".home-risk-summary" in css
    assert ".home-event-card" in css
    assert ".home-priority-risk" in css
    assert ".workbench-detail-drawer" in css
    assert "min-height: 44px" in css
    assert "env(safe-area-inset-bottom" in css


def test_strategy_record_css_exposes_phone_first_accessibility_contract(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    css = client.get("/static/app.css").text

    assert ".strategy-record-card" in css
    assert ".strategy-record-detail" in css
    assert "min-height: 44px" in css
    assert "env(safe-area-inset-bottom)" in css
    assert "overflow-wrap: anywhere" in css
    assert "@media (min-width: 761px)" in css
    assert ":focus-visible" in css


def test_embedded_strategy_records_get_a_desktop_scroll_container(tmp_path):
    css = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.css"
    ).text

    panel_selector = (
        '.trader-layout .workbench-panel[data-workbench-panel="strategies"].is-active {'
    )
    list_selector = (
        '.trader-layout [data-lazy-workbench="strategies"] > .strategy-record-list {'
    )
    feed_selector = (
        '.trader-layout [data-lazy-workbench="strategies"] > '
        ".strategy-record-list .home-event-feed {"
    )
    assert panel_selector in css
    assert list_selector in css
    assert feed_selector in css

    panel_start = css.index(panel_selector)
    panel_block = css[panel_start : css.index("}", panel_start)]
    list_start = css.index(list_selector)
    list_block = css[list_start : css.index("}", list_start)]
    feed_start = css.index(feed_selector)
    feed_block = css[feed_start : css.index("}", feed_start)]

    assert "display: grid;" in panel_block
    assert "grid-template-rows: minmax(0, 1fr);" in panel_block
    assert "overflow: hidden;" in panel_block
    assert "grid-template-rows: auto auto auto auto minmax(0, 1fr) auto;" in list_block
    assert "min-height: 0;" in feed_block
    assert "overflow-y: auto;" in feed_block


def test_strategy_record_pagination_keeps_a_44px_phone_touch_target(tmp_path):
    css = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.css"
    ).text

    selector = ".strategy-record-pagination .secondary-button {"
    assert selector in css
    start = css.index(selector)
    block = css[start : css.index("}", start)]

    assert "min-height: 44px;" in block


def test_strategy_record_css_resets_legacy_event_grid_and_wraps_detail_terms(tmp_path):
    css = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.css"
    ).text

    assert ".strategy-record-card.home-event-card {" in css
    assert ".strategy-record-card.home-event-card .strategy-record-statuses {" in css
    assert ".strategy-record-card.home-event-card .strategy-record-attention-label {" in css
    card_start = css.index(".strategy-record-card.home-event-card {")
    card_block = css[card_start : css.index("}", card_start)]
    statuses_start = css.index(
        ".strategy-record-card.home-event-card .strategy-record-statuses {"
    )
    statuses_block = css[statuses_start : css.index("}", statuses_start)]
    attention_start = css.index(
        ".strategy-record-card.home-event-card .strategy-record-attention-label {"
    )
    attention_block = css[attention_start : css.index("}", attention_start)]
    detail_term_start = css.index(".strategy-record-detail dt {")
    detail_term_block = css[detail_term_start : css.index("}", detail_term_start)]

    assert "grid-template-columns: minmax(0, 1fr);" in card_block
    for block in (statuses_block, attention_block):
        assert "grid-column: 1 / -1;" in block
        assert "grid-row: auto;" in block
        assert "align-self: auto;" in block
    assert "color: inherit;" in statuses_block
    assert "color: #fde68a;" in attention_block
    assert "overflow-wrap: anywhere;" in detail_term_block


def test_group_context_responsive_contract(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    css = client.get("/static/app.css").text

    assert ".group-context" in css
    assert "position: sticky" in css
    assert ".group-picker-surface" in css
    assert "min-height: 48px" in css
    assert "env(safe-area-inset-bottom)" in css
    assert "@media (max-width: 760px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_group_switch_prioritizes_active_destination_without_waiting_for_both_panels(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    js = client.get("/static/app.js").text

    assert "activeWorkbenchView" in js
    assert "loadVisibleGroupDestination" in js
    assert "loadBackgroundGroupDestination" not in js
    assert "Promise.all([" not in js


def test_app_js_lazy_loads_workbench_destinations_without_forced_startup_refresh(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    js = client.get("/static/app.js").text

    assert "workbenchLoadState" in js
    assert "ensureWorkbenchViewLoaded" in js
    assert "loadStrategyRecords" in js
    assert "'/strategy-records?filter=needs_attention'" in js
    assert "loadPositionsPanel" in js
    assert "fetch(`/strategy-records?${params}`, { cache: 'no-store' })" in js
    assert js.count("bindWorkflowFilters();") >= 5
    assert "workflowFilterBound" in js
    assert "refreshFromDatabaseChanges({ force: true });" not in js
    assert "scheduleRecoveryRefresh" in js


def test_strategy_record_controller_persists_independent_scope_and_guards_stale_responses(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    js = client.get("/static/app.js").text

    assert "telegram-workbench:strategy-filter" in js
    assert "telegram-workbench:strategy-group" in js
    assert "telegram-workbench:strategy-scroll" in js
    assert "let strategyRecordRequestId = 0;" in js
    assert "let strategyRecordHasPendingChanges = false;" in js
    load_start = js.index("async function loadStrategyRecords")
    load_end = js.index("\nasync function ", load_start + 1)
    load_block = js[load_start:load_end]
    assert "const requestId = ++strategyRecordRequestId;" in load_block
    assert "if (requestId !== strategyRecordRequestId) return false;" in load_block
    assert load_block.index("if (requestId !== strategyRecordRequestId) return false;") < load_block.index(
        "replaceStrategyRecordList"
    )


def test_strategy_record_controller_defers_live_changes_and_preserves_last_success(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    js = client.get("/static/app.js").text

    assert "function noteStrategyRecordChanges" in js
    assert "strategyRecordHasPendingChanges = true;" in js
    assert "有新变化，点击查看" in js
    assert "条新变化" not in js
    assert "force: true, revealChanges: true, scrollMode: 'preserve'" in js
    assert "showStrategyRecordLoadError" in js
    assert "lastSuccessfulStrategyRecordAt" in js
    assert "bindStrategyRecordController();" in js
    assert "saveStrategyRecordScrollPosition" in js
    assert "restoreStrategyRecordScrollPosition" in js
    load_start = js.index("async function loadStrategyRecords")
    load_end = js.index("\nasync function ", load_start + 1)
    load_block = js[load_start:load_end]
    assert "if (revealChanges) strategyRecordHasPendingChanges = false;" in load_block
    assert load_block.index("if (requestId !== strategyRecordRequestId) return false;") < load_block.index(
        "if (revealChanges) strategyRecordHasPendingChanges = false;"
    )
    assert "if (force || revealChanges)" not in load_block


def test_strategy_record_reconnect_marks_changes_without_reloading_the_page(tmp_path):
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    start = js.index("function connectLiveUpdates")
    end = js.index("\nfunction startPollingUpdates", start)
    block = js[start:end]

    assert "source.onopen" in block
    assert "noteStrategyRecordChanges();" in block
    assert "state: 'monitoring'" in block
    assert "window.location.reload" not in block


def test_strategy_record_filter_and_group_commit_only_after_guarded_success(tmp_path):
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    load_start = js.index("async function loadStrategyRecords")
    load_end = js.index("\nasync function ", load_start + 1)
    load_block = js[load_start:load_end]
    catch_block = load_block[load_block.index("} catch (error) {"):]

    assert "lastSuccessfulStrategyRecordSelection" in js
    assert "commitSuccessfulStrategyRecordSelection" in load_block
    assert load_block.index("if (requestId !== strategyRecordRequestId) return false;") < load_block.index(
        "commitSuccessfulStrategyRecordSelection"
    )
    assert "if (strategyRecordSelectionMatches(selection))" in catch_block
    assert "rollbackStrategyRecordSelection();" in catch_block
    assert catch_block.index("if (requestId !== strategyRecordRequestId) return false;") < catch_block.index(
        "rollbackStrategyRecordSelection();"
    )
    assert js.count("strategyRecordStorageSet(STRATEGY_RECORD_FILTER_KEY") == 1
    assert js.count("strategyRecordStorageSet(STRATEGY_RECORD_GROUP_KEY") == 1
    assert "scrollMode: 'reset'" in js


def test_strategy_record_scroll_modes_reset_scope_changes_and_restore_returns(tmp_path):
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text

    assert "function resetStrategyRecordScrollPosition" in js
    assert "function replaceStrategyRecordList(fragment, { scrollMode })" in js
    assert "if (scrollMode === 'reset')" in js
    assert "resetStrategyRecordScrollPosition();" in js
    assert "restoreStrategyRecordScrollPosition();" in js
    assert "scrollMode: 'reset'" in js
    assert "scrollMode: 'preserve'" in js


def test_strategy_record_scroll_restore_only_runs_when_the_list_is_mounted(tmp_path):
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    ready_start = js.index("window.addEventListener('DOMContentLoaded'")
    ready_block = js[ready_start:]
    guard = "if (document.querySelector('[data-strategy-record-list]')) {"

    assert guard in ready_block
    guard_start = ready_block.index(guard)
    guard_end = ready_block.index("\n  }", guard_start)
    assert "restoreStrategyRecordScrollPosition();" in ready_block[guard_start:guard_end]
    assert "restoreStrategyRecordScrollPosition();" not in ready_block[:guard_start]


def test_app_js_restores_persisted_group_as_state_without_clicking_it(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    js = client.get("/static/app.js").text

    persisted_block_start = js.index("if (persisted && root.querySelector")
    persisted_block_end = js.index("\n  }", persisted_block_start)
    persisted_block = js[persisted_block_start:persisted_block_end]

    assert "syncSelectedGroupState(persisted" in persisted_block
    assert ".click()" not in persisted_block
    assert "initialSelectedChatId" in js
    assert "syncSelectedGroupState(initialSelectedChatId);" in js


def test_app_js_binds_mobile_work_navigation_to_existing_dashboard_views(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "function bindMobileWorkNavigation" in response.text
    assert "[data-workbench-view]" in response.text
    assert "const WORKBENCH_VIEWS = ['strategies', 'positions', 'activity', 'groups', 'more'];" in response.text
    assert "view === 'positions' ? 'exchange-positions' : null" in response.text
    assert "bindMobileWorkNavigation();" in response.text


def test_app_js_binds_workbench_navigation_and_home_event_filters(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "function bindWorkbenchNavigation" in response.text
    assert "[data-workbench-view]" in response.text
    assert "[data-workbench-panel]" in response.text
    assert "function bindHomeEventFilters" in response.text
    assert "[data-home-event-filter]" in response.text
    assert "[data-new-home-events]" in response.text
    assert "function bindGroupContext" in response.text
    assert "telegram-workbench:selected-group" in response.text
    assert "[data-group-picker-search]" in response.text


def test_app_js_defaults_to_strategy_records_and_keeps_orphan_position_deep_link(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    js = client.get("/static/app.js").text

    assert "home: { key: null, promise: null }" not in js
    assert "activity: { key: null, promise: null }" in js
    assert "groups: { key: null, promise: null }" in js
    assert "setWorkbenchView(requestedView || 'strategies')" in js
    assert "params.get('view')" in js
    assert "setWorkbenchView('positions')" in js


def test_app_js_schedules_initial_requested_view_after_first_paint(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    js = client.get("/static/app.js").text

    scheduler_start = js.index("function scheduleInitialWorkbenchView")
    scheduler_end = js.index("\nfunction ", scheduler_start + 1)
    scheduler = js[scheduler_start:scheduler_end]
    dom_ready_start = js.index("window.addEventListener('DOMContentLoaded'")
    dom_ready = js[dom_ready_start:]

    assert scheduler.count("window.requestAnimationFrame") >= 2
    assert "setWorkbenchView(requestedView || 'strategies')" in scheduler
    assert "scheduleInitialWorkbenchView();" in dom_ready
    assert "setWorkbenchView(requestedView || 'strategies')" not in dom_ready
    assert "focusRequestedPosition().catch(() => {});" not in dom_ready
    assert "await ensureWorkbenchViewLoaded('positions')" in scheduler


def test_workbench_loader_returns_explicit_success_for_every_path(tmp_path):
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    start = js.index("async function ensureWorkbenchViewLoaded")
    end = js.index("\nasync function focusRequestedPosition", start)
    block = js[start:end]

    assert "if (!state) return false" in block
    assert "state.key === key) return true" in block
    assert "if (state.promise) return state.promise" in block
    assert "if (!loaded)" in block
    assert "return false" in block
    assert "return true" in block
    assert ".catch((error) =>" not in block
    assert "catch (error)" in block
    assert "showWorkbenchLoadError(view, error)" in block


def test_activity_and_settings_failures_keep_a_visible_retry_surface(tmp_path):
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text

    activity_start = js.index("async function retryActivityAfterGroups")
    activity_end = js.index("\nasync function ", activity_start + 1)
    activity_retry = js[activity_start:activity_end]
    ensure_start = js.index("async function ensureWorkbenchViewLoaded")
    ensure_end = js.index("\nasync function focusRequestedPosition", ensure_start)
    ensure_block = js[ensure_start:ensure_end]
    settings_start = js.index("async function openDashboardPanel")
    settings_end = js.index("\nfunction bindDashboardTabs", settings_start)
    settings_block = js[settings_start:settings_end]
    bind_start = js.index("function bindDashboardTabs")
    bind_end = js.index("\nfunction bindWorkbenchNavigation", bind_start)
    bind_block = js[bind_start:bind_end]

    assert "ensureWorkbenchViewLoaded('groups', { force: true })" in activity_retry
    assert "ensureWorkbenchViewLoaded('activity', { force: true })" in activity_retry
    assert "showActivityBootstrapError" in ensure_block
    assert "if (!groupsLoaded || !getSelectedChatId())" in ensure_block
    assert "const moreLoaded = await ensureWorkbenchViewLoaded('more')" in settings_block
    assert "const targetPanel = document.querySelector" in settings_block
    assert "if (!moreLoaded || !targetPanel)" in settings_block
    assert "showDashboardPanelLoadError" in settings_block
    assert settings_block.index("if (!moreLoaded || !targetPanel)") < settings_block.index(
        "dashboard.dataset.activeWorkbenchView = 'settings'"
    )
    assert ".catch((error) => showDashboardPanelLoadError(tab, error))" in bind_block


def test_missing_settings_target_retry_forces_more_panel_refetch(tmp_path):
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    retry_start = js.index("async function retryDashboardPanelLoad")
    retry_end = js.index("\nfunction ", retry_start + 1)
    retry_block = js[retry_start:retry_end]
    error_start = js.index("function showDashboardPanelLoadError")
    error_end = js.index("\nfunction ", error_start + 1)
    error_block = js[error_start:error_end]

    assert "workbenchLoadState.more.key = null" in retry_block
    assert "ensureWorkbenchViewLoaded('more', { force: true })" in retry_block
    assert "return openDashboardPanel(tab)" in retry_block
    assert "retryDashboardPanelLoad(tab)" in error_block


def test_app_js_coordinates_workbench_and_settings_as_one_primary_surface(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    js = client.get("/static/app.js").text

    assert "function setActiveDashboardPanel" in js
    assert "function setWorkbenchView" in js
    assert "function openDashboardPanel" in js
    settings_start = js.index("function openDashboardPanel")
    settings_end = js.index("\nfunction ", settings_start + 1)
    settings_block = js[settings_start:settings_end]
    assert "data-return-workbench-view" in settings_block
    assert "[data-workbench-panel]" in settings_block
    assert "classList.remove('is-active')" in settings_block
    assert "if (WORKBENCH_VIEWS.includes(currentView))" in settings_block
    assert "setAttribute('data-return-workbench-view', currentView)" in settings_block


def test_app_js_does_not_cache_stale_group_destination_as_loaded(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    js = client.get("/static/app.js").text

    visible_start = js.index("async function loadVisibleGroupDestination")
    visible_end = js.index("\nasync function ", visible_start + 1)
    visible_block = js[visible_start:visible_end]
    selected_start = js.index("async function loadSelectedGroupDestination")
    selected_end = js.index("\nasync function ", selected_start + 1)
    selected_block = js[selected_start:selected_end]
    ensure_start = js.index("async function ensureWorkbenchViewLoaded")
    ensure_end = js.index("\nfunction ", ensure_start + 1)
    ensure_block = js[ensure_start:ensure_end]

    assert visible_block.count("return false") >= 2
    assert visible_block.count("return true") >= 2
    assert "committed = await loadVisibleGroupDestination" in selected_block
    assert "if (!committed) return false" in selected_block
    assert "loaded = await loadSelectedGroupDestination(view)" in ensure_block
    assert "if (!loaded)" in ensure_block


def test_app_js_loads_message_companion_on_any_screen_size(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    js = client.get("/static/app.js").text

    assert "function loadGroupDetailCompanion" in js
    companion_start = js.index("function loadGroupDetailCompanion")
    companion_end = js.index("\nfunction ", companion_start + 1)
    companion_block = js[companion_start:companion_end]
    assert "matchMedia('(min-width: 761px)')" not in companion_block
    assert "requestId !== groupSwitchRequestId" in companion_block
    assert "getSelectedChatId() !== chatId" in companion_block
    assert "await detailPromise" in companion_block


def test_group_switch_starts_detail_fetch_before_strategy_wait_and_aborts_previous(
    tmp_path,
):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    js = client.get("/static/app.js").text

    assert "let activeGroupSwitchController = null;" in js
    assert "activeGroupSwitchController.abort();" in js
    assert "new AbortController()" in js
    assert "signal: controller.signal" in js
    assert "error?.name === 'AbortError'" in js

    bind_start = js.index("function bindGroupLinks")
    bind_end = js.index("\nasync function loadVisibleGroupDestination", bind_start)
    bind_block = js[bind_start:bind_end]
    assert bind_block.index("const detailPromise =") < bind_block.index(
        "await loadVisibleGroupDestination"
    )
    assert "fetchDetailPanel(chatId, { signal: controller.signal })" in bind_block


def test_app_js_routes_group_detail_updates_to_the_active_workbench_panel(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    js = client.get("/static/app.js").text

    assert "function getDetailPanelForWorkbenchView" in js
    helper_start = js.index("function getDetailPanelForWorkbenchView")
    helper_end = js.index("\nasync function ", helper_start + 1)
    helper_block = js[helper_start:helper_end]
    assert '[data-workbench-panel="${activeView}"] [data-detail-panel]' in helper_block

    selected_start = js.index("async function loadSelectedGroupDestination")
    selected_end = js.index("\nasync function ", selected_start + 1)
    selected_block = js[selected_start:selected_end]
    assert "const legacyView = view === 'activity' ? 'activity' : 'strategies';" in selected_block
    assert "activeView: legacyView" in selected_block
    assert "const detailPanel = getDetailPanelForWorkbenchView(view);" in selected_block
    assert "detailPanel," in selected_block

    group_link_start = js.index("function bindGroupLinks")
    group_link_end = js.index("\nasync function ", group_link_start + 1)
    group_link_block = js[group_link_start:group_link_end]
    assert "activeView = document.querySelector" in group_link_block
    assert "const detailPanel = getDetailPanelForWorkbenchView(activeView);" in group_link_block
    assert "document.querySelector('[data-detail-panel]')" not in group_link_block


def test_mobile_assets_use_five_destinations_and_bottom_sheet_settings(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    css = client.get("/static/app.css").text

    assert "grid-template-columns: repeat(5, minmax(0, 1fr));" in css
    mobile_start = css.index("@media (max-width: 760px)")
    mobile_css = css[mobile_start:]
    assert ".settings-menu" in mobile_css
    assert "position: fixed" in mobile_css
    assert "bottom: calc(68px + env(safe-area-inset-bottom))" in mobile_css
    sticky_start = mobile_css.index(".dashboard-tab-panel .prompt-actions")
    sticky_end = mobile_css.index("\n  }", sticky_start)
    sticky_block = mobile_css[sticky_start:sticky_end]
    assert "bottom: calc(68px + env(safe-area-inset-bottom));" in sticky_block


def test_app_assets_expose_persistent_mutation_states(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    js = client.get("/static/app.js").text
    css = client.get("/static/app.css").text

    assert "aria-busy" in js
    assert ".is-loading" in css
    assert ".is-empty" in css
    assert ".is-stale" in css
    assert ".is-error" in css


def test_app_js_binds_recovery_order_confirmation_dry_run(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "bindRecoveryOrderConfirmationButtons" in response.text
    assert "/api/recovery-order-confirm-dry-run" in response.text
    assert "ready_for_live_order" in response.text
    assert "data-recovery-order-confirm-status" in response.text


def test_app_js_binds_recovery_live_submit_gate_simulation(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "bindRecoverySubmitGateButtons" in response.text
    assert "/api/recovery-live-submit-gate" in response.text
    assert "would_submit" in response.text
    assert "data-recovery-submit-gate-status" in response.text


def test_app_js_binds_recovery_live_submit(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "data-live-recovery-submit" in response.text
    assert "/api/recovery-live-submit" in response.text
    assert "实盘提交" in response.text


def test_live_action_confirmation_uses_shared_dialog_hooks(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "requestLiveActionConfirmation" in response.text
    assert "data-live-action-confirm" in response.text
    assert "data-manual-close-lifecycle" in response.text
    assert "data-bind-live-position" in response.text


def test_bound_position_close_app_js_binds_confirmed_exact_market_close(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "function bindBoundPositionCloseButtons" in response.text
    assert "[data-close-bound-position]" in response.text
    assert "/api/execution/close-bound-position" in response.text
    assert "body: JSON.stringify({ pos_id: posId })" in response.text
    assert "正在提交市价全平..." in response.text
    assert "市价全平已提交，正在刷新..." in response.text
    assert "bindBoundPositionCloseButtons();" in response.text


def test_live_action_confirmation_clears_stale_return_value_before_opening(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "dialog.returnValue = '';" in response.text
    assert response.text.index("dialog.returnValue = '';") < response.text.index("dialog.showModal();")
