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
