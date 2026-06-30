from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_events import ExecutionEventRecord
from telegram_kol_research.execution_events import list_execution_events
from telegram_kol_research.execution_events import record_execution_event


def test_record_execution_event_persists_json_context_and_ids(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    event_id = record_execution_event(
        session_factory,
        ExecutionEventRecord(
            action="adjust_position_tpsl",
            strategy_instance_id="deepcoin:100:55:ETH:long",
            execution_binding_id=7,
            trade_signal_id=9,
            kol_id="alice",
            chat_id=100,
            message_id=55,
            source_message_id=88,
            symbol="eth",
            side="LONG",
            order_id="new-tpsl",
            related_order_id="old-tpsl",
            pos_id="pos-1",
            reason="kol_stop_loss_update",
            before={"stop_loss": 1562.29, "take_profit": 1600.25},
            after={"stop_loss": 1571.78, "take_profit": 1609.73},
            request={"posId": "pos-1"},
            response={"code": "0"},
            exchange_event_time=datetime(2026, 6, 30, 8, 30, tzinfo=UTC),
            created_at=datetime(2026, 6, 30, 8, 31, tzinfo=UTC),
        ),
    )

    events = list_execution_events(
        session_factory,
        strategy_instance_id="deepcoin:100:55:ETH:long",
    )

    assert events[0].id == event_id
    assert events[0].action == "adjust_position_tpsl"
    assert events[0].symbol == "ETH"
    assert events[0].side == "long"
    assert events[0].order_id == "new-tpsl"
    assert events[0].related_order_id == "old-tpsl"
    assert events[0].pos_id == "pos-1"
    assert events[0].before == {"stop_loss": 1562.29, "take_profit": 1600.25}
    assert events[0].after == {"stop_loss": 1571.78, "take_profit": 1609.73}
    assert events[0].request == {"posId": "pos-1"}
    assert events[0].response == {"code": "0"}


def test_list_execution_events_filters_by_order_position_and_action(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            action="cancel_position_tpsl",
            strategy_instance_id="strategy-1",
            order_id="old-sl",
            pos_id="pos-1",
            created_at=datetime(2026, 6, 30, 8, 0, tzinfo=UTC),
        ),
    )
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            action="set_position_tpsl",
            strategy_instance_id="strategy-1",
            order_id="new-sl",
            pos_id="pos-1",
            created_at=datetime(2026, 6, 30, 8, 1, tzinfo=UTC),
        ),
    )
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            action="set_position_tpsl",
            strategy_instance_id="strategy-2",
            order_id="other",
            pos_id="pos-2",
            created_at=datetime(2026, 6, 30, 8, 2, tzinfo=UTC),
        ),
    )

    by_pos = list_execution_events(session_factory, pos_id="pos-1")
    by_action = list_execution_events(session_factory, action="cancel_position_tpsl")
    by_order = list_execution_events(session_factory, order_id="new-sl")

    assert [event.order_id for event in by_pos] == ["new-sl", "old-sl"]
    assert [event.order_id for event in by_action] == ["old-sl"]
    assert [event.action for event in by_order] == ["set_position_tpsl"]
