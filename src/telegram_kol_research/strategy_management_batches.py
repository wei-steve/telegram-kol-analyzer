"""Durable persistence primitives for exact-strategy management batches."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import inspect, update
from sqlalchemy.orm import Session, sessionmaker

from telegram_kol_research.db import MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME
from telegram_kol_research.db import MANAGEMENT_BATCH_IDEMPOTENCY_INDEX_NAME
from telegram_kol_research.db import MANAGEMENT_LEG_BATCH_POSITION_INDEX_NAME
from telegram_kol_research.db import REQUIRED_MANAGEMENT_UNIQUE_INDEX_NAMES
from telegram_kol_research.models import ACTIVE_MANAGEMENT_BATCH_SQL_PREDICATE
from telegram_kol_research.models import StrategyManagementBatch
from telegram_kol_research.models import StrategyManagementLeg


RECOVERABLE_BATCH_STATUSES = frozenset({"executing", "reconciling"})
UNSET = object()


class ManagementSchemaSafetyError(RuntimeError):
    """Raised when database uniqueness cannot safely serialize mutations."""


@dataclass(frozen=True, slots=True)
class ManagementLegCreate:
    execution_order_leg_id: int
    pos_id: str
    leg_index: int
    status: str = "planned"
    preflight_size: str | None = None
    planned_close_size: str | None = None
    avg_entry_price: str | None = None
    quantity_step: str | None = None
    old_tpsl: Any = None
    planned_tpsl: Any = None
    client_order_id: str | None = None
    exchange_order_id: str | None = None
    request: Any = None
    response: Any = None
    last_error: Any = None
    last_exchange_snapshot: Any = None


@dataclass(frozen=True, slots=True)
class ManagementLegRecord:
    id: int
    management_batch_id: int
    execution_order_leg_id: int
    pos_id: str
    leg_index: int
    status: str
    preflight_size: str | None
    planned_close_size: str | None
    avg_entry_price: str | None
    quantity_step: str | None
    old_tpsl: Any
    planned_tpsl: Any
    client_order_id: str | None
    exchange_order_id: str | None
    request: Any
    response: Any
    last_error: Any
    last_exchange_snapshot: Any
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ManagementBatchRecord:
    id: int
    idempotency_fingerprint: str
    raw_message_id: int
    recognition_decision_id: int
    recognition_generation: str
    target_lifecycle_id: int
    strategy_instance_id: str
    execution_binding_id: int
    intent: str
    effective_action: str
    requested_fraction: float | None
    effective_fraction: float | None
    partial_round_before: int
    status: str
    reason_code: str | None
    target_fingerprint: str
    target_snapshot: Any
    planned_at: datetime
    started_at: datetime | None
    reconciled_at: datetime | None
    completed_at: datetime | None
    notification_state: str | None
    notification_fingerprint: str | None
    created_at: datetime
    updated_at: datetime
    legs: tuple[ManagementLegRecord, ...]


def create_management_batch(
    session_factory: sessionmaker,
    *,
    idempotency_fingerprint: str,
    raw_message_id: int,
    recognition_decision_id: int,
    recognition_generation: str,
    target_lifecycle_id: int,
    strategy_instance_id: str,
    execution_binding_id: int,
    intent: str,
    effective_action: str,
    requested_fraction: float | None,
    effective_fraction: float | None,
    partial_round_before: int,
    target_fingerprint: str,
    target_snapshot: Any,
    legs: Sequence[ManagementLegCreate],
    planned_at: datetime | None = None,
    status: str = "ready",
    reason_code: str | None = None,
    notification_state: str | None = "pending",
) -> ManagementBatchRecord:
    """Atomically persist one immutable batch target and all of its legs."""

    now = planned_at or datetime.now(UTC)
    with session_factory() as session:
        batch_id = create_management_batch_in_session(
            session,
            idempotency_fingerprint=idempotency_fingerprint,
            raw_message_id=raw_message_id,
            recognition_decision_id=recognition_decision_id,
            recognition_generation=recognition_generation,
            target_lifecycle_id=target_lifecycle_id,
            strategy_instance_id=strategy_instance_id,
            execution_binding_id=execution_binding_id,
            intent=intent,
            effective_action=effective_action,
            requested_fraction=requested_fraction,
            effective_fraction=effective_fraction,
            partial_round_before=partial_round_before,
            target_fingerprint=target_fingerprint,
            target_snapshot=target_snapshot,
            legs=legs,
            planned_at=now,
            status=status,
            reason_code=reason_code,
            notification_state=notification_state,
        )
        session.commit()
    return load_management_batch(session_factory, batch_id)


def create_management_batch_in_session(
    session: Session,
    *,
    idempotency_fingerprint: str,
    raw_message_id: int,
    recognition_decision_id: int,
    recognition_generation: str,
    target_lifecycle_id: int,
    strategy_instance_id: str,
    execution_binding_id: int,
    intent: str,
    effective_action: str,
    requested_fraction: float | None,
    effective_fraction: float | None,
    partial_round_before: int,
    target_fingerprint: str,
    target_snapshot: Any,
    legs: Sequence[ManagementLegCreate],
    planned_at: datetime,
    status: str = "ready",
    reason_code: str | None = None,
    notification_state: str | None = "pending",
    validate_current_state: Callable[[Session], None] | None = None,
) -> int:
    """Insert a batch in the caller transaction after an immediate state gate."""

    _require_management_unique_indexes(session)
    if validate_current_state is not None:
        validate_current_state(session)
    batch = StrategyManagementBatch(
        idempotency_fingerprint=idempotency_fingerprint,
        raw_message_id=raw_message_id,
        recognition_decision_id=recognition_decision_id,
        recognition_generation=recognition_generation,
        target_lifecycle_id=target_lifecycle_id,
        strategy_instance_id=strategy_instance_id,
        execution_binding_id=execution_binding_id,
        intent=intent,
        effective_action=effective_action,
        requested_fraction=requested_fraction,
        effective_fraction=effective_fraction,
        partial_round_before=partial_round_before,
        status=status,
        reason_code=reason_code,
        target_fingerprint=target_fingerprint,
        target_snapshot_json=_encode_json(target_snapshot) or "{}",
        planned_at=planned_at,
        notification_state=notification_state,
        created_at=planned_at,
        updated_at=planned_at,
    )
    session.add(batch)
    session.flush()
    for leg in legs:
        session.add(
            StrategyManagementLeg(
                management_batch_id=batch.id,
                execution_order_leg_id=leg.execution_order_leg_id,
                pos_id=leg.pos_id,
                leg_index=leg.leg_index,
                status=leg.status,
                preflight_size=leg.preflight_size,
                planned_close_size=leg.planned_close_size,
                avg_entry_price=leg.avg_entry_price,
                quantity_step=leg.quantity_step,
                old_tpsl_json=_encode_json(leg.old_tpsl),
                planned_tpsl_json=_encode_json(leg.planned_tpsl),
                client_order_id=leg.client_order_id,
                exchange_order_id=leg.exchange_order_id,
                request_json=_encode_json(leg.request),
                response_json=_encode_json(leg.response),
                last_error=_encode_json(leg.last_error),
                last_exchange_snapshot_json=_encode_json(
                    leg.last_exchange_snapshot
                ),
                created_at=planned_at,
                updated_at=planned_at,
            )
        )
    session.flush()
    return batch.id


def load_management_batch(
    session_factory: sessionmaker, batch_id: int
) -> ManagementBatchRecord:
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        if batch is None:
            raise LookupError("management batch not found")
        return _batch_to_record(session, batch)


def claim_ready_batch(
    session_factory: sessionmaker,
    batch_id: int,
    *,
    claimed_at: datetime | None = None,
) -> ManagementBatchRecord | None:
    """Atomically claim exactly one ready batch for execution."""

    now = claimed_at or datetime.now(UTC)
    with session_factory() as session:
        _require_management_unique_indexes(session)
        result = session.execute(
            update(StrategyManagementBatch)
            .where(
                StrategyManagementBatch.id == batch_id,
                StrategyManagementBatch.status == "ready",
            )
            .values(status="executing", started_at=now, updated_at=now)
        )
        session.commit()
        if result.rowcount != 1:
            return None
    return load_management_batch(session_factory, batch_id)


def transition_batch(
    session_factory: sessionmaker,
    batch_id: int,
    *,
    expected_statuses: Iterable[str],
    new_status: str,
    transitioned_at: datetime | None = None,
    reason_code: Any = UNSET,
) -> bool:
    now = transitioned_at or datetime.now(UTC)
    expected = tuple(expected_statuses)
    if not expected:
        return False
    values: dict[str, Any] = {
        "status": new_status,
        "updated_at": now,
    }
    if reason_code is not UNSET:
        values["reason_code"] = reason_code
    if new_status == "executing":
        values["started_at"] = now
    if new_status == "reconciling":
        values["reconciled_at"] = now
    if new_status in {"succeeded", "blocked", "resolved"}:
        values["completed_at"] = now
    with session_factory() as session:
        _require_management_unique_indexes(session)
        result = session.execute(
            update(StrategyManagementBatch)
            .where(
                StrategyManagementBatch.id == batch_id,
                StrategyManagementBatch.status.in_(expected),
            )
            .values(**values)
        )
        session.commit()
        return result.rowcount == 1


def transition_leg(
    session_factory: sessionmaker,
    leg_id: int,
    *,
    expected_statuses: Iterable[str],
    new_status: str,
    transitioned_at: datetime | None = None,
    client_order_id: Any = UNSET,
    exchange_order_id: Any = UNSET,
    request: Any = UNSET,
    response: Any = UNSET,
    last_error: Any = UNSET,
    last_exchange_snapshot: Any = UNSET,
) -> bool:
    now = transitioned_at or datetime.now(UTC)
    expected = tuple(expected_statuses)
    if not expected:
        return False
    values: dict[str, Any] = {"status": new_status, "updated_at": now}
    optional_values = {
        "client_order_id": client_order_id,
        "exchange_order_id": exchange_order_id,
        "request_json": request,
        "response_json": response,
        "last_error": last_error,
        "last_exchange_snapshot_json": last_exchange_snapshot,
    }
    for key, value in optional_values.items():
        if value is UNSET:
            continue
        values[key] = (
            value
            if key in {"client_order_id", "exchange_order_id"}
            else _encode_json(value)
        )
    with session_factory() as session:
        _require_management_unique_indexes(session)
        result = session.execute(
            update(StrategyManagementLeg)
            .where(
                StrategyManagementLeg.id == leg_id,
                StrategyManagementLeg.status.in_(expected),
            )
            .values(**values)
        )
        session.commit()
        return result.rowcount == 1


def list_recoverable_batches(
    session_factory: sessionmaker, *, limit: int = 50
) -> list[ManagementBatchRecord]:
    if limit <= 0:
        return []
    with session_factory() as session:
        batches = (
            session.query(StrategyManagementBatch)
            .filter(StrategyManagementBatch.status.in_(RECOVERABLE_BATCH_STATUSES))
            .order_by(
                StrategyManagementBatch.planned_at.asc(),
                StrategyManagementBatch.id.asc(),
            )
            .limit(limit)
            .all()
        )
        return [_batch_to_record(session, batch) for batch in batches]


def _batch_to_record(session, batch: StrategyManagementBatch) -> ManagementBatchRecord:
    legs = (
        session.query(StrategyManagementLeg)
        .filter(StrategyManagementLeg.management_batch_id == batch.id)
        .order_by(StrategyManagementLeg.leg_index.asc(), StrategyManagementLeg.id.asc())
        .all()
    )
    return ManagementBatchRecord(
        id=batch.id,
        idempotency_fingerprint=batch.idempotency_fingerprint,
        raw_message_id=batch.raw_message_id,
        recognition_decision_id=batch.recognition_decision_id,
        recognition_generation=batch.recognition_generation,
        target_lifecycle_id=batch.target_lifecycle_id,
        strategy_instance_id=batch.strategy_instance_id,
        execution_binding_id=batch.execution_binding_id,
        intent=batch.intent,
        effective_action=batch.effective_action,
        requested_fraction=batch.requested_fraction,
        effective_fraction=batch.effective_fraction,
        partial_round_before=batch.partial_round_before,
        status=batch.status,
        reason_code=batch.reason_code,
        target_fingerprint=batch.target_fingerprint,
        target_snapshot=_decode_json(batch.target_snapshot_json),
        planned_at=_utc(batch.planned_at),
        started_at=_utc(batch.started_at),
        reconciled_at=_utc(batch.reconciled_at),
        completed_at=_utc(batch.completed_at),
        notification_state=batch.notification_state,
        notification_fingerprint=batch.notification_fingerprint,
        created_at=_utc(batch.created_at),
        updated_at=_utc(batch.updated_at),
        legs=tuple(_leg_to_record(leg) for leg in legs),
    )


def _leg_to_record(leg: StrategyManagementLeg) -> ManagementLegRecord:
    return ManagementLegRecord(
        id=leg.id,
        management_batch_id=leg.management_batch_id,
        execution_order_leg_id=leg.execution_order_leg_id,
        pos_id=leg.pos_id,
        leg_index=leg.leg_index,
        status=leg.status,
        preflight_size=leg.preflight_size,
        planned_close_size=leg.planned_close_size,
        avg_entry_price=leg.avg_entry_price,
        quantity_step=leg.quantity_step,
        old_tpsl=_decode_json(leg.old_tpsl_json),
        planned_tpsl=_decode_json(leg.planned_tpsl_json),
        client_order_id=leg.client_order_id,
        exchange_order_id=leg.exchange_order_id,
        request=_decode_json(leg.request_json),
        response=_decode_json(leg.response_json),
        last_error=_decode_json(leg.last_error),
        last_exchange_snapshot=_decode_json(leg.last_exchange_snapshot_json),
        created_at=_utc(leg.created_at),
        updated_at=_utc(leg.updated_at),
    )


def _encode_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_json(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_management_unique_indexes(session) -> None:
    expected = {
        MANAGEMENT_BATCH_IDEMPOTENCY_INDEX_NAME: (
            "strategy_management_batches",
            ["idempotency_fingerprint"],
        ),
        MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME: (
            "strategy_management_batches",
            ["strategy_instance_id"],
        ),
        MANAGEMENT_LEG_BATCH_POSITION_INDEX_NAME: (
            "strategy_management_legs",
            ["management_batch_id", "pos_id"],
        ),
    }
    inspector = inspect(session.connection())
    observed = {
        index["name"]: index
        for table_name in {table for table, _columns in expected.values()}
        for index in inspector.get_indexes(table_name)
    }
    unsafe = []
    for index_name in sorted(REQUIRED_MANAGEMENT_UNIQUE_INDEX_NAMES):
        _expected_table, expected_columns = expected[index_name]
        index = observed.get(index_name)
        if (
            index is None
            or not index.get("unique")
            or index.get("column_names") != expected_columns
        ):
            unsafe.append(index_name)
            continue
        if index_name == MANAGEMENT_BATCH_ACTIVE_STRATEGY_INDEX_NAME:
            where = str(index.get("dialect_options", {}).get("sqlite_where", ""))
            if (
                session.get_bind().dialect.name == "sqlite"
                and _normalize_sql_predicate(where)
                != _normalize_sql_predicate(ACTIVE_MANAGEMENT_BATCH_SQL_PREDICATE)
            ):
                unsafe.append(index_name)
    if unsafe:
        session.rollback()
        raise ManagementSchemaSafetyError(
            "management database safety indexes are missing or invalid: "
            + ", ".join(unsafe)
        )


def _normalize_sql_predicate(predicate: str) -> tuple[str, tuple[str, ...]]:
    literals: list[str] = []

    def replace_literal(match: re.Match[str]) -> str:
        literals.append(match.group(1).replace("''", "'"))
        return "?"

    structure = re.sub(r"'((?:''|[^'])*)'", replace_literal, predicate)
    structure = re.sub(r'[\s"`\[\]]+', "", structure).lower()
    return structure, tuple(literals)
