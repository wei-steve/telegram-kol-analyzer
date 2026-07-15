from typer.testing import CliRunner
from datetime import UTC, datetime
import json
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
            "VALUES (11, -1001234567890, 8427, 1, '2026-07-15 10:00:00')"
        )
        for batch_id, status in ((21, "recovery_required"), (22, "blocked")):
            connection.execute(
                "INSERT INTO strategy_management_batches "
                "(id, idempotency_fingerprint, raw_message_id, recognition_decision_id, "
                "recognition_generation, target_lifecycle_id, strategy_instance_id, "
                "execution_binding_id, intent, effective_action, execution_mode, "
                "partial_round_before, status, target_fingerprint, target_snapshot_json, "
                "planned_at, created_at, updated_at) "
                "VALUES (?, ?, 11, 31, 'generation-secret', 41, ?, 51, "
                "'partial_take_profit', 'partial_close', 'shadow', 0, ?, ?, ?, "
                "'2026-07-15 10:00:00', '2026-07-15 10:00:00', '2026-07-15 10:00:00')",
                (
                    batch_id,
                    f"fingerprint-{batch_id}",
                    f"strategy-secret-{batch_id}",
                    status,
                    f"target-secret-{batch_id}",
                    "{malformed" if batch_id == 21 else "{}",
                ),
            )
        connection.execute(
            "UPDATE strategy_management_batches "
            "SET planned_at = '2026-07-15 11:00:00' WHERE id = 21"
        )
        connection.execute(
            "INSERT INTO strategy_management_legs "
            "(id, management_batch_id, execution_order_leg_id, pos_id, leg_index, "
            "status, preflight_size, planned_close_size, last_error, created_at, updated_at) "
            "VALUES (61, 21, 71, 'pos-secret-abcdef', 1, 'submit_unknown', '0.02', "
            "'0.01', '{broken', '2026-07-15 10:00:00', '2026-07-15 10:00:00')"
        )
        connection.execute(
            "INSERT INTO trade_signals "
            "(id, signal_uid, source_type, venue, kol_id, chat_id, message_id, symbol, "
            "side, action, status, payload_json, attempts, created_at, updated_at) "
            "VALUES (81, 'signal-secret', 'automatic', 'deepcoin', 'kol-secret', "
            "-1001234567890, 8427, 'BTC', 'short', 'close_position', 'pending', "
            "'{bad-json', 0, '2026-07-15 10:00:00', '2026-07-15 10:00:00')"
        )
        connection.commit()

    before = database_path.read_bytes()
    monkeypatch.setattr(
        cli_module,
        "build_deepcoin_client_from_env",
        lambda: (_ for _ in ()).throw(AssertionError("exchange client called")),
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
    assert payload["batches"][0]["source"]["chat_ref"].startswith("chat:")
    assert payload["batches"][0]["legs"][0]["pos_ref"].startswith("pos:")
    assert payload["batches"][0]["malformed_json_fields"] == ["target_snapshot_json"]
    assert payload["batches"][0]["legs"][0]["malformed_json_fields"] == [
        "last_error"
    ]
    assert "pos-secret" not in result.stdout
    assert "strategy-secret" not in result.stdout
    assert "kol-secret" not in result.stdout
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
    assert "pos-secret" not in text_result.stdout
    assert "strategy-secret" not in text_result.stdout
    assert database_path.read_bytes() == before


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
