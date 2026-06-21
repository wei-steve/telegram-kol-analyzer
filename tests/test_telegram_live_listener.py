import asyncio
from datetime import UTC, datetime

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig, save_ai_recognition_config
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.models import RawMessage
from telegram_kol_research.telegram_live_listener import persist_live_message_event


class _FakeSender:
    def __init__(self, first_name: str, last_name: str = "") -> None:
        self.first_name = first_name
        self.last_name = last_name


class _FakeMessage:
    def __init__(self) -> None:
        self.id = 42
        self.sender_id = 7
        self.message = "live hello"
        self.reply_to_msg_id = None
        self.date = datetime(2026, 4, 19, 10, 0, tzinfo=UTC)
        self.edit_date = None
        self.media = None
        self.photo = None
        self.document = None

    async def get_sender(self):
        return _FakeSender("Alice", "Trader")


class _FakeEvent:
    def __init__(self) -> None:
        self.chat_id = 123
        self.message = _FakeMessage()


def test_persist_live_message_event_writes_db_and_broker_event(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()

    asyncio.run(
        persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
        )
    )

    with session_factory() as session:
        stored = session.query(RawMessage).filter(RawMessage.chat_id == 123).one()

    assert stored.message_id == 42
    assert stored.sender_name == "Alice Trader"
    assert stored.text == "live hello"
    assert broker.published_events[-1]["chat_id"] == 123
    assert broker.published_events[-1]["message_id"] == 42


def test_persist_live_message_event_triggers_strategy_alert_processor(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()
    processed = []

    async def fake_strategy_alert_processor(**kwargs):
        processed.append(kwargs)
        return {"status": "sent"}

    asyncio.run(
        persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            chat_title="VIP BTC Room",
            strategy_alert_config=object(),
            strategy_alert_processor=fake_strategy_alert_processor,
        )
    )

    assert len(processed) == 1
    assert processed[0]["chat_title"] == "VIP BTC Room"
    assert processed[0]["record"].chat_id == 123
    assert processed[0]["record"].message_id == 42


def test_persist_live_message_event_loads_ai_config_path_per_message(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()
    config_path = tmp_path / "ai_recognition.yaml"
    save_ai_recognition_config(
        config_path,
        AiRecognitionConfig(
            recognition_prompt="First prompt.",
            mimo_direct_prompt="First MiMo prompt.",
        ),
    )
    seen_prompts: list[str] = []

    def fake_recognize_message_now(session_factory, *, raw_message_id, ai_recognition_config):
        seen_prompts.append(ai_recognition_config.recognition_prompt)
        return None

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.recognize_message_now",
        fake_recognize_message_now,
    )

    asyncio.run(
        persist_live_message_event(
            event=_FakeEvent(),
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            ai_recognition_config_path=config_path,
        )
    )
    save_ai_recognition_config(
        config_path,
        AiRecognitionConfig(
            recognition_prompt="Second prompt.",
            mimo_direct_prompt="Second MiMo prompt.",
        ),
    )
    second_event = _FakeEvent()
    second_event.message.id = 43
    asyncio.run(
        persist_live_message_event(
            event=second_event,
            session_factory=session_factory,
            broker=broker,
            media_root=tmp_path / "media",
            ai_recognition_config_path=config_path,
        )
    )

    assert seen_prompts[0].startswith("First prompt.")
    assert seen_prompts[1].startswith("Second prompt.")
