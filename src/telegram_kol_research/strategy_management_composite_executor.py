"""Durable execution of ordered composite management components."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from telegram_kol_research.models import (
    PositionProtectionLedger,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyManagementLeg,
)
from telegram_kol_research.position_mutation_authority import (
    PositionMutationAuthorityError,
    build_position_mutation_authority,
)
from telegram_kol_research.position_mutation_gateway import (
    PositionMutationGateway,
    reconcile_submitted_position_mutation_intents,
)
from telegram_kol_research.strategy_management_batches import (
    management_component_set_is_complete_in_session,
)
from telegram_kol_research.strategy_management_components import (
    PROTECTED_RECONCILIATION_STATUSES,
    claim_management_component,
    transition_management_component,
)
from telegram_kol_research.strategy_management_contracts import (
    load_management_contract,
    management_contract_fingerprint,
)
from telegram_kol_research.strategy_management_take_profit_consumption import (
    TakeProfitConsumptionPlan,
    plan_take_profit_consumption,
)
from telegram_kol_research.strategy_management_sizing import (
    ManagementSizingError,
    target_remaining_close_delta,
)


@dataclass(frozen=True, slots=True)
class CompositeComponentExecutionResult:
    status: str
    component_id: int
    reason_code: str | None = None
    proven_filled_quantity: str = "0"
    cancel_intent_ids: tuple[int, ...] = ()
    close_intent_ids: tuple[int, ...] = ()


def execute_take_profit_consumption_component(
    session_factory,
    *,
    batch_id: int,
    component_id: int,
    deepcoin_client: Any,
    live_execution_gate: Callable[[], bool],
    now_provider: Callable[[], Any],
) -> CompositeComponentExecutionResult:
    """Consume one exactly-owned TP stage without ever submitting a close."""

    now = now_provider()
    loaded = _load_component(
        session_factory, batch_id, component_id,
        expected_kind="consume_take_profit_stage",
    )
    if isinstance(loaded, CompositeComponentExecutionResult):
        return loaded
    batch, component, leg, contract, desired = loaded
    if component.status in PROTECTED_RECONCILIATION_STATUSES:
        return _result(component)
    if component.attempt_count >= 3:
        with session_factory() as session:
            current = session.get(StrategyManagementComponent, component_id)
            if current and current.status in {"pending", "recovery_required"}:
                transition_management_component(
                    session,
                    component_id=component_id,
                    expected_status=current.status,
                    new_status="operator_required",
                    now=now,
                    reason_code="take_profit_cancel_retry_exhausted",
                )
                session.commit()
        return _current_result(session_factory, component_id)

    with session_factory() as session:
        if not claim_management_component(
            session,
            component_id=component_id,
            now=now,
            stale_before=now - timedelta(minutes=5),
        ):
            session.rollback()
            return _current_result(session_factory, component_id)
        session.commit()
    with session_factory() as session:
        claimed = session.get(StrategyManagementComponent, component_id)
        if claimed is None or claimed.status != "preflighting":
            return _current_result(session_factory, component_id)
        attempt_number = int(claimed.attempt_count)

    try:
        snapshot = _exchange_snapshot(deepcoin_client, desired["instrument_id"])
    except Exception as exc:
        _transition(
            session_factory, component_id, "preflighting", "recovery_required",
            now, "take_profit_exchange_snapshot_incomplete",
            {"error_type": type(exc).__name__},
        )
        return _current_result(session_factory, component_id)
    plan = _plan(
        session_factory, batch, leg, contract, desired, snapshot
    )
    if plan.refusal_code:
        _transition(
            session_factory,
            component_id,
            "preflighting",
            "recovery_required",
            now,
            plan.refusal_code,
            {"phase": "preflight", "refusal_code": plan.refusal_code},
        )
        return _current_result(session_factory, component_id)
    if not plan.cancel_actions:
        _transition(
            session_factory,
            component_id,
            "preflighting",
            "submitting",
            now,
            None,
            {"phase": "no_cancel_required", "evidence_tier": plan.evidence_tier},
        )
        _transition(
            session_factory,
            component_id,
            "submitting",
            "confirmed",
            now,
            None,
            {"proven_filled_quantity": plan.proven_filled_quantity},
        )
        return _current_result(
            session_factory,
            component_id,
            proven_filled_quantity=plan.proven_filled_quantity,
        )

    if len(plan.cancel_actions) != 1:
        _transition(
            session_factory,
            component_id,
            "preflighting",
            "operator_required",
            now,
            "take_profit_multi_cancel_not_supported",
        )
        return _current_result(session_factory, component_id)

    action = plan.cancel_actions[0]
    live_position = _unique_live_position(snapshot["positions"], desired["pos_id"])
    if live_position is None:
        _transition(
            session_factory, component_id, "preflighting", "recovery_required",
            now, "target_live_position_not_unique"
        )
        return _current_result(session_factory, component_id)
    try:
        with session_factory() as session:
            authority = build_position_mutation_authority(
                session,
                venue="deepcoin",
                pos_id=desired["pos_id"],
                live_position=live_position,
            )
    except PositionMutationAuthorityError as exc:
        _transition(
            session_factory, component_id, "preflighting", "recovery_required",
            now, str(exc)
        )
        return _current_result(session_factory, component_id)

    intent_ids: list[int] = []

    def _protect_before_write(intent_id: int) -> None:
        _persist_plan_and_enter_submitting(
            session_factory,
            component_id=component_id,
            intent_id=intent_id,
            plan=plan,
            now=now_provider(),
        )
        intent_ids.append(intent_id)

    gateway = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=deepcoin_client,
        live_execution_gate=live_execution_gate,
        now_provider=now_provider,
    )
    result = gateway.cancel_owned_position_sltp(
        authority=authority,
        order_id=action["order_id"],
        idempotency_key=(
            f"{component.id}:cancel:{action['order_id']}:attempt:{attempt_number}"
        ),
        before_submit=_protect_before_write,
        retry_pending_order=(
            _pending_row(snapshot["pending"], action["order_id"])
            if attempt_number > 1
            else None
        ),
    )
    if result.intent_id not in intent_ids:
        intent_ids.append(result.intent_id)
    if result.status == "recovery_required":
        _transition(
            session_factory, component_id, "submitting", "awaiting_exchange",
            now_provider(), "take_profit_cancel_outcome_unknown",
            {"intent_id": result.intent_id},
        )
        return _current_result(session_factory, component_id, intent_ids=intent_ids)

    try:
        refreshed = _exchange_snapshot(deepcoin_client, desired["instrument_id"])
    except Exception as exc:
        _transition(
            session_factory, component_id, "submitting", "awaiting_exchange",
            now_provider(), "take_profit_post_write_snapshot_incomplete",
            {"intent_id": result.intent_id, "error_type": type(exc).__name__},
        )
        return _current_result(session_factory, component_id, intent_ids=intent_ids)
    refreshed_plan = _plan(
        session_factory, batch, leg, contract, desired, refreshed
    )
    if result.status == "rejected":
        if (
            refreshed_plan.refusal_code is None
            and refreshed_plan.proven_filled_quantity != "0"
        ):
            _transition(
                session_factory, component_id, "submitting", "confirmed",
                now_provider(), None,
                {"intent_id": result.intent_id, "fill_race": True},
            )
            return _current_result(
                session_factory, component_id,
                proven_filled_quantity=refreshed_plan.proven_filled_quantity,
                intent_ids=intent_ids,
            )
        reason = (
            "take_profit_cancel_definitely_rejected_pending"
            if action["order_id"] in _pending_ids(refreshed["pending"])
            else (refreshed_plan.refusal_code or "take_profit_terminal_state_unknown")
        )
        _transition(
            session_factory, component_id, "submitting", "recovery_required",
            now_provider(), reason, {"intent_id": result.intent_id},
        )
        return _current_result(session_factory, component_id, intent_ids=intent_ids)
    if result.status != "submitted":
        _transition(
            session_factory, component_id, "submitting", "recovery_required",
            now_provider(), result.reason or f"take_profit_cancel_{result.status}",
        )
        return _current_result(session_factory, component_id, intent_ids=intent_ids)

    if action["order_id"] in _pending_ids(refreshed["pending"]):
        _transition(
            session_factory, component_id, "submitting", "awaiting_exchange",
            now_provider(), "take_profit_cancel_pending_after_submit",
            {"intent_id": result.intent_id},
        )
        return _current_result(session_factory, component_id, intent_ids=intent_ids)
    reconcile_submitted_position_mutation_intents(
        session_factory,
        pending_trigger_orders=refreshed["pending"],
        order_history=refreshed["history"],
        trade_fills=refreshed["fills"],
        reconciled_at=now_provider(),
    )
    _transition(
        session_factory, component_id, "submitting", "confirmed",
        now_provider(), None, {"intent_id": result.intent_id},
    )
    return _current_result(session_factory, component_id, intent_ids=intent_ids)


def execute_partial_close_component(
    session_factory,
    *,
    batch_id: int,
    component_id: int,
    deepcoin_client: Any,
    live_execution_gate: Callable[[], bool],
    now_provider: Callable[[], Any],
) -> CompositeComponentExecutionResult:
    """Converge one position to its immutable remaining-size target."""

    now = now_provider()
    loaded = _load_component(
        session_factory, batch_id, component_id,
        expected_kind="converge_partial_close",
    )
    if isinstance(loaded, CompositeComponentExecutionResult):
        return loaded
    batch, component, _leg, _contract, desired = loaded
    if component.status in PROTECTED_RECONCILIATION_STATUSES:
        return _result(component)
    with session_factory() as session:
        predecessors = session.query(StrategyManagementComponent).filter(
            StrategyManagementComponent.management_batch_id == batch.id,
            StrategyManagementComponent.strategy_management_leg_id
            == component.strategy_management_leg_id,
            StrategyManagementComponent.sequence < component.sequence,
        ).all()
        if not predecessors or any(row.status != "confirmed" for row in predecessors):
            return CompositeComponentExecutionResult(
                status="recovery_required", component_id=component.id,
                reason_code="composite_predecessor_not_confirmed",
            )
    if component.attempt_count >= 3:
        _terminalize_retry_exhausted(
            session_factory, component_id, now,
            "partial_close_retry_exhausted",
        )
        return _current_result(session_factory, component_id)
    with session_factory() as session:
        if not claim_management_component(
            session, component_id=component_id, now=now,
            stale_before=now - timedelta(minutes=5),
        ):
            session.rollback()
            return _current_result(session_factory, component_id)
        session.commit()
    with session_factory() as session:
        claimed = session.get(StrategyManagementComponent, component_id)
        attempt_number = int(claimed.attempt_count)

    try:
        positions = deepcoin_client.list_positions(inst_id=desired["instrument_id"])
        if not isinstance(positions, list):
            raise RuntimeError("positions_snapshot_incomplete")
        live_position = _unique_live_position(positions, desired["pos_id"])
        if live_position is None:
            raise RuntimeError("target_live_position_not_unique")
        delta = target_remaining_close_delta(
            trusted_start_size=desired["trusted_start_size"],
            target_remaining_size=desired["target_remaining_size"],
            current_size=live_position.get("pos"),
            quantity_step=desired["quantity_step"],
            min_quantity=desired["min_quantity"],
        )
    except ManagementSizingError as exc:
        _transition(
            session_factory, component_id, "preflighting", "operator_required",
            now, str(exc), {"phase": "close_delta_preflight"},
        )
        return _current_result(session_factory, component_id)
    except Exception as exc:
        _transition(
            session_factory, component_id, "preflighting", "recovery_required",
            now, str(exc), {"phase": "close_position_snapshot"},
        )
        return _current_result(session_factory, component_id)

    if delta == "0":
        _transition(
            session_factory, component_id, "preflighting", "submitting", now,
            evidence={"close_delta": "0"},
        )
        _transition(
            session_factory, component_id, "submitting", "confirmed", now,
            evidence={"remaining_size": desired["target_remaining_size"]},
        )
        return _current_result(session_factory, component_id)
    try:
        with session_factory() as session:
            authority = build_position_mutation_authority(
                session, venue="deepcoin", pos_id=desired["pos_id"],
                live_position=live_position,
            )
    except PositionMutationAuthorityError as exc:
        _transition(
            session_factory, component_id, "preflighting", "recovery_required",
            now, str(exc),
        )
        return _current_result(session_factory, component_id)

    intent_ids: list[int] = []
    client_order_id = f"CM{batch.id}L{component.strategy_management_leg_id}A{attempt_number}"

    def protect_before_write(intent_id: int) -> None:
        _persist_close_plan_and_enter_submitting(
            session_factory, component_id=component_id, intent_id=intent_id,
            close_delta=delta, client_order_id=client_order_id,
            current_size=str(live_position.get("pos")), now=now_provider(),
        )
        intent_ids.append(intent_id)

    result = PositionMutationGateway(
        session_factory=session_factory,
        deepcoin_client=deepcoin_client,
        live_execution_gate=live_execution_gate,
        now_provider=now_provider,
    ).close_exact_position(
        authority=authority,
        size=delta,
        client_order_id=client_order_id,
        idempotency_key=f"{component.id}:close:attempt:{attempt_number}",
        before_submit=protect_before_write,
    )
    if result.intent_id not in intent_ids:
        intent_ids.append(result.intent_id)
    if result.status == "recovery_required":
        _transition(
            session_factory, component_id, "submitting", "awaiting_exchange",
            now_provider(), "partial_close_outcome_unknown",
            {"intent_id": result.intent_id, "close_delta": delta},
        )
        return _current_result(
            session_factory, component_id, close_intent_ids=intent_ids
        )
    if result.status == "rejected":
        _transition(
            session_factory, component_id, "submitting", "recovery_required",
            now_provider(), "partial_close_definitely_rejected",
            {"intent_id": result.intent_id, "close_delta": delta},
        )
        return _current_result(
            session_factory, component_id, close_intent_ids=intent_ids
        )
    if result.status != "submitted":
        _transition(
            session_factory, component_id, "submitting", "recovery_required",
            now_provider(), result.reason or f"partial_close_{result.status}",
        )
        return _current_result(
            session_factory, component_id, close_intent_ids=intent_ids
        )
    try:
        refreshed = deepcoin_client.list_positions(inst_id=desired["instrument_id"])
        if not isinstance(refreshed, list):
            raise RuntimeError("positions_snapshot_incomplete")
        refreshed_position = _unique_live_position(refreshed, desired["pos_id"])
        if refreshed_position is None:
            raise RuntimeError("target_live_position_not_unique")
        remaining = str(refreshed_position.get("pos"))
    except Exception as exc:
        _transition(
            session_factory, component_id, "submitting", "awaiting_exchange",
            now_provider(), "partial_close_post_write_snapshot_incomplete",
            {"intent_id": result.intent_id, "error_type": type(exc).__name__},
        )
        return _current_result(
            session_factory, component_id, close_intent_ids=intent_ids
        )
    if target_remaining_close_delta(
        trusted_start_size=desired["trusted_start_size"],
        target_remaining_size=desired["target_remaining_size"],
        current_size=remaining,
        quantity_step=desired["quantity_step"],
        min_quantity=desired["min_quantity"],
    ) == "0":
        _transition(
            session_factory, component_id, "submitting", "confirmed",
            now_provider(), evidence={
                "intent_id": result.intent_id,
                "remaining_size": remaining,
                "evidence_tier": "exact_position_target",
            },
        )
    else:
        _transition(
            session_factory, component_id, "submitting", "awaiting_exchange",
            now_provider(), "partial_close_not_yet_converged",
            {"intent_id": result.intent_id, "remaining_size": remaining},
        )
    return _current_result(
        session_factory, component_id, close_intent_ids=intent_ids
    )


def _load_component(
    session_factory, batch_id: int, component_id: int, *, expected_kind: str
):
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, int(batch_id))
        component = session.get(StrategyManagementComponent, int(component_id))
        if batch is None or component is None or component.management_batch_id != batch.id:
            return CompositeComponentExecutionResult(
                status="operator_required", component_id=int(component_id),
                reason_code="management_component_identity_mismatch",
            )
        if component.component_kind != expected_kind:
            return CompositeComponentExecutionResult(
                status="operator_required", component_id=component.id,
                reason_code="management_component_kind_mismatch",
            )
        if not management_component_set_is_complete_in_session(session, batch=batch):
            return CompositeComponentExecutionResult(
                status="operator_required", component_id=component.id,
                reason_code="management_instruction_component_dropped",
            )
        leg = session.get(StrategyManagementLeg, component.strategy_management_leg_id)
        try:
            contract = load_management_contract(batch.management_contract_json or "")
            desired = json.loads(component.desired_json)
        except (ValueError, TypeError, json.JSONDecodeError):
            return CompositeComponentExecutionResult(
                status="operator_required", component_id=component.id,
                reason_code="management_component_contract_invalid",
            )
        if (
            leg is None
            or management_contract_fingerprint(contract)
            != batch.management_contract_fingerprint
            or desired.get("contract_fingerprint") != batch.management_contract_fingerprint
            or desired.get("pos_id") != leg.pos_id
            or int(desired.get("execution_order_leg_id") or 0)
            != leg.execution_order_leg_id
        ):
            return CompositeComponentExecutionResult(
                status="operator_required", component_id=component.id,
                reason_code="management_component_contract_invalid",
            )
        # The instrument is persisted by the exact owned protection ledger;
        # differing instruments fail closed.
        instruments = {
            str(row.instrument_id or "").upper()
            for row in session.query(PositionProtectionLedger).filter(
                PositionProtectionLedger.execution_binding_id == batch.execution_binding_id,
                PositionProtectionLedger.execution_order_leg_id == leg.execution_order_leg_id,
                PositionProtectionLedger.pos_id == leg.pos_id,
                PositionProtectionLedger.purpose == "take_profit",
            )
        }
        if len(instruments) != 1 or "" in instruments:
            return CompositeComponentExecutionResult(
                status="operator_required", component_id=component.id,
                reason_code="take_profit_order_identity_conflict",
            )
        desired["instrument_id"] = next(iter(instruments))
        for row in (batch, component, leg):
            session.expunge(row)
        return batch, component, leg, contract, desired


def _exchange_snapshot(client: Any, instrument_id: str) -> dict[str, list]:
    def read(name: str):
        fn = getattr(client, name, None)
        if fn is None:
            raise RuntimeError(f"{name}_snapshot_unavailable")
        value = fn(inst_id=instrument_id)
        if not isinstance(value, list):
            raise RuntimeError(f"{name}_snapshot_incomplete")
        return value
    return {
        "positions": read("list_positions"),
        "pending": read("list_trigger_orders_pending"),
        "history": [
            *read("list_trigger_orders_history"),
            *read("list_order_history"),
        ],
        "fills": read("list_trade_fills"),
    }


def _plan(session_factory, batch, leg, contract, desired, snapshot):
    with session_factory() as session:
        ledger = session.query(PositionProtectionLedger).filter(
            PositionProtectionLedger.execution_binding_id == batch.execution_binding_id,
            PositionProtectionLedger.execution_order_leg_id == leg.execution_order_leg_id,
            PositionProtectionLedger.pos_id == leg.pos_id,
            PositionProtectionLedger.purpose == "take_profit",
        ).all()
        target = {
            "execution_binding_id": batch.execution_binding_id,
            "execution_order_leg_id": leg.execution_order_leg_id,
            "pos_id": leg.pos_id,
            "instrument_id": desired["instrument_id"],
            "side": contract.side,
        }
        return plan_take_profit_consumption(
            contract=contract,
            target_leg=target,
            pending_orders=snapshot["pending"],
            trigger_history=snapshot["history"],
            order_history=(),
            trade_fills=snapshot["fills"],
            protection_ledger=ledger,
            trusted_start_size=desired["trusted_start_size"],
            target_remaining_size=desired["target_remaining_size"],
        )


def _persist_plan_and_enter_submitting(
    session_factory, *, component_id: int, intent_id: int,
    plan: TakeProfitConsumptionPlan, now: Any,
) -> None:
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
        if component is None or component.status != "preflighting":
            raise RuntimeError("management_component_not_preflighting")
        desired = json.loads(component.desired_json)
        execution = {
            "cancel_order_ids": list(plan.cancel_order_ids),
            "cancel_intent_ids": [int(intent_id)],
            "evidence_tier": plan.evidence_tier,
        }
        existing = desired.get("take_profit_consumption_execution")
        if existing is not None and existing.get("cancel_order_ids") != execution["cancel_order_ids"]:
            raise RuntimeError("management_instruction_component_dropped")
        desired["take_profit_consumption_execution"] = execution
        component.desired_json = json.dumps(
            desired, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if not transition_management_component(
            session, component_id=component_id, expected_status="preflighting",
            new_status="submitting", now=now,
            evidence={"intent_id": int(intent_id), "cancel_order_ids": list(plan.cancel_order_ids)},
        ):
            raise RuntimeError("management_component_submit_claim_lost")
        session.commit()


def _persist_close_plan_and_enter_submitting(
    session_factory, *, component_id: int, intent_id: int,
    close_delta: str, client_order_id: str, current_size: str, now: Any,
) -> None:
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
        if component is None or component.status != "preflighting":
            raise RuntimeError("management_component_not_preflighting")
        desired = json.loads(component.desired_json)
        desired["partial_close_execution"] = {
            "close_delta": close_delta,
            "client_order_id": client_order_id,
            "intent_id": int(intent_id),
            "pre_submit_size": current_size,
        }
        component.desired_json = json.dumps(
            desired, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if not transition_management_component(
            session, component_id=component_id, expected_status="preflighting",
            new_status="submitting", now=now,
            evidence={"intent_id": int(intent_id), "close_delta": close_delta},
        ):
            raise RuntimeError("management_component_submit_claim_lost")
        session.commit()


def _terminalize_retry_exhausted(session_factory, component_id, now, reason):
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
        if component and component.status in {"pending", "recovery_required"}:
            transition_management_component(
                session, component_id=component_id,
                expected_status=component.status, new_status="operator_required",
                now=now, reason_code=reason,
            )
            session.commit()


def _transition(
    session_factory, component_id, expected, new, now, reason=None, evidence=None
) -> None:
    with session_factory() as session:
        if not transition_management_component(
            session, component_id=component_id, expected_status=expected,
            new_status=new, now=now, reason_code=reason, evidence=evidence,
        ):
            raise RuntimeError("management_component_transition_lost")
        session.commit()


def _unique_live_position(rows, pos_id):
    matches = [row for row in rows if str(row.get("posId") or "") == str(pos_id)]
    return matches[0] if len(matches) == 1 else None


def _pending_ids(rows) -> set[str]:
    return {
        str(row.get("ordId") or row.get("orderId") or row.get("order_id") or "")
        for row in rows if isinstance(row, dict)
    }


def _pending_row(rows, order_id: str):
    matches = [
        row for row in rows
        if isinstance(row, dict)
        and str(row.get("ordId") or row.get("orderId") or row.get("order_id") or "")
        == str(order_id)
    ]
    return matches[0] if len(matches) == 1 else None


def _result(
    component, *, proven_filled_quantity="0", intent_ids=(), close_intent_ids=()
):
    return CompositeComponentExecutionResult(
        status=component.status, component_id=component.id,
        reason_code=component.reason_code,
        proven_filled_quantity=proven_filled_quantity,
        cancel_intent_ids=tuple(intent_ids),
        close_intent_ids=tuple(close_intent_ids),
    )


def _current_result(
    session_factory, component_id, *, proven_filled_quantity="0", intent_ids=(),
    close_intent_ids=(),
):
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
        if component is None:
            return CompositeComponentExecutionResult(
                status="operator_required", component_id=component_id,
                reason_code="management_component_missing",
            )
        return _result(
            component, proven_filled_quantity=proven_filled_quantity,
            intent_ids=intent_ids, close_intent_ids=close_intent_ids,
        )
