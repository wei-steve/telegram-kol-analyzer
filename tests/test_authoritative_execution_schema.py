import importlib
import importlib.util

import pytest
from sqlalchemy import event, inspect, text
from typer.testing import CliRunner

from telegram_kol_research.db import create_session_factory


EXPECTED_TABLES = {
    "authoritative_execution_attempts",
    "entry_assembly_wakeup_executions",
    "recognition_execution_scan_cursors",
}


def _schema_module():
    name = "telegram_kol_research.authoritative_execution_schema"
    assert importlib.util.find_spec(name) is not None, "explicit schema module is missing"
    return importlib.import_module(name)


def test_core_bootstrap_does_not_auto_create_recognition_execution_tables(tmp_path):
    session_factory = create_session_factory(tmp_path / "core-only.db")

    assert EXPECTED_TABLES.isdisjoint(
        inspect(session_factory.kw["bind"]).get_table_names()
    )


def test_explicit_schema_apply_creates_only_three_tables_and_is_idempotent(tmp_path):
    schema = _schema_module()
    session_factory = create_session_factory(tmp_path / "explicit.db")
    engine = session_factory.kw["bind"]
    before = set(inspect(engine).get_table_names())
    plan = schema.build_recognition_execution_schema_plan(engine)

    first = schema.apply_recognition_execution_schema(
        engine,
        expected_plan_sha256=plan.plan_sha256,
    )
    second = schema.apply_recognition_execution_schema(
        engine,
        expected_plan_sha256=plan.plan_sha256,
    )

    after = set(inspect(engine).get_table_names())
    assert after - before == EXPECTED_TABLES
    assert first.created_tables == tuple(sorted(EXPECTED_TABLES))
    assert second.created_tables == ()
    assert schema.validate_recognition_execution_schema(engine).valid is True


def test_explicit_schema_apply_uses_one_immediate_sqlite_transaction(tmp_path):
    schema = _schema_module()
    session_factory = create_session_factory(tmp_path / "immediate.db")
    engine = session_factory.kw["bind"]
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    plan = schema.build_recognition_execution_schema_plan(engine)
    schema.apply_recognition_execution_schema(
        engine,
        expected_plan_sha256=plan.plan_sha256,
    )

    assert statements.count("BEGIN IMMEDIATE") == 1


def test_explicit_schema_has_required_unique_constraints_indexes_and_checks(tmp_path):
    schema = _schema_module()
    session_factory = create_session_factory(tmp_path / "constraints.db")
    engine = session_factory.kw["bind"]
    plan = schema.build_recognition_execution_schema_plan(engine)
    schema.apply_recognition_execution_schema(
        engine,
        expected_plan_sha256=plan.plan_sha256,
    )
    inspector = inspect(engine)

    attempt_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints(
            "authoritative_execution_attempts"
        )
    }
    wake_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints(
            "entry_assembly_wakeup_executions"
        )
    }
    cursor_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints(
            "recognition_execution_scan_cursors"
        )
    }
    assert ("raw_message_id", "authoritative_generation") in attempt_uniques
    assert ("claim_token",) in attempt_uniques
    assert ("entry_assembly_attempt_id", "wake_generation") in wake_uniques
    assert ("claim_token",) in wake_uniques
    assert ("scan_family",) in cursor_uniques

    wake_columns = {
        item["name"] for item in inspector.get_columns(
            "entry_assembly_wakeup_executions"
        )
    }
    assert "result_json" in wake_columns

    attempt_checks = {
        item["name"]
        for item in inspector.get_check_constraints(
            "authoritative_execution_attempts"
        )
    }
    wake_checks = {
        item["name"]
        for item in inspector.get_check_constraints(
            "entry_assembly_wakeup_executions"
        )
    }
    assert "ck_authoritative_execution_attempts_exchange_effect" in attempt_checks
    assert "ck_entry_assembly_wakeup_executions_exchange_effect" in wake_checks

    index_names = {
        item["name"]
        for table in EXPECTED_TABLES
        for item in inspector.get_indexes(table)
    }
    assert {
        "ix_authoritative_execution_attempts_raw_message_id",
        "ix_authoritative_execution_attempts_status_lease",
        "ix_entry_assembly_wakeup_executions_attempt_id",
        "ix_entry_assembly_wakeup_executions_status_lease",
        "ix_recognition_execution_scan_cursors_updated_at",
    } <= index_names

    with engine.connect() as connection:
        with pytest.raises(Exception):
            connection.execute(
                text(
                    "INSERT INTO recognition_execution_scan_cursors "
                    "(scan_family,last_seen_id,pass_generation,version,updated_at) "
                    "VALUES ('bad family',0,0,0,CURRENT_TIMESTAMP)"
                )
            )


