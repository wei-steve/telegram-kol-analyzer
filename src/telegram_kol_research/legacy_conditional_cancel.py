"""Fail-closed cancellation of explicitly reviewed legacy conditionals."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from telegram_kol_research.execution_events import (
    ExecutionEventRecord,
    record_execution_event,
)
from telegram_kol_research.models import (
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionBackupStopOrder,
    PositionMutationIntent,
    PositionProtectionLedger,
)
from telegram_kol_research.position_mutation_authority import (
    position_authority_fingerprint,
)
from telegram_kol_research.position_mutation_gateway import (
    exact_position_write_gate,
)
from telegram_kol_research.position_mutation_intents import (
    reserve_position_mutation_intent,
    transition_position_mutation_intent,
)
from telegram_kol_research.repair_confirmation import (
    consume_repair_confirmation_token,
    require_repair_confirmation_token_unused,
)


@dataclass(frozen=True, slots=True)
class ReviewedLegacyConditionalTarget:
    order_id: str
    pos_id: str
    trigger_price: str
    size: str
    native_stop_order_id: str
    native_stop_price: str
    instrument_id: str = "BTC-USDT-SWAP"
    side: str = "long"
    orphan: bool = False


@dataclass(frozen=True, slots=True)
class ReviewedLegacyConditionalCancelAction:
    order_id: str
    pos_id: str
    trigger_price: str
    size: str
    native_stop_order_id: str
    native_stop_price: str
    instrument_id: str
    side: str
    orphan: bool
    execution_binding_id: int
    execution_order_leg_id: int
    strategy_instance_id: str
    backup_row_id: int | None
    position_fingerprint: str
    native_pending_fingerprint: str
    native_stop_fingerprint: str
    action_id: str


@dataclass(frozen=True, slots=True)
class ReviewedLegacyConditionalCancelPlan:
    created_at: datetime
    actions: tuple[ReviewedLegacyConditionalCancelAction, ...]
    conflicts: tuple[dict[str, str], ...]
    completed_order_ids: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ReviewedLegacyConditionalCancelResult:
    status: str
    order_id: str
    pos_id: str
    reason_code: str | None = None


REVIEWED_LEGACY_CONDITIONAL_TARGETS = (
    ReviewedLegacyConditionalTarget(
        order_id="1001124328936694",
        pos_id="1001124305898960",
        trigger_price="61491",
        size="3",
        native_stop_order_id="1001124328944999",
        native_stop_price="61800",
        orphan=True,
    ),
    ReviewedLegacyConditionalTarget(
        order_id="1001124346855727",
        pos_id="1001124331806107",
        trigger_price="61676.4",
        size="4",
        native_stop_order_id="1001124352017389",
        native_stop_price="61800",
    ),
    ReviewedLegacyConditionalTarget(
        order_id="1001124346876836",
        pos_id="1001124331869718",
        trigger_price="60878",
        size="3",
        native_stop_order_id="1001124382877387",
        native_stop_price="63900",
    ),
    ReviewedLegacyConditionalTarget(
        order_id="1001124346889183",
        pos_id="1001124332479618",
        trigger_price="63073.6",
        size="16",
        native_stop_order_id="1001124332479617",
        native_stop_price="63200",
    ),
    ReviewedLegacyConditionalTarget(
        order_id="1001124346896177",
        pos_id="1001124331870537",
        trigger_price="62674.4",
        size="9",
        native_stop_order_id="1001124331870536",
        native_stop_price="62800",
    ),
    ReviewedLegacyConditionalTarget(
        order_id="1001124346908329",
        pos_id="1001124333585861",
        trigger_price="62804.1",
        size="20",
        native_stop_order_id="1001124385191201",
        native_stop_price="63895.725",
    ),
)


def build_reviewed_legacy_conditional_cancel_plan(
    session_factory,
    *,
    deepcoin_client,
    targets: Iterable[ReviewedLegacyConditionalTarget],
    now: datetime | None = None,
    recovering_order_id: str | None = None,
) -> ReviewedLegacyConditionalCancelPlan:
    """Build a fresh plan containing only exact, independently protected rows."""

    created_at = now or datetime.now(UTC)
    reviewed_targets = tuple(targets)
    try:
        positions = [
            row
            for row in deepcoin_client.list_positions(inst_id="BTC-USDT-SWAP")
            if isinstance(row, dict)
        ]
        pending = [
            row
            for row in deepcoin_client.list_trigger_orders_pending(
                inst_id="BTC-USDT-SWAP"
            )
            if isinstance(row, dict)
        ]
    except Exception:
        conflicts = tuple(
            {"order_id": target.order_id, "reason": "exchange_snapshot_unavailable"}
            for target in reviewed_targets
        )
        return _plan(created_at, (), conflicts)

    actions: list[ReviewedLegacyConditionalCancelAction] = []
    conflicts: list[dict[str, str]] = []
    completed_order_ids: list[str] = []
    with session_factory() as session:
        for target in reviewed_targets:
            completion = _completed_target_event(session, target)
            if completion is not None:
                if _completed_target_matches(
                    session,
                    target,
                    event=completion,
                    positions=positions,
                    pending=pending,
                ):
                    completed_order_ids.append(target.order_id)
                else:
                    conflicts.append(
                        _conflict(
                            target, "completed_target_state_changed"
                        )
                    )
                continue
            position_rows = [
                row
                for row in positions
                if _position_id(row) == target.pos_id
                and str(row.get("instId") or "").upper()
                == target.instrument_id.upper()
            ]
            if len(position_rows) != 1 or not _position_matches(
                position_rows[0], target
            ):
                conflicts.append(_conflict(target, "live_position_mismatch"))
                continue

            legacy_rows = [
                row for row in pending if _order_id(row) == target.order_id
            ]
            if len(legacy_rows) != 1 or not _legacy_matches(
                legacy_rows[0], target
            ):
                conflicts.append(_conflict(target, "legacy_payload_mismatch"))
                continue

            native_rows = [
                row
                for row in pending
                if _order_id(row) == target.native_stop_order_id
            ]
            if len(native_rows) != 1 or not _native_stop_matches(
                native_rows[0], target
            ):
                conflicts.append(_conflict(target, "native_stop_not_pending"))
                continue

            ledger_rows = (
                session.query(PositionProtectionLedger)
                .filter(
                    PositionProtectionLedger.venue == "deepcoin",
                    PositionProtectionLedger.order_id
                    == target.native_stop_order_id,
                    PositionProtectionLedger.pos_id == target.pos_id,
                    PositionProtectionLedger.status == "verified",
                )
                .all()
            )
            if len(ledger_rows) != 1:
                conflicts.append(_conflict(target, "native_stop_not_verified"))
                continue
            ledger = ledger_rows[0]
            if not _ledger_matches(ledger, target):
                conflicts.append(
                    _conflict(target, "native_stop_ledger_mismatch")
                )
                continue
            entry_leg = session.get(
                ExecutionOrderLeg, ledger.execution_order_leg_id
            )
            if (
                entry_leg is None
                or entry_leg.purpose != "entry"
                or entry_leg.status != "active"
                or entry_leg.attribution_status != "verified"
                or str(entry_leg.pos_id or "") != target.pos_id
                or int(entry_leg.execution_binding_id)
                != int(ledger.execution_binding_id)
            ):
                conflicts.append(
                    _conflict(target, "entry_leg_not_verified")
                )
                continue

            backup_row_id: int | None = None
            if not target.orphan:
                backup_rows = (
                    session.query(PositionBackupStopOrder)
                    .filter(
                        PositionBackupStopOrder.venue == "deepcoin",
                        PositionBackupStopOrder.order_id == target.order_id,
                        PositionBackupStopOrder.pos_id == target.pos_id,
                        PositionBackupStopOrder.status.in_(
                            ("active", "missing")
                            if target.order_id == recovering_order_id
                            else ("active",)
                        ),
                    )
                    .all()
                )
                if len(backup_rows) != 1 or not _backup_matches(
                    backup_rows[0], target
                ):
                    conflicts.append(
                        _conflict(target, "backup_ownership_mismatch")
                    )
                    continue
                backup_row_id = int(backup_rows[0].id)

            base = {
                "order_id": target.order_id,
                "pos_id": target.pos_id,
                "trigger_price": target.trigger_price,
                "size": target.size,
                "native_stop_order_id": target.native_stop_order_id,
                "native_stop_price": target.native_stop_price,
                "instrument_id": target.instrument_id.upper(),
                "side": target.side.lower(),
                "orphan": target.orphan,
                "execution_binding_id": int(ledger.execution_binding_id),
                "execution_order_leg_id": int(
                    ledger.execution_order_leg_id
                ),
                "strategy_instance_id": str(
                    entry_leg.strategy_instance_id or ""
                ),
                "backup_row_id": backup_row_id,
                "position_fingerprint": position_authority_fingerprint(
                    position_rows[0]
                ),
                "native_pending_fingerprint": _pending_row_fingerprint(
                    native_rows[0]
                ),
                "native_stop_fingerprint": _native_stop_fingerprint(
                    native_rows[0], ledger
                ),
            }
            actions.append(
                ReviewedLegacyConditionalCancelAction(
                    **base,
                    action_id=_fingerprint(base),
                )
            )

    return _plan(
        created_at,
        actions,
        conflicts,
        completed_order_ids=completed_order_ids,
    )


def apply_reviewed_legacy_conditional_cancel_plan(
    session_factory,
    plan: ReviewedLegacyConditionalCancelPlan,
    *,
    deepcoin_client,
    targets: Iterable[ReviewedLegacyConditionalTarget],
    pos_id: str,
    action_id: str,
    expected_fingerprint: str,
    confirmation_token: str,
    now: datetime | None = None,
) -> ReviewedLegacyConditionalCancelResult:
    """Cancel exactly one reviewed order and confirm exchange readback."""

    if expected_fingerprint != plan.fingerprint:
        raise ValueError("plan fingerprint mismatch")
    selected = [
        action
        for action in plan.actions
        if action.pos_id == str(pos_id) and action.action_id == str(action_id)
    ]
    if len(selected) != 1:
        raise ValueError("exactly one reviewed cancellation action is required")
    require_repair_confirmation_token_unused(
        session_factory, confirmation_token=confirmation_token
    )
    fresh = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=deepcoin_client,
        targets=targets,
        now=now,
    )
    if fresh.fingerprint != expected_fingerprint:
        raise ValueError("plan fingerprint changed")
    current = [
        action
        for action in fresh.actions
        if action.pos_id == str(pos_id) and action.action_id == str(action_id)
    ]
    if len(current) != 1:
        raise ValueError("reviewed cancellation action changed")
    action = current[0]
    observed_at = now or datetime.now(UTC)
    request = {"instId": action.instrument_id, "ordId": action.order_id}
    authority_fingerprint = _fingerprint(
        {
            "action_id": action.action_id,
            "position_fingerprint": action.position_fingerprint,
            "native_stop_fingerprint": action.native_stop_fingerprint,
        }
    )
    intent = reserve_position_mutation_intent(
        session_factory,
        idempotency_key=(
            f"reviewed-legacy-cancel:{action.order_id}:{action.action_id}"
        ),
        operation="cancel_trigger_order",
        strategy_instance_id=action.strategy_instance_id,
        execution_binding_id=action.execution_binding_id,
        execution_order_leg_id=action.execution_order_leg_id,
        pos_id=action.pos_id,
        order_id=action.order_id,
        authority_fingerprint=authority_fingerprint,
        request_fingerprint=_fingerprint(request),
        request=request,
        reserved_at=observed_at,
        venue="deepcoin",
    )
    intent_id = int(intent.id)
    if intent.status != "reserved":
        return ReviewedLegacyConditionalCancelResult(
            f"intent_{intent.status}",
            action.order_id,
            action.pos_id,
            str(intent.status),
        )
    consume_repair_confirmation_token(
        session_factory,
        confirmation_token=confirmation_token,
        action_kind="cancel_reviewed_legacy_conditional",
        action_id=action.action_id,
        pos_id=action.pos_id,
        consumed_at=observed_at,
    )
    if not exact_position_write_gate(
        session_factory, pos_id=action.pos_id
    ):
        transition_position_mutation_intent(
            session_factory,
            intent_id,
            expected_statuses={"reserved"},
            new_status="blocked",
            transitioned_at=observed_at,
            error={"reason": "exact_position_write_gate_blocked"},
        )
        return ReviewedLegacyConditionalCancelResult(
            "blocked",
            action.order_id,
            action.pos_id,
            "exact_position_write_gate_blocked",
        )
    if not transition_position_mutation_intent(
        session_factory,
        intent_id,
        expected_statuses={"reserved"},
        new_status="submitting",
        transitioned_at=observed_at,
    ):
        return _intent_result(session_factory, intent_id, action)
    try:
        response = deepcoin_client.cancel_trigger_order(request)
    except Exception as exc:
        transition_position_mutation_intent(
            session_factory,
            intent_id,
            expected_statuses={"submitting"},
            new_status="recovery_required",
            transitioned_at=observed_at,
            error={"reason": "cancel_outcome_unknown", "error": str(exc)[:512]},
        )
        _record(
            session_factory,
            action,
            status="unknown",
            reason="cancel_outcome_unknown",
            request=request,
            response={"error": str(exc)[:512]},
            now=observed_at,
        )
        return ReviewedLegacyConditionalCancelResult(
            "cancel_outcome_unknown",
            action.order_id,
            action.pos_id,
            "cancel_outcome_unknown",
        )
    if not _confirmed_cancel_response(response, order_id=action.order_id):
        transition_position_mutation_intent(
            session_factory,
            intent_id,
            expected_statuses={"submitting"},
            new_status="recovery_required",
            transitioned_at=observed_at,
            response=_response_evidence(response),
            error={"reason": "cancel_response_unconfirmed"},
        )
        _record(
            session_factory,
            action,
            status="unconfirmed",
            reason="cancel_response_unconfirmed",
            request=request,
            response=_response_evidence(response),
            now=observed_at,
        )
        return ReviewedLegacyConditionalCancelResult(
            "cancel_unconfirmed",
            action.order_id,
            action.pos_id,
            "cancel_response_unconfirmed",
        )
    transition_position_mutation_intent(
        session_factory,
        intent_id,
        expected_statuses={"submitting"},
        new_status="submitted",
        transitioned_at=observed_at,
        response=_response_evidence(response),
    )

    try:
        positions = [
            row
            for row in deepcoin_client.list_positions(
                inst_id=action.instrument_id
            )
            if isinstance(row, dict)
        ]
        pending = [
            row
            for row in deepcoin_client.list_trigger_orders_pending(
                inst_id=action.instrument_id
            )
            if isinstance(row, dict)
        ]
    except Exception as exc:
        _record(
            session_factory,
            action,
            status="confirmed_pending_readback",
            reason="cancel_readback_unavailable",
            request=request,
            response={
                "cancel_response": _response_evidence(response),
                "error": str(exc)[:512],
            },
            now=observed_at,
        )
        return ReviewedLegacyConditionalCancelResult(
            "cancel_confirmed_pending_readback",
            action.order_id,
            action.pos_id,
            "cancel_readback_unavailable",
        )

    position_rows = [
        row for row in positions if _position_id(row) == action.pos_id
    ]
    legacy_rows = [
        row for row in pending if _order_id(row) == action.order_id
    ]
    native_rows = [
        row
        for row in pending
        if _order_id(row) == action.native_stop_order_id
    ]
    action_target = ReviewedLegacyConditionalTarget(
        order_id=action.order_id,
        pos_id=action.pos_id,
        trigger_price=action.trigger_price,
        size=action.size,
        native_stop_order_id=action.native_stop_order_id,
        native_stop_price=action.native_stop_price,
        instrument_id=action.instrument_id,
        side=action.side,
        orphan=action.orphan,
    )
    if legacy_rows:
        _record(
            session_factory,
            action,
            status="submitted_pending_readback",
            reason="cancel_pending_readback",
            request=request,
            response=_response_evidence(response),
            now=observed_at,
        )
        return ReviewedLegacyConditionalCancelResult(
            "cancel_pending_readback",
            action.order_id,
            action.pos_id,
            "cancel_pending_readback",
        )
    if (
        len(position_rows) != 1
        or not _position_matches(position_rows[0], action_target)
        or position_authority_fingerprint(position_rows[0])
        != action.position_fingerprint
        or len(native_rows) != 1
        or not _native_stop_matches(native_rows[0], action_target)
        or _pending_row_fingerprint(native_rows[0])
        != action.native_pending_fingerprint
    ):
        _mark_confirmed_cancelled_row(
            session_factory,
            action,
            response=response,
            now=observed_at,
            require_active=False,
        )
        transition_position_mutation_intent(
            session_factory,
            intent_id,
            expected_statuses={"submitted"},
            new_status="confirmed",
            transitioned_at=observed_at,
            response=_response_evidence(response),
            error={"reason": "post_cancel_state_changed"},
        )
        _record(
            session_factory,
            action,
            status="confirmed_readback_changed",
            reason="post_cancel_state_changed",
            request=request,
            response=_response_evidence(response),
            now=observed_at,
        )
        return ReviewedLegacyConditionalCancelResult(
            "cancel_confirmed_readback_changed",
            action.order_id,
            action.pos_id,
            "post_cancel_state_changed",
        )

    post_plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=_SnapshotClient(positions, pending),
        targets=targets,
        now=observed_at,
    )
    expected_remaining = {
        item.action_id
        for item in plan.actions
        if item.order_id != action.order_id
    }
    observed_remaining = {item.action_id for item in post_plan.actions}
    allowed_selected_conflict = {
        ("legacy_payload_mismatch", action.order_id)
    }
    unexpected_conflicts = {
        (item.get("reason", ""), item.get("order_id", ""))
        for item in post_plan.conflicts
    } - allowed_selected_conflict
    if (
        expected_remaining != observed_remaining
        or unexpected_conflicts
    ):
        _mark_confirmed_cancelled_row(
            session_factory,
            action,
            response=response,
            now=observed_at,
            require_active=False,
        )
        transition_position_mutation_intent(
            session_factory,
            intent_id,
            expected_statuses={"submitted"},
            new_status="confirmed",
            transitioned_at=observed_at,
            response=_response_evidence(response),
            error={"reason": "post_cancel_state_changed"},
        )
        _record(
            session_factory,
            action,
            status="confirmed_readback_changed",
            reason="post_cancel_state_changed",
            request=request,
            response=_response_evidence(response),
            now=observed_at,
        )
        return ReviewedLegacyConditionalCancelResult(
            "cancel_confirmed_readback_changed",
            action.order_id,
            action.pos_id,
            "post_cancel_state_changed",
        )

    with session_factory() as session:
        if action.backup_row_id is not None:
            backup = session.get(
                PositionBackupStopOrder, action.backup_row_id
            )
            if (
                backup is None
                or backup.status != "active"
                or backup.order_id != action.order_id
            ):
                session.rollback()
                transition_position_mutation_intent(
                    session_factory,
                    intent_id,
                    expected_statuses={"submitted"},
                    new_status="confirmed",
                    transitioned_at=observed_at,
                    response=_response_evidence(response),
                    error={
                        "reason": "confirmed_cancel_database_state_changed"
                    },
                )
                _record(
                    session_factory,
                    action,
                    status="confirmed_audit_state_changed",
                    reason="confirmed_cancel_database_state_changed",
                    request=request,
                    response=_response_evidence(response),
                    now=observed_at,
                )
                return ReviewedLegacyConditionalCancelResult(
                    "cancelled_audit_state_changed",
                    action.order_id,
                    action.pos_id,
                    "confirmed_cancel_database_state_changed",
                )
            backup.status = "cancelled"
            backup.completed_at = observed_at
            backup.updated_at = observed_at
        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                action="cancel_reviewed_legacy_conditional",
                status="confirmed",
                execution_binding_id=action.execution_binding_id,
                venue="deepcoin",
                symbol=action.instrument_id.split("-")[0],
                side=action.side,
                order_id=action.order_id,
                pos_id=action.pos_id,
                related_order_id=action.native_stop_order_id,
                reason="reviewed_legacy_conditional_cancelled",
                before={
                    "trigger_price": action.trigger_price,
                    "size": action.size,
                    "orphan": action.orphan,
                    "plan_fingerprint": plan.fingerprint,
                    "position_fingerprint": action.position_fingerprint,
                    "native_pending_fingerprint": (
                        action.native_pending_fingerprint
                    ),
                    "native_stop_fingerprint": (
                        action.native_stop_fingerprint
                    ),
                    "native_stop_order_id": action.native_stop_order_id,
                    "native_stop_price": action.native_stop_price,
                },
                after={
                    "legacy_pending": False,
                    "native_stop_pending": True,
                },
                request=request,
                response=_response_evidence(response),
                created_at=observed_at,
            ),
            session=session,
        )
        session.commit()
    transition_position_mutation_intent(
        session_factory,
        intent_id,
        expected_statuses={"submitted"},
        new_status="confirmed",
        transitioned_at=observed_at,
        response=_response_evidence(response),
    )
    return ReviewedLegacyConditionalCancelResult(
        "cancelled", action.order_id, action.pos_id
    )


def reconcile_submitted_reviewed_legacy_conditional_cancel(
    session_factory,
    *,
    deepcoin_client,
    target: ReviewedLegacyConditionalTarget,
    reviewed_plan_fingerprint: str,
    now: datetime | None = None,
) -> ReviewedLegacyConditionalCancelResult:
    """Confirm a submitted cancel from a later exact readback without retrying."""

    observed_at = now or datetime.now(UTC)
    clean_reviewed_fingerprint = str(reviewed_plan_fingerprint or "").strip()
    if len(clean_reviewed_fingerprint) != 64:
        raise ValueError("reviewed_plan_fingerprint is required")
    with session_factory() as session:
        intents = (
            session.query(PositionMutationIntent)
            .filter(
                PositionMutationIntent.venue == "deepcoin",
                PositionMutationIntent.operation == "cancel_trigger_order",
                PositionMutationIntent.order_id == target.order_id,
                PositionMutationIntent.pos_id == target.pos_id,
                PositionMutationIntent.status == "submitted",
            )
            .all()
        )
    if len(intents) != 1:
        raise ValueError("exactly one submitted cancel intent is required")
    intent = intents[0]
    response = _load_json(intent.response_json)
    if not _confirmed_cancel_response(response, order_id=target.order_id):
        raise ValueError("submitted cancel response is not explicitly confirmed")
    try:
        positions = [
            row
            for row in deepcoin_client.list_positions(
                inst_id=target.instrument_id
            )
            if isinstance(row, dict)
        ]
        pending = [
            row
            for row in deepcoin_client.list_trigger_orders_pending(
                inst_id=target.instrument_id
            )
            if isinstance(row, dict)
        ]
    except Exception as exc:
        raise ValueError("cancel reconciliation snapshot unavailable") from exc
    if any(_order_id(row) == target.order_id for row in pending):
        raise ValueError("cancelled legacy order is still pending")
    synthetic_pending = [
        *pending,
        {
            "ordId": target.order_id,
            "instId": target.instrument_id,
            "triggerOrderType": "Conditional",
            "posSide": target.side,
            "side": "sell" if target.side == "long" else "buy",
            "sz": target.size,
            "triggerPx": target.trigger_price,
        },
    ]
    candidate_plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=_SnapshotClient(positions, synthetic_pending),
        targets=(target,),
        now=observed_at,
        recovering_order_id=target.order_id,
    )
    if candidate_plan.conflicts or len(candidate_plan.actions) != 1:
        raise ValueError("cancel reconciliation authority changed")
    action = candidate_plan.actions[0]
    authority_fingerprint = _fingerprint(
        {
            "action_id": action.action_id,
            "position_fingerprint": action.position_fingerprint,
            "native_stop_fingerprint": action.native_stop_fingerprint,
        }
    )
    request = {"instId": action.instrument_id, "ordId": action.order_id}
    if (
        str(intent.authority_fingerprint or "") != authority_fingerprint
        or str(intent.request_fingerprint or "") != _fingerprint(request)
        or int(intent.execution_binding_id) != action.execution_binding_id
        or int(intent.execution_order_leg_id)
        != action.execution_order_leg_id
        or str(intent.strategy_instance_id or "")
        != action.strategy_instance_id
        or not exact_position_write_gate(
            session_factory, pos_id=action.pos_id
        )
    ):
        raise ValueError("cancel reconciliation fingerprint changed")

    with session_factory() as session:
        persisted_intent = session.get(PositionMutationIntent, int(intent.id))
        backup = (
            session.get(PositionBackupStopOrder, action.backup_row_id)
            if action.backup_row_id is not None
            else None
        )
        if (
            persisted_intent is None
            or persisted_intent.status != "submitted"
            or (
                action.backup_row_id is not None
                and (
                    backup is None
                    or backup.status not in {"active", "missing"}
                    or backup.order_id != action.order_id
                )
            )
        ):
            raise ValueError("cancel reconciliation database state changed")
        if backup is not None:
            backup.status = "cancelled"
            backup.completed_at = observed_at
            backup.updated_at = observed_at
            backup.error_json = json.dumps(
                {
                    "reason": "reviewed_legacy_conditional_cancelled",
                    "cancel_response": response,
                    "reconciled_from_later_readback": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        persisted_intent.status = "confirmed"
        persisted_intent.confirmed_at = observed_at
        persisted_intent.updated_at = observed_at
        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                action="cancel_reviewed_legacy_conditional",
                status="confirmed",
                execution_binding_id=action.execution_binding_id,
                venue="deepcoin",
                symbol=action.instrument_id.split("-")[0],
                side=action.side,
                order_id=action.order_id,
                pos_id=action.pos_id,
                related_order_id=action.native_stop_order_id,
                reason="reviewed_legacy_conditional_cancelled",
                before={
                    "trigger_price": action.trigger_price,
                    "size": action.size,
                    "orphan": action.orphan,
                    "operator_supplied_reviewed_plan_fingerprint": (
                        clean_reviewed_fingerprint
                    ),
                    "recovery_candidate_fingerprint": (
                        candidate_plan.fingerprint
                    ),
                    "position_fingerprint": action.position_fingerprint,
                    "native_pending_fingerprint": (
                        action.native_pending_fingerprint
                    ),
                    "native_stop_fingerprint": (
                        action.native_stop_fingerprint
                    ),
                    "native_stop_order_id": action.native_stop_order_id,
                    "native_stop_price": action.native_stop_price,
                },
                after={
                    "legacy_pending": False,
                    "native_stop_pending": True,
                    "reconciled_from_later_readback": True,
                },
                request=request,
                response=response,
                created_at=observed_at,
            ),
            session=session,
        )
        session.commit()
    return ReviewedLegacyConditionalCancelResult(
        "cancelled", action.order_id, action.pos_id
    )


def _plan(
    created_at: datetime,
    actions: Iterable[ReviewedLegacyConditionalCancelAction],
    conflicts: Iterable[dict[str, str]],
    *,
    completed_order_ids: Iterable[str] = (),
) -> ReviewedLegacyConditionalCancelPlan:
    action_tuple = tuple(sorted(actions, key=lambda item: item.order_id))
    conflict_tuple = tuple(
        sorted(
            conflicts,
            key=lambda item: (item.get("order_id", ""), item.get("reason", "")),
        )
    )
    completed_tuple = tuple(sorted(set(completed_order_ids)))
    return ReviewedLegacyConditionalCancelPlan(
        created_at=created_at,
        actions=action_tuple,
        conflicts=conflict_tuple,
        completed_order_ids=completed_tuple,
        fingerprint=_fingerprint(
            {
                "actions": [asdict(action) for action in action_tuple],
                "conflicts": list(conflict_tuple),
                "completed_order_ids": list(completed_tuple),
            }
        ),
    )


def _position_matches(
    row: dict[str, Any], target: ReviewedLegacyConditionalTarget
) -> bool:
    return (
        _position_id(row) == target.pos_id
        and str(row.get("instId") or "").upper()
        == target.instrument_id.upper()
        and str(row.get("posSide") or row.get("side") or "").lower()
        == target.side.lower()
        and (_decimal(row.get("pos") or row.get("size")) or Decimal("0")) > 0
    )


def _completed_target_event(
    session,
    target: ReviewedLegacyConditionalTarget,
) -> ExecutionEvent | None:
    event = (
        session.query(ExecutionEvent)
        .filter(
            ExecutionEvent.action
            == "cancel_reviewed_legacy_conditional",
            ExecutionEvent.status == "confirmed",
            ExecutionEvent.order_id == target.order_id,
            ExecutionEvent.pos_id == target.pos_id,
        )
        .one_or_none()
    )
    if event is None:
        return None
    if target.orphan:
        return event
    backup = (
        session.query(PositionBackupStopOrder)
        .filter(
            PositionBackupStopOrder.venue == "deepcoin",
            PositionBackupStopOrder.order_id == target.order_id,
            PositionBackupStopOrder.pos_id == target.pos_id,
        )
        .one_or_none()
    )
    return event if backup is not None and backup.status == "cancelled" else None


def _completed_target_matches(
    session,
    target: ReviewedLegacyConditionalTarget,
    *,
    event: ExecutionEvent,
    positions: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> bool:
    try:
        before = json.loads(event.before_json or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(before, dict):
        return False
    position_rows = [
        row
        for row in positions
        if _position_id(row) == target.pos_id
        and _position_matches(row, target)
    ]
    legacy_rows = [
        row for row in pending if _order_id(row) == target.order_id
    ]
    native_rows = [
        row
        for row in pending
        if _order_id(row) == target.native_stop_order_id
        and _native_stop_matches(row, target)
    ]
    ledger_rows = (
        session.query(PositionProtectionLedger)
        .filter(
            PositionProtectionLedger.venue == "deepcoin",
            PositionProtectionLedger.order_id
            == target.native_stop_order_id,
            PositionProtectionLedger.pos_id == target.pos_id,
            PositionProtectionLedger.status == "verified",
        )
        .all()
    )
    if (
        len(position_rows) != 1
        or legacy_rows
        or len(native_rows) != 1
        or len(ledger_rows) != 1
        or not _ledger_matches(ledger_rows[0], target)
    ):
        return False
    return (
        str(before.get("position_fingerprint") or "")
        == position_authority_fingerprint(position_rows[0])
        and str(before.get("native_pending_fingerprint") or "")
        == _pending_row_fingerprint(native_rows[0])
        and str(before.get("native_stop_fingerprint") or "")
        == _native_stop_fingerprint(native_rows[0], ledger_rows[0])
        and str(before.get("native_stop_order_id") or "")
        == target.native_stop_order_id
        and _decimal(before.get("native_stop_price"))
        == _decimal(target.native_stop_price)
    )


def _pending_row_fingerprint(row: dict[str, Any]) -> str:
    keys = (
        "ordId",
        "instId",
        "triggerOrderType",
        "posId",
        "closePosId",
        "posSide",
        "side",
        "sz",
        "triggerPx",
        "triggerPrice",
        "slTriggerPx",
        "slTriggerPrice",
        "slOrderPx",
        "orderType",
    )
    return _fingerprint(
        {key: str(row.get(key) or "") for key in keys}
    )


def _native_stop_fingerprint(
    row: dict[str, Any],
    ledger: PositionProtectionLedger,
) -> str:
    return _fingerprint(
        {
            "pending": _pending_row_fingerprint(row),
            "ledger": {
                "venue": str(ledger.venue or "").lower(),
                "order_id": str(ledger.order_id or ""),
                "execution_binding_id": int(ledger.execution_binding_id),
                "execution_order_leg_id": int(
                    ledger.execution_order_leg_id
                ),
                "strategy_instance_id": str(
                    ledger.strategy_instance_id or ""
                ),
                "pos_id": str(ledger.pos_id or ""),
                "instrument_id": str(ledger.instrument_id or "").upper(),
                "side": str(ledger.side or "").lower(),
                "purpose": str(ledger.purpose or ""),
                "trigger_price": str(ledger.trigger_price or ""),
                "size_text": str(ledger.size_text or ""),
                "status": str(ledger.status or "").lower(),
                "evidence_source": str(ledger.evidence_source or ""),
            },
        }
    )


def _legacy_matches(
    row: dict[str, Any], target: ReviewedLegacyConditionalTarget
) -> bool:
    return (
        _order_id(row) == target.order_id
        and str(row.get("instId") or "").upper()
        == target.instrument_id.upper()
        and str(row.get("triggerOrderType") or "").lower() == "conditional"
        and str(row.get("posSide") or "").lower() == target.side.lower()
        and str(row.get("side") or "").lower()
        == ("sell" if target.side.lower() == "long" else "buy")
        and _decimal(row.get("triggerPx") or row.get("triggerPrice"))
        == _decimal(target.trigger_price)
        and _decimal(row.get("sz") or row.get("size"))
        == _decimal(target.size)
    )


def _native_stop_matches(
    row: dict[str, Any], target: ReviewedLegacyConditionalTarget
) -> bool:
    return (
        _order_id(row) == target.native_stop_order_id
        and str(row.get("instId") or "").upper()
        == target.instrument_id.upper()
        and str(row.get("triggerOrderType") or "").upper() == "TPSL"
        and str(row.get("posSide") or "").lower() == target.side.lower()
        and str(row.get("side") or "").lower()
        == ("sell" if target.side.lower() == "long" else "buy")
        and _decimal(
            row.get("slTriggerPrice") or row.get("slTriggerPx")
        )
        == _decimal(target.native_stop_price)
        and (
            not str(row.get("posId") or "")
            or str(row.get("posId")) == target.pos_id
        )
    )


def _backup_matches(
    row: PositionBackupStopOrder, target: ReviewedLegacyConditionalTarget
) -> bool:
    try:
        request = json.loads(row.request_json or "{}")
    except (TypeError, ValueError):
        return False
    return (
        isinstance(request, dict)
        and str(row.order_id or "") == target.order_id
        and str(row.pos_id or "") == target.pos_id
        and str(row.instrument_id or "").upper()
        == target.instrument_id.upper()
        and str(row.side or "").lower() == target.side.lower()
        and _decimal(row.trigger_price) == _decimal(target.trigger_price)
        and str(request.get("closePosId") or "") == target.pos_id
        and str(request.get("instId") or "").upper()
        == target.instrument_id.upper()
        and str(request.get("orderType") or "").lower() == "market"
        and str(request.get("posSide") or "").lower()
        == target.side.lower()
        and str(request.get("side") or "").lower()
        == ("sell" if target.side.lower() == "long" else "buy")
        and str(request.get("mrgPosition") or "").lower() == "split"
        and _decimal(request.get("triggerPrice"))
        == _decimal(target.trigger_price)
        and _decimal(request.get("sz")) == _decimal(target.size)
    )


def _ledger_matches(
    row: PositionProtectionLedger,
    target: ReviewedLegacyConditionalTarget,
) -> bool:
    return (
        str(row.venue or "").lower() == "deepcoin"
        and str(row.order_id or "") == target.native_stop_order_id
        and str(row.pos_id or "") == target.pos_id
        and str(row.instrument_id or "").upper()
        == target.instrument_id.upper()
        and str(row.side or "").lower() == target.side.lower()
        and str(row.status or "").lower() == "verified"
        and str(row.purpose or "")
        in {"stop_loss", "supervised_current_tpsl"}
        and (
            not str(row.trigger_price or "")
            or _decimal(row.trigger_price)
            == _decimal(target.native_stop_price)
        )
    )


def _confirmed_cancel_response(response: Any, *, order_id: str) -> bool:
    if not isinstance(response, dict):
        return False
    codes = [response[key] for key in ("code", "sCode") if key in response]
    return (
        bool(codes)
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


def _record(
    session_factory,
    action: ReviewedLegacyConditionalCancelAction,
    *,
    status: str,
    reason: str,
    request: dict[str, Any],
    response: dict[str, Any],
    now: datetime,
) -> None:
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            action="cancel_reviewed_legacy_conditional",
            status=status,
            execution_binding_id=action.execution_binding_id,
            venue="deepcoin",
            symbol=action.instrument_id.split("-")[0],
            side=action.side,
            order_id=action.order_id,
            pos_id=action.pos_id,
            related_order_id=action.native_stop_order_id,
            reason=reason,
            request=request,
            response=response,
            created_at=now,
        ),
    )


def _mark_confirmed_cancelled_row(
    session_factory,
    action: ReviewedLegacyConditionalCancelAction,
    *,
    response: Any,
    now: datetime,
    require_active: bool,
) -> bool:
    if action.backup_row_id is None:
        return True
    with session_factory() as session:
        row = session.get(PositionBackupStopOrder, action.backup_row_id)
        if (
            row is None
            or row.order_id != action.order_id
            or (require_active and row.status != "active")
        ):
            return False
        if row.status == "active":
            row.status = "cancelled"
            row.completed_at = now
            row.updated_at = now
            row.error_json = json.dumps(
                {
                    "reason": "reviewed_legacy_conditional_cancelled",
                    "cancel_response": _response_evidence(response),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            session.commit()
        return row.status == "cancelled"


def _intent_result(
    session_factory,
    intent_id: int,
    action: ReviewedLegacyConditionalCancelAction,
) -> ReviewedLegacyConditionalCancelResult:
    with session_factory() as session:
        row = session.get(PositionMutationIntent, intent_id)
        if row is None:
            raise RuntimeError("position_mutation_intent_missing")
        return ReviewedLegacyConditionalCancelResult(
            f"intent_{row.status}",
            action.order_id,
            action.pos_id,
            str(row.status),
        )


class _SnapshotClient:
    def __init__(
        self,
        positions: list[dict[str, Any]],
        pending: list[dict[str, Any]],
    ) -> None:
        self._positions = list(positions)
        self._pending = list(pending)

    def list_positions(self, *, inst_id=None):
        return list(self._positions)

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self._pending)


def _response_evidence(response: Any) -> dict[str, Any]:
    return response if isinstance(response, dict) else {"response": repr(response)[:512]}


def _load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _position_id(row: dict[str, Any]) -> str:
    return str(row.get("posId") or row.get("pos_id") or row.get("id") or "")


def _order_id(row: dict[str, Any]) -> str:
    return str(
        row.get("ordId")
        or row.get("orderId")
        or row.get("order_id")
        or ""
    )


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _conflict(
    target: ReviewedLegacyConditionalTarget, reason: str
) -> dict[str, str]:
    return {"order_id": target.order_id, "reason": reason}


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
