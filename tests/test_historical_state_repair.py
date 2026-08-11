from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    EntryStrategyAssembly,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    InstructionExecutionContract,
    MessageInstructionItem,
    PositionAttributionAudit,
    PositionMutationIntent,
    PositionTakeProfitOrder,
    RawMessage,
    RepairConfirmationToken,
    SignalCandidate,
    SourceMessageDeletionExit,
    StrategyLifecycle,
    TelegramSourceMessageEvent,
    TradeSignal,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.source_message_deletion import record_source_message_deleted


NOW = datetime(2026, 8, 10, 1, 0, tzinfo=UTC)


def _snapshot(
    *,
    positions=None,
    pending=None,
    errors=None,
    complete=True,
    position_history=None,
):
    return SimpleNamespace(
        positions=list(positions or []),
        open_orders=[],
        pending_trigger_orders=list(pending or []),
        position_history=list(position_history or []),
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


def _seed_restorable_authority_candidate(
    session_factory,
    *,
    pos_id: str = "pos-restorable",
) -> int:
    convergence_id, _ = _seed_convergence(
        session_factory,
        chat_id=39,
        message_id=309,
        pos_id=pos_id,
        status="submitted",
        binding_status="closed",
        with_order=True,
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        leg.attribution_status = "attribution_conflict"
        session.add(
            PositionAttributionAudit(
                execution_binding_id=leg.execution_binding_id,
                execution_order_leg_id=leg.id,
                venue="deepcoin",
                pos_id=leg.pos_id,
                event_type="ownership_verified",
                prior_state="unassigned",
                new_state="verified",
                fingerprint="b" * 64,
                evidence_json=json.dumps(
                    {
                        "policy_version": 2,
                        "evidence_type": "direct_order_position_id",
                    }
                ),
                created_at=NOW,
            )
        )
        session.commit()
    return convergence_id


class _ReadOnlyHistoryClient:
    def __init__(self, history_response):
        self.history_response = history_response
        self.history_calls = []

    def list_positions(self):
        return []

    def list_open_orders(self):
        return []

    def list_trigger_orders_pending(self, *, inst_id):
        return []

    def list_order_history(self, *, inst_id):
        return []

    def list_trade_fills(self, *, inst_id):
        return []

    def list_trigger_order_history(self, *, inst_id):
        return []

    def list_position_history(self, *, inst_id, pos_id):
        self.history_calls.append((inst_id, pos_id))
        if isinstance(self.history_response, Exception):
            raise self.history_response
        return self.history_response

    def submit_order(self, *_args, **_kwargs):
        raise AssertionError("historical repair snapshot must remain read-only")

    cancel_order = submit_order
    close_position = submit_order


def _seed_proven_attribution_repair_candidate(
    session_factory,
    *,
    pos_id: str = "pos-proven-closed",
) -> dict[str, object]:
    strategy_instance_id = "deepcoin:40:400:BTC:short"
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id=strategy_instance_id,
            kol_id="group:40",
            chat_id=40,
            message_id=400,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            pos_id=pos_id,
            status="closed",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=40,
            message_id=400,
            symbol="BTC",
            side="short",
            lifecycle_status="exited",
            exit_reason="manual",
            signal_at=NOW - timedelta(days=2),
            entered_at=NOW - timedelta(days=2),
            exited_at=NOW - timedelta(days=1),
            execution_binding_id=binding.id,
        )
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="entry-proven-closed",
            pos_id=pos_id,
            venue="deepcoin",
            attribution_status="attribution_conflict",
            status="manually_closed",
        )
        session.add_all([lifecycle, leg])
        session.flush()
        authority_audits = []
        for index, evidence_type in enumerate(
            ("trade_fill", "regular_order"), start=1
        ):
            audit = PositionAttributionAudit(
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                venue="deepcoin",
                pos_id=pos_id,
                event_type="ownership_verified",
                prior_state="unassigned" if index == 1 else "verified",
                new_state="verified",
                fingerprint=str(index) * 64,
                evidence_json=json.dumps(
                    {
                        "policy_version": 2,
                        "evidence_type": "direct_order_position_id",
                        "source": evidence_type,
                    }
                ),
                created_at=NOW - timedelta(days=2, minutes=-index),
            )
            session.add(audit)
            authority_audits.append(audit)
        conflict_audit = PositionAttributionAudit(
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            venue="deepcoin",
            pos_id=pos_id,
            event_type="attribution_conflict",
            prior_state="evidence_unavailable",
            new_state="attribution_conflict",
            fingerprint="3" * 64,
            evidence_json=json.dumps(
                {
                    "policy_version": 2,
                    "candidate_leg_ids": [],
                    "candidate_position_ids": [],
                }
            ),
            created_at=NOW - timedelta(hours=12),
        )
        mutation = PositionMutationIntent(
            idempotency_key="close-pos-proven-closed",
            venue="deepcoin",
            operation="close_position",
            strategy_instance_id=strategy_instance_id,
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            pos_id=pos_id,
            authority_fingerprint="4" * 64,
            request_fingerprint="5" * 64,
            status="confirmed",
            request_json=json.dumps({"posId": pos_id}),
            response_json=json.dumps({"posId": pos_id, "status": "success"}),
            reserved_at=NOW - timedelta(days=1, minutes=2),
            submitted_at=NOW - timedelta(days=1, minutes=1),
            confirmed_at=NOW - timedelta(days=1),
        )
        reservation = BoundPositionCloseReservation(
            pos_id=pos_id,
            execution_binding_id=binding.id,
            status="confirmed",
            created_at=NOW - timedelta(days=1, minutes=2),
            updated_at=NOW - timedelta(days=1),
        )
        session.add_all([conflict_audit, mutation, reservation])
        convergence = TriggerTakeProfitConvergence(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            desired_take_profits_json=json.dumps(
                [
                    {"price": "63000", "allocation_pct": "40"},
                    {"price": "62000", "allocation_pct": "30"},
                    {"price": "61000", "allocation_pct": "30"},
                ]
            ),
            status="submitted",
            pos_id=pos_id,
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2),
        )
        session.add(convergence)
        session.flush()
        orders = []
        for index, price in enumerate(("63000", "62000", "61000"), start=1):
            order = PositionTakeProfitOrder(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                trigger_take_profit_convergence_id=convergence.id,
                pos_id=pos_id,
                order_id=f"tp-proven-{index}",
                trigger_price=price,
                size_text="1",
                status="active",
                evidence_json=json.dumps({"mutation_intent_id": index}),
                created_at=NOW - timedelta(days=2),
                updated_at=NOW - timedelta(days=2),
            )
            session.add(order)
            orders.append(order)
        session.commit()
        return {
            "binding_id": int(binding.id),
            "lifecycle_id": int(lifecycle.id),
            "leg_id": int(leg.id),
            "convergence_id": int(convergence.id),
            "order_ids": tuple(int(row.id) for row in orders),
            "authority_audit_ids": tuple(int(row.id) for row in authority_audits),
            "conflict_audit_id": int(conflict_audit.id),
            "mutation_id": int(mutation.id),
            "reservation_id": int(reservation.id),
            "pos_id": pos_id,
            "strategy_instance_id": strategy_instance_id,
        }


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


