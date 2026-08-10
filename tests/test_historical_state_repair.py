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
    SignalCandidate,
    SourceMessageDeletionExit,
    StrategyLifecycle,
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


def _seed_terminal_deletion_with_client_order(
    session_factory,
    *,
    binding_order_id: str | None = None,
) -> int:
    with session_factory() as session:
        raw = RawMessage(
            chat_id=11,
            message_id=21,
            text="BTC short",
            archived_target_group=True,
        )
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:11:21:BTC:short",
            kol_id="group:11",
            chat_id=11,
            message_id=21,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            order_id=binding_order_id,
            status="closed",
        )
        session.add_all([raw, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=11,
            message_id=21,
            symbol="BTC",
            side="short",
            lifecycle_status="exited",
            signal_at=NOW,
            execution_binding_id=binding.id,
        )
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            client_order_id="client-order-still-live",
            venue="deepcoin",
            attribution_status="verified",
            status="exchange_cancelled",
        )
        session.add_all([lifecycle, leg])
        session.commit()
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=11,
        message_id=21,
        deleted_at=NOW,
    )
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


def test_plan_refuses_terminal_strategy_with_no_exact_execution_identity(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    deletion_exit_id = _seed_terminal_deletion_with_client_order(session_factory)
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion_exit_id)
        binding = session.get(ExecutionBinding, deletion_exit.execution_binding_id)
        binding.order_id = None
        binding.client_order_id = None
        binding.pos_id = None
        leg = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=binding.id,
            purpose="entry",
        ).one()
        leg.order_id = None
        leg.client_order_id = None
        leg.pos_id = None
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert all(action.target_id != deletion_exit_id for action in plan.actions)
    assert any(
        finding.target_id == deletion_exit_id
        and finding.reason_code == "source_deletion_identity_not_terminal"
        for finding in plan.conflicts
    )


def test_plan_refuses_unlinked_deletion_when_source_binding_exists(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    deletion_exit_id = _seed_dirty_non_strategy_deletion(session_factory)
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                strategy_instance_id="deepcoin:10:20:BTC:long",
                kol_id="group:10",
                chat_id=10,
                message_id=20,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                order_id="live-source-order",
                status="active",
            )
        )
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert all(action.target_id != deletion_exit_id for action in plan.actions)
    assert any(
        finding.target_id == deletion_exit_id
        and finding.reason_code == "source_deletion_identity_not_terminal"
        for finding in plan.conflicts
    )


def test_plan_refuses_no_execution_when_lifecycle_still_references_binding(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    deletion_exit_id = _seed_terminal_deletion_with_client_order(session_factory)
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion_exit_id)
        binding = session.get(ExecutionBinding, deletion_exit.execution_binding_id)
        deletion_exit.execution_binding_id = None
        binding.chat_id = 999
        binding.message_id = 999
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert all(action.target_id != deletion_exit_id for action in plan.actions)
    assert any(
        finding.target_id == deletion_exit_id
        and finding.reason_code == "source_deletion_identity_not_terminal"
        for finding in plan.conflicts
    )


def test_plan_uses_source_event_raw_message_candidate_evidence(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    deletion_exit_id = _seed_dirty_non_strategy_deletion(session_factory)
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion_exit_id)
        event = session.get(TelegramSourceMessageEvent, deletion_exit.source_event_id)
        deletion_exit.raw_message_id = None
        session.add(
            SignalCandidate(
                raw_message_id=event.raw_message_id,
                symbol="BTC",
                side="long",
                event_type="entry_signal",
                parse_source="test",
                confidence=1.0,
            )
        )
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert all(action.target_id != deletion_exit_id for action in plan.actions)
    assert any(
        finding.target_id == deletion_exit_id
        and finding.reason_code == "source_deletion_identity_not_terminal"
        for finding in plan.conflicts
    )


