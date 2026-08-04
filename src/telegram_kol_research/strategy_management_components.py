"""Compare-and-set persistence for durable management components."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session

from telegram_kol_research.models import StrategyManagementComponent


TERMINAL_COMPONENT_STATUSES = frozenset(
    {"confirmed", "operator_required", "safely_skipped"}
)
PROTECTED_RECONCILIATION_STATUSES = frozenset(
    {"awaiting_exchange", *TERMINAL_COMPONENT_STATUSES}
)
ALLOWED_COMPONENT_TRANSITIONS = {
    "pending": frozenset({"preflighting", "operator_required"}),
    "preflighting": frozenset(
        {"submitting", "definitely_rejected", "recovery_required", "operator_required"}
    ),
    "submitting": frozenset(
        {"awaiting_exchange", "confirmed", "definitely_rejected", "recovery_required"}
    ),
    "awaiting_exchange": frozenset(
        {"confirmed", "definitely_rejected", "recovery_required", "operator_required"}
    ),
    "definitely_rejected": frozenset({"preflighting", "operator_required"}),
    "recovery_required": frozenset(
        {"preflighting", "awaiting_exchange", "confirmed", "operator_required"}
    ),
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def create_management_component(
    session: Session,
    *,
    management_batch_id: int,
    strategy_management_leg_id: int | None,
    component_kind: str,
    sequence: int,
    idempotency_key: str,
    desired: dict[str, Any],
    now: datetime,
    execution_deadline_at: datetime | None = None,
) -> StrategyManagementComponent:
    existing = (
        session.query(StrategyManagementComponent)
        .filter(
            StrategyManagementComponent.management_batch_id
            == int(management_batch_id),
            StrategyManagementComponent.strategy_management_leg_id
            == strategy_management_leg_id,
            StrategyManagementComponent.component_kind == component_kind,
        )
        .one_or_none()
    )
    desired_json = _canonical_json(desired)
    if existing is not None:
        identity = (
            existing.sequence,
            existing.idempotency_key,
            existing.desired_json,
        )
        if identity != (int(sequence), str(idempotency_key), desired_json):
            raise ValueError("management component identity is immutable")
        return existing
    component = StrategyManagementComponent(
        management_batch_id=int(management_batch_id),
        strategy_management_leg_id=strategy_management_leg_id,
        component_kind=str(component_kind),
        sequence=int(sequence),
        status="pending",
        idempotency_key=str(idempotency_key),
        desired_json=desired_json,
        evidence_json="[]",
        attempt_count=0,
        last_progress_at=now,
        execution_deadline_at=execution_deadline_at,
        created_at=now,
        updated_at=now,
    )
    session.add(component)
    session.flush()
    return component


def transition_management_component(
    session: Session,
    *,
    component_id: int,
    expected_status: str,
    new_status: str,
    now: datetime,
    reason_code: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> bool:
    if new_status not in ALLOWED_COMPONENT_TRANSITIONS.get(expected_status, frozenset()):
        raise ValueError(
            f"invalid component transition: {expected_status} -> {new_status}"
        )
    values: dict[str, Any] = {
        "status": new_status,
        "reason_code": reason_code,
        "last_progress_at": now,
        "updated_at": now,
    }
    if new_status in TERMINAL_COMPONENT_STATUSES:
        values["completed_at"] = now
    if evidence is not None:
        component = session.get(StrategyManagementComponent, int(component_id))
        if component is None or component.status != expected_status:
            return False
        try:
            history = json.loads(component.evidence_json or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("component evidence history is corrupt") from exc
        if not isinstance(history, list):
            raise ValueError("component evidence history is corrupt")
        history.append(evidence)
        values["evidence_json"] = _canonical_json(history)
    result = session.execute(
        update(StrategyManagementComponent)
        .where(
            StrategyManagementComponent.id == int(component_id),
            StrategyManagementComponent.status == expected_status,
        )
        .values(**values)
    )
    session.flush()
    return result.rowcount == 1


def claim_management_component(
    session: Session,
    *,
    component_id: int,
    now: datetime,
    stale_before: datetime,
) -> bool:
    """Claim work that is safe to submit, never an exchange-unknown write."""

    result = session.execute(
        update(StrategyManagementComponent)
        .where(
            StrategyManagementComponent.id == int(component_id),
            or_(
                StrategyManagementComponent.status.in_(
                    ("pending", "recovery_required")
                ),
                and_(
                    StrategyManagementComponent.status.in_(
                        ("preflighting", "submitting")
                    ),
                    StrategyManagementComponent.updated_at <= stale_before,
                ),
            ),
        )
        .values(
            status="preflighting",
            attempt_count=StrategyManagementComponent.attempt_count + 1,
            last_progress_at=now,
            updated_at=now,
        )
    )
    session.flush()
    return result.rowcount == 1