def test_plan_treats_symbol_not_allowed_skip_as_no_exchange_execution(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    deletion_exit_id = _seed_dirty_non_strategy_deletion(session_factory)
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion_exit_id)
        event = session.get(TelegramSourceMessageEvent, deletion_exit.source_event_id)
        lifecycle = StrategyLifecycle(
            chat_id=event.chat_id,
            message_id=event.message_id,
            symbol="ZEC",
            side="short",
            lifecycle_status="expired",
            signal_at=NOW,
        )
        session.add(lifecycle)
        session.flush()
        deletion_exit.target_lifecycle_id = lifecycle.id
        session.add_all(
            [
                SignalCandidate(
                    raw_message_id=event.raw_message_id,
                    symbol="ZEC",
                    side="short",
                    event_type="entry_signal",
                    parse_source="test",
                    confidence=1.0,
                ),
                ExecutionEvent(
                    chat_id=event.chat_id,
                    message_id=event.message_id,
                    action="auto_trade_skipped",
                    status="skipped",
                    reason="symbol_not_allowed",
                    request_json='{"symbol":"ZEC"}',
                    created_at=NOW,
                ),
            ]
        )
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert any(
        action.target_id == deletion_exit_id
        and action.reason_code == "strategy_terminal_without_execution"
        for action in plan.actions
    )
    assert all(conflict.target_id != deletion_exit_id for conflict in plan.conflicts)


