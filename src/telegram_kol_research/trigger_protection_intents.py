"""Durable, exchange-free persistence for trigger-protection recovery intents."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from telegram_kol_research.models import ExecutionOrderLeg, TriggerProtectionIntent, utc_now


def create_or_get_trigger_protection_intent(
    session: Session,
    *,
    venue: str,
    execution_order_leg_id: int,
    request_fingerprint: str,
    pre_submit_tpsl_baseline_json: str,
    correlation_id: str,
) -> TriggerProtectionIntent:
    """Create one intent per venue/entry leg, preserving its submit evidence."""

    intent = (
        session.query(TriggerProtectionIntent)
        .filter(TriggerProtectionIntent.venue == venue)
        .filter(TriggerProtectionIntent.execution_order_leg_id == execution_order_leg_id)
        .one_or_none()
    )
    if intent is not None:
        if (
            intent.request_fingerprint != request_fingerprint
            or intent.pre_submit_tpsl_baseline_json != pre_submit_tpsl_baseline_json
        ):
            raise ValueError("immutable trigger-protection evidence differs")
        if intent.correlation_id != correlation_id:
            raise ValueError("immutable trigger-protection correlation differs")
        return intent

    leg = session.get(ExecutionOrderLeg, execution_order_leg_id)
    if leg is None:
        raise ValueError("execution order leg does not exist")
    intent = TriggerProtectionIntent(
        venue=venue,
        execution_binding_id=leg.execution_binding_id,
        execution_order_leg_id=execution_order_leg_id,
        request_fingerprint=request_fingerprint,
        pre_submit_tpsl_baseline_json=pre_submit_tpsl_baseline_json,
        correlation_id=correlation_id,
    )
    session.add(intent)
    session.flush()
    return intent


def record_trigger_protection_parent(
    session: Session,
    intent: TriggerProtectionIntent,
    *,
    parent_trigger_order_id: str,
) -> TriggerProtectionIntent:
    """Record the parent trigger order once, refusing conflicting ownership."""

    if not parent_trigger_order_id:
        raise ValueError("parent trigger order ID must be nonempty")
    if intent.parent_trigger_order_id == parent_trigger_order_id:
        return intent
    if intent.parent_trigger_order_id is not None:
        raise ValueError("parent trigger order ID is immutable")
    _refuse_existing_identity(
        session,
        intent,
        field_name="parent_trigger_order_id",
        value=parent_trigger_order_id,
        label="parent trigger order",
    )
    intent.parent_trigger_order_id = parent_trigger_order_id
    intent.updated_at = utc_now()
    session.flush()
    return intent


def transition_trigger_protection_intent(
    session: Session,
    intent: TriggerProtectionIntent,
    *,
    recovery_state: str,
    retry_attempts: int | None = None,
    next_attempt_at: datetime | None = None,
    adopted_order_id: str | None = None,
) -> TriggerProtectionIntent:
    """Apply an idempotent recovery-state update and optionally adopt one order."""

    adopted_changed = False
    if adopted_order_id is not None:
        if not adopted_order_id:
            raise ValueError("adopted order ID must be nonempty")
        if intent.adopted_order_id not in (None, adopted_order_id):
            raise ValueError("adopted order ID is immutable")
        if intent.adopted_order_id is None:
            _refuse_existing_identity(
                session,
                intent,
                field_name="adopted_order_id",
                value=adopted_order_id,
                label="adopted order",
            )
            intent.adopted_order_id = adopted_order_id
            adopted_changed = True

    changed = intent.recovery_state != recovery_state
    intent.recovery_state = recovery_state
    if retry_attempts is not None:
        changed = changed or intent.retry_attempts != retry_attempts
        intent.retry_attempts = retry_attempts
    if next_attempt_at is not None:
        changed = changed or intent.next_attempt_at != next_attempt_at
        intent.next_attempt_at = next_attempt_at
    if changed or adopted_changed:
        intent.updated_at = utc_now()
        session.flush()
    return intent


def _refuse_existing_identity(
    session: Session,
    intent: TriggerProtectionIntent,
    *,
    field_name: str,
    value: str,
    label: str,
) -> None:
    field = getattr(TriggerProtectionIntent, field_name)
    existing = (
        session.query(TriggerProtectionIntent)
        .filter(TriggerProtectionIntent.venue == intent.venue)
        .filter(field == value)
        .filter(TriggerProtectionIntent.id != intent.id)
        .first()
    )
    if existing is not None:
        raise ValueError(f"{label} is already owned for this venue")
