import sqlite3

from sqlalchemy import inspect

from telegram_kol_research.db import (
    SQLITE_COMPAT_COLUMNS,
    SQLITE_COMPAT_INDEXES,
    create_session_factory,
)


def test_context_resolution_schema_is_created(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    inspector = inspect(session_factory.kw["bind"])

    assert inspector.has_table("message_evidence_versions")
    assert inspector.has_table("message_evidence_extraction_claims")
    assert inspector.has_table("strategy_threads")
    assert inspector.has_table("strategy_message_links")
    assert inspector.has_table("context_resolution_attempts")
    assert inspector.has_table("runtime_incidents")
    assert inspector.has_table("runtime_incident_observations")
    assert inspector.has_table("runtime_agent_recovery_attempts")
    assert inspector.has_table("runtime_agent_investigation_audits")
    assert inspector.has_table("message_operation_contracts")
    assert inspector.has_table("message_operation_items")
    assert inspector.has_table("message_operation_stage1_notifications")
    assert inspector.has_table("runtime_incident_handoff_artifacts")
    assert inspector.has_table("position_protection_health_observations")
    assert inspector.has_table("management_message_envelopes")
    assert inspector.has_table("management_message_targets")
    assert inspector.has_table("instruction_execution_contracts")
    assert inspector.has_table("instruction_execution_transitions")
    assert inspector.has_table("mimo_recognition_runs")
    assert inspector.has_table("mimo_recognition_attempts")
    assert inspector.has_table("mimo_contract_circuit_state")
    assert "strategy_thread_id" in {
        column["name"]
        for column in inspector.get_columns("strategy_lifecycles")
    }


def test_mimo_recognition_audit_schema_is_additive_and_indexed(tmp_path):
    database_path = tmp_path / "legacy-mimo-audit.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE sources "
            "(id INTEGER PRIMARY KEY, display_name VARCHAR(255) NOT NULL)"
        )
        connection.execute(
            "INSERT INTO sources (id, display_name) VALUES (97, 'Legacy Source')"
        )

    session_factory = create_session_factory(database_path)
    inspector = inspect(session_factory.kw["bind"])

    assert inspector.has_table("mimo_recognition_runs")
    assert inspector.has_table("mimo_recognition_attempts")
    run_columns = {
        column["name"]
        for column in inspector.get_columns("mimo_recognition_runs")
    }
    assert {
        "raw_message_id",
        "run_kind",
        "contract_version",
        "model",
        "input_kind",
        "input_fingerprint",
        "prompt_versions_json",
        "status",
        "attempt_count",
        "retry_of_run_id",
        "selected_attempt_ordinal",
        "final_error_code",
        "final_error_message",
        "became_authoritative",
        "canonical_payload_fingerprint",
        "projection_fingerprint",
        "started_at",
        "completed_at",
        "created_at",
    } <= run_columns
    attempt_columns = {
        column["name"]
        for column in inspector.get_columns("mimo_recognition_attempts")
    }
    assert {
        "run_id",
        "ordinal",
        "retry_of_ordinal",
        "status",
        "error_code",
        "error_message",
        "response_fingerprint",
        "started_at",
        "completed_at",
        "duration_ms",
        "created_at",
    } <= attempt_columns
    run_indexes = {
        index["name"]
        for index in inspector.get_indexes("mimo_recognition_runs")
    }
    attempt_indexes = {
        index["name"]
        for index in inspector.get_indexes("mimo_recognition_attempts")
    }
    assert {
        "ix_mimo_recognition_runs_message_status_created",
        "ix_mimo_recognition_runs_status_created",
    } <= run_indexes
    assert {
        "ix_mimo_recognition_attempts_run_created",
        "ix_mimo_recognition_attempts_status_created",
    } <= attempt_indexes
    assert any(
        constraint["column_names"] == ["run_id", "ordinal"]
        for constraint in inspector.get_unique_constraints(
            "mimo_recognition_attempts"
        )
    )
    assert {
        "ix_mimo_recognition_runs_message_status_created",
        "ix_mimo_recognition_runs_status_created",
        "ix_mimo_recognition_attempts_run_created",
        "ix_mimo_recognition_attempts_status_created",
        "ix_message_evidence_versions_mimo_recognition_run_id",
    } <= set(SQLITE_COMPAT_INDEXES)
    evidence_columns = {
        column["name"]
        for column in inspector.get_columns("message_evidence_versions")
    }
    evidence_indexes = {
        index["name"]
        for index in inspector.get_indexes("message_evidence_versions")
    }
    assert "mimo_recognition_run_id" in evidence_columns
    assert "ix_message_evidence_versions_mimo_recognition_run_id" in (
        evidence_indexes
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT id, display_name FROM sources WHERE id = 97"
        ).fetchone() == (97, "Legacy Source")


