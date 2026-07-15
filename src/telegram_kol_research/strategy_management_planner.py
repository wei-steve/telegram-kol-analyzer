"""Fail-closed planning for exact-strategy Deepcoin management batches."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
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
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
)
from telegram_kol_research.position_attribution import (
    TERMINAL_ENTRY_LEG_STATES,
    PositionAttributionError,
    canonical_live_position_economics,
    require_equivalent_live_position_economics,
    require_verified_position_ownership,
)
from telegram_kol_research.protection_attribution import (
    match_position_protection,
    snapshot_protection_rows,
)
from telegram_kol_research.position_authority_lock import position_authority_lock
from telegram_kol_research.strategy_management_batches import (
    ManagementBatchRecord,
    ManagementLegCreate,
    create_management_batch,
    create_management_batch_in_session,
    load_management_batch,
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
SUPPORTED_INTENTS = frozenset(
    {"partial_take_profit", "full_exit", *PROTECTION_INTENTS}
)


class ManagementTargetChangedError(RuntimeError):
    """Raised when a frozen batch target no longer matches fresh preflight."""


class ManagementFractionError(ValueError):
    """Raised when a persisted partial-close fraction is unsafe."""


class ManagementPlanningStateChanged(RuntimeError):
    """Raised when local identity changes before the atomic batch insert."""


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
class _PlanningIdentity:
    raw_message: RawMessage
    candidate: SignalCandidate
    recognition_decision_id: int
    recognition_decision_state: tuple[Any, ...]
    lifecycle: StrategyLifecycle
    binding: ExecutionBinding
    entry_legs: tuple[ExecutionOrderLeg, ...]


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
    if existing is not None:
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

    unsafe_reason = _unsafe_entry_leg_reason(identity.entry_legs, binding=binding)
    if unsafe_reason is not None:
        return _persist_blocked(
            session_factory,
            identity=identity,
            raw_message_id=raw_message_id,
            intent=intent,
            reason_code=unsafe_reason,
            planned_at=now,
            execution_mode=execution_mode,
        )

    instrument_id = f"{str(lifecycle.symbol).upper()}-USDT-SWAP"
    try:
        live_positions = list(reconciliation_snapshot.positions)
        economics = canonical_live_position_economics(
            live_positions,
            target_pos_ids=[str(leg.pos_id) for leg in identity.entry_legs],
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
            for detached_leg in identity.entry_legs:
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
    else:
        effective_action_name, effective_fraction = intent, None

    protection_by_pos_id: dict[str, dict[str, Any]] = {}
    if (
        intent in PROTECTION_INTENTS
        and effective_action_name not in {"full_close", "full_exit"}
    ):
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
        global_protection_order_id_counts = Counter(
            order_id
            for row in tpsl_orders
            if (order_id := _exact_protection_order_id(row)) is not None
        )
        seen_protection_order_ids: set[str] = set()
        for position in economics:
            protection = matches.by_pos_id.get(position["pos_id"])
            if protection is None or protection.status != "verified":
                return _persist_blocked(
                    session_factory,
                    identity=identity,
                    raw_message_id=raw_message_id,
                    intent=intent,
                    reason_code="target_protection_not_verified",
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
                return _persist_blocked(
                    session_factory,
                    identity=identity,
                    raw_message_id=raw_message_id,
                    intent=intent,
                    reason_code="target_protection_order_identity_unavailable",
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
        },
        "positions": [
            {
                **position,
                "execution_order_leg_id": _leg_by_pos_id(identity.entry_legs)[
                    position["pos_id"]
                ].id,
            }
            for position in economics
        ],
        "contract_spec": contract_spec.to_dict(),
        "protection": protection_by_pos_id,
    }
    target_fingerprint = management_target_fingerprint(target_snapshot)
    legs_by_pos_id = _leg_by_pos_id(identity.entry_legs)
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
                {"intent": intent, "stop_loss_text": candidate.stop_loss_text}
                if intent in PROTECTION_INTENTS
                and effective_action_name not in {"full_close", "full_exit"}
                else None
            ),
            last_exchange_snapshot=position,
        )
        for index, position in enumerate(economics)
    ]
    try:
        with session_factory() as session:
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
                    else ("management_disabled_plan_only" if execution_mode == "disabled" else None)
                ),
                validate_current_state=lambda current_session: (
                    _require_frozen_identity_and_policy_current(
                        current_session,
                        identity,
                        partial_policy_state=partial_policy_state,
                    )
                ),
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
            return ManagementPlanningResult(
                status="blocked",
                reason_code="target_strategy_binding_not_found",
                target_lifecycle_id=lifecycle.id,
            )
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


def _unsafe_entry_leg_reason(
    entry_legs: tuple[ExecutionOrderLeg, ...], *, binding: ExecutionBinding
) -> str | None:
    if not entry_legs:
        return "target_position_ownership_not_found"
    position_ids: list[str] = []
    for leg in entry_legs:
        if leg.execution_binding_id != binding.id or (
            leg.strategy_instance_id != binding.strategy_instance_id
        ):
            return "target_strategy_identity_mismatch"
        if str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES:
            return "target_position_ownership_terminal"
        state = str(leg.attribution_status or "unassigned")
        if state == "attribution_conflict":
            return "target_position_ownership_conflict"
        if state == "evidence_unavailable":
            return "target_position_evidence_unavailable"
        if state != "verified" or not leg.pos_id:
            return "target_position_ownership_not_verified"
        position_ids.append(str(leg.pos_id))
    if len(position_ids) != len(set(position_ids)):
        return "target_position_ownership_not_unique"
    if binding.pos_id and str(binding.pos_id) not in set(position_ids):
        return "target_binding_position_mismatch"
    return None


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
    if _unsafe_entry_leg_reason(tuple(current_legs), binding=current_binding) is not None:
        raise ManagementPlanningStateChanged(
            "target_identity_changed_during_planning"
        )
    for leg in current_legs:
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
        )
    return ManagementPlanningResult(
        status="blocked",
        reason_code=reason_code,
        batch=existing,
        target_lifecycle_id=lifecycle.id,
    )


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


def _exact_protection_order_id(row: dict[str, Any]) -> str | None:
    for key in ("ordId", "orderId", "order_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return None


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
