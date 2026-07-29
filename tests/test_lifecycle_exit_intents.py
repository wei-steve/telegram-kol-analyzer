from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.lifecycle_exit_intents import has_live_execution_binding
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    StrategyLifecycle,
)


@pytest.mark.parametrize("leg_status", ["pending", "submitted", "partially_filled"])
def test_unknown_binding_with_pending_entry_leg_is_live_execution_exposure(
    tmp_path,
    leg_status,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=4106,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="unknown",
            strategy_instance_id="deepcoin:88:4106:BTC:short",
        )
        session.add(binding)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=2,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="entry-order-2",
                status=leg_status,
                attribution_status="unassigned",
            )
        )
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=4106,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 27, tzinfo=UTC),
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.commit()

        assert has_live_execution_binding(session, lifecycle) is True


def test_unknown_binding_with_only_terminal_entry_legs_is_not_live_exposure(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=4106,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="unknown",
            strategy_instance_id="deepcoin:88:4106:BTC:short",
        )
        session.add(binding)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=2,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="entry-order-2",
                status="cancelled",
                terminal_reason="operator_cancelled_unfilled_entry_leg",
                attribution_status="unassigned",
            )
        )
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=4106,
            symbol="BTC",
            side="short",
            lifecycle_status="exited",
            signal_at=datetime(2026, 7, 27, tzinfo=UTC),
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.commit()

        assert has_live_execution_binding(session, lifecycle) is False


def test_unrelated_pending_binding_does_not_create_execution_exposure(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        unrelated = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=9999,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="unknown",
            strategy_instance_id="deepcoin:88:9999:BTC:short",
        )
        session.add(unrelated)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=unrelated.id,
                strategy_instance_id=unrelated.strategy_instance_id,
                leg_index=1,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="other-order",
                status="pending",
                attribution_status="unassigned",
            )
        )
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=4106,
            symbol="BTC",
            side="short",
            lifecycle_status="exited",
            signal_at=datetime(2026, 7, 27, tzinfo=UTC),
        )
        session.add(lifecycle)
        session.commit()

        assert has_live_execution_binding(session, lifecycle) is False
