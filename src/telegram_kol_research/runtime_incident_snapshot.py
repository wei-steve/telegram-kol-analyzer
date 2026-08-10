"""Immutable bounded snapshot container for proactive invariant rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Mapping, Any

from sqlalchemy import and_, case, exists, func, or_

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    InstructionExecutionContract,
    ManagementMessageTarget,
    MessageInstructionItem,
    PositionAttributionAudit,
    RuntimeIncident,
    RuntimeIncidentObservation,
    StrategyLifecycle,
)
from telegram_kol_research.trading_settings import load_trading_settings


INSTRUCTION_EXECUTION_CONTRADICTION_CODES = frozenset(
    {
        "deferred_overdue",
        "submitting_stale",
        "submit_unknown",
        "verified_without_binding",
        "binding_without_verified_contract",
        "contract_binding_mismatch",
        "multi_leg_partial",
        "terminal_contract_with_live_exchange_evidence",
        "exchange_snapshot_incomplete",
        "exchange_evidence_duplicate",
    }
)
_EXACT_HISTORICAL_EXECUTION_CODES = frozenset(
    {
        "submit_unknown",
        "verified_without_binding",
        "binding_without_verified_contract",
        "contract_binding_mismatch",
        "multi_leg_partial",
        "terminal_contract_with_live_exchange_evidence",
        "exchange_snapshot_incomplete",
        "exchange_evidence_duplicate",
    }
)
_SUBMITTING_STALE_AFTER = timedelta(minutes=2)


MANAGEMENT_TARGET_DIAGNOSIS_INCIDENT_TYPES = frozenset(
    {
        "management_target_refused",
        "management_target_orchestration_failed",
        "management_target_visibility_exhausted",
        "management_target_drift",
        "management_target_collision",
    }
)


@dataclass(frozen=True, slots=True)
class InvariantSnapshot:
    observed_at: datetime
    complete: bool
    facts_by_rule: Mapping[str, tuple[Mapping[str, Any], ...]]

    def __post_init__(self) -> None:
        if len(self.facts_by_rule) > 16:
            raise ValueError("snapshot rule set is unbounded")
        bounded: dict[str, tuple[Mapping[str, Any], ...]] = {}
        for rule_id, items in self.facts_by_rule.items():
            if len(items) > 100:
                raise ValueError("snapshot objects are unbounded")
            bounded[rule_id] = tuple(MappingProxyType(dict(item)) for item in items)
        object.__setattr__(self, "facts_by_rule", MappingProxyType(bounded))


def build_instruction_execution_contradiction_snapshot(
    session_factory,
    *,
    observed_at: datetime,
    limit: int = 100,
) -> dict[str, Any]:
    """Project bounded execution contradictions without raw or exchange IDs."""

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("instruction execution contradiction limit must be 1..100")
    now = _aware_utc(observed_at)
    settings = load_trading_settings(session_factory)
    mode = settings.instruction_execution_contract_mode
    scan_budget = min(400, max(limit * 4, limit))
    with session_factory() as session:
        unresolved = (
            session.query(RuntimeIncidentObservation.object_id)
            .filter(
                RuntimeIncidentObservation.rule_id
                == "instruction_execution_contradiction_v1",
                RuntimeIncidentObservation.state.in_(("observing", "shadow_confirmed")),
            )
            .order_by(RuntimeIncidentObservation.last_observed_at.asc())
            .limit(100)
            .all()
        )
        prior_contract_ids = []
        for (object_id,) in unresolved:
            try:
                prior_contract_ids.append(int(str(object_id).split("-", 1)[0]))
            except (TypeError, ValueError):
                continue
        comparable_now = now.replace(tzinfo=None)
        live_binding_predicate = and_(
            ExecutionBinding.venue == "deepcoin",
            ExecutionBinding.status.in_(("open", "active")),
        )
        any_strategy_binding = exists().where(
            and_(
                ExecutionBinding.venue == "deepcoin",
                ExecutionBinding.strategy_instance_id
                == InstructionExecutionContract.strategy_instance_id,
            )
        )
        live_strategy_binding = exists().where(
            and_(
                live_binding_predicate,
                ExecutionBinding.strategy_instance_id
                == InstructionExecutionContract.strategy_instance_id,
            )
        )
        live_exact_binding = exists().where(
            and_(
                live_binding_predicate,
                ExecutionBinding.id
                == InstructionExecutionContract.execution_binding_id,
            )
        )
        candidate_filter = or_(
            InstructionExecutionContract.state == "submit_unknown",
            and_(
                InstructionExecutionContract.state == "verified",
                InstructionExecutionContract.completion_scope == "partial",
            ),
            and_(
                InstructionExecutionContract.state == "verified",
                InstructionExecutionContract.terminal_kind == "verified_entry",
                InstructionExecutionContract.execution_binding_id.is_(None),
                ~any_strategy_binding,
            ),
            and_(
                InstructionExecutionContract.state == "deferred",
                InstructionExecutionContract.deadline_at.is_not(None),
                InstructionExecutionContract.deadline_at <= comparable_now,
            ),
            and_(
                InstructionExecutionContract.state == "submitting",
                func.coalesce(
                    InstructionExecutionContract.last_progress_at,
                    InstructionExecutionContract.updated_at,
                    InstructionExecutionContract.created_at,
                )
                <= comparable_now - _SUBMITTING_STALE_AFTER,
            ),
            and_(
                InstructionExecutionContract.state.in_(
                    ("pending", "deferred", "failed", "expired")
                ),
                or_(live_exact_binding, live_strategy_binding),
            ),
        )
        query = session.query(InstructionExecutionContract).filter(candidate_filter)
        candidate_count = query.count()
        contracts = (
            query.order_by(
                case(
                    (
                        InstructionExecutionContract.id.in_(prior_contract_ids),
                        0,
                    ),
                    else_=1,
                ),
                InstructionExecutionContract.id.desc(),
            )
            .limit(scan_budget + 1)
            .all()
        )
        scanned_truncated = candidate_count > scan_budget
        contracts = contracts[:scan_budget]
        binding_ids = {
            int(row.execution_binding_id)
            for row in contracts
            if row.execution_binding_id is not None
        }
        strategy_ids = {
            str(row.strategy_instance_id)
            for row in contracts
            if row.strategy_instance_id
        }
        bindings = (
            session.query(ExecutionBinding)
            .filter(
                ExecutionBinding.venue == "deepcoin",
                (ExecutionBinding.id.in_(binding_ids))
                | (ExecutionBinding.strategy_instance_id.in_(strategy_ids))
            )
            .all()
            if binding_ids or strategy_ids
            else []
        )

    bindings_by_id = {int(row.id): row for row in bindings}
    bindings_by_strategy: dict[str, list[ExecutionBinding]] = {}
    for binding in bindings:
        if binding.strategy_instance_id:
            bindings_by_strategy.setdefault(
                str(binding.strategy_instance_id), []
            ).append(binding)

    facts: list[dict[str, Any]] = []
    for contract in contracts:
        binding = (
            bindings_by_id.get(int(contract.execution_binding_id))
            if contract.execution_binding_id is not None
            else None
        )
        strategy_matches = bindings_by_strategy.get(
            str(contract.strategy_instance_id or ""), []
        )
        if binding is None and len(strategy_matches) == 1:
            binding = strategy_matches[0]
        future_contract = _execution_contract_is_future(
            contract,
            mode=mode,
            entry_watermark=settings.instruction_execution_entry_after_item_id,
            management_watermark=(
                settings.instruction_execution_management_after_item_id
            ),
        )
        codes: list[str] = []
        state = str(contract.state or "").strip().lower()
        if state == "submit_unknown":
            codes.append("submit_unknown")
        if (
            state == "verified"
            and str(contract.terminal_kind or "").strip().lower() == "verified_entry"
            and binding is None
        ):
            codes.append("verified_without_binding")
        binding_live = bool(
            binding is not None
            and str(binding.venue or "").lower() == "deepcoin"
            and str(binding.status or "").lower() in {"open", "active"}
        )
        if binding_live and state != "verified":
            codes.append(
                "terminal_contract_with_live_exchange_evidence"
                if state in {"failed", "expired"}
                else "binding_without_verified_contract"
            )
        if (
            contract.execution_binding_id is not None
            and (
                binding is None
                or int(binding.id) != int(contract.execution_binding_id)
            )
        ):
            codes.append("contract_binding_mismatch")
        if state == "verified" and contract.completion_scope == "partial":
            codes.append("multi_leg_partial")
        if state == "deferred" and contract.deadline_at is not None and now >= _aware_utc(contract.deadline_at):
            codes.append("deferred_overdue")
        if state == "submitting" and _contract_progress_age(contract, now=now) >= _SUBMITTING_STALE_AFTER:
            codes.append("submitting_stale")

        for code in dict.fromkeys(codes):
            exact_historical = code in _EXACT_HISTORICAL_EXECUTION_CODES or (
                code == "submitting_stale"
                and bool(contract.attempted_exchange_write)
            )
            if not future_contract and not exact_historical:
                continue
            facts.append(
                {
                    "reason_code": code,
                    "contract_id": int(contract.id),
                    "message_instruction_item_id": int(
                        contract.message_instruction_item_id
                    ),
                    "raw_message_id": int(contract.raw_message_id),
                    "future_contract": future_contract,
                    "exact_historical": exact_historical,
                }
            )
            if len(facts) > limit:
                break
        if len(facts) > limit:
            break

    facts_truncated = len(facts) > limit
    bounded_facts = tuple(facts[:limit])
    return {
        "scan_truncated": bool(scanned_truncated or facts_truncated),
        "contradictions_total": len(bounded_facts),
        "facts": bounded_facts,
    }


def _execution_contract_is_future(
    contract,
    *,
    mode: str,
    entry_watermark: int,
    management_watermark: int,
) -> bool:
    if mode not in {"shadow", "live"}:
        return False
    intent = str(contract.intent_kind or "").strip().lower()
    watermark = entry_watermark if "entry" in intent else management_watermark
    return int(contract.message_instruction_item_id) > int(watermark)


def _contract_progress_age(contract, *, now: datetime) -> timedelta:
    progress = contract.last_progress_at or contract.updated_at or contract.created_at
    return now - _aware_utc(progress)


def _aware_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def resolve_management_target_incident_snapshot(
    session_factory,
    *,
    incident_id: int,
) -> dict[str, Any]:
    """Resolve one bounded, durable, read-only target diagnosis snapshot."""

    with session_factory() as session:
        incident = session.get(RuntimeIncident, int(incident_id))
        if incident is None:
            raise ValueError("runtime incident does not exist")
        if (
            incident.source_kind != "management_message_target"
            or incident.incident_type
            not in MANAGEMENT_TARGET_DIAGNOSIS_INCIDENT_TYPES
        ):
            return {
                "data": {
                    "incident_id": incident.id,
                    "applicable": False,
                },
                "evidence_refs": [f"incident:{incident.id}"],
            }
        try:
            target_id = int(incident.source_record_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("management target reference is invalid") from exc
        target = session.get(ManagementMessageTarget, target_id)
        if target is None:
            raise ValueError("management target does not exist")
        lifecycle = session.get(StrategyLifecycle, target.target_lifecycle_id)
        binding = (
            session.get(ExecutionBinding, lifecycle.execution_binding_id)
            if lifecycle is not None
            and lifecycle.execution_binding_id is not None
            else None
        )
        item = (
            session.get(MessageInstructionItem, target.message_instruction_item_id)
            if target.message_instruction_item_id is not None
            else None
        )
        legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == binding.id)
            .order_by(ExecutionOrderLeg.leg_index, ExecutionOrderLeg.id)
            .limit(20)
            .all()
            if binding is not None
            else []
        )
        audits = (
            session.query(PositionAttributionAudit)
            .filter(PositionAttributionAudit.execution_binding_id == binding.id)
            .order_by(
                PositionAttributionAudit.created_at.desc(),
                PositionAttributionAudit.id.desc(),
            )
            .limit(10)
            .all()
            if binding is not None
            else []
        )
        references = [
            f"incident:{incident.id}",
            f"management-target:{target.id}",
            f"lifecycle:{target.target_lifecycle_id}",
        ]
        if binding is not None:
            references.append(f"binding:{binding.id}")
        if item is not None:
            references.append(f"instruction-item:{item.id}")
        references.extend(f"exchange-leg:{leg.id}" for leg in legs)
        references.extend(f"attribution-audit:{audit.id}" for audit in audits)
        return {
            "data": {
                "incident_id": incident.id,
                "applicable": True,
                "target": {
                    "id": target.id,
                    "envelope_id": target.envelope_id,
                    "lifecycle_id": target.target_lifecycle_id,
                    "instruction_item_id": target.message_instruction_item_id,
                    "action": target.normalized_action,
                    "admission_state": target.admission_state,
                    "execution_state": target.execution_state,
                    "reason_code": target.closed_reason_code,
                    "admitted_at": _isoformat(target.admitted_at),
                    "terminal_at": _isoformat(target.terminal_at),
                },
                "lifecycle": (
                    {
                        "id": lifecycle.id,
                        "status": lifecycle.lifecycle_status,
                        "binding_id": lifecycle.execution_binding_id,
                        "exit_reason": lifecycle.exit_reason,
                    }
                    if lifecycle is not None
                    else None
                ),
                "binding": (
                    {
                        "id": binding.id,
                        "status": binding.status,
                        "venue": binding.venue,
                        "last_exchange_status": binding.last_exchange_status,
                    }
                    if binding is not None
                    else None
                ),
                "instruction_item": (
                    {
                        "id": item.id,
                        "status": item.status,
                        "execution_deadline_at": _isoformat(
                            item.execution_deadline_at
                        ),
                        "escalation_state": item.escalation_state,
                    }
                    if item is not None
                    else None
                ),
                "exchange_legs": [
                    {
                        "id": leg.id,
                        "purpose": leg.purpose,
                        "leg_index": leg.leg_index,
                        "status": leg.status,
                        "attribution_status": leg.attribution_status,
                    }
                    for leg in legs
                ],
                "attribution_audits": [
                    {
                        "id": audit.id,
                        "event_type": audit.event_type,
                        "prior_state": audit.prior_state,
                        "new_state": audit.new_state,
                    }
                    for audit in audits
                ],
            },
            "evidence_refs": references[:32],
        }
