"""Durable execution of ordered composite management components."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from telegram_kol_research.models import (
    PositionProtectionLedger,
    RawMessage,
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
    submit_exact_position_sltp,
)
from telegram_kol_research.strategy_management_batches import (
    claim_ready_batch,
    load_management_batch,
    management_component_set_is_complete_in_session,
    transition_batch,
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
from telegram_kol_research.strategy_management_market_policy import (
    BreakEvenMarketPolicyError,
    plan_composite_stop_replacement,
)
from telegram_kol_research.protection_ledger import retained_take_profit_total
from telegram_kol_research.strategy_records import (
    CompositeManagementCompletionError,
    validate_composite_management_completion,
)


@dataclass(frozen=True, slots=True)
class CompositeComponentExecutionResult:
    status: str
    component_id: int
    reason_code: str | None = None
    proven_filled_quantity: str = "0"
    cancel_intent_ids: tuple[int, ...] = ()
    close_intent_ids: tuple[int, ...] = ()


def execute_composite_management_batch(
    session_factory,
    *,
    batch_id: int,
    deepcoin_client: Any,
    contract_spec_provider: Any,
    live_execution_gate: Callable[[], bool],
    now_provider: Callable[[], Any],
    backup_buffer_bps: str = "20",
):
    """Run only ordered v2 components, then atomically validate completion."""

    batch = load_management_batch(session_factory, int(batch_id))
    if not (
        batch.management_contract_json
        and batch.management_contract_fingerprint
        and batch.contract_version is not None
        and batch.components
    ):
        raise ValueError("management_contract_requires_component_executor")
    if not _composite_component_topology_is_exact(batch):
        _freeze_composite_batch(
            session_factory, batch.id, now_provider(),
            "management_instruction_component_topology_invalid",
        )
        return load_management_batch(session_factory, batch.id)
    if batch.status == "ready":
        claimed = claim_ready_batch(
            session_factory, batch.id, claimed_at=now_provider()
        )
        batch = claimed or load_management_batch(session_factory, batch.id)
    if batch.status == "succeeded":
        return batch
    if batch.status != "executing":
        raise ValueError(f"composite_batch_not_executable:{batch.status}")

    executors = {
        "consume_take_profit_stage": execute_take_profit_consumption_component,
        "converge_partial_close": execute_partial_close_component,
        "replace_remaining_protection": execute_protection_replacement_component,
    }
    for sequence in range(3):
        current = load_management_batch(session_factory, batch.id)
        if not _composite_component_topology_is_exact(current):
            _freeze_composite_batch(
                session_factory, batch.id, now_provider(),
                "management_instruction_component_topology_invalid",
            )
            return load_management_batch(session_factory, batch.id)
        rows = [row for row in current.components if row.sequence == sequence]
        if not rows:
            _freeze_composite_batch(
                session_factory, batch.id, now_provider(),
                "management_instruction_component_dropped",
            )
            return load_management_batch(session_factory, batch.id)
        for component in rows:
            if component.status == "confirmed":
                continue
            if component.status == "operator_required":
                _freeze_composite_batch(
                    session_factory, batch.id, now_provider(),
                    component.reason_code or "composite_component_operator_required",
                )
                return load_management_batch(session_factory, batch.id)
            if component.status in {"submitting", "awaiting_exchange"}:
                return current
            kwargs = {
                "batch_id": batch.id,
                "component_id": component.id,
                "deepcoin_client": deepcoin_client,
                "live_execution_gate": live_execution_gate,
                "now_provider": now_provider,
            }
            if component.component_kind == "replace_remaining_protection":
                instrument_id = _component_instrument_id(
                    session_factory, component.strategy_management_leg_id
                )
                spec = (
                    contract_spec_provider.get_contract_spec(instrument_id)
                    if contract_spec_provider is not None and instrument_id
                    else None
                )
                if spec is None or getattr(spec, "price_tick", None) is None:
                    _freeze_composite_batch(
                        session_factory, batch.id, now_provider(),
                        "target_contract_spec_unavailable",
                    )
                    return load_management_batch(session_factory, batch.id)
                kwargs.update(
                    price_tick=str(spec.price_tick),
                    backup_buffer_bps=str(backup_buffer_bps),
                )
            result = executors[component.component_kind](
                session_factory, **kwargs
            )
            if result.status == "operator_required":
                _freeze_composite_batch(
                    session_factory, batch.id, now_provider(),
                    result.reason_code or "composite_component_operator_required",
                )
                return load_management_batch(session_factory, batch.id)
            if result.status != "confirmed":
                return load_management_batch(session_factory, batch.id)

    try:
        return _complete_composite_batch(
            session_factory,
            batch_id=batch.id,
            deepcoin_client=deepcoin_client,
            completed_at=now_provider(),
        )
    except (CompositeManagementCompletionError, RuntimeError, ValueError) as exc:
        _freeze_composite_batch(
            session_factory, batch.id, now_provider(), str(exc)
        )
        return load_management_batch(session_factory, batch.id)


def _component_instrument_id(session_factory, management_leg_id: int | None):
    if management_leg_id is None:
        return None
    with session_factory() as session:
        leg = session.get(StrategyManagementLeg, int(management_leg_id))
        if leg is None:
            return None
        values = {
            str(row[0] or "").upper()
            for row in session.query(PositionProtectionLedger.instrument_id)
            .filter(
                PositionProtectionLedger.execution_order_leg_id
                == leg.execution_order_leg_id,
                PositionProtectionLedger.pos_id == leg.pos_id,
            )
            .all()
        }
    return next(iter(values)) if len(values) == 1 and "" not in values else None


def _composite_component_topology_is_exact(batch) -> bool:
    try:
        contract = json.loads(batch.management_contract_json or "{}")
        required = tuple(
            str(item.get("component_kind") if isinstance(item, dict) else item)
            for item in (contract.get("required_components") or [])
        )
        expected = {
            (int(leg.id), kind, sequence)
            for leg in batch.legs
            for sequence, kind in enumerate(required)
        }
        actual = [
            (
                int(component.strategy_management_leg_id),
                str(component.component_kind),
                int(component.sequence),
            )
            for component in batch.components
        ]
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(batch.legs and required) and len(actual) == len(expected) and set(actual) == expected


def _freeze_composite_batch(session_factory, batch_id, now, reason):
    transition_batch(
        session_factory,
        int(batch_id),
        expected_statuses={"ready", "executing"},
        new_status="recovery_required",
        transitioned_at=now,
        reason_code=str(reason)[:128],
    )


def _complete_composite_batch(
    session_factory, *, batch_id: int, deepcoin_client: Any, completed_at: Any
):
    from telegram_kol_research.system_operator_bot import (
        persist_composite_management_completion_in_session,
    )

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, int(batch_id))
        if batch is None or batch.status != "executing":
            raise RuntimeError("composite_batch_completion_state_changed")
        raw = session.get(RawMessage, int(batch.raw_message_id))
        components = (
            session.query(StrategyManagementComponent)
            .filter(StrategyManagementComponent.management_batch_id == batch.id)
            .order_by(StrategyManagementComponent.sequence, StrategyManagementComponent.id)
            .all()
        )
        expected_leg_ids = {
            str(row[0])
            for row in session.query(StrategyManagementLeg.id).filter(
                StrategyManagementLeg.management_batch_id == batch.id
            ).all()
        }
        instruments = {
            str(row[0] or "").upper()
            for row in session.query(PositionProtectionLedger.instrument_id)
            .filter(PositionProtectionLedger.execution_binding_id == batch.execution_binding_id)
            .all()
        }
        if raw is None or not instruments or "" in instruments:
            raise RuntimeError("composite_completion_evidence_incomplete")
        pending = []
        for instrument_id in sorted(instruments):
            rows = deepcoin_client.list_trigger_orders_pending(inst_id=instrument_id)
            if not isinstance(rows, list):
                raise RuntimeError("composite_completion_pending_snapshot_incomplete")
            pending.extend(rows)
        contract = json.loads(batch.management_contract_json or "{}")
        validate_composite_management_completion(
            source_text=raw.text or "",
            contract=contract,
            batch_status="succeeded",
            components=components,
            pending_orders=pending,
            expected_leg_ids=expected_leg_ids,
        )
        evidence = [json.loads(row.evidence_json or "[]") for row in components]
        flattened = [item for rows in evidence for item in rows if isinstance(item, dict)]
        remaining = [item.get("remaining_size") for item in flattened if item.get("remaining_size") is not None]
        retained = [item.get("retained_take_profit_total") for item in flattened if item.get("retained_take_profit_total") is not None]
        batch.status = "succeeded"
        batch.reason_code = "composite_management_exchange_confirmed"
        batch.reconciled_at = completed_at
        batch.completed_at = completed_at
        batch.updated_at = completed_at
        persist_composite_management_completion_in_session(
            session,
            batch,
            summary={
                "batch_id": batch.id,
                "overall_state": "succeeded",
                "first_take_profit": "已消费并核验",
                "partial_close": (
                    f"剩余 {','.join(map(str, remaining))}" if remaining else "已核验"
                ),
                "protection": "主备止损已核验",
                "retained_take_profit_total": ",".join(map(str, retained)) or "0",
            },
        )
        session.commit()
        return load_management_batch(session_factory, batch.id)


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
    for extra_action in plan.cancel_actions[1:]:
        try:
            latest = _exchange_snapshot(
                deepcoin_client, desired["instrument_id"]
            )
            latest_position = _unique_live_position(
                latest["positions"], desired["pos_id"]
            )
            if latest_position is None:
                raise RuntimeError("target_live_position_not_unique")
            with session_factory() as session:
                latest_authority = build_position_mutation_authority(
                    session,
                    venue="deepcoin",
                    pos_id=desired["pos_id"],
                    live_position=latest_position,
                )
            extra_result = gateway.cancel_owned_position_sltp(
                authority=latest_authority,
                order_id=extra_action["order_id"],
                idempotency_key=(
                    f"{component.id}:cancel:{extra_action['order_id']}:"
                    f"attempt:{attempt_number}"
                ),
                before_submit=lambda intent_id: _append_tp_cancel_intent(
                    session_factory, component_id, intent_id
                ),
            )
            intent_ids.append(extra_result.intent_id)
            if extra_result.status != "submitted":
                target = (
                    "awaiting_exchange"
                    if extra_result.status == "recovery_required"
                    else "recovery_required"
                )
                _transition(
                    session_factory, component_id, "submitting", target,
                    now_provider(), "take_profit_cancel_outcome_unresolved",
                    {"intent_id": extra_result.intent_id},
                )
                return _current_result(
                    session_factory, component_id, intent_ids=intent_ids
                )
            latest = _exchange_snapshot(
                deepcoin_client, desired["instrument_id"]
            )
            if extra_action["order_id"] in _pending_ids(latest["pending"]):
                _transition(
                    session_factory, component_id, "submitting",
                    "awaiting_exchange", now_provider(),
                    "take_profit_cancel_pending_after_submit",
                    {"intent_id": extra_result.intent_id},
                )
                return _current_result(
                    session_factory, component_id, intent_ids=intent_ids
                )
            reconcile_submitted_position_mutation_intents(
                session_factory,
                pending_trigger_orders=latest["pending"],
                order_history=latest["history"],
                trade_fills=latest["fills"],
                reconciled_at=now_provider(),
            )
        except Exception as exc:
            _transition(
                session_factory, component_id, "submitting",
                "awaiting_exchange", now_provider(),
                "take_profit_multi_cancel_readback_incomplete",
                {"error_type": type(exc).__name__},
            )
            return _current_result(
                session_factory, component_id, intent_ids=intent_ids
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
    try:
        unresolved_delta = target_remaining_close_delta(
            trusted_start_size=desired["trusted_start_size"],
            target_remaining_size=desired["target_remaining_size"],
            current_size=remaining,
            quantity_step=desired["quantity_step"],
            min_quantity=desired["min_quantity"],
        )
    except ManagementSizingError as exc:
        _transition(
            session_factory, component_id, "submitting", "operator_required",
            now_provider(), str(exc),
            {"intent_id": result.intent_id, "remaining_size": remaining},
        )
        return _current_result(
            session_factory, component_id, close_intent_ids=intent_ids
        )
    if unresolved_delta == "0":
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


def execute_protection_replacement_component(
    session_factory,
    *,
    batch_id: int,
    component_id: int,
    deepcoin_client: Any,
    live_execution_gate: Callable[[], bool],
    now_provider: Callable[[], Any],
    price_tick: str,
    backup_buffer_bps: str,
) -> CompositeComponentExecutionResult:
    """Own two replacement stops before cancelling either old stop."""

    now = now_provider()
    loaded = _load_component(
        session_factory, batch_id, component_id,
        expected_kind="replace_remaining_protection",
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
                    reason_code="protection_replacement_retry_exhausted",
                )
                session.commit()
        return _current_result(session_factory, component_id)
    with session_factory() as session:
        predecessors = session.query(StrategyManagementComponent).filter(
            StrategyManagementComponent.management_batch_id == batch.id,
            StrategyManagementComponent.strategy_management_leg_id == leg.id,
            StrategyManagementComponent.sequence < component.sequence,
        ).all()
        if len(predecessors) != 2 or any(row.status != "confirmed" for row in predecessors):
            return CompositeComponentExecutionResult(
                status="recovery_required", component_id=component.id,
                reason_code="composite_predecessor_not_confirmed",
            )
    with session_factory() as session:
        if not claim_management_component(
            session, component_id=component.id, now=now,
            stale_before=now - timedelta(minutes=5),
        ):
            session.rollback()
            return _current_result(session_factory, component.id)
        session.commit()
    with session_factory() as session:
        claimed = session.get(StrategyManagementComponent, component_id)
        if claimed is None or claimed.status != "preflighting":
            return _current_result(session_factory, component_id)
        attempt_number = int(claimed.attempt_count)
    try:
        positions = deepcoin_client.list_positions(inst_id=desired["instrument_id"])
        if not isinstance(positions, list):
            raise RuntimeError("positions_snapshot_incomplete")
        live_position = _unique_live_position(positions, desired["pos_id"])
        if live_position is None:
            raise RuntimeError("target_live_position_not_unique")
        if target_remaining_close_delta(
            trusted_start_size=desired["trusted_start_size"],
            target_remaining_size=desired["target_remaining_size"],
            current_size=live_position.get("pos"),
            quantity_step=desired["quantity_step"],
            min_quantity=desired["min_quantity"],
        ) != "0":
            raise RuntimeError("partial_close_component_not_converged")
        with session_factory() as session:
            ledger_rows = session.query(PositionProtectionLedger).filter(
                PositionProtectionLedger.execution_binding_id == batch.execution_binding_id,
                PositionProtectionLedger.execution_order_leg_id == leg.execution_order_leg_id,
                PositionProtectionLedger.pos_id == leg.pos_id,
            ).all()
            retained_total = retained_take_profit_total(
                ledger_rows,
                execution_binding_id=batch.execution_binding_id,
                execution_order_leg_id=leg.execution_order_leg_id,
                pos_id=leg.pos_id,
                live_position_size=live_position.get("pos"),
            )
            old_stops = [
                row for row in ledger_rows
                if row.status == "verified"
                and row.purpose in {"stop_loss", "backup_stop"}
            ]
            prior_plan = desired.get("protection_replacement_execution") or {}
            prior_old_ids = [
                str(value) for value in prior_plan.get("old_stop_order_ids") or []
            ]
            if prior_old_ids:
                old_stops = [
                    row for row in ledger_rows if row.order_id in prior_old_ids
                ]
            old_stop_prices = [str(row.trigger_price) for row in old_stops]
            old_stop_ids = [str(row.order_id) for row in old_stops]
        requested_stop = (
            contract.stop_price
            if contract.stop_mode == "explicit_price"
            else desired["avg_entry_price"]
        )
        market_price = (
            live_position.get("markPx")
            or live_position.get("last")
            or live_position.get("lastPx")
        )
        decision = plan_composite_stop_replacement(
            side=contract.side,
            requested_stop=requested_stop,
            market_price=market_price,
            price_tick=price_tick,
            backup_buffer_bps=backup_buffer_bps,
            existing_stop_prices=old_stop_prices,
        )
    except (ValueError, ManagementSizingError, BreakEvenMarketPolicyError, RuntimeError) as exc:
        reason = str(exc)
        terminal = reason in {
            "retained_take_profit_exceeds_position",
            "position_size_increased_after_snapshot",
            "position_below_target_remaining",
            "requested_stop_market_side_invalid",
        }
        _transition(
            session_factory, component.id, "preflighting",
            "operator_required" if terminal else "recovery_required",
            now, reason, {"phase": "protection_preflight"},
        )
        return _current_result(session_factory, component.id)

    plan = {
        "primary_stop": decision.primary_stop,
        "backup_stop": decision.backup_stop,
        "old_stop_order_ids": old_stop_ids,
        "retained_take_profit_total": retained_total,
    }
    _persist_protection_plan_and_enter_submitting(
        session_factory, component_id=component.id, plan=plan, now=now
    )
    payload_base = {
        "instId": desired["instrument_id"],
        "posSide": contract.side,
        "posId": desired["pos_id"],
        "sz": desired["target_remaining_size"],
        "slTriggerPxType": "last",
        "slOrdPx": "-1",
    }
    created_order_ids: list[str] = []
    for role, stop_price in (
        ("primary", decision.primary_stop),
        ("backup", decision.backup_stop),
    ):
        try:
            response = submit_exact_position_sltp(
                session_factory=session_factory,
                deepcoin_client=deepcoin_client,
                pos_id=desired["pos_id"],
                payload={**payload_base, "slTriggerPx": stop_price},
                idempotency_key=f"{component.id}:set:{role}",
                live_execution_gate=live_execution_gate,
                now_provider=now_provider,
                require_readback=True,
                ledger_purpose="stop_loss" if role == "primary" else "backup_stop",
            )
            order_id = _response_order_id(response)
            if not order_id:
                raise RuntimeError("protection_replacement_missing_order_id")
            if order_id in created_order_ids:
                _transition(
                    session_factory, component.id, "submitting", "operator_required",
                    now_provider(), "duplicate_new_stop_order_id",
                    {"new_order_ids": created_order_ids + [order_id]},
                )
                return _current_result(session_factory, component.id)
            created_order_ids.append(order_id)
        except Exception as exc:
            _transition(
                session_factory, component.id, "submitting", "awaiting_exchange",
                now_provider(), "replacement_stop_readback_unresolved",
                {"role": role, "error_type": type(exc).__name__},
            )
            return _current_result(session_factory, component.id)

    # Both new stops are now read back and canonically owned. Only now may old
    # protection be cancelled.
    for old_order_id in sorted(old_stop_ids):
        positions = deepcoin_client.list_positions(inst_id=desired["instrument_id"])
        live_position = _unique_live_position(positions, desired["pos_id"])
        pending_before_cancel = deepcoin_client.list_trigger_orders_pending(
            inst_id=desired["instrument_id"]
        )
        try:
            with session_factory() as session:
                authority = build_position_mutation_authority(
                    session, venue="deepcoin", pos_id=desired["pos_id"],
                    live_position=live_position,
                )
            result = PositionMutationGateway(
                session_factory=session_factory,
                deepcoin_client=deepcoin_client,
                live_execution_gate=live_execution_gate,
                now_provider=now_provider,
            ).cancel_owned_position_sltp(
                authority=authority,
                order_id=old_order_id,
                idempotency_key=f"{component.id}:cancel-old:{old_order_id}",
                retry_pending_order=(
                    _pending_row(pending_before_cancel, old_order_id)
                    if attempt_number > 1
                    else None
                ),
            )
        except Exception as exc:
            _transition(
                session_factory, component.id, "submitting", "awaiting_exchange",
                now_provider(), "old_stop_cancel_unresolved",
                {"order_id": old_order_id, "error_type": type(exc).__name__},
            )
            return _current_result(session_factory, component.id)
        if result.status not in {"submitted", "confirmed"}:
            target_status = (
                "awaiting_exchange" if result.status == "recovery_required"
                else "recovery_required"
            )
            _transition(
                session_factory, component.id, "submitting", target_status,
                now_provider(), "old_stop_cancel_unresolved",
                {"order_id": old_order_id, "intent_id": result.intent_id},
            )
            return _current_result(session_factory, component.id)
        pending = deepcoin_client.list_trigger_orders_pending(
            inst_id=desired["instrument_id"]
        )
        if old_order_id in _pending_ids(pending):
            _transition(
                session_factory, component.id, "submitting", "awaiting_exchange",
                now_provider(), "old_stop_cancel_pending",
                {"order_id": old_order_id, "intent_id": result.intent_id},
            )
            return _current_result(session_factory, component.id)
        reconcile_submitted_position_mutation_intents(
            session_factory, pending_trigger_orders=pending,
            order_history=[], trade_fills=[], reconciled_at=now_provider(),
        )
    try:
        final_positions = deepcoin_client.list_positions(
            inst_id=desired["instrument_id"]
        )
        final_position = _unique_live_position(final_positions, desired["pos_id"])
        if final_position is None:
            raise ValueError("target_live_position_not_unique")
        with session_factory() as session:
            final_rows = session.query(PositionProtectionLedger).filter(
                PositionProtectionLedger.execution_binding_id == batch.execution_binding_id,
                PositionProtectionLedger.execution_order_leg_id == leg.execution_order_leg_id,
                PositionProtectionLedger.pos_id == leg.pos_id,
            ).all()
            retained_take_profit_total(
                final_rows,
                execution_binding_id=batch.execution_binding_id,
                execution_order_leg_id=leg.execution_order_leg_id,
                pos_id=leg.pos_id,
                live_position_size=final_position.get("pos"),
            )
            verified_new = {
                row.order_id: row.purpose
                for row in final_rows
                if row.order_id in created_order_ids and row.status == "verified"
            }
        if verified_new != {
            created_order_ids[0]: "stop_loss",
            created_order_ids[1]: "backup_stop",
        }:
            raise ValueError("replacement_stop_ownership_incomplete")
    except Exception as exc:
        _transition(
            session_factory, component.id, "submitting", "operator_required",
            now_provider(), str(exc), {"phase": "protection_final_invariant"},
        )
        return _current_result(session_factory, component.id)
    _transition(
        session_factory, component.id, "submitting", "confirmed", now_provider(),
        evidence={
            "new_stop_order_ids": created_order_ids,
            "cancelled_old_stop_order_ids": sorted(old_stop_ids),
            "retained_take_profit_total": retained_total,
        },
    )
    return _current_result(session_factory, component.id)


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
        existing = desired.get("take_profit_consumption_execution")
        planned_ids = list(plan.cancel_order_ids)
        intent_ids = [int(intent_id)]
        if existing is not None:
            original_ids = [
                str(value) for value in existing.get("cancel_order_ids") or []
            ]
            if not set(planned_ids).issubset(set(original_ids)):
                raise RuntimeError("management_instruction_component_dropped")
            planned_ids = original_ids
            intent_ids = [
                int(value) for value in existing.get("cancel_intent_ids") or []
            ]
            if int(intent_id) not in intent_ids:
                intent_ids.append(int(intent_id))
        execution = {
            "cancel_order_ids": planned_ids,
            "cancel_intent_ids": intent_ids,
            "evidence_tier": plan.evidence_tier,
        }
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


def _append_tp_cancel_intent(session_factory, component_id: int, intent_id: int):
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, int(component_id))
        if component is None or component.status != "submitting":
            raise RuntimeError("management_component_not_submitting")
        desired = json.loads(component.desired_json or "{}")
        execution = desired.get("take_profit_consumption_execution") or {}
        intent_ids = [int(value) for value in execution.get("cancel_intent_ids", [])]
        if int(intent_id) not in intent_ids:
            intent_ids.append(int(intent_id))
        execution["cancel_intent_ids"] = intent_ids
        desired["take_profit_consumption_execution"] = execution
        component.desired_json = json.dumps(
            desired, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
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


def _persist_protection_plan_and_enter_submitting(
    session_factory, *, component_id: int, plan: dict[str, Any], now: Any
) -> None:
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, component_id)
        if component is None or component.status != "preflighting":
            raise RuntimeError("management_component_not_preflighting")
        desired = json.loads(component.desired_json)
        desired["protection_replacement_execution"] = plan
        component.desired_json = json.dumps(
            desired, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if not transition_management_component(
            session, component_id=component_id, expected_status="preflighting",
            new_status="submitting", now=now,
            evidence={"phase": "create_new_stops", **plan},
        ):
            raise RuntimeError("management_component_submit_claim_lost")
        session.commit()


def _response_order_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    for key in ("ordId", "orderId", "id"):
        if response.get(key) not in (None, ""):
            return str(response[key])
    data = response.get("data")
    if isinstance(data, dict):
        return _response_order_id(data)
    if isinstance(data, list) and len(data) == 1:
        return _response_order_id(data[0])
    return None


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