def test_plan_refuses_conflicted_rejection_without_definite_exchange_evidence(
    tmp_path,
):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id, _ = _seed_convergence(
        session_factory,
        chat_id=33,
        message_id=303,
        pos_id="pos-unproven-rejection",
        status="conflicted",
        binding_status="closed",
        with_order=False,
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.reason_code = "convergence_submit_rejected"
        convergence.error_json = None
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert all(action.target_id != convergence_id for action in plan.actions)
    assert any(
        finding.target_id == convergence_id
        and finding.reason_code == "take_profit_state_not_repairable"
        for finding in plan.conflicts
    )


def test_plan_reports_blank_take_profit_position_identity_as_conflict(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id, _ = _seed_convergence(
        session_factory,
        chat_id=34,
        message_id=304,
        pos_id="pos-to-blank",
        status="submitted",
        binding_status="closed",
        with_order=True,
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.pos_id = " "
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        leg.pos_id = " "
        order = session.query(PositionTakeProfitOrder).filter_by(
            trigger_take_profit_convergence_id=convergence_id
        ).one()
        order.pos_id = " "
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert all(action.target_id != convergence_id for action in plan.actions)
    assert any(
        finding.target_id == convergence_id
        and finding.reason_code == "take_profit_position_identity_missing"
        for finding in plan.conflicts
    )


def test_plan_normalizes_local_take_profit_order_identity_before_live_probe(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id, _ = _seed_convergence(
        session_factory,
        chat_id=35,
        message_id=305,
        pos_id="pos-order-live",
        status="submitted",
        binding_status="closed",
        with_order=True,
    )
    with session_factory() as session:
        order = session.query(PositionTakeProfitOrder).filter_by(
            trigger_take_profit_convergence_id=convergence_id
        ).one()
        order.order_id = " tp-pos-order-live "
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(pending=[{"ordId": "tp-pos-order-live"}]),
        planned_at=NOW,
    )

    assert all(action.target_id != convergence_id for action in plan.actions)
    assert any(
        finding.target_id == convergence_id
        and finding.reason_code == "exact_position_or_order_still_live"
        for finding in plan.exclusions
    )


def test_plan_reports_blank_take_profit_order_identity_as_conflict(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id, _ = _seed_convergence(
        session_factory,
        chat_id=36,
        message_id=306,
        pos_id="pos-blank-order",
        status="submitted",
        binding_status="closed",
        with_order=True,
    )
    with session_factory() as session:
        order = session.query(PositionTakeProfitOrder).filter_by(
            trigger_take_profit_convergence_id=convergence_id
        ).one()
        order.order_id = " "
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert all(action.target_id != convergence_id for action in plan.actions)
    assert any(
        finding.target_id == convergence_id
        and finding.reason_code == "take_profit_state_not_repairable"
        for finding in plan.conflicts
    )


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
            snapshot_loader=lambda: snapshot,
            expected_fingerprint="0" * 64,
            expected_action_count=plan.action_count,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )
    with pytest.raises(HistoricalStateRepairRefused, match="action count"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot_loader=lambda: snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=plan.action_count + 1,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )
    with pytest.raises(HistoricalStateRepairRefused, match="confirmation token"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot_loader=lambda: snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=plan.action_count,
            confirmation_token="wrong-token",
            applied_at=NOW,
        )

    result = apply_historical_state_repair_plan(
        session_factory,
        snapshot_loader=lambda: snapshot,
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
            snapshot_loader=lambda: snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=plan.action_count,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )


def test_apply_reloads_exchange_snapshot_and_refuses_new_live_position(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        HistoricalStateRepairRefused,
        apply_historical_state_repair_plan,
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_convergence(
        session_factory,
        chat_id=45,
        message_id=450,
        pos_id="pos-terminal",
        status="submitted",
        binding_status="closed",
        with_order=True,
    )
    dry_snapshot = _snapshot()
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=dry_snapshot,
        planned_at=NOW,
    )
    changed_snapshot = _snapshot(
        positions=[{"posId": "pos-terminal", "pos": "-1"}],
    )
    loads = []

    def reload_snapshot():
        loads.append("loaded")
        return changed_snapshot

    with pytest.raises(HistoricalStateRepairRefused, match="fingerprint"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot_loader=reload_snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=plan.action_count,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )

    assert loads == ["loaded"]
    with session_factory() as session:
        assert session.query(TriggerTakeProfitConvergence).one().status == "submitted"
        assert session.query(PositionTakeProfitOrder).one().status == "active"


def test_apply_refuses_convergence_venue_change_after_fresh_plan(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.historical_state_repair as repair_module
    from telegram_kol_research.historical_state_repair import (
        HistoricalStateRepairRefused,
        apply_historical_state_repair_plan,
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id, _ = _seed_convergence(
        session_factory,
        chat_id=47,
        message_id=470,
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
    original_builder = repair_module.build_historical_state_repair_plan

    def build_then_mutate(*args, **kwargs):
        fresh_plan = original_builder(*args, **kwargs)
        with session_factory() as session:
            convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
            convergence.venue = "binance"
            session.commit()
        return fresh_plan

    monkeypatch.setattr(
        repair_module,
        "build_historical_state_repair_plan",
        build_then_mutate,
    )

    with pytest.raises(HistoricalStateRepairRefused, match="venue"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot_loader=lambda: snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=plan.action_count,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        order = session.query(PositionTakeProfitOrder).one()
        assert convergence.status == "submitted"
        assert order.status == "active"


def test_apply_refuses_source_binding_inserted_after_fresh_plan(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.historical_state_repair as repair_module
    from telegram_kol_research.historical_state_repair import (
        HistoricalStateRepairRefused,
        apply_historical_state_repair_plan,
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    deletion_exit_id = _seed_dirty_non_strategy_deletion(session_factory)
    snapshot = _snapshot()
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )
    original_builder = repair_module.build_historical_state_repair_plan

    def build_then_insert_binding(*args, **kwargs):
        fresh_plan = original_builder(*args, **kwargs)
        with session_factory() as session:
            session.add(
                ExecutionBinding(
                    strategy_instance_id="deepcoin:10:20:BTC:long",
                    kol_id="group:10",
                    chat_id=10,
                    message_id=20,
                    symbol="BTC",
                    side="long",
                    venue="deepcoin",
                    order_id="late-live-source-order",
                    status="active",
                )
            )
            session.commit()
        return fresh_plan

    monkeypatch.setattr(
        repair_module,
        "build_historical_state_repair_plan",
        build_then_insert_binding,
    )

    with pytest.raises(HistoricalStateRepairRefused, match="source_binding_ids"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot_loader=lambda: snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=plan.action_count,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )

    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion_exit_id)
        assert deletion_exit.state == "cancelling_entries"


@pytest.mark.parametrize(
    "live_position",
    [
        {"posId": "pos-terminal"},
        {"posId": "pos-terminal", "pos": ""},
        {"posId": "pos-terminal", "size": ""},
        {"posId": "pos-terminal", "sz": ""},
        {"posId": "pos-terminal", "pos": "NaN"},
        {"posId": "pos-terminal", "pos": "0", "size": "1"},
        {"posId": "pos-terminal", "pos": "0", "positionSize": "1"},
        {"posId": "other-position", "positionId": "pos-terminal", "pos": "1"},
    ],
)
def test_plan_fails_closed_when_exact_position_size_is_unknown(
    tmp_path,
    live_position,
):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id, _ = _seed_convergence(
        session_factory,
        chat_id=46,
        message_id=460,
        pos_id="pos-terminal",
        status="submitted",
        binding_status="closed",
        with_order=True,
    )

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(positions=[live_position]),
        planned_at=NOW,
    )

    assert all(action.target_id != convergence_id for action in plan.actions)


@pytest.mark.parametrize("ambiguous_kind", ["position", "order"])
def test_plan_refuses_unidentifiable_live_exchange_rows(tmp_path, ambiguous_kind):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id, _ = _seed_convergence(
        session_factory,
        chat_id=48,
        message_id=480,
        pos_id="pos-terminal",
        status="submitted",
        binding_status="closed",
        with_order=True,
    )
    snapshot = (
        _snapshot(positions=[{"instId": "BTC-USDT-SWAP", "pos": "1"}])
        if ambiguous_kind == "position"
        else _snapshot(pending=[{"instId": "BTC-USDT-SWAP", "sz": "1"}])
    )

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )

    assert all(action.target_id != convergence_id for action in plan.actions)
    assert any(
        finding.kind == "snapshot"
        and finding.reason_code == "exchange_snapshot_identity_incomplete"
        for finding in plan.conflicts
    )


def test_exchange_fingerprint_binds_all_position_identity_and_size_aliases(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    first = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(
            positions=[{"positionId": "pos-a", "positionSize": "1"}]
        ),
        planned_at=NOW,
    )
    changed_identity = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(
            positions=[{"positionId": "pos-b", "positionSize": "1"}]
        ),
        planned_at=NOW,
    )
    changed_size = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(
            positions=[{"positionId": "pos-a", "positionSize": "2"}]
        ),
        planned_at=NOW,
    )

    assert first.exchange_fingerprint != changed_identity.exchange_fingerprint
    assert first.exchange_fingerprint != changed_size.exchange_fingerprint


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


def test_plan_excludes_terminal_deletion_when_deepcoin_client_order_is_live(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    deletion_exit_id = _seed_terminal_deletion_with_client_order(session_factory)

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(
            pending=[
                {
                    "algoId": "exchange-algo-id",
                    "clOrdId": "client-order-still-live",
                }
            ]
        ),
        planned_at=NOW,
    )

    assert not any(
        row.kind == "source_deletion_exit" and row.target_id == deletion_exit_id
        for row in plan.actions
    )
    assert any(
        row.kind == "source_deletion_exit"
        and row.target_id == deletion_exit_id
        and row.reason_code == "exact_position_or_order_still_live"
        for row in plan.exclusions
    )


def test_plan_conflicts_when_terminal_binding_identity_is_missing_from_legs(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    deletion_exit_id = _seed_terminal_deletion_with_client_order(
        session_factory,
        binding_order_id="binding-order-not-in-leg",
    )

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert not any(
        row.kind == "source_deletion_exit" and row.target_id == deletion_exit_id
        for row in plan.actions
    )
    assert any(
        row.kind == "source_deletion_exit"
        and row.target_id == deletion_exit_id
        and row.reason_code == "source_deletion_identity_not_terminal"
        for row in plan.conflicts
    )


def test_plan_conflicts_when_deletion_frozen_strategy_differs_from_binding(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    deletion_exit_id = _seed_terminal_deletion_with_client_order(session_factory)
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion_exit_id)
        deletion_exit.strategy_instance_id = "deepcoin:other:strategy:BTC:short"
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert not any(row.target_id == deletion_exit_id for row in plan.actions)
    assert any(
        row.target_id == deletion_exit_id
        and row.reason_code == "source_deletion_identity_not_terminal"
        for row in plan.conflicts
    )
