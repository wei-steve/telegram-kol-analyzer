"""Fail-closed cancellation planning for reviewed pending Deepcoin entries."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Iterable

from telegram_kol_research.execution_events import (
    ExecutionEventRecord,
    record_execution_event,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    EntryRevisionReplacement,
    InstructionExecutionContract,
    MessageProcessingJob,
    PositionMutationIntent,
    PositionBackupStopOrder,
    PositionProtectionLeg,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
    StrategyLifecycle,
    TradeSignal,
    TriggerProtectionIntent,
    TriggerTakeProfitConvergence,
    WorkerCommandJob,
)
from telegram_kol_research.position_mutation_intents import (
    reserve_position_mutation_intent,
    transition_position_mutation_intent,
)
from telegram_kol_research.repair_confirmation import (
    consume_repair_confirmation_token,
    require_repair_confirmation_token_unused,
)


_GOVERNED_INSTRUMENTS = (
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
)
_PENDING_LEG_STATES = frozenset({"pending", "open", "submitted"})
_TERMINAL_LEG_STATES = frozenset(
    {"cancelled", "canceled", "expired", "rejected"}
)
_CANCELLED_HISTORY_STATES = frozenset(
    {"cancelled", "canceled", "cancel", "expired"}
)
_FILLED_HISTORY_STATES = frozenset(
    {"filled", "partially_filled", "partially-filled", "partial_filled"}
)


@dataclass(frozen=True, slots=True)
class ReviewedPendingEntryTarget:
    order_id: str
    instrument_id: str
    lifecycle_id: int
    execution_binding_id: int
    execution_order_leg_id: int
    trigger_price: str
    size: str
    embedded_stop_price: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class ReviewedPendingEntryCancelAction:
    order_id: str
    instrument_id: str
    lifecycle_id: int
    execution_binding_id: int
    execution_order_leg_id: int
    strategy_instance_id: str
    trigger_price: str
    size: str
    embedded_stop_price: str
    request_fingerprint: str
    request_json_fingerprint: str
    exchange_row_fingerprint: str
    action_id: str


@dataclass(frozen=True, slots=True)
class ReviewedPendingEntryCancelPlan:
    created_at: datetime
    actions: tuple[ReviewedPendingEntryCancelAction, ...]
    conflicts: tuple[dict[str, str], ...]
    completed_order_ids: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ReviewedPendingEntryCancelResult:
    status: str
    order_id: str
    reason_code: str | None = None


REVIEWED_PENDING_ENTRY_TARGETS = (
    ReviewedPendingEntryTarget(
        order_id="1001124718697641",
        instrument_id="ETH-USDT-SWAP",
        lifecycle_id=780,
        execution_binding_id=271,
        execution_order_leg_id=479,
        trigger_price="1827",
        size="3",
        embedded_stop_price="1795",
        request_fingerprint="7f9f86c10c30936a062984b6a5839b5db293f9dcbd0222d45a85b90c37f06130",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124718698413",
        instrument_id="ETH-USDT-SWAP",
        lifecycle_id=780,
        execution_binding_id=271,
        execution_order_leg_id=480,
        trigger_price="1812",
        size="3",
        embedded_stop_price="1795",
        request_fingerprint="a05cae373185d2b221b47297b23c25cd854affc402310588ed4a19e3f8ffb3e6",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124760022605",
        instrument_id="BTC-USDT-SWAP",
        lifecycle_id=812,
        execution_binding_id=281,
        execution_order_leg_id=494,
        trigger_price="61890",
        size="13",
        embedded_stop_price="60900",
        request_fingerprint="fa3c307a5da05743b1bfc861757bab70713ed0b642699726ff86a8d516d982b0",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124760022650",
        instrument_id="BTC-USDT-SWAP",
        lifecycle_id=812,
        execution_binding_id=281,
        execution_order_leg_id=495,
        trigger_price="61390",
        size="14",
        embedded_stop_price="60900",
        request_fingerprint="ca8806acf87c2b8d34354aea4e0538f71e952196fdf7f443effed7ec4654c401",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124898942178",
        instrument_id="ETH-USDT-SWAP",
        lifecycle_id=911,
        execution_binding_id=308,
        execution_order_leg_id=532,
        trigger_price="2250",
        size="2.3",
        embedded_stop_price="2186",
        request_fingerprint="1f5a6157ee1fbc697c69ba164ff8bfc23f11a0def0916aabfaa5dca62579f99a",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124905627977",
        instrument_id="BTC-USDT-SWAP",
        lifecycle_id=914,
        execution_binding_id=309,
        execution_order_leg_id=533,
        trigger_price="73690",
        size="8",
        embedded_stop_price="72300",
        request_fingerprint="a1838c649c7b17d2368c71d035719915700c7cd0e759c694c442134c49b787d6",
    ),
    ReviewedPendingEntryTarget(
        order_id="1001124905628046",
        instrument_id="BTC-USDT-SWAP",
        lifecycle_id=914,
        execution_binding_id=309,
        execution_order_leg_id=534,
        trigger_price="73390",
        size="8",
        embedded_stop_price="72300",
        request_fingerprint="a33495361faf3ea1e7a90436a2cd8f6b716d3477a394f4628d2c7a7d47d11786",
    ),
)


def build_reviewed_pending_entry_cancel_plan(
    session_factory,
    *,
    deepcoin_client,
    targets: Iterable[ReviewedPendingEntryTarget],
    now: datetime | None = None,
) -> ReviewedPendingEntryCancelPlan:
    """Build a fresh, read-only plan for the closed reviewed target set."""

    created_at = now or datetime.now(UTC)
    reviewed = tuple(targets)
    order_ids = {target.order_id for target in reviewed}
    if len(order_ids) != len(reviewed):
        return _plan(
            created_at,
            (),
            ({"order_id": "*", "reason": "duplicate_reviewed_target"},),
        )

    instruments = tuple(
        sorted({*_GOVERNED_INSTRUMENTS, *(target.instrument_id for target in reviewed)})
    )
    try:
        snapshots = {
            instrument_id: {
                "positions": tuple(
                    row
                    for row in deepcoin_client.list_positions(
                        inst_id=instrument_id
                    )
                    if isinstance(row, dict)
                ),
                "regular": tuple(
                    row
                    for row in deepcoin_client.list_open_orders(
                        inst_id=instrument_id
                    )
                    if isinstance(row, dict)
                ),
                "pending": tuple(
                    row
                    for row in deepcoin_client.list_trigger_orders_pending(
                        inst_id=instrument_id
                    )
                    if isinstance(row, dict)
                ),
                "history": tuple(
                    row
                    for row in deepcoin_client.list_trigger_order_history(
                        inst_id=instrument_id
                    )
                    if isinstance(row, dict)
                ),
                "fills": tuple(
                    row
                    for row in deepcoin_client.list_trade_fills(
                        inst_id=instrument_id
                    )
                    if isinstance(row, dict)
                ),
            }
            for instrument_id in instruments
        }
    except Exception:
        return _plan(
            created_at,
            (),
            tuple(
                {"order_id": target.order_id, "reason": "exchange_snapshot_unavailable"}
                for target in reviewed
            ),
        )

    global_conflicts: list[dict[str, str]] = []
    if any(snapshot["positions"] for snapshot in snapshots.values()):
        global_conflicts.append(
            {"order_id": "*", "reason": "live_position_present"}
        )
    if any(snapshot["regular"] for snapshot in snapshots.values()):
        global_conflicts.append(
            {"order_id": "*", "reason": "regular_order_present"}
        )
    if any(
        not _order_id(row)
        for snapshot in snapshots.values()
        for row in snapshot["pending"]
    ):
        global_conflicts.append(
            {"order_id": "*", "reason": "unidentified_pending_trigger"}
        )
    unreviewed = sorted(
        {
            order_id
            for snapshot in snapshots.values()
            for row in snapshot["pending"]
            if (order_id := _order_id(row)) and order_id not in order_ids
        }
    )
    if unreviewed:
        global_conflicts.append(
            {"order_id": "*", "reason": "unreviewed_pending_trigger"}
        )
    if global_conflicts:
        return _plan(created_at, (), tuple(global_conflicts))

    actions: list[ReviewedPendingEntryCancelAction] = []
    conflicts: list[dict[str, str]] = []
    completed: list[str] = []
    with session_factory() as session:
        if _active_exchange_authority_present(session):
            return _plan(
                created_at,
                (),
                (
                    {
                        "order_id": "*",
                        "reason": "active_exchange_authority_present",
                    },
                ),
            )
        for target in reviewed:
            snapshot = snapshots.get(target.instrument_id, {})
            pending_rows = [
                row
                for row in snapshot.get("pending", ())
                if _order_id(row) == target.order_id
            ]
            history_rows = [
                row
                for row in snapshot.get("history", ())
                if _matches_order(row, target.order_id)
            ]
            fill_rows = [
                row
                for row in snapshot.get("fills", ())
                if _matches_order(row, target.order_id)
            ]

            leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
            binding = session.get(ExecutionBinding, target.execution_binding_id)
            lifecycle = session.get(StrategyLifecycle, target.lifecycle_id)
            intent_rows = (
                session.query(TriggerProtectionIntent)
                .filter(
                    TriggerProtectionIntent.venue == "deepcoin",
                    TriggerProtectionIntent.execution_order_leg_id
                    == target.execution_order_leg_id,
                )
                .all()
            )
            if not _local_identity_matches(
                target,
                leg=leg,
                binding=binding,
                lifecycle=lifecycle,
                intent_rows=intent_rows,
            ):
                conflicts.append(_conflict(target, "local_ownership_mismatch"))
                continue

            earlier_cancel = (
                session.query(PositionMutationIntent)
                .filter(
                    PositionMutationIntent.operation
                    == "cancel_reviewed_pending_entry",
                    PositionMutationIntent.order_id == target.order_id,
                    PositionMutationIntent.status != "confirmed",
                )
                .first()
            )
            if earlier_cancel is not None:
                conflicts.append(
                    _conflict(target, "prior_cancel_outcome_unknown")
                )
                continue

            if not pending_rows:
                if _completed_state_matches(
                    session,
                    target=target,
                    leg=leg,
                    binding=binding,
                    lifecycle=lifecycle,
                    history_rows=history_rows,
                    fill_rows=fill_rows,
                ):
                    completed.append(target.order_id)
                else:
                    conflicts.append(_conflict(target, "reviewed_order_not_pending"))
                continue
            if len(pending_rows) != 1:
                conflicts.append(_conflict(target, "reviewed_order_not_unique"))
                continue
            row = pending_rows[0]
            if not _exchange_row_matches(row, target):
                conflicts.append(_conflict(target, "reviewed_exchange_row_changed"))
                continue
            if fill_rows or _history_has_filled_state(history_rows):
                conflicts.append(_conflict(target, "reviewed_order_has_fill_evidence"))
                continue
            if not _local_pending_state_matches(
                session,
                target=target,
                leg=leg,
                lifecycle=lifecycle,
                intent=intent_rows[0],
            ):
                conflicts.append(_conflict(target, "reviewed_local_state_changed"))
                continue

            request = _json_object(leg.request_json)
            if not _request_matches(request, target):
                conflicts.append(_conflict(target, "reviewed_request_changed"))
                continue
            base = {
                "order_id": target.order_id,
                "instrument_id": target.instrument_id,
                "lifecycle_id": target.lifecycle_id,
                "execution_binding_id": target.execution_binding_id,
                "execution_order_leg_id": target.execution_order_leg_id,
                "strategy_instance_id": str(leg.strategy_instance_id or ""),
                "trigger_price": target.trigger_price,
                "size": target.size,
                "embedded_stop_price": target.embedded_stop_price,
                "request_fingerprint": target.request_fingerprint,
                "request_json_fingerprint": _fingerprint(request),
                "exchange_row_fingerprint": _fingerprint(row),
            }
            actions.append(
                ReviewedPendingEntryCancelAction(
                    **base,
                    action_id=_fingerprint(base),
                )
            )

    return _plan(
        created_at,
        () if conflicts else actions,
        conflicts,
        completed_order_ids=completed,
    )


def _active_exchange_authority_present(session) -> bool:
    checks = (
        session.query(MessageProcessingJob.id).filter(
            MessageProcessingJob.shadow.is_(False),
            MessageProcessingJob.status == "claimed",
        ),
        session.query(ExecutionOrderLeg.id).filter(
            ExecutionOrderLeg.status.in_({"submitting", "cancel_submitting"})
        ),
        session.query(PositionBackupStopOrder.id).filter(
            PositionBackupStopOrder.status == "submitting"
        ),
        session.query(InstructionExecutionContract.id).filter(
            InstructionExecutionContract.state == "submitting"
        ),
        session.query(StrategyManagementComponent.id).filter(
            StrategyManagementComponent.status.in_(
                {"submitting", "cancel_submitting"}
            )
        ),
        session.query(StrategyManagementBatch.id).filter(
            StrategyManagementBatch.status.not_in(
                {"succeeded", "blocked", "resolved"}
            )
        ),
        session.query(StrategyRevisionBatch.id).filter(
            StrategyRevisionBatch.status.not_in({"succeeded", "blocked"})
        ),
        session.query(StrategyRevisionLeg.id)
        .join(
            StrategyRevisionBatch,
            StrategyRevisionBatch.id == StrategyRevisionLeg.revision_batch_id,
        )
        .filter(
            StrategyRevisionLeg.status == "cancel_submitting",
            StrategyRevisionBatch.advance_claim_token.is_not(None),
            StrategyRevisionBatch.advance_claimed_at.is_not(None),
        ),
        session.query(EntryRevisionReplacement.id)
        .join(
            StrategyRevisionBatch,
            StrategyRevisionBatch.id
            == EntryRevisionReplacement.revision_batch_id,
        )
        .filter(
            EntryRevisionReplacement.status == "submit_reserved",
            StrategyRevisionBatch.advance_claim_token.is_not(None),
            StrategyRevisionBatch.advance_claimed_at.is_not(None),
        ),
        session.query(TriggerProtectionIntent.id).filter(
            TriggerProtectionIntent.recovery_state.in_(
                {"submitting", "cancel_submitting"}
            )
        ),
        session.query(PositionMutationIntent.id).filter(
            PositionMutationIntent.status.in_(
                {"submitting", "cancel_submitting"}
            )
        ),
        session.query(TradeSignal.id).filter(
            TradeSignal.status.in_(
                {"processing", "submitting", "cancel_submitting"}
            )
        ),
        session.query(WorkerCommandJob.id).filter(
            WorkerCommandJob.status.in_({"pending", "claimed", "executing"})
        ),
    )
    return any(query.first() is not None for query in checks)


def apply_reviewed_pending_entry_cancel_plan(
    session_factory,
    plan: ReviewedPendingEntryCancelPlan,
    *,
    deepcoin_client,
    targets: Iterable[ReviewedPendingEntryTarget],
    order_id: str,
    action_id: str,
    expected_fingerprint: str,
    confirmation_token: str,
    now: datetime | None = None,
) -> ReviewedPendingEntryCancelResult:
    """Cancel one exact reviewed entry, without ever retrying the write."""

    if expected_fingerprint != plan.fingerprint:
        raise ValueError("plan fingerprint mismatch")
    if plan.conflicts:
        raise ValueError("plan has conflicts")
    selected = [
        action
        for action in plan.actions
        if action.order_id == str(order_id)
        and action.action_id == str(action_id)
    ]
    if len(selected) != 1:
        raise ValueError("exactly one reviewed cancellation action is required")
    require_repair_confirmation_token_unused(
        session_factory,
        confirmation_token=confirmation_token,
    )

    reviewed = tuple(targets)
    fresh = build_reviewed_pending_entry_cancel_plan(
        session_factory,
        deepcoin_client=deepcoin_client,
        targets=reviewed,
        now=now,
    )
    if fresh.fingerprint != expected_fingerprint:
        raise ValueError("plan fingerprint changed")
    current = [
        action
        for action in fresh.actions
        if action.order_id == str(order_id)
        and action.action_id == str(action_id)
    ]
    if len(current) != 1:
        raise ValueError("reviewed cancellation action changed")
    action = current[0]
    observed_at = now or datetime.now(UTC)
    request = {"instId": action.instrument_id, "ordId": action.order_id}
    authority_fingerprint = _fingerprint(
        {
            "action_id": action.action_id,
            "plan_fingerprint": fresh.fingerprint,
            "exchange_row_fingerprint": action.exchange_row_fingerprint,
            "request_json_fingerprint": action.request_json_fingerprint,
        }
    )
    intent = reserve_position_mutation_intent(
        session_factory,
        idempotency_key=(
            f"reviewed-pending-entry-cancel:{action.order_id}:{action.action_id}"
        ),
        operation="cancel_reviewed_pending_entry",
        strategy_instance_id=action.strategy_instance_id,
        execution_binding_id=action.execution_binding_id,
        execution_order_leg_id=action.execution_order_leg_id,
        pos_id=f"pending-entry:{action.order_id}",
        order_id=action.order_id,
        authority_fingerprint=authority_fingerprint,
        request_fingerprint=_fingerprint(request),
        request=request,
        reserved_at=observed_at,
        venue="deepcoin",
    )
    intent_id = int(intent.id)
    if intent.status != "reserved":
        return ReviewedPendingEntryCancelResult(
            status=f"intent_{intent.status}",
            order_id=action.order_id,
            reason_code=str(intent.status),
        )
    consume_repair_confirmation_token(
        session_factory,
        confirmation_token=confirmation_token,
        action_kind="cancel_reviewed_pending_entry",
        action_id=action.action_id,
        pos_id=f"pending-entry:{action.order_id}",
        consumed_at=observed_at,
    )
    if not _single_pending_cancel_write_gate(
        session_factory,
        action=action,
        mutation_intent_id=intent_id,
    ):
        transition_position_mutation_intent(
            session_factory,
            intent_id,
            expected_statuses={"reserved"},
            new_status="blocked",
            transitioned_at=observed_at,
            error={"reason": "exact_pending_cancel_write_gate_blocked"},
        )
        _record_cancel_event(
            session_factory,
            action,
            status="blocked",
            reason="exact_pending_cancel_write_gate_blocked",
            request=request,
            response={"submitted": False},
            now=observed_at,
        )
        return ReviewedPendingEntryCancelResult(
            status="blocked",
            order_id=action.order_id,
            reason_code="exact_pending_cancel_write_gate_blocked",
        )
    if not transition_position_mutation_intent(
        session_factory,
        intent_id,
        expected_statuses={"reserved"},
        new_status="submitting",
        transitioned_at=observed_at,
    ):
        return ReviewedPendingEntryCancelResult(
            status="intent_changed",
            order_id=action.order_id,
            reason_code="mutation_intent_changed",
        )

    try:
        response = deepcoin_client.cancel_trigger_order(request)
    except Exception:
        transition_position_mutation_intent(
            session_factory,
            intent_id,
            expected_statuses={"submitting"},
            new_status="recovery_required",
            transitioned_at=observed_at,
            error={"reason": "cancel_outcome_unknown"},
        )
        _record_cancel_event(
            session_factory,
            action,
            status="unknown",
            reason="cancel_outcome_unknown",
            request=request,
            response={"outcome": "unknown"},
            now=observed_at,
        )
        return ReviewedPendingEntryCancelResult(
            status="cancel_outcome_unknown",
            order_id=action.order_id,
            reason_code="cancel_outcome_unknown",
        )

    if not _confirmed_cancel_response(response, order_id=action.order_id):
        transition_position_mutation_intent(
            session_factory,
            intent_id,
            expected_statuses={"submitting"},
            new_status="recovery_required",
            transitioned_at=observed_at,
            response={"confirmed": False},
            error={"reason": "cancel_response_unconfirmed"},
        )
        _record_cancel_event(
            session_factory,
            action,
            status="unknown",
            reason="cancel_response_unconfirmed",
            request=request,
            response={"confirmed": False},
            now=observed_at,
        )
        return ReviewedPendingEntryCancelResult(
            status="cancel_outcome_unknown",
            order_id=action.order_id,
            reason_code="cancel_response_unconfirmed",
        )

    transition_position_mutation_intent(
        session_factory,
        intent_id,
        expected_statuses={"submitting"},
        new_status="submitted",
        transitioned_at=observed_at,
        response={"code": "0", "order_id": action.order_id},
    )

    try:
        snapshots = _read_exchange_snapshots(
            deepcoin_client,
            instruments=tuple(
                sorted(
                    {
                        *_GOVERNED_INSTRUMENTS,
                        *(target.instrument_id for target in reviewed),
                    }
                )
            ),
        )
    except Exception:
        transition_position_mutation_intent(
            session_factory,
            intent_id,
            expected_statuses={"submitted"},
            new_status="recovery_required",
            transitioned_at=observed_at,
            error={"reason": "cancel_readback_unavailable"},
        )
        _record_cancel_event(
            session_factory,
            action,
            status="unknown",
            reason="cancel_readback_unavailable",
            request=request,
            response={"code": "0", "order_id": action.order_id},
            now=observed_at,
        )
        return ReviewedPendingEntryCancelResult(
            status="cancel_outcome_unknown",
            order_id=action.order_id,
            reason_code="cancel_readback_unavailable",
        )

    if not _post_cancel_exchange_state_matches(
        snapshots,
        plan=fresh,
        selected=action,
    ):
        transition_position_mutation_intent(
            session_factory,
            intent_id,
            expected_statuses={"submitted"},
            new_status="recovery_required",
            transitioned_at=observed_at,
            response={"code": "0", "order_id": action.order_id},
            error={"reason": "post_cancel_state_changed"},
        )
        _record_cancel_event(
            session_factory,
            action,
            status="confirmed_readback_changed",
            reason="post_cancel_state_changed",
            request=request,
            response={"code": "0", "order_id": action.order_id},
            now=observed_at,
        )
        return ReviewedPendingEntryCancelResult(
            status="cancel_confirmed_readback_changed",
            order_id=action.order_id,
            reason_code="post_cancel_state_changed",
        )

    if not _terminalize_confirmed_cancel(
        session_factory,
        action,
        mutation_intent_id=intent_id,
        plan_fingerprint=fresh.fingerprint,
        request=request,
        now=observed_at,
    ):
        transition_position_mutation_intent(
            session_factory,
            intent_id,
            expected_statuses={"submitted"},
            new_status="recovery_required",
            transitioned_at=observed_at,
            response={"code": "0", "order_id": action.order_id},
            error={"reason": "confirmed_cancel_database_state_changed"},
        )
        _record_cancel_event(
            session_factory,
            action,
            status="confirmed_audit_state_changed",
            reason="confirmed_cancel_database_state_changed",
            request=request,
            response={"code": "0", "order_id": action.order_id},
            now=observed_at,
        )
        return ReviewedPendingEntryCancelResult(
            status="cancelled_audit_state_changed",
            order_id=action.order_id,
            reason_code="confirmed_cancel_database_state_changed",
        )

    return ReviewedPendingEntryCancelResult(
        status="cancelled",
        order_id=action.order_id,
    )


def _single_pending_cancel_write_gate(
    session_factory,
    *,
    action: ReviewedPendingEntryCancelAction,
    mutation_intent_id: int,
) -> bool:
    """Last-moment database gate allowing only this one reserved cancel."""

    with session_factory() as session:
        intent = session.get(PositionMutationIntent, mutation_intent_id)
        leg = session.get(ExecutionOrderLeg, action.execution_order_leg_id)
        competing = (
            session.query(PositionMutationIntent.id)
            .filter(
                PositionMutationIntent.operation
                == "cancel_reviewed_pending_entry",
                PositionMutationIntent.status.in_(
                    {"reserved", "submitting", "submitted", "recovery_required"}
                ),
                PositionMutationIntent.id != mutation_intent_id,
            )
            .first()
        )
        return bool(
            intent is not None
            and intent.status == "reserved"
            and intent.order_id == action.order_id
            and intent.execution_binding_id == action.execution_binding_id
            and intent.execution_order_leg_id == action.execution_order_leg_id
            and leg is not None
            and leg.execution_binding_id == action.execution_binding_id
            and leg.order_id == action.order_id
            and str(leg.status or "").lower() in _PENDING_LEG_STATES
            and competing is None
            and not _active_exchange_authority_present(session)
        )


def _read_exchange_snapshots(deepcoin_client, *, instruments: Iterable[str]):
    return {
        instrument_id: {
            "positions": tuple(
                row
                for row in deepcoin_client.list_positions(inst_id=instrument_id)
                if isinstance(row, dict)
            ),
            "regular": tuple(
                row
                for row in deepcoin_client.list_open_orders(inst_id=instrument_id)
                if isinstance(row, dict)
            ),
            "pending": tuple(
                row
                for row in deepcoin_client.list_trigger_orders_pending(
                    inst_id=instrument_id
                )
                if isinstance(row, dict)
            ),
            "history": tuple(
                row
                for row in deepcoin_client.list_trigger_order_history(
                    inst_id=instrument_id
                )
                if isinstance(row, dict)
            ),
            "fills": tuple(
                row
                for row in deepcoin_client.list_trade_fills(inst_id=instrument_id)
                if isinstance(row, dict)
            ),
        }
        for instrument_id in instruments
    }


def _post_cancel_exchange_state_matches(
    snapshots,
    *,
    plan: ReviewedPendingEntryCancelPlan,
    selected: ReviewedPendingEntryCancelAction,
) -> bool:
    if any(
        snapshot[collection]
        for snapshot in snapshots.values()
        for collection in ("positions", "regular")
    ):
        return False
    all_pending = [
        row
        for snapshot in snapshots.values()
        for row in snapshot["pending"]
    ]
    if any(not _order_id(row) for row in all_pending):
        return False
    if any(_order_id(row) == selected.order_id for row in all_pending):
        return False

    expected = {
        action.order_id: action.exchange_row_fingerprint
        for action in plan.actions
        if action.order_id != selected.order_id
    }
    observed: dict[str, str] = {}
    for row in all_pending:
        order_id = _order_id(row)
        if order_id in observed:
            return False
        observed[order_id] = _fingerprint(row)
    if observed != expected:
        return False

    selected_snapshot = snapshots.get(selected.instrument_id)
    if not selected_snapshot:
        return False
    history_rows = [
        row
        for row in selected_snapshot["history"]
        if _matches_order(row, selected.order_id)
    ]
    if not _history_has_cancelled_state(history_rows):
        return False
    if _history_has_filled_state(history_rows):
        return False
    reviewed_ids = {action.order_id for action in plan.actions}
    return not any(
        _order_id(row) in reviewed_ids
        for snapshot in snapshots.values()
        for row in snapshot["fills"]
    )


def _terminalize_confirmed_cancel(
    session_factory,
    action: ReviewedPendingEntryCancelAction,
    *,
    mutation_intent_id: int,
    plan_fingerprint: str,
    request: dict[str, str],
    now: datetime,
) -> bool:
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, action.execution_order_leg_id)
        binding = session.get(ExecutionBinding, action.execution_binding_id)
        lifecycle = session.get(StrategyLifecycle, action.lifecycle_id)
        intents = (
            session.query(TriggerProtectionIntent)
            .filter(
                TriggerProtectionIntent.venue == "deepcoin",
                TriggerProtectionIntent.execution_order_leg_id
                == action.execution_order_leg_id,
            )
            .all()
        )
        protection = (
            session.query(PositionProtectionLeg)
            .filter(
                PositionProtectionLeg.venue == "deepcoin",
                PositionProtectionLeg.execution_order_leg_id
                == action.execution_order_leg_id,
            )
            .all()
        )
        convergence = (
            session.query(TriggerTakeProfitConvergence)
            .filter(
                TriggerTakeProfitConvergence.venue == "deepcoin",
                TriggerTakeProfitConvergence.execution_order_leg_id
                == action.execution_order_leg_id,
            )
            .all()
        )
        mutation_intent = session.get(
            PositionMutationIntent,
            mutation_intent_id,
        )
        existing_events = (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.action == "cancel_reviewed_pending_entry",
                ExecutionEvent.order_id == action.order_id,
            )
            .all()
        )
        target = ReviewedPendingEntryTarget(
            order_id=action.order_id,
            instrument_id=action.instrument_id,
            lifecycle_id=action.lifecycle_id,
            execution_binding_id=action.execution_binding_id,
            execution_order_leg_id=action.execution_order_leg_id,
            trigger_price=action.trigger_price,
            size=action.size,
            embedded_stop_price=action.embedded_stop_price,
            request_fingerprint=action.request_fingerprint,
        )
        stored_request = _json_object(leg.request_json if leg is not None else None)
        expected_authority_fingerprint = _fingerprint(
            {
                "action_id": action.action_id,
                "plan_fingerprint": plan_fingerprint,
                "exchange_row_fingerprint": action.exchange_row_fingerprint,
                "request_json_fingerprint": action.request_json_fingerprint,
            }
        )
        if not (
            _local_identity_matches(
                target,
                leg=leg,
                binding=binding,
                lifecycle=lifecycle,
                intent_rows=intents,
            )
            and _local_pending_state_matches(
                session,
                target=target,
                leg=leg,
                lifecycle=lifecycle,
                intent=intents[0],
            )
            and str(binding.status or "").lower() in {"open", "active"}
            and str(binding.strategy_instance_id or "")
            == action.strategy_instance_id
            and str(leg.strategy_instance_id or "")
            == action.strategy_instance_id
            and str(lifecycle.symbol or "").upper()
            == action.instrument_id.removesuffix("-USDT-SWAP")
            and str(lifecycle.side or "").lower() == "long"
            and _request_matches(stored_request, target)
            and _fingerprint(stored_request)
            == action.request_json_fingerprint
            and len(intents) == 1
            and len(protection) == 2
            and {str(row.role or "") for row in protection}
            == {"primary_stop", "backup_stop"}
            and all(
                row.execution_binding_id == action.execution_binding_id
                and row.parent_entry_order_id == action.order_id
                and str(row.status or "") in {"planned", "waiting_fill"}
                for row in protection
            )
            and len(convergence) == 1
            and convergence[0].execution_binding_id
            == action.execution_binding_id
            and str(convergence[0].status or "")
            in {"waiting_backup_stop", "waiting_position", "ready"}
            and not existing_events
            and mutation_intent is not None
            and mutation_intent.status == "submitted"
            and mutation_intent.venue == "deepcoin"
            and mutation_intent.operation
            == "cancel_reviewed_pending_entry"
            and mutation_intent.strategy_instance_id
            == action.strategy_instance_id
            and mutation_intent.execution_binding_id
            == action.execution_binding_id
            and mutation_intent.order_id == action.order_id
            and mutation_intent.execution_order_leg_id
            == action.execution_order_leg_id
            and mutation_intent.pos_id
            == f"pending-entry:{action.order_id}"
            and mutation_intent.request_fingerprint == _fingerprint(request)
            and _json_object(mutation_intent.request_json) == request
            and _json_object(mutation_intent.response_json)
            == {"code": "0", "order_id": action.order_id}
            and mutation_intent.error_json in (None, "")
            and mutation_intent.submitted_at is not None
            and mutation_intent.confirmed_at is None
            and mutation_intent.authority_fingerprint
            == expected_authority_fingerprint
        ):
            session.rollback()
            return False

        leg.status = "cancelled"
        leg.terminal_reason = "operator_cancelled_unfilled_entry_leg"
        leg.last_verified_at = now
        leg.updated_at = now
        intent = intents[0]
        intent.recovery_state = "resolved"
        intent.recovery_disposition = "terminal"
        intent.last_reason_code = "parent_trigger_cancelled_before_entry"
        intent.next_attempt_at = None
        intent.updated_at = now
        for row in protection:
            row.status = "cancelled"
            row.updated_at = now
        convergence_row = convergence[0]
        convergence_row.status = "completed"
        convergence_row.reason_code = "parent_trigger_cancelled_before_entry"
        convergence_row.completed_at = now
        convergence_row.updated_at = now
        mutation_intent.status = "confirmed"
        mutation_intent.response_json = json.dumps(
            {"code": "0", "order_id": action.order_id},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        mutation_intent.confirmed_at = now
        mutation_intent.updated_at = now

        entry_legs = (
            session.query(ExecutionOrderLeg)
            .filter(
                ExecutionOrderLeg.execution_binding_id
                == action.execution_binding_id,
                ExecutionOrderLeg.purpose == "entry",
            )
            .all()
        )
        binding_complete = all(
            (
                row.id == leg.id
                or str(row.status or "").lower() in _TERMINAL_LEG_STATES
            )
            for row in entry_legs
        )
        if binding_complete:
            binding.status = "cancelled"
            binding.last_exchange_status = "reviewed_pending_entries_cancelled"
            binding.updated_at = now
            lifecycle.lifecycle_status = "expired"
            lifecycle.exit_reason = "expired"
            lifecycle.exited_at = now
            lifecycle.management_action = "reviewed_pending_entries_cancelled"
            lifecycle.management_note = (
                "All reviewed unfilled pending entry legs were confirmed cancelled."
            )
            lifecycle.expiry_review_next_at = None
            lifecycle.updated_at = now

        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                action="cancel_reviewed_pending_entry",
                status="confirmed",
                execution_binding_id=action.execution_binding_id,
                strategy_instance_id=action.strategy_instance_id,
                venue="deepcoin",
                symbol=action.instrument_id.split("-")[0],
                side="long",
                order_id=action.order_id,
                reason="reviewed_stale_pending_entry_cancelled",
                before={
                    "plan_fingerprint": plan_fingerprint,
                    "action_id": action.action_id,
                    "exchange_row_fingerprint": action.exchange_row_fingerprint,
                },
                after={"pending": False, "terminalized": True},
                request=request,
                response={"code": "0", "order_id": action.order_id},
                created_at=now,
            ),
            session=session,
        )
        session.commit()
        return True


def _record_cancel_event(
    session_factory,
    action: ReviewedPendingEntryCancelAction,
    *,
    status: str,
    reason: str,
    request: dict[str, str],
    response: dict[str, Any],
    now: datetime,
) -> None:
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            action="cancel_reviewed_pending_entry",
            status=status,
            execution_binding_id=action.execution_binding_id,
            strategy_instance_id=action.strategy_instance_id,
            venue="deepcoin",
            symbol=action.instrument_id.split("-")[0],
            side="long",
            order_id=action.order_id,
            reason=reason,
            request=request,
            response=response,
            created_at=now,
        ),
    )


def _local_identity_matches(
    target: ReviewedPendingEntryTarget,
    *,
    leg: ExecutionOrderLeg | None,
    binding: ExecutionBinding | None,
    lifecycle: StrategyLifecycle | None,
    intent_rows: list[TriggerProtectionIntent],
) -> bool:
    return bool(
        leg is not None
        and binding is not None
        and lifecycle is not None
        and len(intent_rows) == 1
        and int(leg.execution_binding_id) == target.execution_binding_id
        and int(lifecycle.execution_binding_id or 0) == target.execution_binding_id
        and str(leg.order_id or "") == target.order_id
        and str(leg.venue or "").lower() == "deepcoin"
        and leg.purpose == "entry"
        and str(binding.venue or "").lower() == "deepcoin"
        and str(binding.symbol or "").upper()
        == target.instrument_id.removesuffix("-USDT-SWAP")
        and str(binding.side or "").lower() == "long"
        and int(intent_rows[0].execution_binding_id)
        == target.execution_binding_id
        and str(intent_rows[0].parent_trigger_order_id or "")
        == target.order_id
        and str(intent_rows[0].request_fingerprint)
        == target.request_fingerprint
    )


def _local_pending_state_matches(
    session,
    *,
    target: ReviewedPendingEntryTarget,
    leg: ExecutionOrderLeg,
    lifecycle: StrategyLifecycle,
    intent: TriggerProtectionIntent,
) -> bool:
    if (
        str(leg.status or "").lower() not in _PENDING_LEG_STATES
        or leg.pos_id not in (None, "")
        or str(lifecycle.lifecycle_status or "") != "pending_entry"
        or str(intent.recovery_state or "") not in {"pending", "retrying"}
        or intent.adopted_order_id not in (None, "")
    ):
        return False
    primary = (
        session.query(PositionProtectionLeg)
        .filter(
            PositionProtectionLeg.venue == "deepcoin",
            PositionProtectionLeg.execution_order_leg_id
            == target.execution_order_leg_id,
            PositionProtectionLeg.role == "primary_stop",
        )
        .all()
    )
    backup = (
        session.query(PositionProtectionLeg)
        .filter(
            PositionProtectionLeg.venue == "deepcoin",
            PositionProtectionLeg.execution_order_leg_id
            == target.execution_order_leg_id,
            PositionProtectionLeg.role == "backup_stop",
        )
        .all()
    )
    convergence = (
        session.query(TriggerTakeProfitConvergence)
        .filter(
            TriggerTakeProfitConvergence.venue == "deepcoin",
            TriggerTakeProfitConvergence.execution_order_leg_id
            == target.execution_order_leg_id,
        )
        .all()
    )
    return bool(
        len(primary) == 1
        and _numbers_equal(
            primary[0].planned_trigger_price,
            target.embedded_stop_price,
        )
        and str(primary[0].status or "") in {"planned", "waiting_fill"}
        and len(backup) == 1
        and str(backup[0].status or "") in {"planned", "waiting_fill"}
        and len(convergence) == 1
        and str(convergence[0].status or "")
        in {"waiting_backup_stop", "waiting_position", "ready"}
    )


def _completed_state_matches(
    session,
    *,
    target: ReviewedPendingEntryTarget,
    leg: ExecutionOrderLeg,
    binding: ExecutionBinding,
    lifecycle: StrategyLifecycle,
    history_rows: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
) -> bool:
    events = (
        session.query(ExecutionEvent)
        .filter(
            ExecutionEvent.execution_binding_id
            == target.execution_binding_id,
            ExecutionEvent.order_id == target.order_id,
            ExecutionEvent.action
            == "cancel_reviewed_pending_entry",
            ExecutionEvent.status == "confirmed",
        )
        .all()
    )
    intents = (
        session.query(TriggerProtectionIntent)
        .filter(
            TriggerProtectionIntent.venue == "deepcoin",
            TriggerProtectionIntent.execution_order_leg_id
            == target.execution_order_leg_id,
        )
        .all()
    )
    protection = (
        session.query(PositionProtectionLeg)
        .filter(
            PositionProtectionLeg.venue == "deepcoin",
            PositionProtectionLeg.execution_order_leg_id
            == target.execution_order_leg_id,
        )
        .all()
    )
    convergence = (
        session.query(TriggerTakeProfitConvergence)
        .filter(
            TriggerTakeProfitConvergence.venue == "deepcoin",
            TriggerTakeProfitConvergence.execution_order_leg_id
            == target.execution_order_leg_id,
        )
        .all()
    )
    mutation_intents = (
        session.query(PositionMutationIntent)
        .filter(
            PositionMutationIntent.operation
            == "cancel_reviewed_pending_entry",
            PositionMutationIntent.order_id == target.order_id,
            PositionMutationIntent.status == "confirmed",
        )
        .all()
    )
    sibling_entry_legs = (
        session.query(ExecutionOrderLeg)
        .filter(
            ExecutionOrderLeg.execution_binding_id
            == target.execution_binding_id,
            ExecutionOrderLeg.purpose == "entry",
            ExecutionOrderLeg.id != target.execution_order_leg_id,
        )
        .all()
    )
    binding_should_be_terminal = all(
        str(row.status or "").lower() in _TERMINAL_LEG_STATES
        for row in sibling_entry_legs
    )
    event = events[0] if len(events) == 1 else None
    mutation = mutation_intents[0] if len(mutation_intents) == 1 else None
    stored_request = _json_object(leg.request_json)
    cancel_request = {
        "instId": target.instrument_id,
        "ordId": target.order_id,
    }
    event_before = _json_object(event.before_json if event is not None else None)
    event_after = _json_object(event.after_json if event is not None else None)
    exchange_row_fingerprint = str(
        event_before.get("exchange_row_fingerprint") or ""
    )
    plan_fingerprint = str(event_before.get("plan_fingerprint") or "")
    action_base = {
        "order_id": target.order_id,
        "instrument_id": target.instrument_id,
        "lifecycle_id": target.lifecycle_id,
        "execution_binding_id": target.execution_binding_id,
        "execution_order_leg_id": target.execution_order_leg_id,
        "strategy_instance_id": str(leg.strategy_instance_id or ""),
        "trigger_price": target.trigger_price,
        "size": target.size,
        "embedded_stop_price": target.embedded_stop_price,
        "request_fingerprint": target.request_fingerprint,
        "request_json_fingerprint": _fingerprint(stored_request),
        "exchange_row_fingerprint": exchange_row_fingerprint,
    }
    expected_action_id = _fingerprint(action_base)
    expected_authority_fingerprint = _fingerprint(
        {
            "action_id": expected_action_id,
            "plan_fingerprint": plan_fingerprint,
            "exchange_row_fingerprint": exchange_row_fingerprint,
            "request_json_fingerprint": _fingerprint(stored_request),
        }
    )
    exact_durable_evidence = bool(
        event is not None
        and mutation is not None
        and _is_sha256(plan_fingerprint)
        and _is_sha256(exchange_row_fingerprint)
        and event_before
        == {
            "plan_fingerprint": plan_fingerprint,
            "action_id": expected_action_id,
            "exchange_row_fingerprint": exchange_row_fingerprint,
        }
        and event_after == {"pending": False, "terminalized": True}
        and _json_object(event.request_json) == cancel_request
        and _json_object(event.response_json)
        == {"code": "0", "order_id": target.order_id}
        and event.execution_binding_id == target.execution_binding_id
        and event.strategy_instance_id == str(leg.strategy_instance_id or "")
        and event.venue == "deepcoin"
        and event.symbol == target.instrument_id.split("-")[0]
        and event.side == "long"
        and event.reason == "reviewed_stale_pending_entry_cancelled"
        and mutation.idempotency_key
        == (
            f"reviewed-pending-entry-cancel:{target.order_id}:"
            f"{expected_action_id}"
        )
        and mutation.strategy_instance_id
        == str(leg.strategy_instance_id or "")
        and mutation.venue == "deepcoin"
        and mutation.execution_binding_id == target.execution_binding_id
        and mutation.execution_order_leg_id
        == target.execution_order_leg_id
        and mutation.pos_id == f"pending-entry:{target.order_id}"
        and mutation.request_fingerprint == _fingerprint(cancel_request)
        and _json_object(mutation.request_json) == cancel_request
        and _json_object(mutation.response_json)
        == {"code": "0", "order_id": target.order_id}
        and mutation.error_json in (None, "")
        and mutation.submitted_at is not None
        and mutation.confirmed_at is not None
        and mutation.authority_fingerprint
        == expected_authority_fingerprint
    )
    return bool(
        not fill_rows
        and _history_has_cancelled_state(history_rows)
        and str(leg.status or "").lower() in _TERMINAL_LEG_STATES
        and leg.terminal_reason == "operator_cancelled_unfilled_entry_leg"
        and str(leg.venue or "").lower() == "deepcoin"
        and leg.purpose == "entry"
        and leg.pos_id in (None, "")
        and _request_matches(stored_request, target)
        and _fingerprint(stored_request) == target.request_fingerprint
        and len(events) == 1
        and len(mutation_intents) == 1
        and exact_durable_evidence
        and len(intents) == 1
        and intents[0].execution_binding_id == target.execution_binding_id
        and intents[0].parent_trigger_order_id == target.order_id
        and intents[0].recovery_state == "resolved"
        and intents[0].recovery_disposition == "terminal"
        and intents[0].last_reason_code
        == "parent_trigger_cancelled_before_entry"
        and len(protection) == 2
        and {row.role for row in protection}
        == {"primary_stop", "backup_stop"}
        and all(
            row.status == "cancelled"
            and row.execution_binding_id == target.execution_binding_id
            and row.parent_entry_order_id == target.order_id
            for row in protection
        )
        and len(convergence) == 1
        and convergence[0].execution_binding_id
        == target.execution_binding_id
        and convergence[0].status == "completed"
        and convergence[0].reason_code
        == "parent_trigger_cancelled_before_entry"
        and (
            (
                binding_should_be_terminal
                and str(binding.status or "").lower() == "cancelled"
                and binding.last_exchange_status
                == "reviewed_pending_entries_cancelled"
                and str(lifecycle.lifecycle_status or "") == "expired"
                and lifecycle.exit_reason == "expired"
                and lifecycle.management_action
                == "reviewed_pending_entries_cancelled"
            )
            or (
                not binding_should_be_terminal
                and str(binding.status or "").lower() in {"open", "active"}
                and str(lifecycle.lifecycle_status or "") == "pending_entry"
            )
        )
    )


def _exchange_row_matches(
    row: dict[str, Any],
    target: ReviewedPendingEntryTarget,
) -> bool:
    return bool(
        str(row.get("instId") or "").upper() == target.instrument_id
        and _order_id(row) == target.order_id
        and str(row.get("triggerOrderType") or "").lower() == "conditional"
        and str(row.get("side") or "").lower() == "buy"
        and str(row.get("posSide") or "").lower() == "long"
        and _numbers_equal(
            row.get("triggerPx") or row.get("triggerPrice"),
            target.trigger_price,
        )
        and _numbers_equal(row.get("sz") or row.get("size"), target.size)
        and _numbers_equal(
            row.get("closeSLTriggerPrice")
            or row.get("slTriggerPx")
            or row.get("slTriggerPrice"),
            target.embedded_stop_price,
        )
    )


def _request_matches(
    request: dict[str, Any],
    target: ReviewedPendingEntryTarget,
) -> bool:
    return bool(
        str(request.get("instId") or "").upper() == target.instrument_id
        and str(request.get("side") or "").lower() == "buy"
        and str(request.get("posSide") or "").lower() == "long"
        and _numbers_equal(
            request.get("triggerPrice") or request.get("triggerPx"),
            target.trigger_price,
        )
        and _numbers_equal(request.get("sz") or request.get("size"), target.size)
        and _numbers_equal(
            request.get("slTriggerPx")
            or request.get("slTriggerPrice")
            or request.get("closeSLTriggerPrice"),
            target.embedded_stop_price,
        )
    )


def _confirmed_cancel_response(response: Any, *, order_id: str) -> bool:
    if not isinstance(response, dict):
        return False
    codes = [response[key] for key in ("code", "sCode") if key in response]
    return bool(
        codes
        and all(str(code) in {"0", "0.0"} for code in codes)
        and _response_contains_order_id(response.get("data"), order_id)
    )


def _response_contains_order_id(value: Any, order_id: str) -> bool:
    if isinstance(value, str):
        return value == order_id
    if isinstance(value, dict):
        return any(
            str(value.get(key) or "") == order_id
            for key in ("ordId", "orderId", "order_id", "id")
        )
    if isinstance(value, list):
        return any(
            _response_contains_order_id(item, order_id) for item in value
        )
    return False


def _history_has_filled_state(rows: Iterable[dict[str, Any]]) -> bool:
    return any(_state(row) in _FILLED_HISTORY_STATES for row in rows)


def _history_has_cancelled_state(rows: Iterable[dict[str, Any]]) -> bool:
    return any(_state(row) in _CANCELLED_HISTORY_STATES for row in rows)


def _state(row: dict[str, Any]) -> str:
    return str(
        row.get("state")
        or row.get("status")
        or row.get("orderStatus")
        or ""
    ).lower()


def _matches_order(row: dict[str, Any], order_id: str) -> bool:
    return _order_id(row) == order_id


def _order_id(row: dict[str, Any]) -> str:
    return str(
        row.get("ordId")
        or row.get("orderId")
        or row.get("triggerOrderId")
        or row.get("order_id")
        or ""
    )


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _numbers_equal(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


def _conflict(
    target: ReviewedPendingEntryTarget,
    reason: str,
) -> dict[str, str]:
    return {"order_id": target.order_id, "reason": reason}


def _fingerprint(value: Any) -> str:
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plan(
    created_at: datetime,
    actions: Iterable[ReviewedPendingEntryCancelAction],
    conflicts: Iterable[dict[str, str]],
    *,
    completed_order_ids: Iterable[str] = (),
) -> ReviewedPendingEntryCancelPlan:
    ordered_actions = tuple(sorted(actions, key=lambda item: item.order_id))
    ordered_conflicts = tuple(
        sorted(
            (dict(item) for item in conflicts),
            key=lambda item: (item.get("order_id", ""), item.get("reason", "")),
        )
    )
    completed = tuple(sorted(str(value) for value in completed_order_ids))
    material = {
        "actions": [asdict(action) for action in ordered_actions],
        "conflicts": ordered_conflicts,
        "completed_order_ids": completed,
    }
    return ReviewedPendingEntryCancelPlan(
        created_at=created_at,
        actions=ordered_actions,
        conflicts=ordered_conflicts,
        completed_order_ids=completed,
        fingerprint=_fingerprint(material),
    )
