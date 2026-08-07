"""Immutable bounded snapshot container for proactive invariant rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, Any

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    ManagementMessageTarget,
    MessageInstructionItem,
    PositionAttributionAudit,
    RuntimeIncident,
    StrategyLifecycle,
)


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
