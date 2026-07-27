"""Fail-closed planning for exact-strategy Deepcoin management batches."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.execution_bindings import (
    build_strategy_instance_id,
    load_deepcoin_execution_reconciliation_snapshot,
    reconcile_deepcoin_execution_bindings,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionIncident,
    PositionProtectionLedger,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
    TriggerProtectionIntent,
    TriggerProtectionStopRescue,
    ExecutionEvent,
    PositionAttributionAudit,
)
from telegram_kol_research.position_attribution import (
    TERMINAL_ENTRY_LEG_STATES,
    PositionAttributionError,
    canonical_live_position_economics,
    require_equivalent_live_position_economics,
    require_verified_position_ownership,
)
from telegram_kol_research.protection_attribution import (
    PositionProtection,
    match_position_protection,
    snapshot_protection_rows,
)
from telegram_kol_research.protection_ledger import (
    list_verified_ledger_rows_for_positions,
)
from telegram_kol_research.position_authority_lock import position_authority_lock
from telegram_kol_research.strategy_management_batches import (
    ManagementBatchRecord,
    ManagementLegCreate,
    create_management_batch,
    create_management_batch_in_session,
    load_management_batch,
    resolve_proven_restored_protection_failure_for_market_successor_in_session,
    resolve_restored_protection_failure_for_full_exit_in_session,
)
from telegram_kol_research.strategy_management_sizing import (
    ManagementSizingError,
    allocate_close_sizes,
    effective_action,
)


PARTIAL_INTENTS = frozenset({"partial_take_profit", "partial_then_break_even"})
PROTECTION_INTENTS = frozenset(
    {"adjust_stop_loss", "move_stop_to_break_even", "partial_then_break_even"}
)
PROTECTION_EVIDENCE_INTENTS = PROTECTION_INTENTS | frozenset({"partial_take_profit"})
SUPPORTED_INTENTS = frozenset(
    {"partial_take_profit", "full_exit", *PROTECTION_INTENTS}
)
MANAGEABLE_ENTRY_LEG_STATES = frozenset(
    {"active", "open", "filled", "partial_closed"}
)
DEFERRED_ENTRY_LEG_STATES = frozenset(
    {"open", "pending", "submitted"}
)
RETRYABLE_PREFLIGHT_BLOCK_REASONS = frozenset(
    {
        "target_contract_spec_unavailable",
        "target_live_position_mode_unavailable",
        "target_position_snapshot_unavailable",
        "target_protection_evidence_unavailable",
        "target_protection_snapshot_incomplete",
        "protection_missing_cancellable_order_id",
    }
)
TEMPORARY_PROTECTION_VISIBILITY_WINDOW = timedelta(minutes=5)
BREAK_EVEN_BY_MARKET_ACTION = "break_even_by_market"


class ManagementTargetChangedError(RuntimeError):
    """Raised when a frozen batch target no longer matches fresh preflight."""


class ManagementFractionError(ValueError):
    """Raised when a persisted partial-close fraction is unsafe."""


class ManagementPlanningStateChanged(RuntimeError):
    """Raised when local identity changes before the atomic batch insert."""


def _protection_incident_requires_recovery(session, *, entry_legs) -> bool:
    """Freeze management only for an exact leg/position with an open incident."""

    pairs = {
        (int(leg.id), str(leg.pos_id))
        for leg in entry_legs
        if getattr(leg, "id", None) is not None and str(getattr(leg, "pos_id", "") or "")
    }
    if not pairs:
        return False
    rows = (
        session.query(
            PositionProtectionIncident.execution_order_leg_id,
            PositionProtectionIncident.pos_id,
        )
        .filter(PositionProtectionIncident.incident_type.in_((
            "stop_trigger_failed",
            "protection_missing",
            "protection_unknown",
            "backup_exchange_outcome_unknown",
            "protection_position_conflict",
        )))
        .all()
    )
    return any((int(leg_id), str(pos_id)) in pairs for leg_id, pos_id in rows)


@dataclass(frozen=True, slots=True)
class ManagementPlanningResult:
    status: str
    reason_code: str | None = None
    batch: ManagementBatchRecord | None = None
    target_lifecycle_id: int | None = None

    @property
    def batch_id(self) -> int | None:
        return self.batch.id if self.batch is not None else None


@dataclass(frozen=True, slots=True)
class TriggerProtectionStopRescuePlanningResult:
    """Result of the explicitly authorized stop-only rescue planner."""

    status: str
    reason_code: str | None = None
    rescue_id: int | None = None


@dataclass(frozen=True, slots=True)
class _PlanningIdentity:
    raw_message: RawMessage
    candidate: SignalCandidate
    recognition_decision_id: int
    recognition_decision_state: tuple[Any, ...]
    lifecycle: StrategyLifecycle
    binding: ExecutionBinding
    entry_legs: tuple[ExecutionOrderLeg, ...]


@dataclass(frozen=True, slots=True)
class _EntryLegManagementPlan:
    target_legs: tuple[ExecutionOrderLeg, ...]
    deferred_legs: tuple[ExecutionOrderLeg, ...]
    block_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _PartialPolicyState:
    round_before: int
    frozen: bool
    history: tuple[tuple[Any, ...], ...]


def plan_strategy_management_batch(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    deepcoin_client,
    contract_spec_provider,
    planned_at: datetime | None = None,
    candidate_id: int | None = None,
    shadow_only: bool = False,
    execution_mode: str | None = None,
) -> ManagementPlanningResult:
    """Reconcile, reload, validate, and persist one immutable exact target."""

    now = planned_at or datetime.now(UTC)
    resolved_execution_mode = (
        execution_mode if execution_mode in {"disabled", "shadow", "live"}
        else ("shadow" if shadow_only else "live")
    )
    with position_authority_lock():
        try:
            reconciliation_snapshot = (
                load_deepcoin_execution_reconciliation_snapshot(
                    session_factory, client=deepcoin_client
                )
            )
            reconcile_deepcoin_execution_bindings(
                session_factory,
                client=deepcoin_client,
                recovered_at=now,
                snapshot=reconciliation_snapshot,
            )
        except Exception:
            return ManagementPlanningResult(
                status="blocked", reason_code="management_reconciliation_failed"
            )
        return _plan_strategy_management_batch_locked(
            session_factory,
            raw_message_id=raw_message_id,
            candidate_id=candidate_id,
            reconciliation_snapshot=reconciliation_snapshot,
            contract_spec_provider=contract_spec_provider,
            planned_at=now,
            execution_mode=resolved_execution_mode,
        )


def _plan_strategy_management_batch_locked(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    candidate_id: int | None,
    reconciliation_snapshot,
    contract_spec_provider,
    planned_at: datetime,
    execution_mode: str = "live",
) -> ManagementPlanningResult:
    now = planned_at

    identity_or_result = _load_exact_identity(
        session_factory,
        raw_message_id=raw_message_id,
        candidate_id=candidate_id,
    )
    if isinstance(identity_or_result, ManagementPlanningResult):
        return identity_or_result
    identity = identity_or_result
    candidate = identity.candidate
    lifecycle = identity.lifecycle
    binding = identity.binding
    intent = str(candidate.management_action or "").strip().lower()
    if intent not in SUPPORTED_INTENTS:
        return _persist_blocked(
            session_factory,
            identity=identity,
            raw_message_id=raw_message_id,
            intent=intent or "unknown",
            reason_code="management_intent_not_supported",
            planned_at=now,
            execution_mode=execution_mode,
        )
    try:
        requested_fraction = normalize_requested_management_fraction(
            intent, candidate.management_fraction
        )
    except ManagementFractionError:
        return ManagementPlanningResult(
            status="blocked",
            reason_code="management_fraction_invalid",
            target_lifecycle_id=lifecycle.id,
        )
    idempotency_fingerprint = _idempotency_fingerprint(
        raw_message_id=raw_message_id,
        recognition_generation=str(candidate.recognition_generation),
        lifecycle_id=lifecycle.id,
        intent=intent,
    )
    existing = _load_existing_by_idempotency(
        session_factory, idempotency_fingerprint=idempotency_fingerprint
    )
    retry_blocked_batch_id = (
        existing.id if _retryable_preflight_blocked_batch(existing, now=now) else None
    )
    if existing is not None and retry_blocked_batch_id is None:
        return ManagementPlanningResult(
            status=existing.status,
            reason_code=existing.reason_code,
            batch=existing,
            target_lifecycle_id=lifecycle.id,
        )

    with session_factory() as session:
        partial_policy_state = _load_partial_policy_state(
            session, target_lifecycle_id=lifecycle.id
        )
    if partial_policy_state.frozen:
        return ManagementPlanningResult(
            status="blocked",
            reason_code="prior_partial_batch_unresolved",
            target_lifecycle_id=lifecycle.id,
        )

    entry_leg_plan = _entry_leg_management_plan(identity.entry_legs, binding=binding)
    if entry_leg_plan.block_reason is not None:
        return _persist_blocked(
            session_factory,
            identity=identity,
            raw_message_id=raw_message_id,
            intent=intent,
            reason_code=entry_leg_plan.block_reason,
            planned_at=now,
            execution_mode=execution_mode,
        )
    target_entry_legs = entry_leg_plan.target_legs
    with session_factory() as session:
        protection_recovery_bypass = _protection_incident_requires_recovery(
            session, entry_legs=target_entry_legs
        )
    protection_recovery_for_risk_reduction = (
        protection_recovery_bypass and intent == "partial_then_break_even"
    )
    if (
        protection_recovery_bypass
        and intent not in {"full_exit", "move_stop_to_break_even"}
        and not protection_recovery_for_risk_reduction
    ):
        return _persist_blocked(
            session_factory,
            identity=identity,
            raw_message_id=raw_message_id,
            intent=intent,
            reason_code="protection_recovery_required",
            planned_at=now,
            execution_mode=execution_mode,
        )
    target_legs_by_pos_id = _leg_by_pos_id(target_entry_legs)

    instrument_id = f"{str(lifecycle.symbol).upper()}-USDT-SWAP"
    try:
        live_positions = list(reconciliation_snapshot.positions)
        economics = canonical_live_position_economics(
            live_positions,
            target_pos_ids=[str(leg.pos_id) for leg in target_entry_legs],
            instrument_id=instrument_id,
            side=str(lifecycle.side),
        )
        with session_factory() as session:
            current_lifecycle = session.get(StrategyLifecycle, lifecycle.id)
            current_binding = session.get(ExecutionBinding, binding.id)
            if (
                current_lifecycle is None
                or current_binding is None
                or current_lifecycle.execution_binding_id != current_binding.id
                or not _binding_matches_lifecycle(current_binding, current_lifecycle)
            ):
                raise PositionAttributionError(
                    "target_identity_changed_during_planning"
                )
            for detached_leg in target_entry_legs:
                current_leg = session.get(ExecutionOrderLeg, detached_leg.id)
                if (
                    current_leg is None
                    or current_leg.execution_binding_id
                    != detached_leg.execution_binding_id
                    or current_leg.strategy_instance_id
                    != detached_leg.strategy_instance_id
                    or current_leg.pos_id != detached_leg.pos_id
                    or current_leg.status != detached_leg.status
                    or current_leg.attribution_status
                    != detached_leg.attribution_status
                ):
                    raise PositionAttributionError(
                        "target_identity_changed_during_planning"
                    )
                owner = require_verified_position_ownership(
                    session, venue="deepcoin", pos_id=str(current_leg.pos_id)
                )
                if owner.id != current_leg.id:
                    raise PositionAttributionError("position_ownership_not_unique")
                require_equivalent_live_position_economics(
                    owner, live_positions=live_positions, session=session
                )
        contract_spec = (
            contract_spec_provider.get_contract_spec(instrument_id)
            if contract_spec_provider is not None
            else None
        )
        if contract_spec is None:
            raise PositionAttributionError("target_contract_spec_unavailable")
    except PositionAttributionError as exc:
        return _persist_blocked(
            session_factory,
            identity=identity,
            raw_message_id=raw_message_id,
            intent=intent,
            reason_code=_planning_reason_from_attribution(str(exc)),
            planned_at=now,
            execution_mode=execution_mode,
        )
    except Exception:
        return _persist_blocked(
            session_factory,
            identity=identity,
            raw_message_id=raw_message_id,
            intent=intent,
            reason_code="target_position_snapshot_unavailable",
            planned_at=now,
            execution_mode=execution_mode,
        )

    partial_round_before = partial_policy_state.round_before
    if intent in PARTIAL_INTENTS:
        effective_action_name, effective_fraction = effective_action(
            round_before=partial_round_before,
            fraction=requested_fraction,
        )
        if (
            intent == "partial_then_break_even"
            and effective_action_name == "partial_close"
        ):
            effective_action_name = "partial_then_break_even"
    elif intent == "full_exit":
        effective_action_name, effective_fraction = intent, 1.0
    elif intent == "move_stop_to_break_even":
        effective_action_name, effective_fraction = BREAK_EVEN_BY_MARKET_ACTION, None
    else:
        effective_action_name, effective_fraction = intent, None

    if (
        intent in {"move_stop_to_break_even", "partial_then_break_even"}
        and candidate.stop_loss_text not in (None, "")
    ):
        if candidate.stop_price_source != "current_message_text":
            candidate.stop_loss_text = None
        else:
            try:
                explicit_stop = Decimal(str(candidate.stop_loss_text))
                price_tick = Decimal(str(contract_spec.price_tick))
            except (InvalidOperation, TypeError, ValueError):
                explicit_stop = Decimal("0")
                price_tick = Decimal("0")
            exact_tick = (
                explicit_stop > 0
                and price_tick > 0
                and explicit_stop % price_tick == 0
            )
            live_entry_prices = [
                Decimal(str(position["avg_entry_price"]))
                for position in economics
            ]
            current_stop = (
                Decimal(str(lifecycle.stop_loss))
                if lifecycle.stop_loss is not None
                else None
            )
            if str(lifecycle.side).lower() == "long":
                tightens = (
                    exact_tick
                    and all(explicit_stop >= price for price in live_entry_prices)
                    and (
                        current_stop is None
                        or explicit_stop >= current_stop
                    )
                )
            else:
                tightens = (
                    exact_tick
                    and all(explicit_stop <= price for price in live_entry_prices)
                    and (
                        current_stop is None
                        or explicit_stop <= current_stop
                    )
                )
            if not tightens:
                return _persist_blocked(
                    session_factory,
                    identity=identity,
                    raw_message_id=raw_message_id,
                    intent=intent,
                    reason_code="explicit_break_even_stop_not_risk_tightening",
                    planned_at=now,
                    execution_mode=execution_mode,
                )

    protection_by_pos_id: dict[str, dict[str, Any]] = {}
    if (
        intent in PROTECTION_EVIDENCE_INTENTS
        and effective_action_name
        not in {"full_close", "full_exit", BREAK_EVEN_BY_MARKET_ACTION}
    ):
        if not _pending_tpsl_snapshot_complete(
            reconciliation_snapshot, instrument_id=instrument_id
        ):
            return _persist_blocked(
                session_factory,
                identity=identity,
                raw_message_id=raw_message_id,
                intent=intent,
                reason_code="target_protection_snapshot_incomplete",
                planned_at=now,
                execution_mode=execution_mode,
            )
        try:
            tpsl_orders = list(reconciliation_snapshot.pending_trigger_orders)
        except Exception:
            return _persist_blocked(
                session_factory,
                identity=identity,
                raw_message_id=raw_message_id,
                intent=intent,
                reason_code="target_protection_evidence_unavailable",
                planned_at=now,
                execution_mode=execution_mode,
            )
        matches = match_position_protection(
            live_positions, tpsl_orders, evidence_available=True
        )
        ledger_rows_by_pos_id: dict[str, list[PositionProtectionLedger]] = {}
        with session_factory() as session:
            for row in list_verified_ledger_rows_for_positions(
                session, [str(position["pos_id"]) for position in economics]
            ):
                ledger_rows_by_pos_id.setdefault(str(row.pos_id), []).append(row)
        global_protection_order_id_counts = Counter(
            order_id
            for row in tpsl_orders
            if (order_id := _exact_protection_order_id(row)) is not None
        )
        seen_protection_order_ids: set[str] = set()
        for position in economics:
            protection = matches.by_pos_id.get(position["pos_id"])
            position_only_without_order_ids = bool(
                protection is not None
                and protection.status == "verified"
                and protection.rows
                and not protection.order_ids
                and all(
                    row.get("_evidence_source") == "position"
                    for row in protection.rows
                )
            )
            if protection is None or protection.status != "verified":
                protection = _ledger_confirmed_position_protection(
                    position=position,
                    entry_leg=target_legs_by_pos_id[position["pos_id"]],
                    binding=binding,
                    tpsl_orders=tpsl_orders,
                    ledger_rows=ledger_rows_by_pos_id.get(position["pos_id"], []),
                    global_order_id_counts=global_protection_order_id_counts,
                )
            elif position_only_without_order_ids:
                ledger_protection = _ledger_confirmed_position_protection(
                    position=position,
                    entry_leg=target_legs_by_pos_id[position["pos_id"]],
                    binding=binding,
                    tpsl_orders=tpsl_orders,
                    ledger_rows=ledger_rows_by_pos_id.get(position["pos_id"], []),
                    global_order_id_counts=global_protection_order_id_counts,
                )
                if ledger_protection is not None:
                    protection = ledger_protection
            if protection is None or protection.status != "verified":
                reason_code = _unverified_protection_reason(
                    protection=protection,
                    ledger_rows=ledger_rows_by_pos_id.get(position["pos_id"], []),
                    tpsl_orders=tpsl_orders,
                )
                return _persist_blocked(
                    session_factory,
                    identity=identity,
                    raw_message_id=raw_message_id,
                    intent=intent,
                    reason_code=reason_code,
                    planned_at=now,
                    execution_mode=execution_mode,
                )
            protection_row_ids = [
                _exact_protection_order_id(row) for row in protection.rows
            ]
            if (
                not protection.rows
                or any(order_id is None for order_id in protection_row_ids)
                or len(set(protection_row_ids)) != len(protection_row_ids)
                or any(
                    global_protection_order_id_counts[str(order_id)] != 1
                    for order_id in protection_row_ids
                )
                or protection.order_ids
                != [str(order_id) for order_id in protection_row_ids]
                or bool(
                    seen_protection_order_ids.intersection(
                        str(order_id) for order_id in protection_row_ids
                    )
                )
            ):
                reason_code = _unusable_protection_order_reason(
                    protection_rows=protection.rows,
                    protection_row_ids=protection_row_ids,
                    global_protection_order_id_counts=global_protection_order_id_counts,
                    seen_protection_order_ids=seen_protection_order_ids,
                )
                return _persist_blocked(
                    session_factory,
                    identity=identity,
                    raw_message_id=raw_message_id,
                    intent=intent,
                    reason_code=reason_code,
                    planned_at=now,
                    execution_mode=execution_mode,
                )
            seen_protection_order_ids.update(
                str(order_id) for order_id in protection_row_ids
            )
            protection_by_pos_id[position["pos_id"]] = {
                "status": protection.status,
                "stop_loss": protection.stop_loss,
                "take_profits": list(protection.take_profits),
                "order_ids": list(protection.order_ids),
                "rows": [dict(row) for row in protection.rows],
                "row_snapshots": snapshot_protection_rows(protection.rows),
                "evidence": protection.evidence,
            }
            if protection_recovery_for_risk_reduction:
                target_leg = target_legs_by_pos_id[position["pos_id"]]
                exact_ledger_order_ids = {
                    str(row.order_id)
                    for row in ledger_rows_by_pos_id.get(position["pos_id"], [])
                    if (
                        row.order_id
                        and str(row.status or "").lower() == "verified"
                        and row.execution_binding_id == binding.id
                        and row.execution_order_leg_id == target_leg.id
                        and row.strategy_instance_id == binding.strategy_instance_id
                        and str(row.instrument_id or "").upper()
                        == instrument_id.upper()
                        and str(row.side or "").lower()
                        == str(lifecycle.side or "").lower()
                    )
                }
                if exact_ledger_order_ids != set(protection.order_ids):
                    return _persist_blocked(
                        session_factory,
                        identity=identity,
                        raw_message_id=raw_message_id,
                        intent=intent,
                        reason_code="protection_recovery_exact_ledger_required",
                        planned_at=now,
                        execution_mode=execution_mode,
                    )

    planned_close_sizes: tuple[str | None, ...]
    if effective_fraction is None:
        planned_close_sizes = tuple(None for _ in economics)
    else:
        try:
            planned_close_sizes = allocate_close_sizes(
                (position["size"] for position in economics),
                fraction=effective_fraction,
                quantity_step=contract_spec.quantity_step,
                min_quantity=contract_spec.min_quantity,
            )
        except ManagementSizingError:
            return _persist_blocked(
                session_factory,
                identity=identity,
                raw_message_id=raw_message_id,
                intent=intent,
                reason_code="management_close_size_unsafe",
                planned_at=now,
                execution_mode=execution_mode,
            )
    target_snapshot = {
        "execution_mode": execution_mode,
        "identity": {
            "target_lifecycle_id": lifecycle.id,
            "execution_binding_id": binding.id,
            "strategy_instance_id": binding.strategy_instance_id,
            "manageable_entry_leg_ids": [leg.id for leg in target_entry_legs],
            "deferred_entry_leg_ids": [leg.id for leg in entry_leg_plan.deferred_legs],
        },
        "positions": [
            {
                **position,
                "execution_order_leg_id": target_legs_by_pos_id[position["pos_id"]].id,
            }
            for position in economics
        ],
        "deferred_entry_legs": [
            {
                "execution_order_leg_id": leg.id,
                "leg_index": leg.leg_index,
                "order_id": leg.order_id,
                "status": leg.status,
                "attribution_status": leg.attribution_status,
            }
            for leg in entry_leg_plan.deferred_legs
        ],
        "contract_spec": contract_spec.to_dict(),
        "protection": protection_by_pos_id,
    }
    if protection_recovery_for_risk_reduction:
        target_snapshot["protection_recovery"] = {
            "version": 1,
            "mode": "replace_after_reduction",
            "positions": [
                {
                    "pos_id": str(position["pos_id"]),
                    "execution_order_leg_id": int(
                        target_legs_by_pos_id[str(position["pos_id"])].id
                    ),
                    "owned_order_ids": list(
                        protection_by_pos_id[str(position["pos_id"])]["order_ids"]
                    ),
                }
                for position in economics
            ],
        }
    if protection_recovery_bypass and intent == "full_exit":
        target_snapshot["protection_recovery_bypass"] = {
            "version": 1,
            "reason": "protection_recovery_required",
            "allowed_action": "full_exit",
            "target_lifecycle_id": lifecycle.id,
            "execution_binding_id": binding.id,
            "target_pos_ids": sorted(
                str(position["pos_id"]) for position in economics
            ),
        }
    target_fingerprint = management_target_fingerprint(target_snapshot)
    legs_by_pos_id = target_legs_by_pos_id
    batch_legs = [
        ManagementLegCreate(
            execution_order_leg_id=legs_by_pos_id[position["pos_id"]].id,
            pos_id=position["pos_id"],
            leg_index=index,
            preflight_size=position["size"],
            planned_close_size=planned_close_sizes[index],
            avg_entry_price=position["avg_entry_price"],
            quantity_step=str(contract_spec.quantity_step),
            old_tpsl=protection_by_pos_id.get(position["pos_id"]),
            planned_tpsl=(
                {
                    "intent": intent,
                    "stop_loss_text": candidate.stop_loss_text,
                    **(
                        {"stop_price_source": candidate.stop_price_source}
                        if candidate.stop_loss_text not in (None, "")
                        and candidate.stop_price_source not in (None, "")
                        else {}
                    ),
                }
                if intent in PROTECTION_INTENTS
                and effective_action_name
                not in {"full_close", "full_exit", BREAK_EVEN_BY_MARKET_ACTION}
                else None
            ),
            last_exchange_snapshot=position,
        )
        for index, position in enumerate(economics)
    ]
    try:
        with session_factory() as session:
            if intent == "full_exit":
                predecessor_state = (
                    resolve_restored_protection_failure_for_full_exit_in_session(
                        session,
                        strategy_instance_id=str(binding.strategy_instance_id),
                        target_lifecycle_id=lifecycle.id,
                        execution_binding_id=binding.id,
                        resolved_at=now,
                    )
                )
                if predecessor_state == "blocked":
                    session.rollback()
                    return ManagementPlanningResult(
                        status="blocked",
                        reason_code="prior_management_batch_unresolved",
                        target_lifecycle_id=lifecycle.id,
                    )
            elif intent == "move_stop_to_break_even":
                predecessor_state = (
                    resolve_proven_restored_protection_failure_for_market_successor_in_session(
                        session,
                        strategy_instance_id=str(binding.strategy_instance_id),
                        target_lifecycle_id=lifecycle.id,
                        execution_binding_id=binding.id,
                        live_pos_ids={
                            str(position["pos_id"]) for position in economics
                        },
                        pending_tpsl_rows=list(
                            reconciliation_snapshot.pending_trigger_orders
                        ),
                        pending_tpsl_snapshot_complete=(
                            _pending_tpsl_snapshot_complete(
                                reconciliation_snapshot,
                                instrument_id=instrument_id,
                            )
                        ),
                        resolved_at=now,
                    )
                )
                if predecessor_state == "blocked":
                    session.rollback()
                    return ManagementPlanningResult(
                        status="blocked",
                        reason_code="prior_management_batch_unresolved",
                        target_lifecycle_id=lifecycle.id,
                    )
            if retry_blocked_batch_id is None:
                batch_id = create_management_batch_in_session(
                    session,
                    idempotency_fingerprint=idempotency_fingerprint,
                    raw_message_id=raw_message_id,
                    recognition_decision_id=identity.recognition_decision_id,
                    recognition_generation=str(candidate.recognition_generation),
                    target_lifecycle_id=lifecycle.id,
                    strategy_instance_id=str(binding.strategy_instance_id),
                    execution_binding_id=binding.id,
                    intent=intent,
                    effective_action=effective_action_name,
                    requested_fraction=requested_fraction,
                    effective_fraction=effective_fraction,
                    partial_round_before=partial_round_before,
                    target_fingerprint=target_fingerprint,
                    target_snapshot=target_snapshot,
                    planned_at=now,
                    legs=batch_legs,
                    execution_mode=execution_mode,
                    status="blocked" if execution_mode != "live" else "ready",
                    reason_code=(
                        "management_shadow_plan_only"
                        if execution_mode == "shadow"
                        else (
                            "management_disabled_plan_only"
                            if execution_mode == "disabled"
                            else (
                                "protection_recovery_bypassed_for_full_exit"
                                if protection_recovery_bypass
                                else None
                            )
                        )
                    ),
                    validate_current_state=lambda current_session: (
                        _require_frozen_identity_and_policy_current(
                            current_session,
                            identity,
                            partial_policy_state=partial_policy_state,
                        )
                    ),
                )
            else:
                batch_id = _replace_retryable_preflight_blocked_batch_in_session(
                    session,
                    batch_id=retry_blocked_batch_id,
                    identity=identity,
                    idempotency_fingerprint=idempotency_fingerprint,
                    raw_message_id=raw_message_id,
                    recognition_generation=str(candidate.recognition_generation),
                    intent=intent,
                    effective_action=effective_action_name,
                    requested_fraction=requested_fraction,
                    effective_fraction=effective_fraction,
                    partial_round_before=partial_round_before,
                    target_fingerprint=target_fingerprint,
                    target_snapshot=target_snapshot,
                    planned_at=now,
                    legs=batch_legs,
                    execution_mode=execution_mode,
                    status="blocked" if execution_mode != "live" else "ready",
                    reason_code=(
                        "management_shadow_plan_only"
                        if execution_mode == "shadow"
                        else (
                            "management_disabled_plan_only"
                            if execution_mode == "disabled"
                            else (
                                "protection_recovery_bypassed_for_full_exit"
                                if protection_recovery_bypass
                                else None
                            )
                        )
                    ),
                    partial_policy_state=partial_policy_state,
                )
            session.commit()
    except ManagementPlanningStateChanged:
        return ManagementPlanningResult(
            status="blocked",
            reason_code="target_identity_changed_during_planning",
            target_lifecycle_id=lifecycle.id,
        )
    batch = load_management_batch(session_factory, batch_id)
    return ManagementPlanningResult(
        status="ready", batch=batch, target_lifecycle_id=lifecycle.id
    )


def management_target_fingerprint(target_snapshot: Any) -> str:
    encoded = json.dumps(
        target_snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_unchanged_target_fingerprint(
    expected_fingerprint: str, target_snapshot: Any
) -> None:
    if management_target_fingerprint(target_snapshot) != str(expected_fingerprint):
        raise ManagementTargetChangedError("management_target_changed")


def _load_exact_identity(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    candidate_id: int | None = None,
) -> _PlanningIdentity | ManagementPlanningResult:
    with session_factory() as session:
        raw_message = session.get(RawMessage, raw_message_id)
        decision = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_message_id)
            .one_or_none()
        )
        candidate_query = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.raw_message_id == raw_message_id)
            .filter(SignalCandidate.parse_source == "mimo_authoritative")
            .filter(
                SignalCandidate.event_type.in_(["close_signal", "position_update"])
            )
        )
        if candidate_id is not None:
            candidate_query = candidate_query.filter(
                SignalCandidate.id == candidate_id
            )
        candidate = candidate_query.order_by(
            SignalCandidate.confidence.desc(), SignalCandidate.id.asc()
        ).first()
        lifecycle_id = candidate.target_lifecycle_id if candidate is not None else None
        if decision is None or candidate is None or not candidate.recognition_generation:
            return ManagementPlanningResult(
                status="blocked",
                reason_code="authoritative_management_candidate_not_found",
                target_lifecycle_id=lifecycle_id,
            )
        if lifecycle_id is None:
            return ManagementPlanningResult(
                status="blocked",
                reason_code="target_lifecycle_not_found",
            )
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        if lifecycle is None:
            return ManagementPlanningResult(
                status="blocked",
                reason_code="target_lifecycle_not_found",
                target_lifecycle_id=lifecycle_id,
            )
        if (
            raw_message is None
            or candidate.raw_message_id != raw_message.id
            or decision.raw_message_id != raw_message.id
            or raw_message.chat_id != lifecycle.chat_id
            or str(candidate.symbol or "").upper()
            != str(lifecycle.symbol or "").upper()
            or str(candidate.side or "").lower()
            != str(lifecycle.side or "").lower()
        ):
            return ManagementPlanningResult(
                status="blocked",
                reason_code="target_source_identity_mismatch",
                target_lifecycle_id=lifecycle.id,
            )
        if lifecycle.execution_binding_id is None:
            expected_strategy_instance_id = build_strategy_instance_id(
                venue="deepcoin",
                chat_id=lifecycle.chat_id,
                message_id=lifecycle.message_id,
                symbol=lifecycle.symbol,
                side=lifecycle.side,
            )
            bindings = (
                session.query(ExecutionBinding)
                .filter(ExecutionBinding.venue == "deepcoin")
                .filter(
                    ExecutionBinding.strategy_instance_id
                    == expected_strategy_instance_id
                )
                .filter(ExecutionBinding.status.in_(["open", "active"]))
                .order_by(ExecutionBinding.id.asc())
                .all()
            )
            if not bindings:
                return ManagementPlanningResult(
                    status="deferred",
                    reason_code="target_strategy_binding_not_visible_yet",
                    target_lifecycle_id=lifecycle.id,
                )
            if len(bindings) != 1:
                return ManagementPlanningResult(
                    status="blocked",
                    reason_code="target_strategy_binding_not_unique",
                    target_lifecycle_id=lifecycle.id,
                )
            binding = bindings[0]
            if not _binding_matches_lifecycle(binding, lifecycle):
                return ManagementPlanningResult(
                    status="blocked",
                    reason_code="target_strategy_binding_not_found",
                    target_lifecycle_id=lifecycle.id,
                )
            lifecycle.execution_binding_id = binding.id
            session.commit()
            for row in (raw_message, decision, candidate, lifecycle, binding):
                session.refresh(row)
        else:
            binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        if (
            binding is None
            or str(binding.venue or "").lower() != "deepcoin"
            or str(binding.status or "").lower() not in {"open", "active"}
            or not binding.strategy_instance_id
            or not _binding_matches_lifecycle(binding, lifecycle)
        ):
            return ManagementPlanningResult(
                status="blocked",
                reason_code="target_strategy_binding_not_found",
                target_lifecycle_id=lifecycle.id,
            )
        bindings = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.strategy_instance_id == binding.strategy_instance_id)
            .filter(ExecutionBinding.status.in_(["open", "active"]))
            .all()
        )
        if len(bindings) != 1 or bindings[0].id != binding.id:
            session.expunge(candidate)
            session.expunge(lifecycle)
            session.expunge(binding)
            return ManagementPlanningResult(
                status="blocked",
                reason_code="target_strategy_binding_not_unique",
                target_lifecycle_id=lifecycle.id,
            )
        entry_legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == binding.id)
            .filter(ExecutionOrderLeg.purpose == "entry")
            .order_by(ExecutionOrderLeg.leg_index.asc(), ExecutionOrderLeg.id.asc())
            .all()
        )
        session.expunge(raw_message)
        session.expunge(candidate)
        session.expunge(lifecycle)
        session.expunge(binding)
        for leg in entry_legs:
            session.expunge(leg)
        return _PlanningIdentity(
            raw_message=raw_message,
            candidate=candidate,
            recognition_decision_id=decision.id,
            recognition_decision_state=_recognition_decision_state(decision),
            lifecycle=lifecycle,
            binding=binding,
            entry_legs=tuple(entry_legs),
        )


def _binding_position_id_set(binding_pos_id: str | None) -> set[str]:
    if not binding_pos_id:
        return set()
    return {
        item.strip()
        for item in str(binding_pos_id).split(",")
        if item.strip()
    }


def _entry_leg_management_plan(
    entry_legs: tuple[ExecutionOrderLeg, ...], *, binding: ExecutionBinding
) -> _EntryLegManagementPlan:
    if not entry_legs:
        return _EntryLegManagementPlan((), (), "target_position_ownership_not_found")
    position_ids: list[str] = []
    target_legs: list[ExecutionOrderLeg] = []
    deferred_legs: list[ExecutionOrderLeg] = []
    terminal_legs: list[ExecutionOrderLeg] = []
    for leg in entry_legs:
        if leg.execution_binding_id != binding.id or (
            leg.strategy_instance_id != binding.strategy_instance_id
        ):
            return _EntryLegManagementPlan(
                (), (), "target_strategy_identity_mismatch"
            )
        status = str(leg.status or "").lower()
        if status in TERMINAL_ENTRY_LEG_STATES:
            terminal_legs.append(leg)
            continue
        state = str(leg.attribution_status or "unassigned")
        if state == "attribution_conflict":
            return _EntryLegManagementPlan(
                (), (), "target_position_ownership_conflict"
            )
        if state == "evidence_unavailable":
            return _EntryLegManagementPlan(
                (), (), "target_position_evidence_unavailable"
            )
        if (
            state == "verified"
            and leg.pos_id
            and status in MANAGEABLE_ENTRY_LEG_STATES
        ):
            target_legs.append(leg)
            position_ids.append(str(leg.pos_id))
            continue
        if (
            status in DEFERRED_ENTRY_LEG_STATES
            and leg.terminal_reason is None
            and not leg.pos_id
            and state not in {"attribution_conflict", "evidence_unavailable"}
        ):
            deferred_legs.append(leg)
            continue
        return _EntryLegManagementPlan(
            (), tuple(deferred_legs), "target_position_ownership_not_verified"
        )
    if not target_legs:
        if terminal_legs and not deferred_legs:
            return _EntryLegManagementPlan(
                (), (), "target_position_ownership_terminal"
            )
        return _EntryLegManagementPlan(
            (), tuple(deferred_legs), "target_position_ownership_not_verified"
        )
    if len(position_ids) != len(set(position_ids)):
        return _EntryLegManagementPlan(
            (), tuple(deferred_legs), "target_position_ownership_not_unique"
        )
    binding_position_ids = _binding_position_id_set(binding.pos_id)
    if binding_position_ids and not binding_position_ids.issubset(set(position_ids)):
        return _EntryLegManagementPlan(
            (), tuple(deferred_legs), "target_binding_position_mismatch"
        )
    return _EntryLegManagementPlan(tuple(target_legs), tuple(deferred_legs), None)


def _unsafe_entry_leg_reason(
    entry_legs: tuple[ExecutionOrderLeg, ...], *, binding: ExecutionBinding
) -> str | None:
    return _entry_leg_management_plan(entry_legs, binding=binding).block_reason


def _binding_matches_lifecycle(
    binding: ExecutionBinding, lifecycle: StrategyLifecycle
) -> bool:
    expected_strategy_instance_id = build_strategy_instance_id(
        venue="deepcoin",
        chat_id=lifecycle.chat_id,
        message_id=lifecycle.message_id,
        symbol=lifecycle.symbol,
        side=lifecycle.side,
    )
    return bool(
        binding.chat_id == lifecycle.chat_id
        and binding.message_id == lifecycle.message_id
        and str(binding.symbol or "").upper() == str(lifecycle.symbol or "").upper()
        and str(binding.side or "").lower() == str(lifecycle.side or "").lower()
        and binding.strategy_instance_id == expected_strategy_instance_id
    )


def _require_frozen_identity_current(session, identity: _PlanningIdentity) -> None:
    expected_candidate = identity.candidate
    current_raw_message = session.get(RawMessage, identity.raw_message.id)
    current_candidate = session.get(SignalCandidate, expected_candidate.id)
    decision = session.get(RecognitionDecision, identity.recognition_decision_id)
    current_lifecycle = session.get(StrategyLifecycle, identity.lifecycle.id)
    current_binding = session.get(ExecutionBinding, identity.binding.id)
    current_legs = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.execution_binding_id == identity.binding.id)
        .filter(ExecutionOrderLeg.purpose == "entry")
        .order_by(ExecutionOrderLeg.leg_index.asc(), ExecutionOrderLeg.id.asc())
        .all()
    )
    active_strategy_binding_ids = [
        binding_id
        for (binding_id,) in session.query(ExecutionBinding.id)
        .filter(ExecutionBinding.venue == "deepcoin")
        .filter(
            ExecutionBinding.strategy_instance_id
            == identity.binding.strategy_instance_id
        )
        .filter(ExecutionBinding.status.in_(["open", "active"]))
        .all()
    ]
    if (
        current_raw_message is None
        or current_candidate is None
        or decision is None
        or current_lifecycle is None
        or current_binding is None
        or _raw_message_state(current_raw_message)
        != _raw_message_state(identity.raw_message)
        or current_candidate.raw_message_id != current_raw_message.id
        or decision.raw_message_id != current_raw_message.id
        or current_raw_message.chat_id != current_lifecycle.chat_id
        or str(current_candidate.symbol or "").upper()
        != str(current_lifecycle.symbol or "").upper()
        or str(current_candidate.side or "").lower()
        != str(current_lifecycle.side or "").lower()
        or _candidate_state(current_candidate) != _candidate_state(expected_candidate)
        or _recognition_decision_state(decision)
        != identity.recognition_decision_state
        or _lifecycle_state(current_lifecycle) != _lifecycle_state(identity.lifecycle)
        or _binding_state(current_binding) != _binding_state(identity.binding)
        or not _binding_matches_lifecycle(current_binding, current_lifecycle)
        or [_leg_state(leg) for leg in current_legs]
        != [_leg_state(leg) for leg in identity.entry_legs]
        or active_strategy_binding_ids != [identity.binding.id]
    ):
        raise ManagementPlanningStateChanged(
            "target_identity_changed_during_planning"
        )
    entry_leg_plan = _entry_leg_management_plan(
        tuple(current_legs), binding=current_binding
    )
    if entry_leg_plan.block_reason is not None:
        raise ManagementPlanningStateChanged(
            "target_identity_changed_during_planning"
        )
    for leg in entry_leg_plan.target_legs:
        try:
            owner = require_verified_position_ownership(
                session, venue="deepcoin", pos_id=str(leg.pos_id)
            )
        except PositionAttributionError as exc:
            raise ManagementPlanningStateChanged(
                "target_identity_changed_during_planning"
            ) from exc
        if owner.id != leg.id:
            raise ManagementPlanningStateChanged(
                "target_identity_changed_during_planning"
            )


def _require_frozen_identity_and_policy_current(
    session,
    identity: _PlanningIdentity,
    *,
    partial_policy_state: _PartialPolicyState,
) -> None:
    _require_frozen_identity_current(session, identity)
    if _load_partial_policy_state(
        session, target_lifecycle_id=identity.lifecycle.id
    ) != partial_policy_state:
        raise ManagementPlanningStateChanged(
            "target_identity_changed_during_planning"
        )


def _load_partial_policy_state(
    session, *, target_lifecycle_id: int
) -> _PartialPolicyState:
    batches = (
        session.query(StrategyManagementBatch)
        .filter(StrategyManagementBatch.target_lifecycle_id == target_lifecycle_id)
        .filter(StrategyManagementBatch.intent.in_(PARTIAL_INTENTS))
        .order_by(StrategyManagementBatch.id.asc())
        .all()
    )
    history: list[tuple[Any, ...]] = []
    confirmed_partials = 0
    frozen = False
    for batch in batches:
        leg_states = tuple(
            session.query(StrategyManagementLeg.id, StrategyManagementLeg.status)
            .filter(StrategyManagementLeg.management_batch_id == batch.id)
            .order_by(
                StrategyManagementLeg.leg_index.asc(),
                StrategyManagementLeg.id.asc(),
            )
            .all()
        )
        history.append(
            (
                batch.id,
                batch.status,
                batch.effective_action,
                batch.reconciled_at,
                batch.completed_at,
                leg_states,
            )
        )
        fully_confirmed = bool(
            batch.effective_action in {"partial_close", "partial_then_break_even"}
            and batch.status == "succeeded"
            and batch.reconciled_at is not None
            and leg_states
            and (
                all(status == "confirmed" for _, status in leg_states)
                or (
                    batch.effective_action == "partial_then_break_even"
                    and all(status == "succeeded" for _, status in leg_states)
                )
            )
        )
        if fully_confirmed:
            confirmed_partials += 1
        elif batch.status not in {"blocked", "resolved"}:
            frozen = True
    if confirmed_partials > 1:
        frozen = True
    return _PartialPolicyState(
        round_before=min(confirmed_partials, 1),
        frozen=frozen,
        history=tuple(history),
    )


def _candidate_state(candidate: SignalCandidate) -> tuple[Any, ...]:
    return (
        candidate.id,
        candidate.raw_message_id,
        candidate.event_type,
        candidate.target_lifecycle_id,
        candidate.management_action,
        candidate.management_fraction,
        candidate.recognition_generation,
        candidate.parse_source,
        candidate.symbol,
        candidate.side,
    )


def _raw_message_state(raw_message: RawMessage) -> tuple[Any, ...]:
    return (raw_message.id, raw_message.chat_id, raw_message.message_id)


def _recognition_decision_state(decision: RecognitionDecision) -> tuple[Any, ...]:
    return (
        decision.id,
        decision.raw_message_id,
        decision.authoritative_model,
        decision.authoritative_status,
        decision.authoritative_payload_json,
    )


def _lifecycle_state(lifecycle: StrategyLifecycle) -> tuple[Any, ...]:
    return (
        lifecycle.id,
        lifecycle.execution_binding_id,
        lifecycle.lifecycle_status,
        lifecycle.chat_id,
        lifecycle.message_id,
        lifecycle.symbol,
        lifecycle.side,
    )


def _binding_state(binding: ExecutionBinding) -> tuple[Any, ...]:
    return (
        binding.id,
        binding.venue,
        binding.status,
        binding.strategy_instance_id,
        binding.chat_id,
        binding.message_id,
        binding.symbol,
        binding.side,
        binding.pos_id,
        binding.margin_mode,
        binding.position_mode,
    )


def _leg_state(leg: ExecutionOrderLeg) -> tuple[Any, ...]:
    return (
        leg.id,
        leg.execution_binding_id,
        leg.strategy_instance_id,
        leg.leg_index,
        leg.purpose,
        leg.order_kind,
        leg.order_id,
        leg.client_order_id,
        leg.pos_id,
        leg.venue,
        leg.attribution_status,
        leg.attribution_evidence_json,
        leg.terminal_reason,
        leg.last_verified_at,
        leg.status,
    )


def _persist_blocked(
    session_factory: sessionmaker,
    *,
    identity: _PlanningIdentity,
    raw_message_id: int,
    intent: str,
    reason_code: str,
    planned_at: datetime,
    execution_mode: str = "live",
) -> ManagementPlanningResult:
    candidate = identity.candidate
    binding = identity.binding
    lifecycle = identity.lifecycle
    idempotency_fingerprint = _idempotency_fingerprint(
        raw_message_id=raw_message_id,
        recognition_generation=str(candidate.recognition_generation),
        lifecycle_id=lifecycle.id,
        intent=intent,
    )
    existing = _load_existing_by_idempotency(
        session_factory, idempotency_fingerprint=idempotency_fingerprint
    )
    if existing is None:
        partial_round_before = 0
        effective_action_name = intent
        effective_fraction = None
        if intent in PARTIAL_INTENTS:
            with session_factory() as session:
                policy_state = _load_partial_policy_state(
                    session, target_lifecycle_id=lifecycle.id
                )
            partial_round_before = policy_state.round_before
            if not policy_state.frozen:
                effective_action_name, effective_fraction = effective_action(
                    round_before=partial_round_before,
                    fraction=candidate.management_fraction,
                )
        elif intent == "full_exit":
            effective_fraction = 1.0
        target_snapshot = {
            "execution_mode": execution_mode,
            "identity": {
                "target_lifecycle_id": lifecycle.id,
                "execution_binding_id": binding.id,
                "strategy_instance_id": binding.strategy_instance_id,
            },
            "positions": [],
            "blocked_reason": reason_code,
        }
        existing = create_management_batch(
            session_factory,
            idempotency_fingerprint=idempotency_fingerprint,
            raw_message_id=raw_message_id,
            recognition_decision_id=identity.recognition_decision_id,
            recognition_generation=str(candidate.recognition_generation),
            target_lifecycle_id=lifecycle.id,
            strategy_instance_id=str(binding.strategy_instance_id),
            execution_binding_id=binding.id,
            intent=intent,
            effective_action=effective_action_name,
            execution_mode=execution_mode,
            requested_fraction=candidate.management_fraction,
            effective_fraction=effective_fraction,
            partial_round_before=partial_round_before,
            status="blocked",
            reason_code=reason_code,
            target_fingerprint=management_target_fingerprint(target_snapshot),
            target_snapshot=target_snapshot,
            planned_at=planned_at,
            legs=[],
            visibility_first_failed_at=(planned_at if reason_code in _TEMPORARY_PROTECTION_VISIBILITY_REASONS else None),
            visibility_retry_attempts=(1 if reason_code in _TEMPORARY_PROTECTION_VISIBILITY_REASONS else 0),
            visibility_next_attempt_at=(planned_at + timedelta(seconds=5) if reason_code in _TEMPORARY_PROTECTION_VISIBILITY_REASONS else None),
        )
    return ManagementPlanningResult(
        status="blocked",
        reason_code=reason_code,
        batch=existing,
        target_lifecycle_id=lifecycle.id,
    )


def _retryable_preflight_blocked_batch(
    batch: ManagementBatchRecord | None, *, now: datetime | None = None
) -> bool:
    if batch is None:
        return False
    if batch.status != "blocked":
        return False
    if not _is_retryable_preflight_reason(batch):
        return False
    if batch.reason_code in _TEMPORARY_PROTECTION_VISIBILITY_REASONS:
        reference = now or datetime.now(UTC)
        first_failure = batch.visibility_first_failed_at or batch.planned_at
        if first_failure.tzinfo is None:
            first_failure = first_failure.replace(tzinfo=UTC)
        if reference >= first_failure + TEMPORARY_PROTECTION_VISIBILITY_WINDOW:
            return False
    if batch.legs:
        return False
    snapshot = batch.target_snapshot if isinstance(batch.target_snapshot, dict) else {}
    if snapshot.get("blocked_reason") != batch.reason_code:
        return False
    positions = snapshot.get("positions")
    return positions == [] or positions is None


def _is_retryable_preflight_reason(batch: ManagementBatchRecord) -> bool:
    if batch.reason_code in RETRYABLE_PREFLIGHT_BLOCK_REASONS:
        return True
    return bool(
        batch.reason_code == "protection_recovery_required"
        and batch.intent == "full_exit"
        and batch.effective_action == "full_exit"
    )


_TEMPORARY_PROTECTION_VISIBILITY_REASONS = frozenset(
    {
        "protection_missing_cancellable_order_id",
        "target_protection_snapshot_incomplete",
    }
)


def _pending_tpsl_snapshot_complete(snapshot, *, instrument_id: str) -> bool:
    """Fail closed if this instrument's pending-TPSL response was incomplete."""

    observations = getattr(snapshot, "pending_tpsl_observations", ())
    normalized_instrument = str(instrument_id).upper()
    for observation in reversed(list(observations)):
        if str(observation.get("instrument_id") or "").upper() != normalized_instrument:
            continue
        return bool(observation.get("complete"))
    return True


