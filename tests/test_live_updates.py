from telegram_kol_research.live_updates import LiveUpdateBroker
from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app


def test_live_update_broker_formats_message_event():
    broker = LiveUpdateBroker()
    payload = broker.format_message_event(chat_id=7, message_id=99)

    assert payload.startswith("event: message")
    assert '"chat_id": 7' in payload
    assert '"message_id": 99' in payload


def test_events_route_returns_sse_payload(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    async def fake_stream():
        yield "event: message\ndata: {\"chat_id\": 7, \"message_id\": 99}\n\n"

    class FakeBroker:
        def stream(self):
            return fake_stream()

        def close(self):
            return None

    app.state.live_update_broker = FakeBroker()
    client = TestClient(app)

    response = client.get("/api/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: message" in response.text