def test_mimo_evidence_run_link_is_added_to_existing_evidence_table(tmp_path):
    database_path = tmp_path / "legacy-message-evidence.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE message_evidence_versions (id INTEGER PRIMARY KEY)"
        )

    session_factory = create_session_factory(database_path)
    inspector = inspect(session_factory.kw["bind"])

    assert "mimo_recognition_run_id" in {
        column["name"]
        for column in inspector.get_columns("message_evidence_versions")
    }
    assert "ix_message_evidence_versions_mimo_recognition_run_id" in {
        index["name"]
        for index in inspector.get_indexes("message_evidence_versions")
    }
    assert any(
        foreign_key["constrained_columns"] == ["mimo_recognition_run_id"]
        and foreign_key["referred_table"] == "mimo_recognition_runs"
        for foreign_key in inspector.get_foreign_keys(
            "message_evidence_versions"
        )
    )


def test_mimo_contract_circuit_state_schema_is_durable_and_bounded(tmp_path):
    database_path = tmp_path / "legacy-mimo-circuit.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE sources "
            "(id INTEGER PRIMARY KEY, display_name VARCHAR(255) NOT NULL)"
        )
        connection.execute(
            "INSERT INTO sources (id, display_name) VALUES (97, 'Legacy Source')"
        )

    session_factory = create_session_factory(database_path)
    inspector = inspect(session_factory.kw["bind"])

    assert inspector.has_table("mimo_contract_circuit_state")
    columns = {
        column["name"]
        for column in inspector.get_columns("mimo_contract_circuit_state")
    }
    assert {
        "id",
        "consecutive_transport_failures",
        "is_open",
        "opened_reason",
        "opened_at",
        "last_success_at",
        "updated_at",
    } <= columns
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            == 1
        )


def test_execution_contract_schema_bootstrap_is_idempotent_on_legacy_database(
    tmp_path,
):
    database_path = tmp_path / "legacy-execution-contract.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE sources "
            "(id INTEGER PRIMARY KEY, display_name VARCHAR(255) NOT NULL)"
        )
        connection.execute(
            "INSERT INTO sources (id, display_name) VALUES (77, 'Legacy Source')"
        )

    first_factory = create_session_factory(database_path)
    first_inspector = inspect(first_factory.kw["bind"])
    first_tables = set(first_inspector.get_table_names())
    first_indexes = {
        table: {index["name"] for index in first_inspector.get_indexes(table)}
        for table in (
            "instruction_execution_contracts",
            "instruction_execution_transitions",
        )
    }
    with sqlite3.connect(database_path) as connection:
        first_source_count = connection.execute(
            "SELECT COUNT(*) FROM sources"
        ).fetchone()[0]

    second_factory = create_session_factory(database_path)
    second_inspector = inspect(second_factory.kw["bind"])

    assert set(second_inspector.get_table_names()) == first_tables
    assert {
        table: {index["name"] for index in second_inspector.get_indexes(table)}
        for table in first_indexes
    } == first_indexes
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == (
            first_source_count
        )
    assert {
        "uq_instruction_execution_contracts_item",
        "ix_instruction_execution_contracts_state_deadline",
        "ix_instruction_execution_contracts_strategy_instance",
        "uq_instruction_execution_transitions_contract_version",
        "ix_instruction_execution_transitions_contract_created",
    } <= set(SQLITE_COMPAT_INDEXES)


