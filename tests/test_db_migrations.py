import sqlite3

from sqlalchemy import inspect

from telegram_kol_research.db import SQLITE_COMPAT_COLUMNS, create_session_factory


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
    assert "strategy_thread_id" in {
        column["name"]
        for column in inspector.get_columns("strategy_lifecycles")
    }


def test_old_lifecycle_table_has_compatible_thread_column_migration():
    statement = SQLITE_COMPAT_COLUMNS["strategy_lifecycles"][
        "strategy_thread_id"
    ]

    assert statement == (
        "ALTER TABLE strategy_lifecycles "
        "ADD COLUMN strategy_thread_id INTEGER"
    )


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
