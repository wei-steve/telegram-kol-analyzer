"""Reconcile close batches from one coherent, read-only exchange snapshot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_normalization import (
    normalize_deepcoin_swap_instrument,
)
from telegram_kol_research.break_even_convergence_planner import (
    BreakEvenConvergencePlanningError,
    plan_or_adopt_break_even_convergence,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionMutationIntent,
    RawMessage,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
)
from telegram_kol_research.position_authority_lock import (
    serialized_position_authority_mutation,
)
from telegram_kol_research.position_attribution import TERMINAL_ENTRY_LEG_STATES
from telegram_kol_research.position_mutation_gateway import (
    reconcile_submitted_position_mutation_intents,
)
from telegram_kol_research.strategy_management_market_decisions import (
    BreakEvenMarketDecisionConflict,
    load_break_even_market_decision_in_session,
)
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.strategy_management_sizing import (
    ManagementSizingError,
    target_remaining_close_delta,
)


_ACTIVE_RECONCILIATION_STATUSES = frozenset(
    {
        "executing",
        "reserved",
        "submitted",
        "submit_unknown",
        "reconciling",
        "partial_failed",
    }
)
_CLOSE_ACTIONS = frozenset(
    {
        "partial_close",
        "full_close",
        "full_exit",
        "partial_then_break_even",
        "break_even_by_market",
    }
)
_ORDER_ID_KEYS = ("ordId", "orderId", "order_id", "id")
_CLIENT_ORDER_ID_KEYS = ("clOrdId", "clientOrderId", "client_order_id")
_PROTECTION_PHASE_LEG_STATES = frozenset(
    {"succeeded", "restored", "recovery_required"}
)
_MANAGEABLE_ENTRY_LEG_STATES = frozenset(
    {"active", "open", "filled", "partial_closed"}
)
_DEFERRED_ENTRY_LEG_STATES = frozenset({"open", "pending", "submitted"})


@dataclass(frozen=True, slots=True)
class ManagementReconciliationResult:
    checked: int = 0
    succeeded: int = 0
    pending: int = 0
    frozen: int = 0


@dataclass(frozen=True, slots=True)
class CompositeCloseReconciliation:
    status: str
    unresolved_delta: str | None = None
    reason_code: str | None = None


def classify_composite_close_reconciliation(
    *,
    trusted_start_size: object,
    target_remaining_size: object,
    pre_submit_size: object,
    current_size: object,
    quantity_step: object,
    min_quantity: object,
    intent_status: str,
) -> CompositeCloseReconciliation:
    """Classify close progress only after the exact intent becomes terminal."""

    if str(intent_status) not in {"confirmed", "rejected"}:
        return CompositeCloseReconciliation(status="awaiting_exchange")
    try:
        if Decimal(str(current_size)) > Decimal(str(pre_submit_size)):
            return CompositeCloseReconciliation(
                status="operator_required",
                reason_code="position_size_increased_after_close_submission",
            )
        delta = target_remaining_close_delta(
            trusted_start_size=trusted_start_size,
            target_remaining_size=target_remaining_size,
            current_size=current_size,
            quantity_step=quantity_step,
            min_quantity=min_quantity,
        )
    except (ManagementSizingError, InvalidOperation, ValueError) as exc:
        return CompositeCloseReconciliation(
            status="operator_required", reason_code=str(exc)
        )
    if delta == "0":
        return CompositeCloseReconciliation(
            status="confirmed", unresolved_delta="0"
        )
    if str(intent_status) == "rejected":
        return CompositeCloseReconciliation(
            status="recovery_required", unresolved_delta=delta,
            reason_code="partial_close_terminal_with_unresolved_delta",
        )
    return CompositeCloseReconciliation(
        status="recovery_required",
        unresolved_delta=delta,
        reason_code="confirmed_partial_close_has_unresolved_delta",
    )


@serialized_position_authority_mutation
def reconcile_strategy_management_batches(
    session_factory: sessionmaker,
    *,
    snapshot: Any,
    reconciled_at: datetime | None = None,
    batch_ids: set[int] | tuple[int, ...] | None = None,
) -> ManagementReconciliationResult:
    """Apply exchange truth without submitting or retrying any order."""

    now = reconciled_at or datetime.now(UTC)
    snapshot_errors = getattr(snapshot, "errors", {})
    pending_snapshot_failed = any(
        key == "pending_trigger_orders"
        or str(key).startswith("pending_trigger_orders:")
        for key in snapshot_errors
    )
    reconcile_submitted_position_mutation_intents(
        session_factory,
        pending_trigger_orders=(
            list(snapshot.pending_trigger_orders)
            if (
                hasattr(snapshot, "pending_trigger_orders")
                and not pending_snapshot_failed
            )
            else None
        ),
        order_history=list(getattr(snapshot, "order_history", [])),
        trade_fills=list(getattr(snapshot, "trade_fills", [])),
        reconciled_at=now,
    )
    _recover_confirmed_protection_legs(session_factory, now=now)
    if snapshot_errors.get("positions"):
        return ManagementReconciliationResult()

    position_rows = _positions_by_id(getattr(snapshot, "positions", []))
    order_rows = _regular_order_rows(snapshot)
    counts = {"checked": 0, "succeeded": 0, "pending": 0, "frozen": 0}
    confirmed_partial_batch_ids: list[int] = []
    settings = load_trading_settings(session_factory)
    automatic_break_even_enabled = bool(
        settings.move_stop_to_breakeven_after_tp1
        and (
            settings.management_execution_mode == "shadow"
            or settings.live_management_execution_enabled
        )
    )

    with session_factory() as session:
        query = session.query(StrategyManagementBatch).filter(
            StrategyManagementBatch.status.in_(_ACTIVE_RECONCILIATION_STATUSES)
        )
        if batch_ids is not None:
            ids = tuple(int(batch_id) for batch_id in batch_ids)
            if not ids:
                return ManagementReconciliationResult()
            query = query.filter(StrategyManagementBatch.id.in_(ids))
        batches = (
            query
            .order_by(StrategyManagementBatch.planned_at.asc(), StrategyManagementBatch.id.asc())
            .all()
        )
        for batch in batches:
            if batch.effective_action not in _CLOSE_ACTIONS:
                continue
            if (
                batch.status == "reconciling"
                and batch.reason_code
                == "management_subset_close_exchange_confirmed"
            ):
                # The original legs are already exchange-confirmed and their
                # entry rows were intentionally terminalized.  This parent is
                # only a durable wait handle for capability recovery; running
                # normal identity checks again would misclassify that proven
                # terminal state before the worker can create its successor.
                counts["checked"] += 1
                counts["pending"] += 1
                continue
            legs = (
                session.query(StrategyManagementLeg)
                .filter(StrategyManagementLeg.management_batch_id == batch.id)
                .order_by(StrategyManagementLeg.leg_index.asc(), StrategyManagementLeg.id.asc())
                .all()
            )
            market_actions = (
                _market_actions_by_leg_id(session, batch=batch, legs=legs)
                if batch.effective_action == "break_even_by_market"
                else None
            )
            if batch.effective_action == "break_even_by_market" and market_actions is None:
                counts["checked"] += 1
                _freeze_batch(
                    session,
                    batch,
                    status="recovery_required",
                    reason="break_even_market_decision_missing_or_invalid",
                    now=now,
                )
                counts["frozen"] += 1
                continue
            if _composite_protection_phase_started(batch, legs):
                continue
            counts["checked"] += 1
            if not legs or not _identity_is_exact(session, batch, legs):
                _freeze_batch(
                    session,
                    batch,
                    status="recovery_required",
                    reason="management_reconciliation_identity_mismatch",
                    now=now,
                )
                counts["frozen"] += 1
                continue

            deferred_leg_ids = _snapshot_deferred_entry_leg_ids(batch)
            close_legs = (
                [
                    leg
                    for leg in legs
                    if market_actions[int(leg.id)] == "full_exit"
                ]
                if market_actions is not None
                else legs
            )
            if (
                batch.effective_action
                in {"full_close", "full_exit", "break_even_by_market"}
                and deferred_leg_ids
                and close_legs
                and all(
                    str(leg.status or "") == "planned" for leg in close_legs
                )
            ):
                # Deferred cancellations are durable exchange writes. If no
                # close leg was subsequently reserved, a crash occurred in the
                # unsafe gap between those phases. Reconciliation is read-only:
                # it must never invent submission evidence for planned legs.
                _freeze_batch(
                    session,
                    batch,
                    status="recovery_required",
                    reason="management_close_not_reserved_after_deferred_cancel",
                    now=now,
                )
                counts["frozen"] += 1
                continue

            binding = session.get(ExecutionBinding, batch.execution_binding_id)
            expected_instrument = normalize_deepcoin_swap_instrument(binding.symbol)

            for leg in close_legs:
                _reconcile_leg(
                    leg,
                    position_rows=position_rows,
                    order_rows=order_rows,
                    snapshot=snapshot,
                    expected_instrument=expected_instrument,
                    now=now,
                    planned_close_size=(
                        leg.preflight_size
                        if market_actions is not None
                        else leg.planned_close_size
                    ),
                )

            if market_actions is not None:
                protection_legs = [
                    leg
                    for leg in legs
                    if market_actions[int(leg.id)] == "set_break_even"
                ]
                close_statuses = [str(leg.status or "") for leg in close_legs]
                protection_statuses = [
                    str(leg.status or "") for leg in protection_legs
                ]
                if close_legs and all(
                    status == "confirmed" for status in close_statuses
                ):
                    _terminalize_selected_market_close_legs(
                        session,
                        batch=batch,
                        close_legs=close_legs,
                        now=now,
                    )
                    batch.reconciled_at = now
                    batch.updated_at = now
                    if all(
                        status == "succeeded"
                        for status in protection_statuses
                    ):
                        batch.status = "succeeded"
                        batch.reason_code = (
                            "break_even_market_exchange_confirmed"
                        )
                        batch.completed_at = now
                        counts["succeeded"] += 1
                    else:
                        _freeze_batch(
                            session,
                            batch,
                            status=(
                                "recovery_required"
                                if "recovery_required" in protection_statuses
                                else "partial_failed"
                            ),
                            reason="break_even_market_protection_not_confirmed",
                            now=now,
                        )
                        counts["frozen"] += 1
                elif any(
                    status
                    in {"failed", "partial", "inconsistent", "submit_unknown"}
                    for status in close_statuses
                ):
                    _freeze_batch(
                        session,
                        batch,
                        status="recovery_required",
                        reason="break_even_market_close_requires_recovery",
                        now=now,
                    )
                    counts["frozen"] += 1
                else:
                    batch.status = "reconciling"
                    batch.reason_code = (
                        "break_even_market_close_pending_confirmation"
                    )
                    batch.updated_at = now
                    counts["pending"] += 1
                continue

            statuses = [str(leg.status or "") for leg in legs]
            if all(status == "confirmed" for status in statuses):
                batch.reconciled_at = now
                batch.updated_at = now
                if (
                    batch.effective_action == "partial_then_break_even"
                    and not automatic_break_even_enabled
                ):
                    batch.status = "protection_ready"
                    batch.reason_code = "management_close_confirmed_protection_ready"
                    batch.completed_at = None
                    counts["pending"] += 1
                else:
                    batch.status = "succeeded"
                    batch.reason_code = "management_close_exchange_confirmed"
                    batch.completed_at = now
                    counts["succeeded"] += 1
                if batch.effective_action in {"full_close", "full_exit"}:
                    fully_terminal = _terminalize_full_close(
                        session, batch=batch, legs=legs, now=now
                    )
                    if not fully_terminal:
                        batch.status = "reconciling"
                        batch.reason_code = (
                            "management_subset_close_exchange_confirmed"
                        )
                        batch.completed_at = None
                        counts["succeeded"] -= 1
                        counts["pending"] += 1
                else:
                    _confirm_partial_close(session, batch=batch, now=now)
                    if automatic_break_even_enabled and batch.effective_action in {
                        "partial_close",
                        "partial_then_break_even",
                    }:
                        confirmed_partial_batch_ids.append(int(batch.id))
            elif "failed" in statuses:
                _freeze_batch(
                    session,
                    batch,
                    status="partial_failed",
                    reason="one_or_more_close_legs_failed",
                    now=now,
                )
                counts["frozen"] += 1
            elif any(status in {"partial", "inconsistent", "submit_unknown"} for status in statuses):
                reason = (
                    "management_close_submission_unresolved"
                    if "submit_unknown" in statuses
                    else "management_close_result_requires_recovery"
                )
                _freeze_batch(
                    session,
                    batch,
                    status="recovery_required",
                    reason=reason,
                    now=now,
                )
                counts["frozen"] += 1
            elif "confirmed" in statuses:
                if batch.effective_action in {"full_close", "full_exit"}:
                    # Exact full-exit legs may settle on different snapshots;
                    # waiting is safe because no request is ever resubmitted.
                    batch.status = "reconciling"
                    batch.reason_code = "management_close_legs_partially_confirmed"
                    batch.updated_at = now
                    counts["pending"] += 1
                else:
                    _freeze_batch(
                        session,
                        batch,
                        status="recovery_required",
                        reason="management_close_legs_partially_confirmed",
                        now=now,
                    )
                    counts["frozen"] += 1
            else:
                batch.status = "reconciling"
                batch.reason_code = "management_close_pending_exchange_confirmation"
                batch.updated_at = now
                counts["pending"] += 1
        session.commit()

    for batch_id in confirmed_partial_batch_ids:
        _plan_confirmed_partial_close_break_even(
            session_factory,
            batch_id=batch_id,
            planned_at=now,
        )

    return ManagementReconciliationResult(**counts)


def _plan_confirmed_partial_close_break_even(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    planned_at: datetime,
) -> None:
    """Bridge exchange-confirmed partial closes into the durable convergence."""

    settings = load_trading_settings(session_factory)
    if (
        not settings.move_stop_to_breakeven_after_tp1
        or (
            settings.management_execution_mode != "shadow"
            and not settings.live_management_execution_enabled
        )
    ):
        return
    if settings.management_execution_mode == "shadow":
        execution_mode = "shadow"
    elif settings.live_management_execution_enabled:
        execution_mode = "live"
    else:
        execution_mode = "disabled"
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, int(batch_id))
        if batch is None:
            raise RuntimeError("confirmed_partial_close_batch_missing")
        legs = (
            session.query(StrategyManagementLeg)
            .filter_by(management_batch_id=batch.id)
            .order_by(StrategyManagementLeg.leg_index.asc())
            .all()
        )
        evidence = {
            "version": 1,
            "management_batch_id": int(batch.id),
            "effective_action": str(batch.effective_action),
            "actual_closed_size": str(
                sum(
                    (Decimal(str(leg.planned_close_size)) for leg in legs),
                    Decimal("0"),
                )
            ),
            "close_legs": [
                {
                    "management_leg_id": int(leg.id),
                    "execution_order_leg_id": int(leg.execution_order_leg_id),
                    "pos_id": str(leg.pos_id),
                    "closed_size": str(leg.planned_close_size),
                    "exchange_order_id": leg.exchange_order_id,
                }
                for leg in legs
            ],
            "confirmed_at": planned_at.isoformat(),
        }
        strategy_instance_id = str(batch.strategy_instance_id)
    try:
        plan_or_adopt_break_even_convergence(
            session_factory,
            trigger_type="confirmed_partial_close",
            trigger_identity=str(batch_id),
            trigger_evidence=evidence,
            strategy_instance_id=strategy_instance_id,
            planned_at=planned_at,
            execution_mode=execution_mode,
        )
    except BreakEvenConvergencePlanningError:
        with session_factory() as session:
            batch = session.get(StrategyManagementBatch, int(batch_id))
            if batch is not None and batch.status == "succeeded":
                batch.status = "reconciling"
                batch.reason_code = "automatic_break_even_planning_pending"
                batch.completed_at = None
                batch.updated_at = planned_at
                session.commit()


def _recover_confirmed_protection_legs(
    session_factory: sessionmaker,
    *,
    now: datetime,
) -> None:
    """Unfreeze protection legs only when every expected set intent is confirmed."""

    with session_factory() as session:
        batches = (
            session.query(StrategyManagementBatch)
            .filter(
                StrategyManagementBatch.status == "recovery_required",
                StrategyManagementBatch.reason_code.like("%protection%"),
            )
            .all()
        )
        for batch in batches:
            rejected_close_recovered = False
            legs = (
                session.query(StrategyManagementLeg)
                .filter(
                    StrategyManagementLeg.management_batch_id == batch.id
                )
                .all()
            )
            for leg in legs:
                if leg.status != "recovery_required":
                    continue
                request = _load_json_object(leg.request_json)
                expected_count = int(
                    request.get("expected_replacement_count") or 0
                )
                if expected_count <= 0:
                    continue
                last_error = _load_json_object(leg.last_error)
                recovery_phase = str(
                    request.get("recovery_phase") or ""
                )
                rejected_close_restore = (
                    recovery_phase == "rejected_close_restore"
                )
                restoring = (
                    rejected_close_restore
                    or bool(last_error.get("restore_error"))
                )
                intent_phases = (
                    ("restore", "rejected_close_restore")
                    if restoring
                    else ("set",)
                )
                prefixes = tuple(
                    f"management:{batch.id}:{leg.id}:{phase}:"
                    for phase in intent_phases
                )
                intents = (
                    session.query(PositionMutationIntent)
                    .filter(
                        PositionMutationIntent.operation
                        == "set_position_sltp",
                    )
                    .all()
                )
                intents = [
                    intent
                    for intent in intents
                    if any(
                        intent.idempotency_key.startswith(prefix)
                        for prefix in prefixes
                    )
                ]
                if (
                    len(intents) == expected_count
                    and all(
                        intent.status == "confirmed" for intent in intents
                    )
                ):
                    if rejected_close_restore:
                        leg.status = "failed"
                        leg.last_error = json.dumps(
                            {
                                "reason": (
                                    "close_rejected_protection_restored"
                                )
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        rejected_close_recovered = True
                    else:
                        leg.status = "restored" if restoring else "succeeded"
                        leg.last_error = None
                    leg.updated_at = now
            if rejected_close_recovered:
                batch.status = "partial_failed"
                batch.reason_code = "close_rejected_protection_restored"
                batch.updated_at = now
                continue
            terminal_leg_statuses = {
                "succeeded",
                "restored",
                "confirmed",
                "closed",
            }
            if legs and all(
                leg.status in terminal_leg_statuses for leg in legs
            ):
                batch.status = "protection_ready"
                batch.reason_code = "protection_intent_readback_confirmed"
                batch.updated_at = now
        session.commit()


def _load_json_object(value: str | None) -> dict[str, Any]:
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _composite_protection_phase_started(batch, legs) -> bool:
    """Keep close reconciliation permanently out after phase hand-off."""

    if batch.effective_action != "partial_then_break_even":
        return False
    reason = str(batch.reason_code or "")
    return bool(
        batch.status == "protection_ready"
        or reason.startswith("protection_")
        or reason.startswith("all_position_protection_")
        or any(str(leg.status or "") in _PROTECTION_PHASE_LEG_STATES for leg in legs)
    )


def _market_actions_by_leg_id(session, *, batch, legs) -> dict[int, str] | None:
    try:
        record = load_break_even_market_decision_in_session(
            session, batch_id=batch.id
        )
    except BreakEvenMarketDecisionConflict:
        return None
    if record is None:
        return None
    actions: dict[int, str] = {}
    expected = {
        (int(leg.id), int(leg.execution_order_leg_id), str(leg.pos_id))
        for leg in legs
    }
    observed: set[tuple[int, int, str]] = set()
    for decision in record.decisions:
        try:
            identity = (
                int(decision["management_leg_id"]),
                int(decision["execution_order_leg_id"]),
                str(decision["pos_id"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        action = str(decision.get("action") or "")
        if (
            identity in observed
            or action not in {"full_exit", "set_break_even"}
        ):
            return None
        observed.add(identity)
        actions[identity[0]] = action
    return actions if observed == expected else None


def _terminalize_selected_market_close_legs(
    session,
    *,
    batch,
    close_legs,
    now: datetime,
) -> None:
    binding = session.get(ExecutionBinding, batch.execution_binding_id)
    lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
    raw = session.get(RawMessage, batch.raw_message_id)
    if binding is None or lifecycle is None:
        raise RuntimeError("management_reconciliation_identity_disappeared")
    for leg in close_legs:
        entry = session.get(ExecutionOrderLeg, leg.execution_order_leg_id)
        if entry is None:
            raise RuntimeError("management_entry_leg_disappeared")
        entry.status = "closed"
        entry.terminal_reason = "management_full_close_confirmed"
        entry.last_verified_at = now
        entry.updated_at = now
    remaining = (
        session.query(ExecutionOrderLeg)
        .filter(
            ExecutionOrderLeg.execution_binding_id
            == batch.execution_binding_id,
            ExecutionOrderLeg.purpose == "entry",
            ExecutionOrderLeg.pos_id.is_not(None),
        )
        .order_by(ExecutionOrderLeg.leg_index.asc(), ExecutionOrderLeg.id.asc())
        .all()
    )
    remaining_pos_ids = [
        str(entry.pos_id)
        for entry in remaining
        if (
            str(entry.status or "").lower() not in TERMINAL_ENTRY_LEG_STATES
            and entry.terminal_reason is None
            and entry.attribution_status == "verified"
        )
    ]
    if remaining_pos_ids:
        binding.status = "active"
        binding.pos_id = ",".join(dict.fromkeys(remaining_pos_ids))
        binding.last_exchange_status = "management_selected_close_confirmed"
        lifecycle.lifecycle_status = "entered"
        lifecycle.exit_reason = None
        lifecycle.exited_at = None
        lifecycle.management_action = "break_even_market_confirmed"
    else:
        binding.status = "closed"
        binding.pos_id = None
        binding.last_exchange_status = "management_full_close_confirmed"
        lifecycle.lifecycle_status = "exited"
        lifecycle.exit_reason = "kol_signal"
        lifecycle.exited_at = now
        lifecycle.management_action = "full_close_confirmed"
    binding.recovered_at = now
    binding.updated_at = now
    lifecycle.management_signal_message_id = (
        int(raw.message_id) if raw is not None else None
    )
    lifecycle.updated_at = now


def _reconcile_leg(
    leg: StrategyManagementLeg,
    *,
    position_rows: dict[str, list[dict[str, Any]]],
    order_rows: list[dict[str, Any]],
    snapshot: Any,
    expected_instrument: str,
    now: datetime,
    planned_close_size: Any = None,
) -> None:
    if leg.status == "failed":
        return

    matching_orders = _matching_orders(leg, order_rows)
    matching_order, ambiguous = _resolve_matching_order(matching_orders)
    identity_conflict = _order_identity_conflicts(leg, matching_orders)
    if ambiguous or identity_conflict:
        leg.status = "inconsistent"
        leg.last_error = _json(
            {
                "reason": (
                    "management_close_order_identity_conflict"
                    if identity_conflict
                    else "management_close_order_identity_ambiguous"
                )
            }
        )
        leg.last_exchange_snapshot_json = _leg_snapshot(
            leg, position_rows, matching_orders
        )
        leg.updated_at = now
        return

    if leg.status == "inconsistent":
        return

    if leg.status in {"reserved", "submit_unknown"}:
        if matching_order is None:
            leg.status = "submit_unknown"
            leg.last_error = _json({"reason": "management_close_order_not_found"})
            leg.last_exchange_snapshot_json = _leg_snapshot(leg, position_rows, matching_orders)
            leg.updated_at = now
            return
        order = matching_order
        order_id = _first_string(order, *_ORDER_ID_KEYS)
        if leg.exchange_order_id and order_id and str(leg.exchange_order_id) != order_id:
            leg.status = "inconsistent"
            leg.last_error = _json({"reason": "management_close_order_id_conflict"})
            leg.updated_at = now
            return
        leg.exchange_order_id = order_id or leg.exchange_order_id
        leg.status = "submitted"
        leg.last_error = None
    elif leg.status in {"submitted", "partial"} and matching_order is None:
        # Position movement without the exact regular-order identity could be a
        # manual or unrelated close. Preserve the non-retryable pending state.
        leg.last_error = _json({"reason": "management_close_order_not_found"})
        leg.last_exchange_snapshot_json = _leg_snapshot(
            leg, position_rows, matching_orders
        )
        leg.updated_at = now
        return

    rows = position_rows.get(str(leg.pos_id), [])
    if len(rows) > 1:
        leg.status = "inconsistent"
        leg.last_error = _json({"reason": "management_position_snapshot_ambiguous"})
        leg.last_exchange_snapshot_json = _leg_snapshot(leg, position_rows, matching_orders)
        leg.updated_at = now
        return

    if rows:
        instrument = _first_string(rows[0], "instId", "instrumentId", "symbol")
        if instrument and instrument.upper() != expected_instrument:
            leg.status = "inconsistent"
            leg.last_error = _json({"reason": "management_position_instrument_mismatch"})
            leg.last_exchange_snapshot_json = _leg_snapshot(leg, position_rows, matching_orders)
            leg.updated_at = now
            return

    try:
        before = _positive_decimal(leg.preflight_size)
        planned = _positive_decimal(
            leg.planned_close_size
            if planned_close_size is None
            else planned_close_size
        )
        current = Decimal("0") if not rows else _position_size(rows[0])
    except (InvalidOperation, ValueError):
        leg.status = "inconsistent"
        leg.last_error = _json({"reason": "management_position_size_invalid"})
        leg.last_exchange_snapshot_json = _leg_snapshot(leg, position_rows, matching_orders)
        leg.updated_at = now
        return

    expected = before - planned
    if expected < 0 or current < 0 or current > before or current < expected:
        leg.status = "inconsistent"
        leg.last_error = _json({"reason": "management_close_size_inconsistent"})
    elif current == expected:
        leg.status = "confirmed"
        leg.last_error = None
    elif current == before:
        # A known submitted order may still be live. An unresolved submission
        # was returned above unless exact order identity was found.
        leg.status = "submitted"
        leg.last_error = None
    else:
        leg.status = "partial"
        leg.last_error = _json({"reason": "management_close_partially_filled"})
    leg.last_exchange_snapshot_json = _leg_snapshot(leg, position_rows, matching_orders)
    leg.updated_at = now


def _identity_is_exact(session, batch, legs) -> bool:
    binding = session.get(ExecutionBinding, batch.execution_binding_id)
    lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
    if (
        binding is None
        or lifecycle is None
        or binding.strategy_instance_id != batch.strategy_instance_id
        or lifecycle.execution_binding_id != batch.execution_binding_id
        or lifecycle.lifecycle_status != "entered"
        or lifecycle.exit_reason is not None
        or lifecycle.exited_at is not None
        or binding.status not in {"open", "active", "stale"}
        or (
            binding.status == "stale"
            and binding.last_exchange_status
            != "verified_position_missing_from_exchange"
        )
    ):
        return False
    managed_identity = {
        (int(leg.execution_order_leg_id), str(leg.pos_id)) for leg in legs
    }
    deferred_leg_ids = _snapshot_deferred_entry_leg_ids(batch)
    capability_deferred_leg_ids = (
        _snapshot_capability_deferred_entry_leg_ids(batch)
    )
    if deferred_leg_ids is None or capability_deferred_leg_ids is None:
        return False
    seen: set[str] = set()
    for leg in legs:
        if not leg.pos_id or str(leg.pos_id) in seen:
            return False
        seen.add(str(leg.pos_id))
        entry = session.get(ExecutionOrderLeg, leg.execution_order_leg_id)
        entry_status = str(getattr(entry, "status", "") or "").lower()
        if (
            entry is None
            or entry.execution_binding_id != batch.execution_binding_id
            or entry.strategy_instance_id != batch.strategy_instance_id
            or entry.purpose != "entry"
            or entry.pos_id != leg.pos_id
            or entry.attribution_status != "verified"
            or entry_status in TERMINAL_ENTRY_LEG_STATES
            or entry_status not in _MANAGEABLE_ENTRY_LEG_STATES
            or entry.terminal_reason is not None
        ):
            return False
    all_entry_rows = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.execution_binding_id == batch.execution_binding_id)
        .filter(ExecutionOrderLeg.purpose == "entry")
        .all()
    )
    accepted_deferred_ids: set[int] = set()
    accepted_capability_deferred_ids: set[int] = set()
    for row in all_entry_rows:
        if row.strategy_instance_id != batch.strategy_instance_id:
            return False
        row_identity = (int(row.id), str(row.pos_id)) if row.pos_id else None
        if row_identity in managed_identity:
            continue
        if int(row.id) in capability_deferred_leg_ids:
            if (
                row.pos_id
                and row.attribution_status == "verified"
                and str(row.status or "").lower()
                in _MANAGEABLE_ENTRY_LEG_STATES
                and row.terminal_reason is None
            ):
                accepted_capability_deferred_ids.add(int(row.id))
                continue
            return False
        if (
            row.pos_id
            and str(row.status or "").lower() in TERMINAL_ENTRY_LEG_STATES
            and row.terminal_reason is not None
        ):
            continue
        if (
            batch.effective_action not in {"full_close", "full_exit"}
            and not row.pos_id
            and str(row.status or "").lower() in TERMINAL_ENTRY_LEG_STATES
            and int(row.id) not in deferred_leg_ids
        ):
            continue
        if int(row.id) in deferred_leg_ids:
            if _is_management_cancelled_deferred_entry_leg(row):
                accepted_deferred_ids.add(int(row.id))
                continue
            if (
                batch.effective_action not in {"full_close", "full_exit"}
                and _is_deferred_pending_entry_leg(row)
            ):
                accepted_deferred_ids.add(int(row.id))
                continue
        return False
    return (
        accepted_deferred_ids == deferred_leg_ids
        and accepted_capability_deferred_ids == capability_deferred_leg_ids
    )


def _snapshot_deferred_entry_leg_ids(batch) -> set[int] | None:
    try:
        snapshot = json.loads(batch.target_snapshot_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict):
        return None
    identity = snapshot.get("identity")
    if not isinstance(identity, dict):
        return None
    values = identity.get("deferred_entry_leg_ids", [])
    if (
        not isinstance(values, list)
        or any(type(value) is not int or value <= 0 for value in values)
        or len(set(values)) != len(values)
    ):
        return None
    return set(values)


def _snapshot_capability_deferred_entry_leg_ids(batch) -> set[int] | None:
    try:
        snapshot = json.loads(batch.target_snapshot_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict):
        return None
    identity = snapshot.get("identity")
    if not isinstance(identity, dict):
        return None
    values = identity.get("capability_deferred_entry_leg_ids", [])
    if (
        not isinstance(values, list)
        or any(type(value) is not int or value <= 0 for value in values)
        or len(set(values)) != len(values)
    ):
        return None
    return set(values)


def _is_deferred_pending_entry_leg(entry: ExecutionOrderLeg) -> bool:
    status = str(entry.status or "").lower()
    state = str(entry.attribution_status or "unassigned")
    return bool(
        status in _DEFERRED_ENTRY_LEG_STATES
        and status not in TERMINAL_ENTRY_LEG_STATES
        and entry.terminal_reason is None
        and not entry.pos_id
        and state not in {"attribution_conflict", "evidence_unavailable"}
    )


def _is_management_cancelled_deferred_entry_leg(entry: ExecutionOrderLeg) -> bool:
    return bool(
        str(entry.status or "").lower() == "cancelled"
        and entry.terminal_reason
        == "management_full_close_cancelled_unfilled_entry_leg"
        and not entry.pos_id
    )


def _terminalize_full_close(session, *, batch, legs, now: datetime) -> bool:
    binding = session.get(ExecutionBinding, batch.execution_binding_id)
    lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
    if binding is None or lifecycle is None:
        raise RuntimeError("management_reconciliation_identity_disappeared")
    for leg in legs:
        entry = session.get(ExecutionOrderLeg, leg.execution_order_leg_id)
        if entry is None:
            raise RuntimeError("management_entry_leg_disappeared")
        entry.status = "closed"
        entry.terminal_reason = "management_full_close_confirmed"
        entry.last_verified_at = now
        entry.updated_at = now
    remaining_entries = (
        session.query(ExecutionOrderLeg)
        .filter(
            ExecutionOrderLeg.execution_binding_id == batch.execution_binding_id,
            ExecutionOrderLeg.purpose == "entry",
            ExecutionOrderLeg.pos_id.is_not(None),
        )
        .all()
    )
    remaining_active = [
        entry for entry in remaining_entries
        if str(entry.status or "").lower() not in TERMINAL_ENTRY_LEG_STATES
        and str(entry.attribution_status or "") == "verified"
        and str(entry.pos_id or "").strip()
    ]
    raw = session.get(RawMessage, batch.raw_message_id)
    lifecycle.management_signal_message_id = (
        int(raw.message_id) if raw is not None else None
    )
    if remaining_active:
        binding.status = "active"
        binding.pos_id = ",".join(sorted(
            str(entry.pos_id) for entry in remaining_active
        ))
        binding.last_exchange_status = "management_subset_close_confirmed"
        binding.recovered_at = now
        binding.updated_at = now
        lifecycle.management_action = "subset_full_close_confirmed"
        lifecycle.management_note = (
            "Exchange confirmed the safe subset; capability-deferred exact "
            "positions remain active."
        )
        lifecycle.updated_at = now
        return False
    binding.status = "closed"
    binding.pos_id = None
    binding.last_exchange_status = "management_full_close_confirmed"
    binding.recovered_at = now
    binding.updated_at = now
    lifecycle.lifecycle_status = "exited"
    lifecycle.exit_reason = "kol_signal"
    lifecycle.exited_at = now
    lifecycle.management_action = "full_close_confirmed"
    lifecycle.updated_at = now
    return True


def _confirm_partial_close(session, *, batch, now: datetime) -> None:
    lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
    raw = session.get(RawMessage, batch.raw_message_id)
    if lifecycle is None or raw is None:
        raise RuntimeError("management_reconciliation_identity_disappeared")
    lifecycle.management_signal_message_id = int(raw.message_id)
    lifecycle.management_action = "partial_close_confirmed"
    lifecycle.management_note = (
        "Deepcoin exchange confirmed every planned close leg."
    )
    lifecycle.updated_at = now


def _freeze_batch(session, batch, *, status: str, reason: str, now: datetime) -> None:
    batch.status = status
    batch.reason_code = reason
    batch.reconciled_at = None
    batch.completed_at = None
    batch.updated_at = now
    from telegram_kol_research.system_operator_bot import (
        persist_strategy_management_notification_in_session,
    )

    persist_strategy_management_notification_in_session(session, batch)


def _positions_by_id(rows: Any) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        pos_id = _first_string(row, "posId", "pos_id", "id")
        if pos_id:
            result.setdefault(pos_id, []).append(row)
    return result


def _regular_order_rows(snapshot: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in ("open_orders", "order_history", "trade_fills"):
        rows = getattr(snapshot, source, [])
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            identity = (
                _first_string(row, *_ORDER_ID_KEYS) or "",
                _first_string(row, *_CLIENT_ORDER_ID_KEYS) or "",
            )
            if identity == ("", "") or identity in seen:
                continue
            seen.add(identity)
            result.append(row)
    return result


def _matching_orders(leg, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches = []
    for row in rows:
        order_id = _first_string(row, *_ORDER_ID_KEYS)
        client_id = _first_string(row, *_CLIENT_ORDER_ID_KEYS)
        if (
            leg.exchange_order_id
            and order_id == str(leg.exchange_order_id)
        ) or (
            leg.client_order_id
            and client_id == str(leg.client_order_id)
        ):
            matches.append(row)
    return matches


def _resolve_matching_order(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool]:
    if not rows:
        return None, False
    order_ids = {
        value for row in rows if (value := _first_string(row, *_ORDER_ID_KEYS))
    }
    client_ids = {
        value
        for row in rows
        if (value := _first_string(row, *_CLIENT_ORDER_ID_KEYS))
    }
    if len(order_ids) > 1 or len(client_ids) > 1:
        return None, True
    connected = {0}
    connected_order_ids = {
        value
        for value in (_first_string(rows[0], *_ORDER_ID_KEYS),)
        if value
    }
    connected_client_ids = {
        value
        for value in (_first_string(rows[0], *_CLIENT_ORDER_ID_KEYS),)
        if value
    }
    changed = True
    while changed:
        changed = False
        for index, row in enumerate(rows):
            if index in connected:
                continue
            order_id = _first_string(row, *_ORDER_ID_KEYS)
            client_id = _first_string(row, *_CLIENT_ORDER_ID_KEYS)
            if not (
                (order_id and order_id in connected_order_ids)
                or (client_id and client_id in connected_client_ids)
            ):
                continue
            connected.add(index)
            if order_id:
                connected_order_ids.add(order_id)
            if client_id:
                connected_client_ids.add(client_id)
            changed = True
    if len(connected) != len(rows):
        return None, True
    merged: dict[str, Any] = {}
    for row in rows:
        merged.update(row)
    if order_ids:
        merged["ordId"] = next(iter(order_ids))
    if client_ids:
        merged["clOrdId"] = next(iter(client_ids))
    return merged, False


def _order_identity_conflicts(leg, rows: list[dict[str, Any]]) -> bool:
    durable_order_id = str(leg.exchange_order_id) if leg.exchange_order_id else None
    durable_client_id = str(leg.client_order_id) if leg.client_order_id else None
    for row in rows:
        order_id = _first_string(row, *_ORDER_ID_KEYS)
        client_id = _first_string(row, *_CLIENT_ORDER_ID_KEYS)
        if durable_order_id and order_id and order_id != durable_order_id:
            return True
        if durable_client_id and client_id and client_id != durable_client_id:
            return True
    return False


def _position_size(row: dict[str, Any]) -> Decimal:
    for key in ("pos", "size", "sz", "positionSize", "position_size"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                result = abs(Decimal(str(value)))
            except InvalidOperation as exc:
                raise ValueError("invalid position size") from exc
            if not result.is_finite():
                raise ValueError("position size must be finite")
            return result
    raise ValueError("position size missing")


def _positive_decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("size must be finite and positive")
    return result


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _leg_snapshot(leg, positions, orders) -> str:
    return _json(
        {
            "position_rows": positions.get(str(leg.pos_id), []),
            "matching_regular_orders": orders,
        }
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
