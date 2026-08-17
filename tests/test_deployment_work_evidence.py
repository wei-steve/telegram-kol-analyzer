from __future__ import annotations

from dataclasses import asdict
import inspect
from pathlib import Path
import sqlite3

import pytest

import telegram_kol_research.deployment_work_evidence as evidence_module
from telegram_kol_research.deployment_work_evidence import (
    MAX_EVIDENCE_COUNT,
    DeploymentEvidenceCounts,
    collect_deployment_evidence,
    decide_deployment,
)


@pytest.mark.parametrize(
    ("counts", "writer_changed", "expected_decision", "expected_reason"),
    [
        (
            DeploymentEvidenceCounts(active_write=1),
            False,
            "BLOCK",
            "active_exchange_write",
        ),
        (
            DeploymentEvidenceCounts(unknown_outcome=1),
            False,
            "BLOCK",
            "unknown_exchange_outcome",
        ),
        (
            DeploymentEvidenceCounts(invalid_evidence=1),
            False,
            "BLOCK",
            "invalid_registered_evidence",
        ),
        (
            DeploymentEvidenceCounts(queued_work=1),
            True,
            "BLOCK",
            "writer_changed_with_queued_work",
        ),
        (
            DeploymentEvidenceCounts(queued_work=1),
            False,
            "WARN",
            "queued_work_with_unchanged_writer",
        ),
        (DeploymentEvidenceCounts(inactive=9), False, "PASS", None),
    ],
)
def test_decision_matrix(
    counts: DeploymentEvidenceCounts,
    writer_changed: bool,
    expected_decision: str,
    expected_reason: str | None,
) -> None:
    result = decide_deployment(counts=counts, writer_changed=writer_changed)

    assert result.decision == expected_decision
    if expected_reason is None:
        assert result.reason_codes == ()
    else:
        assert expected_reason in result.reason_codes


def test_blocking_reasons_follow_fixed_safety_order() -> None:
    result = decide_deployment(
        counts=DeploymentEvidenceCounts(
            active_write=1,
            unknown_outcome=1,
            queued_work=1,
            invalid_evidence=1,
        ),
        writer_changed=True,
    )

    assert result.decision == "BLOCK"
    assert result.reason_codes == (
        "invalid_registered_evidence",
        "active_exchange_write",
        "unknown_exchange_outcome",
        "writer_changed_with_queued_work",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active_write", -1),
        ("unknown_outcome", True),
        ("queued_work", MAX_EVIDENCE_COUNT + 1),
        ("inactive", 1.5),
        ("invalid_evidence", "1"),
    ],
)
def test_counts_reject_invalid_values(field: str, value: object) -> None:
    values = {
        "active_write": 0,
        "unknown_outcome": 0,
        "queued_work": 0,
        "inactive": 0,
        "invalid_evidence": 0,
    }
    values[field] = value

    with pytest.raises(ValueError, match="evidence_count_invalid"):
        DeploymentEvidenceCounts(**values)


def test_counts_reject_unknown_fields() -> None:
    with pytest.raises(TypeError):
        DeploymentEvidenceCounts(unregistered=1)


def test_decision_rejects_non_boolean_writer_change() -> None:
    with pytest.raises(ValueError, match="writer_changed_invalid"):
        decide_deployment(
            counts=DeploymentEvidenceCounts(),
            writer_changed=1,
        )


def test_decision_has_no_operator_override_or_time_inputs() -> None:
    parameters = inspect.signature(decide_deployment).parameters

    assert set(parameters) == {"counts", "writer_changed"}
    assert not {"override", "change_class", "created_at", "updated_at"} & set(
        parameters
    )


