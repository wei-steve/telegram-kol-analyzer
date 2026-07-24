from __future__ import annotations

from datetime import UTC, datetime


NOW = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)


def test_trigger_multi_take_profit_convergence_is_queued_per_exact_entry_leg(tmp_path):
    """A trigger entry saves its full staged TP plan before it can fill."""

    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.execution_bindings import (
        ExecutionBindingRecord,
        ExecutionOrderLegRecord,
        upsert_execution_binding,
        upsert_execution_order_leg,
    )
    from telegram_kol_research.trigger_take_profit_convergence import (
        create_or_get_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol",
            chat_id=1,
            message_id=1,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="open",
        ),
    )
    entry_leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            venue="deepcoin",
            status="submitting",
            request={"instId": "BTC-USDT-SWAP"},
        ),
    )

    with session_factory() as session:
        queued = create_or_get_trigger_take_profit_convergence(
            session,
            venue="deepcoin",
            execution_order_leg_id=entry_leg_id,
            desired_take_profits=[
                {"price": "64500", "allocation_pct": "50"},
                {"price": "63800", "allocation_pct": "30"},
                {"price": "63100", "allocation_pct": "20"},
            ],
            created_at=NOW,
        )
        same = create_or_get_trigger_take_profit_convergence(
            session,
            venue="deepcoin",
            execution_order_leg_id=entry_leg_id,
            desired_take_profits=[
                {"price": "64500", "allocation_pct": "50"},
                {"price": "63800", "allocation_pct": "30"},
                {"price": "63100", "allocation_pct": "20"},
            ],
            created_at=NOW,
        )
        session.commit()

        assert queued.id == same.id
        assert queued.status == "waiting_position"
        assert queued.execution_binding_id == binding_id
        assert queued.execution_order_leg_id == entry_leg_id
        assert queued.desired_take_profits_json == (
            '[{"allocation_pct":"50","price":"64500"},'
            '{"allocation_pct":"30","price":"63800"},'
            '{"allocation_pct":"20","price":"63100"}]'
        )


def test_trigger_single_take_profit_convergence_is_queued_for_full_position(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.execution_bindings import (
        ExecutionBindingRecord,
        ExecutionOrderLegRecord,
        upsert_execution_binding,
        upsert_execution_order_leg,
    )
    from telegram_kol_research.trigger_take_profit_convergence import (
        create_or_get_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol", chat_id=1, message_id=1, symbol="BTC", side="long",
            venue="deepcoin", margin_mode="cross", position_mode="split", status="open",
        ),
    )
    entry_leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, leg_index=1, purpose="entry",
            order_kind="trigger_limit", venue="deepcoin", status="submitting",
        ),
    )

    with session_factory() as session:
        queued = create_or_get_trigger_take_profit_convergence(
            session,
            venue="deepcoin",
            execution_order_leg_id=entry_leg_id,
            desired_take_profits=[{"price": "67300", "allocation_pct": "100"}],
            created_at=NOW,
        )
        session.commit()
        assert queued.status == "waiting_position"
        assert queued.desired_take_profits_json == '[{"allocation_pct":"100","price":"67300"}]'


def test_convergence_becomes_ready_only_after_its_exact_leg_is_verified(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.execution_bindings import (
        ExecutionBindingRecord,
        ExecutionOrderLegRecord,
        upsert_execution_binding,
        upsert_execution_order_leg,
    )
    from telegram_kol_research.models import ExecutionOrderLeg
    from telegram_kol_research.trigger_take_profit_convergence import (
        create_or_get_trigger_take_profit_convergence,
        mark_trigger_take_profit_convergence_ready,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol", chat_id=1, message_id=1, symbol="BTC", side="short",
            venue="deepcoin", margin_mode="cross", position_mode="split", status="active",
            pos_id="pos-7",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, leg_index=1, purpose="entry",
            order_kind="trigger_limit", venue="deepcoin", status="active", pos_id="pos-7",
        ),
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.attribution_status = "verified"
        queued = create_or_get_trigger_take_profit_convergence(
            session, venue="deepcoin", execution_order_leg_id=leg_id,
            desired_take_profits=[
                {"price": "64500", "allocation_pct": "50"},
                {"price": "63800", "allocation_pct": "30"},
                {"price": "63100", "allocation_pct": "20"},
            ], created_at=NOW,
        )
        mark_trigger_take_profit_convergence_ready(session, queued, ready_at=NOW)
        session.commit()

        assert queued.status == "ready"
        assert queued.pos_id == "pos-7"
