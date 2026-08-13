"""Durable compare-and-set records for exact position writes."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import PositionMutationIntent


POSITION_MUTATION_INTENT_STATUSES = frozenset(
    {
        "reserved",
        "not_sent",
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
    strict_set_identity = (
        operation == "set_position_sltp"
        and (
            "_base_authority_fingerprint" in request
            or str(idempotency_key).startswith("protected-entry:")
        )
    )
    if strict_set_identity:
        _require_set_position_request_identity(
            request,
            request_fingerprint=request_fingerprint,
            authority_fingerprint=authority_fingerprint,
        )
    with session_factory() as session:
        existing = (
            session.query(PositionMutationIntent)
            .filter(PositionMutationIntent.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            if strict_set_identity:
                _require_existing_set_position_request_identity(
                    existing,
                    incoming_request=request,
                    incoming_authority_fingerprint=authority_fingerprint,
                    request_fingerprint=request_fingerprint,
                )
            existing_identity = _intent_identity(existing)
            if strict_set_identity:
                existing_identity = (
                    *existing_identity[:6],
                    authority_fingerprint,
                    *existing_identity[7:],
                )
            if (
                existing.status == "confirmed"
                and order_id is None
                and existing.order_id
            ):
                existing_identity = (
                    *existing_identity[:5],
                    None,
                    *existing_identity[6:],
                )
            if existing_identity != identity:
                raise PositionMutationIntentError(
                    "position_mutation_intent_conflict"
                )
            if existing.status == "not_sent":
                rearmed = transition_position_mutation_intent(
                    session_factory,
                    int(existing.id),
                    expected_statuses={"not_sent"},
                    new_status="reserved",
                    transitioned_at=reserved_at,
                    error={},
                )
                if rearmed:
                    with session_factory() as reloaded_session:
                        return reloaded_session.get(
                            PositionMutationIntent, int(existing.id)
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
            if strict_set_identity:
                _require_existing_set_position_request_identity(
                    existing,
                    incoming_request=request,
                    incoming_authority_fingerprint=authority_fingerprint,
                    request_fingerprint=request_fingerprint,
                )
            existing_identity = _intent_identity(existing)
            if strict_set_identity:
                existing_identity = (
                    *existing_identity[:6],
                    authority_fingerprint,
                    *existing_identity[7:],
                )
            if existing_identity != identity:
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
        values[PositionMutationIntent.error_json] = (
            json.dumps(
                dict(error),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if error
            else None
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


def _require_existing_set_position_request_identity(
    row: PositionMutationIntent,
    *,
    incoming_request: Mapping[str, Any],
    incoming_authority_fingerprint: str,
    request_fingerprint: str,
) -> None:
    existing = load_validated_set_position_request(
        row.request_json,
        request_fingerprint=request_fingerprint,
        authority_fingerprint=str(row.authority_fingerprint),
    )
    incoming = dict(incoming_request)
    _require_set_position_request_identity(
        incoming,
        request_fingerprint=request_fingerprint,
        authority_fingerprint=incoming_authority_fingerprint,
    )
    if (
        existing.get("_ledger_purpose")
        != incoming.get("_ledger_purpose")
        or existing.get("_base_authority_fingerprint")
        != incoming.get("_base_authority_fingerprint")
        or existing.get("_base_authority_fingerprint")
        != incoming_authority_fingerprint
    ):
        raise PositionMutationIntentError(
            "position_mutation_intent_conflict"
        )


def load_validated_set_position_request(
    raw: object,
    *,
    request_fingerprint: str,
    authority_fingerprint: str | None = None,
    require_baseline: bool = False,
) -> dict[str, Any]:
    request = _strict_request_object(raw)
    _require_set_position_request_identity(
        request,
        request_fingerprint=request_fingerprint,
        authority_fingerprint=authority_fingerprint,
    )
    if require_baseline and not _valid_order_refs(
        request.get("_pre_submit_order_refs")
    ):
        raise PositionMutationIntentError(
            "position_mutation_intent_conflict"
        )
    return request


def _require_set_position_request_identity(
    request: Mapping[str, Any],
    *,
    request_fingerprint: str,
    authority_fingerprint: str | None = None,
) -> None:
    if not isinstance(request, Mapping):
        raise PositionMutationIntentError(
            "position_mutation_intent_conflict"
        )
    payload = dict(request)
    baseline = payload.pop("_pre_submit_order_refs", None)
    ledger_purpose = payload.pop("_ledger_purpose", None)
    base_authority_fingerprint = payload.pop(
        "_base_authority_fingerprint", None
    )
    try:
        fingerprint_matches = (
            _request_fingerprint(payload) == request_fingerprint
        )
    except (RecursionError, TypeError, ValueError):
        fingerprint_matches = False
    if (
        ledger_purpose not in {"stop_loss", "backup_stop", "take_profit"}
        or not _is_sha256(base_authority_fingerprint)
        or (
            baseline is not None
            and not _valid_order_refs(baseline)
        )
        or not fingerprint_matches
    ):
        raise PositionMutationIntentError(
            "position_mutation_intent_conflict"
        )
    expected_authority_fingerprint = (
        _bound_set_position_authority_fingerprint(
            base_authority_fingerprint=base_authority_fingerprint,
            ledger_purpose=ledger_purpose,
            pre_submit_order_refs=baseline,
        )
        if baseline is not None
        else base_authority_fingerprint
    )
    if (
        authority_fingerprint is not None
        and authority_fingerprint != expected_authority_fingerprint
    ):
        raise PositionMutationIntentError(
            "position_mutation_intent_conflict"
        )


def _strict_request_object(raw: object) -> dict[str, Any]:
    try:
        oversized = (
            not isinstance(raw, str)
            or len(raw.encode("utf-8")) > 4096
        )
    except UnicodeEncodeError:
        oversized = True
    if oversized:
        raise PositionMutationIntentError(
            "position_mutation_intent_conflict"
        )

    def reject_constant(_: str) -> None:
        raise ValueError("invalid constant")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        loaded = json.loads(
            raw,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ) as exc:
        raise PositionMutationIntentError(
            "position_mutation_intent_conflict"
        ) from exc
    if not isinstance(loaded, dict):
        raise PositionMutationIntentError(
            "position_mutation_intent_conflict"
        )
    return loaded


def _valid_order_refs(value: object) -> bool:
    return (
        isinstance(value, list)
        and value == sorted(set(value))
        and all(
            isinstance(item, str)
            and len(item) == 64
            and all(character in "0123456789abcdef" for character in item)
            for item in value
        )
    )


def bound_set_position_authority_fingerprint(
    *,
    base_authority_fingerprint: str,
    ledger_purpose: str,
    pre_submit_order_refs: list[str],
) -> str:
    if (
        not _is_sha256(base_authority_fingerprint)
        or ledger_purpose
        not in {"stop_loss", "backup_stop", "take_profit"}
        or not _valid_order_refs(pre_submit_order_refs)
    ):
        raise PositionMutationIntentError(
            "position_mutation_intent_conflict"
        )
    return _bound_set_position_authority_fingerprint(
        base_authority_fingerprint=base_authority_fingerprint,
        ledger_purpose=ledger_purpose,
        pre_submit_order_refs=pre_submit_order_refs,
    )


def _bound_set_position_authority_fingerprint(
    *,
    base_authority_fingerprint: str,
    ledger_purpose: str,
    pre_submit_order_refs: object,
) -> str:
    return _request_fingerprint(
        {
            "base_authority_fingerprint": base_authority_fingerprint,
            "ledger_purpose": ledger_purpose,
            "pre_submit_order_refs": pre_submit_order_refs,
        }
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _request_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
