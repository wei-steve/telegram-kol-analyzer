"""Durable, exchange-free persistence for trigger-protection recovery intents."""

from __future__ import annotations

from datetime import datetime
import json

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from telegram_kol_research.models import ExecutionOrderLeg, TriggerProtectionIntent, utc_now


ALLOWED_TRIGGER_PROTECTION_RECOVERY_STATES = frozenset(
    {"pending", "submitting", "retrying", "adopted", "failed", "resolved"}
)


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

    venue = _normalized_required_string(venue, label="venue")
    _validate_immutable_evidence(
        request_fingerprint=request_fingerprint,
        pre_submit_tpsl_baseline_json=pre_submit_tpsl_baseline_json,
        correlation_id=correlation_id,
    )
    intent = _intent_for_leg(session, venue=venue, execution_order_leg_id=execution_order_leg_id)
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
    if leg.purpose != "entry":
        raise ValueError("trigger protection requires an entry leg")
    if leg.venue != venue:
        raise ValueError("execution order leg venue differs from intent venue")
    intent = TriggerProtectionIntent(
        venue=venue,
        execution_binding_id=leg.execution_binding_id,
        execution_order_leg_id=execution_order_leg_id,
        request_fingerprint=request_fingerprint,
        pre_submit_tpsl_baseline_json=pre_submit_tpsl_baseline_json,
        correlation_id=correlation_id,
    )
    try:
        with session.begin_nested():
            session.add(intent)
            session.flush()
    except IntegrityError:
        intent = _intent_for_leg(
            session, venue=venue, execution_order_leg_id=execution_order_leg_id
        )
        if intent is None:
            raise
        _assert_matching_immutable_evidence(
            intent,
            request_fingerprint=request_fingerprint,
            pre_submit_tpsl_baseline_json=pre_submit_tpsl_baseline_json,
            correlation_id=correlation_id,
        )
    return intent


def record_trigger_protection_parent(
    session: Session,
    intent: TriggerProtectionIntent,
    *,
    parent_trigger_order_id: str,
) -> TriggerProtectionIntent:
    """Record the parent trigger order once, refusing conflicting ownership."""

    parent_trigger_order_id = _normalized_identity(parent_trigger_order_id)
    if intent.parent_trigger_order_id == parent_trigger_order_id:
        return intent
    if intent.parent_trigger_order_id is not None:
        raise ValueError("parent trigger order ID is immutable")
    _claim_identity(
        session,
        intent,
        field_name="parent_trigger_order_id",
        value=parent_trigger_order_id,
        label="parent trigger order",
    )
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

    if recovery_state not in ALLOWED_TRIGGER_PROTECTION_RECOVERY_STATES:
        raise ValueError("unknown trigger-protection recovery state")
    if retry_attempts is not None and retry_attempts < 0:
        raise ValueError("retry attempts must be nonnegative")
    adopted_changed = False
    if adopted_order_id is not None:
        adopted_order_id = _normalized_identity(adopted_order_id)
        if intent.adopted_order_id not in (None, adopted_order_id):
            raise ValueError("adopted order ID is immutable")
        if intent.adopted_order_id is None:
            _claim_identity(
                session,
                intent,
                field_name="adopted_order_id",
                value=adopted_order_id,
                label="adopted order",
            )
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


def _intent_for_leg(
    session: Session, *, venue: str, execution_order_leg_id: int
) -> TriggerProtectionIntent | None:
    return (
        session.query(TriggerProtectionIntent)
        .filter(TriggerProtectionIntent.venue == venue)
        .filter(TriggerProtectionIntent.execution_order_leg_id == execution_order_leg_id)
        .one_or_none()
    )


def _assert_matching_immutable_evidence(
    intent: TriggerProtectionIntent,
    *,
    request_fingerprint: str,
    pre_submit_tpsl_baseline_json: str,
    correlation_id: str,
) -> None:
    if (
        intent.request_fingerprint != request_fingerprint
        or intent.pre_submit_tpsl_baseline_json != pre_submit_tpsl_baseline_json
    ):
        raise ValueError("immutable trigger-protection evidence differs")
    if intent.correlation_id != correlation_id:
        raise ValueError("immutable trigger-protection correlation differs")


def _claim_identity(
    session: Session,
    intent: TriggerProtectionIntent,
    *,
    field_name: str,
    value: str,
    label: str,
) -> None:
    field = getattr(TriggerProtectionIntent, field_name)
    try:
        with session.begin_nested():
            result = session.execute(
                update(TriggerProtectionIntent)
                .where(TriggerProtectionIntent.id == intent.id)
                .where(field.is_(None))
                .values({field_name: value, "updated_at": utc_now()})
            )
            if result.rowcount != 1:
                raise ValueError(f"{label} is immutable")
    except IntegrityError:
        owner = (
            session.query(TriggerProtectionIntent)
            .filter(TriggerProtectionIntent.venue == intent.venue)
            .filter(field == value)
            .filter(TriggerProtectionIntent.id != intent.id)
            .one_or_none()
        )
        if owner is not None:
            raise ValueError(f"{label} is already owned for this venue")
        raise
    session.refresh(intent)


def _normalized_identity(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("parent trigger order ID must be nonempty")
    return value.strip()


def _validate_immutable_evidence(
    *,
    request_fingerprint: str,
    pre_submit_tpsl_baseline_json: str,
    correlation_id: str,
) -> None:
    if not (
        isinstance(request_fingerprint, str)
        and len(request_fingerprint) == 64
        and request_fingerprint == request_fingerprint.lower()
        and all(character in "0123456789abcdef" for character in request_fingerprint)
    ):
        raise ValueError("immutable evidence requires a normalized request fingerprint")
    if not isinstance(pre_submit_tpsl_baseline_json, str) or not pre_submit_tpsl_baseline_json:
        raise ValueError("immutable evidence requires a nonempty TPSL baseline")
    try:
        parsed_baseline = json.loads(pre_submit_tpsl_baseline_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("immutable evidence requires normalized TPSL baseline JSON") from exc
    normalized_baseline = json.dumps(
        parsed_baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if (
        not isinstance(parsed_baseline, (dict, list))
        or pre_submit_tpsl_baseline_json != normalized_baseline
    ):
        raise ValueError("immutable evidence requires normalized TPSL baseline JSON")
    _normalized_required_string(correlation_id, label="correlation ID")


def _normalized_required_string(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"immutable evidence requires a normalized {label}")
    return value.lower() if label == "venue" else value
