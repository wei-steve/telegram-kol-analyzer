from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app


def test_static_assets_are_served(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.css")

    assert response.status_code == 200


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