def _create_registered_tables(
    database: Path,
    *,
    backup_columns: str = (
        "id INTEGER PRIMARY KEY, venue TEXT, pos_id TEXT, order_id TEXT, "
        "client_order_id TEXT, status TEXT"
    ),
    source_columns: str = (
        "id INTEGER PRIMARY KEY, source_event_id INTEGER, raw_message_id INTEGER, "
        "target_lifecycle_id INTEGER, execution_binding_id INTEGER, "
        "strategy_instance_id TEXT, target_fingerprint TEXT, state TEXT, "
        "claim_token TEXT, claimed_at TEXT"
    ),
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"CREATE TABLE position_backup_stop_orders ({backup_columns})"
        )
        connection.execute(
            f"CREATE TABLE source_message_deletion_exits ({source_columns})"
        )
        connection.executescript(
            """
            CREATE TABLE execution_bindings (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                message_id INTEGER,
                symbol TEXT,
                side TEXT,
                status TEXT,
                updated_at TEXT
            );
            CREATE TABLE execution_order_legs (
                id INTEGER PRIMARY KEY,
                execution_binding_id INTEGER,
                purpose TEXT,
                status TEXT,
                order_id TEXT,
                client_order_id TEXT,
                pos_id TEXT,
                request_json TEXT,
                response_json TEXT,
                updated_at TEXT
            );
            CREATE TABLE message_instruction_items (
                id INTEGER PRIMARY KEY,
                status TEXT,
                updated_at TEXT
            );
            CREATE TABLE instruction_execution_contracts (
                id INTEGER PRIMARY KEY,
                message_instruction_item_id INTEGER,
                state TEXT,
                terminal_kind TEXT,
                completion_scope TEXT,
                evidence_refs_json TEXT,
                updated_at TEXT
            );
            CREATE TABLE strategy_management_batches (
                id INTEGER PRIMARY KEY,
                status TEXT,
                reason_code TEXT,
                execution_mode TEXT,
                updated_at TEXT
            );
            CREATE TABLE strategy_management_components (
                id INTEGER PRIMARY KEY,
                management_batch_id INTEGER,
                status TEXT,
                request_json TEXT,
                response_json TEXT,
                updated_at TEXT
            );
            CREATE TABLE trigger_protection_intents (
                id INTEGER PRIMARY KEY,
                execution_binding_id INTEGER,
                execution_order_leg_id INTEGER,
                recovery_state TEXT,
                parent_trigger_order_id TEXT,
                adopted_order_id TEXT,
                updated_at TEXT
            );
            CREATE TABLE position_mutation_intents (
                id INTEGER PRIMARY KEY,
                status TEXT,
                submitted_at TEXT,
                confirmed_at TEXT,
                response_json TEXT,
                error_json TEXT,
                updated_at TEXT
            );
            CREATE TABLE trade_signals (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                message_id INTEGER,
                symbol TEXT,
                side TEXT,
                status TEXT,
                processed_at TEXT,
                updated_at TEXT
            );
            """
        )


def test_recognition_audit_tables_are_not_execution_evidence(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE mimo_recognition_runs (id INTEGER, status TEXT)"
        )
        connection.execute(
            "CREATE TABLE mimo_recognition_attempts (id INTEGER, state TEXT)"
        )
        connection.execute("INSERT INTO mimo_recognition_runs VALUES (1, 'mystery')")
        connection.execute(
            "INSERT INTO mimo_recognition_attempts VALUES (1, 'unknown')"
        )

    snapshot = collect_deployment_evidence(database)

    assert snapshot.counts == DeploymentEvidenceCounts()


def test_backup_missing_is_valid_inactive_evidence(tmp_path: Path) -> None:
    database = tmp_path / "backup.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO position_backup_stop_orders "
            "(id, venue, pos_id, order_id, client_order_id, status) "
            "VALUES (1, 'deepcoin', 'position-redacted', 'order-redacted', "
            "'client-redacted', 'missing')"
        )

    snapshot = collect_deployment_evidence(database)

    assert snapshot.counts == DeploymentEvidenceCounts(inactive=1)


def test_unbound_source_without_target_or_claim_is_inactive(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO source_message_deletion_exits "
            "(id, source_event_id, state) VALUES (1, 11, 'unbound')"
        )

    snapshot = collect_deployment_evidence(database)

    assert snapshot.counts == DeploymentEvidenceCounts(inactive=1)


