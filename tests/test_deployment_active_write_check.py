from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

import telegram_kol_research.deployment_active_write_check as active_write_check
from telegram_kol_research.deployment_active_write_check import (
    ActiveWriteCheckError,
    count_active_exchange_writes,
)


def _create_authority_database(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE position_backup_stop_orders (id INTEGER, status TEXT);
            CREATE TABLE execution_order_legs (id INTEGER, status TEXT);
            CREATE TABLE instruction_execution_contracts (id INTEGER, state TEXT);
            CREATE TABLE strategy_management_components (id INTEGER, status TEXT);
            CREATE TABLE strategy_management_batches (id INTEGER, status TEXT);
            CREATE TABLE strategy_revision_batches (
                id INTEGER,
                status TEXT,
                advance_claim_token TEXT,
                advance_claimed_at TEXT
            );
            CREATE TABLE strategy_revision_legs (
                id INTEGER,
                revision_batch_id INTEGER,
                status TEXT
            );
            CREATE TABLE entry_revision_replacements (
                id INTEGER,
                revision_batch_id INTEGER,
                status TEXT
            );
            CREATE TABLE trigger_protection_intents (
                id INTEGER,
                recovery_state TEXT
            );
            CREATE TABLE position_mutation_intents (id INTEGER, status TEXT);
            CREATE TABLE trade_signals (id INTEGER, status TEXT);
            """
        )
    return path


def test_empty_authority_tables_have_zero_active_writes(tmp_path: Path) -> None:
    database = _create_authority_database(tmp_path / "research.db")

    assert count_active_exchange_writes(database) == 0


@pytest.mark.parametrize(
    ("table", "column", "status"),
    (
        ("position_backup_stop_orders", "status", "submitting"),
        ("execution_order_legs", "status", "submitting"),
        ("execution_order_legs", "status", "cancel_submitting"),
        ("instruction_execution_contracts", "state", "submitting"),
        ("strategy_management_components", "status", "submitting"),
        ("strategy_management_components", "status", "cancel_submitting"),
        ("strategy_management_batches", "status", "executing"),
        ("strategy_revision_batches", "status", "submitting_replacements"),
        ("trigger_protection_intents", "recovery_state", "submitting"),
        ("trigger_protection_intents", "recovery_state", "cancel_submitting"),
        ("position_mutation_intents", "status", "submitting"),
        ("position_mutation_intents", "status", "cancel_submitting"),
        ("trade_signals", "status", "processing"),
        ("trade_signals", "status", "submitting"),
        ("trade_signals", "status", "cancel_submitting"),
    ),
)
def test_direct_active_state_counts_one(
    tmp_path: Path,
    table: str,
    column: str,
    status: str,
) -> None:
    database = _create_authority_database(tmp_path / "research.db")
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"INSERT INTO {table} (id, {column}) VALUES (1, ?)",
            (status,),
        )

    assert count_active_exchange_writes(database) == 1


@pytest.mark.parametrize(
    ("table", "status"),
    (
        ("strategy_revision_legs", "cancel_submitting"),
        ("entry_revision_replacements", "submit_reserved"),
    ),
)
@pytest.mark.parametrize(
    ("claim_token", "claimed_at", "expected"),
    (
        ("claim-1", "2026-08-17T08:00:00Z", 1),
        (None, "2026-08-17T08:00:00Z", 0),
        ("", "2026-08-17T08:00:00Z", 0),
        ("claim-1", None, 0),
        ("claim-1", "", 0),
        (None, None, 0),
    ),
)
def test_claim_aware_child_requires_complete_nonempty_parent_claim(
    tmp_path: Path,
    table: str,
    status: str,
    claim_token: str | None,
    claimed_at: str | None,
    expected: int,
) -> None:
    database = _create_authority_database(tmp_path / "research.db")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO strategy_revision_batches (
                id, status, advance_claim_token, advance_claimed_at
            ) VALUES (1, 'ready', ?, ?)
            """,
            (claim_token, claimed_at),
        )
        connection.execute(
            f"""
            INSERT INTO {table} (id, revision_batch_id, status)
            VALUES (1, 1, ?)
            """,
            (status,),
        )

    assert count_active_exchange_writes(database) == expected


