import sqlite3

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import Base


EXPECTED_COLUMNS = {
    "id",
    "raw_message_id",
    "chat_id",
    "status",
    "attempt_count",
    "next_attempt_at",
    "claim_token",
    "claimed_at",
    "last_reason",
    "enqueued_at",
    "completed_at",
    "shadow",
}


def test_message_processing_job_table_and_indexes_are_bootstrapped(tmp_path):
    database_path = tmp_path / "research.db"

    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(message_processing_jobs)"
            ).fetchall()
        }
        indexes = {
            row[1]: bool(row[2])
            for row in connection.execute(
                "PRAGMA index_list(message_processing_jobs)"
            ).fetchall()
        }

    assert "message_processing_jobs" in Base.metadata.tables
    assert columns == EXPECTED_COLUMNS
    assert indexes["uq_message_processing_jobs_raw_message_id"] is True
    assert "ix_message_processing_jobs_chat_id" in indexes
    assert "ix_message_processing_jobs_next_attempt_at" in indexes


def test_existing_database_bootstrap_adds_only_job_schema_and_preserves_rows(tmp_path):
    database_path = tmp_path / "production-copy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE legacy_rows (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO legacy_rows (id, value) VALUES (?, ?)",
            [(1, "keep-one"), (2, "keep-two")],
        )

    create_session_factory(database_path)
    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        legacy_rows = connection.execute(
            "SELECT id, value FROM legacy_rows ORDER BY id"
        ).fetchall()
        job_count = connection.execute(
            "SELECT COUNT(*) FROM message_processing_jobs"
        ).fetchone()[0]
        unique_index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='uq_message_processing_jobs_raw_message_id'"
        ).fetchone()[0]

    assert legacy_rows == [(1, "keep-one"), (2, "keep-two")]
    assert job_count == 0
    assert "UNIQUE INDEX" in unique_index_sql.upper()
    assert "raw_message_id" in unique_index_sql
