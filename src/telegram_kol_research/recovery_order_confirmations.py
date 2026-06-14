"""Audit records for second human confirmation before live recovery orders."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import RecoveryOrderConfirmation


def upsert_ready_recovery_order_confirmation(
    session_factory: sessionmaker,
    *,
    confirmation_payload: dict[str, object],
    confirmed_at: datetime | None = None,
) -> dict[str, object]:
    """Persist a ready-confirmed audit record for a recovery order preview."""

    payload_source = confirmation_payload["source"]
    payload_preview = confirmation_payload["payload_preview"]
    confirmed_at = _normalize_datetime(confirmed_at or datetime.now(UTC))
    chat_id = int(payload_source["chat_id"])
    message_id = int(payload_source["message_id"])
    symbol = str(payload_source["symbol"]).upper()
    side = str(payload_source["side"]).lower()

    with session_factory() as session:
        row = (
            session.query(RecoveryOrderConfirmation)
            .filter(RecoveryOrderConfirmation.chat_id == chat_id)
            .filter(RecoveryOrderConfirmation.message_id == message_id)
            .filter(RecoveryOrderConfirmation.symbol == symbol)
            .filter(RecoveryOrderConfirmation.side == side)
            .one_or_none()
        )
        if row is None:
            row = RecoveryOrderConfirmation(
                kol_id=str(payload_preview.get("source", {}).get("kol_id") or "unknown"),
                chat_id=chat_id,
                message_id=message_id,
                symbol=symbol,
                side=side,
                venue=str(payload_preview.get("venue") or "deepcoin").lower(),
                confirmed_at=confirmed_at,
                confirmation_payload_json="{}",
            )
            session.add(row)
            session.flush()

        row.kol_id = str(payload_preview.get("source", {}).get("kol_id") or row.kol_id)
        row.venue = str(payload_preview.get("venue") or row.venue).lower()
        row.status = "ready_confirmed"
        row.confirmed_at = confirmed_at
        row.confirmation_payload_json = json.dumps(
            confirmation_payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        row.updated_at = datetime.now(UTC)
        row_id = row.id
        session.commit()

    return _load_confirmation_by_id(session_factory, row_id)


def has_ready_recovery_order_confirmation(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
) -> bool:
    """Return whether a ready-confirmed audit record exists for a signal."""

    with session_factory() as session:
        return (
            session.query(RecoveryOrderConfirmation)
            .filter(RecoveryOrderConfirmation.chat_id == chat_id)
            .filter(RecoveryOrderConfirmation.message_id == message_id)
            .filter(RecoveryOrderConfirmation.symbol == symbol.upper())
            .filter(RecoveryOrderConfirmation.side == side.lower())
            .filter(RecoveryOrderConfirmation.status == "ready_confirmed")
            .one_or_none()
            is not None
        )


def list_recovery_order_confirmations(
    session_factory: sessionmaker,
) -> list[dict[str, object]]:
    """List second-confirmation audit records in insertion order."""

    with session_factory() as session:
        rows = (
            session.query(RecoveryOrderConfirmation)
            .order_by(RecoveryOrderConfirmation.id.asc())
            .all()
        )
        return [_serialize_confirmation(row) for row in rows]


def _load_confirmation_by_id(
    session_factory: sessionmaker,
    row_id: int,
) -> dict[str, object]:
    with session_factory() as session:
        row = session.get(RecoveryOrderConfirmation, row_id)
        if row is None:
            raise LookupError("recovery order confirmation not found")
        return _serialize_confirmation(row)


def _serialize_confirmation(row: RecoveryOrderConfirmation) -> dict[str, object]:
    payload = json.loads(row.confirmation_payload_json)
    return {
        "id": row.id,
        "kol_id": row.kol_id,
        "chat_id": row.chat_id,
        "message_id": row.message_id,
        "symbol": row.symbol,
        "side": row.side,
        "venue": row.venue,
        "status": row.status,
        "confirmed_at": row.confirmed_at,
        "deepcoin_order_draft": payload.get("deepcoin_order_draft"),
        "confirmation_payload": payload,
    }


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
