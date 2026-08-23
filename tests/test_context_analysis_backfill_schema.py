import sqlite3

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import Base


EXPECTED_COLUMNS = {
    "id",
    "run_id",
    "raw_message_id",
    "source_attempt_id",
    "source_request_sha256",
    "source_state_fingerprint",
    "prompt_version",
    "analyst_model",
    "decision_json",
    "status",
    "skip_reason",
    "created_at",
}
EXPECTED_FOREIGN_KEYS = {
    ("raw_message_id", "raw_messages", "id"),
    ("source_attempt_id", "context_resolution_attempts", "id"),
}
ALLOWED_STATUSES = {
    "analysis_only_completed",
    "skipped_deleted",
    "skipped_stale",
}


def _insert_backfill(connection, *, run_id, raw_message_id, status):
    connection.execute(
        """
        INSERT INTO context_analysis_backfills (
            run_id,
            raw_message_id,
            source_attempt_id,
            source_request_sha256,
            prompt_version,
            analyst_model,
            status,
            created_at
        ) VALUES (?, ?, 9001, ?, 'context-resolution-v1',
                  'codex-manual-context-v1', ?, CURRENT_TIMESTAMP)
        """,
        (run_id, raw_message_id, "a" * 64, status),
    )


def test_context_analysis_backfill_schema_has_only_audit_source_links(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(context_analysis_backfills)"
            ).fetchall()
        }
        foreign_keys = {
            (row[3], row[2], row[4])
            for row in connection.execute(
                "PRAGMA foreign_key_list(context_analysis_backfills)"
            ).fetchall()
        }
        indexes = connection.execute(
            "PRAGMA index_list(context_analysis_backfills)"
        ).fetchall()
        unique_columns = {
            tuple(
                item[2]
                for item in connection.execute(
                    f'PRAGMA index_info("{row[1]}")'
                ).fetchall()
            )
            for row in indexes
            if row[2]
        }
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='context_analysis_backfills'"
        ).fetchone()[0]
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='context_analysis_backfills'"
        ).fetchall()

    assert "context_analysis_backfills" in Base.metadata.tables
    assert columns == EXPECTED_COLUMNS
    assert foreign_keys == EXPECTED_FOREIGN_KEYS
    assert ("run_id", "raw_message_id") in unique_columns
    assert "strategy_thread" not in table_sql
    assert ALLOWED_STATUSES == {
        value
        for value in ALLOWED_STATUSES
        if f"'{value}'" in table_sql
    }
    assert triggers == []


def test_context_analysis_backfill_closed_status_and_identity_are_enforced(tmp_path):
    database_path = tmp_path / "constraints.db"
    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        _insert_backfill(
            connection,
            run_id="run-1",
            raw_message_id=101,
            status="analysis_only_completed",
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_backfill(
                connection,
                run_id="run-1",
                raw_message_id=101,
                status="skipped_stale",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_backfill(
                connection,
                run_id="run-2",
                raw_message_id=102,
                status="operational_replay",
            )


def test_context_analysis_backfill_bootstrap_is_idempotent_on_legacy_database(
    tmp_path,
):
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE legacy_rows (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO legacy_rows (id, value) VALUES (1, 'preserve-me')"
        )

    create_session_factory(database_path)
    with sqlite3.connect(database_path) as connection:
        first_schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE tbl_name='context_analysis_backfills' ORDER BY type, name"
        ).fetchall()

    create_session_factory(database_path)
    with sqlite3.connect(database_path) as connection:
        second_schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE tbl_name='context_analysis_backfills' ORDER BY type, name"
        ).fetchall()
        legacy_rows = connection.execute(
            "SELECT id, value FROM legacy_rows ORDER BY id"
        ).fetchall()
        count = connection.execute(
            "SELECT COUNT(*) FROM context_analysis_backfills"
        ).fetchone()[0]
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]

    assert second_schema == first_schema
    assert legacy_rows == [(1, "preserve-me")]
    assert count == 0
    assert quick_check == "ok"
