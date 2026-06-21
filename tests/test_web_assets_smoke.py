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
    assert "panel.scrollTo({ top: 0, behavior: 'auto' });" in response.text
    assert "function resetInitialMessagePanelScroll" in response.text
    assert "window.requestAnimationFrame" in response.text
    assert "resetInitialMessagePanelScroll();" in response.text
    assert "scrollMessagePanelToTop();" in response.text
    assert "scrollMessagePanelToBottom" not in response.text


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
