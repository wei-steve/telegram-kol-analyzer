"""Closed, read-only planning for the approved composite batch incident."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

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
from telegram_kol_research.strategy_management_components import (
    transition_component_for_exact_position_absent_recovery,
)
from telegram_kol_research.strategy_management_take_profit_consumption import (
    plan_take_profit_consumption,
)
from telegram_kol_research.position_attribution import (
    has_authoritative_persisted_position,
)


class CompositeBatchRecoveryRefusal(ValueError):
    """The supplied incident evidence cannot safely authorize recovery."""

    def __init__(self, reason_code: str):
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


class CompositeBatchRecoveryConflict(RuntimeError):
    """The approved recovery plan no longer matches durable state."""

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


@dataclass(frozen=True, slots=True)
class CompositeBatchRecoveryApplyResult:
    batch_id: int
    status: Literal["repaired", "already_repaired"]
    evidence_fingerprint: str
    audit_event_id: int


@dataclass(frozen=True, slots=True)
class CompositeBatchRecoveryResumeAuthorization:
    plan: CompositeBatchRecoveryPlan
    repair_result: CompositeBatchRecoveryApplyResult


def build_composite_batch_recovery_status_summary(
    session_factory,
    *,
    plan: CompositeBatchRecoveryPlan,
    repair_result: CompositeBatchRecoveryApplyResult,
    executor_calls: int,
) -> dict[str, Any]:
    """Return bounded durable state after the one-shot recovery invocation."""

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, BATCH_119_RECOVERY.batch_id)
        if batch is None:
            raise CompositeBatchRecoveryConflict("repaired_batch_missing")
        components = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
            .order_by(
                StrategyManagementComponent.sequence,
                StrategyManagementComponent.id,
            )
            .all()
        )
        status_counts: dict[str, int] = {}
        for component in components:
            status = str(component.status)
            status_counts[status] = status_counts.get(status, 0) + 1
        mutation_intents = (
            session.query(PositionMutationIntent)
            .filter(
                PositionMutationIntent.execution_binding_id
                == batch.execution_binding_id
            )
            .all()
        )
        component_prefixes = tuple(f"{int(row.id)}:" for row in components)
        component_mutation_intents = [
            row
            for row in mutation_intents
            if str(row.idempotency_key or "").startswith(component_prefixes)
        ]
        recovery_audit_event_count = (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.action == _RECOVERY_AUDIT_ACTION,
                ExecutionEvent.notification_fingerprint
                == plan.evidence_fingerprint,
            )
            .count()
        )
    return {
        "audit_event_id": int(repair_result.audit_event_id),
        "batch_id": BATCH_119_RECOVERY.batch_id,
        "batch_reason_code": str(batch.reason_code or ""),
        "batch_status": str(batch.status),
        "component_count": len(components),
        "component_mutation_intent_count": len(component_mutation_intents),
        "component_status_counts": {
            key: status_counts[key] for key in sorted(status_counts)
        },
        "confirmed_close_intent_count": sum(
            str(row.operation) == "close_position"
            and str(row.status) == "confirmed"
            for row in component_mutation_intents
        ),
        "evidence_fingerprint": plan.evidence_fingerprint,
        "executor_calls": int(executor_calls),
        "position_disposition": (
            None if plan.position is None else plan.position.disposition
        ),
        "recovery_audit_event_count": int(recovery_audit_event_count),
        "repair_status": str(repair_result.status),
        "unresolved_mutation_intent_count": sum(
            str(row.status) not in _TERMINAL_MUTATION_STATUSES
            for row in component_mutation_intents
        ),
    }


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
BATCH_119_RECOVERY_AUTHORIZATION = (
    "I_AUTHORIZE_BATCH_119_TO_REMAINING_19"
)
_RECOVERY_AUTHORIZATION = BATCH_119_RECOVERY_AUTHORIZATION
_RECOVERY_AUDIT_ACTION = "composite_batch_false_state_repaired"
_RECOVERY_REASON = "composite_recovery_false_submission_repaired"


def create_composite_recovery_read_only_session_factory(
    database_path: str | Path,
) -> sessionmaker:
    """Open one existing SQLite database through an OS-enforced read-only URI."""

    resolved_path = Path(database_path).expanduser().resolve(strict=True)
    if not resolved_path.is_file():
        raise FileNotFoundError(resolved_path)
    sqlite_uri = f"file:{resolved_path.as_posix()}?mode=ro"
    engine = create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(
            sqlite_uri,
            uri=True,
            timeout=30,
        ),
        future=True,
    )
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )


def load_composite_batch_recovery_snapshot_read_only(
    session_factory,
    *,
    client: Any,
):
    """Load the generic snapshot plus exact batch-119 position history."""

    from telegram_kol_research.execution_bindings import (
        load_deepcoin_execution_reconciliation_snapshot_read_only,
    )

    snapshot = load_deepcoin_execution_reconciliation_snapshot_read_only(
        session_factory,
        client=client,
    )
    history_reader = getattr(client, "list_position_history", None)
    if history_reader is None:
        snapshot.errors["position_history"] = "unavailable"
        return snapshot
    try:
        rows = history_reader(
            inst_id=BATCH_119_RECOVERY.instrument_id,
            pos_id=_batch_119_position_id(session_factory),
        )
    except Exception:
        snapshot.errors["position_history"] = "unavailable"
        return snapshot
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) for row in rows
    ):
        snapshot.errors["position_history"] = "invalid_schema"
        return snapshot
    snapshot.position_history = rows
    return snapshot


def authorize_composite_batch_recovery_resume(
    session_factory,
    *,
    expected_fingerprint: str,
    snapshot: Any,
) -> CompositeBatchRecoveryResumeAuthorization:
    """Authorize only a progressed state descended from the exact repair audit."""

    if not _is_sha256(expected_fingerprint) or not _snapshot_is_complete(
        snapshot,
        profile=BATCH_119_RECOVERY,
    ):
        raise CompositeBatchRecoveryConflict("resume_evidence_invalid")
    with session_factory() as session:
        event = _load_recovery_audit_event(
            session,
            evidence_fingerprint=expected_fingerprint,
        )
        if event is None:
            raise CompositeBatchRecoveryConflict("resume_audit_missing")
        after = _validated_resume_audit_event(
            event,
            expected_fingerprint=expected_fingerprint,
        )
        batch = session.get(
            StrategyManagementBatch,
            BATCH_119_RECOVERY.batch_id,
        )
        lifecycle = session.get(
            StrategyLifecycle,
            BATCH_119_RECOVERY.lifecycle_id,
        )
        raw = session.get(RawMessage, BATCH_119_RECOVERY.raw_message_id)
        if batch is None or lifecycle is None or raw is None:
            raise CompositeBatchRecoveryConflict("resume_source_state_conflict")
        binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
        legs = (
            session.query(StrategyManagementLeg)
            .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
            .all()
        )
        components = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
            .order_by(
                StrategyManagementComponent.sequence,
                StrategyManagementComponent.id,
            )
            .all()
        )
        if binding is None or len(legs) != 1 or len(components) != 3:
            raise CompositeBatchRecoveryConflict("resume_source_state_conflict")
        if int(event.execution_binding_id) != int(binding.id):
            raise CompositeBatchRecoveryConflict("resume_audit_invalid")
        leg = legs[0]
        entry = session.get(ExecutionOrderLeg, int(leg.execution_order_leg_id))
        if entry is None or _durable_identity_refusal(
            session=session,
            batch=batch,
            raw=raw,
            lifecycle=lifecycle,
            binding=binding,
            entry=entry,
            leg=leg,
            profile=BATCH_119_RECOVERY,
        ) is not None:
            raise CompositeBatchRecoveryConflict("resume_source_state_conflict")
        contract = _validated_contract(batch, profile=BATCH_119_RECOVERY)
        target = _validated_target_snapshot(
            batch,
            binding=binding,
            leg=leg,
            entry=entry,
            profile=BATCH_119_RECOVERY,
        )
        if isinstance(contract, str) or isinstance(target, str):
            raise CompositeBatchRecoveryConflict("resume_source_state_conflict")
        original_owned_stop_refs = _original_owned_stop_refs(
            session,
            batch=batch,
            leg=leg,
            entry=entry,
            audit_created_at=event.created_at,
        )
        if after["original_owned_stop_refs"] != original_owned_stop_refs:
            raise CompositeBatchRecoveryConflict("resume_audit_invalid")
        disposition = str(after["position_disposition"])
        _validate_progressed_recovery_state(
            session,
            batch=batch,
            leg=leg,
            entry=entry,
            components=components,
            contract=contract,
            target=target,
            disposition=disposition,
            expected_fingerprint=expected_fingerprint,
            snapshot=snapshot,
            approved_current_size=after["current_size"],
            original_owned_stop_refs=original_owned_stop_refs,
        )
        if not _resume_exchange_close_evidence_is_owned(
            session,
            snapshot=snapshot,
            batch=batch,
            leg=leg,
            entry=entry,
            components=components,
        ):
            raise CompositeBatchRecoveryConflict(
                "resume_exchange_close_unowned"
            )
        audit_event_id = int(event.id)
        source_fingerprint = str(after["source_fingerprint"])
        exchange_fingerprint = str(after["exchange_snapshot_fingerprint"])
        current_size = after["current_size"]

    position = _resume_position(
        disposition=disposition,
        current_size=current_size,
    )
    plan = CompositeBatchRecoveryPlan(
        batch_id=BATCH_119_RECOVERY.batch_id,
        status="ready",
        reason_code="audited_recovery_resume_authorized",
        position=position,
        source_fingerprint=source_fingerprint,
        exchange_snapshot_fingerprint=exchange_fingerprint,
        evidence_fingerprint=expected_fingerprint,
        evidence=MappingProxyType({}),
    )
    return CompositeBatchRecoveryResumeAuthorization(
        plan=plan,
        repair_result=CompositeBatchRecoveryApplyResult(
            batch_id=BATCH_119_RECOVERY.batch_id,
            status="already_repaired",
            evidence_fingerprint=expected_fingerprint,
            audit_event_id=audit_event_id,
        ),
    )


def _batch_119_position_id(session_factory) -> str:
    with session_factory() as session:
        legs = (
            session.query(StrategyManagementLeg)
            .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
            .all()
        )
    if len(legs) != 1 or not str(legs[0].pos_id or ""):
        raise CompositeBatchRecoveryRefusal("management_leg_identity_mismatch")
    return str(legs[0].pos_id)


def _validated_resume_audit_event(
    event: ExecutionEvent,
    *,
    expected_fingerprint: str,
) -> Mapping[str, Any]:
    before = _safe_json_value(event.before_json)
    after = _safe_json_value(event.after_json)
    expected_after_keys = {
        "batch_id",
        "batch_status",
        "leg_status",
        "component_statuses",
        "component_attempt_counts",
        "source_fingerprint",
        "exchange_snapshot_fingerprint",
        "evidence_fingerprint",
        "position_disposition",
        "current_size",
        "target_remaining_size",
        "exchange_call_possible",
        "original_owned_stop_refs",
    }
    expected_before_keys = {
        "batch_id",
        "batch_status",
        "leg_status",
        "component_statuses",
        "component_attempt_counts",
    }
    if (
        not isinstance(before, Mapping)
        or not isinstance(after, Mapping)
        or set(before) != expected_before_keys
        or set(after) != expected_after_keys
        or event.before_json != _canonical_json(before)
        or event.after_json != _canonical_json(after)
        or before.get("batch_id") != BATCH_119_RECOVERY.batch_id
        or before.get("batch_status") != "reconciling"
        or before.get("leg_status") != "submitted"
        or before.get("component_statuses")
        != ["recovery_required", "pending", "pending"]
        or after.get("batch_id") != BATCH_119_RECOVERY.batch_id
        or after.get("evidence_fingerprint") != expected_fingerprint
        or after.get("target_remaining_size")
        != BATCH_119_RECOVERY.target_remaining_size
        or after.get("exchange_call_possible") is not False
        or not _is_sha256(after.get("source_fingerprint"))
        or not _is_sha256(after.get("exchange_snapshot_fingerprint"))
        or event.action != _RECOVERY_AUDIT_ACTION
        or event.status != "resolved"
        or event.notification_fingerprint != expected_fingerprint
        or event.venue != "deepcoin"
        or event.execution_binding_id is None
    ):
        raise CompositeBatchRecoveryConflict("resume_audit_invalid")
    disposition = str(after.get("position_disposition") or "")
    position_absent = disposition == "position_absent"
    expected_reason = (
        "composite_recovery_exact_position_absent"
        if position_absent
        else _RECOVERY_REASON
    )
    if (
        disposition
        not in {
            "resume_to_target",
            "protection_only_at_target",
            "protection_only_below_target",
            "position_absent",
        }
        or after.get("batch_status")
        != ("resolved" if position_absent else "ready")
        or after.get("leg_status")
        != ("failed" if position_absent else "planned")
        or after.get("component_statuses")
        != (
            ["safely_skipped"] * 3
            if position_absent
            else ["recovery_required", "pending", "pending"]
        )
        or event.reason != expected_reason
        or not _valid_original_owned_stop_refs(
            after.get("original_owned_stop_refs"),
            position_absent=position_absent,
        )
        or any(
            getattr(event, field) is not None
            for field in (
                "trade_signal_id",
                "strategy_instance_id",
                "kol_id",
                "chat_id",
                "message_id",
                "source_message_id",
                "symbol",
                "side",
                "order_id",
                "client_order_id",
                "pos_id",
                "related_order_id",
                "request_json",
                "response_json",
                "exchange_event_time",
                "notification_status",
                "notification_error",
                "notification_next_attempt_at",
                "notification_claim_token",
                "notification_claimed_at",
                "notified_at",
            )
        )
        or int(event.notification_attempts or 0) != 0
    ):
        raise CompositeBatchRecoveryConflict("resume_audit_invalid")
    attempts = before.get("component_attempt_counts")
    after_attempts = after.get("component_attempt_counts")
    if (
        not isinstance(attempts, list)
        or attempts != after_attempts
        or len(attempts) != 3
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in attempts
        )
    ):
        raise CompositeBatchRecoveryConflict("resume_audit_invalid")
    return after


def _validate_progressed_recovery_state(
    session,
    *,
    batch,
    leg,
    entry,
    components,
    contract,
    target,
    disposition: str,
    expected_fingerprint: str,
    snapshot: Any,
    approved_current_size: Any,
    original_owned_stop_refs: list[str],
) -> None:
    position_absent = disposition == "position_absent"
    if str(batch.status) not in (
        {"resolved"} if position_absent else {"ready", "executing", "succeeded"}
    ) or str(leg.status) != ("failed" if position_absent else "planned"):
        raise CompositeBatchRecoveryConflict("resume_state_not_executable")
    expected_reason = (
        "composite_recovery_exact_position_absent"
        if position_absent
        else _RECOVERY_REASON
    )
    if _safe_json_value(leg.last_error) != {
        "reason": expected_reason,
        "recovery_evidence_fingerprint": expected_fingerprint,
    }:
        raise CompositeBatchRecoveryConflict("resume_state_not_executable")
    allowed_statuses = {
        "pending",
        "preflighting",
        "submitting",
        "awaiting_exchange",
        "confirmed",
        "definitely_rejected",
        "recovery_required",
        "operator_required",
        "safely_skipped",
    }
    statuses = [str(row.status) for row in components]
    if not _resume_component_status_order_is_valid(
        batch_status=str(batch.status),
        statuses=statuses,
        position_absent=position_absent,
    ):
        raise CompositeBatchRecoveryConflict("resume_component_conflict")
    mutable_keys = {
        "consume_take_profit_stage": "take_profit_consumption_execution",
        "converge_partial_close": "partial_close_execution",
        "replace_remaining_protection": "protection_replacement_execution",
    }
    for sequence, (component, kind) in enumerate(
        zip(components, _EXPECTED_COMPONENTS, strict=True)
    ):
        if (
            int(component.management_batch_id) != BATCH_119_RECOVERY.batch_id
            or int(component.strategy_management_leg_id or 0) != int(leg.id)
            or int(component.strategy_management_leg_scope) != int(leg.id)
            or int(component.sequence) != sequence
            or str(component.component_kind) != kind
            or str(component.status) not in allowed_statuses
        ):
            raise CompositeBatchRecoveryConflict("resume_component_conflict")
        desired = _safe_json_value(component.desired_json)
        evidence = _safe_json_value(component.evidence_json)
        if not isinstance(desired, Mapping) or not isinstance(evidence, list):
            raise CompositeBatchRecoveryConflict("resume_component_conflict")
        expected = {
            "contract_fingerprint": str(batch.management_contract_fingerprint),
            "pos_id": str(leg.pos_id),
            "execution_order_leg_id": int(entry.id),
            "trusted_start_size": str(target["trusted_start_size"]),
            "target_remaining_size": str(target["target_remaining_size"]),
            "avg_entry_price": str(target["avg_entry_price"]),
            "quantity_step": str(target["quantity_step"]),
            "min_quantity": str(target["min_quantity"]),
            "component_kind": kind,
        }
        desired_copy = dict(desired)
        execution = desired_copy.pop(mutable_keys[kind], None)
        if desired_copy != expected or (
            execution is not None and not isinstance(execution, Mapping)
        ):
            raise CompositeBatchRecoveryConflict("resume_component_conflict")
        if not _resume_component_execution_matches_locked_state(
            session,
            batch=batch,
            leg=leg,
            entry=entry,
            component=component,
            kind=kind,
            execution=execution,
            target=target,
            original_owned_stop_refs=original_owned_stop_refs,
        ):
            raise CompositeBatchRecoveryConflict("resume_component_conflict")
        expected_key = hashlib.sha256(
            (
                f"{batch.management_contract_fingerprint}|{batch.id}|"
                f"{leg.id}|{kind}"
            ).encode("utf-8")
        ).hexdigest()
        if str(component.idempotency_key) != expected_key:
            raise CompositeBatchRecoveryConflict("resume_component_conflict")
        try:
            _fingerprint(evidence)
        except (TypeError, ValueError, RecursionError, OverflowError):
            raise CompositeBatchRecoveryConflict(
                "resume_component_conflict"
            ) from None
        if str(component.status) == "confirmed" and not (
            _resume_confirmed_component_matches_locked_state(
                session,
                batch=batch,
                leg=leg,
                entry=entry,
                component=component,
                kind=kind,
                contract=contract,
                execution=execution,
                evidence=evidence,
                snapshot=snapshot,
                target=target,
                disposition=disposition,
                approved_current_size=approved_current_size,
            )
        ):
            raise CompositeBatchRecoveryConflict("resume_component_conflict")
        if disposition == "protection_only_below_target":
            attestations = [
                row
                for row in evidence
                if isinstance(row, Mapping)
                and row.get("kind") == "approved_under_target_recovery"
                and row.get("recovery_evidence_fingerprint")
                == expected_fingerprint
            ]
            if len(attestations) != 1:
                raise CompositeBatchRecoveryConflict(
                    "resume_component_conflict"
                )
    own_prefixes = tuple(f"{int(row.id)}:" for row in components)
    for intent in session.query(PositionMutationIntent).filter(
        PositionMutationIntent.status.notin_(_TERMINAL_MUTATION_STATUSES)
    ):
        if not str(intent.idempotency_key or "").startswith(own_prefixes):
            raise CompositeBatchRecoveryConflict("additional_active_work_present")
    if (
        session.query(StrategyManagementBatch.id)
        .filter(
            StrategyManagementBatch.id != BATCH_119_RECOVERY.batch_id,
            StrategyManagementBatch.status.notin_(
                _SAFE_TERMINAL_MANAGEMENT_STATUSES
            ),
        )
        .first()
        is not None
        or session.query(StrategyManagementComponent.id)
        .filter(
            StrategyManagementComponent.management_batch_id
            != BATCH_119_RECOVERY.batch_id,
            StrategyManagementComponent.status.notin_(
                _SAFE_TERMINAL_COMPONENT_STATUSES
            ),
        )
        .first()
        is not None
        or session.query(MessageInstructionItem.id)
        .filter(
            MessageInstructionItem.retired_at.is_(None),
            MessageInstructionItem.status.notin_(
                _SAFE_TERMINAL_INSTRUCTION_STATUSES
            ),
        )
        .first()
        is not None
    ):
        raise CompositeBatchRecoveryConflict("additional_active_work_present")


def _resume_component_status_order_is_valid(
    *,
    batch_status: str,
    statuses: list[str],
    position_absent: bool,
) -> bool:
    if position_absent:
        return batch_status == "resolved" and statuses == [
            "safely_skipped",
            "safely_skipped",
            "safely_skipped",
        ]
    executable = {
        "pending",
        "preflighting",
        "submitting",
        "awaiting_exchange",
        "definitely_rejected",
        "recovery_required",
    }
    if len(statuses) != 3 or any(
        status not in executable | {"confirmed"} for status in statuses
    ):
        return False
    confirmed_count = 0
    for status in statuses:
        if status != "confirmed":
            break
        confirmed_count += 1
    if any(status == "confirmed" for status in statuses[confirmed_count:]):
        return False
    if any(status != "pending" for status in statuses[confirmed_count + 1 :]):
        return False
    if batch_status == "succeeded":
        return confirmed_count == 3
    if batch_status == "ready":
        return confirmed_count == 0 and statuses == [
            "recovery_required",
            "pending",
            "pending",
        ]
    return batch_status == "executing"


def _resume_confirmed_component_matches_locked_state(
    session,
    *,
    batch,
    leg,
    entry,
    component,
    kind: str,
    contract,
    execution: Any,
    evidence: list[Any],
    snapshot: Any,
    target: Mapping[str, Any],
    disposition: str,
    approved_current_size: Any,
) -> bool:
    if component.completed_at is None or int(component.attempt_count or 0) <= 0:
        return False
    intents = [
        row
        for row in session.query(PositionMutationIntent).all()
        if str(row.idempotency_key or "").startswith(f"{int(component.id)}:")
    ]
    if kind == "consume_take_profit_stage":
        return _resume_confirmed_take_profit_matches(
            session,
            batch=batch,
            leg=leg,
            entry=entry,
            component=component,
            contract=contract,
            execution=execution,
            evidence=evidence,
            intents=intents,
            snapshot=snapshot,
            target=target,
            approved_current_size=approved_current_size,
        )
    if kind == "converge_partial_close":
        return _resume_confirmed_close_matches(
            leg=leg,
            component=component,
            execution=execution,
            evidence=evidence,
            intents=intents,
            snapshot=snapshot,
            target=target,
            disposition=disposition,
            approved_current_size=approved_current_size,
        )
    return _resume_confirmed_protection_matches(
        session,
        batch=batch,
        leg=leg,
        entry=entry,
        component=component,
        execution=execution,
        evidence=evidence,
        intents=intents,
        snapshot=snapshot,
    )


def _resume_confirmed_take_profit_matches(
    session,
    *,
    batch,
    leg,
    entry,
    component,
    contract,
    execution: Any,
    evidence: list[Any],
    intents: list[Any],
    snapshot: Any,
    target: Mapping[str, Any],
    approved_current_size: Any,
) -> bool:
    if execution is None:
        if intents or len(evidence) < 2:
            return False
        phase = evidence[-2]
        result = evidence[-1]
        quantity = (
            _decimal_or_none(result.get("proven_filled_quantity"))
            if isinstance(result, Mapping)
            else None
        )
        if not (
            isinstance(phase, Mapping)
            and set(phase) == {"phase", "evidence_tier"}
            and phase.get("phase") == "no_cancel_required"
            and phase.get("evidence_tier")
            in {"exact_terminal_fill", "exact_terminal_no_fill"}
            and isinstance(result, Mapping)
            and set(result) == {"proven_filled_quantity"}
            and quantity is not None
            and quantity >= 0
        ):
            return False
        ledger_rows = session.query(PositionProtectionLedger).filter(
            PositionProtectionLedger.execution_binding_id
            == int(batch.execution_binding_id),
            PositionProtectionLedger.execution_order_leg_id == int(entry.id),
            PositionProtectionLedger.pos_id == str(leg.pos_id),
            PositionProtectionLedger.purpose == "take_profit",
        ).all()
        target_remaining = _decimal_or_none(
            target.get("target_remaining_size")
        )
        approved_current = _decimal_or_none(approved_current_size)
        if target_remaining is None or approved_current is None:
            return False
        effective_remaining = min(target_remaining, approved_current)
        try:
            rebuilt = plan_take_profit_consumption(
                contract=contract,
                target_leg={
                    "execution_binding_id": int(batch.execution_binding_id),
                    "execution_order_leg_id": int(entry.id),
                    "pos_id": str(leg.pos_id),
                    "instrument_id": BATCH_119_RECOVERY.instrument_id,
                    "side": BATCH_119_RECOVERY.side,
                },
                pending_orders=snapshot.pending_trigger_orders,
                trigger_history=snapshot.trigger_history,
                order_history=snapshot.order_history,
                trade_fills=snapshot.trade_fills,
                protection_ledger=ledger_rows,
                trusted_start_size=str(target["trusted_start_size"]),
                target_remaining_size=_decimal_text(effective_remaining),
            )
        except (TypeError, ValueError, RecursionError, OverflowError):
            return False
        return (
            rebuilt.refusal_code is None
            and not rebuilt.cancel_order_ids
            and not rebuilt.cancel_actions
            and rebuilt.evidence_tier == phase.get("evidence_tier")
            and rebuilt.proven_filled_quantity
            == str(result["proven_filled_quantity"])
        )
    order_ids = list(execution["cancel_order_ids"])
    intent_ids = list(execution["cancel_intent_ids"])
    selected = {int(row.id): row for row in intents}
    if set(selected) != set(intent_ids) or any(
        str(row.status) != "confirmed"
        or _safe_json_value(row.request_json)
        != {
            "instId": BATCH_119_RECOVERY.instrument_id,
            "instType": "SWAP",
            "ordId": str(row.order_id),
        }
        for row in selected.values()
    ):
        return False
    first_fact = {
        "cancel_order_ids": order_ids,
        "intent_id": intent_ids[0],
    }
    if first_fact not in evidence:
        return False
    terminal_fact = evidence[-1] if evidence else None
    terminal_valid = (
        isinstance(terminal_fact, Mapping)
        and set(terminal_fact) in (
            {"intent_id"},
            {"intent_id", "fill_race"},
        )
        and int(terminal_fact.get("intent_id") or 0) in set(intent_ids)
        and (
            "fill_race" not in terminal_fact
            or terminal_fact.get("fill_race") is True
        )
    ) or str(component.reason_code) == "take_profit_cancel_exchange_confirmed"
    if not terminal_valid:
        return False
    ledgers = session.query(PositionProtectionLedger).filter(
        PositionProtectionLedger.order_id.in_(order_ids)
    ).all()
    pending_ids = _snapshot_order_ids(snapshot.pending_trigger_orders)
    return len(ledgers) == len(order_ids) and all(
        str(row.venue) == "deepcoin"
        and int(row.execution_binding_id) == int(batch.execution_binding_id)
        and int(row.execution_order_leg_id) == int(entry.id)
        and str(row.pos_id) == str(leg.pos_id)
        and str(row.status) == "cancelled"
        for row in ledgers
    ) and not set(order_ids).intersection(pending_ids)


def _resume_confirmed_close_matches(
    *,
    leg,
    component,
    execution: Any,
    evidence: list[Any],
    intents: list[Any],
    snapshot: Any,
    target: Mapping[str, Any],
    disposition: str,
    approved_current_size: Any,
) -> bool:
    expected_remaining = (
        _decimal_or_none(approved_current_size)
        if disposition == "protection_only_below_target"
        else _decimal_or_none(target.get("target_remaining_size"))
    )
    live_remaining = _snapshot_exact_position_size(
        snapshot,
        pos_id=str(leg.pos_id),
    )
    if (
        expected_remaining is None
        or live_remaining is None
        or live_remaining != expected_remaining
    ):
        return False
    if execution is None:
        if intents or len(evidence) < 2:
            return False
        plan_fact = evidence[-2]
        terminal_fact = evidence[-1]
        return bool(
            isinstance(plan_fact, Mapping)
            and plan_fact == {"close_delta": "0"}
            and isinstance(terminal_fact, Mapping)
            and terminal_fact.get("remaining_size")
            == _decimal_text(expected_remaining)
            and terminal_fact.get("evidence_tier")
            in {
                "exact_position_target",
                "approved_under_target_recovery",
            }
        )
    intent_id = int(execution["intent_id"])
    if len(intents) != 1 or int(intents[0].id) != intent_id:
        return False
    intent = intents[0]
    if str(intent.status) != "confirmed":
        return False
    plan_fact = {
        "close_delta": str(execution["close_delta"]),
        "intent_id": intent_id,
    }
    if plan_fact not in evidence:
        return False
    terminal_fact = evidence[-1] if evidence else None
    return bool(
        isinstance(terminal_fact, Mapping)
        and int(terminal_fact.get("intent_id") or 0) == intent_id
        and (
            (
                terminal_fact.get("remaining_size")
                == _decimal_text(expected_remaining)
                and terminal_fact.get("evidence_tier")
                == "exact_position_target"
            )
            or terminal_fact.get("unresolved_delta") == "0"
        )
    )


def _resume_confirmed_protection_matches(
    session,
    *,
    batch,
    leg,
    entry,
    component,
    execution: Any,
    evidence: list[Any],
    intents: list[Any],
    snapshot: Any,
) -> bool:
    if not isinstance(execution, Mapping) or len(evidence) < 2:
        return False
    old_ids = list(execution["old_stop_order_ids"])
    terminal = evidence[-1]
    if (
        not isinstance(terminal, Mapping)
        or set(terminal) != {
            "new_stop_order_ids",
            "cancelled_old_stop_order_ids",
            "retained_take_profit_total",
            "effective_remaining_size",
        }
        or not _unique_nonempty_strings(terminal.get("new_stop_order_ids"))
        or len(terminal["new_stop_order_ids"]) != 2
        or terminal.get("cancelled_old_stop_order_ids") != sorted(old_ids)
        or terminal.get("retained_take_profit_total")
        != execution.get("retained_take_profit_total")
        or terminal.get("effective_remaining_size")
        != execution.get("effective_remaining_size")
    ):
        return False
    new_ids = list(terminal["new_stop_order_ids"])
    by_key = {str(row.idempotency_key): row for row in intents}
    expected_keys = {
        f"{int(component.id)}:set:primary",
        f"{int(component.id)}:set:backup",
        *(f"{int(component.id)}:cancel-old:{order_id}" for order_id in old_ids),
    }
    if set(by_key) != expected_keys or any(
        str(row.status) != "confirmed" for row in by_key.values()
    ):
        return False
    binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
    if binding is None:
        return False
    set_intents = [
        by_key[f"{int(component.id)}:set:{role}"]
        for role in ("primary", "backup")
    ]
    if {str(row.order_id or "") for row in set_intents} != set(new_ids):
        return False
    for old_order_id in old_ids:
        cancel_intent = by_key[
            f"{int(component.id)}:cancel-old:{old_order_id}"
        ]
        if _safe_json_value(cancel_intent.request_json) != {
            "instId": BATCH_119_RECOVERY.instrument_id,
            "instType": "SWAP",
            "ordId": old_order_id,
        }:
            return False
    old_ledgers = session.query(PositionProtectionLedger).filter(
        PositionProtectionLedger.order_id.in_(old_ids)
    ).all()
    new_ledgers = session.query(PositionProtectionLedger).filter(
        PositionProtectionLedger.order_id.in_(new_ids)
    ).all()
    if len(old_ledgers) != len(old_ids) or len(new_ledgers) != 2:
        return False
    if any(
        str(row.status) != "cancelled"
        or int(row.execution_binding_id) != int(batch.execution_binding_id)
        or int(row.execution_order_leg_id) != int(entry.id)
        or str(row.pos_id) != str(leg.pos_id)
        for row in old_ledgers
    ):
        return False
    expected_prices = {
        "stop_loss": _decimal_or_none(execution.get("primary_stop")),
        "backup_stop": _decimal_or_none(execution.get("backup_stop")),
    }
    expected_size = _decimal_or_none(execution.get("effective_remaining_size"))
    if expected_size is None or any(
        str(row.status) != "verified"
        or int(row.execution_binding_id) != int(batch.execution_binding_id)
        or int(row.execution_order_leg_id) != int(entry.id)
        or str(row.pos_id) != str(leg.pos_id)
        or str(row.instrument_id).upper() != BATCH_119_RECOVERY.instrument_id
        or str(row.side).lower() != BATCH_119_RECOVERY.side
        or str(row.purpose) not in expected_prices
        or _decimal_or_none(row.trigger_price)
        != expected_prices[str(row.purpose)]
        or _decimal_or_none(row.size_text) != expected_size
        for row in new_ledgers
    ):
        return False
    pending_rows = {
        order_id: [
            row
            for row in snapshot.pending_trigger_orders
            if isinstance(row, Mapping)
            and str(
                row.get("ordId")
                or row.get("orderId")
                or row.get("order_id")
                or ""
            )
            == order_id
        ]
        for order_id in new_ids
    }
    pending = {
        order_id: rows[0]
        for order_id, rows in pending_rows.items()
        if len(rows) == 1
    }
    if (
        any(len(rows) != 1 for rows in pending_rows.values())
        or set(old_ids).intersection(
            _snapshot_order_ids(snapshot.pending_trigger_orders)
        )
    ):
        return False
    ledger_by_id = {str(row.order_id): row for row in new_ledgers}
    for role, purpose in (("primary", "stop_loss"), ("backup", "backup_stop")):
        intent = by_key[f"{int(component.id)}:set:{role}"]
        order_id = str(intent.order_id or "")
        ledger = ledger_by_id.get(order_id)
        pending_row = pending.get(order_id)
        request = _safe_json_value(intent.request_json)
        if (
            ledger is None
            or pending_row is None
            or not isinstance(request, Mapping)
            or set(request) != {
                "_ledger_purpose",
                "instId",
                "instType",
                "mrgPosition",
                "posId",
                "posSide",
                "slOrdPx",
                "slTriggerPx",
                "slTriggerPxType",
                "sz",
                "tdMode",
            }
            or request.get("_ledger_purpose") != purpose
            or request.get("instId") != BATCH_119_RECOVERY.instrument_id
            or request.get("instType") != "SWAP"
            or str(request.get("mrgPosition") or "").lower()
            != str(binding.position_mode).lower()
            or request.get("posId") != str(leg.pos_id)
            or str(request.get("posSide") or "").lower()
            != BATCH_119_RECOVERY.side
            or request.get("slOrdPx") != "-1"
            or request.get("slTriggerPxType") != "last"
            or _decimal_or_none(request.get("slTriggerPx"))
            != expected_prices[purpose]
            or _decimal_or_none(request.get("sz")) != expected_size
            or str(request.get("tdMode") or "").lower()
            != str(binding.margin_mode).lower()
            or str(ledger.purpose) != purpose
            or _decimal_or_none(ledger.trigger_price)
            != expected_prices[purpose]
            or str(pending_row.get("posId") or pending_row.get("pos_id") or "")
            != str(leg.pos_id)
            or str(
                pending_row.get("instId")
                or pending_row.get("instrument_id")
                or ""
            ).upper()
            != BATCH_119_RECOVERY.instrument_id
            or str(
                pending_row.get("posSide")
                or pending_row.get("side")
                or ""
            ).lower()
            != BATCH_119_RECOVERY.side
            or _decimal_or_none(
                pending_row.get("sz") or pending_row.get("size")
            )
            != expected_size
            or _decimal_or_none(
                pending_row.get("slTriggerPx")
                or pending_row.get("slTriggerPrice")
                or pending_row.get("trigger_price")
            )
            != expected_prices[purpose]
        ):
            return False
    return len(pending) == 2


def _snapshot_order_ids(rows: Any) -> set[str]:
    return {
        order_id
        for row in rows
        if isinstance(row, Mapping)
        and (
            order_id := str(
                row.get("ordId")
                or row.get("orderId")
                or row.get("order_id")
                or ""
            )
        )
    }


def _snapshot_exact_position_size(snapshot: Any, *, pos_id: str) -> Decimal | None:
    matches = [
        row
        for row in snapshot.positions
        if isinstance(row, Mapping)
        and str(row.get("posId") or row.get("pos_id") or "") == pos_id
    ]
    return (
        _decimal_or_none(matches[0].get("pos") or matches[0].get("size"))
        if len(matches) == 1
        else None
    )


def _resume_component_execution_matches_locked_state(
    session,
    *,
    batch,
    leg,
    entry,
    component,
    kind: str,
    execution: Any,
    target: Mapping[str, Any],
    original_owned_stop_refs: list[str],
) -> bool:
    intents = [
        row
        for row in session.query(PositionMutationIntent).all()
        if str(row.idempotency_key or "").startswith(f"{int(component.id)}:")
    ]
    try:
        if any(
            not isinstance((request := _safe_json_value(row.request_json)), Mapping)
            or not _is_sha256(row.request_fingerprint)
            or _fingerprint(
                {
                    key: value
                    for key, value in request.items()
                    if key != "_ledger_purpose"
                }
            )
            != str(row.request_fingerprint)
            for row in intents
        ):
            return False
    except (TypeError, ValueError, RecursionError, OverflowError):
        return False
    expected_operations = {
        "consume_take_profit_stage": {"cancel_position_sltp"},
        "converge_partial_close": {"close_position"},
        "replace_remaining_protection": {
            "set_position_sltp",
            "cancel_position_sltp",
        },
    }[kind]
    if any(
        str(intent.operation) not in expected_operations
        or str(intent.venue) != "deepcoin"
        or str(intent.strategy_instance_id) != str(batch.strategy_instance_id)
        or int(intent.execution_binding_id) != int(batch.execution_binding_id)
        or int(intent.execution_order_leg_id) != int(entry.id)
        or str(intent.pos_id) != str(leg.pos_id)
        for intent in intents
    ):
        return False
    if execution is None:
        return not intents
    if kind == "consume_take_profit_stage":
        if set(execution) != {
            "cancel_order_ids",
            "cancel_intent_ids",
            "evidence_tier",
        }:
            return False
        order_ids = execution.get("cancel_order_ids")
        intent_ids = execution.get("cancel_intent_ids")
        if (
            not _unique_nonempty_strings(order_ids)
            or not _unique_positive_ints(intent_ids)
            or not isinstance(execution.get("evidence_tier"), str)
            or execution.get("evidence_tier")
            not in {
                "exact_pending_owned_order",
                "exact_terminal_fill",
                "exact_terminal_no_fill",
                "none",
            }
        ):
            return False
        selected = [row for row in intents if int(row.id) in set(intent_ids)]
        return (
            len(selected) == len(intent_ids)
            and len(selected) == len(intents)
            and all(
                str(row.operation) == "cancel_position_sltp"
                and str(row.order_id or "") in set(order_ids)
                and str(row.idempotency_key).startswith(
                    f"{int(component.id)}:cancel:{str(row.order_id)}:attempt:"
                )
                for row in selected
            )
        )
    if kind == "converge_partial_close":
        if set(execution) != {
            "close_delta",
            "client_order_id",
            "intent_id",
            "pre_submit_size",
        } or not _unique_positive_ints([execution.get("intent_id")]):
            return False
        selected = [
            row for row in intents
            if int(row.id) == int(execution["intent_id"])
        ]
        if len(selected) != 1 or len(intents) != 1:
            return False
        intent = selected[0]
        request = _safe_json_value(intent.request_json)
        binding = session.get(
            ExecutionBinding,
            int(batch.execution_binding_id),
        )
        close_delta = _decimal_or_none(execution.get("close_delta"))
        pre_submit_size = _decimal_or_none(execution.get("pre_submit_size"))
        target_remaining = _decimal_or_none(target.get("target_remaining_size"))
        return bool(
            isinstance(request, Mapping)
            and binding is not None
            and set(request) == {
                "clOrdId",
                "closePosId",
                "instId",
                "mrgPosition",
                "ordType",
                "posSide",
                "side",
                "sz",
                "tdMode",
            }
            and close_delta is not None
            and close_delta > 0
            and pre_submit_size is not None
            and target_remaining is not None
            and pre_submit_size - close_delta == target_remaining
            and str(intent.operation) == "close_position"
            and str(intent.idempotency_key).startswith(
                f"{int(component.id)}:close:attempt:"
            )
            and str(request.get("clOrdId") or "")
            == str(execution.get("client_order_id") or "")
            and _decimal_or_none(request.get("sz")) == close_delta
            and str(request.get("closePosId") or "") == str(leg.pos_id)
            and str(request.get("instId") or "").upper()
            == BATCH_119_RECOVERY.instrument_id
            and str(request.get("posSide") or "").lower()
            == BATCH_119_RECOVERY.side
            and str(request.get("side") or "").lower() == "sell"
            and str(request.get("ordType") or "").lower() == "market"
            and str(request.get("mrgPosition") or "").lower()
            == str(binding.position_mode).lower()
            and str(request.get("tdMode") or "").lower()
            == str(binding.margin_mode).lower()
        )
    if set(execution) != {
        "primary_stop",
        "backup_stop",
        "old_stop_order_ids",
        "retained_take_profit_total",
        "effective_remaining_size",
    }:
        return False
    old_order_ids = execution.get("old_stop_order_ids")
    if sorted(
        _redacted_ref("protection_order", order_id)
        for order_id in (old_order_ids or [])
    ) != original_owned_stop_refs:
        return False
    primary = _decimal_or_none(execution.get("primary_stop"))
    backup = _decimal_or_none(execution.get("backup_stop"))
    effective = _decimal_or_none(execution.get("effective_remaining_size"))
    retained = _decimal_or_none(execution.get("retained_take_profit_total"))
    target_remaining = _decimal_or_none(target.get("target_remaining_size"))
    if (
        not _unique_nonempty_strings(old_order_ids, allow_empty=True)
        or primary is None
        or backup is None
        or primary <= 0
        or backup <= 0
        or primary == backup
        or effective is None
        or target_remaining is None
        or effective <= 0
        or effective > target_remaining
        or retained is None
        or retained < 0
        or retained > effective
    ):
        return False
    ledger_rows = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.order_id.in_(list(old_order_ids)))
        .all()
        if old_order_ids
        else []
    )
    return len(ledger_rows) == len(old_order_ids) and all(
        str(row.venue) == "deepcoin"
        and int(row.execution_binding_id) == int(batch.execution_binding_id)
        and int(row.execution_order_leg_id) == int(entry.id)
        and str(row.pos_id) == str(leg.pos_id)
        and str(row.instrument_id).upper() == BATCH_119_RECOVERY.instrument_id
        and str(row.side).lower() == BATCH_119_RECOVERY.side
        and str(row.purpose) in {"stop_loss", "backup_stop"}
        for row in ledger_rows
    )


def _unique_nonempty_strings(value: Any, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) and bool(item) for item in value)
        and len(set(value)) == len(value)
    )


def _unique_positive_ints(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            not isinstance(item, bool) and isinstance(item, int) and item > 0
            for item in value
        )
        and len(set(value)) == len(value)
    )


def _resume_exchange_close_evidence_is_owned(
    session,
    *,
    snapshot: Any,
    batch,
    leg,
    entry,
    components,
) -> bool:
    close_rows = _exchange_close_rows(snapshot, pos_id=str(leg.pos_id))
    if not close_rows:
        return True
    close_component = next(
        row
        for row in components
        if str(row.component_kind) == "converge_partial_close"
    )
    intents = [
        row
        for row in session.query(PositionMutationIntent).filter_by(
            execution_binding_id=int(batch.execution_binding_id),
            execution_order_leg_id=int(entry.id),
            pos_id=str(leg.pos_id),
            operation="close_position",
        )
        if str(row.idempotency_key or "").startswith(
            f"{int(close_component.id)}:close:attempt:"
        )
    ]
    order_ids: set[str] = set()
    client_order_ids: set[str] = set()
    for intent in intents:
        request = _safe_json_value(intent.request_json)
        response = _safe_json_value(intent.response_json)
        if not isinstance(request, Mapping):
            return False
        client_order_id = str(request.get("clOrdId") or "")
        if client_order_id:
            client_order_ids.add(client_order_id)
        order_id = str(intent.order_id or "")
        if isinstance(response, Mapping):
            data = response.get("data")
            if isinstance(data, Mapping):
                order_id = order_id or str(data.get("ordId") or "")
            order_id = order_id or str(response.get("ordId") or "")
        if order_id:
            order_ids.add(order_id)
    return bool(intents) and all(
        str(
            row.get("ordId")
            or row.get("orderId")
            or row.get("order_id")
            or ""
        )
        in order_ids
        or str(
            row.get("clOrdId")
            or row.get("clientOrderId")
            or row.get("client_order_id")
            or ""
        )
        in client_order_ids
        for row in close_rows
    )


def _exchange_close_rows(snapshot: Any, *, pos_id: str) -> list[Mapping]:
    rows: list[Mapping] = []
    for row in snapshot.position_history:
        if not isinstance(row, Mapping) or not _row_matches_position(
            row, pos_id=pos_id
        ):
            continue
        state = str(row.get("state") or row.get("status") or "").lower()
        close_size = _decimal_or_none(
            row.get("closeSz")
            or row.get("closedSize")
            or row.get("close_size")
        )
        if _row_matches_close_position(row, pos_id=pos_id) or state in {
            "closed",
            "filled",
            "completed",
            "exited",
        } or (close_size is not None and close_size > 0):
            rows.append(row)
    for field in ("open_orders", "order_history", "trade_fills"):
        for row in getattr(snapshot, field):
            if not isinstance(row, Mapping) or not _row_matches_position(
                row, pos_id=pos_id
            ):
                continue
            reduce_only = str(
                row.get("reduceOnly") or row.get("reduce_only") or ""
            ).lower() in {"true", "1", "yes"}
            if (
                _row_matches_close_position(row, pos_id=pos_id)
                or reduce_only
                or str(row.get("side") or "").lower() == "sell"
            ):
                rows.append(row)
    for row in snapshot.trigger_history:
        if not isinstance(row, Mapping) or not _row_matches_position(
            row, pos_id=pos_id
        ):
            continue
        state = str(row.get("state") or row.get("status") or "").lower()
        reduce_only = str(
            row.get("reduceOnly") or row.get("reduce_only") or ""
        ).lower() in {"true", "1", "yes"}
        if _row_matches_close_position(row, pos_id=pos_id) or (
            state in {"filled", "triggered", "completed"}
            and (reduce_only or str(row.get("side") or "").lower() == "sell")
        ):
            rows.append(row)
    return rows


def _resume_position(
    *,
    disposition: str,
    current_size: Any,
) -> CompositeRecoveryPosition:
    if disposition == "position_absent":
        if current_size is not None:
            raise CompositeBatchRecoveryConflict("resume_audit_invalid")
        return CompositeRecoveryPosition(
            disposition="position_absent",
            current_size=None,
            close_delta="0",
            effective_remaining_size="0",
        )
    current = _decimal_or_none(current_size)
    target = Decimal(BATCH_119_RECOVERY.target_remaining_size)
    if current is None or current <= 0:
        raise CompositeBatchRecoveryConflict("resume_audit_invalid")
    expected_relation = (
        "resume_to_target"
        if current > target
        else "protection_only_at_target"
        if current == target
        else "protection_only_below_target"
    )
    if disposition != expected_relation:
        raise CompositeBatchRecoveryConflict("resume_audit_invalid")
    return CompositeRecoveryPosition(
        disposition=disposition,
        current_size=_decimal_text(current),
        close_delta=(
            _decimal_text(current - target)
            if disposition == "resume_to_target"
            else "0"
        ),
        effective_remaining_size=(
            _decimal_text(current)
            if disposition == "protection_only_below_target"
            else BATCH_119_RECOVERY.target_remaining_size
        ),
    )


def apply_composite_batch_false_state_repair(
    session_factory,
    *,
    plan: CompositeBatchRecoveryPlan,
    expected_fingerprint: str,
    authorization: str,
    applied_at: datetime | None = None,
) -> CompositeBatchRecoveryApplyResult:
    """Atomically repair only the proven batch-119 legacy false state."""

    if authorization != _RECOVERY_AUTHORIZATION:
        raise CompositeBatchRecoveryConflict("authorization_invalid")
    if (
        not isinstance(plan, CompositeBatchRecoveryPlan)
        or plan.status != "ready"
        or plan.batch_id != BATCH_119_RECOVERY.batch_id
    ):
        raise CompositeBatchRecoveryConflict("plan_not_actionable")
    try:
        if (
            str(expected_fingerprint) != plan.evidence_fingerprint
            or _fingerprint(plan.evidence) != plan.evidence_fingerprint
        ):
            raise CompositeBatchRecoveryConflict(
                "evidence_fingerprint_mismatch"
            )
        _validate_recovery_plan_consistency(plan)
    except CompositeBatchRecoveryConflict:
        raise
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise CompositeBatchRecoveryConflict("plan_evidence_invalid") from exc
    applied = applied_at or datetime.now(UTC)
    with session_factory() as session:
        _acquire_recovery_write_lock(session)
        existing = _load_recovery_audit_event(
            session, evidence_fingerprint=plan.evidence_fingerprint
        )
        if existing is not None:
            if _repaired_state_and_event_match(session, event=existing, plan=plan):
                return CompositeBatchRecoveryApplyResult(
                    batch_id=BATCH_119_RECOVERY.batch_id,
                    status="already_repaired",
                    evidence_fingerprint=plan.evidence_fingerprint,
                    audit_event_id=int(existing.id),
                )
            raise CompositeBatchRecoveryConflict("recovery_audit_conflict")
        source = _load_locked_recovery_source(session)
        if source is None:
            raise CompositeBatchRecoveryConflict("source_state_conflict")
        (
            batch,
            binding,
            entry,
            leg,
            components,
            ledger,
            source_fingerprint,
        ) = source
        if source_fingerprint != plan.source_fingerprint:
            raise CompositeBatchRecoveryConflict("source_fingerprint_conflict")
        evidence = _plain_json_value(plan.evidence)
        if (
            evidence["durable"]["component_attempt_counts"]
            != [int(row.attempt_count) for row in components]
            or evidence["exchange"]["owned_protection_count"] != len(ledger)
        ):
            raise CompositeBatchRecoveryConflict("plan_evidence_stale")
        if plan.position is None or plan.position.disposition not in {
            "resume_to_target",
            "protection_only_at_target",
            "protection_only_below_target",
            "position_absent",
        }:
            raise CompositeBatchRecoveryConflict("plan_disposition_not_supported")

        before = {
            "batch_id": int(batch.id),
            "batch_status": str(batch.status),
            "leg_status": str(leg.status),
            "component_statuses": [str(row.status) for row in components],
            "component_attempt_counts": [
                int(row.attempt_count) for row in components
            ],
        }
        position_absent = plan.position.disposition == "position_absent"
        recovery_reason = (
            "composite_recovery_exact_position_absent"
            if position_absent
            else _RECOVERY_REASON
        )
        batch.status = "resolved" if position_absent else "ready"
        batch.reason_code = recovery_reason
        batch.last_progress_at = applied
        batch.updated_at = applied
        if position_absent:
            batch.reconciled_at = applied
            batch.completed_at = applied
        leg.status = "failed" if position_absent else "planned"
        leg.last_error = _canonical_json(
            {
                "reason": recovery_reason,
                "recovery_evidence_fingerprint": plan.evidence_fingerprint,
            }
        )
        leg.updated_at = applied
        if position_absent:
            for component in components:
                if not transition_component_for_exact_position_absent_recovery(
                    session,
                    component_id=int(component.id),
                    expected_status=str(component.status),
                    recovery_evidence_fingerprint=plan.evidence_fingerprint,
                    now=applied,
                ):
                    raise CompositeBatchRecoveryConflict(
                        "component_state_conflict"
                    )
        elif plan.position.disposition == "protection_only_below_target":
            attestation = _under_target_attestation(plan)
            for component in components:
                evidence = _safe_json_value(component.evidence_json)
                if not isinstance(evidence, list):
                    raise CompositeBatchRecoveryConflict(
                        "component_evidence_invalid"
                    )
                component.evidence_json = _canonical_json(
                    [*evidence, attestation]
                )
                component.updated_at = applied
        after = _recovery_audit_after(
            plan,
            batch_status=str(batch.status),
            leg_status=str(leg.status),
            component_statuses=[str(row.status) for row in components],
            original_owned_stop_refs=_owned_stop_refs_from_ledger(ledger),
        )
        event = ExecutionEvent(
            execution_binding_id=int(binding.id),
            venue="deepcoin",
            action=_RECOVERY_AUDIT_ACTION,
            status="resolved",
            reason=recovery_reason,
            before_json=_canonical_json(before),
            after_json=_canonical_json(after),
            notification_fingerprint=plan.evidence_fingerprint,
            created_at=applied,
        )
        session.add(event)
        session.commit()
        return CompositeBatchRecoveryApplyResult(
            batch_id=BATCH_119_RECOVERY.batch_id,
            status="repaired",
            evidence_fingerprint=plan.evidence_fingerprint,
            audit_event_id=int(event.id),
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
            session=session,
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
                "component_attempt_counts": [
                    int(row.attempt_count) for row in components
                ],
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


def _acquire_recovery_write_lock(session) -> None:
    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def _load_locked_recovery_source(session):
    batch = session.get(StrategyManagementBatch, BATCH_119_RECOVERY.batch_id)
    if batch is None:
        return None
    lifecycle = session.get(StrategyLifecycle, BATCH_119_RECOVERY.lifecycle_id)
    raw = session.get(RawMessage, BATCH_119_RECOVERY.raw_message_id)
    binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
    legs = (
        session.query(StrategyManagementLeg)
        .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
        .order_by(StrategyManagementLeg.leg_index, StrategyManagementLeg.id)
        .all()
    )
    components = (
        session.query(StrategyManagementComponent)
        .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
        .order_by(
            StrategyManagementComponent.sequence,
            StrategyManagementComponent.id,
        )
        .all()
    )
    if (
        raw is None
        or lifecycle is None
        or binding is None
        or len(legs) != 1
        or len(components) != len(_EXPECTED_COMPONENTS)
    ):
        return None
    leg = legs[0]
    entry = session.get(ExecutionOrderLeg, int(leg.execution_order_leg_id))
    if entry is None:
        return None
    if _durable_identity_refusal(
        session=session,
        batch=batch,
        raw=raw,
        lifecycle=lifecycle,
        binding=binding,
        entry=entry,
        leg=leg,
        profile=BATCH_119_RECOVERY,
    ) is not None:
        return None
    contract = _validated_contract(batch, profile=BATCH_119_RECOVERY)
    if isinstance(contract, str):
        return None
    target = _validated_target_snapshot(
        batch,
        binding=binding,
        leg=leg,
        entry=entry,
        profile=BATCH_119_RECOVERY,
    )
    if isinstance(target, str):
        return None
    if _component_topology_refusal(
        components,
        batch=batch,
        leg=leg,
        entry=entry,
        target=target,
        expected_contract_fingerprint=str(batch.management_contract_fingerprint),
    ) is not None:
        return None
    if not _exact_false_submission_state(batch, leg=leg, components=components):
        return None
    if _legacy_false_state_evidence_refusal(
        leg, profile=BATCH_119_RECOVERY
    ) is not None:
        return None
    if any(
        value not in (None, "")
        for value in (
            leg.request_json,
            leg.response_json,
            leg.client_order_id,
            leg.exchange_order_id,
        )
    ):
        return None
    if _has_durable_close_submission(
        session, batch=batch, leg=leg, entry=entry
    ):
        return None
    if _has_additional_active_database_work(
        session, batch_id=BATCH_119_RECOVERY.batch_id
    ):
        return None
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
    try:
        payload = _source_evidence_payload(
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
        source_fingerprint = _fingerprint(payload)
    except (CompositeBatchRecoveryRefusal, TypeError, ValueError, RecursionError):
        return None
    return batch, binding, entry, leg, components, ledger, source_fingerprint


def _recovery_audit_after(
    plan: CompositeBatchRecoveryPlan,
    *,
    batch_status: str,
    leg_status: str,
    component_statuses: list[str],
    original_owned_stop_refs: list[str],
) -> dict[str, Any]:
    return {
        "batch_id": BATCH_119_RECOVERY.batch_id,
        "batch_status": str(batch_status),
        "leg_status": str(leg_status),
        "component_statuses": list(component_statuses),
        "component_attempt_counts": _component_attempt_counts_from_plan(plan),
        "source_fingerprint": plan.source_fingerprint,
        "exchange_snapshot_fingerprint": plan.exchange_snapshot_fingerprint,
        "evidence_fingerprint": plan.evidence_fingerprint,
        "position_disposition": plan.position.disposition if plan.position else None,
        "current_size": plan.position.current_size if plan.position else None,
        "target_remaining_size": BATCH_119_RECOVERY.target_remaining_size,
        "exchange_call_possible": False,
        "original_owned_stop_refs": list(original_owned_stop_refs),
    }


def _owned_stop_refs_from_ledger(ledger: Sequence[Any]) -> list[str]:
    refs = sorted(
        _redacted_ref("protection_order", row.order_id)
        for row in ledger
        if str(row.purpose) in {"stop_loss", "backup_stop"}
    )
    if len(refs) != len(set(refs)):
        raise CompositeBatchRecoveryConflict("owned_stop_identity_conflict")
    return refs


def _valid_original_owned_stop_refs(
    value: Any,
    *,
    position_absent: bool,
) -> bool:
    expected_count = 0 if position_absent else 2
    return bool(
        isinstance(value, list)
        and len(value) == expected_count
        and value == sorted(value)
        and len(set(value)) == len(value)
        and all(_is_sha256(item) for item in value)
    )


def _original_owned_stop_refs(
    session,
    *,
    batch,
    leg,
    entry,
    audit_created_at: Any,
) -> list[str]:
    rows = (
        session.query(PositionProtectionLedger)
        .filter(
            PositionProtectionLedger.execution_binding_id
            == int(batch.execution_binding_id),
            PositionProtectionLedger.execution_order_leg_id == int(entry.id),
            PositionProtectionLedger.pos_id == str(leg.pos_id),
            PositionProtectionLedger.purpose.in_(("stop_loss", "backup_stop")),
            PositionProtectionLedger.created_at <= audit_created_at,
        )
        .all()
    )
    return _owned_stop_refs_from_ledger(rows)


def _under_target_attestation(
    plan: CompositeBatchRecoveryPlan,
) -> dict[str, str]:
    if (
        plan.position is None
        or plan.position.disposition != "protection_only_below_target"
    ):
        raise CompositeBatchRecoveryConflict("under_target_attestation_invalid")
    return {
        "kind": "approved_under_target_recovery",
        "actual_remaining_size": str(plan.position.effective_remaining_size),
        "original_target_remaining_size": (
            BATCH_119_RECOVERY.target_remaining_size
        ),
        "recovery_evidence_fingerprint": plan.evidence_fingerprint,
    }


def _position_absent_evidence(
    plan: CompositeBatchRecoveryPlan,
) -> dict[str, str]:
    if plan.position is None or plan.position.disposition != "position_absent":
        raise CompositeBatchRecoveryConflict("position_absent_evidence_invalid")
    return {
        "kind": "composite_recovery_exact_position_absent",
        "recovery_evidence_fingerprint": plan.evidence_fingerprint,
    }


def _load_recovery_audit_event(session, *, evidence_fingerprint: str):
    rows = (
        session.query(ExecutionEvent)
        .filter(
            ExecutionEvent.notification_fingerprint
            == str(evidence_fingerprint)
        )
        .all()
    )
    if len(rows) > 1:
        raise CompositeBatchRecoveryConflict("recovery_audit_conflict")
    return rows[0] if rows else None


def _repaired_state_and_event_match(
    session, *, event: ExecutionEvent, plan: CompositeBatchRecoveryPlan
) -> bool:
    batch = session.get(StrategyManagementBatch, BATCH_119_RECOVERY.batch_id)
    lifecycle = session.get(
        StrategyLifecycle, BATCH_119_RECOVERY.lifecycle_id
    )
    raw = session.get(RawMessage, BATCH_119_RECOVERY.raw_message_id)
    if batch is None or lifecycle is None or raw is None:
        return False
    binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
    legs = (
        session.query(StrategyManagementLeg)
        .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
        .all()
    )
    components = (
        session.query(StrategyManagementComponent)
        .filter_by(management_batch_id=BATCH_119_RECOVERY.batch_id)
        .order_by(StrategyManagementComponent.sequence)
        .all()
    )
    if binding is None or len(legs) != 1 or len(components) != 3:
        return False
    leg = legs[0]
    entry = session.get(ExecutionOrderLeg, int(leg.execution_order_leg_id))
    if entry is None:
        return False
    if _durable_identity_refusal(
        session=session,
        batch=batch,
        raw=raw,
        lifecycle=lifecycle,
        binding=binding,
        entry=entry,
        leg=leg,
        profile=BATCH_119_RECOVERY,
    ) is not None:
        return False
    contract = _validated_contract(batch, profile=BATCH_119_RECOVERY)
    if isinstance(contract, str):
        return False
    target = _validated_target_snapshot(
        batch,
        binding=binding,
        leg=leg,
        entry=entry,
        profile=BATCH_119_RECOVERY,
    )
    if isinstance(target, str):
        return False
    if plan.position is None:
        return False
    position_absent = plan.position.disposition == "position_absent"
    expected_component_statuses = (
        ("safely_skipped",) * len(_EXPECTED_COMPONENTS)
        if position_absent
        else ("recovery_required", "pending", "pending")
    )
    recovery_reason = (
        "composite_recovery_exact_position_absent"
        if position_absent
        else _RECOVERY_REASON
    )
    evidence_suffix = (
        _position_absent_evidence(plan)
        if position_absent
        else _under_target_attestation(plan)
        if plan.position.disposition == "protection_only_below_target"
        else None
    )
    if _component_topology_refusal(
        components,
        batch=batch,
        leg=leg,
        entry=entry,
        target=target,
        expected_contract_fingerprint=str(
            batch.management_contract_fingerprint
        ),
        evidence_suffix=evidence_suffix,
        expected_statuses=expected_component_statuses,
        expected_reason_codes=(
            (recovery_reason,) * len(_EXPECTED_COMPONENTS)
            if position_absent
            else (
                "take_profit_exchange_snapshot_incomplete",
                None,
                None,
            )
        ),
    ) is not None:
        return False
    if _legacy_false_exchange_snapshot_refusal(
        leg, profile=BATCH_119_RECOVERY
    ) is not None:
        return False
    if _has_durable_close_submission(
        session, batch=batch, leg=leg, entry=entry
    ):
        return False
    if _has_additional_active_database_work(
        session, batch_id=BATCH_119_RECOVERY.batch_id
    ):
        return False
    component_attempt_counts = _component_attempt_counts_from_plan(plan)
    before = {
        "batch_id": BATCH_119_RECOVERY.batch_id,
        "batch_status": "reconciling",
        "leg_status": "submitted",
        "component_statuses": ["recovery_required", "pending", "pending"],
        "component_attempt_counts": component_attempt_counts,
    }
    after = _recovery_audit_after(
        plan,
        batch_status="resolved" if position_absent else "ready",
        leg_status="failed" if position_absent else "planned",
        component_statuses=list(expected_component_statuses),
        original_owned_stop_refs=_original_owned_stop_refs(
            session,
            batch=batch,
            leg=leg,
            entry=entry,
            audit_created_at=event.created_at,
        ),
    )
    return (
        str(batch.status) == ("resolved" if position_absent else "ready")
        and str(batch.reason_code) == recovery_reason
        and batch.reconciled_at
        == (event.created_at if position_absent else None)
        and batch.completed_at
        == (event.created_at if position_absent else None)
        and str(leg.status) == ("failed" if position_absent else "planned")
        and _safe_json_value(leg.last_error)
        == {
            "reason": recovery_reason,
            "recovery_evidence_fingerprint": plan.evidence_fingerprint,
        }
        and leg.request_json is None
        and leg.response_json is None
        and leg.client_order_id is None
        and leg.exchange_order_id is None
        and [str(row.status) for row in components]
        == list(expected_component_statuses)
        and [int(row.attempt_count) for row in components]
        == component_attempt_counts
        and all(
            row.completed_at
            == (event.created_at if position_absent else None)
            for row in components
        )
        and event.execution_binding_id == int(batch.execution_binding_id)
        and event.trade_signal_id is None
        and event.strategy_instance_id is None
        and event.venue == "deepcoin"
        and event.action == _RECOVERY_AUDIT_ACTION
        and event.status == "resolved"
        and event.reason == recovery_reason
        and event.before_json == _canonical_json(before)
        and event.after_json == _canonical_json(after)
        and event.kol_id is None
        and event.chat_id is None
        and event.message_id is None
        and event.source_message_id is None
        and event.symbol is None
        and event.side is None
        and event.order_id is None
        and event.client_order_id is None
        and event.pos_id is None
        and event.related_order_id is None
        and event.request_json is None
        and event.response_json is None
        and event.exchange_event_time is None
        and event.notification_status is None
        and event.notification_fingerprint == plan.evidence_fingerprint
        and event.notification_message_id is None
        and event.notification_error is None
        and event.notification_attempts == 0
        and event.notification_next_attempt_at is None
        and event.notification_claim_token is None
        and event.notification_claimed_at is None
        and event.notified_at is None
    )


def _safe_json_value(value: str | None) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError, RecursionError):
        return None


def _validate_recovery_plan_consistency(plan: CompositeBatchRecoveryPlan) -> None:
    if (
        plan.reason_code != "false_legacy_submission_proven"
        or plan.production_writes != 0
        or plan.exchange_calls != 0
    ):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    evidence = _plain_json_value(plan.evidence)
    if not isinstance(evidence, Mapping):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    expected_evidence_keys = {
        "schema_version",
        "batch_id",
        "decision",
        "reason_code",
        "source_fingerprint",
        "exchange_snapshot_fingerprint",
        "immutable_target",
        "position",
        "durable",
        "exchange",
        "proposed_transition",
    }
    immutable_target = evidence.get("immutable_target")
    durable = evidence.get("durable")
    exchange = evidence.get("exchange")
    if (
        set(evidence) != expected_evidence_keys
        or not isinstance(immutable_target, Mapping)
        or not isinstance(durable, Mapping)
        or not isinstance(exchange, Mapping)
    ):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    attempt_counts = durable.get("component_attempt_counts")
    if (
        not isinstance(attempt_counts, list)
        or len(attempt_counts) != len(_EXPECTED_COMPONENTS)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in attempt_counts
        )
    ):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    expected_durable = {
        "batch_status": "reconciling",
        "leg_status": "submitted",
        "component_statuses": ["recovery_required", "pending", "pending"],
        "component_attempt_counts": attempt_counts,
        "component_count": len(_EXPECTED_COMPONENTS),
        "close_submission_evidence_count": 0,
    }
    expected_target = {
        "instrument_id": BATCH_119_RECOVERY.instrument_id,
        "side": BATCH_119_RECOVERY.side,
        "trusted_start_size": BATCH_119_RECOVERY.trusted_start_size,
        "target_remaining_size": BATCH_119_RECOVERY.target_remaining_size,
        "quantity_step": immutable_target.get("quantity_step"),
        "min_quantity": immutable_target.get("min_quantity"),
    }
    if not isinstance(
        plan.position, CompositeRecoveryPosition
    ) or not _position_matches_recovery_profile(
        plan.position,
        quantity_step=expected_target["quantity_step"],
        min_quantity=expected_target["min_quantity"],
    ):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    expected_exchange = {
        "snapshot_complete": True,
        "exact_position_count": (
            0 if plan.position.disposition == "position_absent" else 1
        ),
        "regular_close_evidence_count": 0,
        "owned_protection_count": exchange.get("owned_protection_count"),
    }
    owned_protection_count = exchange.get("owned_protection_count")
    minimum_owned_protection = (
        0 if plan.position.disposition == "position_absent" else 2
    )
    if (
        evidence.get("schema_version") != 1
        or evidence.get("batch_id") != BATCH_119_RECOVERY.batch_id
        or evidence.get("decision") != "repair_false_legacy_submission"
        or evidence.get("reason_code") != "false_legacy_submission_proven"
        or evidence.get("source_fingerprint") != plan.source_fingerprint
        or evidence.get("exchange_snapshot_fingerprint")
        != plan.exchange_snapshot_fingerprint
        or evidence.get("immutable_target") != expected_target
        or durable != expected_durable
        or not isinstance(owned_protection_count, int)
        or isinstance(owned_protection_count, bool)
        or owned_protection_count < minimum_owned_protection
        or exchange != expected_exchange
        or evidence.get("position") != _serialize_position(plan.position)
        or evidence.get("proposed_transition") != _proposed_transition(plan.position)
        or not all(
            _is_sha256(value)
            for value in (
                plan.source_fingerprint,
                plan.exchange_snapshot_fingerprint,
                plan.evidence_fingerprint,
            )
        )
    ):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")


def _component_attempt_counts_from_plan(
    plan: CompositeBatchRecoveryPlan,
) -> list[int]:
    try:
        durable = _plain_json_value(plan.evidence)["durable"]
        values = durable["component_attempt_counts"]
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent") from exc
    if (
        not isinstance(values, list)
        or len(values) != len(_EXPECTED_COMPONENTS)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        )
    ):
        raise CompositeBatchRecoveryConflict("plan_evidence_inconsistent")
    return list(values)


def _position_matches_recovery_profile(
    position: CompositeRecoveryPosition,
    *,
    quantity_step: object,
    min_quantity: object,
) -> bool:
    try:
        step = Decimal(str(quantity_step))
        minimum = Decimal(str(min_quantity))
        close_delta = Decimal(str(position.close_delta))
        effective = Decimal(str(position.effective_remaining_size))
        current = (
            None
            if position.current_size is None
            else Decimal(str(position.current_size))
        )
    except (InvalidOperation, TypeError, ValueError):
        return False
    decimal_values = [close_delta, effective]
    if current is not None:
        decimal_values.append(current)
    if (
        any(not value.is_finite() for value in [step, minimum, *decimal_values])
        or step <= 0
        or minimum <= 0
        or any(value < 0 or value % step != 0 for value in decimal_values)
        or _decimal_text(close_delta) != str(position.close_delta)
        or _decimal_text(effective) != str(position.effective_remaining_size)
        or (
            current is not None
            and _decimal_text(current) != str(position.current_size)
        )
    ):
        return False
    trusted = Decimal(BATCH_119_RECOVERY.trusted_start_size)
    target = Decimal(BATCH_119_RECOVERY.target_remaining_size)
    expected_by_disposition = {
        "resume_to_target": (trusted, trusted - target, target),
        "protection_only_at_target": (target, Decimal(0), target),
        "position_absent": (None, Decimal(0), Decimal(0)),
    }
    if position.disposition == "protection_only_below_target":
        return (
            current is not None
            and minimum <= current < target
            and close_delta == 0
            and effective == current
        )
    expected = expected_by_disposition.get(position.disposition)
    return expected is not None and (current, close_delta, effective) == expected


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
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
    *, session, batch, raw, lifecycle, binding, entry, leg, profile
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
    if not has_authoritative_persisted_position(entry, session=session):
        return "position_ownership_evidence_not_authoritative"
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
    evidence_suffix: Mapping[str, Any] | None = None,
    expected_statuses: tuple[str, ...] = (
        "recovery_required",
        "pending",
        "pending",
    ),
    expected_reason_codes: tuple[str | None, ...] = (
        "take_profit_exchange_snapshot_incomplete",
        None,
        None,
    ),
) -> str | None:
    if len(components) != len(_EXPECTED_COMPONENTS):
        return "component_topology_mismatch"
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
        expected_attestation = (
            [dict(evidence_suffix)]
            if evidence_suffix is not None
            else []
        )
        if sequence == 0:
            if not (
                isinstance(evidence, list)
                and _is_bounded_snapshot_incomplete_evidence(evidence[:1])
                and evidence[1:] == expected_attestation
            ):
                return "component_topology_mismatch"
        elif evidence != expected_attestation:
            return "component_topology_mismatch"
    if tuple(row.reason_code for row in components) != expected_reason_codes:
        return "component_topology_mismatch"
    return None


def _exact_false_submission_state(batch, *, leg, components) -> bool:
    return (
        str(batch.status) == "reconciling"
        and str(batch.reason_code)
        == "management_close_pending_exchange_confirmation"
        and batch.reconciled_at is None
        and batch.completed_at is None
        and str(leg.status) == "submitted"
        and [str(row.status) for row in components]
        == ["recovery_required", "pending", "pending"]
        and all(row.completed_at is None for row in components)
    )


def _legacy_false_state_evidence_refusal(leg, *, profile) -> str | None:
    snapshot_reason = _legacy_false_exchange_snapshot_refusal(
        leg, profile=profile
    )
    if snapshot_reason is not None:
        return snapshot_reason
    if leg.last_error in (None, ""):
        return None
    try:
        last_error = json.loads(str(leg.last_error))
    except (TypeError, ValueError, RecursionError):
        return "durable_evidence_invalid"
    if last_error != {"reason": "management_close_order_not_found"}:
        return "false_submission_state_mismatch"
    return None


def _legacy_false_exchange_snapshot_refusal(leg, *, profile) -> str | None:
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