def test_unknown_state_in_registered_table_is_invalid_only(tmp_path: Path) -> None:
    database = tmp_path / "unknown.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO position_backup_stop_orders "
            "(id, venue, pos_id, status) "
            "VALUES (1, 'deepcoin', 'position-redacted', 'invented')"
        )

    snapshot = collect_deployment_evidence(database)

    assert snapshot.counts == DeploymentEvidenceCounts(invalid_evidence=1)


def test_unregistered_status_table_is_ignored(tmp_path: Path) -> None:
    database = tmp_path / "unregistered.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated_jobs (id INTEGER, status TEXT)")
        connection.execute("INSERT INTO unrelated_jobs VALUES (1, 'submitting')")

    snapshot = collect_deployment_evidence(database)

    assert snapshot.counts == DeploymentEvidenceCounts()


def test_missing_required_registered_column_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "missing-column.db"
    _create_registered_tables(database, backup_columns="id INTEGER, status TEXT")

    snapshot = collect_deployment_evidence(database)

    assert snapshot.counts == DeploymentEvidenceCounts(invalid_evidence=1)


def test_conflicting_duplicate_projection_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "duplicate.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO source_message_deletion_exits "
            "(id, source_event_id, state) VALUES (?, 11, ?)",
            [(1, "unbound"), (2, "pending")],
        )

    snapshot = collect_deployment_evidence(database)

    assert snapshot.counts.invalid_evidence == 1
    assert snapshot.counts.active_write == 0
    assert snapshot.counts.unknown_outcome == 0
    assert snapshot.counts.queued_work == 0
    assert snapshot.counts.inactive == 0


def test_malformed_registered_value_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "malformed.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO position_backup_stop_orders "
            "(id, venue, pos_id, status) VALUES (1, 'deepcoin', 'position-redacted', NULL)"
        )

    snapshot = collect_deployment_evidence(database)

    assert snapshot.counts == DeploymentEvidenceCounts(invalid_evidence=1)


def test_active_backup_without_exchange_order_identity_fails_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "active-without-order.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO position_backup_stop_orders "
            "(id, venue, pos_id, client_order_id, status) "
            "VALUES (1, 'deepcoin', 'position-redacted', 'client-redacted', 'active')"
        )

    snapshot = collect_deployment_evidence(database)

    assert snapshot.counts == DeploymentEvidenceCounts(invalid_evidence=1)


def test_duplicate_backup_exchange_order_identity_fails_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "duplicate-order.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO position_backup_stop_orders "
            "(id, venue, pos_id, order_id, client_order_id, status) "
            "VALUES (?, 'deepcoin', ?, 'shared-order', ?, 'active')",
            [
                (1, "position-a", "client-a"),
                (2, "position-b", "client-b"),
            ],
        )

    snapshot = collect_deployment_evidence(database)

    assert snapshot.counts == DeploymentEvidenceCounts(invalid_evidence=1)


def test_collection_uses_uri_read_only_and_query_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "readonly.db"
    _create_registered_tables(database)
    original_connect = sqlite3.connect
    calls: list[tuple[object, bool]] = []
    statements: list[str] = []

    class RecordingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            statements.append(str(sql))
            return super().execute(sql, parameters)

    def recording_connect(database_value, *args, **kwargs):
        calls.append((database_value, bool(kwargs.get("uri"))))
        kwargs["factory"] = RecordingConnection
        return original_connect(database_value, *args, **kwargs)

    monkeypatch.setattr(evidence_module.sqlite3, "connect", recording_connect)

    before = database.read_bytes()
    collect_deployment_evidence(database)

    assert calls == [(f"{database.resolve().as_uri()}?mode=ro", True)]
    assert any(statement.strip().upper() == "PRAGMA QUERY_ONLY=ON" for statement in statements)
    assert database.read_bytes() == before
    assert not database.with_name(f"{database.name}-wal").exists()
    assert not database.with_name(f"{database.name}-shm").exists()