def test_plan_leaves_unrelated_conflicted_take_profit_state_out_of_scope(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id, _ = _seed_convergence(
        session_factory,
        chat_id=37,
        message_id=307,
        pos_id="pos-unrelated-conflict",
        status="conflicted",
        binding_status="closed",
        with_order=True,
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        convergence.reason_code = "convergence_partial_position_unexplained"
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert all(action.target_id != convergence_id for action in plan.actions)
    assert all(conflict.target_id != convergence_id for conflict in plan.conflicts)
    assert all(exclusion.target_id != convergence_id for exclusion in plan.exclusions)


def test_plan_excludes_submitted_take_profit_with_unverified_identity(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    convergence_id, _ = _seed_convergence(
        session_factory,
        chat_id=38,
        message_id=308,
        pos_id="pos-unverified-history",
        status="submitted",
        binding_status="closed",
        with_order=True,
    )
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        leg.attribution_status = "attribution_conflict"
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert all(action.target_id != convergence_id for action in plan.actions)
    assert all(conflict.target_id != convergence_id for conflict in plan.conflicts)
    assert any(
        exclusion.target_id == convergence_id
        and exclusion.reason_code == "take_profit_attribution_repair_not_proven"
        for exclusion in plan.exclusions
    )


def test_historical_repair_snapshot_loads_exact_history_for_restorable_authority(
    tmp_path,
):
    from telegram_kol_research.historical_state_repair import (
        load_historical_state_repair_snapshot_read_only,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_restorable_authority_candidate(session_factory)
    history_row = {
        "instId": "BTC-USDT-SWAP",
        "posId": "pos-restorable",
        "posSide": "short",
        "pos": "1",
        "closePos": "1",
    }
    client = _ReadOnlyHistoryClient([history_row])
    snapshot = load_historical_state_repair_snapshot_read_only(
        session_factory,
        client=client,
    )

    assert client.history_calls == [("BTC-USDT-SWAP", "pos-restorable")]
    assert snapshot.position_history == [history_row]
    assert snapshot.errors == {}


@pytest.mark.parametrize(
    ("history_response", "expected_error"),
    [
        (RuntimeError("history unavailable"), "history unavailable"),
        ({"posId": "pos-restorable"}, "invalid list response schema"),
        (["not-a-row"], "invalid list response schema"),
        (
            [{"posId": "different-position", "pos": "1", "closePos": "1"}],
            "position history response identity mismatch",
        ),
        (
            [{"instId": "BTC-USDT-SWAP", "pos": "1", "closePos": "1"}],
            "position history response identity mismatch",
        ),
    ],
)
def test_historical_repair_snapshot_fails_closed_on_invalid_exact_history(
    tmp_path,
    history_response,
    expected_error,
):
    from telegram_kol_research.historical_state_repair import (
        load_historical_state_repair_snapshot_read_only,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_restorable_authority_candidate(session_factory)
    snapshot = load_historical_state_repair_snapshot_read_only(
        session_factory,
        client=_ReadOnlyHistoryClient(history_response),
    )

    source = "position_history:BTC-USDT-SWAP:pos-restorable"
    assert expected_error in snapshot.errors[source]
    assert snapshot.position_history == []


def test_historical_repair_snapshot_deduplicates_exact_history_rows(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        load_historical_state_repair_snapshot_read_only,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_restorable_authority_candidate(session_factory)
    row = {
        "instId": "BTC-USDT-SWAP",
        "posId": "pos-restorable",
        "posSide": "short",
        "pos": "5",
        "closePos": "5",
    }
    snapshot = load_historical_state_repair_snapshot_read_only(
        session_factory,
        client=_ReadOnlyHistoryClient([row, dict(row)]),
    )

    assert snapshot.position_history == [row]
    assert snapshot.errors == {}


def test_plan_builds_one_proven_terminal_attribution_repair(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    snapshot = _snapshot(
        position_history=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": seeded["pos_id"],
                "posSide": "short",
                "pos": "5",
                "closePos": "5",
            }
        ]
    )

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )

    assert [
        (row.kind, row.target_id, row.reason_code) for row in plan.actions
    ] == [
        (
            "take_profit_attribution_repair",
            seeded["convergence_id"],
            "convergence_position_terminal_prior_authority_restored",
        )
    ]
    assert plan.conflicts == ()


def test_plan_accepts_terminal_binding_with_canonical_cleared_position_identity(
    tmp_path,
):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    with session_factory() as session:
        binding = session.get(ExecutionBinding, seeded["binding_id"])
        binding.pos_id = None
        binding.last_exchange_status = "entry_legs_terminal"
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(
            position_history=[
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": seeded["pos_id"],
                    "posSide": "short",
                    "pos": "5",
                    "closePos": "5",
                }
            ]
        ),
        planned_at=NOW,
    )

    assert [
        (row.kind, row.target_id) for row in plan.actions
    ] == [("take_profit_attribution_repair", seeded["convergence_id"])]
    assert plan.conflicts == ()


def test_plan_accepts_production_shaped_confirmed_close_intent(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    with session_factory() as session:
        mutation = session.get(PositionMutationIntent, seeded["mutation_id"])
        mutation.order_id = "close-order-1"
        mutation.request_json = json.dumps(
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "ordType": "market",
                "sz": "5",
                "closePosId": seeded["pos_id"],
            }
        )
        mutation.response_json = json.dumps(
            {
                "code": "0",
                "data": {"ordId": "close-order-1"},
                "reconciled_from_exchange_readback": True,
            }
        )
        session.commit()
    snapshot = _snapshot(
        position_history=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": seeded["pos_id"],
                "posSide": "short",
                "pos": "5",
                "closePos": "5",
            }
        ]
    )

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )

    assert [
        (row.kind, row.target_id) for row in plan.actions
    ] == [("take_profit_attribution_repair", seeded["convergence_id"])]
    assert plan.conflicts == ()


@pytest.mark.parametrize(
    "position_id_key",
    ["PositionID", "positionId", "position_id"],
)
def test_plan_accepts_supported_exact_history_position_aliases(
    tmp_path,
    position_id_key,
):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    history_row = {
        "instId": "BTC-USDT-SWAP",
        position_id_key: seeded["pos_id"],
        "posSide": "short",
        "pos": "5",
        "closePos": "5",
    }

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(position_history=[history_row]),
        planned_at=NOW,
    )

    assert [row.kind for row in plan.actions] == [
        "take_profit_attribution_repair"
    ]