def test_runtime_agent_investigation_audit_has_additive_bounded_shape(tmp_path):
    session_factory = create_session_factory(tmp_path / "broker-audit-schema.db")
    inspector = inspect(session_factory.kw["bind"])

    columns = {
        column["name"]
        for column in inspector.get_columns("runtime_agent_investigation_audits")
    }
    assert {
        "runtime_incident_id",
        "evidence_kind",
        "arguments_fingerprint",
        "result_status",
        "evidence_reference",
        "result_bytes",
        "duration_ms",
        "denial_code",
        "created_at",
    } <= columns
    indexes = {
        index["name"]
        for index in inspector.get_indexes("runtime_agent_investigation_audits")
    }
    assert "ix_runtime_agent_investigation_incident_created" in indexes
    assert "ix_runtime_agent_investigation_status_created" in indexes


def test_message_operation_stage1_outbox_has_additive_bounded_shape(tmp_path):
    session_factory = create_session_factory(tmp_path / "stage1-schema.db")
    inspector = inspect(session_factory.kw["bind"])

    columns = {
        column["name"]
        for column in inspector.get_columns(
            "message_operation_stage1_notifications"
        )
    }
    assert {
        "runtime_incident_id",
        "raw_message_id",
        "notification_kind",
        "status",
        "claim_token",
        "claimed_at",
        "attempt_count",
        "next_attempt_at",
        "telegram_message_id",
        "delivered_at",
        "error_code",
        "created_at",
        "updated_at",
    } <= columns
    indexes = {
        index["name"]
        for index in inspector.get_indexes(
            "message_operation_stage1_notifications"
        )
    }
    assert "uq_message_operation_stage1_identity" in indexes
    assert "ix_message_operation_stage1_claimable" in indexes


def test_runtime_incident_handoff_artifact_has_additive_revisioned_shape(tmp_path):
    session_factory = create_session_factory(tmp_path / "handoff-schema.db")
    inspector = inspect(session_factory.kw["bind"])

    columns = {
        column["name"]
        for column in inspector.get_columns("runtime_incident_handoff_artifacts")
    }
    assert {
        "runtime_incident_id",
        "diagnosis_revision",
        "outcome_kind",
        "content_json",
        "codex_prompt",
        "evidence_document_json",
        "content_fingerprint",
        "status",
        "claim_token",
        "claimed_at",
        "attempt_count",
        "next_attempt_at",
        "telegram_message_id",
        "telegram_document_message_id",
        "delivered_at",
        "error_code",
        "created_at",
        "updated_at",
    } <= columns
    indexes = {
        index["name"]
        for index in inspector.get_indexes("runtime_incident_handoff_artifacts")
    }
    assert "uq_runtime_incident_handoff_revision" in indexes
    assert "ix_runtime_incident_handoff_claimable" in indexes


