from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionTakeProfitOrder,
    RawMessage,
    RepairConfirmationToken,
    SourceMessageDeletionExit,
    TelegramSourceMessageEvent,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.source_message_deletion import record_source_message_deleted


NOW = datetime(2026, 8, 10, 1, 0, tzinfo=UTC)


def _snapshot(*, positions=None, pending=None, errors=None, complete=True):
    return SimpleNamespace(
        positions=list(positions or []),
        open_orders=[],
        pending_trigger_orders=list(pending or []),
        errors=dict(errors or {}),
        pending_tpsl_observations=[
            {
                "instrument_id": "BTC-USDT-SWAP",
                "complete": complete,
                "order_ids": [
                    str(row.get("ordId"))
                    for row in list(pending or [])
                    if row.get("ordId")
                ],
            }
        ],
    )


def _seed_dirty_non_strategy_deletion(session_factory) -> int:
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=10,
                message_id=20,
                text="not a strategy",
                archived_target_group=True,
            )
        )
        session.commit()
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=10,
        message_id=20,
        deleted_at=NOW,
    )
    with session_factory() as session:
        row = session.get(SourceMessageDeletionExit, deletion.exit_id)
        row.state = "cancelling_entries"
        row.claim_token = "stale-claim"
        row.claimed_at = NOW
        row.attempt_count = 999
        row.last_reason = None
        row.completed_at = None
        event = session.get(TelegramSourceMessageEvent, row.source_event_id)
        event.processing_status = "recorded"
        event.reason_code = None
        event.completed_at = None
        session.commit()
    return deletion.exit_id