@pytest.mark.parametrize(
    ("mutation", "expected_failure"),
    [
        ("authority_missing", "policy_v2_authority_missing"),
        ("authority_wrong_policy", "policy_v2_authority_missing"),
        ("authority_wrong_leg", "policy_v2_authority_missing"),
        ("authority_wrong_venue", "policy_v2_authority_missing"),
        ("authority_wrong_pos", "policy_v2_authority_missing"),
        ("later_true_conflict", "later_competing_attribution_evidence"),
        ("later_unexplained_conflict", "later_competing_attribution_evidence"),
        ("close_mutation_unconfirmed", "confirmed_close_mutation_missing_or_ambiguous"),
        ("close_mutation_wrong_leg", "confirmed_close_mutation_missing_or_ambiguous"),
        ("close_mutation_wrong_strategy", "confirmed_close_mutation_missing_or_ambiguous"),
        ("close_mutation_wrong_pos", "confirmed_close_mutation_missing_or_ambiguous"),
        ("close_mutation_order_id", "close_mutation_payload_identity_mismatch"),
        (
            "close_mutation_unidentified_response",
            "close_mutation_payload_identity_mismatch",
        ),
        ("reservation_unconfirmed", "confirmed_close_reservation_missing_or_mismatched"),
        ("reservation_wrong_binding", "confirmed_close_reservation_missing_or_mismatched"),
        ("competing_leg", "position_owned_by_other_leg"),
        ("competing_leg_case_variant", "position_owned_by_other_leg"),
        ("competing_leg_whitespace_venue", "position_owned_by_other_leg"),
        ("lifecycle_active", "lifecycle_not_uniquely_terminal"),
        ("binding_active", "strategy_or_ledger_identity_mismatch"),
        (
            "binding_cleared_without_terminal_evidence",
            "strategy_or_ledger_identity_mismatch",
        ),
        ("leg_active", "strategy_or_ledger_identity_mismatch"),
        ("order_position_mismatch", "strategy_or_ledger_identity_mismatch"),
    ],
)
def test_plan_fails_closed_when_local_attribution_repair_proof_changes(
    tmp_path,
    mutation,
    expected_failure,
):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    with session_factory() as session:
        if mutation.startswith("authority_"):
            audits = (
                session.query(PositionAttributionAudit)
                .filter(PositionAttributionAudit.event_type == "ownership_verified")
                .all()
            )
            if mutation == "authority_missing":
                for row in audits:
                    session.delete(row)
            elif mutation == "authority_wrong_policy":
                for row in audits:
                    row.evidence_json = json.dumps({"policy_version": 1})
            elif mutation == "authority_wrong_leg":
                for row in audits:
                    row.execution_order_leg_id = int(seeded["leg_id"]) + 999
            elif mutation == "authority_wrong_venue":
                for row in audits:
                    row.venue = "binance"
            elif mutation == "authority_wrong_pos":
                for row in audits:
                    row.pos_id = "different-position"
        elif mutation in {"later_true_conflict", "later_unexplained_conflict"}:
            row = session.get(
                PositionAttributionAudit,
                seeded["conflict_audit_id"],
            )
            row.evidence_json = (
                json.dumps(
                    {"candidate_leg_ids": [999], "candidate_position_ids": []}
                )
                if mutation == "later_true_conflict"
                else "{}"
            )
        elif mutation.startswith("close_mutation_"):
            row = session.get(PositionMutationIntent, seeded["mutation_id"])
            if mutation == "close_mutation_unconfirmed":
                row.status = "submitted"
            elif mutation == "close_mutation_wrong_leg":
                row.execution_order_leg_id = int(seeded["leg_id"]) + 999
            elif mutation == "close_mutation_wrong_strategy":
                row.strategy_instance_id = "different-strategy"
            elif mutation == "close_mutation_order_id":
                row.order_id = "different-close-order"
                row.response_json = json.dumps(
                    {
                        "code": "0",
                        "data": {"ordId": "original-close-order"},
                        "posId": seeded["pos_id"],
                    }
                )
            elif mutation == "close_mutation_unidentified_response":
                row.response_json = json.dumps({"code": "0"})
            else:
                row.pos_id = "different-position"
        elif mutation.startswith("reservation_"):
            row = session.get(
                BoundPositionCloseReservation,
                seeded["reservation_id"],
            )
            if mutation == "reservation_unconfirmed":
                row.status = "submitted"
            else:
                row.execution_binding_id = int(seeded["binding_id"]) + 999
        elif mutation in {
            "competing_leg",
            "competing_leg_case_variant",
            "competing_leg_whitespace_venue",
        }:
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=seeded["binding_id"],
                    strategy_instance_id=seeded["strategy_instance_id"],
                    leg_index=2,
                    purpose="entry",
                    order_kind="market",
                    pos_id=f'{seeded["pos_id"]},legacy-other',
                    venue=(
                        "Deepcoin"
                        if mutation == "competing_leg_case_variant"
                        else (
                            " Deepcoin "
                            if mutation == "competing_leg_whitespace_venue"
                            else "deepcoin"
                        )
                    ),
                    attribution_status="verified",
                    status="manually_closed",
                )
            )
        elif mutation == "lifecycle_active":
            session.get(
                StrategyLifecycle, seeded["lifecycle_id"]
            ).lifecycle_status = "entered"
        elif mutation == "binding_active":
            session.get(ExecutionBinding, seeded["binding_id"]).status = "active"
        elif mutation == "binding_cleared_without_terminal_evidence":
            binding = session.get(ExecutionBinding, seeded["binding_id"])
            binding.pos_id = None
            binding.last_exchange_status = "position_attribution_conflict"
        elif mutation == "leg_active":
            session.get(ExecutionOrderLeg, seeded["leg_id"]).status = "active"
        elif mutation == "order_position_mismatch":
            session.get(
                PositionTakeProfitOrder, seeded["order_ids"][0]
            ).pos_id = "different-position"
        session.commit()

    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(
            position_history=[
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": seeded["pos_id"],
                    "posSide": "short",
                    "pos": "5",
                    "closePos": "5",
                }
            ]
        ),
        planned_at=NOW,
    )

    assert not any(
        row.kind == "take_profit_attribution_repair" for row in plan.actions
    )
    exclusion = next(
        row
        for row in plan.exclusions
        if row.target_id == seeded["convergence_id"]
    )
    evidence = json.loads(exclusion.evidence_json)
    assert evidence["attribution_repair_failure"] == expected_failure


