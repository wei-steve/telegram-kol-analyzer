import asyncio
from datetime import UTC, datetime

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
