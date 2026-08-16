"""Closed, read-only material authority for the joint recovery incident."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
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
    ACTIVE_CLOSE_RESERVATION_STATUSES,
    MAX_RECOVERY_PLAN_BYTES,
    _BOUND_CLOSE_RESERVATION_AUDIT_ACTION,
    _REQUIRED_SOURCE_COLUMNS,
    _bounded_recovery_json_tree,
    _closed_json_object,
    _durable_invariant_fingerprints,
    _exact_object_keys,
    _load_bound_close_reservation_audits,
    _load_source_descendants,
    _parse_sqlite_utc,
    _redacted_ref,
    _reject_json_constant,
    _require_lower_hex_64,
)
from telegram_kol_research.composite_management_batch_recovery import (
    CompositeBatchRecoveryRefusal,
    _batch119_material_row_payload,
    _fingerprint,
    _load_exact_batch119_recovery_source,
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
_CONFIRMED_RESERVATION_STATUS = "confirmed"


@dataclass(frozen=True, slots=True)
class JointRecoveryMaterialAuthority:
    material_fingerprint: str
    batch119_material_fingerprint: str
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
        batch119_material_fingerprint=_fingerprint(
            {"schema_version": 1, "batch119_status": "unavailable"}
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


def _post_target_rows(
    session,
    rows: list[dict],
) -> tuple[
    list[dict],
    dict[int, str],
    dict[str, object],
    dict[str, str],
]:
    connection = session.connection().connection.driver_connection
    if type(connection) is not sqlite3.Connection:
        raise ValueError("joint_database_invalid")
    previous_row_factory = connection.row_factory
    try:
        connection.row_factory = sqlite3.Row
        audits = _load_bound_close_reservation_audits(connection)
    finally:
        connection.row_factory = previous_row_factory
    if len(audits) != 1:
        raise ValueError("joint_population_invalid")
    audit = audits[0]
    if not (
        type(audit["id"]) is int
        and audit["id"] > 0
        and audit["execution_binding_id"] is None
        and audit["venue"] == "deepcoin"
        and audit["action"] == _BOUND_CLOSE_RESERVATION_AUDIT_ACTION
        and audit["status"] == "succeeded"
        and audit["order_id"] is None
        and audit["client_order_id"] is None
        and audit["pos_id"] is None
        and audit["related_order_id"] is None
        and audit["request_json"] is None
        and audit["response_json"] is None
        and type(audit["notification_attempts"]) is int
        and audit["notification_attempts"] == 0
        and _parse_sqlite_utc(audit["created_at"]) is not None
    ):
        raise ValueError("joint_population_invalid")
    evidence_fingerprint = _require_lower_hex_64(
        audit["notification_fingerprint"],
        "notification_fingerprint",
    )
    expected_before_keys = frozenset(
        {"action_count", "evidence_fingerprint", "reservations"}
    )
    expected_after_keys = frozenset(
        {"action_count", "evidence_fingerprint", "reservations", "status"}
    )
    expected_item_keys = frozenset(
        {"durable_invariant_fingerprint", "reservation_ref", "status"}
    )
    documents = (audit["before_json"], audit["after_json"])
    if any(
        type(document) is not str
        or len(document.encode("utf-8")) > MAX_RECOVERY_PLAN_BYTES
        for document in documents
    ):
        raise ValueError("joint_population_invalid")
    before = json.loads(
        documents[0],
        object_pairs_hook=_closed_json_object,
        parse_constant=_reject_json_constant,
    )
    after = json.loads(
        documents[1],
        object_pairs_hook=_closed_json_object,
        parse_constant=_reject_json_constant,
    )
    _bounded_recovery_json_tree(before)
    _bounded_recovery_json_tree(after)
    before = _exact_object_keys(before, expected_before_keys, "audit.before")
    after = _exact_object_keys(after, expected_after_keys, "audit.after")
    if (
        type(before["action_count"]) is not int
        or before["action_count"] != _EXPECTED_RESERVATION_COUNT
        or type(after["action_count"]) is not int
        or after["action_count"] != _EXPECTED_RESERVATION_COUNT
        or before["evidence_fingerprint"] != evidence_fingerprint
        or after["evidence_fingerprint"] != evidence_fingerprint
        or after["status"] != _CONFIRMED_RESERVATION_STATUS
        or type(before["reservations"]) is not list
        or type(after["reservations"]) is not list
        or len(before["reservations"]) != _EXPECTED_RESERVATION_COUNT
        or len(after["reservations"]) != _EXPECTED_RESERVATION_COUNT
    ):
        raise ValueError("joint_population_invalid")
    source_status_by_ref: dict[str, str] = {}
    invariant_by_ref: dict[str, str] = {}
    ordered_refs: list[str] = []
    for raw_item in before["reservations"]:
        item = _exact_object_keys(raw_item, expected_item_keys, "audit.before.item")
        reference = _require_lower_hex_64(item["reservation_ref"], "reservation_ref")
        invariant = _require_lower_hex_64(
            item["durable_invariant_fingerprint"],
            "durable_invariant_fingerprint",
        )
        status = item["status"]
        if (
            type(status) is not str
            or status not in ACTIVE_CLOSE_RESERVATION_STATUSES
            or reference in source_status_by_ref
        ):
            raise ValueError("joint_population_invalid")
        source_status_by_ref[reference] = status
        invariant_by_ref[reference] = invariant
        ordered_refs.append(reference)
    after_refs: list[str] = []
    for raw_item in after["reservations"]:
        item = _exact_object_keys(raw_item, expected_item_keys, "audit.after.item")
        reference = _require_lower_hex_64(item["reservation_ref"], "reservation_ref")
        invariant = _require_lower_hex_64(
            item["durable_invariant_fingerprint"],
            "durable_invariant_fingerprint",
        )
        if (
            item["status"] != _CONFIRMED_RESERVATION_STATUS
            or invariant_by_ref.get(reference) != invariant
        ):
            raise ValueError("joint_population_invalid")
        after_refs.append(reference)
    if after_refs != ordered_refs:
        raise ValueError("joint_population_invalid")
    candidates = {
        _redacted_ref("reservation", row["id"]): row
        for row in rows
        if row["status"] == _CONFIRMED_RESERVATION_STATUS
        and row["last_error"] is None
        and row["updated_at"] == audit["created_at"]
    }
    if set(candidates) != set(ordered_refs):
        raise ValueError("joint_population_invalid")
    target_rows = [candidates[reference] for reference in ordered_refs]
    source_status_by_id = {
        int(row["id"]): source_status_by_ref[reference]
        for reference, row in zip(ordered_refs, target_rows, strict=True)
    }
    audit_material = {key: audit[key] for key in audit.keys()}
    return (
        target_rows,
        source_status_by_id,
        audit_material,
        invariant_by_ref,
    )


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
    row_material = [
        {name: row[name] for name in sorted(columns)} for row in rows
    ]
    post_audit_material: dict[str, object] | None = None
    post_invariant_by_ref: dict[str, str] | None = None
    source_status_overrides: dict[int, str] | None = None
    if phase == BOUND_APPLY_POST:
        (
            target_rows,
            source_status_overrides,
            post_audit_material,
            post_invariant_by_ref,
        ) = _post_target_rows(session, row_material)
        target_ids = {int(row["id"]) for row in target_rows}
        confirmed_residue = [
            row for row in row_material if int(row["id"]) not in target_ids
        ]
    else:
        target_rows = [
            row for row in row_material if row["status"] in _TARGET_STATES
        ]
        confirmed_residue = [
            row
            for row in row_material
            if row["status"] == _CONFIRMED_RESERVATION_STATUS
        ]
    if (
        len(target_rows) != _EXPECTED_RESERVATION_COUNT
        or len(target_rows) + len(confirmed_residue) != len(row_material)
        or any(
            row["status"] != _CONFIRMED_RESERVATION_STATUS
            for row in confirmed_residue
        )
    ):
        raise ValueError("joint_population_invalid")
    material = target_rows
    driver_connection = session.connection().connection.driver_connection
    if type(driver_connection) is not sqlite3.Connection:
        raise ValueError("joint_database_invalid")
    previous_row_factory = driver_connection.row_factory
    try:
        driver_connection.row_factory = sqlite3.Row
        target_ids = tuple(int(row["id"]) for row in target_rows)
        placeholders = ",".join("?" for _ in target_ids)
        source_rows = driver_connection.execute(
            f"""
            SELECT id, pos_id, execution_binding_id, status, last_error,
                   created_at, updated_at
            FROM bound_position_close_reservations
            WHERE id IN ({placeholders})
            ORDER BY id
            """,
            target_ids,
        ).fetchall()
        source = _load_source_descendants(
            driver_connection,
            source_rows,
            source_status_overrides=source_status_overrides,
        )
        if (
            len(source.reservations) != _EXPECTED_RESERVATION_COUNT
            or any(
                item.local_reason_code is not None
                for item in source.reservations
            )
        ):
            raise ValueError("joint_population_invalid")
        if post_invariant_by_ref is not None:
            try:
                raw_rows = tuple(
                    (
                        reservation.reservation_ref,
                        source._capability._get(reservation.reservation_ref),
                    )
                    for reservation in source.reservations
                )
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError("joint_population_invalid") from exc
            if (
                _durable_invariant_fingerprints(
                    driver_connection,
                    raw_rows=raw_rows,
                )
                != post_invariant_by_ref
            ):
                raise ValueError("joint_population_invalid")
    finally:
        driver_connection.row_factory = previous_row_factory
    binding_ids = tuple(int(row["execution_binding_id"]) for row in target_rows)
    position_ids = tuple(str(row["pos_id"]) for row in target_rows)

    def complete_rows(query) -> list[dict[str, object]]:
        selected = query.limit(257).all()
        if len(selected) > 256:
            raise ValueError("joint_population_invalid")
        return [_batch119_material_row_payload(row) for row in selected]

    descendant_material: dict[str, object] = {
        "source_fingerprint": str(source.source_fingerprint),
        "confirmed_residue": confirmed_residue,
        "post_audit": post_audit_material,
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
            exact_source = _load_exact_batch119_recovery_source(
                session,
                target_instruction_only=True,
            )
            if exact_source is None:
                raise CompositeBatchRecoveryRefusal("durable_evidence_invalid")
            local = _load_batch119_local_material_authority_in_session(
                session,
                validated_source=exact_source,
                target_instruction_only=True,
            )
            try:
                (
                    reservations,
                    reservation_count,
                    reservation_source_material,
                ) = _target_material(session, phase=phase)
            except (RecursionError, RuntimeError, TypeError, ValueError) as exc:
                raise CompositeBatchRecoveryRefusal(
                    "joint_material_invalid"
                ) from exc
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
            batch119_material_fingerprint = _fingerprint(
                {"schema_version": 1, "batch119": local.payload}
            )
            session.rollback()
        if blocking != 0:
            return JointRecoveryMaterialAuthority(
                material_fingerprint=material_fingerprint,
                batch119_material_fingerprint=batch119_material_fingerprint,
                reservation_count=reservation_count,
                batch119_incident_count=1,
                blocking_writer_count=blocking,
                status="refused",
                reason_code="joint_writer_not_quiescent",
            )
        return JointRecoveryMaterialAuthority(
            material_fingerprint=material_fingerprint,
            batch119_material_fingerprint=batch119_material_fingerprint,
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