def _seed_convergence(
    session_factory,
    *,
    chat_id: int,
    message_id: int,
    pos_id: str,
    status: str,
    binding_status: str,
    with_order: bool,
    error_json: str | None = None,
) -> tuple[int, int | None]:
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id=f"deepcoin:{chat_id}:{message_id}:BTC:short",
            kol_id=f"group:{chat_id}",
            chat_id=chat_id,
            message_id=message_id,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            pos_id=(pos_id if binding_status == "active" else None),
            status=binding_status,
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            order_id=f"entry-{pos_id}",
            pos_id=pos_id,
            venue="deepcoin",
            attribution_status="verified",
            status=("active" if binding_status == "active" else "manually_closed"),
        )
        session.add(leg)
        session.flush()
        convergence = TriggerTakeProfitConvergence(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            desired_take_profits_json='[{"price":"63000","allocation_pct":"100"}]',
            status=status,
            reason_code=(
                "convergence_submit_unknown" if status == "submit_unknown" else None
            ),
            pos_id=pos_id,
            error_json=error_json,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(convergence)
        session.flush()
        order_id = None
        if with_order:
            order = PositionTakeProfitOrder(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                trigger_take_profit_convergence_id=convergence.id,
                pos_id=pos_id,
                order_id=f"tp-{pos_id}",
                trigger_price="63000",
                size_text="1",
                status="active",
                evidence_json=json.dumps({"source": "test"}),
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(order)
            session.flush()
            order_id = order.id
        session.commit()
        return convergence.id, order_id


def test_plan_classifies_terminal_history_and_excludes_current_live_position(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    deletion_exit_id = _seed_dirty_non_strategy_deletion(session_factory)
    terminal_convergence_id, terminal_order_id = _seed_convergence(
        session_factory,
        chat_id=30,
        message_id=300,
        pos_id="pos-terminal",
        status="submitted",
        binding_status="closed",
        with_order=True,
    )
    rejected_convergence_id, _ = _seed_convergence(
        session_factory,
        chat_id=31,
        message_id=301,
        pos_id="pos-rejected",
        status="submit_unknown",
        binding_status="closed",
        with_order=False,
        error_json=json.dumps(
            {
                "type": "DeepcoinDefiniteRejection",
                "message": "price below lower limit",
            }
        ),
    )
    live_convergence_id, _ = _seed_convergence(
        session_factory,
        chat_id=32,
        message_id=302,
        pos_id="pos-live",
        status="submitted",
        binding_status="active",
        with_order=True,
    )
    snapshot = _snapshot(
        positions=[{"posId": "pos-live", "pos": "1"}],
        pending=[{"ordId": "tp-pos-live", "posId": "pos-live"}],
    )

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )

    assert plan.conflicts == ()
    assert plan.action_count == 3
    assert {(row.kind, row.target_id, row.reason_code) for row in plan.actions} == {
        (
            "source_deletion_exit",
            deletion_exit_id,
            "non_strategy_or_unlinked",
        ),
        (
            "take_profit_convergence",
            terminal_convergence_id,
            "convergence_position_terminal",
        ),
        (
            "take_profit_rejection",
            rejected_convergence_id,
            "convergence_submit_rejected_position_terminal",
        ),
    }
    terminal_action = next(
        row
        for row in plan.actions
        if row.kind == "take_profit_convergence"
        and row.target_id == terminal_convergence_id
    )
    assert terminal_action.related_ids == (terminal_order_id,)
    assert [(row.target_id, row.reason_code) for row in plan.exclusions] == [
        (live_convergence_id, "exact_position_or_order_still_live")
    ]
    assert len(plan.fingerprint) == 64
    assert len(plan.database_fingerprint) == 64
    assert len(plan.exchange_fingerprint) == 64
    assert len(plan.confirmation_token) == 16


def test_apply_requires_exact_gates_preserves_rows_and_is_single_use(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        HistoricalStateRepairRefused,
        apply_historical_state_repair_plan,
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    deletion_exit_id = _seed_dirty_non_strategy_deletion(session_factory)
    convergence_id, order_id = _seed_convergence(
        session_factory,
        chat_id=40,
        message_id=400,
        pos_id="pos-terminal",
        status="submitted",
        binding_status="closed",
        with_order=True,
    )
    snapshot = _snapshot()
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )

    with pytest.raises(HistoricalStateRepairRefused, match="fingerprint"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot=snapshot,
            expected_fingerprint="0" * 64,
            expected_action_count=plan.action_count,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )
    with pytest.raises(HistoricalStateRepairRefused, match="action count"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot=snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=plan.action_count + 1,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )
    with pytest.raises(HistoricalStateRepairRefused, match="confirmation token"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot=snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=plan.action_count,
            confirmation_token="wrong-token",
            applied_at=NOW,
        )

    result = apply_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        expected_fingerprint=plan.fingerprint,
        expected_action_count=plan.action_count,
        confirmation_token=plan.confirmation_token,
        applied_at=NOW,
    )

    assert result.applied_actions == 2
    assert result.fingerprint == plan.fingerprint
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion_exit_id)
        event = session.get(TelegramSourceMessageEvent, deletion_exit.source_event_id)
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.get(PositionTakeProfitOrder, order_id)
        assert deletion_exit.state == "succeeded"
        assert deletion_exit.claim_token is None
        assert deletion_exit.claimed_at is None
        assert event.processing_status == "ignored"
        assert convergence.status == "completed"
        assert order.status == "expired"
        assert session.query(SourceMessageDeletionExit).count() == 1
        assert session.query(TriggerTakeProfitConvergence).count() == 1
        assert session.query(PositionTakeProfitOrder).count() == 1
        audit = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "historical_state_convergence_repair")
            .one()
        )
        assert audit.notification_status == "not_needed"
        assert session.query(RepairConfirmationToken).count() == 1

    rerun = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )
    assert rerun.action_count == 0
    with pytest.raises(HistoricalStateRepairRefused, match="fingerprint"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot=snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=plan.action_count,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )


def test_plan_refuses_incomplete_exchange_snapshot(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_convergence(
        session_factory,
        chat_id=50,
        message_id=500,
        pos_id="pos-terminal",
        status="submitted",
        binding_status="closed",
        with_order=True,
    )

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(errors={"positions": "timeout"}, complete=False),
        planned_at=NOW,
    )

    assert plan.action_count == 0
    assert any(row.reason_code == "exchange_snapshot_incomplete" for row in plan.conflicts)
