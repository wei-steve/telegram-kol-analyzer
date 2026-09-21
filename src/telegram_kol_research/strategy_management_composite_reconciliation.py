"""Read-before-write recovery for durable composite management components."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram_kol_research.models import (
    PositionMutationIntent,
    PositionProtectionLedger,
    StrategyManagementComponent,
)
from telegram_kol_research.position_mutation_gateway import (
    reconcile_submitted_position_mutation_intents,
)
from telegram_kol_research.position_mutation_intents import (
    transition_position_mutation_intent,
)
from telegram_kol_research.strategy_management_components import (
    transition_management_component,
)
from telegram_kol_research.strategy_management_composite_executor import (
    REMAINDER_CLOSE_EXECUTION_KEY,
    REMAINDER_CLOSE_OUTCOME,
)
from telegram_kol_research.strategy_management_reconciliation import (
    classify_composite_close_reconciliation,
)


@dataclass(frozen=True, slots=True)
class CompositeReconciliationResult:
    reconciled: int = 0
    awaiting: int = 0
    recoverable: int = 0


def has_recoverable_composite_components(session_factory) -> bool:
    with session_factory() as session:
        return session.query(StrategyManagementComponent.id).filter(
            StrategyManagementComponent.status.in_(("submitting", "awaiting_exchange"))
        ).first() is not None


def reconcile_composite_management_components(
    session_factory,
    *,
    deepcoin_client: Any,
    reconciled_at: Any,
    allow_new_writes: bool = False,
) -> CompositeReconciliationResult:
    """Resolve protected components from complete exchange evidence only.

    ``allow_new_writes`` is intentionally accepted for worker policy routing,
    but this reconciler remains read-only. A later executor tick may claim only
    components moved to ``recovery_required`` by terminal evidence.
    """

    del allow_new_writes
    with session_factory() as session:
        components = session.query(StrategyManagementComponent).filter(
            StrategyManagementComponent.status.in_(("submitting", "awaiting_exchange"))
        ).order_by(StrategyManagementComponent.id.asc()).all()
        identities = [(row.id, row.component_kind) for row in components]
    if not identities:
        return CompositeReconciliationResult()
    snapshots: dict[str, dict[str, list]] = {}
    counts = {"reconciled": 0, "awaiting": 0, "recoverable": 0}
    for component_id, kind in identities:
        with session_factory() as session:
            component = session.get(StrategyManagementComponent, component_id)
            if component is None or component.status not in {"submitting", "awaiting_exchange"}:
                continue
            desired = json.loads(component.desired_json or "{}")
            instrument_id = _component_instrument(session, component, desired)
            current_status = component.status
        if not instrument_id:
            counts["awaiting"] += 1
            continue
        snapshot = snapshots.get(instrument_id)
        if snapshot is None:
            try:
                snapshot = _complete_snapshot(deepcoin_client, instrument_id)
            except Exception:
                counts["awaiting"] += 1
                continue
            snapshots[instrument_id] = snapshot
        reconcile_submitted_position_mutation_intents(
            session_factory,
            pending_trigger_orders=snapshot["pending"],
            order_history=snapshot["history"],
            trade_fills=snapshot["fills"],
            reconciled_at=reconciled_at,
        )
        if kind == "consume_take_profit_stage":
            outcome = _reconcile_tp_component(
                session_factory, component_id, desired, current_status, reconciled_at
            )
        elif kind == "converge_partial_close":
            outcome = _reconcile_close_component(
                session_factory, component_id, desired, current_status,
                snapshot["positions"], reconciled_at,
            )
        elif kind == "replace_remaining_protection":
            outcome = _reconcile_protection_component(
                session_factory, component_id, desired, current_status,
                snapshot["positions"], reconciled_at,
            )
        else:
            outcome = "awaiting"
        counts[outcome] += 1
    return CompositeReconciliationResult(**counts)


def _reconcile_tp_component(session_factory, component_id, desired, status, now):
    execution = desired.get("take_profit_consumption_execution") or {}
    intent_ids = [int(value) for value in execution.get("cancel_intent_ids") or []]
    with session_factory() as session:
        persisted = session.query(PositionMutationIntent).filter(
            PositionMutationIntent.idempotency_key.like(
                f"{int(component_id)}:cancel:%"
            )
        ).all()
        intents = {
            int(row.id): row for row in persisted
        }
        for value in intent_ids:
            row = session.get(PositionMutationIntent, value)
            if row is not None:
                intents[int(row.id)] = row
        intent_statuses = {row.status for row in intents.values()}
    planned_ids = {
        str(value) for value in execution.get("cancel_order_ids") or []
    }
    reserved = [row for row in intents.values() if row.status == "reserved"]
    if reserved:
        for row in reserved:
            transition_position_mutation_intent(
                session_factory,
                row.id,
                expected_statuses={"reserved"},
                new_status="blocked",
                transitioned_at=now,
                error={"reason": "component_restart_before_exchange_submit"},
            )
        _component_transition(
            session_factory, component_id, status, "recovery_required", now,
            "take_profit_reserved_before_write",
        )
        return "recoverable"
    if intent_statuses.intersection(
        {"submitting", "submitted", "recovery_required"}
    ):
        return "awaiting"
    confirmed_order_ids = {
        str(row.order_id)
        for row in intents.values()
        if row.order_id and row.status == "confirmed"
    }
    if intents and planned_ids and planned_ids.issubset(confirmed_order_ids):
        _component_transition(session_factory, component_id, status, "confirmed", now,
                              "take_profit_cancel_exchange_confirmed")
        return "reconciled"
    if intents and intent_statuses.issubset({"confirmed", "rejected", "blocked"}):
        _component_transition(session_factory, component_id, status, "recovery_required", now,
                              "take_profit_cancel_terminal_incomplete")
        return "recoverable"
    return "awaiting"


def _reconcile_close_component(
    session_factory, component_id, desired, status, positions, now
):
    execution = desired.get("partial_close_execution") or {}
    intent_id = execution.get("intent_id")
    if not intent_id:
        return "awaiting"
    with session_factory() as session:
        intent = session.get(PositionMutationIntent, int(intent_id))
    if intent is None:
        return "awaiting"
    if intent.status == "reserved":
        # A reserved gateway intent is durably before the submitting/write
        # boundary and therefore proven not sent.
        transition_position_mutation_intent(
            session_factory, intent.id, expected_statuses={"reserved"},
            new_status="blocked", transitioned_at=now,
            error={"reason": "component_restart_before_exchange_submit"},
        )
        _component_transition(session_factory, component_id, status, "recovery_required", now,
                              "close_reserved_before_write")
        return "recoverable"
    matches = [
        row for row in positions
        if str(row.get("posId") or "") == str(desired.get("pos_id") or "")
    ]
    if len(matches) != 1:
        return "awaiting"
    result = classify_composite_close_reconciliation(
        trusted_start_size=desired.get("trusted_start_size"),
        target_remaining_size=desired.get("target_remaining_size"),
        pre_submit_size=execution.get("pre_submit_size"),
        current_size=matches[0].get("pos"),
        quantity_step=desired.get("quantity_step"),
        min_quantity=desired.get("min_quantity"),
        intent_status=intent.status,
    )
    if result.status not in {"confirmed", "recovery_required", "operator_required"}:
        return "awaiting"
    _component_transition(
        session_factory, component_id, status, result.status, now,
        result.reason_code or "partial_close_exchange_reconciled",
        {"unresolved_delta": result.unresolved_delta, "intent_id": intent.id},
    )
    return "reconciled" if result.status == "confirmed" else "recoverable"


def _reconcile_protection_component(
    session_factory, component_id, desired, status, positions, now
):
    if desired.get(REMAINDER_CLOSE_EXECUTION_KEY):
        return _reconcile_remainder_close_component(
            session_factory, component_id, desired, status, positions, now
        )
    execution = desired.get("protection_replacement_execution") or {}
    if not execution:
        return "awaiting"
    with session_factory() as session:
        intents = session.query(PositionMutationIntent).filter(
            PositionMutationIntent.idempotency_key.like(f"{component_id}:%")
        ).all()
        statuses = {row.status for row in intents}
    if statuses.intersection({"submitting", "submitted", "recovery_required"}):
        return "awaiting"
    # Stable set keys make resuming idempotent. Confirmed set intents return
    # their existing response and require a fresh pending readback again.
    _component_transition(
        session_factory, component_id, status, "recovery_required", now,
        "protection_replacement_safe_to_resume",
    )
    return "recoverable"


def _reconcile_remainder_close_component(
    session_factory, component_id, desired, status, positions, now
):
    """Design 3.3. Read-only: exchange evidence in, local state out.

    A remainder close is never resubmitted from here. The only transitions this
    can make are to ``confirmed`` on proof the position is flat, to
    ``recovery_required`` so a later executor tick may try again with a fresh
    attempt key, or to ``operator_required`` when the outcome cannot be told
    apart from someone else's close.
    """

    execution = desired.get(REMAINDER_CLOSE_EXECUTION_KEY) or {}
    plan_intent_ids = [int(value) for value in execution.get("intent_ids") or []]
    with session_factory() as session:
        intents = {
            int(row.id): row
            for row in session.query(PositionMutationIntent).filter(
                PositionMutationIntent.idempotency_key.like(
                    f"{int(component_id)}:close:remainder:%"
                )
            )
        }
        for value in plan_intent_ids:
            row = session.get(PositionMutationIntent, value)
            if row is not None:
                intents[int(row.id)] = row
        ordered = [intents[key] for key in sorted(intents)]
        statuses = {str(row.status) for row in ordered}
        latest_status = str(ordered[-1].status) if ordered else None
    if not ordered:
        # Nothing was ever reserved, so the crash happened before the close --
        # in the deferred-entry cancel. The original stop is still armed.
        _component_transition(
            session_factory, component_id, status, "operator_required", now,
            "remainder_close_interrupted_before_close",
        )
        return "recoverable"
    reserved = [row for row in ordered if str(row.status) == "reserved"]
    if reserved:
        for row in reserved:
            transition_position_mutation_intent(
                session_factory, row.id, expected_statuses={"reserved"},
                new_status="blocked", transitioned_at=now,
                error={"reason": "component_restart_before_exchange_submit"},
            )
        _component_transition(
            session_factory, component_id, status, "recovery_required", now,
            "remainder_close_reserved_before_write",
        )
        return "recoverable"
    # Any unresolved attempt at all, not just the newest one: while one write's
    # outcome is unknown, no later evidence can prove what this position did.
    if statuses.intersection({"submitting", "submitted", "recovery_required"}):
        return "awaiting"
    matches = [
        row for row in positions
        if str(row.get("posId") or "") == str(desired.get("pos_id") or "")
    ]
    flat = _position_is_flat(matches[0]) if matches else True
    if flat is None:
        return "awaiting"
    if latest_status == "confirmed":
        if flat:
            _component_transition(
                session_factory, component_id, status, "confirmed", now, None,
                {
                    "outcome": REMAINDER_CLOSE_OUTCOME,
                    "close_intent_id": int(ordered[-1].id),
                    "remaining_size": "0",
                    "requested_stop": execution.get("requested_stop"),
                    "primary_stop": execution.get("primary_stop"),
                    "position_market_price": execution.get(
                        "position_market_price"
                    ),
                    "ticker_last": execution.get("ticker_last"),
                    "cancelled_deferred_entry_leg_ids": list(
                        execution.get("cancelled_deferred_entry_leg_ids") or []
                    ),
                    "evidence_tier": "exact_close_intent_confirmed",
                },
            )
            return "reconciled"
        _component_transition(
            session_factory, component_id, status, "recovery_required", now,
            "remainder_close_confirmed_with_residual",
        )
        return "recoverable"
    if latest_status in {"rejected", "blocked"}:
        if flat:
            _component_transition(
                session_factory, component_id, status, "operator_required", now,
                "remainder_position_absent_without_confirmed_close",
            )
        else:
            _component_transition(
                session_factory, component_id, status, "recovery_required", now,
                "remainder_close_terminal_without_fill",
            )
        return "recoverable"
    return "awaiting"


def _position_is_flat(row) -> bool | None:
    try:
        return Decimal(str((row or {}).get("pos"))) == 0
    except (InvalidOperation, TypeError, ValueError):
        return None


def _component_instrument(session, component, desired) -> str | None:
    rows = session.query(PositionProtectionLedger).filter(
        PositionProtectionLedger.pos_id == str(desired.get("pos_id") or ""),
        PositionProtectionLedger.execution_order_leg_id
        == int(desired.get("execution_order_leg_id") or 0),
    ).all()
    instruments = {str(row.instrument_id or "").upper() for row in rows}
    return next(iter(instruments)) if len(instruments) == 1 else None


def _complete_snapshot(client, instrument_id):
    def read(name):
        value = getattr(client, name)(inst_id=instrument_id)
        if not isinstance(value, list):
            raise RuntimeError(f"{name}_snapshot_incomplete")
        return value
    return {
        "positions": read("list_positions"),
        "pending": read("list_trigger_orders_pending"),
        "history": [*read("list_trigger_order_history"), *read("list_order_history")],
        "fills": read("list_trade_fills"),
    }


def _component_transition(
    session_factory, component_id, expected, new, now, reason, evidence=None
):
    with session_factory() as session:
        if transition_management_component(
            session, component_id=component_id, expected_status=expected,
            new_status=new, now=now, reason_code=reason, evidence=evidence,
        ):
            session.commit()
