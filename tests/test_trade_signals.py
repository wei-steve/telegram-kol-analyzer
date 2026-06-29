from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import TradeSignal
from telegram_kol_research.trade_signals import enqueue_trade_signal
from telegram_kol_research.trade_signals import list_pending_trade_signals
from telegram_kol_research.trade_signals import mark_trade_signal_failed
from telegram_kol_research.trade_signals import mark_trade_signal_submitted


def test_enqueue_trade_signal_creates_stable_pending_signal(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    first = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="alice",
        chat_id=100,
        message_id=55,
        symbol="btc",
        side="LONG",
        action="open_position",
        payload={"hello": "world"},
    )
    second = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="alice",
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        action="open_position",
        payload={"hello": "again"},
    )

    assert first.id == second.id
    assert second.signal_uid == "deepcoin:recovery:100:55:BTC:long:open_position"
    assert second.strategy_instance_id == "deepcoin:100:55:BTC:long"
    assert second.status == "pending"
    assert second.payload == {"hello": "again"}
    assert [item.id for item in list_pending_trade_signals(session_factory)] == [first.id]


def test_trade_signal_status_transitions(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="alice",
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        action="open_position",
        payload={},
    )

    mark_trade_signal_failed(session_factory, signal_id=signal.id, error="boom")
    with session_factory() as session:
        row = session.query(TradeSignal).one()
    assert row.status == "failed"
    assert row.attempts == 1
    assert row.last_error == "boom"

    refreshed = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="alice",
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        action="open_position",
        payload={},
    )
    assert refreshed.status == "pending"

    mark_trade_signal_submitted(
        session_factory,
        signal_id=signal.id,
        result={"submitted": True},
    )
    with session_factory() as session:
        row = session.query(TradeSignal).one()
    assert row.status == "submitted"
    assert '"submitted": true' in row.result_json
