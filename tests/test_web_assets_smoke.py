from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app


def test_static_assets_are_served(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.css")

    assert response.status_code == 200


def test_deepcoin_history_assets_are_scoped_to_the_history_panel(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    css = client.get("/static/app.css").text
    history_css = css[
        css.index("/* DeepCoin historical-position treatment"):
        css.index(".exchange-position-actions")
    ]

    assert ".exchange-tab.is-active::after" in css
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


def test_app_js_restores_exchange_position_view_after_partial_reload(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    js = client.get("/static/app.js").text

    assert "const EXCHANGE_POSITION_VIEW_KEY" in js
    assert "function restoreExchangePositionView(root)" in js
    assert "restoreExchangePositionView(root);" in js
    assert "saveExchangePositionView(mode);" in js


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


def test_app_css_keeps_message_header_sticky(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.css")

    assert response.status_code == 200
    assert ".messages-panel-header" in response.text
    assert "position: sticky" in response.text


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
    assert "const committed = await loadVisibleGroupDestination" in selected_block
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
    assert "fetchDetailPanel(chatId)" in companion_block


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
    assert "detailPanel: getDetailPanelForWorkbenchView(view)" in selected_block

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
