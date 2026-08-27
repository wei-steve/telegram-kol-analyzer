"""Durable cross-process authority for entry-revision exchange writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Literal
import uuid

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from telegram_kol_research.models import TradingSetting
from telegram_kol_research.trading_settings import (
    TRADING_SETTINGS_KEY,
    TradingSettings,
    trading_settings_from_payload,
)


ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY = "entry_revision_exchange_authority"
_SCHEMA_VERSION = 1
_OWNER_KINDS = frozenset(
    {"entry_revision_worker", "reviewed_pending_entry_cancel"}
)
_HELD_KEYS = frozenset(
    {
        "schema_version",
        "state",
        "owner_kind",
        "owner_id",
        "token",
        "acquired_at",
    }
)
_IDLE_KEYS = frozenset({"schema_version", "state", "released_at"})


@dataclass(frozen=True, slots=True)
class EntryRevisionExchangeAuthorityAcquisition:
    acquired: bool
    token: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class EntryRevisionExchangeAuthorityRelease:
    released: bool
    reason_code: str | None = None


def acquire_entry_revision_exchange_authority(
    session_factory,
    *,
    owner_kind: Literal[
        "entry_revision_worker", "reviewed_pending_entry_cancel"
    ],
    owner_id: str,
    acquired_at: datetime,
    require_cancel_quiescence: bool,
) -> EntryRevisionExchangeAuthorityAcquisition:
    """Atomically acquire the one fail-closed entry-revision exchange lease."""

    clean_owner_kind = _owner_kind(owner_kind)
    clean_owner_id = _bounded_text(owner_id, field_name="owner_id", maximum=128)
    clean_acquired_at = _timestamp(acquired_at)
    if require_cancel_quiescence != (
        clean_owner_kind == "reviewed_pending_entry_cancel"
    ):
        raise ValueError("authority owner and quiescence contract differ")

    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if require_cancel_quiescence:
                settings_reason = _cancel_quiescence_reason(session)
                if settings_reason is not None:
                    session.rollback()
                    return EntryRevisionExchangeAuthorityAcquisition(
                        acquired=False,
                        reason_code=settings_reason,
                    )

            row = (
                session.query(TradingSetting)
                .filter(
                    TradingSetting.key
                    == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
                )
                .one_or_none()
            )
            if row is not None:
                document = _authority_document(row.value_json)
                if document is None:
                    session.rollback()
                    return EntryRevisionExchangeAuthorityAcquisition(
                        acquired=False,
                        reason_code="entry_revision_exchange_authority_invalid",
                    )
                if document["state"] == "held":
                    session.rollback()
                    return EntryRevisionExchangeAuthorityAcquisition(
                        acquired=False,
                        reason_code="entry_revision_exchange_authority_busy",
                    )

            token = uuid.uuid4().hex
            document = {
                "acquired_at": clean_acquired_at.isoformat(),
                "owner_id": clean_owner_id,
                "owner_kind": clean_owner_kind,
                "schema_version": _SCHEMA_VERSION,
                "state": "held",
                "token": token,
            }
            if row is None:
                row = TradingSetting(
                    key=ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
                    value_json=_canonical_json(document),
                    updated_at=clean_acquired_at,
                )
                session.add(row)
            else:
                row.value_json = _canonical_json(document)
                row.updated_at = clean_acquired_at
            session.commit()
            return EntryRevisionExchangeAuthorityAcquisition(
                acquired=True,
                token=token,
            )
    except SQLAlchemyError:
        return EntryRevisionExchangeAuthorityAcquisition(
            acquired=False,
            reason_code="entry_revision_exchange_authority_unavailable",
        )


def release_entry_revision_exchange_authority(
    session_factory,
    *,
    token: str,
    owner_kind: str,
    released_at: datetime,
) -> EntryRevisionExchangeAuthorityRelease:
    """Release only an exactly owned valid lease; every mismatch stays held."""

    clean_token = _bounded_text(token, field_name="token", maximum=64)
    clean_owner_kind = _owner_kind(owner_kind)
    clean_released_at = _timestamp(released_at)
    try:
        with session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = (
                session.query(TradingSetting)
                .filter(
                    TradingSetting.key
                    == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
                )
                .one_or_none()
            )
            if row is None:
                session.rollback()
                return EntryRevisionExchangeAuthorityRelease(
                    released=False,
                    reason_code="entry_revision_exchange_authority_invalid",
                )
            document = _authority_document(row.value_json)
            if document is None or document["state"] != "held":
                session.rollback()
                return EntryRevisionExchangeAuthorityRelease(
                    released=False,
                    reason_code="entry_revision_exchange_authority_invalid",
                )
            if (
                document["token"] != clean_token
                or document["owner_kind"] != clean_owner_kind
            ):
                session.rollback()
                return EntryRevisionExchangeAuthorityRelease(
                    released=False,
                    reason_code=(
                        "entry_revision_exchange_authority_owner_mismatch"
                    ),
                )
            row.value_json = _canonical_json(
                {
                    "released_at": clean_released_at.isoformat(),
                    "schema_version": _SCHEMA_VERSION,
                    "state": "idle",
                }
            )
            row.updated_at = clean_released_at
            session.commit()
            return EntryRevisionExchangeAuthorityRelease(released=True)
    except SQLAlchemyError:
        return EntryRevisionExchangeAuthorityRelease(
            released=False,
            reason_code="entry_revision_exchange_authority_unavailable",
        )


def _cancel_quiescence_reason(session) -> str | None:
    row = (
        session.query(TradingSetting)
        .filter(TradingSetting.key == TRADING_SETTINGS_KEY)
        .one_or_none()
    )
    if row is None:
        settings = TradingSettings()
    else:
        try:
            payload = json.loads(row.value_json)
            if not isinstance(payload, dict):
                raise ValueError("settings payload is not an object")
            settings = trading_settings_from_payload(payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            return "pending_entry_cancel_settings_invalid"
    if settings.auto_trade_enabled is not False:
        return "pending_entry_cancel_auto_trade_not_frozen"
    if settings.entry_revision_v2_mode != "disabled":
        return "pending_entry_cancel_revision_not_disabled"
    return None


def _authority_document(value_json: str) -> dict[str, object] | None:
    try:
        document = json.loads(value_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("schema_version") != _SCHEMA_VERSION:
        return None
    state = document.get("state")
    if state == "idle":
        if frozenset(document) != _IDLE_KEYS:
            return None
        if _parsed_timestamp(document.get("released_at")) is None:
            return None
        return document
    if state != "held" or frozenset(document) != _HELD_KEYS:
        return None
    if document.get("owner_kind") not in _OWNER_KINDS:
        return None
    if not _valid_text(document.get("owner_id"), maximum=128):
        return None
    if not _valid_text(document.get("token"), maximum=64):
        return None
    if _parsed_timestamp(document.get("acquired_at")) is None:
        return None
    return document


def _owner_kind(value: object) -> str:
    clean = str(value or "").strip()
    if clean not in _OWNER_KINDS:
        raise ValueError("unknown entry revision exchange authority owner")
    return clean


def _bounded_text(value: object, *, field_name: str, maximum: int) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > maximum:
        raise ValueError(f"{field_name} is invalid")
    return clean


def _valid_text(value: object, *, maximum: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= maximum


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("authority timestamp is invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parsed_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