@pytest.mark.parametrize(
    ("history_rows", "expected_failure"),
    [
        ([], "exact_full_close_history_not_proven"),
        (
            [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-proven-closed",
                    "posSide": "short",
                    "pos": "5",
                    "closePos": "4",
                }
            ],
            "exact_full_close_history_not_proven",
        ),
        (
            [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-proven-closed",
                    "posSide": "short",
                    "pos": "5",
                    "closePos": "5",
                }
            ],
            "exact_full_close_history_not_proven",
        ),
        (
            [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-proven-closed",
                    "posSide": "long",
                    "pos": "5",
                    "closePos": "5",
                }
            ],
            "exact_full_close_history_not_proven",
        ),
        (
            [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "different-position",
                    "posSide": "short",
                    "pos": "5",
                    "closePos": "5",
                }
            ],
            "exact_full_close_history_not_proven",
        ),
        (
            [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-proven-closed",
                    "posSide": "short",
                    "pos": "5",
                    "closePos": "5",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-proven-closed",
                    "posSide": "short",
                    "pos": "5",
                    "closePos": "5",
                    "uTime": "2",
                },
            ],
            "exact_full_close_history_not_proven",
        ),
    ],
)
def test_plan_fails_closed_without_one_exact_full_close_history(
    tmp_path,
    history_rows,
    expected_failure,
):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(position_history=history_rows),
        planned_at=NOW,
    )

    assert not any(
        row.kind == "take_profit_attribution_repair" for row in plan.actions
    )
    exclusion = next(
        row
        for row in plan.exclusions
        if row.target_id == seeded["convergence_id"]
    )
    assert (
        json.loads(exclusion.evidence_json)["attribution_repair_failure"]
        == expected_failure
    )


@pytest.mark.parametrize("live_kind", ["position", "take_profit", "incomplete"])
def test_plan_blocks_attribution_repair_when_current_snapshot_is_not_flat(
    tmp_path,
    live_kind,
):
    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    kwargs = {
        "position_history": [
            {
                "instId": "BTC-USDT-SWAP",
                "posId": seeded["pos_id"],
                "posSide": "short",
                "pos": "5",
                "closePos": "5",
            }
        ]
    }
    if live_kind == "position":
        kwargs["positions"] = [{"posId": seeded["pos_id"], "pos": "1"}]
    elif live_kind == "take_profit":
        kwargs["pending"] = [{"ordId": "tp-proven-2"}]
    else:
        kwargs["complete"] = False
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=_snapshot(**kwargs),
        planned_at=NOW,
    )

    assert not any(
        row.kind == "take_profit_attribution_repair" for row in plan.actions
    )
    reasons = {
        row.reason_code
        for row in (*plan.exclusions, *plan.conflicts)
        if row.target_id == seeded["convergence_id"]
    }
    assert reasons & {
        "exact_position_or_order_still_live",
        "pending_order_snapshot_incomplete",
    }