def test_collection_result_is_bounded_and_sanitized(tmp_path: Path) -> None:
    database = tmp_path / "sanitized.db"
    _create_registered_tables(database)
    secret_values = (
        "row-identifier-987",
        "private-message-text",
        "BTC-USDT",
        "exchange-order-secret",
        "api-credential-secret",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO position_backup_stop_orders "
            "(id, venue, pos_id, order_id, client_order_id, status) "
            "VALUES (1, ?, ?, ?, 'client-redacted', 'missing')",
            (secret_values[2], secret_values[0], secret_values[3]),
        )
        connection.execute(
            "CREATE TABLE unrelated_payloads "
            "(message_text TEXT, payload TEXT, credential TEXT)"
        )
        connection.execute(
            "INSERT INTO unrelated_payloads VALUES (?, ?, ?)",
            (secret_values[1], '{"private": true}', secret_values[4]),
        )

    rendered = repr(asdict(collect_deployment_evidence(database)))

    assert len(rendered) < 1_024
    assert all(secret not in rendered for secret in secret_values)


def _assert_only_category(snapshot, category: str) -> None:
    values = asdict(snapshot.counts)
    assert values[category] > 0
    assert all(value == 0 for name, value in values.items() if name != category)


def test_stale_unknown_binding_and_leg_without_claim_are_inactive(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stale-binding.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO execution_bindings "
            "(id, chat_id, message_id, symbol, side, status) "
            "VALUES (1, 10, 20, 'BTC', 'long', 'unknown')"
        )
        connection.execute(
            "INSERT INTO execution_order_legs "
            "(id, execution_binding_id, purpose, status) "
            "VALUES (1, 1, 'entry', 'unknown')"
        )

    _assert_only_category(collect_deployment_evidence(database), "inactive")


def test_unknown_instruction_without_contract_is_inactive(tmp_path: Path) -> None:
    database = tmp_path / "item-without-contract.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO message_instruction_items (id, status) VALUES (1, 'unknown')"
        )

    _assert_only_category(collect_deployment_evidence(database), "inactive")


def test_submit_unknown_contract_without_terminal_proof_is_unknown(
    tmp_path: Path,
) -> None:
    database = tmp_path / "unknown-contract.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO message_instruction_items (id, status) VALUES (1, 'unknown')"
        )
        connection.execute(
            "INSERT INTO instruction_execution_contracts "
            "(id, message_instruction_item_id, state, evidence_refs_json) "
            "VALUES (1, 1, 'submit_unknown', '[]')"
        )

    _assert_only_category(collect_deployment_evidence(database), "unknown_outcome")


def test_operator_paused_management_recovery_is_inactive(tmp_path: Path) -> None:
    database = tmp_path / "paused-management.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO strategy_management_batches "
            "(id, status, reason_code, execution_mode) "
            "VALUES (1, 'recovery_required', 'operator_review_required', 'live')"
        )

    _assert_only_category(collect_deployment_evidence(database), "inactive")


def test_paused_management_with_unknown_attempted_child_is_unknown(
    tmp_path: Path,
) -> None:
    database = tmp_path / "management-child-unknown.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO strategy_management_batches "
            "(id, status, reason_code, execution_mode) "
            "VALUES (1, 'recovery_required', 'operator_review_required', 'live')"
        )
        connection.execute(
            "INSERT INTO strategy_management_components "
            "(id, management_batch_id, status, request_json) "
            "VALUES (1, 1, 'submit_unknown', '{}')"
        )

    _assert_only_category(collect_deployment_evidence(database), "unknown_outcome")


def test_closed_parent_protection_recovery_is_inactive(tmp_path: Path) -> None:
    database = tmp_path / "closed-protection.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO execution_bindings "
            "(id, chat_id, message_id, symbol, side, status) "
            "VALUES (1, 10, 20, 'BTC', 'long', 'closed')"
        )
        connection.execute(
            "INSERT INTO execution_order_legs "
            "(id, execution_binding_id, purpose, status) "
            "VALUES (1, 1, 'entry', 'closed')"
        )
        connection.execute(
            "INSERT INTO trigger_protection_intents "
            "(id, execution_binding_id, execution_order_leg_id, recovery_state) "
            "VALUES (1, 1, 1, 'recovery_required')"
        )

    _assert_only_category(collect_deployment_evidence(database), "inactive")


