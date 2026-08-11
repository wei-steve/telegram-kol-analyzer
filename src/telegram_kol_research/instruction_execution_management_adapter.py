"""Project exact durable management evidence into instruction contracts."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping

from sqlalchemy import and_, or_
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.instruction_execution_contracts import (
    InstructionExecutionConflictError,
    load_or_create_instruction_execution_contract,
    transition_instruction_execution_contract,
)
from telegram_kol_research.models import (
    EntryRevisionReplacement,
    ExecutionBinding,
    ExecutionEvent,
    InstructionExecutionContract,
    MessageInstructionItem,
    PositionMutationIntent,
    RecognitionDecision,
    SignalCandidate,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyManagementLeg,
    StrategyLifecycle,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
    StrategyThread,
    TradingSetting,
)


logger = logging.getLogger(__name__)


_UNKNOWN_ARTIFACT_STATES = frozenset(
    {
        "submit_unknown",
        "unknown",
        "unknown_exchange_outcome",
        "recovery_required",
        "operator_required",
    }
)
_WRITE_ARTIFACT_STATES = frozenset(
    {
        "reserved",
        "awaiting_exchange",
        "submitting",
        "submitted",
        "succeeded",
        "confirmed",
        "restored",
        *_UNKNOWN_ARTIFACT_STATES,
    }
)
_SUCCESS_ARTIFACT_STATES = frozenset(
    {"succeeded", "confirmed", "verified", "closed", "restored"}
)
_DEFINITE_FAILURE_STATES = frozenset(
    {"failed", "blocked", "rejected", "cancelled", "skipped"}
)
_CANCEL_ACTIONS = frozenset(
    {"cancel", "cancel_entry", "cancel_pending_entry", "cancel_order"}
)
_EXIT_ACTIONS = frozenset(
    {"full_exit", "full_close", "close_position", "exit_full"}
)
_UNKNOWN_CONVERGENCE_CURSOR_KEY = (
    "instruction_execution_management_unknown_convergence_cursor"
)


class ManagementExecutionContractBlocked(RuntimeError):
    """Durable management evidence cannot authorize the requested projection."""


@dataclass(frozen=True, slots=True)
class ManagementInstructionMirror:
    effective_status: str
    contract_id: int | None
    contract_state: str | None
    contract_state_version: int | None
    divergence: bool
    evidence: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class LinkedManagementProjection:
    message_instruction_item_id: int
    mode: str
    contract: InstructionExecutionContract


@dataclass(frozen=True, slots=True)
class _ManagementEvidence:
    target_state: str
    reason_code: str
    attempted_exchange_write: bool
    terminal_kind: str | None
    completion_scope: str | None
    refs: tuple[dict[str, object], ...]


def _enabled(mode: str) -> bool:
    normalized = str(mode).strip().lower()
    if normalized not in {"disabled", "shadow", "live"}:
        raise ValueError("execution contract mode must be disabled, shadow, or live")
    return normalized != "disabled"


def _load_contract(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
) -> InstructionExecutionContract | None:
    with session_factory() as session:
        contract = (
            session.query(InstructionExecutionContract)
            .filter(
                InstructionExecutionContract.message_instruction_item_id
                == int(message_instruction_item_id)
            )
            .one_or_none()
        )
        if contract is not None:
            session.expunge(contract)
        return contract


def _json_mapping(value: str | None) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _event_matches_batch(event: ExecutionEvent, *, batch_id: int) -> bool:
    for payload in (event.before_json, event.after_json, event.request_json):
        mapping = _json_mapping(payload)
        value = mapping.get("managementBatchId", mapping.get("management_batch_id"))
        if value is not None and str(value) == str(batch_id):
            return True
    return False


def _terminal_kind(*, candidate_action: str, batch_action: str) -> str:
    candidate_action = str(candidate_action or "").strip().lower()
    batch_action = str(batch_action or "").strip().lower()
    if candidate_action in _CANCEL_ACTIONS:
        return "verified_cancel"
    if candidate_action in _EXIT_ACTIONS or batch_action in _EXIT_ACTIONS:
        return "verified_exit"
    return "verified_management"


def _load_management_evidence(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    management_batch_id: int,
) -> _ManagementEvidence:
    with session_factory() as session:
        item = session.get(MessageInstructionItem, int(message_instruction_item_id))
        if (
            item is None
            or str(item.instruction_kind) != "management"
            or item.retired_at is not None
        ):
            raise ManagementExecutionContractBlocked("management_item_missing")
        candidate = session.get(SignalCandidate, int(item.signal_candidate_id))
        batch = session.get(StrategyManagementBatch, int(management_batch_id))
        if candidate is None or batch is None:
            raise ManagementExecutionContractBlocked("management_batch_missing")
        lifecycle = session.get(StrategyLifecycle, int(batch.target_lifecycle_id))
        binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
        decision = session.get(
            RecognitionDecision,
            int(batch.recognition_decision_id),
        )
        if (
            int(batch.raw_message_id) != int(item.raw_message_id)
            or int(batch.target_lifecycle_id)
            != int(candidate.target_lifecycle_id or 0)
            or lifecycle is None
            or int(lifecycle.execution_binding_id or 0)
            != int(batch.execution_binding_id)
            or binding is None
            or str(binding.strategy_instance_id or "")
            != str(batch.strategy_instance_id or "")
            or str(batch.strategy_instance_id or "")
            != str(item.strategy_instance_id or "")
            or str(candidate.management_action or "") != str(batch.intent or "")
            or str(candidate.recognition_generation or "")
            != str(batch.recognition_generation or "")
            or decision is None
            or int(decision.raw_message_id) != int(batch.raw_message_id)
        ):
            raise ManagementExecutionContractBlocked(
                "management_batch_target_mismatch"
            )
        legs = (
            session.query(StrategyManagementLeg)
            .filter(StrategyManagementLeg.management_batch_id == int(batch.id))
            .order_by(StrategyManagementLeg.id)
            .all()
        )
        components = (
            session.query(StrategyManagementComponent)
            .filter(
                StrategyManagementComponent.management_batch_id == int(batch.id)
            )
            .order_by(StrategyManagementComponent.id)
            .all()
        )
        entry_leg_ids = {int(leg.execution_order_leg_id) for leg in legs}
        mutations = []
        if entry_leg_ids:
            mutations = (
                session.query(PositionMutationIntent)
                .filter(
                    PositionMutationIntent.execution_binding_id
                    == int(batch.execution_binding_id),
                    PositionMutationIntent.execution_order_leg_id.in_(entry_leg_ids),
                    PositionMutationIntent.idempotency_key.like(
                        f"management:{int(batch.id)}:%"
                    ),
                )
                .order_by(PositionMutationIntent.id)
                .all()
            )
        candidate_events = (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.execution_binding_id
                == int(batch.execution_binding_id),
                ExecutionEvent.source_message_id == int(batch.raw_message_id),
            )
            .order_by(ExecutionEvent.id)
            .all()
        )
        events = [
            event
            for event in candidate_events
            if _event_matches_batch(event, batch_id=int(batch.id))
        ]

        leg_states = {str(leg.status or "").lower() for leg in legs}
        component_states = {
            str(component.status or "").lower() for component in components
        }
        mutation_states = {
            str(mutation.status or "").lower() for mutation in mutations
        }
        event_states = {str(event.status or "").lower() for event in events}
        current_artifact_states = {
            *leg_states,
            *component_states,
            *mutation_states,
        }
        artifact_states = {*current_artifact_states, *event_states}
        batch_status = str(batch.status or "").lower()
        attempted = bool(artifact_states.intersection(_WRITE_ARTIFACT_STATES))
        unknown = bool(
            current_artifact_states.intersection(_UNKNOWN_ARTIFACT_STATES)
            or (
                batch_status not in {"succeeded", "resolved"}
                and event_states.intersection(_UNKNOWN_ARTIFACT_STATES)
            )
        )
        refs: list[dict[str, object]] = [
            {
                "kind": "management_batch",
                "id": int(batch.id),
                "status": str(batch.status),
                "action": str(batch.effective_action),
            }
        ]
        refs.extend(
            {
                "kind": "management_leg",
                "id": int(leg.id),
                "status": str(leg.status),
                "pos_id": str(leg.pos_id),
            }
            for leg in legs[:12]
        )
        refs.extend(
            {
                "kind": "management_component",
                "id": int(component.id),
                "status": str(component.status),
                "component_kind": str(component.component_kind),
            }
            for component in components[:8]
        )
        refs.extend(
            {
                "kind": "position_mutation_intent",
                "id": int(mutation.id),
                "status": str(mutation.status),
            }
            for mutation in mutations[:8]
        )
        refs.extend(
            {
                "kind": "execution_event",
                "id": int(event.id),
                "status": str(event.status),
            }
            for event in events[:8]
        )

        reason = str(batch.reason_code or "") or f"management_batch_{batch_status}"
        if unknown or batch_status == "recovery_required":
            return _ManagementEvidence(
                "submit_unknown",
                reason,
                True,
                None,
                None,
                tuple(refs),
            )
        if batch_status in {"executing", "reconciling"} and attempted:
            return _ManagementEvidence(
                "submitting", reason, True, None, None, tuple(refs)
            )
        if batch_status in {"blocked", "shadow_planned"} and not attempted:
            return _ManagementEvidence(
                "verified",
                reason,
                False,
                "verified_refusal",
                "full",
                tuple(refs),
            )
        if batch_status in {"succeeded", "resolved"}:
            terminal_states = {
                *_SUCCESS_ARTIFACT_STATES,
                *_DEFINITE_FAILURE_STATES,
            }
            if current_artifact_states and not current_artifact_states.issubset(
                terminal_states
            ):
                raise ManagementExecutionContractBlocked(
                    "management_terminal_evidence_incomplete"
                )
            return _ManagementEvidence(
                "verified",
                reason,
                attempted,
                _terminal_kind(
                    candidate_action=str(candidate.management_action or ""),
                    batch_action=str(batch.effective_action or ""),
                ),
                "full",
                tuple(refs),
            )
        if batch_status == "partial_failed":
            terminal_states = {
                *_SUCCESS_ARTIFACT_STATES,
                *_DEFINITE_FAILURE_STATES,
            }
            if (
                current_artifact_states.intersection(_SUCCESS_ARTIFACT_STATES)
                and current_artifact_states.intersection(_DEFINITE_FAILURE_STATES)
                and current_artifact_states.issubset(terminal_states)
            ):
                refs.append(
                    {"kind": "incident_fact", "code": "management_partial"}
                )
                return _ManagementEvidence(
                    "verified",
                    reason,
                    attempted,
                    _terminal_kind(
                        candidate_action=str(candidate.management_action or ""),
                        batch_action=str(batch.effective_action or ""),
                    ),
                    "partial",
                    tuple(refs),
                )
        if batch_status in {"failed", "partial_failed"} and not attempted:
            return _ManagementEvidence(
                "failed", reason, False, None, None, tuple(refs)
            )
        if attempted:
            return _ManagementEvidence(
                "submit_unknown", reason, True, None, None, tuple(refs)
            )
        return _ManagementEvidence("pending", reason, False, None, None, tuple(refs))


def _reload_required_contract(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
) -> InstructionExecutionContract:
    contract = _load_contract(
        session_factory,
        message_instruction_item_id=message_instruction_item_id,
    )
    if contract is None:
        raise ManagementExecutionContractBlocked(
            "management_contract_missing_after_conflict"
        )
    return contract


def _transition_management_evidence(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    evidence: _ManagementEvidence,
    projected_at: datetime,
) -> InstructionExecutionContract:
    contract = load_or_create_instruction_execution_contract(
        session_factory,
        message_instruction_item_id=int(message_instruction_item_id),
        projected_at=projected_at,
    )
    for _attempt in range(8):
        if contract.intent_kind != "management":
            raise ManagementExecutionContractBlocked(
                "non_management_instruction_contract"
            )
        if contract.state in {"verified", "failed", "expired"}:
            if contract.state != evidence.target_state:
                raise ManagementExecutionContractBlocked(
                    "management_terminal_contract_contradiction"
                )
            if contract.state == "verified" and (
                contract.terminal_kind != evidence.terminal_kind
                or contract.completion_scope != evidence.completion_scope
            ):
                raise ManagementExecutionContractBlocked(
                    "management_terminal_kind_contradiction"
                )
            return contract
        if contract.state == "submit_unknown":
            if evidence.target_state == "submit_unknown":
                return contract
            if evidence.target_state not in {"verified", "failed"}:
                return contract
        if contract.state == "deferred":
            try:
                contract = transition_instruction_execution_contract(
                    session_factory,
                    contract_id=int(contract.id),
                    expected_state="deferred",
                    expected_version=int(contract.state_version),
                    new_state="pending",
                    reason_code="management_visibility_released",
                    evidence_refs=[{"kind": "management_visibility_recheck"}],
                    transitioned_at=projected_at,
                )
            except InstructionExecutionConflictError:
                contract = _reload_required_contract(
                    session_factory,
                    message_instruction_item_id=message_instruction_item_id,
                )
            continue
        if evidence.target_state == "pending" and not evidence.attempted_exchange_write:
            return contract
        if evidence.target_state == "failed":
            if contract.state == "submitting":
                evidence = _ManagementEvidence(
                    "submit_unknown",
                    "management_failure_after_write_unknown",
                    True,
                    None,
                    None,
                    evidence.refs,
                )
                continue
            try:
                return transition_instruction_execution_contract(
                    session_factory,
                    contract_id=int(contract.id),
                    expected_state=str(contract.state),
                    expected_version=int(contract.state_version),
                    new_state="failed",
                    reason_code=evidence.reason_code[:128],
                    evidence_refs=list(evidence.refs),
                    transitioned_at=projected_at,
                )
            except InstructionExecutionConflictError:
                contract = _reload_required_contract(
                    session_factory,
                    message_instruction_item_id=message_instruction_item_id,
                )
                continue
        if evidence.attempted_exchange_write and contract.state == "pending":
            try:
                contract = transition_instruction_execution_contract(
                    session_factory,
                    contract_id=int(contract.id),
                    expected_state="pending",
                    expected_version=int(contract.state_version),
                    new_state="submitting",
                    reason_code="management_writer_evidence_observed",
                    evidence_refs=list(evidence.refs),
                    transitioned_at=projected_at,
                    attempted_exchange_write=True,
                )
            except InstructionExecutionConflictError:
                contract = _reload_required_contract(
                    session_factory,
                    message_instruction_item_id=message_instruction_item_id,
                )
            continue
        if evidence.target_state == "submitting":
            return contract
        if evidence.target_state == "submit_unknown":
            if contract.state == "pending":
                evidence = _ManagementEvidence(
                    evidence.target_state,
                    evidence.reason_code,
                    True,
                    evidence.terminal_kind,
                    evidence.completion_scope,
                    evidence.refs,
                )
                continue
            try:
                return transition_instruction_execution_contract(
                    session_factory,
                    contract_id=int(contract.id),
                    expected_state="submitting",
                    expected_version=int(contract.state_version),
                    new_state="submit_unknown",
                    reason_code=evidence.reason_code[:128],
                    evidence_refs=list(evidence.refs),
                    transitioned_at=projected_at,
                )
            except InstructionExecutionConflictError:
                contract = _reload_required_contract(
                    session_factory,
                    message_instruction_item_id=message_instruction_item_id,
                )
                continue
        if evidence.target_state == "verified":
            try:
                return transition_instruction_execution_contract(
                    session_factory,
                    contract_id=int(contract.id),
                    expected_state=str(contract.state),
                    expected_version=int(contract.state_version),
                    new_state="verified",
                    reason_code=evidence.reason_code[:128],
                    evidence_refs=list(evidence.refs),
                    transitioned_at=projected_at,
                    terminal_kind=evidence.terminal_kind,
                    completion_scope=evidence.completion_scope,
                )
            except InstructionExecutionConflictError:
                contract = _reload_required_contract(
                    session_factory,
                    message_instruction_item_id=message_instruction_item_id,
                )
                continue
        return contract
    raise ManagementExecutionContractBlocked(
        "management_contract_projection_conflict"
    )


def project_management_instruction_contract(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    management_batch_id: int,
    projected_at: datetime,
    mode: str,
) -> InstructionExecutionContract | None:
    """Project one exact management batch without invoking planner or writer."""

    if not _enabled(mode):
        return None
    evidence = _load_management_evidence(
        session_factory,
        message_instruction_item_id=message_instruction_item_id,
        management_batch_id=management_batch_id,
    )
    return _transition_management_evidence(
        session_factory,
        message_instruction_item_id=message_instruction_item_id,
        evidence=evidence,
        projected_at=projected_at,
    )


def _load_revision_evidence(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    revision_batch_id: int,
) -> _ManagementEvidence:
    with session_factory() as session:
        item = session.get(MessageInstructionItem, int(message_instruction_item_id))
        if (
            item is None
            or str(item.instruction_kind) != "management"
            or item.retired_at is not None
        ):
            raise ManagementExecutionContractBlocked("revision_item_missing")
        candidate = session.get(SignalCandidate, int(item.signal_candidate_id))
        batch = session.get(StrategyRevisionBatch, int(revision_batch_id))
        if candidate is None or batch is None:
            raise ManagementExecutionContractBlocked("revision_batch_missing")
        lifecycle = session.get(StrategyLifecycle, int(batch.target_lifecycle_id))
        binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
        thread = session.get(StrategyThread, int(batch.strategy_thread_id))
        replacement = _json_mapping(batch.replacement_json)
        expected_replacement = {
            "entry": candidate.entry_text,
            "stop_loss": candidate.stop_loss_text,
            "take_profit": candidate.take_profit_text,
            "leverage": candidate.leverage_text,
        }
        if (
            str(candidate.event_type) != "strategy_revision"
            or str(batch.revision_kind) != "replacement"
            or int(batch.raw_message_id) != int(item.raw_message_id)
            or int(batch.target_lifecycle_id)
            != int(candidate.target_lifecycle_id or 0)
            or lifecycle is None
            or int(lifecycle.execution_binding_id or 0)
            != int(batch.execution_binding_id)
            or binding is None
            or str(binding.strategy_instance_id or "")
            != str(item.strategy_instance_id or "")
            or thread is None
            or int(thread.current_lifecycle_id or 0) != int(batch.target_lifecycle_id)
            or int(lifecycle.strategy_thread_id or 0) != int(batch.strategy_thread_id)
            or any(
                replacement.get(key) != value
                for key, value in expected_replacement.items()
            )
        ):
            raise ManagementExecutionContractBlocked("revision_batch_target_mismatch")
        legs = (
            session.query(StrategyRevisionLeg)
            .filter(StrategyRevisionLeg.revision_batch_id == int(batch.id))
            .order_by(StrategyRevisionLeg.id)
            .all()
        )
        replacements = (
            session.query(EntryRevisionReplacement)
            .filter(EntryRevisionReplacement.revision_batch_id == int(batch.id))
            .order_by(EntryRevisionReplacement.id)
            .all()
        )
        leg_states = {str(row.status or "").lower() for row in legs}
        replacement_states = {
            str(row.status or "").lower() for row in replacements
        }
        artifact_states = {*leg_states, *replacement_states}
        attempted = bool(
            leg_states.intersection({"cancel_submitting", "cancelled", "retained"})
            or replacement_states.intersection(
                {"submit_reserved", "submitted", "verified"}
            )
        )
        refs: list[dict[str, object]] = [
            {
                "kind": "strategy_revision_batch",
                "id": int(batch.id),
                "status": str(batch.status),
                "revision_kind": str(batch.revision_kind),
            }
        ]
        refs.extend(
            {
                "kind": "strategy_revision_leg",
                "id": int(row.id),
                "status": str(row.status),
                "action": str(row.action),
            }
            for row in legs[:12]
        )
        refs.extend(
            {
                "kind": "entry_revision_replacement",
                "id": int(row.id),
                "status": str(row.status),
                "leg_index": int(row.leg_index),
            }
            for row in replacements[:12]
        )
        batch_status = str(batch.status or "").lower()
        reason = str(batch.reason_code or "") or f"revision_batch_{batch_status}"
        if batch_status == "recovery_required" or artifact_states.intersection(
            _UNKNOWN_ARTIFACT_STATES
        ):
            return _ManagementEvidence(
                "submit_unknown", reason, True, None, None, tuple(refs)
            )
        if batch_status in {
            "cancelling_old_entries",
            "old_entries_terminal",
            "rebuilding",
            "reconciling",
        } and attempted:
            return _ManagementEvidence(
                "submitting", reason, True, None, None, tuple(refs)
            )
        if batch_status in {"blocked", "shadow_planned"} and not attempted:
            return _ManagementEvidence(
                "verified",
                reason,
                False,
                "verified_refusal",
                "full",
                tuple(refs),
            )
        if batch_status == "succeeded":
            terminal_states = {"cancelled", "retained", "verified"}
            if not artifact_states or not artifact_states.issubset(terminal_states):
                raise ManagementExecutionContractBlocked(
                    "revision_terminal_evidence_incomplete"
                )
            return _ManagementEvidence(
                "verified",
                reason,
                attempted,
                "verified_management",
                "full",
                tuple(refs),
            )
        if attempted:
            return _ManagementEvidence(
                "submit_unknown", reason, True, None, None, tuple(refs)
            )
        return _ManagementEvidence("pending", reason, False, None, None, tuple(refs))


def project_revision_instruction_contract(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    revision_batch_id: int,
    projected_at: datetime,
    mode: str,
) -> InstructionExecutionContract | None:
    """Project one exact entry-revision batch as a management contract."""

    if not _enabled(mode):
        return None
    evidence = _load_revision_evidence(
        session_factory,
        message_instruction_item_id=message_instruction_item_id,
        revision_batch_id=revision_batch_id,
    )
    return _transition_management_evidence(
        session_factory,
        message_instruction_item_id=message_instruction_item_id,
        evidence=evidence,
        projected_at=projected_at,
    )


def project_linked_management_batch_contract(
    session_factory: sessionmaker,
    *,
    management_batch_id: int,
    projected_at: datetime,
) -> LinkedManagementProjection | None:
    """Project a batch only when it has one exact future authoritative item."""

    from telegram_kol_research.instruction_execution_projection import (
        instruction_execution_mode_for_item,
    )
    from telegram_kol_research.trading_settings import load_trading_settings

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, int(management_batch_id))
        if batch is None:
            return None
        items = (
            session.query(MessageInstructionItem)
            .join(
                SignalCandidate,
                SignalCandidate.id == MessageInstructionItem.signal_candidate_id,
            )
            .filter(
                MessageInstructionItem.instruction_kind == "management",
                MessageInstructionItem.retired_at.is_(None),
                MessageInstructionItem.raw_message_id == int(batch.raw_message_id),
                MessageInstructionItem.strategy_instance_id
                == str(batch.strategy_instance_id),
                SignalCandidate.target_lifecycle_id
                == int(batch.target_lifecycle_id),
                SignalCandidate.management_action == str(batch.intent),
                SignalCandidate.recognition_generation
                == str(batch.recognition_generation),
                SignalCandidate.parse_source == "mimo_authoritative",
            )
            .order_by(MessageInstructionItem.id)
            .all()
        )
        if not items:
            return None
        if len(items) != 1:
            raise ManagementExecutionContractBlocked(
                "management_batch_instruction_collision"
            )
        item = items[0]
        session.expunge(item)
    mode = instruction_execution_mode_for_item(
        item,
        load_trading_settings(session_factory),
    )
    if mode == "disabled":
        return None
    contract = project_management_instruction_contract(
        session_factory,
        message_instruction_item_id=int(item.id),
        management_batch_id=int(management_batch_id),
        projected_at=projected_at,
        mode=mode,
    )
    if contract is None:
        return None
    return LinkedManagementProjection(int(item.id), mode, contract)


def project_linked_revision_batch_contract(
    session_factory: sessionmaker,
    *,
    revision_batch_id: int,
    projected_at: datetime,
) -> LinkedManagementProjection | None:
    """Project one exact revision batch without selecting its target."""

    from telegram_kol_research.instruction_execution_projection import (
        instruction_execution_mode_for_item,
    )
    from telegram_kol_research.trading_settings import load_trading_settings

    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(revision_batch_id))
        if batch is None:
            return None
        binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
        if binding is None or str(batch.revision_kind) != "replacement":
            return None
        replacement = _json_mapping(batch.replacement_json)
        items = (
            session.query(MessageInstructionItem)
            .join(
                SignalCandidate,
                SignalCandidate.id == MessageInstructionItem.signal_candidate_id,
            )
            .filter(
                MessageInstructionItem.instruction_kind == "management",
                MessageInstructionItem.retired_at.is_(None),
                MessageInstructionItem.raw_message_id == int(batch.raw_message_id),
                MessageInstructionItem.strategy_instance_id
                == str(binding.strategy_instance_id),
                SignalCandidate.target_lifecycle_id
                == int(batch.target_lifecycle_id),
                SignalCandidate.event_type == "strategy_revision",
                SignalCandidate.parse_source == "mimo_authoritative",
            )
            .order_by(MessageInstructionItem.id)
            .all()
        )
        items = [
            item
            for item in items
            if (
                (candidate := session.get(SignalCandidate, item.signal_candidate_id))
                is not None
                and replacement.get("entry") == candidate.entry_text
                and replacement.get("stop_loss") == candidate.stop_loss_text
                and replacement.get("take_profit") == candidate.take_profit_text
                and replacement.get("leverage") == candidate.leverage_text
            )
        ]
        if not items:
            return None
        if len(items) != 1:
            raise ManagementExecutionContractBlocked(
                "revision_batch_instruction_collision"
            )
        item = items[0]
        session.expunge(item)
    mode = instruction_execution_mode_for_item(
        item,
        load_trading_settings(session_factory),
    )
    if mode == "disabled":
        return None
    contract = project_revision_instruction_contract(
        session_factory,
        message_instruction_item_id=int(item.id),
        revision_batch_id=int(revision_batch_id),
        projected_at=projected_at,
        mode=mode,
    )
    if contract is None:
        return None
    return LinkedManagementProjection(int(item.id), mode, contract)


def converge_unknown_management_instruction_contracts(
    session_factory: sessionmaker,
    *,
    converged_at: datetime,
    limit: int = 20,
) -> int:
    """Converge bounded unknown mirrors from terminal readback; never write venue."""

    bounded_limit = max(0, int(limit))
    if bounded_limit == 0:
        return 0
    scan_budget = max(20, bounded_limit * 4)
    with session_factory() as session:
        cursor_row = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == _UNKNOWN_CONVERGENCE_CURSOR_KEY)
            .one_or_none()
        )
        try:
            cursor = int(json.loads(cursor_row.value_json)) if cursor_row else 0
        except (TypeError, ValueError, json.JSONDecodeError):
            cursor = 0
        base = (
            session.query(
                InstructionExecutionContract,
                MessageInstructionItem,
                SignalCandidate,
            )
            .join(
                MessageInstructionItem,
                InstructionExecutionContract.message_instruction_item_id
                == MessageInstructionItem.id,
            )
            .join(
                SignalCandidate,
                SignalCandidate.id == MessageInstructionItem.signal_candidate_id,
            )
            .filter(
                InstructionExecutionContract.state.in_(
                    ("submit_unknown", "verified", "failed")
                ),
                or_(
                    MessageInstructionItem.status == "unknown",
                    and_(
                        MessageInstructionItem.status == "executing",
                        MessageInstructionItem.updated_at
                        <= converged_at - timedelta(minutes=5),
                    ),
                ),
                MessageInstructionItem.instruction_kind == "management",
                MessageInstructionItem.retired_at.is_(None),
                SignalCandidate.parse_source == "mimo_authoritative",
            )
        )
        rows = (
            base.filter(InstructionExecutionContract.id > cursor)
            .order_by(InstructionExecutionContract.id)
            .limit(scan_budget)
            .all()
        )
        if len(rows) < scan_budget:
            rows.extend(
                base.filter(InstructionExecutionContract.id <= cursor)
                .order_by(InstructionExecutionContract.id)
                .limit(scan_budget - len(rows))
                .all()
            )
        candidates = [
            {
                "contract_id": int(contract.id),
                "contract_state": str(contract.state),
                "item_id": int(item.id),
                "item_status": str(item.status),
                "raw_message_id": int(item.raw_message_id),
                "strategy_instance_id": str(item.strategy_instance_id or ""),
                "event_type": str(candidate.event_type),
                "target_lifecycle_id": int(candidate.target_lifecycle_id or 0),
                "management_action": str(candidate.management_action or ""),
                "recognition_generation": str(
                    candidate.recognition_generation or ""
                ),
                "entry": candidate.entry_text,
                "stop_loss": candidate.stop_loss_text,
                "take_profit": candidate.take_profit_text,
                "leverage": candidate.leverage_text,
            }
            for contract, item, candidate in rows
        ]

    converged = 0
    last_inspected_contract_id = cursor
    from telegram_kol_research.instruction_execution_projection import (
        instruction_execution_mode_for_item,
    )
    from telegram_kol_research.message_instruction_items import (
        finish_message_instruction_item,
    )
    from telegram_kol_research.trading_settings import load_trading_settings

    settings = load_trading_settings(session_factory)
    for candidate in candidates:
        if converged >= bounded_limit:
            break
        last_inspected_contract_id = int(candidate["contract_id"])
        try:
            with session_factory() as session:
                item = session.get(MessageInstructionItem, int(candidate["item_id"]))
                if item is None:
                    continue
                session.expunge(item)
            mode = instruction_execution_mode_for_item(item, settings)
            if candidate["contract_state"] in {"verified", "failed"}:
                if mode != "live":
                    continue
                finish_message_instruction_item(
                    session_factory,
                    item_id=int(candidate["item_id"]),
                    status=str(candidate["item_status"]),
                    result={
                        "status": (
                            "succeeded"
                            if candidate["contract_state"] == "verified"
                            else "failed"
                        ),
                        "reason": "durable_management_mirror_recovered",
                        "contract_id": int(candidate["contract_id"]),
                    },
                    now=converged_at,
                    execution_contract_mode=mode,
                    expected_current_statuses=("executing", "unknown"),
                )
                converged += 1
                continue

            with session_factory() as session:
                if candidate["event_type"] == "strategy_revision":
                    revision_batches = (
                        session.query(StrategyRevisionBatch)
                        .filter(
                            StrategyRevisionBatch.raw_message_id
                            == int(candidate["raw_message_id"]),
                            StrategyRevisionBatch.target_lifecycle_id
                            == int(candidate["target_lifecycle_id"]),
                            StrategyRevisionBatch.status == "succeeded",
                            StrategyRevisionBatch.revision_kind == "replacement",
                        )
                        .order_by(StrategyRevisionBatch.id)
                        .all()
                    )
                    expected_replacement = {
                        key: candidate[key]
                        for key in ("entry", "stop_loss", "take_profit", "leverage")
                    }
                    batch_ids = tuple(
                        int(batch.id)
                        for batch in revision_batches
                        if (
                            (binding := session.get(
                                ExecutionBinding,
                                int(batch.execution_binding_id),
                            ))
                            is not None
                            and str(binding.strategy_instance_id or "")
                            == candidate["strategy_instance_id"]
                            and all(
                                _json_mapping(batch.replacement_json).get(key)
                                == value
                                for key, value in expected_replacement.items()
                            )
                        )
                    )
                    artifact_kind = "revision"
                else:
                    batch_ids = tuple(
                        int(value)
                        for (value,) in session.query(StrategyManagementBatch.id)
                        .filter(
                            StrategyManagementBatch.raw_message_id
                            == int(candidate["raw_message_id"]),
                            StrategyManagementBatch.target_lifecycle_id
                            == int(candidate["target_lifecycle_id"]),
                            StrategyManagementBatch.strategy_instance_id
                            == candidate["strategy_instance_id"],
                            StrategyManagementBatch.intent
                            == candidate["management_action"],
                            StrategyManagementBatch.recognition_generation
                            == candidate["recognition_generation"],
                            StrategyManagementBatch.status.in_(
                                ("succeeded", "resolved")
                            ),
                        )
                        .order_by(StrategyManagementBatch.id)
                        .all()
                    )
                    artifact_kind = "management"
            if len(batch_ids) != 1:
                continue
            batch_id = batch_ids[0]
            linked = (
                project_linked_revision_batch_contract(
                    session_factory,
                    revision_batch_id=batch_id,
                    projected_at=converged_at,
                )
                if artifact_kind == "revision"
                else project_linked_management_batch_contract(
                    session_factory,
                    management_batch_id=batch_id,
                    projected_at=converged_at,
                )
            )
            if (
                linked is None
                or linked.message_instruction_item_id != int(candidate["item_id"])
                or linked.contract.state != "verified"
                or linked.mode != "live"
            ):
                continue
            finish_message_instruction_item(
                session_factory,
                item_id=int(candidate["item_id"]),
                status=str(candidate["item_status"]),
                result={
                    "status": "succeeded",
                    "reason": "durable_management_readback_verified",
                    "contract_id": int(linked.contract.id),
                },
                now=converged_at,
                execution_contract_mode=linked.mode,
                expected_current_statuses=("executing", "unknown"),
            )
            converged += 1
        except Exception as exc:
            logger.warning(
                "management unknown convergence candidate failed: contract_id=%s error=%s",
                int(candidate["contract_id"]),
                type(exc).__name__,
            )
            continue
    if candidates:
        with session_factory() as session:
            cursor_row = (
                session.query(TradingSetting)
                .filter(TradingSetting.key == _UNKNOWN_CONVERGENCE_CURSOR_KEY)
                .one_or_none()
            )
            if cursor_row is None:
                cursor_row = TradingSetting(key=_UNKNOWN_CONVERGENCE_CURSOR_KEY)
                session.add(cursor_row)
            cursor_row.value_json = json.dumps(last_inspected_contract_id)
            cursor_row.updated_at = converged_at
            session.commit()
    return converged


def resolve_management_instruction_mirror(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    requested_status: str,
    mode: str,
) -> ManagementInstructionMirror:
    """Resolve the compatibility item status from durable management truth."""

    normalized_mode = str(mode).strip().lower()
    if normalized_mode not in {"disabled", "shadow", "live"}:
        raise ValueError("execution contract mode must be disabled, shadow, or live")
    requested = str(requested_status)
    if normalized_mode == "disabled":
        return ManagementInstructionMirror(requested, None, None, None, False, {})
    with session_factory() as session:
        item = session.get(MessageInstructionItem, int(message_instruction_item_id))
        if item is None:
            raise LookupError("message instruction item not found")
        if str(item.instruction_kind) != "management":
            return ManagementInstructionMirror(requested, None, None, None, False, {})
    contract = _load_contract(
        session_factory,
        message_instruction_item_id=message_instruction_item_id,
    )
    expected = "failed"
    contract_id = None
    state = None
    state_version = None
    terminal_kind = None
    completion_scope = None
    reason_code = "execution_contract_missing"
    if contract is not None:
        contract_id = int(contract.id)
        state = str(contract.state)
        state_version = int(contract.state_version)
        terminal_kind = contract.terminal_kind
        completion_scope = contract.completion_scope
        reason_code = str(contract.reason_code or "") or "execution_contract_pending"
        if state == "deferred":
            expected = "pending"
        elif state == "submitting":
            expected = "executing"
        elif state == "submit_unknown":
            expected = "unknown"
        elif state in {"failed", "expired"}:
            expected = "failed"
        elif (
            state == "verified"
            and contract.intent_kind == "management"
            and terminal_kind == "verified_refusal"
            and completion_scope == "full"
        ):
            expected = "succeeded"
        elif (
            state == "verified"
            and contract.intent_kind == "management"
            and terminal_kind
            in {"verified_management", "verified_cancel", "verified_exit"}
            and completion_scope in {"full", "partial"}
        ):
            expected = "submitted"
        elif state == "verified":
            reason_code = "management_terminal_contract_invalid"
        else:
            reason_code = "verified_terminal_contract_required"
    divergence = expected != requested
    evidence: dict[str, object] = {
        "contract_id": contract_id,
        "state": state or "missing",
        "state_version": state_version,
        "terminal_kind": terminal_kind,
        "completion_scope": completion_scope,
        "reason_code": reason_code[:128],
        "expected_item_status": expected,
        "observed_item_status": requested,
        "divergence": divergence,
    }
    return ManagementInstructionMirror(
        expected if normalized_mode == "live" else requested,
        contract_id,
        state,
        state_version,
        divergence,
        evidence,
    )
