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
        {
            "awaiting_exchange", "confirmed", "definitely_rejected",
            "recovery_required", "operator_required",
        }
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
        strategy_management_leg_scope=(
            int(strategy_management_leg_id)
            if strategy_management_leg_id is not None
            else -1
        ),
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


def transition_component_for_exact_position_absent_recovery(
    session: Session,
    *,
    component_id: int,
    expected_status: str,
    recovery_evidence_fingerprint: str,
    now: datetime,
) -> bool:
    """Safely skip only a batch-119 component after exact-position absence."""

    if expected_status not in {"pending", "recovery_required"}:
        raise ValueError("invalid exact-position-absent recovery status")
    fingerprint = str(recovery_evidence_fingerprint)
    if len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint
    ):
        raise ValueError("invalid recovery evidence fingerprint")
    component = session.get(StrategyManagementComponent, int(component_id))
    if (
        component is None
        or int(component.management_batch_id) != 119
        or str(component.status) != expected_status
    ):
        return False
    try:
        history = json.loads(component.evidence_json or "[]")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("component evidence history is corrupt") from exc
    if not isinstance(history, list) or len(history) > 1:
        raise ValueError("component evidence history is corrupt")
    if history:
        fact = history[0]
        error_type = fact.get("error_type") if isinstance(fact, dict) else None
        if (
            not isinstance(fact, dict)
            or set(fact) != {"error_type"}
            or not isinstance(error_type, str)
            or not 0 < len(error_type) <= 64
            or not error_type.replace("_", "").replace(".", "").isalnum()
        ):
            raise ValueError("component evidence history is corrupt")
    history.append(
        {
            "kind": "composite_recovery_exact_position_absent",
            "recovery_evidence_fingerprint": fingerprint,
        }
    )
    result = session.execute(
        update(StrategyManagementComponent)
        .where(
            StrategyManagementComponent.id == int(component_id),
            StrategyManagementComponent.management_batch_id == 119,
            StrategyManagementComponent.status == expected_status,
        )
        .values(
            status="safely_skipped",
            reason_code="composite_recovery_exact_position_absent",
            evidence_json=_canonical_json(history),
            last_progress_at=now,
            updated_at=now,
            completed_at=now,
        )
    )
    session.flush()
    if result.rowcount == 1:
        session.refresh(component)
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
                        ("preflighting",)
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