@pytest.mark.parametrize(
    ("table", "column", "status"),
    (
        ("position_backup_stop_orders", "status", "pending"),
        ("execution_order_legs", "status", "ready"),
        ("instruction_execution_contracts", "state", "reserved"),
        ("strategy_management_components", "status", "submitted"),
        ("strategy_management_batches", "status", "submit_unknown"),
        (
            "strategy_revision_batches",
            "status",
            "unknown_exchange_outcome",
        ),
        ("trigger_protection_intents", "recovery_state", "recovery_required"),
        ("position_mutation_intents", "status", "completed"),
        ("trade_signals", "status", "invented_future_state"),
    ),
)
def test_historical_and_unfamiliar_states_are_ignored(
    tmp_path: Path,
    table: str,
    column: str,
    status: str,
) -> None:
    database = _create_authority_database(tmp_path / "research.db")
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"INSERT INTO {table} (id, {column}) VALUES (1, ?)",
            (status,),
        )

    assert count_active_exchange_writes(database) == 0


def test_missing_required_table_fails_closed(tmp_path: Path) -> None:
    database = _create_authority_database(tmp_path / "research.db")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE trade_signals")

    with pytest.raises(ActiveWriteCheckError, match="^active_write_check_failed$"):
        count_active_exchange_writes(database)


def test_missing_required_column_fails_closed(tmp_path: Path) -> None:
    database = _create_authority_database(tmp_path / "research.db")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE trade_signals")
        connection.execute("CREATE TABLE trade_signals (id INTEGER)")

    with pytest.raises(ActiveWriteCheckError, match="^active_write_check_failed$"):
        count_active_exchange_writes(database)


@pytest.mark.parametrize("kind", ("missing", "directory", "invalid_input"))
def test_invalid_database_inputs_fail_closed(tmp_path: Path, kind: str) -> None:
    if kind == "missing":
        database: object = tmp_path / "missing.db"
    elif kind == "directory":
        database = tmp_path
    else:
        database = None

    with pytest.raises(ActiveWriteCheckError, match="^active_write_check_failed$"):
        count_active_exchange_writes(database)  # type: ignore[arg-type]


def test_sqlite_connection_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _create_authority_database(tmp_path / "research.db")

    def fail_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("sensitive database failure")

    monkeypatch.setattr(active_write_check.sqlite3, "connect", fail_connect)

    with pytest.raises(ActiveWriteCheckError, match="^active_write_check_failed$"):
        count_active_exchange_writes(database)


class _ConnectionRecorder:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.statements: list[str] = []

    def execute(self, statement: str, *args: object) -> sqlite3.Cursor:
        self.statements.append(statement)
        return self.connection.execute(statement, *args)

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


def test_connection_is_uri_read_only_and_query_only_is_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _create_authority_database(tmp_path / "research.db")
    original_connect = sqlite3.connect
    calls: list[tuple[str, bool]] = []
    recorders: list[_ConnectionRecorder] = []

    def recording_connect(
        database_uri: str,
        *,
        uri: bool = False,
    ) -> _ConnectionRecorder:
        calls.append((database_uri, uri))
        recorder = _ConnectionRecorder(original_connect(database_uri, uri=uri))
        recorders.append(recorder)
        return recorder

    monkeypatch.setattr(active_write_check.sqlite3, "connect", recording_connect)

    assert count_active_exchange_writes(database) == 0
    assert calls == [(f"{database.resolve().as_uri()}?mode=ro", True)]
    assert recorders[0].statements[:3] == [
        "PRAGMA query_only=ON",
        "PRAGMA query_only",
        "BEGIN",
    ]


def test_database_bytes_are_unchanged(tmp_path: Path) -> None:
    database = _create_authority_database(tmp_path / "research.db")
    before = database.read_bytes()

    assert count_active_exchange_writes(database) == 0

    assert database.read_bytes() == before


class _ValueCursor:
    def __init__(self, value: Any) -> None:
        self.value = value

    def fetchone(self) -> tuple[Any]:
        return (self.value,)


class _ValueConnection:
    def __init__(self, value: Any) -> None:
        self.value = value

    def execute(self, statement: str, *_args: object) -> _ValueCursor:
        if statement == "PRAGMA query_only":
            return _ValueCursor(1)
        if statement.lstrip().startswith("SELECT"):
            return _ValueCursor(self.value)
        return _ValueCursor(None)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.mark.parametrize("invalid_count", (True, -1, "1", None))
def test_invalid_sql_count_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_count: Any,
) -> None:
    database = _create_authority_database(tmp_path / "research.db")
    monkeypatch.setattr(
        active_write_check.sqlite3,
        "connect",
        lambda *_args, **_kwargs: _ValueConnection(invalid_count),
    )

    with pytest.raises(ActiveWriteCheckError, match="^active_write_check_failed$"):
        count_active_exchange_writes(database)


def test_sum_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _create_authority_database(tmp_path / "research.db")
    monkeypatch.setattr(
        active_write_check.sqlite3,
        "connect",
        lambda *_args, **_kwargs: _ValueConnection(200_000),
    )

    assert count_active_exchange_writes(database) == 1_000_000