def test_apply_atomically_restores_terminal_authority_and_expires_ledgers(
    tmp_path,
):
    from telegram_kol_research.historical_state_repair import (
        apply_historical_state_repair_plan,
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    snapshot = _snapshot(
        position_history=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": seeded["pos_id"],
                "posSide": "short",
                "pos": "5",
                "closePos": "5",
            }
        ]
    )
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )
    with session_factory() as session:
        before_counts = {
            model.__tablename__: session.query(model).count()
            for model in (
                ExecutionBinding,
                ExecutionOrderLeg,
                PositionAttributionAudit,
                TriggerTakeProfitConvergence,
                PositionTakeProfitOrder,
                PositionMutationIntent,
                BoundPositionCloseReservation,
            )
        }

    result = apply_historical_state_repair_plan(
        session_factory,
        snapshot_loader=lambda: snapshot,
        expected_fingerprint=plan.fingerprint,
        expected_action_count=1,
        confirmation_token=plan.confirmation_token,
        applied_at=NOW,
    )

    assert result.applied_actions == 1
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, seeded["leg_id"])
        convergence = session.get(
            TriggerTakeProfitConvergence,
            seeded["convergence_id"],
        )
        orders = (
            session.query(PositionTakeProfitOrder)
            .filter(
                PositionTakeProfitOrder.trigger_take_profit_convergence_id
                == seeded["convergence_id"]
            )
            .order_by(PositionTakeProfitOrder.id.asc())
            .all()
        )
        assert leg.attribution_status == "verified"
        assert leg.status == "manually_closed"
        leg_evidence = json.loads(leg.attribution_evidence_json)
        assert leg_evidence["evidence_type"] == "historical_authority_restored"
        assert leg_evidence["plan_fingerprint"] == plan.fingerprint
        restoration = (
            session.query(PositionAttributionAudit)
            .filter(PositionAttributionAudit.event_type == "historical_authority_restored")
            .one()
        )
        assert restoration.new_state == "verified"
        assert restoration.execution_order_leg_id == seeded["leg_id"]
        assert convergence.status == "completed"
        assert convergence.reason_code == (
            "convergence_position_terminal_prior_authority_restored"
        )
        assert [row.status for row in orders] == ["expired", "expired", "expired"]
        for row in orders:
            evidence = json.loads(row.evidence_json)
            assert evidence["mutation_intent_id"] in {1, 2, 3}
            assert evidence["terminalization"]["plan_fingerprint"] == plan.fingerprint
        summary = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "historical_state_convergence_repair")
            .one()
        )
        assert summary.notification_status == "not_needed"
        after_counts = {
            model.__tablename__: session.query(model).count()
            for model in (
                ExecutionBinding,
                ExecutionOrderLeg,
                PositionAttributionAudit,
                TriggerTakeProfitConvergence,
                PositionTakeProfitOrder,
                PositionMutationIntent,
                BoundPositionCloseReservation,
            )
        }
    assert after_counts == {
        **before_counts,
        "position_attribution_audits": before_counts["position_attribution_audits"]
        + 1,
    }


@pytest.mark.parametrize(
    ("original_evidence", "preserved_key", "preserved_value"),
    [
        ("not-json", "original_evidence_raw", "not-json"),
        ("[]", "original_evidence", []),
    ],
)
def test_apply_preserves_non_object_take_profit_evidence(
    tmp_path,
    original_evidence,
    preserved_key,
    preserved_value,
):
    from telegram_kol_research.historical_state_repair import (
        apply_historical_state_repair_plan,
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    with session_factory() as session:
        session.get(
            PositionTakeProfitOrder,
            seeded["order_ids"][0],
        ).evidence_json = original_evidence
        session.commit()
    snapshot = _snapshot(
        position_history=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": seeded["pos_id"],
                "posSide": "short",
                "pos": "5",
                "closePos": "5",
            }
        ]
    )
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )

    apply_historical_state_repair_plan(
        session_factory,
        snapshot_loader=lambda: snapshot,
        expected_fingerprint=plan.fingerprint,
        expected_action_count=1,
        confirmation_token=plan.confirmation_token,
        applied_at=NOW,
    )

    with session_factory() as session:
        row = session.get(PositionTakeProfitOrder, seeded["order_ids"][0])
        evidence = json.loads(row.evidence_json)
    assert evidence[preserved_key] == preserved_value
    assert evidence["terminalization"]["plan_fingerprint"] == plan.fingerprint


def test_apply_refuses_leg_attribution_evidence_change_after_fresh_plan(
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
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    snapshot = _snapshot(
        position_history=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": seeded["pos_id"],
                "posSide": "short",
                "pos": "5",
                "closePos": "5",
            }
        ]
    )
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )
    original_builder = repair_module.build_historical_state_repair_plan

    def build_then_mutate(*args, **kwargs):
        fresh_plan = original_builder(*args, **kwargs)
        with session_factory() as session:
            leg = session.get(ExecutionOrderLeg, seeded["leg_id"])
            leg.attribution_evidence_json = json.dumps({"changed": True})
            session.commit()
        return fresh_plan

    monkeypatch.setattr(
        repair_module,
        "build_historical_state_repair_plan",
        build_then_mutate,
    )

    with pytest.raises(HistoricalStateRepairRefused, match="changed before apply"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot_loader=lambda: snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=1,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )

    with session_factory() as session:
        convergence = session.get(
            TriggerTakeProfitConvergence,
            seeded["convergence_id"],
        )
        assert convergence.status == "submitted"
        assert (
            session.query(PositionAttributionAudit)
            .filter(
                PositionAttributionAudit.event_type
                == "historical_authority_restored"
            )
            .count()
            == 0
        )
        assert session.query(RepairConfirmationToken).count() == 0


def test_apply_refuses_convergence_desired_plan_change_after_fresh_plan(
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
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    snapshot = _snapshot(
        position_history=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": seeded["pos_id"],
                "posSide": "short",
                "pos": "5",
                "closePos": "5",
            }
        ]
    )
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )
    original_builder = repair_module.build_historical_state_repair_plan

    def build_then_mutate(*args, **kwargs):
        fresh_plan = original_builder(*args, **kwargs)
        with session_factory() as session:
            convergence = session.get(
                TriggerTakeProfitConvergence,
                seeded["convergence_id"],
            )
            convergence.desired_take_profits_json = "[]"
            session.commit()
        return fresh_plan

    monkeypatch.setattr(
        repair_module,
        "build_historical_state_repair_plan",
        build_then_mutate,
    )

    with pytest.raises(HistoricalStateRepairRefused, match="changed before apply"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot_loader=lambda: snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=1,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )


def test_apply_refuses_take_profit_terms_change_after_fresh_plan(
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
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    snapshot = _snapshot(
        position_history=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": seeded["pos_id"],
                "posSide": "short",
                "pos": "5",
                "closePos": "5",
            }
        ]
    )
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )
    original_builder = repair_module.build_historical_state_repair_plan

    def build_then_mutate(*args, **kwargs):
        fresh_plan = original_builder(*args, **kwargs)
        with session_factory() as session:
            order = session.get(PositionTakeProfitOrder, seeded["order_ids"][0])
            order.trigger_price = "1"
            session.commit()
        return fresh_plan

    monkeypatch.setattr(
        repair_module,
        "build_historical_state_repair_plan",
        build_then_mutate,
    )

    with pytest.raises(HistoricalStateRepairRefused, match="changed before apply"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot_loader=lambda: snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=1,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "leg_status",
        "leg_strategy",
        "leg_position",
        "authority_audit_evidence",
        "conflict_audit_evidence",
        "close_mutation_status",
        "close_mutation_order_id",
        "close_reservation_status",
        "binding_status",
        "binding_last_exchange_status",
        "lifecycle_status",
        "convergence_status",
        "take_profit_evidence",
    ],
)
def test_apply_refuses_every_local_cas_category_after_fresh_plan(
    tmp_path,
    monkeypatch,
    mutation,
):
    import telegram_kol_research.historical_state_repair as repair_module
    from telegram_kol_research.historical_state_repair import (
        HistoricalStateRepairRefused,
        apply_historical_state_repair_plan,
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    snapshot = _snapshot(
        position_history=[
            {
                "instId": "BTC-USDT-SWAP",
                "posId": seeded["pos_id"],
                "posSide": "short",
                "pos": "5",
                "closePos": "5",
            }
        ]
    )
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )
    original_builder = repair_module.build_historical_state_repair_plan

    def build_then_mutate(*args, **kwargs):
        fresh_plan = original_builder(*args, **kwargs)
        with session_factory() as session:
            if mutation.startswith("leg_"):
                row = session.get(ExecutionOrderLeg, seeded["leg_id"])
                if mutation == "leg_status":
                    row.status = "active"
                elif mutation == "leg_strategy":
                    row.strategy_instance_id = "different-strategy"
                else:
                    row.pos_id = "different-position"
            elif mutation == "authority_audit_evidence":
                row = session.get(
                    PositionAttributionAudit,
                    seeded["authority_audit_ids"][0],
                )
                row.evidence_json = json.dumps(
                    {"policy_version": 2, "changed": True}
                )
            elif mutation == "conflict_audit_evidence":
                row = session.get(
                    PositionAttributionAudit,
                    seeded["conflict_audit_id"],
                )
                row.evidence_json = json.dumps(
                    {"candidate_leg_ids": [999], "candidate_position_ids": []}
                )
            elif mutation in {"close_mutation_status", "close_mutation_order_id"}:
                row = session.get(PositionMutationIntent, seeded["mutation_id"])
                if mutation == "close_mutation_status":
                    row.status = "submitted"
                else:
                    row.order_id = "late-close-order"
            elif mutation == "close_reservation_status":
                session.get(
                    BoundPositionCloseReservation,
                    seeded["reservation_id"],
                ).status = "submitted"
            elif mutation == "binding_status":
                session.get(ExecutionBinding, seeded["binding_id"]).status = "active"
            elif mutation == "binding_last_exchange_status":
                session.get(
                    ExecutionBinding, seeded["binding_id"]
                ).last_exchange_status = "changed-after-plan"
            elif mutation == "lifecycle_status":
                session.get(
                    StrategyLifecycle, seeded["lifecycle_id"]
                ).lifecycle_status = "entered"
            elif mutation == "convergence_status":
                session.get(
                    TriggerTakeProfitConvergence,
                    seeded["convergence_id"],
                ).status = "waiting_position"
            elif mutation == "take_profit_evidence":
                session.get(
                    PositionTakeProfitOrder, seeded["order_ids"][0]
                ).evidence_json = json.dumps({"changed": True})
            session.commit()
        return fresh_plan

    monkeypatch.setattr(
        repair_module,
        "build_historical_state_repair_plan",
        build_then_mutate,
    )

    with pytest.raises(HistoricalStateRepairRefused, match="changed before apply"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot_loader=lambda: snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=1,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )

    with session_factory() as session:
        assert (
            session.query(PositionAttributionAudit)
            .filter(
                PositionAttributionAudit.event_type
                == "historical_authority_restored"
            )
            .count()
            == 0
        )
        assert (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "historical_state_convergence_repair")
            .count()
            == 0
        )
        assert session.query(RepairConfirmationToken).count() == 0


