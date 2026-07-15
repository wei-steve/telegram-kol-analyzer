from typer.testing import CliRunner
from datetime import UTC, datetime
import json
import os
import sqlite3
from types import SimpleNamespace

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    list_execution_order_legs,
    upsert_execution_binding,
)
from telegram_kol_research.trading_settings import load_trading_settings


def test_cli_help_renders():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "sync" in result.stdout
    assert "report" in result.stdout
    assert "recovery-dry-run" in result.stdout
    assert "repair-position-attribution" in result.stdout
    assert "audit-management-batches" in result.stdout


def test_audit_management_batches_is_bounded_redacted_and_read_only(
    tmp_path, monkeypatch
):
    import telegram_kol_research.cli as cli_module

    database_path = tmp_path / "research.db"
    create_session_factory(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO raw_messages "
            "(id, chat_id, message_id, archived_target_group, created_at) "
            "VALUES (876543211, -1001234567890, 398475612, 1, "
            "'2026-07-15 10:00:00')"
        )
        for batch_id, status in (
            (982134701, "recovery_required"),
            (982134702, "blocked"),
        ):
            connection.execute(
                "INSERT INTO strategy_management_batches "
                "(id, idempotency_fingerprint, raw_message_id, recognition_decision_id, "
                "recognition_generation, target_lifecycle_id, strategy_instance_id, "
                "execution_binding_id, intent, effective_action, execution_mode, "
                "partial_round_before, status, target_fingerprint, target_snapshot_json, "
                "planned_at, created_at, updated_at) "
                "VALUES (?, ?, 876543211, 765432131, 'generation-secret', "
                "789654341, ?, 675849351, "
                "'partial_take_profit', 'partial_close', 'shadow', 0, ?, ?, ?, "
                "'2026-07-15 10:00:00', '2026-07-15 10:00:00', '2026-07-15 10:00:00')",
                (
                    batch_id,
                    f"fingerprint-{batch_id}",
                    f"strategy-secret-{batch_id}",
                    status,
                    f"target-secret-{batch_id}",
                        "{malformed" if batch_id == 982134701 else "{}",
                ),
            )
        connection.execute(
            "UPDATE strategy_management_batches "
            "SET planned_at = '2026-07-15 11:00:00' WHERE id = 982134701"
        )
        connection.execute(
            "INSERT INTO strategy_management_legs "
            "(id, management_batch_id, execution_order_leg_id, pos_id, leg_index, "
            "status, preflight_size, planned_close_size, last_error, created_at, updated_at) "
            "VALUES (564738291, 982134701, 453627191, 'pos-secret-abcdef', 1, "
            "'submit_unknown', '0.02', "
            "'0.01', '{broken', '2026-07-15 10:00:00', '2026-07-15 10:00:00')"
        )
        connection.execute(
            "INSERT INTO trade_signals "
            "(id, signal_uid, source_type, venue, kol_id, chat_id, message_id, symbol, "
            "side, action, status, payload_json, attempts, created_at, updated_at) "
            "VALUES (453627181, 'signal-secret', 'automatic', 'deepcoin', 'kol-secret', "
            "-1001234567890, 398475612, 'BTC', 'short', 'close_position', 'pending', "
            "'{bad-json', 0, '2026-07-15 10:00:00', '2026-07-15 10:00:00')"
        )
        connection.commit()

    before = database_path.read_bytes()
    monkeypatch.setattr(
        cli_module,
        "build_deepcoin_client_from_env",
        lambda: (_ for _ in ()).throw(AssertionError("exchange client called")),
    )
    monkeypatch.setattr(
        cli_module,
        "create_session_factory",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("session factory called")
        ),
    )
    result = CliRunner().invoke(
        app,
        [
            "audit-management-batches",
            "--database-path",
            str(database_path),
            "--limit",
            "1",
            "--output-format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema_status"] == "available"
    assert payload["counts"]["batches_total"] == 2
    assert payload["counts"]["blocked"] == 1
    assert payload["counts"]["submit_unknown"] == 1
    assert payload["counts"]["recovery_required"] == 1
    assert payload["legacy_pending_management"]["total"] == 1
    assert payload["batches_returned"] == 1
    assert payload["batches_truncated"] is True
    assert payload["snapshot_status"] == "stable"
    assert payload["snapshot_validation"] == "ok"
    assert payload["batches"][0]["batch_ref"].startswith("batch:")
    assert payload["batches"][0]["source"]["chat_ref"].startswith("chat:")
    assert payload["batches"][0]["source"]["message_ref"].startswith("message:")
    assert payload["batches"][0]["target"]["lifecycle_ref"].startswith(
        "lifecycle:"
    )
    assert payload["batches"][0]["target"]["binding_ref"].startswith("binding:")
    assert payload["batches"][0]["legs"][0]["leg_ref"].startswith("leg:")
    assert payload["batches"][0]["legs"][0]["pos_ref"].startswith("pos:")
    assert payload["batches"][0]["malformed_json_fields"] == ["target_snapshot_json"]
    assert payload["batches"][0]["legs"][0]["malformed_json_fields"] == [
        "last_error"
    ]
    assert "pos-secret" not in result.stdout
    assert "strategy-secret" not in result.stdout
    assert "kol-secret" not in result.stdout
    for identity in (
        "-1001234567890",
        "398475612",
        "982134701",
        "982134702",
        "876543211",
        "789654341",
        "675849351",
        "564738291",
        "453627181",
    ):
        assert identity not in result.stdout
    text_result = CliRunner().invoke(
        app,
        [
            "audit-management-batches",
            "--database-path",
            str(database_path),
            "--limit",
            "1",
            "--output-format",
            "text",
        ],
    )
    assert text_result.exit_code == 0, text_result.stdout
    assert "Batch counts:" in text_result.stdout
    assert "Legacy pending management:" in text_result.stdout
    assert "snapshot_status=stable" in text_result.stdout
    assert "snapshot_validation=ok" in text_result.stdout
    assert "by_action=" in text_result.stdout
    assert "complete=true" in text_result.stdout
    assert "signal:" in text_result.stdout
    assert "pos-secret" not in text_result.stdout
    assert "strategy-secret" not in text_result.stdout
    assert database_path.read_bytes() == before


def test_audit_management_batches_source_snapshot_creates_no_sidecars(tmp_path):
    database_path = tmp_path / "no-sidecars.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE harmless (id INTEGER PRIMARY KEY)")
        connection.commit()
    before_files = sorted(path.name for path in tmp_path.iterdir())
    before = database_path.read_bytes(), database_path.stat()

    result = CliRunner().invoke(
        app,
        [
            "audit-management-batches",
            "--database-path",
            str(database_path),
            "--output-format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["snapshot_status"] == "stable"
    assert sorted(path.name for path in tmp_path.iterdir()) == before_files
    assert database_path.read_bytes() == before[0]
    assert database_path.stat() == before[1]


def test_audit_management_batches_active_wal_read_only_source_is_unchanged(tmp_path):
    database_path = tmp_path / "active-wal.db"
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.executescript(
            """
            CREATE TABLE strategy_management_batches (
                id INTEGER, raw_message_id INTEGER, target_lifecycle_id INTEGER,
                strategy_instance_id TEXT, execution_binding_id INTEGER,
                intent TEXT, effective_action TEXT, execution_mode TEXT,
                status TEXT, target_snapshot_json TEXT, planned_at TEXT
            );
            CREATE TABLE strategy_management_legs (
                id INTEGER, management_batch_id INTEGER, pos_id TEXT,
                leg_index INTEGER, status TEXT, preflight_size TEXT,
                planned_close_size TEXT, last_error TEXT
            );
            CREATE TABLE trade_signals (
                id INTEGER, source_type TEXT, venue TEXT, chat_id INTEGER,
                message_id INTEGER, action TEXT, status TEXT, payload_json TEXT,
                created_at TEXT
            );
            """
        )
        connection.commit()
        source_paths = [
            path
            for path in (
                database_path,
                database_path.with_name(database_path.name + "-wal"),
                database_path.with_name(database_path.name + "-shm"),
            )
            if path.exists()
        ]
        for path in source_paths:
            path.chmod(0o444)
        tmp_path.chmod(0o555)
        before_files = sorted(path.name for path in tmp_path.iterdir())
        before = {
            path.name: (path.read_bytes(), path.stat()) for path in source_paths
        }

        result = CliRunner().invoke(
            app,
            [
                "audit-management-batches",
                "--database-path",
                str(database_path),
                "--output-format",
                "json",
            ],
        )

        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["snapshot_status"] == "stable"
        assert payload["schema_status"] == "available"
        assert payload["snapshot_components"] == ["main", "shm", "wal"]
        assert sorted(path.name for path in tmp_path.iterdir()) == before_files
        for path in source_paths:
            assert path.read_bytes() == before[path.name][0]
            assert path.stat() == before[path.name][1]
    finally:
        tmp_path.chmod(0o755)
        for suffix in ("", "-wal", "-shm"):
            path = database_path.with_name(database_path.name + suffix)
            if path.exists():
                path.chmod(0o644)
        connection.close()


def test_audit_management_batches_fails_closed_when_snapshot_changes(
    tmp_path, monkeypatch
):
    import telegram_kol_research.cli as cli_module

    database_path = tmp_path / "changing.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE harmless (id INTEGER PRIMARY KEY)")
        connection.commit()
    original = cli_module._read_snapshot_component
    calls = 0

    def changing_read(path):
        nonlocal calls
        result = original(path)
        calls += 1
        if calls == 1:
            with open(database_path, "ab") as stream:
                stream.write(b"changed")
        return result

    monkeypatch.setattr(cli_module, "_read_snapshot_component", changing_read)
    result = CliRunner().invoke(
        app,
        [
            "audit-management-batches",
            "--database-path",
            str(database_path),
            "--output-format",
            "json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["snapshot_status"] == "snapshot_unstable"
    assert payload["batches"] == []


def test_audit_management_batches_fails_closed_on_rollback_journal(tmp_path):
    database_path = tmp_path / "journal.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE harmless (id INTEGER PRIMARY KEY)")
        connection.commit()
    database_path.with_name(database_path.name + "-journal").write_bytes(b"active")

    result = CliRunner().invoke(
        app,
        [
            "audit-management-batches",
            "--database-path",
            str(database_path),
            "--output-format",
            "json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["snapshot_status"] == "snapshot_unstable"
    assert payload["snapshot_reason"] == "rollback_journal_present"
    assert payload["output_complete"] is False


def test_audit_management_batches_handles_old_schema_without_migrating(tmp_path):
    database_path = tmp_path / "old.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE trade_signals (id INTEGER PRIMARY KEY)")
        connection.commit()
    before = database_path.read_bytes()

    result = CliRunner().invoke(
        app,
        [
            "audit-management-batches",
            "--database-path",
            str(database_path),
            "--output-format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema_status"] == "management_schema_missing"
    assert payload["counts"]["batches_total"] == 0
    assert payload["legacy_pending_management"]["status"] == "schema_unavailable"
    assert database_path.read_bytes() == before


def test_database_initialization_twice_is_idempotent_and_management_defaults_disabled(
    tmp_path,
):
    database_path = tmp_path / "initialized-twice.db"
    first_factory = create_session_factory(database_path)
    with sqlite3.connect(database_path) as connection:
        first_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()

    second_factory = create_session_factory(database_path)
    with sqlite3.connect(database_path) as connection:
        second_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()

    assert second_schema == first_schema
    assert load_trading_settings(first_factory).auto_trade_enabled is False
    assert load_trading_settings(second_factory).management_execution_mode == "disabled"


def test_audit_management_batches_streams_all_legacy_candidates_past_5000(
    tmp_path,
):
    database_path = tmp_path / "many-signals.db"
    create_session_factory(database_path)
    common = (
        "automatic",
        "deepcoin",
        "kol",
        -1009,
        "BTC",
        "short",
        "close_position",
        "pending",
        0,
        "2026-07-15 10:00:00",
        "2026-07-15 10:00:00",
    )
    rows = [
        (
            f"signal-{row_id}",
            *common[:4],
            row_id,
            *common[4:8],
            json.dumps({"management_batch_id": row_id}),
            *common[8:],
        )
        for row_id in range(1, 5002)
    ]
    rows.append(
        (
            "signal-legacy-last",
            *common[:4],
            6000,
            *common[4:8],
            "{}",
            *common[8:],
        )
    )
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            "INSERT INTO trade_signals "
            "(signal_uid, source_type, venue, kol_id, chat_id, message_id, symbol, "
            "side, action, status, payload_json, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()

    result = CliRunner().invoke(
        app,
        [
            "audit-management-batches",
            "--database-path",
            str(database_path),
            "--limit",
            "1",
            "--output-format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    legacy = json.loads(result.stdout)["legacy_pending_management"]
    assert legacy["candidate_pending_count"] == 5002
    assert legacy["scanned_count"] == 5002
    assert legacy["total"] == 1
    assert legacy["complete"] is True
    assert legacy["scan_truncated"] is False
    assert legacy["items"][0]["message_ref"].startswith("message:")
    assert "6000" not in result.stdout


def test_audit_management_batches_malformed_complete_columns_are_safe(tmp_path):
    database_path = tmp_path / "malformed-columns.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE strategy_management_batches (
                id TEXT, raw_message_id TEXT, target_lifecycle_id TEXT,
                strategy_instance_id TEXT, execution_binding_id TEXT,
                intent TEXT, effective_action TEXT, execution_mode TEXT,
                status TEXT, target_snapshot_json TEXT, planned_at TEXT
            );
            CREATE TABLE strategy_management_legs (
                id TEXT, management_batch_id TEXT, pos_id TEXT, leg_index TEXT,
                status TEXT, preflight_size TEXT, planned_close_size TEXT,
                last_error TEXT
            );
            CREATE TABLE raw_messages (id TEXT, chat_id TEXT, message_id TEXT);
            CREATE TABLE trade_signals (
                id TEXT, source_type TEXT, venue TEXT, chat_id TEXT,
                message_id TEXT, action TEXT, status TEXT, payload_json TEXT,
                created_at TEXT
            );
            INSERT INTO strategy_management_batches VALUES (
                'batch-secret-bad', NULL, 'life-secret-bad', 'strategy-secret-bad',
                'binding-secret-bad', 'bad intent !', 'evil\nraw', 'LIVE!',
                'unknown state !', '{bad', 'not-a-date'
            );
            INSERT INTO strategy_management_legs VALUES (
                'leg-secret-bad', 'batch-secret-bad', 'pos-secret-bad', 'NaN',
                'bad state !', 'Infinity', 'steal-me', '{bad'
            );
            INSERT INTO raw_messages VALUES (NULL, 'chat-secret-bad', 'msg-secret-bad');
            """
        )
        connection.commit()

    result = CliRunner().invoke(
        app,
        [
            "audit-management-batches",
            "--database-path",
            str(database_path),
            "--output-format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema_status"] == "available"
    assert payload["malformed_field_count"] >= 8
    assert payload["malformed_row_count"] >= 2
    batch = payload["batches"][0]
    assert batch["status"] == "invalid"
    assert batch["planned_at"] is None
    assert batch["legs"][0]["leg_index"] is None
    assert batch["legs"][0]["preflight_size"] is None
    assert batch["legs"][0]["planned_close_size"] is None
    for secret in (
        "batch-secret-bad",
        "life-secret-bad",
        "strategy-secret-bad",
        "binding-secret-bad",
        "leg-secret-bad",
        "pos-secret-bad",
        "steal-me",
        "evil",
    ):
        assert secret not in result.stdout


def test_repair_position_attribution_cli_defaults_to_dry_run(tmp_path, monkeypatch):
    import telegram_kol_research.cli as cli_module

    class EmptyDeepcoinClient:
        def list_positions(self):
            return []

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id):
            return []

    database_path = tmp_path / "research.db"
    create_session_factory(database_path)
    monkeypatch.setattr(
        cli_module,
        "build_deepcoin_client_from_env",
        lambda: EmptyDeepcoinClient(),
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        ["repair-position-attribution", "--database-path", str(database_path)],
    )

    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    assert '"actions": []' in result.stdout
    assert '"historical_actions": []' in result.stdout


def test_repair_position_attribution_cli_apply_requires_expected_fingerprint(
    tmp_path, monkeypatch
):
    import telegram_kol_research.cli as cli_module
    from telegram_kol_research.position_attribution_repair import (
        PositionAttributionRepairAction,
        PositionAttributionRepairPlan,
    )

    plan = PositionAttributionRepairPlan(
        created_at=datetime(2026, 7, 15, tzinfo=UTC),
        live_position_ids=("pos-1",),
        exchange_evidence_fingerprint="exchange",
        actions=(
            PositionAttributionRepairAction(
                action="assign_verified_position",
                binding_id=1,
                leg_id=1,
                leg_index=1,
                old_pos_id=None,
                new_pos_id="pos-1",
                old_status="filled",
                new_status="active",
                old_attribution_status="unassigned",
                new_attribution_status="verified",
            ),
        ),
        unresolved_conflicts=[],
        database_fingerprint="database",
        fingerprint="reviewed-fingerprint",
    )
    client = object()
    monkeypatch.setattr(
        cli_module, "build_deepcoin_client_from_env", lambda: client
    )
    monkeypatch.setattr(
        cli_module,
        "build_position_attribution_repair_plan",
        lambda *args, **kwargs: plan,
    )
    applied = []
    monkeypatch.setattr(
        cli_module,
        "apply_position_attribution_repair_plan",
        lambda *args, **kwargs: (
            applied.append(kwargs) or SimpleNamespace(applied=1)
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "repair-position-attribution",
            "--database-path",
            str(tmp_path / "research.db"),
            "--apply",
        ],
    )

    assert result.exit_code == 2
    assert "expected-fingerprint" in result.stdout + result.stderr
    assert applied == []

    matching = CliRunner().invoke(
        app,
        [
            "repair-position-attribution",
            "--database-path",
            str(tmp_path / "research.db"),
            "--apply",
            "--expected-fingerprint",
            plan.fingerprint,
        ],
    )

    assert matching.exit_code == 0
    assert applied == [
        {"deepcoin_client": client, "expected_fingerprint": plan.fingerprint}
    ]


def test_repair_position_attribution_cli_historical_only_apply_requires_fingerprint(
    tmp_path, monkeypatch
):
    import telegram_kol_research.cli as cli_module
    from telegram_kol_research.historical_attribution_cleanup import (
        HistoricalCleanupAction,
    )
    from telegram_kol_research.position_attribution_repair import (
        PositionAttributionRepairPlan,
    )

    plan = PositionAttributionRepairPlan(
        created_at=datetime(2026, 7, 15, tzinfo=UTC),
        live_position_ids=(),
        exchange_evidence_fingerprint="exchange",
        actions=(),
        historical_actions=(
            HistoricalCleanupAction(
                action="install_position_ownership_unique_index",
                binding_id=None,
                leg_id=None,
                lifecycle_id=None,
                venue="deepcoin",
                old_pos_id=None,
                new_pos_id=None,
                old_state="absent",
                new_state="present",
            ),
        ),
        unresolved_conflicts=[],
        database_fingerprint="database",
        fingerprint="historical-fingerprint",
    )
    client = object()
    monkeypatch.setattr(
        cli_module, "build_deepcoin_client_from_env", lambda: client
    )
    monkeypatch.setattr(
        cli_module,
        "build_position_attribution_repair_plan",
        lambda *args, **kwargs: plan,
    )
    applied = []
    monkeypatch.setattr(
        cli_module,
        "apply_position_attribution_repair_plan",
        lambda *args, **kwargs: applied.append(kwargs) or SimpleNamespace(applied=1),
    )

    refused = CliRunner().invoke(
        app,
        [
            "repair-position-attribution",
            "--database-path",
            str(tmp_path / "research.db"),
            "--apply",
        ],
    )
    assert refused.exit_code == 2
    assert "expected-fingerprint" in refused.stdout + refused.stderr
    assert applied == []

    accepted = CliRunner().invoke(
        app,
        [
            "repair-position-attribution",
            "--database-path",
            str(tmp_path / "research.db"),
            "--apply",
            "--expected-fingerprint",
            plan.fingerprint,
        ],
    )
    assert accepted.exit_code == 0
    assert applied == [
        {"deepcoin_client": client, "expected_fingerprint": plan.fingerprint}
    ]


def test_repair_execution_order_legs_cli_backfills_legacy_bindings(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="alice",
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            order_id="trigger-1,trigger-2",
            client_order_id="client-1,client-2",
            status="open",
            payload={
                "submitted_orders": [
                    {
                        "leg_index": 1,
                        "execution_type": "trigger_limit",
                        "order_id": "trigger-1",
                        "client_order_id": "client-1",
                    },
                    {
                        "leg_index": 2,
                        "execution_type": "trigger_limit",
                        "order_id": "trigger-2",
                        "client_order_id": "client-2",
                        "pos_id": "pos-2",
                    },
                ]
            },
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "repair-execution-order-legs",
            "--database-path",
            str(database_path),
        ],
    )

    assert result.exit_code == 0
    assert "Repaired 2 execution order leg(s)" in result.stdout
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [
        (leg.leg_index, leg.order_id, leg.client_order_id, leg.pos_id, leg.status)
        for leg in legs
    ] == [
        (1, "trigger-1", "client-1", None, "open"),
        (2, "trigger-2", "client-2", "pos-2", "active"),
    ]
