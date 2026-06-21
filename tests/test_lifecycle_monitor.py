from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.lifecycle_monitor import LifecycleMonitor, StateTransition
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.models import RawMessage, StrategyLifecycle


def test_lifecycle_monitor_rejects_entry_before_signal_time(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9033,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 19, 11, 32, 47, tzinfo=UTC),
            entry_range_low=62300,
            entry_range_high=62500,
            stop_loss=60800,
            take_profit="63600/64800",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    monitor = LifecycleMonitor(session_factory, LiveUpdateBroker())
    monitor._apply_transitions(
        [
            StateTransition(
                signal_id=lifecycle_id,
                from_status="pending_entry",
                to_status="entered",
                trigger_price=62486.1,
                occurred_at=datetime(2026, 6, 19, 4, 54, tzinfo=UTC),
            )
        ]
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert lifecycle.lifecycle_status == "pending_entry"
    assert lifecycle.entered_at is None
    assert lifecycle.entry_price_actual is None


def test_lifecycle_backfill_keeps_entered_record_with_entry_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9033,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 19, 11, 32, 47, tzinfo=UTC),
            entered_at=datetime(2026, 6, 19, 11, 32, 47, tzinfo=UTC),
            entry_price_actual=62486.1,
            entry_range_low=62300,
            entry_range_high=62500,
            stop_loss=60800,
            take_profit="63600/64800",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    monitor = LifecycleMonitor(session_factory, LiveUpdateBroker())
    monitor.backfill_from_trade_ideas()

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entered_at == datetime(2026, 6, 19, 11, 32, 47)
    assert lifecycle.entry_price_actual == 62486.1


def test_lifecycle_monitor_rejects_protective_stop_before_management_signal(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=1395,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 17, 10, 26, 13, tzinfo=UTC),
            entered_at=datetime(2026, 6, 18, 4, 11, tzinfo=UTC),
            entry_price_actual=63794.4,
            entry_range_low=61800,
            entry_range_high=63800,
            stop_loss=63794.4,
            take_profit="65500/66500/67500",
            management_signal_message_id=1400,
            management_action="partial_take_profit, move_stop_to_protect",
        )
        management_message = RawMessage(
            chat_id=88,
            message_id=1400,
            posted_at=datetime(2026, 6, 18, 8, 36, 45, tzinfo=UTC),
            text="现价64500附近提前止盈一半带保护",
        )
        session.add_all([lifecycle, management_message])
        session.commit()
        lifecycle_id = lifecycle.id

    monitor = LifecycleMonitor(session_factory, LiveUpdateBroker())
    monitor._apply_transitions(
        [
            StateTransition(
                signal_id=lifecycle_id,
                from_status="entered",
                to_status="exited",
                exit_reason="stop_loss",
                trigger_price=63794.4,
                occurred_at=datetime(2026, 6, 18, 5, 8, tzinfo=UTC),
            )
        ]
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exited_at is None
    assert lifecycle.exit_reason is None
