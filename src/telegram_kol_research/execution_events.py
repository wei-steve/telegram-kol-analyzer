"""Append-only execution event ledger helpers."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.exc import IntegrityError

from telegram_kol_research.models import ExecutionEvent


@dataclass(slots=True)
class ExecutionEventRecord:
    action: str
    venue: str = "deepcoin"
    status: str = "submitted"
    execution_binding_id: int | None = None
    trade_signal_id: int | None = None
    strategy_instance_id: str | None = None
    kol_id: str | None = None
    chat_id: int | None = None
    message_id: int | None = None
    source_message_id: int | None = None
    symbol: str | None = None
    side: str | None = None
    order_id: str | None = None
    client_order_id: str | None = None
    pos_id: str | None = None
    related_order_id: str | None = None
    reason: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    exchange_event_time: datetime | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class ExecutionEventView:
    id: int
    action: str
    venue: str
    status: str
    strategy_instance_id: str | None
    execution_binding_id: int | None
    trade_signal_id: int | None
    kol_id: str | None
    chat_id: int | None
    message_id: int | None
    source_message_id: int | None
    symbol: str | None
    side: str | None
    order_id: str | None
    client_order_id: str | None
    pos_id: str | None
    related_order_id: str | None
    reason: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    request: dict[str, Any] | None
    response: dict[str, Any] | None
    exchange_event_time: datetime | None
    created_at: datetime


def record_execution_event(
    session_factory: sessionmaker,
    record: ExecutionEventRecord,
    *,
    session: Session | None = None,
) -> int:
    """Persist one immutable execution event and return its id.

    When ``session`` is supplied, the caller owns the surrounding transaction.
    """

    now = record.created_at or datetime.now(UTC)
    row = ExecutionEvent(
        execution_binding_id=record.execution_binding_id,
        trade_signal_id=record.trade_signal_id,
        strategy_instance_id=record.strategy_instance_id,
        venue=record.venue.lower(),
        action=record.action,
        status=record.status,
        kol_id=record.kol_id,
        chat_id=record.chat_id,
        message_id=record.message_id,
        source_message_id=record.source_message_id,
        symbol=record.symbol.upper() if record.symbol else None,
        side=record.side.lower() if record.side else None,
        order_id=record.order_id,
        client_order_id=record.client_order_id,
        pos_id=record.pos_id,
        related_order_id=record.related_order_id,
        reason=record.reason,
        before_json=_json_or_none(record.before),
        after_json=_json_or_none(record.after),
        request_json=_json_or_none(record.request),
        response_json=_json_or_none(record.response),
        exchange_event_time=record.exchange_event_time,
        created_at=now,
    )
    if session is not None:
        session.add(row)
        session.flush()
        return int(row.id)
    with session_factory() as owned_session:
        owned_session.add(row)
        owned_session.commit()
        return int(row.id)


def list_execution_events(
    session_factory: sessionmaker,
    *,
    strategy_instance_id: str | None = None,
    execution_binding_id: int | None = None,
    order_id: str | None = None,
    pos_id: str | None = None,
    action: str | None = None,
    limit: int = 100,
) -> list[ExecutionEventView]:
    """List execution events newest first, with optional filters."""

    with session_factory() as session:
        query = session.query(ExecutionEvent)
        if strategy_instance_id is not None:
            query = query.filter(ExecutionEvent.strategy_instance_id == strategy_instance_id)
        if execution_binding_id is not None:
            query = query.filter(ExecutionEvent.execution_binding_id == execution_binding_id)
        if order_id is not None:
            query = query.filter(ExecutionEvent.order_id == order_id)
        if pos_id is not None:
            query = query.filter(ExecutionEvent.pos_id == pos_id)
        if action is not None:
            query = query.filter(ExecutionEvent.action == action)
        rows = (
            query.order_by(ExecutionEvent.created_at.desc(), ExecutionEvent.id.desc())
            .limit(max(1, min(int(limit), 500)))
            .all()
        )
        return [_row_to_view(row) for row in rows]


def enqueue_terminal_entry_cleanup_notification(
    session_factory: sessionmaker,
    *,
    lifecycle_id: int,
    binding_id: int,
    status: str,
    leg_ids: tuple[int, ...],
    order_ids: tuple[str, ...],
    reason: str,
    created_at: datetime | None = None,
) -> int:
    """Persist one durable notification event for a stable cleanup outcome."""

    normalized_status = str(status or "unknown").lower()
    if normalized_status not in {"resolved", "already_absent", "blocked", "unknown"}:
        normalized_status = "unknown"
    identity_payload = {
        "binding_id": int(binding_id),
        "lifecycle_id": int(lifecycle_id),
        "leg_ids": sorted({int(value) for value in leg_ids}),
        "order_ids": sorted(
            {str(value)[:255] for value in order_ids if str(value)}
        ),
        "reason": str(reason or "strategy_terminal")[:64],
        "status": normalized_status,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            identity_payload,
            ensure_ascii=True,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        **identity_payload,
        "notification_policy_version": "terminal-entry-cleanup-v2",
    }
    now = created_at or datetime.now(UTC)
    with session_factory() as session:
        existing = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.notification_fingerprint == fingerprint)
            .one_or_none()
        )
        if existing is not None:
            _upgrade_cleanup_notification_payload_version(existing, payload)
            session.commit()
            return int(existing.id)
        row = ExecutionEvent(
            execution_binding_id=int(binding_id),
            venue="deepcoin",
            action="terminal_entry_cleanup_outcome",
            status=normalized_status,
            reason=payload["reason"],
            response_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            notification_status="pending",
            notification_fingerprint=fingerprint,
            notification_attempts=0,
            created_at=now,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing_id = (
                session.query(ExecutionEvent)
                .filter(ExecutionEvent.notification_fingerprint == fingerprint)
                .one_or_none()
            )
            if existing_id is None:
                raise
            _upgrade_cleanup_notification_payload_version(existing_id, payload)
            session.commit()
            return int(existing_id.id)
        return int(row.id)


def enqueue_entry_price_geometry_rejection_notification(
    session_factory: sessionmaker,
    *,
    raw_message_id: int | None,
    candidate_id: int | None,
    chat_id: int,
    symbol: str,
    side: str,
    parse_source: str,
    authoritative_generation: str | None,
    geometry: dict[str, Any],
    execution_source_id: str | None = None,
    created_at: datetime | None = None,
) -> int:
    """Persist one redacted, deduplicated operator alert for a safety rejection."""

    reason_code = str(geometry.get("reason_code") or "")[:64]
    normalized_geometry = {
        "entry_domain": list(geometry.get("entry_domain") or [])[:2],
        "entry_prices": list(geometry.get("entry_prices") or [])[:16],
        "stop_loss": geometry.get("stop_loss"),
        "take_profit_prices": list(
            geometry.get("take_profit_prices") or []
        )[:16],
        "explicit_average_entry": geometry.get("explicit_average_entry"),
        "offending_field": geometry.get("offending_field"),
        "offending_value": geometry.get("offending_value"),
        "geometry_status": geometry.get("geometry_status"),
    }
    fingerprint_payload = {
        "candidate_identity": (
            {"candidate_id": int(candidate_id)}
            if candidate_id is not None
            else {"execution_source_id": str(execution_source_id or "")[:128]}
        ),
        "authoritative_generation": str(authoritative_generation or "unknown")[:64],
        "geometry": normalized_geometry,
        "reason_code": reason_code,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        "raw_message_id": (
            int(raw_message_id) if raw_message_id is not None else None
        ),
        "candidate_id": int(candidate_id) if candidate_id is not None else None,
        "chat_id": int(chat_id),
        "symbol": str(symbol or "")[:64].upper(),
        "side": str(side or "")[:16].lower(),
        "parse_source": str(parse_source or "unknown")[:32],
        "authoritative_generation": str(
            authoritative_generation or "unknown"
        )[:64],
        "execution_source_id": str(execution_source_id or "")[:128] or None,
        "reason_code": reason_code,
        **normalized_geometry,
        "notification_policy_version": "entry-price-geometry-v1",
    }
    now = created_at or datetime.now(UTC)
    with session_factory() as session:
        existing = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.notification_fingerprint == fingerprint)
            .one_or_none()
        )
        if existing is not None:
            return int(existing.id)
        row = ExecutionEvent(
            venue="deepcoin",
            action="entry_price_geometry_rejected",
            status="manual_review",
            chat_id=int(chat_id),
            source_message_id=(
                int(raw_message_id) if raw_message_id is not None else None
            ),
            symbol=str(symbol or "")[:64].upper() or None,
            side=str(side or "")[:16].lower() or None,
            reason=reason_code,
            response_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            notification_status="pending",
            notification_fingerprint=fingerprint,
            notification_attempts=0,
            created_at=now,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = (
                session.query(ExecutionEvent)
                .filter(ExecutionEvent.notification_fingerprint == fingerprint)
                .one_or_none()
            )
            if existing is None:
                raise
            return int(existing.id)
        return int(row.id)


def _upgrade_cleanup_notification_payload_version(
    event: ExecutionEvent,
    versioned_payload: dict[str, Any],
) -> None:
    if event.notification_status not in {"pending", "failed"}:
        return
    existing_payload = _json_load(event.response_json)
    if existing_payload and existing_payload.get("notification_policy_version"):
        return
    event.response_json = json.dumps(
        versioned_payload,
        ensure_ascii=False,
        sort_keys=True,
    )


def _row_to_view(row: ExecutionEvent) -> ExecutionEventView:
    return ExecutionEventView(
        id=row.id,
        action=row.action,
        venue=row.venue,
        status=row.status,
        strategy_instance_id=row.strategy_instance_id,
        execution_binding_id=row.execution_binding_id,
        trade_signal_id=row.trade_signal_id,
        kol_id=row.kol_id,
        chat_id=row.chat_id,
        message_id=row.message_id,
        source_message_id=row.source_message_id,
        symbol=row.symbol,
        side=row.side,
        order_id=row.order_id,
        client_order_id=row.client_order_id,
        pos_id=row.pos_id,
        related_order_id=row.related_order_id,
        reason=row.reason,
        before=_json_load(row.before_json),
        after=_json_load(row.after_json),
        request=_json_load(row.request_json),
        response=_json_load(row.response_json),
        exchange_event_time=row.exchange_event_time,
        created_at=row.created_at,
    )


def _json_or_none(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_load(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None