def test_position_protection_health_observation_schema_is_append_only_shape(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-health.db")
    inspector = inspect(session_factory.kw["bind"])

    columns = {
        column["name"]
        for column in inspector.get_columns(
            "position_protection_health_observations"
        )
    }
    assert {
        "venue",
        "execution_binding_id",
        "execution_order_leg_id",
        "pos_id",
        "classification",
        "evidence_fingerprint",
        "exchange_snapshot_fingerprint",
        "source_incident_ids_json",
        "summary_json",
        "observed_at",
    } <= columns
    indexes = {
        index["name"]
        for index in inspector.get_indexes(
            "position_protection_health_observations"
        )
    }
    assert {
        "ix_position_protection_health_scope_observed",
        "ix_position_protection_health_evidence",
    } <= indexes


def test_position_protection_health_table_is_added_without_rewriting_legacy_rows(
    tmp_path,
):
    database_path = tmp_path / "legacy-protection-health.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE sources "
            "(id INTEGER PRIMARY KEY, display_name VARCHAR(255) NOT NULL)"
        )
        connection.execute(
            "INSERT INTO sources (id, display_name) VALUES (91, 'Legacy Source')"
        )

    session_factory = create_session_factory(database_path)
    inspector = inspect(session_factory.kw["bind"])

    assert inspector.has_table("position_protection_health_observations")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT id, display_name FROM sources WHERE id = 91"
        ).fetchone() == (91, "Legacy Source")


def test_multi_target_management_schema_is_idempotent(tmp_path):
    database_path = tmp_path / "multi-target.db"

    first_factory = create_session_factory(database_path)
    second_factory = create_session_factory(database_path)
    inspector = inspect(second_factory.kw["bind"])

    assert inspector.has_table("management_message_envelopes")
    assert inspector.has_table("management_message_targets")
    assert {
        index["name"]
        for index in inspector.get_indexes("management_message_envelopes")
    } >= {"uq_management_message_envelopes_decision"}
    assert {
        index["name"]
        for index in inspector.get_indexes("management_message_targets")
    } >= {"uq_management_message_targets_idempotency"}
    assert first_factory.kw["bind"].url.database == str(database_path)


def test_old_lifecycle_table_has_compatible_thread_column_migration():
    statement = SQLITE_COMPAT_COLUMNS["strategy_lifecycles"][
        "strategy_thread_id"
    ]

    assert statement == (
        "ALTER TABLE strategy_lifecycles "
        "ADD COLUMN strategy_thread_id INTEGER"
    )


def test_context_resolution_rejected_diagnostic_has_additive_compat_migration(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "context-diagnostic.db")
    columns = {
        column["name"]
        for column in inspect(session_factory.kw["bind"]).get_columns(
            "context_resolution_attempts"
        )
    }

    assert "rejected_response_diagnostic_json" in columns
    assert (
        SQLITE_COMPAT_COLUMNS["context_resolution_attempts"][
            "rejected_response_diagnostic_json"
        ]
        == "ALTER TABLE context_resolution_attempts "
        "ADD COLUMN rejected_response_diagnostic_json TEXT"
    )


def test_context_resolution_rejected_diagnostic_migrates_without_rewriting_rows(
    tmp_path,
):
    database_path = tmp_path / "legacy-context-resolution.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE context_resolution_attempts "
            "(id INTEGER PRIMARY KEY, status VARCHAR(32) NOT NULL)"
        )
        connection.execute(
            "INSERT INTO context_resolution_attempts (id, status) "
            "VALUES (81, 'exhausted')"
        )

    session_factory = create_session_factory(database_path)
    columns = {
        column["name"]
        for column in inspect(session_factory.kw["bind"]).get_columns(
            "context_resolution_attempts"
        )
    }

    assert "rejected_response_diagnostic_json" in columns
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT id, status, rejected_response_diagnostic_json "
            "FROM context_resolution_attempts WHERE id = 81"
        ).fetchone() == (81, "exhausted", None)