def test_runtime_schema_validation_fails_closed_when_any_table_is_missing(tmp_path):
    schema = _schema_module()
    session_factory = create_session_factory(tmp_path / "missing.db")

    with pytest.raises(RuntimeError, match="recognition_execution_schema_invalid"):
        schema.require_recognition_execution_schema(session_factory)


def test_runtime_schema_validation_rejects_same_name_wrong_shape(tmp_path):
    schema = _schema_module()
    session_factory = create_session_factory(tmp_path / "wrong-shape.db")
    engine = session_factory.kw["bind"]
    plan = schema.build_recognition_execution_schema_plan(engine)
    schema.apply_recognition_execution_schema(
        engine,
        expected_plan_sha256=plan.plan_sha256,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP INDEX ix_recognition_execution_scan_cursors_updated_at"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_recognition_execution_scan_cursors_updated_at "
            "ON recognition_execution_scan_cursors(last_seen_id)"
        )

    validation = schema.validate_recognition_execution_schema(engine)

    assert validation.valid is False
    assert any("index_signature" in error for error in validation.errors)


def test_runtime_schema_validation_rejects_extra_column_and_wrong_index_table(
    tmp_path,
):
    schema = _schema_module()
    session_factory = create_session_factory(tmp_path / "extra-shape.db")
    engine = session_factory.kw["bind"]
    plan = schema.build_recognition_execution_schema_plan(engine)
    schema.apply_recognition_execution_schema(
        engine,
        expected_plan_sha256=plan.plan_sha256,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE recognition_execution_scan_cursors "
            "ADD COLUMN unexpected TEXT"
        )
        connection.exec_driver_sql(
            "DROP INDEX ix_recognition_execution_scan_cursors_updated_at"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_recognition_execution_scan_cursors_updated_at "
            "ON authoritative_execution_attempts(updated_at)"
        )

    validation = schema.validate_recognition_execution_schema(engine)

    assert validation.valid is False
    assert "extra_column:recognition_execution_scan_cursors:unexpected" in (
        validation.errors
    )
    assert any("index_signature" in error for error in validation.errors)


def test_schema_apply_requires_exact_canonical_plan_hash(tmp_path):
    schema = _schema_module()
    session_factory = create_session_factory(tmp_path / "wrong-hash.db")

    with pytest.raises(RuntimeError, match="plan_hash_mismatch"):
        schema.apply_recognition_execution_schema(
            session_factory.kw["bind"],
            expected_plan_sha256="0" * 64,
        )
    assert EXPECTED_TABLES.isdisjoint(
        inspect(session_factory.kw["bind"]).get_table_names()
    )


def test_schema_cli_rehearse_is_hash_bound_and_repeatable(tmp_path):
    from telegram_kol_research.cli import app

    database_path = tmp_path / "cli-copy.db"
    session_factory = create_session_factory(database_path)
    plan = _schema_module().build_recognition_execution_schema_plan(
        session_factory.kw["bind"]
    )
    runner = CliRunner()
    args = [
        "recognition-execution-schema",
        "--database-path",
        str(database_path),
        "--mode",
        "rehearse",
        "--expected-plan-sha256",
        plan.plan_sha256,
    ]

    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert '"changed": true' in first.output
    assert '"changed": false' in second.output
