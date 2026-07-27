from sqlalchemy import inspect

from telegram_kol_research.db import SQLITE_COMPAT_COLUMNS, create_session_factory


def test_context_resolution_schema_is_created(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    inspector = inspect(session_factory.kw["bind"])

    assert inspector.has_table("message_evidence_versions")
    assert inspector.has_table("strategy_threads")
    assert inspector.has_table("strategy_message_links")
    assert inspector.has_table("context_resolution_attempts")
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
