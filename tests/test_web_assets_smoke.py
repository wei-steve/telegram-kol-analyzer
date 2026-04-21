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
