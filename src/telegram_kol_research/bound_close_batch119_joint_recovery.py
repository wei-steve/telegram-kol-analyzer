"""Closed, read-only material authority for the joint recovery incident."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3
from typing import Literal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from telegram_kol_research.bound_close_writer_quiescence import (
    _ACTIVE_WINDOW,
    _DEEPCOIN_TABLE,
    _MAX_INSPECTED_ROWS_PER_TABLE,
    _SAFE_NONWRITER_STATES,
    _TARGET_STATES,
    _TARGET_TABLE,
    _identifier,
    _normalize_now,
    _parse_writer_timestamp,
)
from telegram_kol_research.bound_close_reservation_recovery import (
    _REQUIRED_SOURCE_COLUMNS,
    _load_source_descendants,
)
from telegram_kol_research.composite_management_batch_recovery import (
    CompositeBatchRecoveryRefusal,
    _batch119_material_row_payload,
    _fingerprint,
    _load_batch119_local_material_authority_in_session,
    create_composite_recovery_read_only_session_factory,
)
from telegram_kol_research.deployment_preflight import (
    _KNOWN_PRIOR_SCHEMA_MISSING_TABLE_SETS,
    _WORK_SPECS,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionMutationIntent,
)


JOINT_DIAGNOSTIC = "joint_diagnostic"
BOUND_APPLY_PRE = "bound_apply_pre"
BOUND_APPLY_POST = "bound_apply_post"
_PHASES = frozenset({JOINT_DIAGNOSTIC, BOUND_APPLY_PRE, BOUND_APPLY_POST})
_EXPECTED_RESERVATION_COUNT = 29
_MAX_TARGET_ROWS = 64


@dataclass(frozen=True, slots=True)
class JointRecoveryMaterialAuthority:
    material_fingerprint: str
    reservation_count: int
    batch119_incident_count: int
    blocking_writer_count: int
    status: Literal["ready", "refused"]
    reason_code: str | None


def _refused(
    reason_code: str, *, blocking: int = 1
) -> JointRecoveryMaterialAuthority:
    return JointRecoveryMaterialAuthority(
        material_fingerprint=_fingerprint(
            {"schema_version": 1, "status": "refused"}
        ),
        reservation_count=0,
        batch119_incident_count=0,
        blocking_writer_count=max(1, int(blocking)),
        status="refused",
        reason_code=reason_code,
    )


def _table_columns(session, table: str) -> frozenset[str]:
    rows = session.execute(
        text(f"PRAGMA table_info({_identifier(table)})")
    ).fetchall()
    return frozenset(str(row[1]) for row in rows)


def _target_material(
    session, *, phase: str
) -> tuple[list[dict], int, dict[str, object]]:
    columns = _table_columns(session, _TARGET_TABLE)
    required = frozenset(_REQUIRED_SOURCE_COLUMNS[_TARGET_TABLE])
    if columns != required:
        raise ValueError("joint_schema_invalid")
    selected = ", ".join(_identifier(name) for name in sorted(columns))
    rows = session.execute(
        text(
            f"SELECT {selected} FROM {_identifier(_TARGET_TABLE)} "
            f"ORDER BY id LIMIT {_MAX_TARGET_ROWS + 1}"
        )
    ).mappings().all()
    if len(rows) > _MAX_TARGET_ROWS:
        raise ValueError("joint_population_invalid")
    expected_status = "confirmed" if phase == BOUND_APPLY_POST else None
    material: list[dict] = []
    for row in rows:
        status = row.get("status")
        if expected_status is None:
            if type(status) is not str or status not in _TARGET_STATES:
                raise ValueError("joint_population_invalid")
        elif status != expected_status:
            raise ValueError("joint_population_invalid")
        material.append({name: row[name] for name in sorted(columns)})
    if len(material) != _EXPECTED_RESERVATION_COUNT:
        raise ValueError("joint_population_invalid")
    driver_connection = session.connection().connection.driver_connection
    if type(driver_connection) is not sqlite3.Connection:
        raise ValueError("joint_database_invalid")
    previous_row_factory = driver_connection.row_factory
    try:
        driver_connection.row_factory = sqlite3.Row
        source_rows = driver_connection.execute(
            """
            SELECT id, pos_id, execution_binding_id, status, last_error,
                   created_at, updated_at
            FROM bound_position_close_reservations
            ORDER BY id
            LIMIT 65
            """
        ).fetchall()
        source = _load_source_descendants(
            driver_connection,
            source_rows,
            source_status_overrides=(
                {int(row["id"]): "submitted" for row in source_rows}
                if phase == BOUND_APPLY_POST
                else None
            ),
        )
    finally:
        driver_connection.row_factory = previous_row_factory
    if (
        len(source.reservations) != _EXPECTED_RESERVATION_COUNT
        or any(item.local_reason_code is not None for item in source.reservations)
    ):
        raise ValueError("joint_population_invalid")
    binding_ids = tuple(int(row["execution_binding_id"]) for row in rows)
    position_ids = tuple(str(row["pos_id"]) for row in rows)

    def complete_rows(query) -> list[dict[str, object]]:
        selected = query.limit(257).all()
        if len(selected) > 256:
            raise ValueError("joint_population_invalid")
        return [_batch119_material_row_payload(row) for row in selected]

    descendant_material: dict[str, object] = {
        "source_fingerprint": str(source.source_fingerprint),
        "execution_bindings": complete_rows(
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.id.in_(binding_ids))
            .order_by(ExecutionBinding.id)
        ),
        "execution_events": complete_rows(
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.action == "close_bound_position_market",
                ExecutionEvent.pos_id.in_(position_ids),
            )
            .order_by(ExecutionEvent.id)
        ),
        "execution_order_legs": complete_rows(
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.pos_id.in_(position_ids))
            .order_by(ExecutionOrderLeg.id)
        ),
        "position_mutation_intents": complete_rows(
            session.query(PositionMutationIntent)
            .filter(
                PositionMutationIntent.operation == "close_position",
                PositionMutationIntent.pos_id.in_(position_ids),
            )
            .order_by(PositionMutationIntent.id)
        ),
    }
    if any(
        len(descendant_material[field]) != _EXPECTED_RESERVATION_COUNT
        for field in (
            "execution_bindings",
            "execution_events",
            "execution_order_legs",
            "position_mutation_intents",
        )
    ):
        raise ValueError("joint_population_invalid")
    return material, len(material), descendant_material


def _blocking_writer_count(
    session,
    *,
    now: datetime,
    batch_row_id: int,
    management_leg_row_id: int,
    component_row_ids: tuple[int, ...],
    instruction_item_row_ids: tuple[int, ...],
) -> int:
    available = {
        str(row[0])
        for row in session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }
    required_source_tables = frozenset(_REQUIRED_SOURCE_COLUMNS)
    if not required_source_tables.issubset(available):
        raise ValueError("joint_schema_invalid")
    spec_tables = frozenset(spec.table for spec in _WORK_SPECS)
    missing = spec_tables - available
    if missing not in _KNOWN_PRIOR_SCHEMA_MISSING_TABLE_SETS:
        raise ValueError("joint_schema_invalid")

    incident_rows = {
        "strategy_management_batches": frozenset({batch_row_id}),
        "strategy_management_legs": frozenset({management_leg_row_id}),
        "strategy_management_components": frozenset(component_row_ids),
        "message_instruction_items": frozenset(instruction_item_row_ids),
    }
    cutoff = now - _ACTIVE_WINDOW
    blocking = 0
    for spec in _WORK_SPECS:
        if spec.table not in available or spec.table == _TARGET_TABLE:
            continue
        columns = _table_columns(session, spec.table)
        if not {"id", spec.state_column, spec.time_column}.issubset(columns):
            raise ValueError("joint_schema_invalid")
        safe = _SAFE_NONWRITER_STATES[spec.table]
        placeholders = ",".join(f":safe_{index}" for index, _ in enumerate(safe))
        parameters = {
            f"safe_{index}": state
            for index, state in enumerate(sorted(safe))
        }
        rows = session.execute(
            text(
                f"SELECT id, {_identifier(spec.state_column)}, "
                f"{_identifier(spec.time_column)} "
                f"FROM {_identifier(spec.table)} "
                f"WHERE {_identifier(spec.state_column)} IS NULL OR "
                f"{_identifier(spec.state_column)} NOT IN ({placeholders}) "
                f"LIMIT {_MAX_INSPECTED_ROWS_PER_TABLE + 1}"
            ),
            parameters,
        ).fetchall()
        if len(rows) > _MAX_INSPECTED_ROWS_PER_TABLE:
            raise ValueError("joint_population_invalid")
        allowed_ids = incident_rows.get(spec.table, frozenset())
        known = frozenset(spec.active_states) | frozenset(spec.unknown_states)
        for row_id, state, raw_timestamp in rows:
            if type(row_id) is int and row_id in allowed_ids:
                continue
            if spec.table == _DEEPCOIN_TABLE:
                blocking += 1
                continue
            if state is None or type(state) is not str or state not in known:
                blocking += 1
                continue
            if _parse_writer_timestamp(raw_timestamp) >= cutoff:
                blocking += 1
    return blocking


def inspect_joint_recovery_material_authority(
    database_path: str | Path,
    *,
    phase: str,
    now: datetime | None = None,
) -> JointRecoveryMaterialAuthority:
    """Inspect the one closed incident set in a coherent query-only snapshot."""

    if type(phase) is not str or phase not in _PHASES:
        return _refused("joint_phase_invalid")
    try:
        observed_at = _normalize_now(now)
        factory = create_composite_recovery_read_only_session_factory(
            database_path
        )
        with factory() as session:
            session.execute(text("PRAGMA query_only=ON"))
            query_only = session.execute(text("PRAGMA query_only")).scalar_one()
            if type(query_only) is not int or query_only != 1:
                raise ValueError("joint_query_only_failed")
            session.execute(text("BEGIN"))
            local = _load_batch119_local_material_authority_in_session(session)
            (
                reservations,
                reservation_count,
                reservation_source_material,
            ) = _target_material(session, phase=phase)
            blocking = _blocking_writer_count(
                session,
                now=observed_at,
                batch_row_id=local.batch_row_id,
                management_leg_row_id=local.management_leg_row_id,
                component_row_ids=local.component_row_ids,
                instruction_item_row_ids=local.instruction_item_row_ids,
            )
            material_fingerprint = _fingerprint(
                {
                    "schema_version": 1,
                    "batch119": local.payload,
                    "reservations": reservations,
                    "reservation_source": reservation_source_material,
                }
            )
            session.rollback()
        if blocking != 0:
            return JointRecoveryMaterialAuthority(
                material_fingerprint=material_fingerprint,
                reservation_count=reservation_count,
                batch119_incident_count=1,
                blocking_writer_count=blocking,
                status="refused",
                reason_code="joint_writer_not_quiescent",
            )
        return JointRecoveryMaterialAuthority(
            material_fingerprint=material_fingerprint,
            reservation_count=reservation_count,
            batch119_incident_count=1,
            blocking_writer_count=0,
            status="ready",
            reason_code=None,
        )
    except CompositeBatchRecoveryRefusal:
        return _refused("joint_material_invalid")
    except (
        FileNotFoundError,
        OSError,
        OverflowError,
        RecursionError,
        RuntimeError,
        SQLAlchemyError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        return _refused("joint_read_failed")


JOINT_RECOVERY_PHASES = tuple(sorted(_PHASES))
