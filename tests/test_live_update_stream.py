import asyncio

from telegram_kol_research.live_updates import LiveUpdateBroker


async def _read_one_event() -> str:
    broker = LiveUpdateBroker()
    stream = broker.stream()
    first_chunk = await anext(stream)
    assert first_chunk == ": keep-alive\n\n"
    broker.publish_message(chat_id=7, message_id=99)
    second_chunk = await anext(stream)
    await stream.aclose()
    return second_chunk


def test_live_update_broker_stream_yields_published_message():
    payload = asyncio.run(_read_one_event())

    assert payload.startswith("event: message")
    assert '"chat_id": 7' in payload
    assert '"message_id": 99' in payload
