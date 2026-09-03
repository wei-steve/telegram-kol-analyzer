"""Explicit additive schema plan for recognition execution ownership fences."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import CheckConstraint, UniqueConstraint, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.schema import CreateIndex, CreateTable

from telegram_kol_research.db import EXPLICIT_RECOGNITION_EXECUTION_TABLES
from telegram_kol_research.models import Base


REQUIRED_TABLES = tuple(sorted(EXPLICIT_RECOGNITION_EXECUTION_TABLES))
REQUIRED_INDEXES = frozenset(
    {
        "ix_authoritative_execution_attempts_raw_message_id",
        "ix_authoritative_execution_attempts_status_lease",
        "ix_entry_assembly_wakeup_executions_attempt_id",
        "ix_entry_assembly_wakeup_executions_status_lease",
        "ix_recognition_execution_scan_cursors_updated_at",
    }
)
REQUIRED_UNIQUES = {
    "authoritative_execution_attempts": frozenset(
        {
            "uq_authoritative_execution_attempts_generation",
            "uq_authoritative_execution_attempts_claim_token",
        }
    ),
    "entry_assembly_wakeup_executions": frozenset(
        {
            "uq_entry_assembly_wakeup_executions_generation",
            "uq_entry_assembly_wakeup_executions_claim_token",
        }
    ),
    "recognition_execution_scan_cursors": frozenset(
        {"uq_recognition_execution_scan_cursors_family"}
    ),
}
REQUIRED_CHECKS = {
    "authoritative_execution_attempts": frozenset(
        {
            "ck_authoritative_execution_attempts_status",
            "ck_authoritative_execution_attempts_owner_role",
            "ck_authoritative_execution_attempts_exchange_effect",
        }
    ),
    "entry_assembly_wakeup_executions": frozenset(
        {
            "ck_entry_assembly_wakeup_executions_status",
            "ck_entry_assembly_wakeup_executions_owner_role",
            "ck_entry_assembly_wakeup_executions_exchange_effect",
        }
    ),
    "recognition_execution_scan_cursors": frozenset(
        {"ck_recognition_execution_scan_cursors_family"}
    ),
}
REQUIRED_FOREIGN_KEYS = {
    "authoritative_execution_attempts": frozenset(
        {("raw_message_id", "raw_messages", "id")}
    ),
    "entry_assembly_wakeup_executions": frozenset(
        {
            ("entry_assembly_attempt_id", "entry_assembly_attempts", "id"),
            ("strategy_raw_message_id", "raw_messages", "id"),
            ("trigger_raw_message_id", "raw_messages", "id"),
        }
    ),
    "recognition_execution_scan_cursors": frozenset(),
}


@dataclass(frozen=True)
class RecognitionExecutionSchemaPlan:
    plan_sha256: str
    table_names: tuple[str, ...]
    ddl_statements: tuple[str, ...]


@dataclass(frozen=True)
class RecognitionExecutionSchemaApplyResult:
    plan_sha256: str
    created_tables: tuple[str, ...]


@dataclass(frozen=True)
class RecognitionExecutionSchemaValidation:
    valid: bool
    errors: tuple[str, ...]


def _engine(value: Any) -> Engine:
    if isinstance(value, Engine):
        return value
    bind = getattr(value, "kw", {}).get("bind")
    if isinstance(bind, Engine):
        return bind
    raise TypeError("an Engine or sessionmaker is required")


def build_recognition_execution_schema_plan(
    engine: Engine,
) -> RecognitionExecutionSchemaPlan:
    statements: list[str] = []
    for name in REQUIRED_TABLES:
        table = Base.metadata.tables[name]
        statements.append(str(CreateTable(table).compile(dialect=engine.dialect)))
        for index in sorted(table.indexes, key=lambda item: str(item.name)):
            statements.append(str(CreateIndex(index).compile(dialect=engine.dialect)))
    canonical = json.dumps(
        {"tables": REQUIRED_TABLES, "ddl": statements},
        sort_keys=True,
        separators=(",", ":"),
    )
    return RecognitionExecutionSchemaPlan(
        plan_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        table_names=REQUIRED_TABLES,
        ddl_statements=tuple(statements),
    )


def _validation(connection: Connection) -> RecognitionExecutionSchemaValidation:
    inspector = inspect(connection)
    errors: list[str] = []
    existing = set(inspector.get_table_names())
    for name in REQUIRED_TABLES:
        if name not in existing:
            errors.append(f"missing_table:{name}")
            continue
        table = Base.metadata.tables[name]
        observed_columns = {
            str(item.get("name") or ""): item
            for item in inspector.get_columns(name)
        }
        expected_column_names = {column.name for column in table.columns}
        for extra in set(observed_columns) - expected_column_names:
            errors.append(f"extra_column:{name}:{extra}")
        for column in table.columns:
            observed = observed_columns.get(column.name)
            if observed is None:
                errors.append(f"missing_column:{name}:{column.name}")
                continue
            expected_type = str(column.type.compile(dialect=connection.dialect)).upper()
            observed_type = str(observed.get("type")).upper()
            if observed_type != expected_type:
                errors.append(
                    f"column_type:{name}:{column.name}:{observed_type}:{expected_type}"
                )
            if bool(observed.get("nullable")) != bool(column.nullable):
                errors.append(f"column_nullable:{name}:{column.name}")
            if bool(observed.get("primary_key")) != bool(column.primary_key):
                errors.append(f"column_primary_key:{name}:{column.name}")
            expected_default = (
                str(column.server_default.arg) if column.server_default is not None else None
            )
            observed_default = observed.get("default")
            if _normalize_sql(observed_default) != _normalize_sql(expected_default):
                errors.append(f"column_default:{name}:{column.name}")
        observed_uniques = {
            str(item.get("name") or ""): tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(name)
        }
        expected_uniques = {
            str(constraint.name): tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        if observed_uniques != expected_uniques:
            errors.append(f"unique_signature:{name}")
        observed_checks = {
            str(item.get("name") or ""): _normalize_sql(item.get("sqltext"))
            for item in inspector.get_check_constraints(name)
        }
        expected_checks = {
            str(constraint.name): _normalize_sql(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        if observed_checks != expected_checks:
            errors.append(f"check_signature:{name}")
        observed_foreign_keys = {
            (
                tuple(str(value) for value in item.get("constrained_columns") or ()),
                str(item.get("referred_table") or ""),
                tuple(str(value) for value in item.get("referred_columns") or ()),
                _normalize_sql((item.get("options") or {}).get("ondelete")),
                _normalize_sql((item.get("options") or {}).get("onupdate")),
            )
            for item in inspector.get_foreign_keys(name)
        }
        expected_foreign_keys = {
            (
                tuple(element.parent.name for element in constraint.elements),
                next(iter(constraint.elements)).column.table.name,
                tuple(element.column.name for element in constraint.elements),
                _normalize_sql(constraint.ondelete),
                _normalize_sql(constraint.onupdate),
            )
            for constraint in table.foreign_key_constraints
        }
        if observed_foreign_keys != expected_foreign_keys:
            errors.append(f"foreign_key_signature:{name}")
        observed_indexes = {
            str(item.get("name") or ""): (
                tuple(item.get("column_names") or ()),
                bool(item.get("unique")),
            )
            for item in inspector.get_indexes(name)
        }
        expected_indexes = {
            str(index.name): (
                tuple(column.name for column in index.columns),
                bool(index.unique),
            )
            for index in table.indexes
        }
        if observed_indexes != expected_indexes:
            errors.append(f"index_signature:{name}")
    return RecognitionExecutionSchemaValidation(not errors, tuple(sorted(errors)))


def _normalize_sql(value: Any) -> str:
    if value is None:
        return ""
    return "".join(str(value).replace('"', "").replace("`", "").split()).lower()


def validate_recognition_execution_schema(
    engine_or_session_factory: Any,
) -> RecognitionExecutionSchemaValidation:
    engine = _engine(engine_or_session_factory)
    with engine.connect() as connection:
        return _validation(connection)


def require_recognition_execution_schema(engine_or_session_factory: Any) -> None:
    validation = validate_recognition_execution_schema(engine_or_session_factory)
    if not validation.valid:
        raise RuntimeError(
            "recognition_execution_schema_invalid:"
            + ",".join(validation.errors)
        )


def apply_recognition_execution_schema(
    engine: Engine,
    *,
    expected_plan_sha256: str,
) -> RecognitionExecutionSchemaApplyResult:
    plan = build_recognition_execution_schema_plan(engine)
    if str(expected_plan_sha256) != plan.plan_sha256:
        raise RuntimeError("recognition_execution_schema_plan_hash_mismatch")
    before = set(inspect(engine).get_table_names())
    connection = engine.connect()
    try:
        if engine.dialect.name != "sqlite":
            raise RuntimeError("recognition_execution_schema_requires_sqlite")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        for name in REQUIRED_TABLES:
            Base.metadata.tables[name].create(connection, checkfirst=True)
        validation = _validation(connection)
        if not validation.valid:
            raise RuntimeError(
                "recognition_execution_schema_invalid:"
                + ",".join(validation.errors)
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    after = set(inspect(engine).get_table_names())
    return RecognitionExecutionSchemaApplyResult(
        plan_sha256=plan.plan_sha256,
        created_tables=tuple(sorted((after - before) & set(REQUIRED_TABLES))),
    )