def _replace_retryable_preflight_blocked_batch_in_session(
    session,
    *,
    batch_id: int,
    identity: _PlanningIdentity,
    idempotency_fingerprint: str,
    raw_message_id: int,
    recognition_generation: str,
    intent: str,
    effective_action: str,
    requested_fraction: float | None,
    effective_fraction: float | None,
    partial_round_before: int,
    target_fingerprint: str,
    target_snapshot: dict[str, Any],
    planned_at: datetime,
    legs: list[ManagementLegCreate],
    execution_mode: str,
    status: str,
    reason_code: str | None,
    partial_policy_state: _PartialPolicyState,
) -> int:
    _require_frozen_identity_and_policy_current(
        session, identity, partial_policy_state=partial_policy_state
    )
    batch = session.get(StrategyManagementBatch, int(batch_id))
    if batch is None:
        raise ManagementPlanningStateChanged("retryable_management_batch_missing")
    current_legs = (
        session.query(StrategyManagementLeg)
        .filter(StrategyManagementLeg.management_batch_id == batch.id)
        .all()
    )
    if current_legs:
        raise ManagementPlanningStateChanged("retryable_management_batch_has_legs")
    snapshot = json.loads(batch.target_snapshot_json or "{}")
    if (
        batch.idempotency_fingerprint != idempotency_fingerprint
        or batch.raw_message_id != raw_message_id
        or batch.recognition_generation != recognition_generation
        or batch.target_lifecycle_id != identity.lifecycle.id
        or batch.execution_binding_id != identity.binding.id
        or batch.strategy_instance_id != str(identity.binding.strategy_instance_id)
        or batch.intent != intent
        or batch.status != "blocked"
        or not _is_retryable_preflight_reason(batch)
        or not isinstance(snapshot, dict)
        or snapshot.get("blocked_reason") != batch.reason_code
    ):
        raise ManagementPlanningStateChanged("retryable_management_batch_changed")
    batch.effective_action = effective_action
    batch.execution_mode = execution_mode
    batch.requested_fraction = requested_fraction
    batch.effective_fraction = effective_fraction
    batch.partial_round_before = partial_round_before
    batch.status = status
    batch.reason_code = reason_code
    batch.target_fingerprint = target_fingerprint
    batch.target_snapshot_json = json.dumps(
        target_snapshot, ensure_ascii=False, sort_keys=True
    )
    batch.planned_at = planned_at
    batch.started_at = None
    batch.reconciled_at = None
    batch.completed_at = planned_at if status in {"succeeded", "blocked", "resolved"} else None
    batch.notification_state = "pending"
    batch.notification_fingerprint = None
    batch.updated_at = planned_at
    for leg in legs:
        session.add(
            StrategyManagementLeg(
                management_batch_id=batch.id,
                execution_order_leg_id=leg.execution_order_leg_id,
                pos_id=leg.pos_id,
                leg_index=leg.leg_index,
                status=leg.status,
                preflight_size=leg.preflight_size,
                planned_close_size=leg.planned_close_size,
                avg_entry_price=leg.avg_entry_price,
                quantity_step=leg.quantity_step,
                old_tpsl_json=json.dumps(leg.old_tpsl, ensure_ascii=False)
                if leg.old_tpsl is not None
                else None,
                planned_tpsl_json=json.dumps(leg.planned_tpsl, ensure_ascii=False)
                if leg.planned_tpsl is not None
                else None,
                client_order_id=leg.client_order_id,
                exchange_order_id=leg.exchange_order_id,
                request_json=json.dumps(leg.request, ensure_ascii=False)
                if leg.request is not None
                else None,
                response_json=json.dumps(leg.response, ensure_ascii=False)
                if leg.response is not None
                else None,
                last_error=json.dumps(leg.last_error, ensure_ascii=False)
                if leg.last_error is not None
                else None,
                last_exchange_snapshot_json=json.dumps(
                    leg.last_exchange_snapshot, ensure_ascii=False
                )
                if leg.last_exchange_snapshot is not None
                else None,
                created_at=planned_at,
                updated_at=planned_at,
            )
        )
    session.flush()
    return int(batch.id)