def test_runtime_incident_table_is_added_to_an_existing_database(tmp_path):
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_sender_id INTEGER,
                chat_id INTEGER,
                username VARCHAR(255),
                display_name VARCHAR(255) NOT NULL,
                custom_label VARCHAR(255),
                is_active BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO sources (
                telegram_sender_id, chat_id, username, display_name,
                custom_label, is_active, created_at
            ) VALUES (7, 9, 'legacy', 'Legacy Source', NULL, 1, CURRENT_TIMESTAMP)
            """
        )

    session_factory = create_session_factory(database_path)
    inspector = inspect(session_factory.kw["bind"])

    assert inspector.has_table("runtime_incidents")
    assert inspector.has_table("message_operation_contracts")
    assert inspector.has_table("message_operation_items")
    assert inspector.has_table("message_operation_stage1_notifications")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT display_name FROM sources WHERE telegram_sender_id = 7"
        ).fetchone() == ("Legacy Source",)


def test_runtime_incident_agent_retry_columns_have_additive_compat_migrations():
    statements = SQLITE_COMPAT_COLUMNS["runtime_incidents"]

    assert "agent_attempt_count" in statements
    assert "agent_next_attempt_at" in statements
    assert "ADD COLUMN agent_attempt_count INTEGER NOT NULL DEFAULT 0" in (
        statements["agent_attempt_count"]
    )
    assert "ADD COLUMN agent_next_attempt_at DATETIME" in (
        statements["agent_next_attempt_at"]
    )


def test_trigger_protection_recovery_columns_are_added_without_rewriting_rows(
    tmp_path,
):
    database_path = tmp_path / "legacy-trigger-protection.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE trigger_protection_intents (
                id INTEGER PRIMARY KEY,
                venue VARCHAR(64) NOT NULL,
                execution_order_leg_id INTEGER NOT NULL,
                parent_trigger_order_id VARCHAR(255),
                adopted_order_id VARCHAR(255),
                recovery_state VARCHAR(32) NOT NULL,
                next_attempt_at DATETIME
            )
            """
        )
        connection.execute(
            "INSERT INTO trigger_protection_intents "
            "(id, venue, execution_order_leg_id, recovery_state) "
            "VALUES (81, 'deepcoin', 434, 'failed')"
        )

    session_factory = create_session_factory(database_path)
    inspector = inspect(session_factory.kw["bind"])
    columns = {
        column["name"]: str(column["type"])
        for column in inspector.get_columns("trigger_protection_intents")
    }

    assert columns["last_reason_code"] == "VARCHAR(128)"
    assert columns["recovery_disposition"] == "VARCHAR(32)"
    assert columns["last_evidence_json"] == "TEXT"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT id, recovery_state FROM trigger_protection_intents WHERE id = 81"
        ).fetchone() == (81, "failed")


def test_composite_management_schema_is_added_to_existing_database(tmp_path):
    database_path = tmp_path / "legacy-composite.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE signal_candidates (id INTEGER PRIMARY KEY)"
        )
        connection.execute(
            """
            CREATE TABLE strategy_management_batches (
                id INTEGER PRIMARY KEY,
                idempotency_fingerprint VARCHAR(64) NOT NULL,
                strategy_instance_id VARCHAR(255) NOT NULL,
                status VARCHAR(32) NOT NULL
            )
            """
        )

    session_factory = create_session_factory(database_path)
    inspector = inspect(session_factory.kw["bind"])

    assert {
        "management_contract_json",
        "management_contract_fingerprint",
    } <= {
        column["name"]
        for column in inspector.get_columns("signal_candidates")
    }
    assert {
        "management_contract_json",
        "management_contract_fingerprint",
        "contract_version",
    } <= {
        column["name"]
        for column in inspector.get_columns("strategy_management_batches")
    }
    assert inspector.has_table("strategy_management_components")
    assert {
        "management_batch_id",
        "strategy_management_leg_id",
        "component_kind",
        "sequence",
        "status",
        "idempotency_key",
        "desired_json",
        "evidence_json",
        "reason_code",
        "attempt_count",
        "last_progress_at",
        "execution_deadline_at",
        "created_at",
        "updated_at",
        "completed_at",
    } <= {
        column["name"]
        for column in inspector.get_columns("strategy_management_components")
    }
    indexes = {
        index["name"]: index
        for index in inspector.get_indexes("strategy_management_components")
    }
    assert indexes["uq_strategy_management_components_idempotency"]["unique"]
    assert indexes["uq_strategy_management_components_batch_leg_kind"]["unique"]