def _insert_partial_submission(
    connection: sqlite3.Connection,
    *,
    second_leg_status: str,
) -> None:
    connection.execute(
        "INSERT INTO trade_signals "
        "(id, chat_id, message_id, symbol, side, status) "
        "VALUES (1, 10, 20, 'BTC', 'long', 'partial_submission_failed')"
    )
    connection.execute(
        "INSERT INTO execution_bindings "
        "(id, chat_id, message_id, symbol, side, status) "
        "VALUES (1, 10, 20, 'BTC', 'long', 'active')"
    )
    connection.executemany(
        "INSERT INTO execution_order_legs "
        "(id, execution_binding_id, purpose, status, order_id) "
        "VALUES (?, 1, 'entry', ?, ?)",
        [
            (1, "active", "order-a"),
            (2, second_leg_status, "order-b" if second_leg_status != "submit_unknown" else None),
        ],
    )


def test_verified_partial_submission_projection_is_inactive(tmp_path: Path) -> None:
    database = tmp_path / "verified-partial.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        _insert_partial_submission(connection, second_leg_status="closed")

    _assert_only_category(collect_deployment_evidence(database), "inactive")


def test_incomplete_partial_submission_projection_is_unknown(tmp_path: Path) -> None:
    database = tmp_path / "unknown-partial.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        _insert_partial_submission(connection, second_leg_status="submit_unknown")

    _assert_only_category(collect_deployment_evidence(database), "unknown_outcome")


@pytest.mark.parametrize("status", ["submitting", "cancel_submitting"])
def test_durable_position_mutation_claim_is_active_write(
    tmp_path: Path,
    status: str,
) -> None:
    database = tmp_path / f"active-{status}.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO position_mutation_intents "
            "(id, status, submitted_at) VALUES (1, ?, NULL)",
            (status,),
        )

    _assert_only_category(collect_deployment_evidence(database), "active_write")


def test_pending_entry_is_queued_work(tmp_path: Path) -> None:
    database = tmp_path / "pending-entry.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO trade_signals "
            "(id, chat_id, message_id, symbol, side, status) "
            "VALUES (1, 10, 20, 'BTC', 'long', 'pending')"
        )

    _assert_only_category(collect_deployment_evidence(database), "queued_work")


def test_ready_management_is_queued_work(tmp_path: Path) -> None:
    database = tmp_path / "ready-management.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO strategy_management_batches "
            "(id, status, execution_mode) VALUES (1, 'ready', 'live')"
        )

    _assert_only_category(collect_deployment_evidence(database), "queued_work")


def test_terminal_and_permanently_paused_work_are_inactive(tmp_path: Path) -> None:
    database = tmp_path / "terminal-paused.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO trade_signals "
            "(id, chat_id, message_id, symbol, side, status) "
            "VALUES (1, 10, 20, 'BTC', 'long', 'failed')"
        )
        connection.execute(
            "INSERT INTO strategy_management_batches "
            "(id, status, reason_code, execution_mode) "
            "VALUES (1, 'recovery_required', 'operator_review_required', 'disabled')"
        )

    _assert_only_category(collect_deployment_evidence(database), "inactive")


def test_timestamp_only_change_does_not_change_evidence_fingerprint(
    tmp_path: Path,
) -> None:
    database = tmp_path / "fingerprint.db"
    _create_registered_tables(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO trade_signals "
            "(id, chat_id, message_id, symbol, side, status, updated_at) "
            "VALUES (1, 10, 20, 'BTC', 'long', 'pending', '2026-01-01')"
        )
    original = collect_deployment_evidence(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE trade_signals SET updated_at = '2030-12-31' WHERE id = 1"
        )
    timestamp_only = collect_deployment_evidence(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE trade_signals SET status = 'processing' WHERE id = 1"
        )
    claimed = collect_deployment_evidence(database)

    assert timestamp_only.evidence_fingerprint == original.evidence_fingerprint
    assert claimed.evidence_fingerprint != original.evidence_fingerprint
    _assert_only_category(claimed, "active_write")
