import sqlite3

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import Base


EXPECTED_COLUMNS = {
    "id",
    "command_id",
    "command_type",
    "request_json",
    "request_fingerprint",
    "idempotency_key",
    "status",
    "claim_token",
    "claimed_at",
    "lease_expires_at",
    "attempt_count",
    "side_effect_started_at",
    "result_schema_version",
    "http_status",
    "result_json",
    "error_code",
    "error_summary",
    "uncertain_at",
    "reconciled_at",
    "created_at",
    "completed_at",
}

EXPECTED_INDEXES = {
    "uq_worker_command_jobs_command_id": True,
    "uq_worker_command_jobs_type_idempotency": True,
    "ix_worker_command_jobs_claim_scan": False,
    "ix_worker_command_jobs_request_fingerprint": False,
}


def _insert_job(
    connection,
    *,
    command_id,
    command_type="sync_deepcoin_execution",
    status="pending",
    idempotency_key=None,
):
    connection.execute(
        """
        INSERT INTO worker_command_jobs (
            command_id,
            command_type,
            request_json,
            request_fingerprint,
            idempotency_key,
            status,
            attempt_count,
            result_schema_version,
            created_at
        ) VALUES (?, ?, '{}', ?, ?, ?, 0, 1, CURRENT_TIMESTAMP)
        """,
        (
            command_id,
            command_type,
            f"fingerprint-{command_id}",
            idempotency_key,
            status,
        ),
    )


def test_worker_command_job_table_constraints_and_indexes_are_bootstrapped(
    tmp_path,
):
    database_path = tmp_path / "research.db"

    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(worker_command_jobs)"
            ).fetchall()
        }
        indexes = {
            row[1]: bool(row[2])
            for row in connection.execute(
                "PRAGMA index_list(worker_command_jobs)"
            ).fetchall()
        }
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='worker_command_jobs'"
        ).fetchone()[0]

    assert "worker_command_jobs" in Base.metadata.tables
    assert columns == EXPECTED_COLUMNS
    assert EXPECTED_INDEXES.items() <= indexes.items()
    assert "'pending', 'claimed', 'executing', 'succeeded', 'failed', 'uncertain'" in table_sql
    assert "'sync_deepcoin_execution', 'close_bound_position', 'recovery_live_submit', 'process_next_trade_signal'" in table_sql


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("status", "retrying"),
        ("command_type", "arbitrary_exchange_write"),
    ],
)
def test_worker_command_job_closed_enums_are_enforced_by_sqlite(
    tmp_path, field_name, value
):
    database_path = tmp_path / f"invalid-{field_name}.db"
    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        kwargs = {field_name: value}
        with pytest.raises(sqlite3.IntegrityError):
            _insert_job(connection, command_id=f"invalid-{field_name}", **kwargs)


def test_worker_command_job_identities_are_unique_but_null_keys_are_independent(
    tmp_path,
):
    database_path = tmp_path / "identities.db"
    create_session_factory(database_path)

    with sqlite3.connect(database_path) as connection:
        _insert_job(connection, command_id="command-1", idempotency_key="action-1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_job(connection, command_id="command-1", idempotency_key="action-2")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_job(connection, command_id="command-2", idempotency_key="action-1")
        _insert_job(connection, command_id="command-3", idempotency_key=None)
        _insert_job(connection, command_id="command-4", idempotency_key=None)

        assert connection.execute(
            "SELECT COUNT(*) FROM worker_command_jobs"
        ).fetchone()[0] == 3


def test_existing_database_bootstrap_is_idempotent_and_preserves_legacy_rows(
    tmp_path,
):
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
    with sqlite3.connect(database_path) as connection:
        first_schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE tbl_name='worker_command_jobs' ORDER BY type, name"
        ).fetchall()

    create_session_factory(database_path)
    with sqlite3.connect(database_path) as connection:
        second_schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE tbl_name='worker_command_jobs' ORDER BY type, name"
        ).fetchall()
        legacy_rows = connection.execute(
            "SELECT id, value FROM legacy_rows ORDER BY id"
        ).fetchall()
        job_count = connection.execute(
            "SELECT COUNT(*) FROM worker_command_jobs"
        ).fetchone()[0]
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]

    assert second_schema == first_schema
    assert legacy_rows == [(1, "keep-one"), (2, "keep-two")]
    assert job_count == 0
    assert quick_check == "ok"
