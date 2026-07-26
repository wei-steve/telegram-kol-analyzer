"""Durable compare-and-set records for exact position writes."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import PositionMutationIntent


POSITION_MUTATION_INTENT_STATUSES = frozenset(
    {
        "reserved",
        "submitting",
        "submitted",
        "confirmed",
        "rejected",
        "recovery_required",
        "blocked",
    }
)


class PositionMutationIntentError(RuntimeError):
    """Raised when a durable mutation reservation conflicts."""


def reserve_position_mutation_intent(
    session_factory: sessionmaker,
    *,
    idempotency_key: str,
    operation: str,
    strategy_instance_id: str,
    execution_binding_id: int,
    execution_order_leg_id: int,
    pos_id: str,
    order_id: str | None,
    authority_fingerprint: str,
    request_fingerprint: str,
    request: Mapping[str, Any],
    reserved_at: datetime,
    venue: str = "deepcoin",
) -> PositionMutationIntent:
    """Reserve one write or return the identical existing reservation."""

    identity = (
        operation,
        strategy_instance_id,
        int(execution_binding_id),
        int(execution_order_leg_id),
        pos_id,
        order_id,
        authority_fingerprint,
        request_fingerprint,
    )
    with session_factory() as session:
        existing = (
            session.query(PositionMutationIntent)
            .filter(PositionMutationIntent.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            if _intent_identity(existing) != identity:
                raise PositionMutationIntentError(
                    "position_mutation_intent_conflict"
                )
            return existing
        if order_id:
            existing_write = (
                session.query(PositionMutationIntent)
                .filter(
                    PositionMutationIntent.venue
                    == str(venue or "deepcoin").lower(),
                    PositionMutationIntent.operation == operation,
                    PositionMutationIntent.order_id == order_id,
                    PositionMutationIntent.request_fingerprint
                    == request_fingerprint,
                )
                .one_or_none()
            )
            if existing_write is not None:
                if _intent_identity(existing_write) != identity:
                    raise PositionMutationIntentError(
                        "position_mutation_intent_conflict"
                    )
                return existing_write
        row = PositionMutationIntent(
            idempotency_key=idempotency_key,
            venue=str(venue or "deepcoin").lower(),
            operation=operation,
            strategy_instance_id=strategy_instance_id,
            execution_binding_id=int(execution_binding_id),
            execution_order_leg_id=int(execution_order_leg_id),
            pos_id=pos_id,
            order_id=order_id,
            authority_fingerprint=authority_fingerprint,
            request_fingerprint=request_fingerprint,
            status="reserved",
            request_json=json.dumps(
                dict(request),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            reserved_at=reserved_at,
            created_at=reserved_at,
            updated_at=reserved_at,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = (
                session.query(PositionMutationIntent)
                .filter(
                    (
                        PositionMutationIntent.idempotency_key
                        == idempotency_key
                    )
                    | (
                        (
                            PositionMutationIntent.venue
                            == str(venue or "deepcoin").lower()
                        )
                        & (PositionMutationIntent.operation == operation)
                        & (PositionMutationIntent.order_id == order_id)
                        & (
                            PositionMutationIntent.request_fingerprint
                            == request_fingerprint
                        )
                    )
                )
                .first()
            )
            if existing is None:
                raise
            if _intent_identity(existing) != identity:
                raise PositionMutationIntentError(
                    "position_mutation_intent_conflict"
                ) from None
            return existing
        session.refresh(row)
        return row


def transition_position_mutation_intent(
    session_factory: sessionmaker,
    intent_id: int,
    *,
    expected_statuses: set[str],
    new_status: str,
    transitioned_at: datetime,
    response: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
) -> bool:
    """Atomically transition one intent only from an expected status."""

    if new_status not in POSITION_MUTATION_INTENT_STATUSES:
        raise PositionMutationIntentError("position_mutation_status_invalid")
    values: dict[Any, Any] = {
        PositionMutationIntent.status: new_status,
        PositionMutationIntent.updated_at: transitioned_at,
    }
    if new_status == "submitted":
        values[PositionMutationIntent.submitted_at] = transitioned_at
    if new_status == "confirmed":
        values[PositionMutationIntent.confirmed_at] = transitioned_at
    if response is not None:
        values[PositionMutationIntent.response_json] = json.dumps(
            dict(response),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    if error is not None:
        values[PositionMutationIntent.error_json] = json.dumps(
            dict(error),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    with session_factory() as session:
        count = (
            session.query(PositionMutationIntent)
            .filter(PositionMutationIntent.id == int(intent_id))
            .filter(PositionMutationIntent.status.in_(set(expected_statuses)))
            .update(values, synchronize_session=False)
        )
        session.commit()
        return count == 1


def _intent_identity(row: PositionMutationIntent) -> tuple[object, ...]:
    return (
        row.operation,
        row.strategy_instance_id,
        int(row.execution_binding_id),
        int(row.execution_order_leg_id),
        row.pos_id,
        row.order_id,
        row.authority_fingerprint,
        row.request_fingerprint,
    )
