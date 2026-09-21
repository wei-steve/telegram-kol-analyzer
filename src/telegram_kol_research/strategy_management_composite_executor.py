"""Durable execution of ordered composite management components."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Callable

from telegram_kol_research.management_stop_price_gate import validate_batch_stops, reject_execution_stop

from telegram_kol_research.models import (
    PositionMutationIntent,
    PositionProtectionLedger,
    PositionTakeProfitOrder,
    RawMessage,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyManagementLeg,
)
from telegram_kol_research.protection_authority import resolve_protection_authority
from telegram_kol_research.position_authority_lock import (
    serialized_position_authority_mutation,
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
from telegram_kol_research.break_even_reference import break_even_target_price
from telegram_kol_research.protection_ledger import retained_take_profit_total
from telegram_kol_research.protection_replacement_persistence import (
    VerifiedProtectionReplacement,
    persist_verified_protection_replacement,
)
from telegram_kol_research.strategy_records import (
    CompositeManagementCompletionError,
    validate_composite_management_completion,
)


logger = logging.getLogger(__name__)

# The break-even remainder close (design
# ``docs/plans/2026-09-21-composite-remainder-market-close-design.md``). All of
# these names are free text in their own columns; none of them is a new status
# value, a new component kind, or a schema change, so the previous release can
# read every row this branch writes.
REMAINDER_CLOSE_EXECUTION_KEY = "remainder_close_execution"
REMAINDER_CLOSE_OUTCOME = "remainder_closed_at_market"
REMAINDER_CLOSE_BATCH_REASON = "composite_remainder_market_closed"
REMAINDER_CLOSE_INCIDENT_TYPE = "composite_break_even_remainder_closed"
_UNRESOLVED_CLOSE_INTENT_STATUSES = (
    "reserved", "submitting", "submitted", "recovery_required",
)


@dataclass(frozen=True, slots=True)
class CompositeComponentExecutionResult:
    status: str
    component_id: int
    reason_code: str | None = None
    proven_filled_quantity: str = "0"
    cancel_intent_ids: tuple[int, ...] = ()
    close_intent_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class _RemainderCloseQuote:
    """One fresh ticker read, and what it says about the break-even side."""

    price: str | None = None
    price_field: str | None = None
    reason: str | None = None


@serialized_position_authority_mutation
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
    if batch.status in {"ready", "executing"}:
        now = now_provider()
        stop_gate = validate_batch_stops(session_factory, batch=batch, client=deepcoin_client, now=now)
        if stop_gate is not None:
            reject_execution_stop(session_factory, batch=batch, result=stop_gate, now=now)
            return load_management_batch(session_factory, batch.id)
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

    # The contract has always said `cancel_deferred_entries` for this intent
    # and the non-composite reduce path has always honoured it; the composite
    # path never did. A KOL who says "reduce and move the stop to cost" must
    # not have a resting second entry leg fill afterwards and re-grow the
    # position. Same point in the sequence as `execute_management_batch`:
    # after the batch is claimed, before any write it makes.
    try:
        if load_management_contract(
            batch.management_contract_json or ""
        ).cancel_deferred_entries:
            _cancel_batch_deferred_entry_legs(
                session_factory,
                batch_id=batch.id,
                deepcoin_client=deepcoin_client,
                now=now_provider(),
            )
    except Exception as exc:  # noqa: BLE001 - same failure semantics as above
        _freeze_composite_batch(
            session_factory, batch.id, now_provider(),
            _deferred_entry_cancel_freeze_reason(exc),
        )
        return load_management_batch(session_factory, batch.id)

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


def _deferred_entry_cancel_freeze_reason(exc: Exception) -> str:
    """The reason codes `execute_management_batch` writes, for the same causes."""

    from telegram_kol_research.deepcoin_client import DeepcoinDefiniteRejection

    if isinstance(exc, DeepcoinDefiniteRejection):
        return "deferred_entry_cancel_race_detected"
    return "deferred_entry_cancel_preflight_failed"


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
        closed = _remainder_closed_legs(session, batch=batch, components=components)
        if closed.rows:
            # Design 3.4. The books must be terminalized in the same
            # transaction that makes the batch ``succeeded``: the moment the
            # batch leaves a managed state the manual-close scan stops skipping
            # this position, and a position that is flat on the exchange but
            # still open on our side is recorded as a manual exit within ~90s.
            _require_remainder_terminalization_identity(session, batch=batch)
            from telegram_kol_research.strategy_management_reconciliation import (
                _terminalize_full_close,
            )

            _terminalize_full_close(
                session, batch=batch, legs=closed.rows, now=completed_at
            )
        batch.status = "succeeded"
        batch.reason_code = (
            REMAINDER_CLOSE_BATCH_REASON
            if closed.rows
            else "composite_management_exchange_confirmed"
        )
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
                    f"剩余 0（保本价 {closed.requested_stop} 已被市价 "
                    f"{closed.market_price} 越过，剩余仓位已市价全平）"
                    if closed.rows
                    else (
                        f"剩余 {','.join(map(str, remaining))}"
                        if remaining
                        else "已核验"
                    )
                ),
                "protection": (
                    "未挂保本止损：仓位已全平，原保护单随仓位失效"
                    if closed.rows
                    else "主备止损已核验"
                ),
                "retained_take_profit_total": ",".join(map(str, retained)) or "0",
            },
        )
        session.commit()
        return load_management_batch(session_factory, batch.id)


@dataclass(frozen=True, slots=True)
class _RemainderClosedLegs:
    rows: tuple[Any, ...] = ()
    requested_stop: str = "-"
    market_price: str = "-"


def _remainder_closed_legs(session, *, batch, components) -> _RemainderClosedLegs:
    """The legs whose protection component ended by closing the remainder."""

    leg_ids: list[int] = []
    requested_stop = "-"
    market_price = "-"
    for component in components:
        if str(component.component_kind) != "replace_remaining_protection":
            continue
        try:
            history = json.loads(component.evidence_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        match = next(
            (
                item
                for item in (history if isinstance(history, list) else [])
                if isinstance(item, dict)
                and item.get("outcome") == REMAINDER_CLOSE_OUTCOME
            ),
            None,
        )
        if match is None or component.strategy_management_leg_id is None:
            continue
        leg_ids.append(int(component.strategy_management_leg_id))
        requested_stop = str(match.get("requested_stop") or requested_stop)
        market_price = str(match.get("ticker_last") or market_price)
    if not leg_ids:
        return _RemainderClosedLegs()
    rows = (
        session.query(StrategyManagementLeg)
        .filter(StrategyManagementLeg.id.in_(leg_ids))
        .order_by(StrategyManagementLeg.id.asc())
        .all()
    )
    if len(rows) != len(set(leg_ids)):
        raise RuntimeError("composite_remainder_terminalization_identity_mismatch")
    return _RemainderClosedLegs(
        rows=tuple(rows),
        requested_stop=requested_stop,
        market_price=market_price,
    )


def _require_remainder_terminalization_identity(session, *, batch) -> None:
    from telegram_kol_research.models import ExecutionBinding, StrategyLifecycle

    binding = session.get(ExecutionBinding, batch.execution_binding_id)
    lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
    if (
        binding is None
        or lifecycle is None
        or str(lifecycle.lifecycle_status or "") != "entered"
        or lifecycle.exit_reason is not None
        or str(binding.status or "").lower() not in {"open", "active", "stale"}
    ):
        raise RuntimeError("composite_remainder_terminalization_identity_mismatch")


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
        now_provider(), None,
        {
            "intent_id": result.intent_id,
            # Additive evidence only: nothing reads this to decide anything.
            # It is here because when a stage filled by itself, what this
            # component believed had already been taken is otherwise
            # unrecoverable from the record -- and that is precisely the number
            # a person needs to judge whether component two's fraction, which
            # is taken of the *current* position, is the intended one.
            "proven_filled_quantity": plan.proven_filled_quantity,
            "evidence_tier": plan.evidence_tier,
        },
    )
    return _current_result(
        session_factory,
        component_id,
        proven_filled_quantity=plan.proven_filled_quantity,
        intent_ids=intent_ids,
    )


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
    live_position: Any = None
    requested_stop: Any = None
    market_price: Any = None
    try:
        positions = deepcoin_client.list_positions(inst_id=desired["instrument_id"])
        if not isinstance(positions, list):
            raise RuntimeError("positions_snapshot_incomplete")
        if desired.get(REMAINDER_CLOSE_EXECUTION_KEY):
            # A persisted decision to close the remainder is sticky, and it is
            # resumed ahead of every other preflight judgement -- including the
            # unique-position check, because "this position is already gone" is
            # one of the outcomes only this branch knows how to read.
            return _resume_remainder_close(
                session_factory,
                batch=batch,
                component=component,
                leg=leg,
                desired=desired,
                positions=positions,
                deepcoin_client=deepcoin_client,
                live_execution_gate=live_execution_gate,
                now_provider=now_provider,
                attempt_number=attempt_number,
            )
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
            # The break-even target: the strategy's price when this batch
            # carries one, and otherwise ``desired["avg_entry_price"]``
            # verbatim, which is the same value this leg's column holds.
            else break_even_target_price(leg)
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
        if (
            reason == "requested_stop_market_side_invalid"
            and live_position is not None
            and requested_stop is not None
            and market_price is not None
            and _break_even_fallback_applies(contract=contract, desired=desired)
        ):
            # Design 3.1/3.2. The break-even stop can never be armed, so the
            # remaining position leaves at market instead of sitting behind a
            # stop the instruction has already disowned. Every other reason,
            # and every unmet condition, keeps today's disposition exactly.
            return _begin_remainder_close(
                session_factory,
                batch=batch,
                component=component,
                leg=leg,
                contract=contract,
                desired=desired,
                requested_stop=requested_stop,
                market_price=market_price,
                price_tick=price_tick,
                backup_buffer_bps=backup_buffer_bps,
                attempt_number=attempt_number,
                deepcoin_client=deepcoin_client,
                live_execution_gate=live_execution_gate,
                now_provider=now_provider,
            )
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
            retained_take_profits = tuple(
                VerifiedProtectionReplacement(
                    role="take_profit",
                    order_id=str(row.order_id),
                    trigger_price=str(row.trigger_price or ""),
                    size_text=row.size_text,
                )
                for row in final_rows
                if row.purpose == "take_profit" and row.status == "verified"
            )
        if verified_new != {
            created_order_ids[0]: "stop_loss",
            created_order_ids[1]: "backup_stop",
        }:
            raise ValueError("replacement_stop_ownership_incomplete")
        with session_factory() as session:
            persist_verified_protection_replacement(
                session,
                venue="deepcoin",
                execution_binding_id=batch.execution_binding_id,
                execution_order_leg_id=leg.execution_order_leg_id,
                strategy_instance_id=batch.strategy_instance_id,
                pos_id=leg.pos_id,
                instrument_id=desired["instrument_id"],
                side=contract.side,
                source="composite_management_replacement",
                replacement_identity=f"component:{component.id}",
                replacements=(
                    VerifiedProtectionReplacement(
                        role="primary_stop",
                        order_id=created_order_ids[0],
                        trigger_price=decision.primary_stop,
                        size_text=desired["target_remaining_size"],
                    ),
                    VerifiedProtectionReplacement(
                        role="backup_stop",
                        order_id=created_order_ids[1],
                        trigger_price=decision.backup_stop,
                        size_text=desired["target_remaining_size"],
                    ),
                    *retained_take_profits,
                ),
                seen_at=now_provider(),
            )
            session.commit()
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


def _break_even_fallback_applies(*, contract, desired) -> bool:
    """Design 3.1, the two conditions that can be answered without any I/O.

    ``explicit_price`` contracts are deliberately excluded: a price a person
    put in a message is not a break-even instruction, so a stop that cannot be
    armed there still stops for a person. And a component that has already
    started creating replacement stops has written to the exchange on the
    protection route; it never crosses over to the close route.
    """

    return (
        str(getattr(contract, "stop_mode", "") or "") == "actual_entry_price"
        and not desired.get("protection_replacement_execution")
        and not desired.get(REMAINDER_CLOSE_EXECUTION_KEY)
    )


def _confirm_market_side_invalid(
    *,
    deepcoin_client: Any,
    instrument_id: str,
    side: Any,
    requested_stop: Any,
    price_tick: Any,
    backup_buffer_bps: Any,
) -> _RemainderCloseQuote:
    """Design 3.1 condition 6: a second opinion from a price that is not cached.

    The position row's ``markPx`` can be up to one reconcile round old and is
    not the price the stop would have triggered on. This read is the same one
    ``break_even_by_market`` uses -- ``last``, never cached -- and the same
    validation, verbatim. Only when it *also* says the requested stop is on the
    wrong side of the market does anything get closed.
    """

    try:
        quote = deepcoin_client.get_ticker_quote(inst_id=instrument_id)
    except Exception:  # noqa: BLE001 - any failure is "we do not know"
        return _RemainderCloseQuote(reason="break_even_market_quote_unavailable")
    if (
        not isinstance(quote, dict)
        or str(quote.get("instrument_id") or "").upper()
        != str(instrument_id or "").upper()
        or quote.get("price") in (None, "")
        or quote.get("price_field") not in {"last", "lastPx"}
    ):
        return _RemainderCloseQuote(reason="break_even_market_quote_unavailable")
    try:
        plan_composite_stop_replacement(
            side=side,
            requested_stop=requested_stop,
            market_price=quote["price"],
            price_tick=price_tick,
            backup_buffer_bps=backup_buffer_bps,
            # No existing stop is consulted here. This call answers one
            # question only -- can this stop be armed at all -- and
            # ``keep_tighter_stop`` is provably unreachable whenever the answer
            # is no (design 1.2).
            existing_stop_prices=(),
        )
    except BreakEvenMarketPolicyError as exc:
        if str(exc) == "requested_stop_market_side_invalid":
            return _RemainderCloseQuote(
                price=str(quote["price"]),
                price_field=str(quote["price_field"]),
            )
        return _RemainderCloseQuote(reason="break_even_market_quote_unavailable")
    return _RemainderCloseQuote(reason="break_even_market_side_disagreement")


def _begin_remainder_close(
    session_factory,
    *,
    batch,
    component,
    leg,
    contract,
    desired: dict[str, Any],
    requested_stop: Any,
    market_price: Any,
    price_tick: Any,
    backup_buffer_bps: Any,
    attempt_number: int,
    deepcoin_client: Any,
    live_execution_gate: Callable[[], bool],
    now_provider: Callable[[], Any],
) -> CompositeComponentExecutionResult:
    """Design 3.2 step 1: prove the last two conditions, then make it durable."""

    component_id = int(component.id)
    now = now_provider()
    if not live_execution_gate():
        _transition(
            session_factory, component_id, "preflighting", "recovery_required",
            now, "live_execution_disabled", {"phase": "remainder_close_gate"},
        )
        return _current_result(session_factory, component_id)
    quote = _confirm_market_side_invalid(
        deepcoin_client=deepcoin_client,
        instrument_id=desired["instrument_id"],
        side=contract.side,
        requested_stop=requested_stop,
        price_tick=price_tick,
        backup_buffer_bps=backup_buffer_bps,
    )
    if quote.reason is not None:
        _transition(
            session_factory, component_id, "preflighting", "recovery_required",
            now, quote.reason, {"phase": "remainder_close_quote"},
        )
        return _current_result(session_factory, component_id)
    plan = {
        "reason": "requested_stop_market_side_invalid",
        "requested_stop": str(requested_stop),
        "primary_stop": _tick_normalized_stop(
            requested_stop, side=contract.side, price_tick=price_tick
        ),
        "position_market_price": str(market_price),
        "ticker_last": str(quote.price),
        "ticker_field": str(quote.price_field),
        "decided_at": str(now),
        "phase": "cancel_deferred_entries",
        "deferred_entries_cancelled": False,
        "intent_ids": [],
    }
    try:
        _persist_remainder_close_plan_and_enter_submitting(
            session_factory, component_id=component_id, plan=plan, now=now,
        )
    except Exception as exc:  # noqa: BLE001
        _transition(
            session_factory, component_id, "preflighting", "recovery_required",
            now_provider(), "remainder_close_plan_not_persisted",
            {"error_type": type(exc).__name__},
        )
        return _current_result(session_factory, component_id)
    return _run_remainder_close(
        session_factory,
        batch=batch,
        component=component,
        leg=leg,
        desired=desired,
        deepcoin_client=deepcoin_client,
        live_execution_gate=live_execution_gate,
        now_provider=now_provider,
        attempt_number=attempt_number,
    )


def _resume_remainder_close(
    session_factory,
    *,
    batch,
    component,
    leg,
    desired: dict[str, Any],
    positions: list,
    deepcoin_client: Any,
    live_execution_gate: Callable[[], bool],
    now_provider: Callable[[], Any],
    attempt_number: int,
) -> CompositeComponentExecutionResult:
    """Design 3.2 step 6: re-enter a decision that is already durable."""

    component_id = int(component.id)
    now = now_provider()
    plan = dict(desired.get(REMAINDER_CLOSE_EXECUTION_KEY) or {})
    try:
        _persist_remainder_close_plan_and_enter_submitting(
            session_factory, component_id=component_id, plan=plan, now=now,
        )
    except Exception as exc:  # noqa: BLE001
        _transition(
            session_factory, component_id, "preflighting", "recovery_required",
            now_provider(), "remainder_close_resume_conflict",
            {"error_type": type(exc).__name__},
        )
        return _current_result(session_factory, component_id)
    matches = [
        row for row in positions
        if str(row.get("posId") or "") == str(desired["pos_id"])
    ]
    if len(matches) > 1:
        _transition(
            session_factory, component_id, "submitting", "recovery_required",
            now_provider(), "target_live_position_not_unique",
        )
        return _current_result(session_factory, component_id)
    flat = _position_is_flat(matches[0]) if matches else True
    if flat is None:
        _transition(
            session_factory, component_id, "submitting", "recovery_required",
            now_provider(), "remainder_close_position_size_invalid",
        )
        return _current_result(session_factory, component_id)
    if flat:
        return _finish_absent_remainder_position(
            session_factory,
            batch=batch,
            component=component,
            leg=leg,
            desired=desired,
            now_provider=now_provider,
        )
    if not plan.get("deferred_entries_cancelled"):
        # We crashed while cancelling this batch's own unfilled entry orders.
        # Whether one of them was cancelled, or filled in the meantime, is not
        # knowable from here, and re-running the cancel could race a fill, so
        # this stops for a person. The original stop is still armed.
        _transition(
            session_factory, component_id, "submitting", "operator_required",
            now_provider(), "remainder_close_interrupted_before_close",
            {"phase": str(plan.get("phase") or "")},
        )
        return _current_result(session_factory, component_id)
    return _run_remainder_close(
        session_factory,
        batch=batch,
        component=component,
        leg=leg,
        desired=desired,
        deepcoin_client=deepcoin_client,
        live_execution_gate=live_execution_gate,
        now_provider=now_provider,
        attempt_number=attempt_number,
    )


def _run_remainder_close(
    session_factory,
    *,
    batch,
    component,
    leg,
    desired: dict[str, Any],
    deepcoin_client: Any,
    live_execution_gate: Callable[[], bool],
    now_provider: Callable[[], Any],
    attempt_number: int,
) -> CompositeComponentExecutionResult:
    """Design 3.2 steps 2-5. The component is durably ``submitting`` already."""

    component_id = int(component.id)
    plan = _load_remainder_close_plan(session_factory, component_id)

    # Step 2. An unfilled entry leg that fills after this close would re-grow
    # the very position the instruction just asked us to leave.
    if not plan.get("deferred_entries_cancelled"):
        try:
            cancelled_leg_ids = _cancel_batch_deferred_entry_legs(
                session_factory,
                batch_id=batch.id,
                deepcoin_client=deepcoin_client,
                now=now_provider(),
            )
        except Exception as exc:  # noqa: BLE001
            _transition(
                session_factory, component_id, "submitting", "operator_required",
                now_provider(), "remainder_close_deferred_entry_cancel_failed",
                {
                    "phase": "cancel_deferred_entries",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:120],
                },
            )
            return _current_result(session_factory, component_id)
        plan = _update_remainder_close_plan(
            session_factory,
            component_id,
            {
                "deferred_entries_cancelled": True,
                "cancelled_deferred_entry_leg_ids": cancelled_leg_ids,
                "phase": "close",
            },
        )

    # Step 3. The reduction confirms on position size, so its own close intent
    # is usually still ``submitted``; the gateway would block this close over
    # it (design 9.3). Reconcile first, and wait rather than block.
    try:
        snapshot = _exchange_snapshot(deepcoin_client, desired["instrument_id"])
    except Exception as exc:  # noqa: BLE001
        _transition(
            session_factory, component_id, "submitting", "recovery_required",
            now_provider(), "remainder_close_snapshot_incomplete",
            {"error_type": type(exc).__name__},
        )
        return _current_result(session_factory, component_id)
    reconcile_submitted_position_mutation_intents(
        session_factory,
        pending_trigger_orders=snapshot["pending"],
        order_history=snapshot["history"],
        trade_fills=snapshot["fills"],
        reconciled_at=now_provider(),
    )
    if _unresolved_close_intent_exists(
        session_factory,
        pos_id=desired["pos_id"],
        execution_order_leg_id=leg.execution_order_leg_id,
    ):
        _transition(
            session_factory, component_id, "submitting", "recovery_required",
            now_provider(), "remainder_close_waiting_partial_close_confirmation",
        )
        return _current_result(session_factory, component_id)

    # Step 4. Rebuild authority from a fresh read; the ownership gate, the
    # position fingerprint recheck and the live gate all stay the gateway's.
    matches = [
        row for row in snapshot["positions"]
        if str(row.get("posId") or "") == str(desired["pos_id"])
    ]
    if len(matches) != 1:
        _transition(
            session_factory, component_id, "submitting", "recovery_required",
            now_provider(), "target_live_position_not_unique",
        )
        return _current_result(session_factory, component_id)
    live_position = matches[0]
    flat = _position_is_flat(live_position)
    if flat is None:
        _transition(
            session_factory, component_id, "submitting", "recovery_required",
            now_provider(), "remainder_close_position_size_invalid",
        )
        return _current_result(session_factory, component_id)
    if flat:
        return _finish_absent_remainder_position(
            session_factory, batch=batch, component=component, leg=leg,
            desired=desired, now_provider=now_provider,
        )
    try:
        with session_factory() as session:
            authority = build_position_mutation_authority(
                session, venue="deepcoin", pos_id=desired["pos_id"],
                live_position=live_position,
            )
    except PositionMutationAuthorityError as exc:
        _transition(
            session_factory, component_id, "submitting", "recovery_required",
            now_provider(), str(exc), {"phase": "remainder_close_authority"},
        )
        return _current_result(session_factory, component_id)

    size = str(live_position.get("pos"))
    # ``R`` instead of the reduction's ``A``: the gateway falls back to the
    # client order id when a response carries no ordId, so the two writes of
    # one batch/leg must never collide.
    client_order_id = f"CM{batch.id}L{leg.id}R{attempt_number}"
    intent_ids: list[int] = []

    def protect_before_write(intent_id: int) -> None:
        _append_remainder_close_intent(
            session_factory,
            component_id,
            intent_id=intent_id,
            pre_submit_size=size,
            client_order_id=client_order_id,
        )
        intent_ids.append(int(intent_id))

    try:
        result = PositionMutationGateway(
            session_factory=session_factory,
            deepcoin_client=deepcoin_client,
            live_execution_gate=live_execution_gate,
            now_provider=now_provider,
        ).close_exact_position(
            authority=authority,
            size=size,
            client_order_id=client_order_id,
            # ``:close:`` is kept verbatim so the production monitor's
            # duplicate-close check covers this write too, and the
            # ``{component id}:`` prefix keeps it visible to the reconciler.
            idempotency_key=(
                f"{component_id}:close:remainder:attempt:{attempt_number}"
            ),
            before_submit=protect_before_write,
        )
    except Exception as exc:  # noqa: BLE001 - an unknown outcome is never resent
        _transition(
            session_factory, component_id, "submitting", "awaiting_exchange",
            now_provider(), "remainder_close_outcome_unknown",
            {"error_type": type(exc).__name__},
        )
        return _current_result(
            session_factory, component_id, close_intent_ids=intent_ids
        )
    if result.intent_id not in intent_ids:
        intent_ids.append(int(result.intent_id))
        _append_remainder_close_intent(
            session_factory, component_id, intent_id=result.intent_id,
            pre_submit_size=size, client_order_id=client_order_id,
            require_submitting=False,
        )

    # Step 5.
    if result.status == "recovery_required":
        _transition(
            session_factory, component_id, "submitting", "awaiting_exchange",
            now_provider(), "remainder_close_outcome_unknown",
            {"intent_id": result.intent_id, "close_size": size},
        )
        return _current_result(
            session_factory, component_id, close_intent_ids=intent_ids
        )
    if result.status == "rejected":
        _transition(
            session_factory, component_id, "submitting", "recovery_required",
            now_provider(), "remainder_close_definitely_rejected",
            {"intent_id": result.intent_id, "close_size": size},
        )
        return _current_result(
            session_factory, component_id, close_intent_ids=intent_ids
        )
    if result.status != "submitted":
        _transition(
            session_factory, component_id, "submitting", "recovery_required",
            now_provider(), result.reason or f"remainder_close_{result.status}",
            {"intent_id": result.intent_id},
        )
        return _current_result(
            session_factory, component_id, close_intent_ids=intent_ids
        )
    try:
        refreshed = deepcoin_client.list_positions(inst_id=desired["instrument_id"])
        if not isinstance(refreshed, list):
            raise RuntimeError("positions_snapshot_incomplete")
    except Exception as exc:  # noqa: BLE001
        _transition(
            session_factory, component_id, "submitting", "awaiting_exchange",
            now_provider(), "remainder_close_post_write_snapshot_incomplete",
            {"intent_id": result.intent_id, "error_type": type(exc).__name__},
        )
        return _current_result(
            session_factory, component_id, close_intent_ids=intent_ids
        )
    remaining = [
        row for row in refreshed
        if str(row.get("posId") or "") == str(desired["pos_id"])
    ]
    if remaining and _position_is_flat(remaining[0]) is not True:
        _transition(
            session_factory, component_id, "submitting", "awaiting_exchange",
            now_provider(), "remainder_close_not_yet_flat",
            {"intent_id": result.intent_id, "close_size": size},
        )
        return _current_result(
            session_factory, component_id, close_intent_ids=intent_ids
        )
    _confirm_remainder_close(
        session_factory,
        batch=batch,
        component_id=component_id,
        leg=leg,
        desired=desired,
        close_intent_id=int(result.intent_id),
        evidence_tier="exact_position_absent_after_accepted_close",
        now=now_provider(),
    )
    return _current_result(
        session_factory, component_id, close_intent_ids=intent_ids
    )


def _finish_absent_remainder_position(
    session_factory, *, batch, component, leg, desired, now_provider,
) -> CompositeComponentExecutionResult:
    """The position is gone. Only a confirmed close of ours may claim it."""

    component_id = int(component.id)
    intents = _remainder_close_intents(session_factory, component_id)
    latest = intents[-1] if intents else None
    if latest is not None and str(latest.status) == "confirmed":
        _confirm_remainder_close(
            session_factory,
            batch=batch,
            component_id=component_id,
            leg=leg,
            desired=desired,
            close_intent_id=int(latest.id),
            evidence_tier="exact_close_intent_confirmed",
            now=now_provider(),
        )
        return _current_result(session_factory, component_id)
    _transition(
        session_factory, component_id, "submitting", "operator_required",
        now_provider(), "remainder_position_absent_without_confirmed_close",
        {"intent_id": int(latest.id) if latest is not None else None},
    )
    return _current_result(session_factory, component_id)


def _confirm_remainder_close(
    session_factory, *, batch, component_id, leg, desired, close_intent_id,
    evidence_tier, now,
) -> None:
    plan = _load_remainder_close_plan(session_factory, component_id)
    evidence = {
        "outcome": REMAINDER_CLOSE_OUTCOME,
        "close_intent_id": int(close_intent_id),
        "remaining_size": "0",
        "requested_stop": plan.get("requested_stop"),
        "primary_stop": plan.get("primary_stop"),
        "position_market_price": plan.get("position_market_price"),
        "ticker_last": plan.get("ticker_last"),
        "cancelled_deferred_entry_leg_ids": list(
            plan.get("cancelled_deferred_entry_leg_ids") or []
        ),
        "evidence_tier": str(evidence_tier),
    }
    _transition(
        session_factory, component_id, "submitting", "confirmed", now,
        evidence=evidence,
    )
    _record_remainder_close_incident(
        session_factory,
        batch=batch,
        component_id=component_id,
        leg=leg,
        desired=desired,
        plan=plan,
        now=now,
    )


def _record_remainder_close_incident(
    session_factory, *, batch, component_id, leg, desired, plan, now
) -> None:
    """Design 3.7: a low-severity ledger row, best effort, never a blocker."""

    from telegram_kol_research.runtime_incidents import record_runtime_incident

    summary = json.dumps(
        {
            "component": "strategy_management",
            "reason_code": REMAINDER_CLOSE_INCIDENT_TYPE,
            "raw_message_id": int(batch.raw_message_id),
            # ``_SUMMARY_FIELDS`` is a closed allowlist of scalars, so the
            # numbers ride inside ``impact``; the colons also keep every run
            # short enough that the opaque-value guard cannot fire.
            "impact": (
                "remainder:closed:"
                f"stop:{plan.get('primary_stop') or plan.get('requested_stop')}:"
                f"last:{plan.get('ticker_last')}"
            ),
            "operation": f"management_batch_{int(batch.id)}",
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        record_runtime_incident(
            session_factory,
            source_kind="strategy_management_component",
            source_record_id=str(int(component_id)),
            incident_type=REMAINDER_CLOSE_INCIDENT_TYPE,
            severity="low",
            fingerprint=hashlib.sha256(
                f"{REMAINDER_CLOSE_INCIDENT_TYPE}:{int(component_id)}".encode()
            ).hexdigest(),
            redacted_summary=summary,
            occurred_at=now,
            feature_policy_version="composite-remainder-market-close-v1",
            prompt_version="none",
            tool_policy_version="exact-position-close",
            diagnosis_json=json.dumps(
                {
                    "observed_state": {
                        "management_batch_id": int(batch.id),
                        "pos_id": str(desired.get("pos_id") or ""),
                        "execution_order_leg_id": int(
                            leg.execution_order_leg_id
                        ),
                        **{
                            key: plan.get(key)
                            for key in (
                                "requested_stop", "primary_stop",
                                "position_market_price", "ticker_last",
                                "ticker_field",
                                "cancelled_deferred_entry_leg_ids",
                            )
                        },
                    }
                },
                ensure_ascii=False,
            ),
            evidence_refs_json=json.dumps(
                [
                    f"raw_message:{int(batch.raw_message_id)}",
                    f"management_batch:{int(batch.id)}",
                ]
            ),
        )
    except Exception:  # noqa: BLE001 - the close must survive a ledger failure
        logger.warning(
            "failed to record %s for component_id=%s",
            REMAINDER_CLOSE_INCIDENT_TYPE,
            component_id,
            exc_info=True,
        )


def _cancel_batch_deferred_entry_legs(
    session_factory, *, batch_id: int, deepcoin_client: Any, now: Any
) -> list[int]:
    """Cancel this batch's own unfilled entry orders, at most once.

    Reuses the full-exit path's cancel verbatim -- identity checks, per-leg
    diagnostics and all. The one thing added here is idempotency: the composite
    batch executor is re-entered on every worker tick, while the non-composite
    path runs its cancel exactly once per execution, so a second tick would
    otherwise find the legs already cancelled and fail closed on them.

    A snapshot that cannot be parsed still fails closed, because the caller
    must not be able to mistake "no deferred legs" for "we could not tell".
    """

    from telegram_kol_research.models import ExecutionOrderLeg
    from telegram_kol_research.strategy_management_executor import (
        _cancel_deferred_entry_legs,
        _is_management_cancelled_deferred_entry_leg,
        _load_exact_binding,
        _parse_exact_deferred_entry_leg_ids,
    )

    with session_factory() as session:
        row = session.get(StrategyManagementBatch, int(batch_id))
        snapshot_json = getattr(row, "target_snapshot_json", None)
    leg_ids = _parse_exact_deferred_entry_leg_ids(
        snapshot_json, error_code="deferred_entry_cancel_identity_drift"
    )
    if not leg_ids:
        return []
    with session_factory() as session:
        rows = [
            session.get(ExecutionOrderLeg, int(leg_id)) for leg_id in leg_ids
        ]
        already_done = all(
            row is not None and _is_management_cancelled_deferred_entry_leg(row)
            for row in rows
        )
    if already_done:
        # An earlier tick of this same batch cancelled them. A *partial*
        # completion deliberately does not qualify: it goes through the real
        # cancel below, which fails closed on it.
        return leg_ids
    record = load_management_batch(session_factory, int(batch_id))
    binding = _load_exact_binding(session_factory, record)
    _cancel_deferred_entry_legs(
        session_factory,
        batch=record,
        binding=binding,
        deepcoin_client=deepcoin_client,
        cancelled_at=now,
    )
    return leg_ids


def _unresolved_close_intent_exists(
    session_factory, *, pos_id: Any, execution_order_leg_id: Any
) -> bool:
    """The exact predicate ``PositionMutationGateway`` would block on."""

    with session_factory() as session:
        return session.query(PositionMutationIntent.id).filter(
            PositionMutationIntent.pos_id == str(pos_id),
            PositionMutationIntent.execution_order_leg_id
            == int(execution_order_leg_id),
            PositionMutationIntent.operation == "close_position",
            PositionMutationIntent.status.in_(_UNRESOLVED_CLOSE_INTENT_STATUSES),
        ).first() is not None


def _remainder_close_intents(session_factory, component_id: int) -> list[Any]:
    with session_factory() as session:
        rows = session.query(PositionMutationIntent).filter(
            PositionMutationIntent.idempotency_key.like(
                f"{int(component_id)}:close:remainder:%"
            )
        ).order_by(PositionMutationIntent.id.asc()).all()
        for row in rows:
            session.expunge(row)
        return rows


def _persist_remainder_close_plan_and_enter_submitting(
    session_factory, *, component_id: int, plan: dict[str, Any], now: Any
) -> None:
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, int(component_id))
        if component is None or component.status != "preflighting":
            raise RuntimeError("management_component_not_preflighting")
        desired = json.loads(component.desired_json)
        stored = dict(desired.get(REMAINDER_CLOSE_EXECUTION_KEY) or {})
        stored.update(plan)
        desired[REMAINDER_CLOSE_EXECUTION_KEY] = stored
        component.desired_json = json.dumps(
            desired, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if not transition_management_component(
            session, component_id=int(component_id),
            expected_status="preflighting", new_status="submitting", now=now,
            evidence={
                "phase": "remainder_close_decided",
                "requested_stop": stored.get("requested_stop"),
                "ticker_last": stored.get("ticker_last"),
            },
        ):
            raise RuntimeError("management_component_submit_claim_lost")
        session.commit()


def _load_remainder_close_plan(session_factory, component_id: int) -> dict[str, Any]:
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, int(component_id))
        desired = json.loads(getattr(component, "desired_json", None) or "{}")
    return dict(desired.get(REMAINDER_CLOSE_EXECUTION_KEY) or {})


def _update_remainder_close_plan(
    session_factory, component_id: int, values: dict[str, Any]
) -> dict[str, Any]:
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, int(component_id))
        if component is None:
            raise RuntimeError("management_component_missing")
        desired = json.loads(component.desired_json or "{}")
        stored = dict(desired.get(REMAINDER_CLOSE_EXECUTION_KEY) or {})
        stored.update(values)
        desired[REMAINDER_CLOSE_EXECUTION_KEY] = stored
        component.desired_json = json.dumps(
            desired, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        session.commit()
        return stored


def _append_remainder_close_intent(
    session_factory,
    component_id: int,
    *,
    intent_id: int,
    pre_submit_size: str,
    client_order_id: str,
    require_submitting: bool = True,
) -> None:
    with session_factory() as session:
        component = session.get(StrategyManagementComponent, int(component_id))
        if component is None or (
            require_submitting and component.status != "submitting"
        ):
            raise RuntimeError("management_component_not_submitting")
        desired = json.loads(component.desired_json or "{}")
        stored = dict(desired.get(REMAINDER_CLOSE_EXECUTION_KEY) or {})
        intent_ids = [int(value) for value in stored.get("intent_ids") or []]
        if int(intent_id) not in intent_ids:
            intent_ids.append(int(intent_id))
        stored.update(
            intent_ids=intent_ids,
            intent_id=int(intent_id),
            pre_submit_size=str(pre_submit_size),
            client_order_id=str(client_order_id),
        )
        desired[REMAINDER_CLOSE_EXECUTION_KEY] = stored
        component.desired_json = json.dumps(
            desired, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        session.commit()


def _position_is_flat(row: Any) -> bool | None:
    """``True`` flat, ``False`` still open, ``None`` unreadable."""

    try:
        return Decimal(str((row or {}).get("pos"))) == 0
    except (InvalidOperation, TypeError, ValueError):
        return None


def _tick_normalized_stop(requested_stop: Any, *, side: Any, price_tick: Any):
    """Evidence only: the price the stop would have taken. Never written out."""

    try:
        requested = Decimal(str(requested_stop))
        tick = Decimal(str(price_tick))
        if not (requested.is_finite() and tick.is_finite() and tick > 0):
            return None
        rounding = (
            ROUND_FLOOR if str(side or "").lower() == "long" else ROUND_CEILING
        )
        value = (requested / tick).to_integral_value(rounding=rounding) * tick
        text = format(value.normalize(), "f")
        return "0" if text == "-0" else text
    except (InvalidOperation, TypeError, ValueError):
        return None


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
        #
        # It used to be read from this leg's ``take_profit`` rows only, so a
        # position that never had a staged take profit -- batch 129's and batch
        # 159's shape, a stop and nothing else -- could not start *any* of the
        # three components: all three refused with
        # ``take_profit_order_identity_conflict`` for want of an instrument
        # name, and component one has nothing to consume there anyway. The
        # narrowest widening that fixes it is to fall back to this leg's other
        # protection rows and then to the binding's, both of which name the
        # same instrument by construction; disagreement still fails closed.
        instruments = _distinct_ledger_instruments(
            session,
            binding_id=batch.execution_binding_id,
            leg_id=leg.execution_order_leg_id,
            pos_id=leg.pos_id,
            purpose="take_profit",
        )
        if not instruments:
            instruments = _distinct_ledger_instruments(
                session,
                binding_id=batch.execution_binding_id,
                leg_id=leg.execution_order_leg_id,
                pos_id=leg.pos_id,
            )
        if not instruments:
            instruments = _distinct_ledger_instruments(
                session, binding_id=batch.execution_binding_id
            )
        if len(instruments) != 1 or "" in instruments:
            return CompositeComponentExecutionResult(
                status="operator_required", component_id=component.id,
                reason_code="take_profit_order_identity_conflict",
            )
        desired["instrument_id"] = next(iter(instruments))
        for row in (batch, component, leg):
            session.expunge(row)
        return batch, component, leg, contract, desired


def _distinct_ledger_instruments(
    session,
    *,
    binding_id,
    leg_id=None,
    pos_id=None,
    purpose: str | None = None,
) -> set[str]:
    query = session.query(PositionProtectionLedger.instrument_id).filter(
        PositionProtectionLedger.execution_binding_id == binding_id
    )
    if leg_id is not None:
        query = query.filter(
            PositionProtectionLedger.execution_order_leg_id == leg_id
        )
    if pos_id is not None:
        query = query.filter(PositionProtectionLedger.pos_id == pos_id)
    if purpose is not None:
        query = query.filter(PositionProtectionLedger.purpose == purpose)
    return {str(row[0] or "").upper() for row in query}


def _exchange_snapshot(client: Any, instrument_id: str) -> dict[str, list]:
    def read(name: str):
        fn = getattr(client, name, None)
        if fn is None:
            raise RuntimeError(f"{name}_snapshot_unavailable")
        value = fn(inst_id=instrument_id)
        if not isinstance(value, list):
            raise RuntimeError(f"{name}_snapshot_incomplete")
        return value
    trigger_history = read("list_trigger_order_history")
    order_history = read("list_order_history")
    return {
        "positions": read("list_positions"),
        "pending": read("list_trigger_orders_pending"),
        # ``history`` stays the merged list the intent reconciler already
        # consumes. The two endpoints are kept apart as well because they speak
        # different vocabularies: ``trigger-orders-history`` has no ``state``
        # and answers with ``triggerTime``/``errorCode``, ``orders-history``
        # has ``state`` and no trigger fields. Merging them and then asking one
        # question of the result is how a single order can look like two.
        "history": [*trigger_history, *order_history],
        "trigger_history": trigger_history,
        "order_history": order_history,
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
        # Which of the instrument's resting protection orders are this
        # position's, by ordId -> ledger or ``TU == posId``. A pending TPSL row
        # carries no position id at all, so nothing else can answer it.
        authority = resolve_protection_authority(
            session,
            venue="deepcoin",
            pos_id=str(leg.pos_id),
            instrument_id=str(desired["instrument_id"]),
            side=str(contract.side),
            pending_rows=snapshot["pending"],
        )
        recorded_statuses: dict[str, tuple[str, ...]] = {}
        for row in session.query(PositionTakeProfitOrder).filter(
            PositionTakeProfitOrder.execution_binding_id == batch.execution_binding_id,
            PositionTakeProfitOrder.execution_order_leg_id == leg.execution_order_leg_id,
            PositionTakeProfitOrder.pos_id == leg.pos_id,
        ):
            order_id = str(row.order_id or "").strip()
            if order_id:
                recorded_statuses.setdefault(order_id, ())
                recorded_statuses[order_id] += (str(row.status or ""),)
        return plan_take_profit_consumption(
            contract=contract,
            target_leg=target,
            pending_orders=snapshot["pending"],
            trigger_history=snapshot["trigger_history"],
            order_history=snapshot["order_history"],
            trade_fills=snapshot["fills"],
            protection_ledger=ledger,
            trusted_start_size=desired["trusted_start_size"],
            target_remaining_size=desired["target_remaining_size"],
            protection_authority=authority,
            # ``_exchange_snapshot`` raises unless every read returned a list,
            # so reaching here means the pending read was complete.
            pending_snapshot_complete=True,
            recorded_order_statuses=recorded_statuses,
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
