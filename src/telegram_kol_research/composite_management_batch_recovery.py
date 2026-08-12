"""Closed, read-only planning for the approved composite batch incident."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    MessageInstructionItem,
    PositionMutationIntent,
    PositionProtectionLedger,
    RawMessage,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyManagementLeg,
)
from telegram_kol_research.strategy_management_contracts import (
    management_contract_fingerprint,
    load_management_contract,
)
from telegram_kol_research.strategy_management_planner import (
    management_target_fingerprint,
)


class CompositeBatchRecoveryRefusal(ValueError):
    """The supplied incident evidence cannot safely authorize recovery."""

    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


@dataclass(frozen=True, slots=True)
class CompositeBatchRecoveryProfile:
    batch_id: int
    raw_message_id: int
    lifecycle_id: int
    trusted_start_size: str
    target_remaining_size: str
    instrument_id: str
    side: str


BATCH_119_RECOVERY = CompositeBatchRecoveryProfile(
    batch_id=119,
    raw_message_id=10532,
    lifecycle_id=794,
    trusted_start_size="38",
    target_remaining_size="19",
    instrument_id="BTC-USDT-SWAP",
    side="long",
)


@dataclass(frozen=True, slots=True)
class CompositeRecoveryPosition:
    disposition: Literal[
        "resume_to_target",
        "protection_only_at_target",
        "protection_only_below_target",
        "position_absent",
    ]
    current_size: str | None
    close_delta: str
    effective_remaining_size: str


@dataclass(frozen=True, slots=True)
class CompositeBatchRecoveryPlan:
    batch_id: int
    status: Literal["ready", "refused"]
    reason_code: str
    position: CompositeRecoveryPosition | None
    source_fingerprint: str
    exchange_snapshot_fingerprint: str
    evidence_fingerprint: str
    evidence: Mapping[str, Any]
    production_writes: int = 0
    exchange_calls: int = 0


_EXPECTED_COMPONENTS = (
    "consume_take_profit_stage",
    "converge_partial_close",
    "replace_remaining_protection",
)
_REQUIRED_SNAPSHOT_FIELDS = (
    "positions",
    "position_history",
    "open_orders",
    "pending_trigger_orders",
    "order_history",
    "trade_fills",
    "trigger_history",
    "pending_tpsl_observations",
    "errors",
)
_SAFE_TERMINAL_MANAGEMENT_STATUSES = frozenset(
    {"succeeded", "blocked", "resolved"}
)
_SAFE_TERMINAL_INSTRUCTION_STATUSES = frozenset({"succeeded", "failed"})
_TERMINAL_MUTATION_STATUSES = frozenset({"confirmed", "rejected", "blocked"})
_SAFE_TERMINAL_COMPONENT_STATUSES = frozenset(
    {"confirmed", "operator_required", "safely_skipped"}
)


def build_composite_batch_recovery_plan(
    session_factory,
    *,
    profile: CompositeBatchRecoveryProfile,
    snapshot: Any,
    planned_at: Any = None,
) -> CompositeBatchRecoveryPlan:
    """Fail closed at every untrusted durable/snapshot decoding boundary."""

    try:
        return _build_composite_batch_recovery_plan(
            session_factory,
            profile=profile,
            snapshot=snapshot,
            planned_at=planned_at,
        )
    except CompositeBatchRecoveryRefusal as exc:
        return _refusal(_refusal_batch_id(profile), exc.reason_code)
    except (TypeError, ValueError, RecursionError, OverflowError):
        return _refusal(_refusal_batch_id(profile), "planner_evidence_invalid")


def _build_composite_batch_recovery_plan(
    session_factory,
    *,
    profile: CompositeBatchRecoveryProfile,
    snapshot: Any,
    planned_at: Any = None,
) -> CompositeBatchRecoveryPlan:
    """Prove the single approved incident without writes or exchange access."""

    _ = planned_at
    if profile != BATCH_119_RECOVERY:
        return _refusal(
            _refusal_batch_id(profile), "incident_profile_not_allowlisted"
        )
    if not _snapshot_is_complete(snapshot, profile=profile):
        return _refusal(profile.batch_id, "exchange_snapshot_incomplete")

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, profile.batch_id)
        if batch is None:
            return _refusal(profile.batch_id, "management_batch_missing")
        if (
            int(batch.raw_message_id) != profile.raw_message_id
            or int(batch.target_lifecycle_id) != profile.lifecycle_id
        ):
            return _refusal(profile.batch_id, "incident_identity_mismatch")
        lifecycle = session.get(StrategyLifecycle, profile.lifecycle_id)
        raw = session.get(RawMessage, profile.raw_message_id)
        binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
        legs = (
            session.query(StrategyManagementLeg)
            .filter_by(management_batch_id=batch.id)
            .order_by(StrategyManagementLeg.leg_index, StrategyManagementLeg.id)
            .all()
        )
        components = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=batch.id)
            .order_by(
                StrategyManagementComponent.sequence,
                StrategyManagementComponent.id,
            )
            .all()
        )
        if raw is None or lifecycle is None or binding is None or len(legs) != 1:
            return _refusal(profile.batch_id, "durable_identity_mismatch")
        leg = legs[0]
        entry = session.get(ExecutionOrderLeg, int(leg.execution_order_leg_id))
        if entry is None:
            return _refusal(profile.batch_id, "durable_identity_mismatch")

        identity_reason = _durable_identity_refusal(
            batch=batch,
            raw=raw,
            lifecycle=lifecycle,
            binding=binding,
            entry=entry,
            leg=leg,
            profile=profile,
        )
        if identity_reason is not None:
            return _refusal(profile.batch_id, identity_reason)

        contract_result = _validated_contract(batch, profile=profile)
        if isinstance(contract_result, str):
            return _refusal(profile.batch_id, contract_result)
        contract = contract_result

        target_result = _validated_target_snapshot(
            batch, binding=binding, leg=leg, entry=entry, profile=profile
        )
        if isinstance(target_result, str):
            return _refusal(profile.batch_id, target_result)
        target = target_result

        topology_reason = _component_topology_refusal(
            components,
            batch=batch,
            leg=leg,
            entry=entry,
            target=target,
            expected_contract_fingerprint=str(
                batch.management_contract_fingerprint
            ),
        )
        if topology_reason is not None:
            return _refusal(profile.batch_id, topology_reason)
        if not _exact_false_submission_state(batch, leg=leg, components=components):
            return _refusal(profile.batch_id, "false_submission_state_mismatch")
        legacy_state_reason = _legacy_false_state_evidence_refusal(
            leg, profile=profile
        )
        if legacy_state_reason is not None:
            return _refusal(profile.batch_id, legacy_state_reason)
        if any(
            value not in (None, "")
            for value in (
                leg.request_json,
                leg.response_json,
                leg.client_order_id,
                leg.exchange_order_id,
            )
        ):
            return _refusal(
                profile.batch_id, "durable_close_submission_evidence_present"
            )
        if _has_durable_close_submission(
            session, batch=batch, leg=leg, entry=entry
        ):
            return _refusal(
                profile.batch_id, "durable_close_submission_evidence_present"
            )
        if _has_additional_active_database_work(session, batch_id=batch.id):
            return _refusal(profile.batch_id, "additional_active_work_present")

        positions = list(snapshot.positions)
        try:
            position = classify_recovery_position(
                profile=profile,
                positions=positions,
                expected_pos_id=str(leg.pos_id),
                instrument_id=profile.instrument_id,
                side=profile.side,
                quantity_step=str(target["quantity_step"]),
                min_quantity=str(target["min_quantity"]),
            )
        except CompositeBatchRecoveryRefusal as exc:
            return _refusal(profile.batch_id, exc.reason_code)
        if _has_exchange_close_submission(snapshot, pos_id=str(leg.pos_id)):
            return _refusal(
                profile.batch_id, "exchange_close_submission_evidence_present"
            )

        ledger = (
            session.query(PositionProtectionLedger)
            .filter(
                PositionProtectionLedger.execution_binding_id == binding.id,
                PositionProtectionLedger.execution_order_leg_id == entry.id,
                PositionProtectionLedger.pos_id == str(leg.pos_id),
            )
            .order_by(PositionProtectionLedger.id)
            .all()
        )
        protection_reason = _protection_ownership_refusal(
            snapshot.pending_trigger_orders,
            batch=batch,
            binding=binding,
            entry=entry,
            ledger=ledger,
            pos_id=str(leg.pos_id),
            position=position,
            profile=profile,
        )
        if protection_reason is not None:
            return _refusal(profile.batch_id, protection_reason)

        try:
            source_payload = _source_evidence_payload(
                batch=batch,
                raw=raw,
                lifecycle=lifecycle,
                binding=binding,
                entry=entry,
                leg=leg,
                components=components,
                target=target,
                contract=contract,
                protection_ledger=ledger,
            )
        except CompositeBatchRecoveryRefusal:
            return _refusal(profile.batch_id, "durable_evidence_invalid")
        source_fingerprint = _fingerprint(source_payload)
        exchange_payload = _exchange_evidence_payload(
            snapshot,
            position=position,
            pos_id=str(leg.pos_id),
            ledger=ledger,
            profile=profile,
        )
        exchange_fingerprint = _fingerprint(exchange_payload)
        evidence = {
            "schema_version": 1,
            "batch_id": profile.batch_id,
            "decision": "repair_false_legacy_submission",
            "reason_code": "false_legacy_submission_proven",
            "source_fingerprint": source_fingerprint,
            "exchange_snapshot_fingerprint": exchange_fingerprint,
            "immutable_target": {
                "instrument_id": profile.instrument_id,
                "side": profile.side,
                "trusted_start_size": profile.trusted_start_size,
                "target_remaining_size": profile.target_remaining_size,
                "quantity_step": str(target["quantity_step"]),
                "min_quantity": str(target["min_quantity"]),
            },
            "position": _serialize_position(position),
            "durable": {
                "batch_status": str(batch.status),
                "leg_status": str(leg.status),
                "component_statuses": [str(row.status) for row in components],
                "component_count": len(components),
                "close_submission_evidence_count": 0,
            },
            "exchange": {
                "snapshot_complete": True,
                "exact_position_count": 0 if position.current_size is None else 1,
                "regular_close_evidence_count": 0,
                "owned_protection_count": len(ledger),
            },
            "proposed_transition": _proposed_transition(position),
        }
        evidence_fingerprint = _fingerprint(evidence)
        return CompositeBatchRecoveryPlan(
            batch_id=profile.batch_id,
            status="ready",
            reason_code="false_legacy_submission_proven",
            position=position,
            source_fingerprint=source_fingerprint,
            exchange_snapshot_fingerprint=exchange_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
            evidence=_freeze_mapping(evidence),
        )


def serialize_composite_batch_recovery_plan(
    plan: CompositeBatchRecoveryPlan,
) -> dict[str, Any]:
    """Return the only supported, strictly allowlisted CLI serialization."""

    return {
        "batch_id": int(plan.batch_id),
        "status": str(plan.status),
        "reason_code": str(plan.reason_code),
        "position": (
            None if plan.position is None else _serialize_position(plan.position)
        ),
        "source_fingerprint": str(plan.source_fingerprint),
        "exchange_snapshot_fingerprint": str(
            plan.exchange_snapshot_fingerprint
        ),
        "evidence_fingerprint": str(plan.evidence_fingerprint),
        "evidence": _plain_json_value(plan.evidence),
        "production_writes": 0,
        "exchange_calls": 0,
    }


def _snapshot_is_complete(
    snapshot: Any, *, profile: CompositeBatchRecoveryProfile
) -> bool:
    if any(not hasattr(snapshot, field) for field in _REQUIRED_SNAPSHOT_FIELDS):
        return False
    if any(
        not isinstance(getattr(snapshot, field), (list, tuple))
        for field in _REQUIRED_SNAPSHOT_FIELDS
        if field != "errors"
    ):
        return False
    errors = getattr(snapshot, "errors", None)
    if not isinstance(errors, Mapping) or errors:
        return False
    try:
        for field in _REQUIRED_SNAPSHOT_FIELDS:
            if field == "errors":
                continue
            for row in getattr(snapshot, field):
                if not isinstance(row, Mapping):
                    return False
                _fingerprint(dict(row))
    except (TypeError, ValueError, RecursionError, OverflowError):
        return False
    observations = list(snapshot.pending_tpsl_observations)
    if not observations or any(
        not isinstance(row, Mapping) or row.get("complete") is not True
        for row in observations
    ):
        return False
    matching = [
        row
        for row in observations
        if isinstance(row, Mapping)
        and str(
            row.get("instrument_id")
            or row.get("instId")
            or row.get("instrumentId")
            or ""
        ).upper()
        == profile.instrument_id.upper()
    ]
    return len(matching) == 1


def _durable_identity_refusal(
    *, batch, raw, lifecycle, binding, entry, leg, profile
) -> str | None:
    expected_symbol = profile.instrument_id.split("-", 1)[0].upper()
    if (
        str(batch.intent) != "partial_then_break_even"
        or str(batch.effective_action) != "partial_then_break_even"
        or str(batch.execution_mode) != "live"
        or int(batch.execution_binding_id) != int(binding.id)
        or str(batch.strategy_instance_id) != str(binding.strategy_instance_id)
    ):
        return "management_batch_identity_mismatch"
    if (
        int(raw.id) != profile.raw_message_id
        or int(raw.chat_id) != int(lifecycle.chat_id)
    ):
        return "raw_message_identity_mismatch"
    if (
        int(lifecycle.id) != profile.lifecycle_id
        or int(lifecycle.execution_binding_id or 0) != int(binding.id)
        or str(lifecycle.symbol).upper() != expected_symbol
        or str(lifecycle.side).lower() != profile.side.lower()
        or str(lifecycle.lifecycle_status) != "entered"
    ):
        return "lifecycle_identity_mismatch"
    if (
        str(binding.venue).lower() != "deepcoin"
        or int(binding.chat_id) != int(lifecycle.chat_id)
        or int(binding.message_id) != int(lifecycle.message_id)
        or str(binding.symbol).upper() != expected_symbol
        or str(binding.side).lower() != profile.side.lower()
        or str(binding.status).lower() not in {"active", "open"}
        or str(binding.pos_id or "") != str(leg.pos_id)
    ):
        return "execution_binding_identity_mismatch"
    if (
        int(entry.execution_binding_id) != int(binding.id)
        or str(entry.strategy_instance_id or "") != str(batch.strategy_instance_id)
        or str(entry.pos_id or "") != str(leg.pos_id)
        or str(entry.venue).lower() != "deepcoin"
        or str(entry.purpose) != "entry"
        or str(entry.attribution_status) != "verified"
        or str(entry.status) not in {"active", "filled", "partially_filled"}
    ):
        return "execution_leg_identity_mismatch"
    if (
        int(leg.management_batch_id) != profile.batch_id
        or int(leg.execution_order_leg_id) != int(entry.id)
        or int(leg.leg_index) != 0
        or str(leg.preflight_size) != profile.trusted_start_size
        or str(leg.planned_close_size) != (
            _decimal_text(
                Decimal(profile.trusted_start_size)
                - Decimal(profile.target_remaining_size)
            )
        )
    ):
        return "management_leg_identity_mismatch"
    return None


def _validated_contract(
    batch: StrategyManagementBatch, *, profile: CompositeBatchRecoveryProfile
):
    if (
        not batch.management_contract_json
        or not batch.management_contract_fingerprint
        or int(batch.contract_version or 0) != 2
    ):
        return "management_contract_missing"
    try:
        contract = load_management_contract(batch.management_contract_json)
        actual_fingerprint = management_contract_fingerprint(contract)
    except (TypeError, ValueError, RecursionError, OverflowError):
        return "management_contract_invalid"
    if (
        actual_fingerprint != str(batch.management_contract_fingerprint)
    ):
        return "management_contract_fingerprint_mismatch"
    expected_symbol = profile.instrument_id.split("-", 1)[0].upper()
    if (
        int(contract.target_lifecycle_id or 0) != profile.lifecycle_id
        or str(contract.strategy_instance_id or "")
        != str(batch.strategy_instance_id)
        or str(contract.symbol or "").upper() != expected_symbol
        or str(contract.side or "").lower() != profile.side.lower()
        or str(contract.close_fraction) != "0.5"
        or contract.stop_mode != "actual_entry_price"
        or contract.take_profit_consumption != "consume_first_stage"
        or contract.cancel_deferred_entries is not True
        or tuple(contract.required_components) != _EXPECTED_COMPONENTS
    ):
        return "management_contract_identity_mismatch"
    return contract


def _validated_target_snapshot(batch, *, binding, leg, entry, profile):
    try:
        payload = json.loads(batch.target_snapshot_json)
        actual_target_fingerprint = management_target_fingerprint(payload)
    except (TypeError, ValueError, RecursionError, OverflowError):
        return "target_snapshot_invalid"
    rows = payload.get("positions") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        return "target_snapshot_invalid"
    if actual_target_fingerprint != str(batch.target_fingerprint):
        return "target_snapshot_fingerprint_mismatch"
    identity = payload.get("identity")
    if not isinstance(identity, Mapping):
        return "target_snapshot_identity_mismatch"
    target_lifecycle_id = _exact_int(identity.get("target_lifecycle_id"))
    execution_binding_id = _exact_int(identity.get("execution_binding_id"))
    if (
        str(payload.get("execution_mode") or "") != str(batch.execution_mode)
        or target_lifecycle_id != int(batch.target_lifecycle_id)
        or execution_binding_id != int(batch.execution_binding_id)
        or str(identity.get("strategy_instance_id") or "")
        != str(batch.strategy_instance_id)
        or identity.get("manageable_entry_leg_ids") != [int(entry.id)]
        or identity.get("deferred_entry_leg_ids") != []
        or identity.get("capability_deferred_entry_leg_ids") != []
        or identity.get("capability_deferred_pos_ids") != []
        or payload.get("deferred_entry_legs") != []
    ):
        return "target_snapshot_identity_mismatch"
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("pos_id") or row.get("posId") or "") == str(leg.pos_id)
    ]
    if len(rows) != 1 or len(matches) != 1:
        return "target_snapshot_identity_mismatch"
    row = matches[0]
    required = (
        "trusted_start_size",
        "target_remaining_size",
        "avg_entry_price",
        "quantity_step",
        "min_quantity",
    )
    if any(row.get(key) in (None, "") for key in required):
        return "target_snapshot_identity_mismatch"
    if (
        str(row["trusted_start_size"]) != profile.trusted_start_size
        or str(row["target_remaining_size"]) != profile.target_remaining_size
        or str(row.get("instrument_id") or "").upper()
        != profile.instrument_id.upper()
        or str(row.get("side") or "").lower() != profile.side.lower()
        or str(row.get("size") or "") != profile.trusted_start_size
        or str(leg.avg_entry_price) != str(row["avg_entry_price"])
        or str(leg.quantity_step) != str(row["quantity_step"])
        or int(leg.execution_order_leg_id) != int(entry.id)
        or _exact_int(row.get("execution_order_leg_id")) != int(entry.id)
        or str(row.get("margin_mode") or "") != str(binding.margin_mode)
        or str(row.get("position_mode") or "") != str(binding.position_mode)
    ):
        return "target_snapshot_identity_mismatch"
    try:
        _positive_decimal(row["avg_entry_price"], "avg_entry_price")
        _positive_decimal(row["quantity_step"], "quantity_step")
        _positive_decimal(row["min_quantity"], "min_quantity")
    except CompositeBatchRecoveryRefusal:
        return "target_snapshot_identity_mismatch"
    return dict(row)


def _component_topology_refusal(
    components,
    *,
    batch,
    leg,
    entry,
    target,
    expected_contract_fingerprint: str,
) -> str | None:
    if len(components) != len(_EXPECTED_COMPONENTS):
        return "component_topology_mismatch"
    expected_statuses = ("recovery_required", "pending", "pending")
    for sequence, (component, kind, status) in enumerate(
        zip(components, _EXPECTED_COMPONENTS, expected_statuses, strict=True)
    ):
        if (
            int(component.management_batch_id) != int(batch.id)
            or int(component.strategy_management_leg_id or 0) != int(leg.id)
            or int(component.strategy_management_leg_scope) != int(leg.id)
            or int(component.sequence) != sequence
            or str(component.component_kind) != kind
            or str(component.status) != status
        ):
            return "component_topology_mismatch"
        try:
            desired = json.loads(component.desired_json)
        except (TypeError, ValueError, RecursionError):
            return "component_topology_mismatch"
        if not isinstance(desired, Mapping):
            return "component_topology_mismatch"
        expected = {
            "contract_fingerprint": expected_contract_fingerprint,
            "pos_id": str(leg.pos_id),
            "execution_order_leg_id": int(entry.id),
            "trusted_start_size": str(target["trusted_start_size"]),
            "target_remaining_size": str(target["target_remaining_size"]),
            "avg_entry_price": str(target["avg_entry_price"]),
            "quantity_step": str(target["quantity_step"]),
            "min_quantity": str(target["min_quantity"]),
            "component_kind": kind,
        }
        if dict(desired) != expected:
            return "component_topology_mismatch"
        expected_idempotency_key = hashlib.sha256(
            (
                f"{expected_contract_fingerprint}|{int(batch.id)}|"
                f"{int(leg.id)}|{kind}"
            ).encode("utf-8")
        ).hexdigest()
        if str(component.idempotency_key) != expected_idempotency_key:
            return "component_topology_mismatch"
        try:
            evidence = json.loads(component.evidence_json)
        except (TypeError, ValueError, RecursionError):
            return "component_topology_mismatch"
        if sequence == 0:
            if not _is_bounded_snapshot_incomplete_evidence(evidence):
                return "component_topology_mismatch"
        elif evidence != []:
            return "component_topology_mismatch"
    if (
        components[0].reason_code
        != "take_profit_exchange_snapshot_incomplete"
        or any(row.reason_code is not None for row in components[1:])
    ):
        return "component_topology_mismatch"
    return None


def _exact_false_submission_state(batch, *, leg, components) -> bool:
    return (
        str(batch.status) == "reconciling"
        and str(batch.reason_code)
        == "management_close_pending_exchange_confirmation"
        and str(leg.status) == "submitted"
        and [str(row.status) for row in components]
        == ["recovery_required", "pending", "pending"]
    )


def _legacy_false_state_evidence_refusal(leg, *, profile) -> str | None:
    try:
        snapshot = json.loads(str(leg.last_exchange_snapshot_json))
    except (TypeError, ValueError, RecursionError):
        return "durable_evidence_invalid"
    if not isinstance(snapshot, Mapping) or set(snapshot) != {
        "position_rows", "matching_regular_orders"
    }:
        return "false_submission_state_mismatch"
    position_rows = snapshot.get("position_rows")
    if (
        not isinstance(position_rows, list)
        or len(position_rows) != 1
        or snapshot.get("matching_regular_orders") != []
    ):
        return "false_submission_state_mismatch"
    position_row = position_rows[0]
    if not isinstance(position_row, Mapping) or set(position_row) != {
        "posId", "instId", "posSide", "pos"
    }:
        return "false_submission_state_mismatch"
    if (
        str(position_row.get("posId") or "") != str(leg.pos_id)
        or str(position_row.get("instId") or "").upper()
        != profile.instrument_id.upper()
        or str(position_row.get("posSide") or "").lower()
        != profile.side.lower()
        or str(position_row.get("pos") or "") != profile.trusted_start_size
    ):
        return "false_submission_state_mismatch"
    if leg.last_error in (None, ""):
        return None
    try:
        last_error = json.loads(str(leg.last_error))
    except (TypeError, ValueError, RecursionError):
        return "durable_evidence_invalid"
    if last_error != {"reason": "management_close_order_not_found"}:
        return "false_submission_state_mismatch"
    return None


def _has_durable_close_submission(session, *, batch, leg, entry) -> bool:
    if (
        session.query(PositionMutationIntent)
        .filter(
            PositionMutationIntent.execution_binding_id
            == int(batch.execution_binding_id),
            PositionMutationIntent.execution_order_leg_id == int(entry.id),
            PositionMutationIntent.pos_id == str(leg.pos_id),
            PositionMutationIntent.operation == "close_position",
        )
        .first()
        is not None
    ):
        return True
    events = (
        session.query(ExecutionEvent)
        .filter(
            ExecutionEvent.execution_binding_id
            == int(batch.execution_binding_id),
            ExecutionEvent.pos_id == str(leg.pos_id),
        )
        .all()
    )
    return any("close" in str(event.action or "").lower() for event in events)


def _has_additional_active_database_work(session, *, batch_id: int) -> bool:
    other_batch = (
        session.query(StrategyManagementBatch.id)
        .filter(
            StrategyManagementBatch.id != int(batch_id),
            StrategyManagementBatch.status.notin_(
                _SAFE_TERMINAL_MANAGEMENT_STATUSES
            ),
        )
        .first()
    )
    if other_batch is not None:
        return True
    if (
        session.query(StrategyManagementComponent.id)
        .filter(
            StrategyManagementComponent.management_batch_id != int(batch_id),
            StrategyManagementComponent.status.notin_(
                _SAFE_TERMINAL_COMPONENT_STATUSES
            ),
        )
        .first()
        is not None
    ):
        return True
    if (
        session.query(PositionMutationIntent.id)
        .filter(PositionMutationIntent.status.notin_(_TERMINAL_MUTATION_STATUSES))
        .first()
        is not None
    ):
        return True
    return (
        session.query(MessageInstructionItem.id)
        .filter(
            MessageInstructionItem.retired_at.is_(None),
            MessageInstructionItem.status.notin_(
                _SAFE_TERMINAL_INSTRUCTION_STATUSES
            ),
        )
        .first()
        is not None
    )


def _has_exchange_close_submission(snapshot: Any, *, pos_id: str) -> bool:
    for row in snapshot.position_history:
        if not isinstance(row, Mapping):
            return True
        if not _row_matches_position(row, pos_id=pos_id):
            continue
        if _row_matches_close_position(row, pos_id=pos_id):
            return True
        state = str(row.get("state") or row.get("status") or "").lower()
        close_size = _decimal_or_none(
            row.get("closeSz")
            or row.get("closedSize")
            or row.get("close_size")
        )
        if state in {"closed", "filled", "completed", "exited"} or (
            close_size is not None and close_size > 0
        ):
            return True

    for field in ("open_orders", "order_history", "trade_fills"):
        for row in getattr(snapshot, field):
            if not isinstance(row, Mapping):
                return True
            if not _row_matches_position(row, pos_id=pos_id):
                continue
            if _row_matches_close_position(row, pos_id=pos_id):
                return True
            reduce_only = str(
                row.get("reduceOnly") or row.get("reduce_only") or ""
            ).lower() in {"true", "1", "yes"}
            side = str(row.get("side") or "").lower()
            if reduce_only or side == "sell":
                return True
    for row in snapshot.trigger_history:
        if not isinstance(row, Mapping):
            return True
        if not _row_matches_position(row, pos_id=pos_id):
            continue
        if _row_matches_close_position(row, pos_id=pos_id):
            return True
        state = str(row.get("state") or row.get("status") or "").lower()
        reduce_only = str(
            row.get("reduceOnly") or row.get("reduce_only") or ""
        ).lower() in {"true", "1", "yes"}
        side = str(row.get("side") or "").lower()
        if state in {"filled", "triggered", "completed"} and (
            reduce_only or side == "sell"
        ):
            return True
    return False


def _protection_ownership_refusal(
    pending_rows,
    *,
    batch,
    binding,
    entry,
    ledger,
    pos_id: str,
    position: CompositeRecoveryPosition,
    profile,
) -> str | None:
    ledger_by_id: dict[str, Any] = {}
    purpose_counts = {"stop_loss": 0, "backup_stop": 0, "take_profit": 0}
    for row in ledger:
        order_id = str(row.order_id or "")
        purpose = str(row.purpose or "")
        if (
            not order_id
            or order_id in ledger_by_id
            or str(row.venue or "").lower() != "deepcoin"
            or int(row.execution_binding_id) != int(binding.id)
            or int(row.execution_order_leg_id) != int(entry.id)
            or str(row.strategy_instance_id or "")
            != str(batch.strategy_instance_id)
            or str(row.pos_id or "") != str(pos_id)
            or str(row.instrument_id or "").upper()
            != profile.instrument_id.upper()
            or str(row.side or "").lower() != profile.side.lower()
            or purpose not in {"stop_loss", "backup_stop", "take_profit"}
            or str(row.status or "").lower() != "verified"
        ):
            return "unexpected_protection_ownership"
        try:
            _optional_json_fingerprint(row.evidence_json)
        except CompositeBatchRecoveryRefusal:
            return "durable_evidence_invalid"
        ledger_by_id[order_id] = row
        purpose_counts[purpose] += 1
    if position.current_size is not None and (
        purpose_counts["stop_loss"] != 1
        or purpose_counts["backup_stop"] != 1
    ):
        return "unexpected_protection_ownership"

    pending_by_id: dict[str, Mapping[str, object]] = {}
    for row in pending_rows:
        if not isinstance(row, Mapping):
            return "unexpected_protection_ownership"
        if str(row.get("posId") or row.get("pos_id") or "") != str(pos_id):
            continue
        order_id = str(
            row.get("ordId") or row.get("orderId") or row.get("order_id") or ""
        )
        if not order_id or order_id in pending_by_id:
            return "unexpected_protection_ownership"
        if (
            str(row.get("instId") or row.get("instrument_id") or "").upper()
            != profile.instrument_id.upper()
            or str(row.get("posSide") or row.get("side") or "").lower()
            != profile.side.lower()
            or str(row.get("triggerOrderType") or "").upper() != "TPSL"
            or str(row.get("state") or row.get("status") or "").lower()
            != "live"
        ):
            return "unexpected_protection_ownership"
        pending_by_id[order_id] = row
    if position.current_size is None and not pending_by_id:
        return None
    if set(pending_by_id) != set(ledger_by_id):
        return "unexpected_protection_ownership"
    for order_id, ledger_row in ledger_by_id.items():
        pending_row = pending_by_id[order_id]
        trigger_price = _pending_protection_trigger_price(
            pending_row, purpose=str(ledger_row.purpose)
        )
        if not _same_optional_decimal(trigger_price, ledger_row.trigger_price):
            return "unexpected_protection_ownership"
        pending_size = pending_row.get("sz")
        if pending_size in (None, ""):
            pending_size = pending_row.get("size")
        if not _same_optional_decimal(pending_size, ledger_row.size_text):
            return "unexpected_protection_ownership"
    return None


def _source_evidence_payload(
    *, batch, raw, lifecycle, binding, entry, leg, components, target, contract,
    protection_ledger
):
    return {
        "schema_version": 1,
        "batch_id": int(batch.id),
        "raw_message_id": int(batch.raw_message_id),
        "raw_chat_ref": _redacted_ref("raw_chat", raw.chat_id),
        "lifecycle_id": int(lifecycle.id),
        "lifecycle_chat_ref": _redacted_ref("lifecycle_chat", lifecycle.chat_id),
        "lifecycle_message_ref": _redacted_ref(
            "lifecycle_message", lifecycle.message_id
        ),
        "lifecycle_symbol": str(lifecycle.symbol),
        "lifecycle_side": str(lifecycle.side),
        "binding_ref": _redacted_ref("binding", binding.id),
        "strategy_ref": _redacted_ref("strategy", batch.strategy_instance_id),
        "entry_leg_ref": _redacted_ref("entry_leg", entry.id),
        "position_ref": _redacted_ref("position", leg.pos_id),
        "batch_status": str(batch.status),
        "batch_reason_code": str(batch.reason_code),
        "batch_intent": str(batch.intent),
        "batch_effective_action": str(batch.effective_action),
        "batch_execution_mode": str(batch.execution_mode),
        "lifecycle_status": str(lifecycle.lifecycle_status),
        "lifecycle_binding_ref": _redacted_ref(
            "lifecycle_binding", lifecycle.execution_binding_id
        ),
        "binding_status": str(binding.status),
        "binding_strategy_ref": _redacted_ref(
            "binding_strategy", binding.strategy_instance_id
        ),
        "binding_chat_ref": _redacted_ref("binding_chat", binding.chat_id),
        "binding_message_ref": _redacted_ref(
            "binding_message", binding.message_id
        ),
        "binding_venue": str(binding.venue),
        "binding_symbol": str(binding.symbol),
        "binding_side": str(binding.side),
        "binding_margin_mode": str(binding.margin_mode),
        "binding_position_mode": str(binding.position_mode),
        "binding_position_ref": _redacted_ref("binding_position", binding.pos_id),
        "entry_status": str(entry.status),
        "entry_strategy_ref": _redacted_ref(
            "entry_strategy", entry.strategy_instance_id
        ),
        "entry_venue": str(entry.venue),
        "entry_purpose": str(entry.purpose),
        "entry_leg_index": int(entry.leg_index),
        "entry_attribution_status": str(entry.attribution_status),
        "entry_binding_ref": _redacted_ref(
            "entry_binding", entry.execution_binding_id
        ),
        "entry_position_ref": _redacted_ref("entry_position", entry.pos_id),
        "leg_status": str(leg.status),
        "management_leg_ref": _redacted_ref("management_leg", leg.id),
        "management_leg_batch_id": int(leg.management_batch_id),
        "management_leg_entry_ref": _redacted_ref(
            "management_leg_entry", leg.execution_order_leg_id
        ),
        "management_leg_index": int(leg.leg_index),
        "management_leg_position_ref": _redacted_ref(
            "management_leg_position", leg.pos_id
        ),
        "leg_preflight_size": str(leg.preflight_size),
        "leg_planned_close_size": str(leg.planned_close_size),
        "leg_avg_entry_price": str(leg.avg_entry_price),
        "leg_quantity_step": str(leg.quantity_step),
        "leg_submission_fields_present": {
            "request": leg.request_json not in (None, ""),
            "response": leg.response_json not in (None, ""),
            "client_order_id": leg.client_order_id not in (None, ""),
            "exchange_order_id": leg.exchange_order_id not in (None, ""),
        },
        "leg_last_exchange_snapshot_fingerprint": _optional_json_fingerprint(
            leg.last_exchange_snapshot_json
        ),
        "leg_last_error_fingerprint": _optional_json_fingerprint(
            leg.last_error
        ),
        "components": [
            {
                "component_ref": _redacted_ref("component", row.id),
                "leg_ref": _redacted_ref(
                    "component_leg", row.strategy_management_leg_id
                ),
                "sequence": int(row.sequence),
                "kind": str(row.component_kind),
                "status": str(row.status),
                "idempotency_ref": _redacted_ref(
                    "component_idempotency", row.idempotency_key
                ),
                "reason_code": row.reason_code,
                "attempt_count": int(row.attempt_count),
                "desired_fingerprint": _optional_json_fingerprint(
                    row.desired_json
                ),
                "evidence_fingerprint": _optional_json_fingerprint(
                    row.evidence_json
                ),
            }
            for row in components
        ],
        "contract_fingerprint": str(batch.management_contract_fingerprint),
        "contract_version": int(contract.version),
        "target_fingerprint": str(batch.target_fingerprint),
        "target_snapshot_fingerprint": _fingerprint(
            json.loads(batch.target_snapshot_json)
        ),
        "trusted_start_size": str(target["trusted_start_size"]),
        "target_remaining_size": str(target["target_remaining_size"]),
        "quantity_step": str(target["quantity_step"]),
        "min_quantity": str(target["min_quantity"]),
        "owned_protection_count": len(protection_ledger),
        "owned_protection": [
            {
                "ledger_ref": _redacted_ref("protection_ledger", row.id),
                "binding_ref": _redacted_ref(
                    "protection_binding", row.execution_binding_id
                ),
                "entry_leg_ref": _redacted_ref(
                    "protection_entry_leg", row.execution_order_leg_id
                ),
                "position_ref": _redacted_ref("protection_position", row.pos_id),
                "order_ref": _redacted_ref("protection_order", row.order_id),
                "instrument_id": str(row.instrument_id),
                "side": str(row.side),
                "purpose": str(row.purpose),
                "size": str(row.size_text),
                "trigger_price": str(row.trigger_price),
                "status": str(row.status),
                "evidence_fingerprint": _optional_json_fingerprint(
                    row.evidence_json
                ),
            }
            for row in protection_ledger
        ],
        "submission_fields_present": 0,
        "durable_close_evidence_count": 0,
    }


def _exchange_evidence_payload(
    snapshot, *, position, pos_id: str, ledger, profile
):
    owned_order_refs = sorted(
        _redacted_ref("protection_order", row.order_id) for row in ledger
    )
    exact_pending_refs = sorted(
        _redacted_ref(
            "pending_protection",
            row.get("ordId") or row.get("orderId") or row.get("order_id"),
        )
        for row in snapshot.pending_trigger_orders
        if isinstance(row, Mapping)
        and str(row.get("posId") or row.get("pos_id") or "") == pos_id
    )
    collection_digests = {
        field: {
            "count": len(getattr(snapshot, field)),
            "digest": _fingerprint(
                sorted(
                    _canonical_snapshot_row(row)
                    for row in getattr(snapshot, field)
                )
            ),
        }
        for field in (
            "positions",
            "position_history",
            "open_orders",
            "pending_trigger_orders",
            "order_history",
            "trade_fills",
            "trigger_history",
            "pending_tpsl_observations",
        )
    }
    return {
        "schema_version": 1,
        "instrument_id": profile.instrument_id,
        "side": profile.side,
        "position": _serialize_position(position),
        "collections": collection_digests,
        "owned_protection_refs": owned_order_refs,
        "pending_protection_refs": exact_pending_refs,
        "regular_close_evidence_count": 0,
        "snapshot_complete": True,
    }


def _refusal(batch_id: int, reason_code: str) -> CompositeBatchRecoveryPlan:
    evidence = {
        "schema_version": 1,
        "batch_id": int(batch_id),
        "decision": "refused",
        "reason_code": str(reason_code),
    }
    empty_source = _fingerprint(
        {"batch_id": int(batch_id), "source_state": "unproven"}
    )
    empty_exchange = _fingerprint(
        {"batch_id": int(batch_id), "exchange_state": "unproven"}
    )
    return CompositeBatchRecoveryPlan(
        batch_id=int(batch_id),
        status="refused",
        reason_code=str(reason_code),
        position=None,
        source_fingerprint=empty_source,
        exchange_snapshot_fingerprint=empty_exchange,
        evidence_fingerprint=_fingerprint(evidence),
        evidence=_freeze_mapping(evidence),
    )


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        _plain_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redacted_ref(kind: str, value: object) -> str:
    return hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()


def _serialize_position(position: CompositeRecoveryPosition) -> dict[str, Any]:
    return {
        "disposition": position.disposition,
        "current_size": position.current_size,
        "close_delta": position.close_delta,
        "effective_remaining_size": position.effective_remaining_size,
    }


def _proposed_transition(position: CompositeRecoveryPosition) -> dict[str, Any]:
    if position.disposition == "position_absent":
        return {
            "batch_status": "resolved",
            "batch_reason_code": "composite_recovery_exact_position_absent",
            "leg_status": "failed",
            "component_statuses": [
                "safely_skipped",
                "safely_skipped",
                "safely_skipped",
            ],
            "exchange_call_possible": False,
        }
    result: dict[str, Any] = {
        "batch_status": "ready",
        "leg_status": "planned",
        "component_statuses": ["recovery_required", "pending", "pending"],
        "exchange_call_possible": False,
    }
    if position.disposition == "protection_only_below_target":
        result.update(
            {
                "attestation_kind": "approved_under_target_recovery",
                "actual_remaining_size": position.effective_remaining_size,
                "original_target_remaining_size": (
                    BATCH_119_RECOVERY.target_remaining_size
                ),
                "append_component_attestation": True,
            }
        )
    return result


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            str(key): (
                _freeze_mapping(item)
                if isinstance(item, Mapping)
                else tuple(
                    _freeze_mapping(part) if isinstance(part, Mapping) else part
                    for part in item
                )
                if isinstance(item, (list, tuple))
                else item
            )
            for key, item in value.items()
        }
    )


def _plain_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item) for item in value]
    return value


def _canonical_snapshot_row(row: object) -> str:
    """Hash one raw row before it can enter retained evidence."""

    if not isinstance(row, Mapping):
        raise CompositeBatchRecoveryRefusal("exchange_snapshot_row_invalid")
    return _fingerprint(dict(row))


def _optional_json_fingerprint(value: str | None) -> str:
    if value in (None, ""):
        return _fingerprint(None)
    try:
        payload = json.loads(str(value))
        return _fingerprint(payload)
    except (TypeError, ValueError, RecursionError) as exc:
        raise CompositeBatchRecoveryRefusal("durable_json_invalid") from exc


def _exact_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and len(value) <= 20 and value.isdigit():
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if str(parsed) == value else None
    return None


def _refusal_batch_id(profile: object) -> int:
    value = getattr(profile, "batch_id", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _is_bounded_snapshot_incomplete_evidence(value: object) -> bool:
    if not isinstance(value, list) or len(value) != 1:
        return False
    fact = value[0]
    if not isinstance(fact, Mapping) or set(fact) != {"error_type"}:
        return False
    error_type = fact.get("error_type")
    return (
        isinstance(error_type, str)
        and 0 < len(error_type) <= 64
        and error_type.replace("_", "").replace(".", "").isalnum()
    )


def _row_matches_position(row: Mapping[str, object], *, pos_id: str) -> bool:
    return any(
        str(row.get(key) or "") == str(pos_id)
        for key in ("posId", "pos_id", "closePosId", "close_pos_id")
    )


def _row_matches_close_position(
    row: Mapping[str, object], *, pos_id: str
) -> bool:
    return any(
        str(row.get(key) or "") == str(pos_id)
        for key in ("closePosId", "close_pos_id")
    )


def _pending_protection_trigger_price(
    row: Mapping[str, object], *, purpose: str
) -> object:
    keys = (
        ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
        if purpose in {"stop_loss", "backup_stop"}
        else ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")
    )
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _same_optional_decimal(left: object, right: object) -> bool:
    if left in (None, "") and right in (None, ""):
        return True
    if left in (None, "") or right in (None, ""):
        return False
    left_decimal = _decimal_or_none(left)
    right_decimal = _decimal_or_none(right)
    return left_decimal is not None and left_decimal == right_decimal


def _decimal_or_none(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def classify_recovery_position(
    *,
    profile: CompositeBatchRecoveryProfile,
    positions: Sequence[Mapping[str, object]],
    expected_pos_id: str,
    instrument_id: str,
    side: str,
    quantity_step: str,
    min_quantity: str,
) -> CompositeRecoveryPosition:
    """Classify one exact exchange position against an immutable target."""

    trusted = _positive_decimal(profile.trusted_start_size, "trusted_start_size")
    target = _positive_decimal(profile.target_remaining_size, "target_remaining_size")
    step = _positive_decimal(quantity_step, "quantity_step")
    minimum = _positive_decimal(min_quantity, "min_quantity")
    if target > trusted:
        raise CompositeBatchRecoveryRefusal("recovery_target_above_trusted_start")
    for value, reason in (
        (trusted, "trusted_start_not_step_aligned"),
        (target, "target_remaining_not_step_aligned"),
    ):
        if not _is_step_aligned(value, step):
            raise CompositeBatchRecoveryRefusal(reason)

    matches = [
        row
        for row in positions
        if isinstance(row, Mapping)
        and str(row.get("posId") or row.get("pos_id") or "")
        == str(expected_pos_id)
    ]
    if len(matches) > 1:
        raise CompositeBatchRecoveryRefusal("exact_position_ambiguous")
    if not matches:
        return CompositeRecoveryPosition(
            disposition="position_absent",
            current_size=None,
            close_delta="0",
            effective_remaining_size="0",
        )

    row = matches[0]
    actual_instrument = str(
        row.get("instId") or row.get("instrument_id") or row.get("symbol") or ""
    ).upper()
    if actual_instrument != str(instrument_id).upper():
        raise CompositeBatchRecoveryRefusal("exact_position_instrument_mismatch")
    actual_side = str(row.get("posSide") or row.get("side") or "").lower()
    if actual_side != str(side).lower():
        raise CompositeBatchRecoveryRefusal("exact_position_side_mismatch")
    current = _positive_decimal(
        _first_present(row, "pos", "size", "sz", "positionSize", "position_size"),
        "current_size",
    )
    if current > trusted:
        raise CompositeBatchRecoveryRefusal("position_size_increased_after_snapshot")
    if current < minimum:
        raise CompositeBatchRecoveryRefusal("current_position_below_minimum")
    if not _is_step_aligned(current, step):
        raise CompositeBatchRecoveryRefusal("current_position_not_step_aligned")

    if current > target:
        delta = current - target
        if delta < minimum or not _is_step_aligned(delta, step):
            raise CompositeBatchRecoveryRefusal("target_remaining_delta_not_executable")
        return CompositeRecoveryPosition(
            disposition="resume_to_target",
            current_size=_decimal_text(current),
            close_delta=_decimal_text(delta),
            effective_remaining_size=_decimal_text(target),
        )
    if current == target:
        return CompositeRecoveryPosition(
            disposition="protection_only_at_target",
            current_size=_decimal_text(current),
            close_delta="0",
            effective_remaining_size=_decimal_text(target),
        )
    return CompositeRecoveryPosition(
        disposition="protection_only_below_target",
        current_size=_decimal_text(current),
        close_delta="0",
        effective_remaining_size=_decimal_text(current),
    )


def _positive_decimal(value: object, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CompositeBatchRecoveryRefusal(f"{field_name}_invalid") from exc
    if not result.is_finite() or result <= 0:
        raise CompositeBatchRecoveryRefusal(f"{field_name}_invalid")
    return result


def _is_step_aligned(value: Decimal, step: Decimal) -> bool:
    return (value / step) == (value / step).to_integral_value()


def _first_present(row: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    raise CompositeBatchRecoveryRefusal("current_size_missing")


def _decimal_text(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return "0" if normalized in {"", "-0"} else normalized