def test_attribution_repair_dry_run_and_apply_never_call_exchange_writes(
    tmp_path,
):
    from telegram_kol_research.historical_state_repair import (
        apply_historical_state_repair_plan,
        build_historical_state_repair_plan,
        load_historical_state_repair_snapshot_read_only,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    seeded = _seed_proven_attribution_repair_candidate(session_factory)
    history_row = {
        "instId": "BTC-USDT-SWAP",
        "posId": seeded["pos_id"],
        "posSide": "short",
        "pos": "5",
        "closePos": "5",
    }
    client = _ReadOnlyHistoryClient([history_row])
    snapshot = load_historical_state_repair_snapshot_read_only(
        session_factory,
        client=client,
    )
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )

    result = apply_historical_state_repair_plan(
        session_factory,
        snapshot_loader=lambda: load_historical_state_repair_snapshot_read_only(
            session_factory,
            client=client,
        ),
        expected_fingerprint=plan.fingerprint,
        expected_action_count=1,
        confirmation_token=plan.confirmation_token,
        applied_at=NOW,
    )

    assert result.applied_actions == 1
    assert client.history_calls == [
        ("BTC-USDT-SWAP", seeded["pos_id"]),
        ("BTC-USDT-SWAP", seeded["pos_id"]),
    ]


def test_apply_refuses_zero_action_plan(tmp_path):
    from telegram_kol_research.historical_state_repair import (
        HistoricalStateRepairRefused,
        apply_historical_state_repair_plan,
        build_historical_state_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    snapshot = _snapshot()
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=NOW,
    )
    assert plan.action_count == 0

    with pytest.raises(HistoricalStateRepairRefused, match="no actions"):
        apply_historical_state_repair_plan(
            session_factory,
            snapshot_loader=lambda: snapshot,
            expected_fingerprint=plan.fingerprint,
            expected_action_count=0,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
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


def test_sanitized_legacy_snapshot_bootstraps_twice_without_execution_backfill(
    tmp_path,
):
    """Additive schema bootstrap must never reinterpret historical truth."""

    from telegram_kol_research.historical_state_repair import (
        build_historical_state_repair_plan,
    )

    database = tmp_path / "sanitized-production-snapshot.db"
    initial_factory = create_session_factory(database)
    with initial_factory() as session:
        rows = [
            RawMessage(
                chat_id=501,
                message_id=4100,
                text="redacted non-strategy neighbor",
                posted_at=NOW - timedelta(minutes=2),
            ),
            RawMessage(
                chat_id=501,
                message_id=4101,
                text="redacted legacy pending entry",
                posted_at=NOW - timedelta(minutes=1),
            ),
            RawMessage(
                chat_id=501,
                message_id=4102,
                text="redacted legacy entered position",
                posted_at=NOW,
            ),
        ]
        session.add_all(rows)
        session.flush()
        session.add_all(
            [
                SignalCandidate(
                    raw_message_id=rows[1].id,
                    symbol="BTC",
                    side="long",
                    event_type="entry_signal",
                    parse_source="legacy_text_ai",
                ),
                StrategyLifecycle(
                    chat_id=501,
                    message_id=4101,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=NOW - timedelta(minutes=1),
                ),
                StrategyLifecycle(
                    chat_id=501,
                    message_id=4102,
                    symbol="ETH",
                    side="short",
                    lifecycle_status="entered",
                    signal_at=NOW,
                ),
            ]
        )
        session.commit()
    initial_factory.kw["bind"].dispose()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE instruction_execution_transitions")
        connection.execute("DROP TABLE instruction_execution_contracts")
        connection.commit()

    execution_models = (
        MessageInstructionItem,
        TradeSignal,
        ExecutionBinding,
        ExecutionEvent,
        EntryStrategyAssembly,
        InstructionExecutionContract,
    )

    first_factory = create_session_factory(database)
    with first_factory() as session:
        first_counts = {
            model.__tablename__: session.query(model).count()
            for model in execution_models
        }
        assert first_counts == {
            "message_instruction_items": 0,
            "trade_signals": 0,
            "execution_bindings": 0,
            "execution_events": 0,
            "entry_strategy_assemblies": 0,
            "instruction_execution_contracts": 0,
        }

    second_factory = create_session_factory(database)
    with second_factory() as session:
        second_counts = {
            model.__tablename__: session.query(model).count()
            for model in execution_models
        }
    assert second_counts == first_counts
    plan = build_historical_state_repair_plan(
        second_factory,
        snapshot=_snapshot(),
        planned_at=NOW,
    )

    assert plan.action_count == 0
    with second_factory() as session:
        assert session.query(RawMessage).count() == 3
        assert session.query(SignalCandidate).count() == 1
        assert session.query(StrategyLifecycle).count() == 2
        assert session.query(MessageInstructionItem).count() == 0
        assert session.query(TradeSignal).count() == 0
        assert session.query(ExecutionBinding).count() == 0
        assert session.query(ExecutionEvent).count() == 0
        assert session.query(EntryStrategyAssembly).count() == 0
        assert session.query(InstructionExecutionContract).count() == 0
    first_factory.kw["bind"].dispose()
    second_factory.kw["bind"].dispose()