def normalize_requested_management_fraction(
    intent: str, value: object
) -> float | None:
    if intent not in PARTIAL_INTENTS:
        return None
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManagementFractionError("management_fraction_invalid")
    fraction = float(value)
    if not isfinite(fraction) or not 0 < fraction < 1:
        raise ManagementFractionError("management_fraction_invalid")
    return fraction


def plan_trigger_protection_stop_rescue(
    session_factory: sessionmaker,
    *,
    intent_id: int,
    deepcoin_client,
    planned_at: datetime | None = None,
) -> TriggerProtectionStopRescuePlanningResult:
    """Reserve a narrowly-scoped stop-only rescue, or fail closed.

    This is deliberately separate from normal management TPSL adjustment.  It is
    only available for a saved, unresolved trigger-protection intent whose exact
    split position has no ledger-owned stop.  No exchange mutation occurs here.
    """

    now = planned_at or datetime.now(UTC)
    with position_authority_lock():
        with session_factory() as session:
            intent = session.get(TriggerProtectionIntent, int(intent_id))
            if intent is None:
                return TriggerProtectionStopRescuePlanningResult("blocked", "rescue_intent_not_found")
            existing = (
                session.query(TriggerProtectionStopRescue)
                .filter(TriggerProtectionStopRescue.trigger_protection_intent_id == intent.id)
                .one_or_none()
            )
            if existing is not None:
                return TriggerProtectionStopRescuePlanningResult(
                    existing.status, existing.reason_code, int(existing.id)
                )
            prepared = _prepare_trigger_protection_stop_rescue(
                session, intent=intent, deepcoin_client=deepcoin_client
            )
            if isinstance(prepared, str):
                return TriggerProtectionStopRescuePlanningResult(
                    "noop" if prepared == "rescue_managed_stop_already_present" else "blocked",
                    prepared,
                )
            leg, payload = prepared
            rescue = TriggerProtectionStopRescue(
                trigger_protection_intent_id=int(intent.id),
                execution_binding_id=int(intent.execution_binding_id),
                execution_order_leg_id=int(leg.id),
                pos_id=str(leg.pos_id),
                status="ready",
                request_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                planned_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(rescue)
            session.commit()
            return TriggerProtectionStopRescuePlanningResult("ready", rescue_id=int(rescue.id))


def _prepare_trigger_protection_stop_rescue(session, *, intent, deepcoin_client):
    """Validate every authorization condition and produce an SL-only payload."""

    if intent.venue != "deepcoin" or not _rescue_intent_is_deferred_or_ambiguous(session, intent):
        return "rescue_intent_not_deferred"
    leg = session.get(ExecutionOrderLeg, int(intent.execution_order_leg_id))
    binding = session.get(ExecutionBinding, int(intent.execution_binding_id))
    if (
        leg is None
        or binding is None
        or int(leg.execution_binding_id) != int(binding.id)
        or str(leg.purpose) != "entry"
        or str(leg.order_kind) != "trigger_limit"
        or str(leg.status).lower() not in MANAGEABLE_ENTRY_LEG_STATES
        or str(leg.attribution_status) != "verified"
        or not str(leg.pos_id or "").strip()
        or str(binding.status).lower() != "active"
        or str(binding.position_mode).lower() != "split"
    ):
        return "rescue_position_not_verified"
    pos_id = str(leg.pos_id)
    if not _binding_has_exact_pos_id(binding.pos_id, pos_id):
        return "rescue_binding_position_mismatch"
    ledger_rows = list_verified_ledger_rows_for_positions(session, [pos_id])
    if any(
        int(row.execution_binding_id) == int(binding.id)
        and int(row.execution_order_leg_id) == int(leg.id)
        and str(row.purpose) in {"stop_loss", "combined"}
        for row in ledger_rows
    ):
        return "rescue_managed_stop_already_present"
    inst_id = f"{str(binding.symbol).upper()}-USDT-SWAP"
    try:
        live_positions = deepcoin_client.list_positions(inst_id=inst_id)
        pending_tpsl = deepcoin_client.list_trigger_orders_pending(inst_id=inst_id)
    except Exception:
        return "rescue_exchange_preflight_unavailable"
    positions = [
        row for row in live_positions
        if str(row.get("posId") or row.get("pos_id") or "") == pos_id
        and str(row.get("instId") or "").upper() == inst_id
        and str(row.get("posSide") or row.get("pos_side") or "").lower() == str(binding.side).lower()
        and str(row.get("posMode") or row.get("mrgPosition") or "").lower() == "split"
        and _positive_live_size(row.get("pos"))
    ]
    if len(positions) != 1:
        return "rescue_exact_live_position_not_verified"
    if any(
        str(row.get("instId") or "").upper() == inst_id
        and str(row.get("posId") or row.get("pos_id") or "") == pos_id
        and _present(row, "tpTriggerPx", "tpTriggerPrice")
        for row in pending_tpsl
    ):
        return "rescue_opaque_take_profit_present"
    parent_events = (
        session.query(ExecutionEvent)
        .filter(ExecutionEvent.execution_binding_id == binding.id)
        .filter(ExecutionEvent.action == "create_trigger_entry")
        .filter(ExecutionEvent.order_id == str(intent.parent_trigger_order_id or ""))
        .all()
    )
    if len(parent_events) != 1 or str(leg.order_id or "") != str(intent.parent_trigger_order_id or ""):
        return "rescue_parent_trigger_evidence_not_unique"
    request = _json_dict(parent_events[0].request_json)
    stop_loss = _first_present(request, "slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
    if stop_loss is None:
        return "rescue_stop_loss_not_saved"
    return leg, {
        "instType": "SWAP", "instId": inst_id, "posId": pos_id,
        "posSide": str(binding.side).lower(), "mrgPosition": "split",
        "tdMode": str(binding.margin_mode).lower(), "slTriggerPx": stop_loss,
        "slTriggerPxType": _first_present(request, "slTriggerPxType") or "last",
        "slOrdPx": _first_present(request, "slOrdPx", "slOrderPrice") or "-1",
    }


def _json_dict(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_present(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", 0, "0"):
            return str(value)
    return None


def _present(payload: dict[str, Any], *keys: str) -> bool:
    return _first_present(payload, *keys) is not None


def _positive_live_size(value: Any) -> bool:
    try:
        return float(str(value)) > 0
    except (TypeError, ValueError):
        return False


def _binding_has_exact_pos_id(binding_pos_id: object, target_pos_id: str) -> bool:
    """Require an exact persisted binding membership before live rescue checks."""

    return str(target_pos_id) in {
        value.strip()
        for value in str(binding_pos_id or "").split(",")
        if value.strip()
    }


def _rescue_intent_is_deferred_or_ambiguous(session, intent) -> bool:
    if intent.recovery_state in {"pending", "retrying"}:
        return True
    if intent.recovery_state != "failed":
        return False
    audits = (
        session.query(PositionAttributionAudit.evidence_json)
        .filter(PositionAttributionAudit.execution_order_leg_id == intent.execution_order_leg_id)
        .filter(PositionAttributionAudit.event_type == "protection_adoption_refused")
        .all()
    )
    return any(
        any(token in str(evidence or "").lower() for token in ("ambiguous", "not_unique", "deferred"))
        for (evidence,) in audits
    )


def _unverified_protection_reason(
    *,
    protection: PositionProtection | None,
    ledger_rows: list[PositionProtectionLedger],
    tpsl_orders: list[dict[str, Any]],
) -> str:
    if protection is not None and protection.status == "present_but_ambiguous":
        return "protection_ambiguous_global_assignment"
    current_order_ids = {
        order_id
        for row in tpsl_orders
        if (order_id := _exact_protection_order_id(row)) is not None
    }
    if ledger_rows and not any(str(row.order_id) in current_order_ids for row in ledger_rows):
        return "protection_missing_cancellable_order_id"
    if ledger_rows:
        return "protection_price_or_size_mismatch"
    if current_order_ids:
        return "protection_ambiguous_global_assignment"
    return "target_protection_not_verified"


def _unusable_protection_order_reason(
    *,
    protection_rows: list[dict[str, Any]],
    protection_row_ids: list[str | None],
    global_protection_order_id_counts: Counter,
    seen_protection_order_ids: set[str],
) -> str:
    if not protection_rows or any(order_id is None for order_id in protection_row_ids):
        return "protection_missing_cancellable_order_id"
    if (
        len(set(protection_row_ids)) != len(protection_row_ids)
        or any(
            global_protection_order_id_counts[str(order_id)] != 1
            for order_id in protection_row_ids
        )
        or bool(
            seen_protection_order_ids.intersection(
                str(order_id) for order_id in protection_row_ids
            )
        )
    ):
        return "protection_ambiguous_global_assignment"
    return "protection_missing_cancellable_order_id"


def _ledger_confirmed_position_protection(
    *,
    position: dict[str, Any],
    entry_leg: ExecutionOrderLeg,
    binding: ExecutionBinding,
    tpsl_orders: list[dict[str, Any]],
    ledger_rows: list[PositionProtectionLedger],
    global_order_id_counts: Counter,
) -> PositionProtection | None:
    if not ledger_rows:
        return None
    rows_by_order_id = {
        order_id: row
        for row in tpsl_orders
        if (order_id := _exact_protection_order_id(row)) is not None
    }
    confirmed_rows: list[dict[str, Any]] = []
    order_ids: list[str] = []
    position_id = str(position.get("pos_id") or position.get("posId") or "")
    for ledger in ledger_rows:
        order_id = str(ledger.order_id or "")
        if (
            not order_id
            or global_order_id_counts[order_id] != 1
            or int(ledger.execution_binding_id) != int(binding.id)
            or int(ledger.execution_order_leg_id) != int(entry_leg.id)
            or str(ledger.strategy_instance_id or "")
            != str(binding.strategy_instance_id or "")
            or str(ledger.pos_id or "") != position_id
        ):
            continue
        row = rows_by_order_id.get(order_id)
        if row is None or not _ledger_row_matches_current_protection(
            ledger, row, position=position
        ):
            continue
        confirmed_rows.append(dict(row))
        order_ids.append(order_id)
    if not confirmed_rows:
        return None
    stop_losses = [
        value
        for row in confirmed_rows
        if (value := _protection_price(row, "sl")) is not None
    ]
    take_profits = [
        value
        for row in confirmed_rows
        if (value := _protection_price(row, "tp")) is not None
    ]
    return PositionProtection(
        status="verified",
        stop_loss=stop_losses[-1] if stop_losses else None,
        take_profits=_unique_float_values(take_profits),
        order_ids=order_ids,
        rows=confirmed_rows,
        evidence={
            "match": "ledger_confirmed_current_order",
            "order_ids": list(order_ids),
        },
    )


def _ledger_row_matches_current_protection(
    ledger: PositionProtectionLedger,
    row: dict[str, Any],
    *,
    position: dict[str, Any],
) -> bool:
    if str(row.get("triggerOrderType") or "TPSL").upper() != "TPSL":
        return False
    if _instrument_id(row) != str(ledger.instrument_id or "").upper():
        return False
    if _instrument_id(position) != str(ledger.instrument_id or "").upper():
        return False
    if _position_side(row) != str(ledger.side or "").lower():
        return False
    if _position_side(position) != str(ledger.side or "").lower():
        return False
    ledger_price = _to_float(ledger.trigger_price)
    if ledger_price is not None:
        current_price = _protection_price(row, str(ledger.purpose or ""))
        if current_price is None or current_price != ledger_price:
            return False
    ledger_size = _to_float(ledger.size_text)
    row_size = _to_float(row.get("sz") or row.get("size"))
    if ledger_size is not None and row_size is not None and row_size != ledger_size:
        return False
    return True


def _instrument_id(row: dict[str, Any]) -> str:
    return str(row.get("instrument_id") or row.get("instId") or "").upper()


def _position_side(row: dict[str, Any]) -> str:
    value = str(row.get("posSide") or row.get("side") or "").lower()
    return {"buy": "long", "sell": "short"}.get(value, value)


def _protection_price(row: dict[str, Any], purpose: str) -> float | None:
    keys = (
        ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
        if purpose in {"stop_loss", "sl", "loss"}
        else ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")
    )
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None and value != 0:
            return value
    return None


def _to_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unique_float_values(values: list[float]) -> list[float]:
    result: list[float] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _exact_protection_order_id(row: dict[str, Any]) -> str | None:
    for key in ("ordId", "orderId", "order_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return None


def _pending_order_ids_by_pos(rows: Any) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for row in rows if isinstance(rows, (list, tuple)) else []:
        if not isinstance(row, dict):
            continue
        pos_id = str(
            row.get("posId")
            or row.get("pos_id")
            or row.get("positionId")
            or ""
        ).strip()
        order_id = _exact_protection_order_id(row)
        if pos_id and order_id:
            result.setdefault(pos_id, set()).add(order_id)
    return result


def _leg_by_pos_id(
    entry_legs: tuple[ExecutionOrderLeg, ...],
) -> dict[str, ExecutionOrderLeg]:
    return {str(leg.pos_id): leg for leg in entry_legs}


def _idempotency_fingerprint(
    *, raw_message_id: int, recognition_generation: str, lifecycle_id: int, intent: str
) -> str:
    return management_target_fingerprint(
        {
            "raw_message_id": raw_message_id,
            "recognition_generation": recognition_generation,
            "target_lifecycle_id": lifecycle_id,
            "intent": intent,
        }
    )


def _load_existing_by_idempotency(
    session_factory: sessionmaker, *, idempotency_fingerprint: str
) -> ManagementBatchRecord | None:
    with session_factory() as session:
        row = (
            session.query(StrategyManagementBatch.id)
            .filter(
                StrategyManagementBatch.idempotency_fingerprint
                == idempotency_fingerprint
            )
            .one_or_none()
        )
    return load_management_batch(session_factory, row[0]) if row is not None else None


def _planning_reason_from_attribution(reason: str) -> str:
    if reason.startswith("position_ownership_not_verified:attribution_conflict"):
        return "target_position_ownership_conflict"
    if reason.startswith("position_ownership_not_verified:evidence_unavailable"):
        return "target_position_evidence_unavailable"
    if reason.startswith("position_ownership_not_verified"):
        return "target_position_ownership_not_verified"
    if reason == "position_ownership_terminal":
        return "target_position_ownership_terminal"
    return reason
