import json
from datetime import datetime

import pytest

from telegram_kol_research.break_even_convergence_planner import (
    BreakEvenConvergencePlanningError,
    plan_or_adopt_break_even_convergence,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionReconciliationObservation,
    StrategyBreakEvenConvergence,
    StrategyBreakEvenConvergenceLeg,
    StrategyLifecycle,
)


def _seed_strategy(session_factory, *, live_count=1, with_deferred=True):
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:1:2:BTC:short",
            kol_id="group:1",
            chat_id=1,
            message_id=2,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="active",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=1,
            message_id=2,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 8, 2, 6, 0),
            entered_at=datetime(2026, 8, 2, 6, 1),
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        for index in range(1, live_count + 1):
            pos_id = f"pos-{index}"
            leg = ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=index,
                purpose="entry",
                order_kind="market" if index == 1 else "trigger_limit",
                order_id=pos_id,
                pos_id=pos_id,
                venue="deepcoin",
                attribution_status="verified",
                status="active",
            )
            session.add(leg)
            session.flush()
            session.add(PositionReconciliationObservation(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id=pos_id,
                instrument_id="BTC-USDT-SWAP",
                side="short",
                size_text=str(index * 5),
                avg_entry_price=str(63000 + index * 100),
                pending_tpsl_json="[]",
                snapshot_complete=True,
                snapshot_fingerprint=(str(index) * 64)[:64],
                observed_at=datetime(2026, 8, 2, 7, index),
            ))
        if with_deferred:
            session.add(ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=live_count + 1,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="deferred-1",
                client_order_id="deferred-client-1",
                venue="deepcoin",
                attribution_status="unassigned",
                status="pending",
            ))
        session.commit()
        return binding.strategy_instance_id


def test_plans_all_verified_live_legs_and_deferred_entries(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    strategy_id = _seed_strategy(session_factory, live_count=2)

    result = plan_or_adopt_break_even_convergence(
        session_factory,
        trigger_type="tp1_fill",
        trigger_identity="tp-1",
        trigger_evidence={"evidence_tier": "exact_order_terminal"},
        strategy_instance_id=strategy_id,
        planned_at=datetime(2026, 8, 2, 8, 0),
        execution_mode="shadow",
    )

    assert result.status == "planned"
    assert result.execution_mode == "shadow"
    assert {leg.pos_id for leg in result.legs} == {"pos-1", "pos-2"}
    assert {leg.avg_entry_price for leg in result.legs} == {"63100", "63200"}
    assert json.loads(result.target_snapshot_json)["deferred_entry_leg_ids"]


def test_repeated_tp_trigger_adopts_same_convergence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    strategy_id = _seed_strategy(session_factory)
    kwargs = dict(
        trigger_type="tp1_fill",
        trigger_identity="tp-1",
        trigger_evidence={"evidence_tier": "exact_order_terminal"},
        strategy_instance_id=strategy_id,
        planned_at=datetime(2026, 8, 2, 8, 0),
        execution_mode="shadow",
    )

    first = plan_or_adopt_break_even_convergence(session_factory, **kwargs)
    second = plan_or_adopt_break_even_convergence(session_factory, **kwargs)

    assert first.id == second.id
    with session_factory() as session:
        assert session.query(StrategyBreakEvenConvergence).count() == 1
        assert session.query(StrategyBreakEvenConvergenceLeg).count() == 1


def test_disabled_mode_records_blocked_task_without_executable_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    strategy_id = _seed_strategy(session_factory)

    result = plan_or_adopt_break_even_convergence(
        session_factory,
        trigger_type="tp1_fill",
        trigger_identity="tp-1",
        trigger_evidence={"evidence_tier": "exact_order_terminal"},
        strategy_instance_id=strategy_id,
        planned_at=datetime(2026, 8, 2, 8, 0),
        execution_mode="disabled",
    )

    assert result.status == "blocked"
    assert result.reason_code == "automatic_break_even_disabled"
    assert result.legs == ()


def test_planner_rejects_unverified_or_incomplete_live_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    strategy_id = _seed_strategy(session_factory)
    with session_factory() as session:
        observation = session.query(PositionReconciliationObservation).one()
        observation.snapshot_complete = False
        session.commit()

    with pytest.raises(
        BreakEvenConvergencePlanningError,
        match="break_even_live_observation_incomplete",
    ):
        plan_or_adopt_break_even_convergence(
            session_factory,
            trigger_type="tp1_fill",
            trigger_identity="tp-1",
            trigger_evidence={"evidence_tier": "exact_order_terminal"},
            strategy_instance_id=strategy_id,
            planned_at=datetime(2026, 8, 2, 8, 0),
            execution_mode="shadow",
        )
