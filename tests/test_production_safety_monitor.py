import json
import os
import sqlite3
import stat
import sys
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints

import pytest

from telegram_kol_research.entry_assembly_fingerprint_repair import (
    RECONCILIATION_ACTION,
    RECONCILIATION_POLICY,
    build_reconciliation_fingerprint,
    canonical_fingerprint,
    derive_pre_finalization_fingerprint,
)
from typer.testing import CliRunner

from telegram_kol_research.cli import app
import telegram_kol_research.production_safety_monitor as monitor_module
from telegram_kol_research.production_safety_monitor import MAX_ALERT_LENGTH
from telegram_kol_research.production_safety_monitor import MONITOR_TEST_NOTIFICATION_TEXT
from telegram_kol_research.production_safety_monitor import MonitorExpectations
from telegram_kol_research.production_safety_monitor import MonitorNotificationDecision
from telegram_kol_research.production_safety_monitor import MonitorResult
from telegram_kol_research.production_safety_monitor import MonitorSnapshot
from telegram_kol_research.production_safety_monitor import MonitorState
from telegram_kol_research.production_safety_monitor import ProductionSafetyAdapters
from telegram_kol_research.production_safety_monitor import decide_monitor_notification
from telegram_kol_research.production_safety_monitor import evaluate_monitor_snapshot
from telegram_kol_research.production_safety_monitor import fingerprint_monitor_result
from telegram_kol_research.production_safety_monitor import format_monitor_alert
from telegram_kol_research.production_safety_monitor import load_monitor_state
from telegram_kol_research.production_safety_monitor import read_abnormal_execution_events
from telegram_kol_research.production_safety_monitor import read_loopback_settings
from telegram_kol_research.production_safety_monitor import read_message_operation_coverage
from telegram_kol_research.production_safety_monitor import read_composite_management_invariants
from telegram_kol_research.production_safety_monitor import read_entry_preamble_invariants
from telegram_kol_research.production_safety_monitor import read_adjacent_entry_invariants
from telegram_kol_research.production_safety_monitor import run_daily_management_audit
from telegram_kol_research.production_safety_monitor import run_production_safety_monitor
from telegram_kol_research.production_safety_monitor import save_monitor_state
from telegram_kol_research.production_safety_monitor import build_monitor_incident_capture_projection
from telegram_kol_research.production_safety_monitor import send_monitor_incident_capture
from telegram_kol_research.production_safety_monitor import send_monitor_test_notification
from telegram_kol_research.production_safety_monitor import should_run_daily_audit
from telegram_kol_research.config import RuntimeIncidentConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionBackupStopOrder,
    PositionProtectionIncident,
    PositionProtectionLedger,
    PositionProtectionRevision,
    RawMessage,
    RuntimeIncident,
    StrategyManagementBatch,
    StrategyManagementLeg,
    TriggerProtectionIntent,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.strategy_management_batches import (
    ManagementLegCreate,
    create_management_batch,
)


REVIEWED_HEAD = "3ec22ca77a1362167ce9d2cf702cfc50b1491967"
OTHER_HEAD = "a94c7b4cbb41331858be886bf341e79bd6bc2f4a"


EXPECTATIONS = MonitorExpectations(
    head=REVIEWED_HEAD,
    auto_trade_enabled=True,
    management_execution_mode="live",
    max_concurrent_positions=4,
    entry_preamble_mode="live",
)


def _clear_monitor_bot_environment(monkeypatch):
    for name in (
        "TELEGRAM_KOL_SYSTEM_BOT_TOKEN",
        "TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID",
        "TELEGRAM_KOL_SYSTEM_BOT_TIMEOUT_SECONDS",
        "TELEGRAM_KOL_NOTIFICATION_BOT_TOKEN",
        "TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID",
        "TELEGRAM_KOL_NOTIFICATION_BOT_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_monitor_bot_config_uses_system_operator_service_environment(monkeypatch):
    _clear_monitor_bot_environment(monkeypatch)
    monkeypatch.setenv("TELEGRAM_KOL_SYSTEM_BOT_TOKEN", "system-token")
    monkeypatch.setenv("TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID", "-100123")
    monkeypatch.setenv("TELEGRAM_KOL_SYSTEM_BOT_TIMEOUT_SECONDS", "7")

    config = monitor_module._load_monitor_bot_config()

    assert config.bot_token == "system-token"
    assert config.chat_id == "-100123"
    assert config.timeout_seconds == 7


def test_monitor_bot_config_rejects_notification_bot_only_environment(monkeypatch):
    _clear_monitor_bot_environment(monkeypatch)
    monkeypatch.setenv("TELEGRAM_KOL_NOTIFICATION_BOT_TOKEN", "notification-token")
    monkeypatch.setenv("TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID", "-100456")

    config = monitor_module._load_monitor_bot_config()

    assert not monitor_module.system_operator_bot_enabled(config)


def test_monitor_bot_config_never_reads_checkout_environment(tmp_path, monkeypatch):
    _clear_monitor_bot_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TELEGRAM_KOL_SYSTEM_BOT_TOKEN=checkout-token\n"
        "TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID=-100789\n",
        encoding="utf-8",
    )

    config = monitor_module._load_monitor_bot_config()

    assert not monitor_module.system_operator_bot_enabled(config)


def test_cli_unreadable_state_reaches_bounded_monitor_handling(tmp_path, monkeypatch):
    import telegram_kol_research.cli as cli_module

    state_path = tmp_path / "state.json"
    state_path.write_text("existing", encoding="utf-8")
    real_read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == state_path:
            raise PermissionError("bounded test detail")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    monkeypatch.setattr(
        cli_module,
        "create_existing_session_factory",
        lambda path: None,
    )
    monkeypatch.setattr(
        cli_module,
        "ProductionSafetyAdapters",
        lambda **kwargs: _RecordingAdapters(),
    )

    assert get_type_hints(cli_module.monitor_production_safety)["state_path"] is str
    result = CliRunner().invoke(
        app,
        [
            "monitor-production-safety",
            "--expected-head",
            "a" * 40,
            "--expected-auto-trade-enabled",
            "--expected-management-mode",
            "live",
            "--expected-entry-preamble-mode",
            "live",
            "--expected-max-concurrent-positions",
            "4",
            "--state-path",
            str(state_path),
        ],
    )

    assert result.exit_code == 1, result.output
    summary = json.loads(result.stdout)
    assert summary["reason_codes"] == ["state_invalid"]
    assert "state.json" not in result.output
    assert "PermissionError" not in result.output


def test_cli_routes_incident_capture_to_trusted_loopback_writer(monkeypatch):
    import telegram_kol_research.cli as cli_module

    calls = []
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_MONITOR_CAPTURE_TOKEN",
        "m" * 43,
    )
    monkeypatch.setattr(
        cli_module,
        "run_production_safety_monitor",
        lambda **kwargs: (
            calls.append(kwargs)
            or SimpleNamespace(
                audit_ran=False,
                result=SimpleNamespace(healthy=True, reason_codes=()),
                monitor_error=None,
                notification_status="not_needed",
                exit_code=0,
            )
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "monitor-production-safety",
            "--expected-head",
            "a" * 40,
            "--expected-auto-trade-enabled",
            "--expected-management-mode",
            "live",
            "--expected-entry-preamble-mode",
            "live",
            "--expected-max-concurrent-positions",
            "4",
            "--runtime-incident-capture-url",
            "http://127.0.0.1:8000/api/runtime-incidents/monitor-capture",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["expectations"].entry_preamble_mode == "live"
    assert calls[0]["runtime_incident_session_factory"] is None
    assert calls[0]["runtime_incident_capture_token"] == "m" * 43
    assert calls[0]["runtime_incident_capture_url"].endswith(
        "/api/runtime-incidents/monitor-capture"
    )


def _snapshot(**overrides):
    values = {
        "service_state": "active",
        "head": REVIEWED_HEAD,
        "settings": {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "max_concurrent_positions": 4,
            "entry_preamble_mode": "live",
        },
        "journal_error_count": 0,
        "abnormal_events": (),
        "audit": None,
    }
    values.update(overrides)
    return MonitorSnapshot(**values)


def _healthy_message_operation_coverage(**overrides):
    values = {
        "schema_version": 1,
        "coverage_enabled": True,
        "scan_truncated": False,
        "executable_messages_total": 2,
        "contracts_created_total": 2,
        "contracts_verified_total": 1,
        "contracts_violated_total": 1,
        "executable_without_contract_total": 0,
        "violations_without_stage1_total": 0,
        "stage1_pending": 0,
        "stage1_delivered": 1,
        "stage1_failed": 0,
        "agent_pending": 0,
        "agent_diagnosed": 1,
        "agent_failed": 0,
        "agent_timed_out": 0,
        "incidents_without_terminal_stage2_total": 0,
        "handoffs_persisted_total": 1,
        "stage2_pending": 0,
        "stage2_delivered": 1,
        "stage2_failed": 0,
        "oldest_nonterminal_age_seconds": 0,
        "supervisor_last_success_at": "2026-08-09T01:59:30+00:00",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"executable_without_contract_total": 1}, "executable_message_missing_contract"),
        ({"violations_without_stage1_total": 1}, "contract_violation_missing_stage1"),
        (
            {"incidents_without_terminal_stage2_total": 1},
            "message_operation_incident_missing_terminal",
        ),
        (
            {"supervisor_last_success_at": "2026-08-09T01:50:00+00:00"},
            "message_operation_supervisor_stale",
        ),
        ({"scan_truncated": True}, "message_operation_coverage_incomplete"),
    ],
)
def test_message_operation_coverage_gap_makes_monitor_unhealthy(overrides, reason):
    result = evaluate_monitor_snapshot(
        _snapshot(
            message_operation_coverage=_healthy_message_operation_coverage(
                **overrides
            )
        ),
        EXPECTATIONS,
        checked_at=datetime(2026, 8, 9, 2, 0, tzinfo=UTC),
    )

    assert result.healthy is False
    assert reason in result.reason_codes


def test_disabled_message_operation_coverage_is_a_clean_rollback_state():
    result = evaluate_monitor_snapshot(
        _snapshot(
            message_operation_coverage=_healthy_message_operation_coverage(
                coverage_enabled=False,
                supervisor_last_success_at=None,
            )
        ),
        EXPECTATIONS,
        checked_at=datetime(2026, 8, 9, 2, 0, tzinfo=UTC),
    )

    assert result.healthy is True


def _healthy_audit(**overrides):
    values = {
        "snapshot_status": "stable",
        "snapshot_validation": "ok",
        "schema_status": "available",
        "output_complete": True,
        "batches_truncated": False,
        "counts": {
            "blocked": 0,
            "partial_failed": 0,
            "submit_unknown": 0,
            "recovery_required": 0,
        },
        "actionable_batches": {
            "total": 0,
            "returned": 0,
            "truncated": False,
            "items": [],
        },
        "malformed_row_count": 0,
        "malformed_field_count": 0,
        "legacy_pending_management": {
            "complete": True,
            "truncated": False,
            "scan_truncated": False,
        },
    }
    values.update(overrides)
    if "actionable_batches" not in overrides:
        items = []
        batch_id = 1
        for state in (
            "blocked",
            "submit_unknown",
            "partial_failed",
            "recovery_required",
        ):
            for _ in range(int(values["counts"].get(state, 0))):
                if len(items) < 10:
                    items.append(
                        {"batch_ref": f"batch:{batch_id}", "states": [state]}
                    )
                batch_id += 1
        total = batch_id - 1
        values["actionable_batches"] = {
            "total": total,
            "returned": len(items),
            "truncated": total > len(items),
            "items": items,
        }
    return values


def test_exact_healthy_snapshot_has_no_reasons():
    result = evaluate_monitor_snapshot(_snapshot(), EXPECTATIONS)

    assert result.healthy is True
    assert result.reason_codes == ()
    assert result.details == {}


def test_monitor_surfaces_each_composite_management_invariant():
    codes = (
        "completed_batch_missing_component_evidence",
        "duplicate_composite_close_submission",
        "live_position_retained_tp_oversized",
        "composite_position_without_verified_stop",
        "stalled_composite_component",
    )

    result = evaluate_monitor_snapshot(
        _snapshot(composite_invariant_codes=codes), EXPECTATIONS
    )

    assert result.healthy is False
    assert result.reason_codes == tuple(sorted(codes))


def test_monitor_surfaces_each_entry_preamble_invariant():
    codes = (
        "stale_entry_preamble_unresolved",
        "entry_preamble_ambiguous",
        "live_entry_preamble_binding_evidence_missing",
    )

    result = evaluate_monitor_snapshot(
        _snapshot(entry_preamble_invariant_codes=codes), EXPECTATIONS
    )

    assert result.healthy is False
    assert result.reason_codes == tuple(sorted(codes))


def test_entry_preamble_monitor_reader_detects_faults_without_writes(tmp_path):
    database = tmp_path / "entry-preamble-monitor.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE entry_preambles (
          id INTEGER PRIMARY KEY, raw_message_id INTEGER, chat_id INTEGER,
          message_id INTEGER, symbol TEXT, side TEXT, status TEXT, created_at TEXT
        );
        CREATE TABLE raw_messages (id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER, posted_at TEXT);
        CREATE TABLE signal_candidates (id INTEGER PRIMARY KEY, raw_message_id INTEGER, event_type TEXT, management_action TEXT);
        CREATE TABLE entry_strategy_assemblies (
          id INTEGER PRIMARY KEY, strategy_instance_id TEXT, fingerprint TEXT
        );
        CREATE TABLE execution_bindings (
          id INTEGER PRIMARY KEY, strategy_instance_id TEXT, payload_json TEXT
        );
        INSERT INTO entry_preambles VALUES
          (1, 101, 9, 9901, 'BTCUSDT', 'long', 'pending', '2026-08-04 00:00:00'),
          (2, 102, 9, 9902, 'BTCUSDT', 'long', 'pending', '2026-08-04 00:01:00');
        INSERT INTO raw_messages VALUES
          (101, 9, 9901, '2026-08-04 00:00:00'),
          (102, 9, 9902, '2026-08-04 00:01:00');
        INSERT INTO entry_strategy_assemblies VALUES
          (1, 'strategy-1', 'assembly-fingerprint');
        INSERT INTO execution_bindings VALUES
          (1, 'strategy-1', '{"draft":{}}');
        """
    )
    connection.commit()
    before = database.read_bytes()

    codes = read_entry_preamble_invariants(
        database,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert set(codes) == {
        "stale_entry_preamble_unresolved",
        "entry_preamble_ambiguous",
        "live_entry_preamble_binding_evidence_missing",
    }
    assert database.read_bytes() == before


def _seed_terminal_prebinding_refusal(database):
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE entry_preambles (
          id INTEGER PRIMARY KEY, raw_message_id INTEGER, chat_id INTEGER,
          message_id INTEGER, symbol TEXT, side TEXT, status TEXT, created_at TEXT
        );
        CREATE TABLE raw_messages (
          id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER, posted_at TEXT
        );
        CREATE TABLE signal_candidates (
          id INTEGER PRIMARY KEY, raw_message_id INTEGER,
          event_type TEXT, management_action TEXT
        );
        CREATE TABLE message_instruction_items (
          id INTEGER PRIMARY KEY, raw_message_id INTEGER,
          signal_candidate_id INTEGER, instruction_kind TEXT,
          strategy_instance_id TEXT, status TEXT, result_json TEXT,
          error_json TEXT, retired_at TEXT
        );
        CREATE TABLE entry_strategy_assemblies (
          id INTEGER PRIMARY KEY, entry_preamble_id INTEGER,
          strategy_raw_message_id INTEGER, signal_candidate_id INTEGER,
          strategy_instance_id TEXT, evidence_json TEXT, fingerprint TEXT
        );
        CREATE TABLE execution_bindings (
          id INTEGER PRIMARY KEY, strategy_instance_id TEXT, payload_json TEXT
        );
        CREATE TABLE trade_signals (
          id INTEGER PRIMARY KEY, strategy_instance_id TEXT,
          chat_id INTEGER, message_id INTEGER
        );
        CREATE TABLE execution_events (
          id INTEGER PRIMARY KEY, strategy_instance_id TEXT,
          chat_id INTEGER, message_id INTEGER, source_message_id INTEGER
        );
        INSERT INTO raw_messages VALUES
          (9955, -1001, 3478, '2026-08-08 00:00:00');
        INSERT INTO signal_candidates VALUES
          (1643, 9955, 'entry_signal', NULL);
        INSERT INTO entry_strategy_assemblies VALUES
          (3, NULL, 9955, 1643, 'deepcoin:-1001:3478:SOL:short', '{}',
           'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
        INSERT INTO message_instruction_items VALUES
          (438, 9955, 1643, 'entry', 'deepcoin:-1001:3478:SOL:short', 'failed', NULL,
           '{"type":"RecoveryLiveSubmitError","message":"signal_enqueue_blocked:missing_ready_confirmation,contract_size_unverified"}',
           NULL);
        """
    )
    connection.commit()
    connection.close()


def test_entry_preamble_monitor_accepts_exact_terminal_prebinding_refusal(tmp_path):
    database = tmp_path / "terminal-prebinding-refusal.db"
    _seed_terminal_prebinding_refusal(database)
    before = database.read_bytes()

    codes = read_entry_preamble_invariants(
        database,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )

    assert codes == ()
    assert database.read_bytes() == before


def _assert_terminal_prebinding_refusal_fails_closed(database):
    before = database.read_bytes()
    codes = read_entry_preamble_invariants(
        database,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    assert codes == ("live_entry_preamble_binding_evidence_missing",)
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    (
        "DELETE FROM message_instruction_items",
        "UPDATE message_instruction_items SET instruction_kind = 'management'",
        "UPDATE message_instruction_items SET retired_at = '2026-08-08 00:01:00'",
        "UPDATE message_instruction_items SET raw_message_id = 9956",
        "INSERT INTO message_instruction_items VALUES "
        "(439, 9955, 1643, 'entry', 'deepcoin:-1001:3478:SOL:short', 'failed', NULL, "
        "'{\"type\":\"RecoveryLiveSubmitError\",\"message\":\"signal_enqueue_blocked:missing_ready_confirmation,contract_size_unverified\"}', NULL)",
    ),
)
def test_entry_preamble_monitor_terminal_prebinding_shape_is_fail_closed(
    tmp_path, mutation
):
    database = tmp_path / "terminal-prebinding-shape.db"
    _seed_terminal_prebinding_refusal(database)
    with sqlite3.connect(database) as connection:
        connection.execute(mutation)
    _assert_terminal_prebinding_refusal_fails_closed(database)


@pytest.mark.parametrize(
    "status",
    ("pending", "executing", "unknown", "succeeded"),
)
def test_entry_preamble_monitor_nonterminal_or_nonsuccess_status_is_fail_closed(
    tmp_path, status
):
    database = tmp_path / "terminal-prebinding-status.db"
    _seed_terminal_prebinding_refusal(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE message_instruction_items SET status = ?", (status,)
        )
    _assert_terminal_prebinding_refusal_fails_closed(database)


@pytest.mark.parametrize("item_strategy_instance_id", (None, "other-strategy"))
def test_entry_preamble_monitor_instruction_strategy_identity_is_exact(
    tmp_path, item_strategy_instance_id
):
    database = tmp_path / "terminal-prebinding-item-strategy.db"
    _seed_terminal_prebinding_refusal(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE message_instruction_items SET strategy_instance_id = ?",
            (item_strategy_instance_id,),
        )
    _assert_terminal_prebinding_refusal_fails_closed(database)


@pytest.mark.parametrize("result_json", ('{}', '{"status":"submitted"}'))
def test_entry_preamble_monitor_failed_instruction_result_is_fail_closed(
    tmp_path, result_json
):
    database = tmp_path / "terminal-prebinding-instruction-result.db"
    _seed_terminal_prebinding_refusal(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE message_instruction_items SET result_json = ?", (result_json,)
        )
    _assert_terminal_prebinding_refusal_fails_closed(database)


@pytest.mark.parametrize(
    "mutation",
    (
        "DELETE FROM signal_candidates",
        "UPDATE signal_candidates SET raw_message_id = 9956",
        "UPDATE signal_candidates SET event_type = 'management_signal'",
    ),
)
def test_entry_preamble_monitor_candidate_link_is_fail_closed(tmp_path, mutation):
    database = tmp_path / "terminal-prebinding-candidate-link.db"
    _seed_terminal_prebinding_refusal(database)
    with sqlite3.connect(database) as connection:
        connection.execute(mutation)
    _assert_terminal_prebinding_refusal_fails_closed(database)


@pytest.mark.parametrize(
    ("column", "value"),
    (("chat_id", "bad-chat"), ("chat_id", 0), ("message_id", "bad-message"),
     ("message_id", 0)),
)
def test_entry_preamble_monitor_source_identity_is_fail_closed(
    tmp_path, column, value
):
    database = tmp_path / "terminal-prebinding-source-identity.db"
    _seed_terminal_prebinding_refusal(database)
    with sqlite3.connect(database) as connection:
        connection.execute(f"UPDATE raw_messages SET {column} = ?", (value,))
    _assert_terminal_prebinding_refusal_fails_closed(database)


def test_entry_preamble_monitor_instruction_schema_without_id_is_fail_closed(tmp_path):
    database = tmp_path / "terminal-prebinding-missing-instruction-id.db"
    _seed_terminal_prebinding_refusal(database)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            ALTER TABLE message_instruction_items RENAME TO old_instruction_items;
            CREATE TABLE message_instruction_items (
              raw_message_id INTEGER, signal_candidate_id INTEGER,
              instruction_kind TEXT, strategy_instance_id TEXT,
              status TEXT, result_json TEXT, error_json TEXT, retired_at TEXT
            );
            INSERT INTO message_instruction_items
            SELECT raw_message_id, signal_candidate_id, instruction_kind,
                   strategy_instance_id, status, result_json, error_json, retired_at
            FROM old_instruction_items;
            DROP TABLE old_instruction_items;
            """
        )
    _assert_terminal_prebinding_refusal_fails_closed(database)


@pytest.mark.parametrize(
    "error_json",
    (
        None,
        "not-json",
        "[]",
        '{"type":"RecoveryLiveSubmitError","type":"RecoveryLiveSubmitError","message":"signal_enqueue_blocked:missing_ready_confirmation,contract_size_unverified"}',
        '{"type":"RecoveryLiveSubmitError","message":"signal_enqueue_blocked:missing_ready_confirmation,contract_size_unverified","extra":true}',
        '{"type":"OtherError","message":"signal_enqueue_blocked:missing_ready_confirmation,contract_size_unverified"}',
        '{"type":"RecoveryLiveSubmitError","message":"signal_enqueue_blocked:missing_ready_confirmation"}',
        '{"type":"RecoveryLiveSubmitError","message":"signal_enqueue_blocked:missing_ready_confirmation,missing_ready_confirmation"}',
        '{"type":"RecoveryLiveSubmitError","message":"signal_enqueue_blocked:missing_ready_confirmation,unknown_reason"}',
        '{"type":"RecoveryLiveSubmitError","message":"other:missing_ready_confirmation,contract_size_unverified"}',
        '{"type":"RecoveryLiveSubmitError","message":123}',
        '{"type":"RecoveryLiveSubmitError","message":"signal_enqueue_blocked:contract_size_unverified,missing_ready_confirmation,unknown_reason"}',
    ),
)
def test_entry_preamble_monitor_terminal_prebinding_error_is_closed_set(
    tmp_path, error_json
):
    database = tmp_path / "terminal-prebinding-error.db"
    _seed_terminal_prebinding_refusal(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE message_instruction_items SET error_json = ?", (error_json,)
        )
    _assert_terminal_prebinding_refusal_fails_closed(database)


def test_entry_preamble_monitor_oversized_terminal_prebinding_error_is_fail_closed(
    tmp_path,
):
    database = tmp_path / "terminal-prebinding-oversized-error.db"
    _seed_terminal_prebinding_refusal(database)
    oversized = json.dumps(
        {
            "type": "RecoveryLiveSubmitError",
            "message": "signal_enqueue_blocked:" + "x" * 1_000_000,
        }
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE message_instruction_items SET error_json = ?", (oversized,)
        )
    _assert_terminal_prebinding_refusal_fails_closed(database)


@pytest.mark.parametrize(
    "downstream_insert",
    (
        "INSERT INTO trade_signals VALUES (1, 'deepcoin:-1001:3478:SOL:short', 9, 9)",
        "INSERT INTO trade_signals VALUES (1, 'other-strategy', -1001, 3478)",
        "INSERT INTO execution_bindings VALUES "
        "(1, 'deepcoin:-1001:3478:SOL:short', '{}')",
        "INSERT INTO execution_events VALUES "
        "(1, 'deepcoin:-1001:3478:SOL:short', 9, 9, 9)",
        "INSERT INTO execution_events VALUES (1, 'other-strategy', -1001, 3478, NULL)",
        "INSERT INTO execution_events VALUES (1, 'other-strategy', -1001, 9, 3478)",
    ),
)
def test_entry_preamble_monitor_downstream_artifact_is_fail_closed(
    tmp_path, downstream_insert
):
    database = tmp_path / "terminal-prebinding-downstream.db"
    _seed_terminal_prebinding_refusal(database)
    with sqlite3.connect(database) as connection:
        connection.execute(downstream_insert)
    _assert_terminal_prebinding_refusal_fails_closed(database)


def _seed_reconciled_entry_preamble_monitor(database, *, second_mismatch=False):
    final_evidence = {
        "assembly_id": 2,
        "strategy_instance_id": "strategy-1",
        "chat_id": -1001,
        "strategy_message_id": 55,
        "symbol": "BTC",
        "side": "long",
        "order_draft_snapshot": {
            "strategy_instance_id": "strategy-1",
            "instrument_id": "BTC-USDT-SWAP",
            "symbol": "BTC",
            "margin_mode": "cross",
            "position_mode": "split",
            "stop_loss": 63000,
            "take_profit_legs": [{"price": 66000, "allocation_pct": 100}],
            "risk_budget_usdt": 10,
            "contract_spec": {
                "contract_value": 0.001,
                "quantity_step": 1,
                "min_quantity": 1,
            },
            "source": {
                "kol_id": "group:-1001",
                "kol_code": None,
                "chat_id": -1001,
                "message_id": 55,
            },
            "selected_entry_leg_indices": [1],
            "selected_entry_leg_count": 1,
            "order_legs": [
                {
                    "price": 64000,
                    "order_type": "limit",
                    "allocation_pct": 60,
                    "risk_budget_usdt": 6,
                    "quantity": 10,
                    "base_asset_estimate": 0.01,
                    "quantity_unit": "contracts",
                    "estimated_stop_loss_usdt": 6,
                    "client_order_id": "entry-1",
                    "side": "buy",
                    "position_side": "long",
                    "take_profit_leg": None,
                },
                {
                    "price": 63800,
                    "order_type": "limit",
                    "allocation_pct": 40,
                    "risk_budget_usdt": 4,
                    "quantity": 5,
                    "base_asset_estimate": 0.005,
                    "quantity_unit": "contracts",
                    "estimated_stop_loss_usdt": 4,
                    "client_order_id": "entry-2",
                    "side": "buy",
                    "position_side": "long",
                    "take_profit_leg": None,
                },
            ],
        },
        "final_entry_leg_count": 2,
    }
    final_fingerprint = canonical_fingerprint(final_evidence)
    old_fingerprint = derive_pre_finalization_fingerprint(final_evidence)
    repair_fingerprint = build_reconciliation_fingerprint(
        assembly_id=2,
        execution_binding_id=266,
        trade_signal_id=398,
        strategy_instance_id="strategy-1",
        old_fingerprint=old_fingerprint,
        final_fingerprint=final_fingerprint,
    )
    stale = {
        "assembly_id": 2,
        "strategy_instance_id": "strategy-1",
        "assembly_fingerprint": old_fingerprint,
    }
    common = {
        "policy_version": RECONCILIATION_POLICY,
        "assembly_id": 2,
        "execution_binding_id": 266,
        "trade_signal_id": 398,
        "strategy_instance_id": "strategy-1",
    }
    before = {**common, "assembly_fingerprint": old_fingerprint}
    after = {
        **common,
        "assembly_fingerprint": final_fingerprint,
        "repair_fingerprint": repair_fingerprint,
    }
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE entry_preambles (id INTEGER PRIMARY KEY, raw_message_id INTEGER, chat_id INTEGER, message_id INTEGER, symbol TEXT, side TEXT, status TEXT, created_at TEXT);
        CREATE TABLE raw_messages (id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER, posted_at TEXT);
        CREATE TABLE signal_candidates (id INTEGER PRIMARY KEY, raw_message_id INTEGER, event_type TEXT, management_action TEXT);
        CREATE TABLE entry_strategy_assemblies (id INTEGER PRIMARY KEY, strategy_instance_id TEXT, fingerprint TEXT, evidence_json TEXT);
        CREATE TABLE execution_bindings (
          id INTEGER PRIMARY KEY, strategy_instance_id TEXT, kol_id TEXT,
          chat_id INTEGER, message_id INTEGER, symbol TEXT, side TEXT,
          venue TEXT, margin_mode TEXT, position_mode TEXT, payload_json TEXT
        );
        CREATE TABLE trade_signals (
          id INTEGER PRIMARY KEY, strategy_instance_id TEXT, kol_id TEXT,
          chat_id INTEGER, message_id INTEGER, symbol TEXT, side TEXT,
          venue TEXT, source_type TEXT, action TEXT, status TEXT,
          processed_at TEXT, payload_json TEXT
        );
        CREATE TABLE execution_events (
          id INTEGER PRIMARY KEY, execution_binding_id INTEGER, trade_signal_id INTEGER,
          strategy_instance_id TEXT, venue TEXT, action TEXT, status TEXT,
          reason TEXT, before_json TEXT, after_json TEXT,
          kol_id TEXT, chat_id INTEGER, message_id INTEGER, source_message_id INTEGER,
          symbol TEXT, side TEXT, order_id TEXT, client_order_id TEXT, pos_id TEXT,
          related_order_id TEXT, request_json TEXT, response_json TEXT,
          exchange_event_time TEXT,
          notification_status TEXT, notification_fingerprint TEXT,
          notification_message_id TEXT, notification_error TEXT,
          notification_attempts INTEGER, notification_next_attempt_at TEXT,
          notification_claim_token TEXT, notification_claimed_at TEXT, notified_at TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO entry_strategy_assemblies VALUES (2, 'strategy-1', ?, ?)",
        (final_fingerprint, json.dumps(final_evidence, sort_keys=True)),
    )
    snapshot = final_evidence["order_draft_snapshot"]
    binding_draft = {**snapshot, "entry_preamble_assembly": stale}
    signal_draft = {**snapshot, "entry_preamble_assembly": stale}
    connection.execute(
        "INSERT INTO execution_bindings VALUES (266, 'strategy-1', 'group:-1001', -1001, 55, 'BTC', 'long', 'deepcoin', 'cross', 'split', ?)",
        (json.dumps({"draft": binding_draft}, sort_keys=True),),
    )
    connection.execute(
        "INSERT INTO trade_signals VALUES (398, 'strategy-1', 'group:-1001', -1001, 55, 'BTC', 'long', 'deepcoin', 'recovery', 'open_position', 'submitted', '2026-08-08 00:00:00', ?)",
        (json.dumps({
            "entry_preamble_assembly": stale,
            "deepcoin_order_draft": signal_draft,
        }, sort_keys=True),),
    )
    connection.execute(
        """INSERT INTO execution_events (
          id, execution_binding_id, trade_signal_id, strategy_instance_id,
          venue, action, status, reason, before_json, after_json,
          notification_status, notification_fingerprint, notification_attempts
        ) VALUES (1, 266, 398, 'strategy-1', 'deepcoin', ?, 'resolved',
                  'pre_finalization_payload_preserved', ?, ?, NULL, ?, 0)""",
        (
            RECONCILIATION_ACTION,
            json.dumps(before, sort_keys=True),
            json.dumps(after, sort_keys=True),
            repair_fingerprint,
        ),
    )
    if second_mismatch:
        other_evidence = {**final_evidence, "assembly_id": 3, "strategy_instance_id": "strategy-2"}
        other_final = canonical_fingerprint(other_evidence)
        other_old = derive_pre_finalization_fingerprint(other_evidence)
        connection.execute(
            "INSERT INTO entry_strategy_assemblies VALUES (3, 'strategy-2', ?, ?)",
            (other_final, json.dumps(other_evidence, sort_keys=True)),
        )
        connection.execute(
            "INSERT INTO execution_bindings VALUES (267, 'strategy-2', 'group:-1001', -1001, 55, 'BTC', 'long', 'deepcoin', 'cross', 'split', ?)",
            (json.dumps({"draft": {**other_evidence["order_draft_snapshot"], "entry_preamble_assembly": {
                "assembly_id": 3,
                "strategy_instance_id": "strategy-2",
                "assembly_fingerprint": other_old,
            }}}, sort_keys=True),),
        )
    connection.commit()
    connection.close()


def test_entry_preamble_monitor_accepts_exact_reconciliation_without_writes(tmp_path):
    database = tmp_path / "entry-preamble-reconciled.db"
    _seed_reconciled_entry_preamble_monitor(database)
    before = database.read_bytes()

    assert read_entry_preamble_invariants(
        database, now=datetime(2026, 8, 8, tzinfo=UTC)
    ) == ()
    assert database.read_bytes() == before


def test_entry_preamble_monitor_rejects_consistent_forged_alternate_signal_id(tmp_path):
    database = tmp_path / "entry-preamble-reconciled-forged-signal.db"
    _seed_reconciled_entry_preamble_monitor(database)
    connection = sqlite3.connect(database)
    assembly_fingerprint = connection.execute(
        "SELECT fingerprint FROM entry_strategy_assemblies WHERE id = 2"
    ).fetchone()[0]
    evidence = json.loads(
        connection.execute(
            "SELECT evidence_json FROM entry_strategy_assemblies WHERE id = 2"
        ).fetchone()[0]
    )
    old_fingerprint = derive_pre_finalization_fingerprint(evidence)
    forged_repair = build_reconciliation_fingerprint(
        assembly_id=2,
        execution_binding_id=266,
        trade_signal_id=399,
        strategy_instance_id="strategy-1",
        old_fingerprint=old_fingerprint,
        final_fingerprint=assembly_fingerprint,
    )
    before = json.loads(
        connection.execute("SELECT before_json FROM execution_events").fetchone()[0]
    )
    after = json.loads(
        connection.execute("SELECT after_json FROM execution_events").fetchone()[0]
    )
    before["trade_signal_id"] = 399
    after["trade_signal_id"] = 399
    after["repair_fingerprint"] = forged_repair
    connection.execute(
        "UPDATE execution_events SET trade_signal_id = 399, before_json = ?, after_json = ?, notification_fingerprint = ?",
        (json.dumps(before), json.dumps(after), forged_repair),
    )
    connection.commit()
    connection.close()

    assert read_entry_preamble_invariants(
        database, now=datetime(2026, 8, 8, tzinfo=UTC)
    ) == ("live_entry_preamble_binding_evidence_missing",)


def test_entry_preamble_monitor_treats_partial_event_schema_as_strict(tmp_path):
    database = tmp_path / "entry-preamble-reconciled-partial-event-schema.db"
    _seed_reconciled_entry_preamble_monitor(database)
    connection = sqlite3.connect(database)
    columns = [
        row[1]
        for row in connection.execute("PRAGMA table_info(execution_events)").fetchall()
        if row[1] != "id"
    ]
    selected = ", ".join(columns)
    connection.execute("ALTER TABLE execution_events RENAME TO old_execution_events")
    connection.execute(
        f"CREATE TABLE execution_events AS SELECT {selected} FROM old_execution_events"
    )
    connection.execute("DROP TABLE old_execution_events")
    connection.commit()
    connection.close()

    assert read_entry_preamble_invariants(
        database, now=datetime(2026, 8, 8, tzinfo=UTC)
    ) == ("live_entry_preamble_binding_evidence_missing",)


@pytest.mark.parametrize(
    "mutation",
    ["signal_top", "signal_nested", "signal_draft", "binding_draft", "duplicate_signal"],
)
def test_entry_preamble_monitor_requires_exact_durable_recovery_signal(
    tmp_path, mutation
):
    database = tmp_path / f"entry-preamble-reconciled-durable-{mutation}.db"
    _seed_reconciled_entry_preamble_monitor(database)
    connection = sqlite3.connect(database)
    if mutation == "duplicate_signal":
        connection.execute(
            "INSERT INTO trade_signals SELECT 399, strategy_instance_id, kol_id, chat_id, message_id, symbol, side, venue, source_type, action, status, processed_at, payload_json FROM trade_signals WHERE id = 398"
        )
    elif mutation == "binding_draft":
        raw = connection.execute(
            "SELECT payload_json FROM execution_bindings WHERE id = 266"
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["draft"]["order_legs"][0]["price"] = 1
        connection.execute(
            "UPDATE execution_bindings SET payload_json = ? WHERE id = 266",
            (json.dumps(payload),),
        )
    else:
        raw = connection.execute(
            "SELECT payload_json FROM trade_signals WHERE id = 398"
        ).fetchone()[0]
        payload = json.loads(raw)
        if mutation == "signal_top":
            payload["entry_preamble_assembly"]["assembly_fingerprint"] = "f" * 64
        elif mutation == "signal_nested":
            payload["deepcoin_order_draft"]["entry_preamble_assembly"][
                "assembly_fingerprint"
            ] = "f" * 64
        else:
            payload["deepcoin_order_draft"]["order_legs"][0]["price"] = 1
        connection.execute(
            "UPDATE trade_signals SET payload_json = ? WHERE id = 398",
            (json.dumps(payload),),
        )
    connection.commit()
    connection.close()

    assert read_entry_preamble_invariants(
        database, now=datetime(2026, 8, 8, tzinfo=UTC)
    ) == ("live_entry_preamble_binding_evidence_missing",)


@pytest.mark.parametrize(
    "mutation",
    [
        "margin_mode",
        "position_mode",
        "symbol",
        "source",
        "leg_side",
        "leg_position_side",
        "leg_base_asset_estimate",
        "leg_take_profit",
        "selected_legs",
        "selected_leg_count",
    ],
)
def test_entry_preamble_monitor_proves_complete_canonical_draft_snapshot(
    tmp_path, mutation
):
    database = tmp_path / f"entry-preamble-reconciled-complete-{mutation}.db"
    _seed_reconciled_entry_preamble_monitor(database)
    connection = sqlite3.connect(database)
    evidence = json.loads(
        connection.execute(
            "SELECT evidence_json FROM entry_strategy_assemblies WHERE id = 2"
        ).fetchone()[0]
    )
    snapshot = evidence["order_draft_snapshot"]
    if mutation == "margin_mode":
        snapshot["margin_mode"] = "isolated"
    elif mutation == "position_mode":
        snapshot["position_mode"] = "merge"
    elif mutation == "symbol":
        snapshot["symbol"] = "ETH"
    elif mutation == "source":
        snapshot["source"]["message_id"] = 56
    elif mutation == "leg_side":
        snapshot["order_legs"][0]["side"] = "sell"
    elif mutation == "leg_position_side":
        snapshot["order_legs"][0]["position_side"] = "short"
    elif mutation == "leg_base_asset_estimate":
        snapshot["order_legs"][0]["base_asset_estimate"] = 0.02
    elif mutation == "leg_take_profit":
        snapshot["order_legs"][0]["take_profit_leg"] = {
            "price": 66000,
            "allocation_pct": 100,
        }
    elif mutation == "selected_legs":
        snapshot["selected_entry_leg_indices"] = [2]
    else:
        snapshot["selected_entry_leg_indices"] = [1, 2]
        snapshot["selected_entry_leg_count"] = 2

    final_fingerprint = canonical_fingerprint(evidence)
    old_fingerprint = derive_pre_finalization_fingerprint(evidence)
    repair_fingerprint = build_reconciliation_fingerprint(
        assembly_id=2,
        execution_binding_id=266,
        trade_signal_id=398,
        strategy_instance_id="strategy-1",
        old_fingerprint=old_fingerprint,
        final_fingerprint=final_fingerprint,
    )
    after = json.loads(
        connection.execute("SELECT after_json FROM execution_events").fetchone()[0]
    )
    after["assembly_fingerprint"] = final_fingerprint
    after["repair_fingerprint"] = repair_fingerprint
    connection.execute(
        "UPDATE entry_strategy_assemblies SET evidence_json = ?, fingerprint = ? WHERE id = 2",
        (json.dumps(evidence), final_fingerprint),
    )
    connection.execute(
        "UPDATE execution_events SET after_json = ?, notification_fingerprint = ?",
        (json.dumps(after), repair_fingerprint),
    )
    connection.commit()
    connection.close()

    assert read_entry_preamble_invariants(
        database, now=datetime(2026, 8, 8, tzinfo=UTC)
    ) == ("live_entry_preamble_binding_evidence_missing",)


@pytest.mark.parametrize(
    "mutation",
    ["strategy", "symbol", "source", "margin_mode", "position_mode"],
)
def test_entry_preamble_monitor_rejects_coordinated_snapshot_identity_drift(
    tmp_path, mutation
):
    database = tmp_path / f"entry-preamble-reconciled-identity-{mutation}.db"
    _seed_reconciled_entry_preamble_monitor(database)
    connection = sqlite3.connect(database)
    evidence = json.loads(
        connection.execute(
            "SELECT evidence_json FROM entry_strategy_assemblies WHERE id = 2"
        ).fetchone()[0]
    )
    binding_payload = json.loads(
        connection.execute(
            "SELECT payload_json FROM execution_bindings WHERE id = 266"
        ).fetchone()[0]
    )
    signal_payload = json.loads(
        connection.execute(
            "SELECT payload_json FROM trade_signals WHERE id = 398"
        ).fetchone()[0]
    )
    snapshots = (
        evidence["order_draft_snapshot"],
        binding_payload["draft"],
        signal_payload["deepcoin_order_draft"],
    )
    for snapshot in snapshots:
        if mutation == "strategy":
            snapshot["strategy_instance_id"] = "forged-strategy"
        elif mutation == "symbol":
            snapshot["symbol"] = "ETH"
            snapshot["instrument_id"] = "ETH-USDT-SWAP"
        elif mutation == "source":
            snapshot["source"]["message_id"] = 56
        elif mutation == "margin_mode":
            snapshot["margin_mode"] = "isolated"
        else:
            snapshot["position_mode"] = "merge"

    final_fingerprint = canonical_fingerprint(evidence)
    old_fingerprint = derive_pre_finalization_fingerprint(evidence)
    repair_fingerprint = build_reconciliation_fingerprint(
        assembly_id=2,
        execution_binding_id=266,
        trade_signal_id=398,
        strategy_instance_id="strategy-1",
        old_fingerprint=old_fingerprint,
        final_fingerprint=final_fingerprint,
    )
    after = json.loads(
        connection.execute("SELECT after_json FROM execution_events").fetchone()[0]
    )
    after["assembly_fingerprint"] = final_fingerprint
    after["repair_fingerprint"] = repair_fingerprint
    connection.execute(
        "UPDATE entry_strategy_assemblies SET evidence_json = ?, fingerprint = ? WHERE id = 2",
        (json.dumps(evidence), final_fingerprint),
    )
    connection.execute(
        "UPDATE execution_bindings SET payload_json = ? WHERE id = 266",
        (json.dumps(binding_payload),),
    )
    connection.execute(
        "UPDATE trade_signals SET payload_json = ? WHERE id = 398",
        (json.dumps(signal_payload),),
    )
    connection.execute(
        "UPDATE execution_events SET after_json = ?, notification_fingerprint = ?",
        (json.dumps(after), repair_fingerprint),
    )
    connection.commit()
    connection.close()

    assert read_entry_preamble_invariants(
        database, now=datetime(2026, 8, 8, tzinfo=UTC)
    ) == ("live_entry_preamble_binding_evidence_missing",)


@pytest.mark.parametrize(
    "mutation",
    [
        "snapshot_missing",
        "snapshot_not_mapping",
        "legs_not_list",
        "legs_empty",
        "legs_too_many",
        "count_missing",
        "count_bool",
        "count_mismatch",
    ],
)
def test_entry_preamble_monitor_rejects_invalid_finalized_assembly_shape(
    tmp_path, mutation
):
    database = tmp_path / f"entry-preamble-reconciled-shape-{mutation}.db"
    _seed_reconciled_entry_preamble_monitor(database)
    connection = sqlite3.connect(database)
    evidence = json.loads(
        connection.execute(
            "SELECT evidence_json FROM entry_strategy_assemblies WHERE id = 2"
        ).fetchone()[0]
    )
    if mutation == "snapshot_missing":
        evidence.pop("order_draft_snapshot")
    elif mutation == "snapshot_not_mapping":
        evidence["order_draft_snapshot"] = []
    elif mutation == "legs_not_list":
        evidence["order_draft_snapshot"]["order_legs"] = {}
    elif mutation == "legs_empty":
        evidence["order_draft_snapshot"]["order_legs"] = []
        evidence["final_entry_leg_count"] = 0
    elif mutation == "legs_too_many":
        evidence["order_draft_snapshot"]["order_legs"] = [
            {"price": 64000 + index} for index in range(6)
        ]
        evidence["final_entry_leg_count"] = 6
    elif mutation == "count_missing":
        evidence.pop("final_entry_leg_count")
    elif mutation == "count_bool":
        evidence["final_entry_leg_count"] = True
    elif mutation == "count_mismatch":
        evidence["final_entry_leg_count"] = 1
    final_fingerprint = canonical_fingerprint(evidence)
    old_fingerprint = derive_pre_finalization_fingerprint(evidence)
    repair_fingerprint = build_reconciliation_fingerprint(
        assembly_id=2,
        execution_binding_id=266,
        trade_signal_id=398,
        strategy_instance_id="strategy-1",
        old_fingerprint=old_fingerprint,
        final_fingerprint=final_fingerprint,
    )
    after = json.loads(
        connection.execute("SELECT after_json FROM execution_events").fetchone()[0]
    )
    after["assembly_fingerprint"] = final_fingerprint
    after["repair_fingerprint"] = repair_fingerprint
    connection.execute(
        "UPDATE entry_strategy_assemblies SET evidence_json = ?, fingerprint = ? WHERE id = 2",
        (json.dumps(evidence), final_fingerprint),
    )
    connection.execute(
        "UPDATE execution_events SET after_json = ?, notification_fingerprint = ?",
        (json.dumps(after), repair_fingerprint),
    )
    connection.commit()
    connection.close()

    assert read_entry_preamble_invariants(
        database, now=datetime(2026, 8, 8, tzinfo=UTC)
    ) == ("live_entry_preamble_binding_evidence_missing",)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("action", "other_action"),
        ("status", "confirmed"),
        ("trade_signal_id", 399),
        ("strategy_instance_id", "strategy-2"),
        ("notification_fingerprint", "f" * 64),
    ],
)
def test_entry_preamble_monitor_rejects_mutated_reconciliation_columns(
    tmp_path, column, value
):
    database = tmp_path / f"entry-preamble-reconciled-{column}.db"
    _seed_reconciled_entry_preamble_monitor(database)
    connection = sqlite3.connect(database)
    connection.execute(f"UPDATE execution_events SET {column} = ?", (value,))
    connection.commit()
    connection.close()

    assert read_entry_preamble_invariants(
        database, now=datetime(2026, 8, 8, tzinfo=UTC)
    ) == ("live_entry_preamble_binding_evidence_missing",)


@pytest.mark.parametrize(
    ("document", "field", "value"),
    [
        ("before_json", "assembly_id", 3),
        ("after_json", "execution_binding_id", 267),
        ("before_json", "trade_signal_id", 399),
        ("after_json", "strategy_instance_id", "strategy-2"),
        ("before_json", "assembly_fingerprint", "e" * 64),
        ("after_json", "assembly_fingerprint", "d" * 64),
        ("after_json", "policy_version", "future-policy"),
        ("after_json", "repair_fingerprint", "c" * 64),
    ],
)
def test_entry_preamble_monitor_rejects_mutated_reconciliation_documents(
    tmp_path, document, field, value
):
    database = tmp_path / f"entry-preamble-reconciled-{document}-{field}.db"
    _seed_reconciled_entry_preamble_monitor(database)
    connection = sqlite3.connect(database)
    raw = connection.execute(f"SELECT {document} FROM execution_events").fetchone()[0]
    payload = json.loads(raw)
    payload[field] = value
    connection.execute(
        f"UPDATE execution_events SET {document} = ?", (json.dumps(payload),)
    )
    connection.commit()
    connection.close()

    assert read_entry_preamble_invariants(
        database, now=datetime(2026, 8, 8, tzinfo=UTC)
    ) == ("live_entry_preamble_binding_evidence_missing",)


def test_entry_preamble_monitor_rejects_non_derivable_reconciliation(tmp_path):
    database = tmp_path / "entry-preamble-reconciled-non-derivable.db"
    _seed_reconciled_entry_preamble_monitor(database)
    connection = sqlite3.connect(database)
    evidence = json.loads(
        connection.execute(
            "SELECT evidence_json FROM entry_strategy_assemblies WHERE id = 2"
        ).fetchone()[0]
    )
    evidence["risk_multiplier"] = "0.5"
    connection.execute(
        "UPDATE entry_strategy_assemblies SET evidence_json = ?, fingerprint = ? WHERE id = 2",
        (json.dumps(evidence), canonical_fingerprint(evidence)),
    )
    connection.commit()
    connection.close()

    assert read_entry_preamble_invariants(
        database, now=datetime(2026, 8, 8, tzinfo=UTC)
    ) == ("live_entry_preamble_binding_evidence_missing",)


@pytest.mark.parametrize("conflict", ["duplicate", "malformed"])
def test_entry_preamble_monitor_rejects_conflicting_reconciliation(tmp_path, conflict):
    database = tmp_path / f"entry-preamble-reconciled-{conflict}.db"
    _seed_reconciled_entry_preamble_monitor(database)
    connection = sqlite3.connect(database)
    if conflict == "duplicate":
        connection.execute(
            """INSERT INTO execution_events (
              id, execution_binding_id, trade_signal_id, strategy_instance_id,
              venue, action, status, reason, before_json, after_json,
              notification_status, notification_fingerprint, notification_attempts
            ) SELECT 2, execution_binding_id, trade_signal_id, strategy_instance_id,
                     venue, action, status, reason, before_json, after_json,
                     notification_status, notification_fingerprint,
                     notification_attempts
              FROM execution_events WHERE id = 1"""
        )
    else:
        connection.execute("UPDATE execution_events SET before_json = '{'")
    connection.commit()
    connection.close()

    assert read_entry_preamble_invariants(
        database, now=datetime(2026, 8, 8, tzinfo=UTC)
    ) == ("live_entry_preamble_binding_evidence_missing",)


def test_entry_preamble_monitor_rejects_event_for_previous_final_fingerprint(tmp_path):
    database = tmp_path / "entry-preamble-reconciled-future-mismatch.db"
    _seed_reconciled_entry_preamble_monitor(database)
    connection = sqlite3.connect(database)
    evidence = json.loads(
        connection.execute(
            "SELECT evidence_json FROM entry_strategy_assemblies WHERE id = 2"
        ).fetchone()[0]
    )
    evidence["final_entry_leg_count"] = 1
    connection.execute(
        "UPDATE entry_strategy_assemblies SET evidence_json = ?, fingerprint = ? WHERE id = 2",
        (json.dumps(evidence), canonical_fingerprint(evidence)),
    )
    connection.commit()
    connection.close()

    assert read_entry_preamble_invariants(
        database, now=datetime(2026, 8, 8, tzinfo=UTC)
    ) == ("live_entry_preamble_binding_evidence_missing",)


def test_entry_preamble_monitor_does_not_hide_second_mismatch(tmp_path):
    database = tmp_path / "entry-preamble-two-mismatches.db"
    _seed_reconciled_entry_preamble_monitor(database, second_mismatch=True)

    assert read_entry_preamble_invariants(
        database, now=datetime(2026, 8, 8, tzinfo=UTC)
    ) == ("live_entry_preamble_binding_evidence_missing",)


def test_monitor_detects_adjacent_entry_rollout_mode_drift():
    expectations = MonitorExpectations(
        head=REVIEWED_HEAD,
        auto_trade_enabled=True,
        management_execution_mode="live",
        max_concurrent_positions=4,
        entry_preamble_mode="live",
        entry_message_assembly_v2_mode="disabled",
        entry_revision_v2_mode="disabled",
    )
    result = evaluate_monitor_snapshot(
        _snapshot(
            settings={
                "auto_trade_enabled": True,
                "management_execution_mode": "live",
                "max_concurrent_positions": 4,
                "entry_preamble_mode": "live",
                "entry_message_assembly_v2_mode": "live",
                "entry_revision_v2_mode": "shadow",
            }
        ),
        expectations,
    )
    assert set(result.reason_codes) == {
        "entry_message_assembly_v2_mode_drift",
        "entry_revision_v2_mode_drift",
    }


def test_adjacent_entry_monitor_detects_all_v2_faults_without_writes(tmp_path):
    database = tmp_path / "adjacent-entry-monitor.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE entry_strategy_fragments (id INTEGER PRIMARY KEY, status TEXT, created_at TEXT);
        CREATE TABLE entry_assembly_fragments (id INTEGER PRIMARY KEY, entry_strategy_assembly_id INTEGER, entry_strategy_fragment_id INTEGER);
        CREATE TABLE entry_strategy_assemblies (id INTEGER PRIMARY KEY, strategy_instance_id TEXT, fingerprint TEXT, evidence_json TEXT);
        CREATE TABLE execution_bindings (id INTEGER PRIMARY KEY, strategy_instance_id TEXT, payload_json TEXT);
        CREATE TABLE strategy_revision_batches (id INTEGER PRIMARY KEY, execution_binding_id INTEGER, revision_kind TEXT, status TEXT, target_snapshot_json TEXT, market_snapshot_json TEXT, replacement_json TEXT);
        CREATE TABLE strategy_revision_legs (id INTEGER PRIMARY KEY, revision_batch_id INTEGER, action TEXT, status TEXT);
        CREATE TABLE entry_revision_replacements (id INTEGER PRIMARY KEY, revision_batch_id INTEGER, status TEXT);
        CREATE TABLE position_protection_ledger (id INTEGER PRIMARY KEY, execution_binding_id INTEGER, pos_id TEXT, purpose TEXT, status TEXT);
        INSERT INTO entry_strategy_fragments VALUES
          (1, 'pending', '2026-08-08 00:00:00'),
          (2, 'consumed', '2026-08-08 00:00:00'),
          (3, 'consumed', '2026-08-08 00:00:00');
        INSERT INTO entry_assembly_fragments VALUES (1, 1, 3);
        INSERT INTO entry_strategy_assemblies VALUES
          (1, 'strategy-1', 'fp-1', '{"effective_risk_budget_usdt":"10","order_draft_snapshot":{"order_legs":[{"estimated_stop_loss_usdt":"11"}]}}');
        INSERT INTO execution_bindings VALUES (1, 'strategy-1', '{"draft":{}}');
        INSERT INTO strategy_revision_batches VALUES
          (1, 1, 'entry_sizing', 'rebuilding', '{"fragment_ids":[]}', '{"position":{"posId":"p1"},"verified_stop":null}', '{"risk_budget_usdt":"10","order_legs":[]}');
        INSERT INTO strategy_revision_legs VALUES (1, 1, 'cancel_pending', 'cancel_submitting');
        INSERT INTO entry_revision_replacements VALUES (1, 1, 'submitted');
        """
    )
    connection.commit()
    before = database.read_bytes()

    codes = read_adjacent_entry_invariants(
        database, now=datetime(2026, 8, 8, 1, 0, tzinfo=UTC)
    )

    assert set(codes) == {
        "stale_adjacent_entry_admission",
        "consumed_entry_fragment_missing_assembly",
        "live_entry_assembly_binding_evidence_missing",
        "entry_revision_risk_budget_exceeded",
        "entry_revision_replacement_before_old_terminal",
        "live_entry_revision_protection_unverified",
    }
    assert database.read_bytes() == before


def test_entry_preamble_monitor_accepts_valid_bindings_from_multiple_chats(tmp_path):
    database = tmp_path / "entry-preamble-multi-chat-monitor.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE entry_preambles (id INTEGER PRIMARY KEY, raw_message_id INTEGER, chat_id INTEGER, message_id INTEGER, symbol TEXT, side TEXT, status TEXT, created_at TEXT);
        CREATE TABLE raw_messages (id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER, posted_at TEXT);
        CREATE TABLE signal_candidates (id INTEGER PRIMARY KEY, raw_message_id INTEGER, event_type TEXT, management_action TEXT);
        CREATE TABLE entry_strategy_assemblies (id INTEGER PRIMARY KEY, strategy_instance_id TEXT, fingerprint TEXT);
        CREATE TABLE execution_bindings (id INTEGER PRIMARY KEY, strategy_instance_id TEXT, payload_json TEXT);
        INSERT INTO entry_strategy_assemblies VALUES
          (1, 'chat-100-strategy', 'fingerprint-100'),
          (2, 'chat-200-strategy', 'fingerprint-200');
        INSERT INTO execution_bindings VALUES
          (1, 'chat-100-strategy', '{"draft":{"entry_preamble_assembly":{"assembly_fingerprint":"fingerprint-100"}}}'),
          (2, 'chat-200-strategy', '{"draft":{"entry_preamble_assembly":{"assembly_fingerprint":"fingerprint-200"}}}');
        """
    )
    connection.commit()

    assert read_entry_preamble_invariants(
        database,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    ) == ()


def test_entry_preamble_monitor_ignores_shadow_preamble_behind_entry_boundary(tmp_path):
    database = tmp_path / "entry-preamble-shadow-monitor.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE entry_preambles (id INTEGER PRIMARY KEY, raw_message_id INTEGER, chat_id INTEGER, message_id INTEGER, symbol TEXT, side TEXT, status TEXT, created_at TEXT);
        CREATE TABLE raw_messages (id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER, posted_at TEXT);
        CREATE TABLE signal_candidates (id INTEGER PRIMARY KEY, raw_message_id INTEGER, event_type TEXT, management_action TEXT);
        CREATE TABLE entry_strategy_assemblies (id INTEGER PRIMARY KEY, strategy_instance_id TEXT, fingerprint TEXT);
        CREATE TABLE execution_bindings (id INTEGER PRIMARY KEY, strategy_instance_id TEXT, payload_json TEXT);
        INSERT INTO raw_messages VALUES
          (101, 9, 9901, '2026-08-04 00:00:00'),
          (102, 9, 9902, '2026-08-04 00:01:00'),
          (103, 9, 9903, '2026-08-04 00:02:00');
        INSERT INTO entry_preambles VALUES
          (1, 101, 9, 9901, 'BTCUSDT', 'long', 'pending', '2026-08-04 00:00:00'),
          (2, 103, 9, 9903, 'BTCUSDT', 'long', 'pending', '2026-08-04 00:02:00');
        INSERT INTO signal_candidates VALUES (1, 102, 'entry_signal', NULL);
        """
    )
    connection.commit()

    codes = read_entry_preamble_invariants(
        database,
        now=datetime(2026, 8, 4, 0, 3, tzinfo=UTC),
    )

    assert codes == ()


def test_entry_preamble_monitor_does_not_truncate_recent_boundary(tmp_path):
    database = tmp_path / "entry-preamble-large-monitor.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE entry_preambles (id INTEGER PRIMARY KEY, raw_message_id INTEGER, chat_id INTEGER, message_id INTEGER, symbol TEXT, side TEXT, status TEXT, created_at TEXT);
        CREATE TABLE raw_messages (id INTEGER PRIMARY KEY, chat_id INTEGER, message_id INTEGER, posted_at TEXT);
        CREATE TABLE signal_candidates (id INTEGER PRIMARY KEY, raw_message_id INTEGER, event_type TEXT, management_action TEXT);
        CREATE TABLE entry_strategy_assemblies (id INTEGER PRIMARY KEY, strategy_instance_id TEXT, fingerprint TEXT);
        CREATE TABLE execution_bindings (id INTEGER PRIMARY KEY, strategy_instance_id TEXT, payload_json TEXT);
        INSERT INTO raw_messages VALUES (1, 9, 9901, '2026-08-04 00:00:00');
        INSERT INTO entry_preambles VALUES (1, 1, 9, 9901, 'BTCUSDT', 'long', 'pending', '2026-08-04 00:00:00');
        """
    )
    unrelated = [
        (index + 2, 99, index + 1, f"2026-08-04 00:{index // 60:02d}:{index % 60:02d}")
        for index in range(1001)
    ]
    connection.executemany("INSERT INTO raw_messages VALUES (?, ?, ?, ?)", unrelated)
    connection.executemany(
        "INSERT INTO signal_candidates VALUES (?, ?, 'entry_signal', NULL)",
        [(index + 1, row[0]) for index, row in enumerate(unrelated)],
    )
    connection.execute(
        "INSERT INTO raw_messages VALUES (2005, 9, 9902, '2026-08-05 00:00:00')"
    )
    connection.execute(
        "INSERT INTO signal_candidates VALUES (2005, 2005, 'entry_signal', NULL)"
    )
    connection.commit()

    assert read_entry_preamble_invariants(
        database,
        now=datetime(2026, 8, 6, tzinfo=UTC),
    ) == ()


def test_composite_monitor_reader_detects_persisted_faults_without_writes(tmp_path):
    database = tmp_path / "composite-monitor.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE strategy_management_batches (
          id INTEGER PRIMARY KEY, status TEXT, management_contract_json TEXT
        );
        CREATE TABLE strategy_management_legs (
          id INTEGER PRIMARY KEY, management_batch_id INTEGER,
          execution_order_leg_id INTEGER, pos_id TEXT
        );
        CREATE TABLE strategy_management_components (
          id INTEGER PRIMARY KEY, management_batch_id INTEGER,
          strategy_management_leg_id INTEGER, component_kind TEXT,
          status TEXT, desired_json TEXT, evidence_json TEXT,
          last_progress_at TEXT, updated_at TEXT
        );
        CREATE TABLE position_mutation_intents (
          id INTEGER PRIMARY KEY, idempotency_key TEXT, operation TEXT,
          status TEXT
        );
        CREATE TABLE position_protection_ledger (
          id INTEGER PRIMARY KEY, execution_order_leg_id INTEGER, pos_id TEXT,
          purpose TEXT, size_text TEXT, status TEXT
        );
        """
    )
    contract = json.dumps({"required_components": ["converge_partial_close", "replace_remaining_protection"]})
    stale = "2026-07-30 00:00:00"
    connection.execute(
        "INSERT INTO strategy_management_batches VALUES (1, 'succeeded', ?)",
        (contract,),
    )
    connection.execute(
        "INSERT INTO strategy_management_batches VALUES (2, 'executing', ?)",
        (contract,),
    )
    connection.execute("INSERT INTO strategy_management_legs VALUES (1, 1, 11, 'pos-1')")
    connection.execute("INSERT INTO strategy_management_legs VALUES (2, 2, 12, 'pos-2')")
    connection.executemany(
        "INSERT INTO strategy_management_components VALUES (?, 1, 1, ?, ?, ?, ?, ?, ?)",
        [
            (1, "converge_partial_close", "confirmed", json.dumps({"target_remaining_size": "5"}), "[]", stale, stale),
            (2, "replace_remaining_protection", "pending", "{}", "[]", stale, stale),
        ],
    )
    connection.execute(
        "INSERT INTO strategy_management_components VALUES (3, 2, 2, 'consume_take_profit_stage', 'pending', '{}', '[]', ?, ?)",
        (stale, stale),
    )
    connection.executemany(
        "INSERT INTO position_mutation_intents VALUES (?, ?, 'close_position', 'submitted')",
        [(1, "1:close:attempt:1"), (2, "1:close:attempt:2")],
    )
    connection.execute(
        "INSERT INTO position_protection_ledger VALUES (1, 11, 'pos-1', 'take_profit', '4', 'verified')"
    )
    connection.commit()
    before = database.read_bytes()
    live_snapshot = tmp_path / "deepcoin_live_positions.json"
    live_snapshot.write_text(json.dumps({
        "captured_at": "2026-08-05T00:00:00+00:00",
        "payload": {"_live_source": {"positions": [
            {"posId": "pos-1", "pos": "3"},
            {"posId": "pos-2", "pos": "2"},
        ]}},
    }), encoding="utf-8")

    codes = read_composite_management_invariants(
        database,
        now=datetime(2026, 8, 5, tzinfo=UTC),
        live_position_snapshot_path=live_snapshot,
    )

    assert set(codes) == {
        "completed_batch_missing_component_evidence",
        "duplicate_composite_close_submission",
        "live_position_retained_tp_oversized",
        "composite_position_without_verified_stop",
        "stalled_composite_component",
    }
    assert database.read_bytes() == before


def test_composite_monitor_allows_confirmed_partial_fill_attempt_history(tmp_path):
    database = tmp_path / "composite-retry-monitor.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE strategy_management_batches (id INTEGER PRIMARY KEY, status TEXT, management_contract_json TEXT);
        CREATE TABLE strategy_management_legs (id INTEGER PRIMARY KEY, management_batch_id INTEGER, execution_order_leg_id INTEGER, pos_id TEXT);
        CREATE TABLE strategy_management_components (id INTEGER PRIMARY KEY, management_batch_id INTEGER, strategy_management_leg_id INTEGER, component_kind TEXT, status TEXT, desired_json TEXT, evidence_json TEXT, last_progress_at TEXT, updated_at TEXT);
        CREATE TABLE position_mutation_intents (id INTEGER PRIMARY KEY, idempotency_key TEXT, operation TEXT, status TEXT);
        CREATE TABLE position_protection_ledger (id INTEGER PRIMARY KEY, execution_order_leg_id INTEGER, pos_id TEXT, purpose TEXT, size_text TEXT, status TEXT);
        INSERT INTO strategy_management_batches VALUES (1, 'succeeded', '{}');
        INSERT INTO position_mutation_intents VALUES (1, '9:close:attempt:1', 'close_position', 'confirmed');
        INSERT INTO position_mutation_intents VALUES (2, '9:close:attempt:2', 'close_position', 'confirmed');
        """
    )
    connection.commit()

    assert read_composite_management_invariants(
        database, now=datetime(2026, 8, 5, tzinfo=UTC)
    ) == ()


def test_composite_monitor_checks_completion_evidence_for_every_leg(tmp_path):
    database = tmp_path / "composite-multileg-monitor.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE strategy_management_batches (id INTEGER PRIMARY KEY, status TEXT, management_contract_json TEXT);
        CREATE TABLE strategy_management_legs (id INTEGER PRIMARY KEY, management_batch_id INTEGER, execution_order_leg_id INTEGER, pos_id TEXT);
        CREATE TABLE strategy_management_components (id INTEGER PRIMARY KEY, management_batch_id INTEGER, strategy_management_leg_id INTEGER, component_kind TEXT, status TEXT, desired_json TEXT, evidence_json TEXT, last_progress_at TEXT, updated_at TEXT);
        CREATE TABLE position_mutation_intents (id INTEGER PRIMARY KEY, idempotency_key TEXT, operation TEXT, status TEXT);
        CREATE TABLE position_protection_ledger (id INTEGER PRIMARY KEY, execution_order_leg_id INTEGER, pos_id TEXT, purpose TEXT, size_text TEXT, status TEXT);
        """
    )
    contract = json.dumps({"required_components": ["consume_take_profit_stage"]})
    connection.execute(
        "INSERT INTO strategy_management_batches VALUES (1, 'succeeded', ?)",
        (contract,),
    )
    connection.executemany(
        "INSERT INTO strategy_management_legs VALUES (?, 1, ?, ?)",
        [(1, 11, "pos-1"), (2, 12, "pos-2")],
    )
    now_text = "2026-08-05 00:00:00"
    connection.executemany(
        "INSERT INTO strategy_management_components VALUES (?, 1, ?, 'consume_take_profit_stage', 'confirmed', '{}', ?, ?, ?)",
        [
            (1, 1, '[{"order_id":"tp-1"}]', now_text, now_text),
        ],
    )
    connection.commit()

    assert read_composite_management_invariants(
        database, now=datetime(2026, 8, 5, tzinfo=UTC)
    ) == ("completed_batch_missing_component_evidence",)


def test_composite_monitor_rejects_duplicate_component_for_same_leg(tmp_path):
    database = tmp_path / "composite-duplicate-component-monitor.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE strategy_management_batches (id INTEGER PRIMARY KEY, status TEXT, management_contract_json TEXT);
        CREATE TABLE strategy_management_legs (id INTEGER PRIMARY KEY, management_batch_id INTEGER, execution_order_leg_id INTEGER, pos_id TEXT);
        CREATE TABLE strategy_management_components (id INTEGER PRIMARY KEY, management_batch_id INTEGER, strategy_management_leg_id INTEGER, component_kind TEXT, status TEXT, desired_json TEXT, evidence_json TEXT, last_progress_at TEXT, updated_at TEXT);
        CREATE TABLE position_mutation_intents (id INTEGER PRIMARY KEY, idempotency_key TEXT, operation TEXT, status TEXT);
        CREATE TABLE position_protection_ledger (id INTEGER PRIMARY KEY, execution_order_leg_id INTEGER, pos_id TEXT, purpose TEXT, size_text TEXT, status TEXT);
        """
    )
    contract = json.dumps({"required_components": ["consume_take_profit_stage"]})
    connection.execute(
        "INSERT INTO strategy_management_batches VALUES (1, 'succeeded', ?)",
        (contract,),
    )
    connection.execute(
        "INSERT INTO strategy_management_legs VALUES (1, 1, 11, 'pos-1')"
    )
    now_text = "2026-08-05 00:00:00"
    connection.executemany(
        "INSERT INTO strategy_management_components VALUES (?, 1, 1, 'consume_take_profit_stage', 'confirmed', '{}', '[{\"ok\":true}]', ?, ?)",
        [(1, now_text, now_text), (2, now_text, now_text)],
    )
    connection.commit()

    assert read_composite_management_invariants(
        database, now=datetime(2026, 8, 5, tzinfo=UTC)
    ) == ("completed_batch_missing_component_evidence",)


def test_monitor_inputs_and_result_are_frozen():
    result = evaluate_monitor_snapshot(_snapshot(), EXPECTATIONS)

    with pytest.raises(FrozenInstanceError):
        EXPECTATIONS.head = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.healthy = False  # type: ignore[misc]


@pytest.mark.parametrize(
    ("snapshot", "expected_reasons"),
    [
        (_snapshot(service_state="inactive"), ("service_inactive",)),
        (
            _snapshot(
                settings={
                    "auto_trade_enabled": False,
                    "management_execution_mode": "shadow",
                    "max_concurrent_positions": 8,
                    "entry_preamble_mode": "shadow",
                }
            ),
            (
                "auto_trade_enabled_drift",
                "entry_preamble_mode_drift",
                "management_execution_mode_drift",
                "max_concurrent_positions_drift",
            ),
        ),
        (_snapshot(journal_error_count=2), ("journal_errors",)),
        (
            _snapshot(adapter_failures=("settings",)),
            ("adapter_failure",),
        ),
    ],
)
def test_system_drift_and_adapter_failure_alert(snapshot, expected_reasons):
    result = evaluate_monitor_snapshot(snapshot, EXPECTATIONS)

    assert result.healthy is False
    assert result.reason_codes == tuple(sorted(expected_reasons))


def test_head_drift_is_version_context_not_safety_failure():
    result = evaluate_monitor_snapshot(_snapshot(head=OTHER_HEAD), EXPECTATIONS)

    assert result.healthy is True
    assert result.reason_codes == ()
    assert result.details == {
        "head": OTHER_HEAD,
        "expected_head": REVIEWED_HEAD,
    }


def test_entry_preamble_mode_drift_retains_only_valid_modes():
    settings = dict(_snapshot().settings)
    settings["entry_preamble_mode"] = "shadow"

    result = evaluate_monitor_snapshot(_snapshot(settings=settings), EXPECTATIONS)

    assert result.reason_codes == ("entry_preamble_mode_drift",)
    assert result.details == {
        "entry_preamble_mode": "shadow",
        "expected_entry_preamble_mode": "live",
    }
    alert = format_monitor_alert(result, checked_at="2026-08-06T00:00:00+00:00")
    assert "前置仓位提示模式与批准设置不同" in alert
    assert "entry_preamble_mode_drift" in alert


@pytest.mark.parametrize("invalid_expected", [1, "true", None])
def test_malformed_expected_auto_trade_value_is_never_compared_or_retained(
    invalid_expected,
):
    expectations = MonitorExpectations(
        head=REVIEWED_HEAD,
        auto_trade_enabled=invalid_expected,
        management_execution_mode="live",
        max_concurrent_positions=4,
        entry_preamble_mode="live",
    )

    result = evaluate_monitor_snapshot(_snapshot(), expectations)

    assert result.reason_codes == ("malformed_snapshot",)
    assert result.details == {}


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("unknown_exchange_outcome", "event_unknown_status"),
        ("submit_unknown", "event_unknown_status"),
        ("recovery_required", "event_recovery_status"),
    ],
)
def test_unknown_and_recovery_event_statuses_alert(status, reason):
    result = evaluate_monitor_snapshot(
        _snapshot(abnormal_events=({"action": "open_market_position", "status": status},)),
        EXPECTATIONS,
    )

    assert result.reason_codes == (reason,)


@pytest.mark.parametrize(
    "event",
    [
        {"action": "submit_entry_order", "status": "submitted", "pos_id": "p-1"},
        {"action": "set_position_tpsl", "status": "submitted", "pos_id": "p-1"},
        {"action": "adjust_position_tpsl", "status": "submitted", "pos_id": "p-1"},
        {
            "action": "close_bound_position_market",
            "status": "submitted",
            "pos_id": "p-1",
        },
    ],
)
def test_normal_submitted_trade_events_do_not_alert(event):
    result = evaluate_monitor_snapshot(
        _snapshot(abnormal_events=(event,)), EXPECTATIONS
    )

    assert result.healthy is True
    assert result.reason_codes == ()


def test_real_manual_close_reservation_and_submission_do_not_alert():
    result = evaluate_monitor_snapshot(
        _snapshot(
            abnormal_events=(
                {
                    "action": "close_bound_position_reservation",
                    "status": "reserved",
                    "pos_id": "1001124101591781",
                },
                {
                    "action": "close_bound_position_reservation",
                    "status": "submitted",
                    "pos_id": "1001124101591781",
                },
                {
                    "action": "close_bound_position_market",
                    "status": "submitted",
                    "pos_id": "1001124101591781",
                },
            )
        ),
        EXPECTATIONS,
    )

    assert result.healthy is True
    assert result.reason_codes == ()


def test_reserved_is_not_globally_normal_for_other_actions():
    result = evaluate_monitor_snapshot(
        _snapshot(
            abnormal_events=(
                {"action": "submit_entry_order", "status": "reserved", "pos_id": "p-1"},
            )
        ),
        EXPECTATIONS,
    )

    assert result.reason_codes == ("event_unknown_status",)


def test_only_duplicate_exact_manual_close_for_same_pos_id_alerts():
    duplicate = {"action": "close_bound_position_market", "status": "submitted", "pos_id": "p-1"}
    result = evaluate_monitor_snapshot(
        _snapshot(
            abnormal_events=(
                duplicate,
                dict(duplicate),
                {**duplicate, "pos_id": "p-2"},
                {**duplicate, "action": "close_position_market"},
            )
        ),
        EXPECTATIONS,
    )

    assert result.reason_codes == ("duplicate_manual_close",)
    assert result.details["duplicate_manual_close_count"] == 1


@pytest.mark.parametrize(
    "audit",
    [
        _healthy_audit(output_complete=False),
        _healthy_audit(snapshot_status="snapshot_unstable"),
        _healthy_audit(
            legacy_pending_management={
                "complete": False,
                "truncated": True,
                "scan_truncated": True,
            }
        ),
    ],
)
def test_incomplete_audit_alerts(audit):
    result = evaluate_monitor_snapshot(_snapshot(audit=audit), EXPECTATIONS)

    assert "audit_incomplete" in result.reason_codes


@pytest.mark.parametrize(
    "audit",
    [
        _healthy_audit(
            counts={
                "blocked": 1,
                "partial_failed": 0,
                "submit_unknown": 0,
                "recovery_required": 0,
            }
        ),
        _healthy_audit(malformed_row_count=1),
        _healthy_audit(schema_status="management_schema_missing"),
    ],
)
def test_abnormal_audit_alerts(audit):
    result = evaluate_monitor_snapshot(_snapshot(audit=audit), EXPECTATIONS)

    assert "audit_abnormal" in result.reason_codes


def test_audit_projects_actionable_counts_and_batch_references():
    result = evaluate_monitor_snapshot(
        _snapshot(
            audit=_healthy_audit(
                counts={
                    "blocked": 0,
                    "partial_failed": 0,
                    "submit_unknown": 0,
                    "recovery_required": 2,
                },
                actionable_batches={
                    "total": 2,
                    "returned": 2,
                    "truncated": False,
                    "items": [
                        {
                            "batch_ref": "batch:17",
                            "states": ["recovery_required"],
                        },
                        {
                            "batch_ref": "batch:22",
                            "states": ["recovery_required"],
                        },
                    ],
                },
            )
        ),
        EXPECTATIONS,
    )

    assert result.reason_codes == ("audit_abnormal",)
    assert result.details["audit_state_counts"] == {
        "blocked": 0,
        "partial_failed": 0,
        "recovery_required": 2,
        "submit_unknown": 0,
    }
    assert result.details["actionable_batch_refs"] == (
        ("batch:17", ("recovery_required",)),
        ("batch:22", ("recovery_required",)),
    )
    assert result.details["actionable_batches_total"] == 2
    assert result.details["actionable_batches_truncated"] is False


@pytest.mark.parametrize(
    "actionable_batches",
    [
        None,
        {},
        {"total": True, "returned": 0, "truncated": False, "items": []},
        {"total": 1, "returned": 0, "truncated": False, "items": []},
        {
            "total": 1,
            "returned": 1,
            "truncated": False,
            "items": [{"batch_ref": "batch:0", "states": ["blocked"]}],
        },
        {
            "total": 1,
            "returned": 1,
            "truncated": False,
            "items": [{"batch_ref": "batch:17", "states": ["unknown"]}],
        },
        {
            "total": 1,
            "returned": 1,
            "truncated": False,
            "items": [
                {"batch_ref": "batch:17", "states": ["recovery_required"]},
                {"batch_ref": "batch:17", "states": ["recovery_required"]},
            ],
        },
    ],
)
def test_malformed_actionable_batch_projection_fails_closed(actionable_batches):
    result = evaluate_monitor_snapshot(
        _snapshot(
            audit=_healthy_audit(
                counts={
                    "blocked": 0,
                    "partial_failed": 0,
                    "submit_unknown": 0,
                    "recovery_required": 1,
                },
                actionable_batches=actionable_batches,
            )
        ),
        EXPECTATIONS,
    )

    assert "audit_incomplete" in result.reason_codes
    assert "malformed_snapshot" in result.reason_codes
    assert result.details["audit_abnormal_count"] == 1
    assert "actionable_batch_refs" not in result.details


def test_terminal_blocked_history_is_visible_but_not_alerting():
    result = evaluate_monitor_snapshot(
        _snapshot(
            audit=_healthy_audit(
                counts={
                    "terminal_blocked": 36,
                    "blocked": 0,
                    "partial_failed": 0,
                    "submit_unknown": 0,
                    "recovery_required": 0,
                }
            )
        ),
        EXPECTATIONS,
    )

    assert result.healthy is True
    assert result.reason_codes == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("settings", {"auto_trade_enabled": "definitely"}),
        ("journal_error_count", "not-an-int"),
        ("abnormal_events", ({"action": None, "status": {"raw": "boom"}},)),
        ("audit", {"exception": "raw adapter traceback"}),
    ],
)
def test_malformed_inputs_use_fixed_reason_without_raw_values(field, value):
    result = evaluate_monitor_snapshot(_snapshot(**{field: value}), EXPECTATIONS)

    assert "malformed_snapshot" in result.reason_codes
    rendered = repr(result)
    assert "definitely" not in rendered
    assert "not-an-int" not in rendered
    assert "raw adapter traceback" not in rendered
    assert "boom" not in rendered


def test_formatter_uses_readable_sections_and_sorted_reason_codes():
    result = evaluate_monitor_snapshot(
        _snapshot(service_state="inactive", head=OTHER_HEAD, journal_error_count=2),
        EXPECTATIONS,
    )

    text = format_monitor_alert(
        result,
        checked_at=datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
    )

    assert text.startswith("【🔴立即处理：自动交易服务未正常运行】")
    assert "发生了什么：" in text
    assert "自动交易服务没有正常运行" in text
    assert "当前影响：" in text
    assert "你需要做什么：" in text
    assert "检查时间：2026-07-16 17:30（北京时间）" in text
    assert "技术代码：journal_errors,service_inactive" in text
    assert OTHER_HEAD[:12] not in text
    assert REVIEWED_HEAD[:12] not in text


@pytest.mark.parametrize(
    ("reason_code", "expected_severity", "expected_problem"),
    [
        ("service_inactive", "critical", "自动交易服务没有正常运行"),
        ("auto_trade_enabled_drift", "critical", "自动交易开关与批准设置不同"),
        ("management_execution_mode_drift", "critical", "仓位管理模式与批准设置不同"),
        ("max_concurrent_positions_drift", "critical", "持仓数量限制与批准设置不同"),
        ("event_unknown_status", "critical", "交易请求已经发出，但交易所结果无法确认"),
        ("event_recovery_status", "critical", "仓位管理操作没有正常结束"),
        ("duplicate_manual_close", "critical", "同一仓位可能被重复发起平仓"),
        ("adapter_failure", "critical", "安全监控无法读取关键生产信息"),
        ("audit_incomplete", "critical", "仓位管理记录检查没有完整完成"),
        ("malformed_snapshot", "critical", "安全检查收到无法识别的数据"),
        ("audit_abnormal", "review", "历史仓位管理任务缺少足够证据"),
        ("journal_errors", "review", "交易服务近期记录了程序错误"),
        ("state_invalid", "review", "监控自己的通知记录发生异常"),
    ],
)
def test_alert_presentation_maps_every_reason_to_plain_chinese(
    reason_code,
    expected_severity,
    expected_problem,
):
    presentation = monitor_module.build_monitor_alert_presentation(
        MonitorResult(healthy=False, reason_codes=(reason_code,), details={})
    )

    assert presentation.severity == expected_severity
    assert expected_problem in presentation.problems[0]
    assert presentation.impact
    assert presentation.operator_action


def test_audit_alert_is_readable_for_known_production_batches():
    text = format_monitor_alert(
        MonitorResult(
            healthy=False,
            reason_codes=("audit_abnormal",),
            details={
                "audit_abnormal_count": 2,
                "audit_state_counts": {
                    "blocked": 0,
                    "partial_failed": 0,
                    "recovery_required": 2,
                    "submit_unknown": 0,
                },
                "actionable_batch_refs": (
                    ("batch:17", ("recovery_required",)),
                    ("batch:22", ("recovery_required",)),
                ),
                "actionable_batches_total": 2,
                "actionable_batches_truncated": False,
            },
        ),
        checked_at=datetime(2026, 8, 4, 1, 0, tzinfo=UTC),
    )

    assert text.startswith("【🟡稍后核查：2条历史交易管理记录无法确认】")
    assert "发生了什么：" in text
    assert "当前影响：" in text
    assert "你需要做什么：" in text
    assert "不要手动重复平仓" in text
    assert "管理批次 17、22" in text
    assert "系统定时安全检查，不是 AI Agent" in text
    assert "2026-08-04 09:00（北京时间）" in text
    assert "技术代码：audit_abnormal" in text
    assert "当前版本" not in text
    assert "期望版本" not in text


def test_submit_unknown_escalates_audit_alert_to_critical():
    presentation = monitor_module.build_monitor_alert_presentation(
        MonitorResult(
            healthy=False,
            reason_codes=("audit_abnormal",),
            details={
                "audit_abnormal_count": 1,
                "audit_state_counts": {
                    "blocked": 0,
                    "partial_failed": 0,
                    "recovery_required": 0,
                    "submit_unknown": 1,
                },
            },
        )
    )

    assert presentation.severity == "critical"
    assert presentation.title.startswith("🔴立即处理")
    assert "不要重复下单或平仓" in presentation.operator_action


def test_unknown_alert_reason_uses_safe_critical_fallback():
    presentation = monitor_module.build_monitor_alert_presentation(
        MonitorResult(
            healthy=False,
            reason_codes=("raw-secret-reason",),
            details={"raw": "bot-token-secret"},
        )
    )

    assert presentation.severity == "critical"
    assert "无法解释的问题" in presentation.title
    assert "raw-secret-reason" not in repr(presentation)
    assert "bot-token-secret" not in repr(presentation)


def test_alert_presentation_bounds_multiple_plain_language_problems():
    presentation = monitor_module.build_monitor_alert_presentation(
        MonitorResult(
            healthy=False,
            reason_codes=(
                "adapter_failure",
                "audit_incomplete",
                "journal_errors",
                "service_inactive",
            ),
            details={},
        )
    )

    assert presentation.severity == "critical"
    assert presentation.title == "🔴立即处理：自动交易服务未正常运行"
    assert len(presentation.problems) == 3
    assert presentation.additional_problem_count == 1


def test_audit_alert_explains_bounded_batch_reference_list():
    batch_refs = tuple(
        (f"batch:{batch_id}", ("recovery_required",))
        for batch_id in range(1, 11)
    )
    text = format_monitor_alert(
        MonitorResult(
            healthy=False,
            reason_codes=("audit_abnormal",),
            details={
                "audit_abnormal_count": 12,
                "audit_state_counts": {
                    "blocked": 0,
                    "partial_failed": 0,
                    "recovery_required": 12,
                    "submit_unknown": 0,
                },
                "actionable_batch_refs": batch_refs,
                "actionable_batches_total": 12,
                "actionable_batches_truncated": True,
            },
        ),
        checked_at=datetime(2026, 8, 4, 1, 0, tzinfo=UTC),
    )

    assert "共12个，仅展示前10个" in text
    assert "管理批次 1、2、3、4、5、6、7、8、9、10" in text
    assert "batch:11" not in text


def test_formatter_is_bounded_and_never_emits_sensitive_or_raw_fields():
    secrets = (
        "bot-token-secret",
        "passphrase-secret",
        "DC-ACCESS-KEY",
        "raw-request-secret",
        "raw-response-secret",
        "raw-exception-secret",
    )
    result = evaluate_monitor_snapshot(
        _snapshot(
            service_state="inactive\n" + secrets[0] * 500,
            head=secrets[1] * 500,
            settings={
                "auto_trade_enabled": False,
                "management_execution_mode": secrets[2] * 500,
                "max_concurrent_positions": 999,
                "token": secrets[0],
                "passphrase": secrets[1],
                "headers": secrets[2],
            },
            abnormal_events=(
                {
                    "action": "open_market_position",
                    "status": "submit_unknown",
                    "request": secrets[3],
                    "response": secrets[4],
                    "exception": secrets[5],
                },
            ),
            adapter_failures=(secrets[5],),
        ),
        EXPECTATIONS,
    )

    text = format_monitor_alert(result, checked_at="2026-07-16T09:30:00+00:00")

    assert len(text) <= MAX_ALERT_LENGTH
    assert all(secret not in text for secret in secrets)


def test_realistic_short_bot_token_is_rejected_from_every_string_detail():
    bot_token = "1234567890:AAEabcDEF_ghIJK-lmnOPQrstUVWxyz12345"
    result = evaluate_monitor_snapshot(
        _snapshot(
            service_state=bot_token,
            head=bot_token,
            settings={
                "auto_trade_enabled": False,
                "management_execution_mode": bot_token,
                "max_concurrent_positions": 8,
                "entry_preamble_mode": bot_token,
            },
            adapter_failures=(bot_token,),
        ),
        MonitorExpectations(
            head=bot_token,
            auto_trade_enabled=True,
            management_execution_mode=bot_token,
            max_concurrent_positions=4,
            entry_preamble_mode=bot_token,
        ),
    )

    text = format_monitor_alert(result, checked_at=bot_token)

    assert "malformed_snapshot" in result.reason_codes
    assert bot_token not in repr(result)
    assert bot_token not in text


def test_formatter_revalidates_each_string_detail_field():
    bot_token = "1234567890:AAEabcDEF_ghIJK-lmnOPQrstUVWxyz12345"
    result = MonitorResult(
        healthy=False,
        reason_codes=("malformed_snapshot",),
        details={
            "service_state": bot_token,
            "head": bot_token,
            "expected_head": bot_token,
            "management_execution_mode": bot_token,
            "expected_management_execution_mode": bot_token,
            "adapter_failures": (bot_token,),
        },
    )

    text = format_monitor_alert(result, checked_at=bot_token)

    assert bot_token not in text


def test_oversized_integer_is_malformed_and_formatter_never_crashes():
    oversized = 10**5000
    result = evaluate_monitor_snapshot(
        _snapshot(journal_error_count=oversized),
        EXPECTATIONS,
    )

    assert result.reason_codes == ("malformed_snapshot",)
    assert oversized not in result.details.values()

    injected = MonitorResult(
        healthy=False,
        reason_codes=("malformed_snapshot",),
        details={"journal_error_count": oversized},
    )
    text = format_monitor_alert(injected, checked_at="2026-07-16T09:30:00+00:00")

    assert "安全检查收到无法识别的数据" in text
    assert "日志错误数" not in text
    assert len(text) <= MAX_ALERT_LENGTH


@pytest.mark.parametrize(
    "contents",
    [
        None,
        "not-json",
        "[]",
        "{}",
        json.dumps(
            {
                "last_window_at": None,
                "last_full_audit_date": None,
                "anomaly_fingerprint": None,
                "last_notification_at": None,
                "unexpected": "field",
            }
        ),
        json.dumps(
            {
                "last_window_at": "2026-07-16 09:00:00",
                "last_full_audit_date": None,
                "anomaly_fingerprint": None,
                "last_notification_at": None,
            }
        ),
        json.dumps(
            {
                "last_window_at": None,
                "last_full_audit_date": None,
                "anomaly_fingerprint": "not-a-sha256",
                "last_notification_at": None,
            }
        ),
        json.dumps(
            {
                "last_window_at": None,
                "last_full_audit_date": None,
                "anomaly_fingerprint": "a" * 64,
                "last_notification_at": "2026-07-16T09:00:00+00:00",
                "active_reason_codes": ["raw_unknown_reason"],
            }
        ),
    ],
)
def test_missing_or_malformed_monitor_state_defaults_safely(tmp_path, contents):
    path = tmp_path / "state.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")

    assert load_monitor_state(path) == MonitorState()


def test_monitor_state_is_persisted_atomically_with_exact_fields_and_mode(
    tmp_path, monkeypatch
):
    path = tmp_path / "state.json"
    state = MonitorState(
        last_window_at="2026-07-16T09:00:00+00:00",
        last_full_audit_date="2026-07-16",
        anomaly_fingerprint="a" * 64,
        last_notification_at="2026-07-16T09:01:00+00:00",
    )
    real_replace = os.replace
    replacements = []

    def record_replace(source, destination):
        replacements.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", record_replace)

    save_monitor_state(path, state)

    assert len(replacements) == 1
    source, destination = replacements[0]
    assert os.path.dirname(source) == str(tmp_path)
    assert destination == path
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {
        "last_window_at",
        "last_full_audit_date",
        "anomaly_fingerprint",
        "last_notification_at",
        "active_reason_codes",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_monitor_state(path) == state


def test_legacy_four_field_monitor_state_loads_without_speculative_causes(tmp_path):
    path = tmp_path / "legacy-state.json"
    path.write_text(
        json.dumps(
            {
                "last_window_at": "2026-08-04T01:00:00+00:00",
                "last_full_audit_date": None,
                "anomaly_fingerprint": "a" * 64,
                "last_notification_at": "2026-08-04T01:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    state = load_monitor_state(path)

    assert state.active_reason_codes == ()


def test_monitor_fingerprint_is_order_independent_and_canonical():
    first = MonitorResult(
        healthy=False,
        reason_codes=("service_inactive", "journal_errors"),
        details={"service_state": "inactive", "journal_error_count": 2},
    )
    reordered = MonitorResult(
        healthy=False,
        reason_codes=("journal_errors", "service_inactive"),
        details={"journal_error_count": 2, "service_state": "inactive"},
    )

    fingerprint = fingerprint_monitor_result(first)

    assert fingerprint == fingerprint_monitor_result(reordered)
    assert len(fingerprint) == 64
    assert all(character in "0123456789abcdef" for character in fingerprint)


def test_audit_fingerprint_ignores_unrelated_deployment_heads():
    details = {
        "audit_abnormal_count": 2,
        "audit_state_counts": {
            "blocked": 0,
            "partial_failed": 0,
            "recovery_required": 2,
            "submit_unknown": 0,
        },
        "actionable_batch_refs": (
            ("batch:17", ("recovery_required",)),
            ("batch:22", ("recovery_required",)),
        ),
        "actionable_batches_total": 2,
        "actionable_batches_truncated": False,
        "audit_abnormal": True,
    }
    first = MonitorResult(
        healthy=False,
        reason_codes=("audit_abnormal",),
        details={**details, "head": REVIEWED_HEAD, "expected_head": OTHER_HEAD},
    )
    deployed_later = MonitorResult(
        healthy=False,
        reason_codes=("audit_abnormal",),
        details={**details, "head": OTHER_HEAD, "expected_head": REVIEWED_HEAD},
    )

    assert fingerprint_monitor_result(first) == fingerprint_monitor_result(
        deployed_later
    )


@pytest.mark.parametrize(
    "changed_detail",
    [
        {
            "audit_state_counts": {
                "blocked": 0,
                "partial_failed": 0,
                "recovery_required": 1,
                "submit_unknown": 1,
            }
        },
        {
            "actionable_batch_refs": (
                ("batch:17", ("recovery_required",)),
                ("batch:23", ("recovery_required",)),
            )
        },
        {
            "actionable_batch_refs": (
                ("batch:17", ("recovery_required",)),
                ("batch:22", ("partial_failed",)),
            )
        },
        {"actionable_batches_total": 3},
        {"actionable_batches_truncated": True},
    ],
)
def test_audit_fingerprint_changes_for_operator_relevant_facts(changed_detail):
    base_details = {
        "audit_abnormal_count": 2,
        "audit_state_counts": {
            "blocked": 0,
            "partial_failed": 0,
            "recovery_required": 2,
            "submit_unknown": 0,
        },
        "actionable_batch_refs": (
            ("batch:17", ("recovery_required",)),
            ("batch:22", ("recovery_required",)),
        ),
        "actionable_batches_total": 2,
        "actionable_batches_truncated": False,
        "audit_abnormal": True,
    }
    changed_details = {**base_details, **changed_detail}

    assert fingerprint_monitor_result(
        MonitorResult(False, ("audit_abnormal",), base_details)
    ) != fingerprint_monitor_result(
        MonitorResult(False, ("audit_abnormal",), changed_details)
    )


def test_journal_fingerprint_ignores_unrelated_setting_details():
    first = MonitorResult(
        healthy=False,
        reason_codes=("journal_errors",),
        details={"journal_error_count": 2, "auto_trade_enabled": False},
    )
    unrelated = MonitorResult(
        healthy=False,
        reason_codes=("journal_errors",),
        details={"journal_error_count": 2, "auto_trade_enabled": True},
    )

    assert fingerprint_monitor_result(first) == fingerprint_monitor_result(unrelated)


def test_changed_monitor_fingerprint_notifies_immediately():
    now = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    state = MonitorState(
        anomaly_fingerprint="a" * 64,
        last_notification_at=(now - timedelta(minutes=1)).isoformat(),
    )
    result = MonitorResult(
        healthy=False,
        reason_codes=("service_inactive",),
        details={"service_state": "inactive"},
    )

    decision = decide_monitor_notification(result, state, now=now)

    assert isinstance(decision, MonitorNotificationDecision)
    assert decision.should_notify is True
    assert decision.next_state.anomaly_fingerprint == fingerprint_monitor_result(result)
    assert decision.next_state.last_notification_at == now.isoformat()


def test_partial_recovery_replaces_active_causes_before_final_recovery():
    started_at = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    initial = MonitorResult(
        healthy=False,
        reason_codes=("service_inactive", "auto_trade_enabled_drift"),
        details={
            "service_state": "inactive",
            "auto_trade_enabled": True,
            "expected_auto_trade_enabled": False,
        },
    )
    first = decide_monitor_notification(initial, MonitorState(), now=started_at)

    remaining = MonitorResult(
        healthy=False,
        reason_codes=("auto_trade_enabled_drift",),
        details={
            "auto_trade_enabled": True,
            "expected_auto_trade_enabled": False,
        },
    )
    partial = decide_monitor_notification(
        remaining,
        first.next_state,
        now=started_at + timedelta(minutes=30),
    )
    recovered = decide_monitor_notification(
        MonitorResult(healthy=True, reason_codes=(), details={}),
        partial.next_state,
        now=started_at + timedelta(hours=1),
    )

    assert partial.should_notify is True
    assert partial.kind == "anomaly"
    assert partial.next_state.active_reason_codes == ("auto_trade_enabled_drift",)
    assert recovered.should_notify is True
    assert recovered.kind == "recovery"
    assert recovered.next_state.active_reason_codes == ()


def test_same_monitor_notification_is_suppressed_for_six_hours():
    now = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    result = MonitorResult(
        healthy=False,
        reason_codes=("service_inactive",),
        details={"service_state": "inactive"},
    )
    fingerprint = fingerprint_monitor_result(result)
    recent_state = MonitorState(
        anomaly_fingerprint=fingerprint,
        last_notification_at=(now - timedelta(hours=5, minutes=59)).isoformat(),
    )
    eligible_state = MonitorState(
        anomaly_fingerprint=fingerprint,
        last_notification_at=(now - timedelta(hours=6)).isoformat(),
    )

    suppressed = decide_monitor_notification(result, recent_state, now=now)
    eligible = decide_monitor_notification(result, eligible_state, now=now)

    assert suppressed.should_notify is False
    assert suppressed.next_state == recent_state
    assert eligible.should_notify is True
    assert eligible.next_state.last_notification_at == now.isoformat()


def test_same_low_priority_monitor_notification_stays_suppressed_after_six_hours():
    now = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    result = MonitorResult(
        healthy=False,
        reason_codes=("audit_abnormal",),
        details={
            "head": OTHER_HEAD,
            "expected_head": REVIEWED_HEAD,
            "audit_abnormal_count": 4,
            "audit_abnormal": True,
        },
    )
    fingerprint = fingerprint_monitor_result(result)
    state = MonitorState(
        anomaly_fingerprint=fingerprint,
        last_notification_at=(now - timedelta(hours=24)).isoformat(),
    )

    decision = decide_monitor_notification(result, state, now=now)

    assert decision.should_notify is False
    assert decision.next_state == state


def test_changed_low_priority_monitor_fingerprint_notifies_immediately():
    now = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    previous = MonitorResult(
        healthy=False,
        reason_codes=("audit_abnormal",),
        details={
            "head": OTHER_HEAD,
            "expected_head": REVIEWED_HEAD,
            "audit_abnormal_count": 4,
            "audit_abnormal": True,
        },
    )
    changed = MonitorResult(
        healthy=False,
        reason_codes=("audit_abnormal",),
        details={
            "head": OTHER_HEAD,
            "expected_head": REVIEWED_HEAD,
            "audit_abnormal_count": 5,
            "audit_abnormal": True,
        },
    )
    state = MonitorState(
        anomaly_fingerprint=fingerprint_monitor_result(previous),
        last_notification_at=(now - timedelta(minutes=1)).isoformat(),
    )

    decision = decide_monitor_notification(changed, state, now=now)

    assert decision.should_notify is True
    assert decision.next_state.anomaly_fingerprint == fingerprint_monitor_result(changed)
    assert decision.next_state.last_notification_at == now.isoformat()


def test_healthy_monitor_result_silently_clears_active_fingerprint():
    now = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    previous_notification = (now - timedelta(minutes=30)).isoformat()
    state = MonitorState(
        last_window_at="2026-07-16T09:30:00+00:00",
        last_full_audit_date="2026-07-16",
        anomaly_fingerprint="a" * 64,
        last_notification_at=previous_notification,
    )
    result = MonitorResult(healthy=True, reason_codes=(), details={})

    decision = decide_monitor_notification(result, state, now=now)

    assert decision.should_notify is False
    assert decision.next_state == MonitorState(
        last_window_at=state.last_window_at,
        last_full_audit_date=state.last_full_audit_date,
        anomaly_fingerprint=None,
        last_notification_at=previous_notification,
    )


def test_service_failure_then_complete_check_sends_one_recovery_notice(tmp_path):
    state_path = tmp_path / "service-recovery-state.json"
    config = SimpleNamespace(bot_token="token", chat_id="chat")
    deliveries = []

    failed = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(service_state="inactive"),
        now=datetime(2026, 8, 4, 0, 0, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )
    recovered = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 8, 4, 0, 30, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )

    assert failed.notification_status == "sent"
    assert recovered.notification_status == "sent"
    assert len(deliveries) == 2
    assert deliveries[1]["text"].startswith(
        "【🔵状态提醒：生产安全监控已恢复正常】"
    )
    assert "无需处理" in deliveries[1]["text"]
    assert "系统定时安全检查，不是 AI Agent" in deliveries[1]["text"]
    state = load_monitor_state(state_path)
    assert state.active_reason_codes == ()
    assert state.anomaly_fingerprint is None


def test_skipped_audit_cannot_claim_recovery_before_complete_healthy_audit(tmp_path):
    state_path = tmp_path / "audit-recovery-state.json"
    config = SimpleNamespace(bot_token="token", chat_id="chat")
    deliveries = []
    abnormal_audit = _healthy_audit(
        counts={
            "blocked": 0,
            "partial_failed": 0,
            "submit_unknown": 0,
            "recovery_required": 1,
        }
    )

    first = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(audit=abnormal_audit),
        now=datetime(2026, 8, 4, 1, 0, tzinfo=UTC),
        notify=True,
        force_full_audit=True,
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )
    skipped = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )
    pending_state = load_monitor_state(state_path)
    recovered = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(audit=_healthy_audit()),
        now=datetime(2026, 8, 5, 1, 0, tzinfo=UTC),
        notify=True,
        force_full_audit=True,
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )

    assert first.notification_status == "sent"
    assert skipped.notification_status == "not_needed"
    assert pending_state.active_reason_codes == ("audit_abnormal",)
    assert len(deliveries) == 2
    assert recovered.notification_status == "sent"
    assert deliveries[-1]["text"].startswith(
        "【🔵状态提醒：生产安全监控已恢复正常】"
    )


def test_new_non_audit_alert_cannot_discard_an_unrechecked_audit_cause(tmp_path):
    state_path = tmp_path / "mixed-cause-recovery-state.json"
    config = SimpleNamespace(bot_token="token", chat_id="chat")
    deliveries = []
    abnormal_audit = _healthy_audit(
        counts={
            "blocked": 0,
            "partial_failed": 0,
            "submit_unknown": 0,
            "recovery_required": 1,
        }
    )

    run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(audit=abnormal_audit),
        now=datetime(2026, 8, 4, 1, 0, tzinfo=UTC),
        notify=True,
        force_full_audit=True,
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )
    run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(service_state="inactive"),
        now=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )
    service_recovered = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 8, 5, 0, 30, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )

    assert service_recovered.notification_status == "not_needed"
    assert len(deliveries) == 2
    assert load_monitor_state(state_path).active_reason_codes == ("audit_abnormal",)


def test_failed_recovery_delivery_preserves_active_cause_and_retries(tmp_path):
    state_path = tmp_path / "recovery-retry-state.json"
    config = SimpleNamespace(bot_token="token", chat_id="chat")
    deliveries = []
    recovery_attempts = 0

    def send(**payload):
        nonlocal recovery_attempts
        text = payload["text"]
        if text.startswith("【🔵状态提醒"):
            recovery_attempts += 1
            if recovery_attempts == 1:
                raise RuntimeError("raw delivery secret")
        deliveries.append(payload)

    run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(service_state="inactive"),
        now=datetime(2026, 8, 4, 0, 0, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=send,
    )
    failed_recovery = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 8, 4, 0, 30, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=send,
    )
    pending_state = load_monitor_state(state_path)
    successful_recovery = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 8, 4, 1, 0, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=send,
    )

    assert failed_recovery.notification_status == "delivery_failed"
    assert failed_recovery.monitor_error == "notification_delivery_failed"
    assert pending_state.active_reason_codes == ("service_inactive",)
    assert successful_recovery.notification_status == "sent"
    assert recovery_attempts == 2
    assert load_monitor_state(state_path).active_reason_codes == ()


class _RecordingAdapters:
    def __init__(self, *, service_state="active", audit=None, head=REVIEWED_HEAD):
        self.calls = []
        self.service_state = service_state
        self.audit = audit or _healthy_audit()
        self.head = head

    def read_service_state(self):
        self.calls.append("service")
        return self.service_state

    def read_git_head(self):
        self.calls.append("head")
        return self.head

    def read_settings(self):
        self.calls.append("settings")
        return _snapshot().settings

    def count_journal_errors(self, *, since):
        self.calls.append(("journal", since))
        return 0

    def read_abnormal_events(self, *, since, limit):
        self.calls.append(("events", since, limit))
        return ()

    def run_management_audit(self):
        self.calls.append("audit")
        return self.audit


def test_authoritative_processor_required_journal_signal_is_critical(tmp_path):
    class AuthorityMissingAdapters(_RecordingAdapters):
        def read_journal_summary(self, *, since):
            self.calls.append(("journal", since))
            return {
                "generic_error_count": 0,
                "reason_codes": ("authoritative_processor_required",),
            }

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=tmp_path / "state.json",
        adapters=AuthorityMissingAdapters(),
        now=datetime(2026, 8, 8, 1, 0, tzinfo=UTC),
        notify=False,
    )
    presentation = monitor_module.build_monitor_alert_presentation(outcome.result)

    assert outcome.result.reason_codes == ("authoritative_processor_required",)
    assert presentation.severity == "critical"
    assert "原始消息仍可继续接收" in presentation.impact
    assert "自动解读已停止" in presentation.impact
    assert "不要启用旧识别器" in presentation.operator_action
    assert "123" not in monitor_module.format_monitor_alert(
        outcome.result,
        checked_at="2026-08-08T01:00:00+00:00",
    )


def test_malformed_journal_summary_fails_closed(tmp_path):
    class MalformedJournalAdapters(_RecordingAdapters):
        def read_journal_summary(self, *, since):
            return {"unexpected": "raw journal detail"}

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=tmp_path / "state.json",
        adapters=MalformedJournalAdapters(),
        now=datetime(2026, 8, 8, 1, 0, tzinfo=UTC),
        notify=False,
    )

    assert outcome.result.reason_codes == ("malformed_snapshot",)
    assert "raw journal detail" not in repr(outcome.result)


def test_monitor_orchestration_reads_all_bounded_lightweight_sources(tmp_path):
    adapters = _RecordingAdapters()
    state_path = tmp_path / "state.json"
    save_monitor_state(
        state_path,
        MonitorState(last_window_at="2026-07-16T00:00:00+00:00"),
    )

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=adapters,
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
        notify=False,
        abnormal_event_limit=50,
    )

    assert outcome.exit_code == 0
    assert adapters.calls == [
        "service",
        "head",
        "settings",
        ("journal", datetime(2026, 7, 16, 0, 0, tzinfo=UTC)),
        ("events", datetime(2026, 7, 16, 0, 0, tzinfo=UTC), 50),
    ]


def test_monitor_records_adapter_failure_after_read_only_evaluation(tmp_path):
    adapters = _RecordingAdapters()
    adapters.read_service_state = lambda: (_ for _ in ()).throw(
        RuntimeError("service adapter unavailable")
    )
    session_factory = create_session_factory(tmp_path / "monitor-incident.db")

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=tmp_path / "state.json",
        adapters=adapters,
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
        notify=False,
        runtime_incident_session_factory=session_factory,
        runtime_incident_config=RuntimeIncidentConfig(
            capture_types=frozenset({"monitor_adapter_failure"})
        ),
    )

    assert "adapter_failure" in outcome.result.reason_codes
    with session_factory() as session:
        row = session.query(RuntimeIncident).one()
        assert row.incident_type == "monitor_adapter_failure"


@pytest.mark.parametrize(
    "incident_type",
    [
        "monitor_adapter_failure",
        "monitor_audit_incomplete",
    ],
)
def test_monitor_failure_capture_repeats_one_generation_without_business_rows(
    tmp_path,
    incident_type,
):
    session_factory = create_session_factory(tmp_path / f"{incident_type}.db")
    adapters = _RecordingAdapters(
        audit=_healthy_audit(output_complete=False)
    )
    if incident_type == "monitor_adapter_failure":
        adapters.read_service_state = lambda: (_ for _ in ()).throw(
            RuntimeError("service adapter unavailable")
        )
    kwargs = {
        "expectations": EXPECTATIONS,
        "state_path": tmp_path / f"{incident_type}.json",
        "adapters": adapters,
        "now": datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
        "notify": False,
        "runtime_incident_session_factory": session_factory,
        "runtime_incident_config": RuntimeIncidentConfig(
            capture_types=frozenset({incident_type})
        ),
    }

    for _ in range(3):
        run_production_safety_monitor(**kwargs)

    with session_factory() as session:
        row = session.query(RuntimeIncident).one()
        assert row.incident_type == incident_type
        assert row.generation == 1
        assert row.repeat_count == 3
        assert session.query(StrategyManagementBatch).count() == 0
        assert session.query(PositionProtectionIncident).count() == 0


def test_monitor_missing_notification_config_repeats_one_generation(tmp_path):
    session_factory = create_session_factory(tmp_path / "notify-config-missing.db")
    kwargs = {
        "expectations": EXPECTATIONS,
        "state_path": tmp_path / "notify-config-missing.json",
        "adapters": _RecordingAdapters(service_state="failed"),
        "now": datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
        "notify": True,
        "load_bot_config": lambda: None,
        "runtime_incident_session_factory": session_factory,
        "runtime_incident_config": RuntimeIncidentConfig(
            capture_types=frozenset({"notification_delivery_failure"})
        ),
    }

    outcomes = [run_production_safety_monitor(**kwargs) for _ in range(3)]

    assert {outcome.monitor_error for outcome in outcomes} == {
        "notification_config_missing"
    }
    with session_factory() as session:
        row = session.query(RuntimeIncident).one()
        assert row.incident_type == "notification_delivery_failure"
        assert row.generation == 1
        assert row.repeat_count == 3


def test_monitor_routes_capture_projection_without_writable_database(tmp_path):
    submissions = []

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=tmp_path / "remote-capture.json",
        adapters=_RecordingAdapters(service_state="failed"),
        now=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
        notify=False,
        runtime_incident_capture_url=(
            "http://127.0.0.1:8000/api/runtime-incidents/monitor-capture"
        ),
        runtime_incident_capture_token="m" * 43,
        send_runtime_incident_capture=lambda url, **kwargs: (
            submissions.append((url, kwargs)) or 1
        ),
    )

    assert outcome.result.healthy is False
    assert submissions == [
        (
            "http://127.0.0.1:8000/api/runtime-incidents/monitor-capture",
            {
                "token": "m" * 43,
                "projection": {
                    "schema_version": 1,
                    "checked_at": "2026-08-03T00:00:00+00:00",
                    "reason_codes": [],
                    "adapter_failures": [],
                    "notification_error": None,
                },
            },
        )
    ]


def test_monitor_capture_transport_failure_does_not_change_outcome(tmp_path):
    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=tmp_path / "remote-capture-failure.json",
        adapters=_RecordingAdapters(service_state="failed"),
        now=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
        notify=False,
        runtime_incident_capture_url=(
            "http://127.0.0.1:8000/api/runtime-incidents/monitor-capture"
        ),
        runtime_incident_capture_token="m" * 43,
        send_runtime_incident_capture=lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(RuntimeError("writer unavailable")),
    )

    assert outcome.result.reason_codes == ("service_inactive",)
    assert outcome.monitor_error is None


def test_monitor_adapts_each_durable_severe_protection_incident_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-incident.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:1:2:BTC:long",
            kol_id="kol",
            chat_id=1,
            message_id=2,
            symbol="BTC",
            side="long",
            status="open",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=0,
            purpose="entry",
            order_kind="market",
            pos_id="pos-1",
            status="active",
        )
        session.add(leg)
        session.flush()
        source = PositionProtectionIncident(
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            pos_id="pos-1",
            incident_type="stop_trigger_failed",
            fingerprint="f" * 64,
            evidence_json='{"reason_code":"stop_trigger_failed"}',
            delivery_status="pending",
            created_at=datetime(2026, 7, 16, 0, 20),
            updated_at=datetime(2026, 7, 16, 0, 20),
        )
        session.add(source)
        session.commit()
        source_id = source.id
        source_snapshot = (
            source.incident_type,
            source.fingerprint,
            source.evidence_json,
            source.delivery_status,
            source.updated_at,
        )

    kwargs = {
        "expectations": EXPECTATIONS,
        "state_path": tmp_path / "state.json",
        "adapters": _RecordingAdapters(),
        "notify": False,
        "runtime_incident_session_factory": session_factory,
        "runtime_incident_config": RuntimeIncidentConfig(
            capture_types=frozenset({"severe_protection_incident"})
        ),
    }
    run_production_safety_monitor(
        **kwargs,
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
    )
    run_production_safety_monitor(
        **kwargs,
        now=datetime(2026, 7, 16, 0, 31, tzinfo=UTC),
    )
    run_production_safety_monitor(
        **kwargs,
        now=datetime(2026, 7, 16, 0, 32, tzinfo=UTC),
    )

    with session_factory() as session:
        row = session.query(RuntimeIncident).one()
        assert row.incident_type == "severe_protection_incident"
        assert row.source_record_id == str(source_id)
        assert row.generation == 1
        assert row.repeat_count == 1
        source = session.get(PositionProtectionIncident, source_id)
        assert (
            source.incident_type,
            source.fingerprint,
            source.evidence_json,
            source.delivery_status,
            source.updated_at,
        ) == source_snapshot


def test_monitor_skips_incident_resolved_by_newer_exact_verified_replacement(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "resolved-protection.db")
    incident_at = datetime(2026, 7, 16, 0, 20)
    replacement_at = datetime(2026, 7, 16, 0, 25)
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:1:2:BTC:long",
            kol_id="kol",
            chat_id=1,
            message_id=2,
            symbol="BTC",
            side="long",
            status="open",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=0,
            purpose="entry",
            order_kind="market",
            pos_id="pos-1",
            status="active",
        )
        session.add(leg)
        session.flush()
        session.add(
            PositionProtectionIncident(
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                pos_id="pos-1",
                incident_type="stop_trigger_failed",
                fingerprint="e" * 64,
                evidence_json="{}",
                delivery_status="pending",
                created_at=incident_at,
                updated_at=incident_at,
            )
        )
        replacements = (
            ("primary-2", "stop_loss", "63200"),
            ("backup-2", "backup_stop", "63000"),
            ("tp-2", "take_profit", "65000"),
        )
        for order_id, purpose, trigger_price in replacements:
            session.add(
                PositionProtectionLedger(
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    pos_id="pos-1",
                    instrument_id="BTC-USDT-SWAP",
                    side="long",
                    order_id=order_id,
                    purpose=purpose,
                    trigger_price=trigger_price,
                    size_text="1",
                    status="verified",
                    evidence_source="management_tpsl_readback",
                    evidence_json="{}",
                    created_at=replacement_at,
                    updated_at=replacement_at,
                )
            )
        session.add(
            PositionBackupStopOrder(
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                pos_id="pos-1",
                instrument_id="BTC-USDT-SWAP",
                side="long",
                trigger_price="63000",
                order_id="backup-2",
                client_order_id="backup-client-2",
                status="active",
                request_json="{}",
                created_at=replacement_at,
                updated_at=replacement_at,
            )
        )
        session.add(
            PositionProtectionRevision(
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id="pos-1",
                source="management_tpsl_readback",
                status="active",
                protection_json=json.dumps(
                    {
                        "roles": [
                            "primary_stop",
                            "backup_stop",
                            "take_profit",
                        ],
                        "order_ids": ["primary-2", "backup-2", "tp-2"],
                        "replacements": [
                            {
                                "role": "primary_stop",
                                "order_id": "primary-2",
                                "trigger_price": "63200",
                                "size_text": "1",
                            },
                            {
                                "role": "backup_stop",
                                "order_id": "backup-2",
                                "trigger_price": "63000",
                                "size_text": "1",
                            },
                            {
                                "role": "take_profit",
                                "order_id": "tp-2",
                                "trigger_price": "65000",
                                "size_text": "1",
                            },
                        ],
                    }
                ),
                created_at=replacement_at,
                updated_at=replacement_at,
            )
        )
        session.commit()

    run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=tmp_path / "state.json",
        adapters=_RecordingAdapters(),
        notify=False,
        runtime_incident_session_factory=session_factory,
        runtime_incident_config=RuntimeIncidentConfig(
            capture_types=frozenset({"severe_protection_incident"})
        ),
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
    )

    with session_factory() as session:
        assert session.query(RuntimeIncident).count() == 0


def test_monitor_captures_tp_unknown_and_protection_deadline_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-transitions.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:1:2:ETH:short",
            kol_id="kol", chat_id=1, message_id=2, symbol="ETH", side="short",
            venue="deepcoin", pos_id="pos-1", status="active",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=0, purpose="entry", order_kind="trigger_limit",
            venue="deepcoin", pos_id="pos-1", status="active",
        )
        session.add(leg)
        session.flush()
        session.add(TriggerTakeProfitConvergence(
            venue="deepcoin", execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            desired_take_profits_json="[]", pos_id="pos-1",
            status="submit_unknown", reason_code="convergence_submit_unknown",
            updated_at=datetime(2026, 7, 16, 0, 20),
        ))
        session.add(TriggerProtectionIntent(
            venue="deepcoin", execution_binding_id=binding.id,
            execution_order_leg_id=leg.id, request_fingerprint="f" * 64,
            pre_submit_tpsl_baseline_json="[]", correlation_id="deadline-1",
            recovery_state="failed", recovery_disposition="retry",
            last_reason_code="protection_retry_deadline_expired",
            next_attempt_at=datetime(2026, 7, 16, 0, 10),
            updated_at=datetime(2026, 7, 16, 0, 20),
        ))
        session.commit()

    kwargs = {
        "expectations": EXPECTATIONS,
        "state_path": tmp_path / "state.json",
        "adapters": _RecordingAdapters(),
        "notify": False,
        "runtime_incident_session_factory": session_factory,
        "runtime_incident_config": RuntimeIncidentConfig(
            capture_types=frozenset({"severe_protection_incident"})
        ),
    }
    run_production_safety_monitor(
        **kwargs, now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC)
    )
    run_production_safety_monitor(
        **kwargs, now=datetime(2026, 7, 16, 0, 31, tzinfo=UTC)
    )
    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        intent.retry_attempts = 2
        intent.next_attempt_at = datetime(2026, 7, 16, 0, 25)
        session.commit()
    run_production_safety_monitor(
        **kwargs, now=datetime(2026, 7, 16, 0, 32, tzinfo=UTC)
    )
    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        intent.recovery_disposition = "manual_review"
        session.add(PositionBackupStopOrder(
            venue="deepcoin",
            execution_binding_id=intent.execution_binding_id,
            execution_order_leg_id=intent.execution_order_leg_id,
            pos_id="pos-1", instrument_id="ETH-USDT-SWAP", side="short",
            trigger_price="1935", order_id="backup-active-1",
            client_order_id="backup-active-client-1", status="active",
            request_json="{}",
        ))
        session.commit()
    run_production_safety_monitor(
        **kwargs, now=datetime(2026, 7, 16, 0, 33, tzinfo=UTC)
    )

    with session_factory() as session:
        rows = session.query(RuntimeIncident).order_by(RuntimeIncident.id).all()
    assert len(rows) == 4
    assert all(row.repeat_count == 1 for row in rows)
    assert any(row.source_record_id.startswith("tp-convergence-") for row in rows)
    assert sum(
        row.source_record_id.startswith("trigger-intent-") for row in rows
    ) == 3


def test_protection_incident_classification_distinguishes_recoverable_and_terminal():
    from telegram_kol_research.production_safety_monitor import (
        classify_protection_incident,
    )

    assert classify_protection_incident(
        "native_stop_assignment_pending", exact_backup_verified=True
    ) == "warning"
    assert classify_protection_incident(
        "take_profit_convergence_ready", exact_backup_verified=True
    ) == "healthy"
    assert classify_protection_incident(
        "convergence_submit_unknown", exact_backup_verified=True
    ) == "critical"
    assert classify_protection_incident(
        "position_owner_unverified", exact_backup_verified=False
    ) == "critical"


def test_monitor_notification_failure_is_adapted_without_changing_outcome(tmp_path):
    session_factory = create_session_factory(tmp_path / "monitor-notify-failure.db")
    adapters = _RecordingAdapters(service_state="failed")

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=tmp_path / "state.json",
        adapters=adapters,
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: SimpleNamespace(bot_token="token", chat_id="chat"),
        send_bot_message=lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("Telegram unavailable")
        ),
        runtime_incident_session_factory=session_factory,
        runtime_incident_config=RuntimeIncidentConfig(
            capture_types=frozenset({"notification_delivery_failure"})
        ),
    )

    assert outcome.notification_status == "delivery_failed"
    assert outcome.monitor_error == "notification_delivery_failed"
    with session_factory() as session:
        row = session.query(RuntimeIncident).one()
        assert row.incident_type == "notification_delivery_failure"


@pytest.mark.parametrize(
    ("source_status", "incident_type", "reason_code"),
    [
        (
            "submit_unknown",
            "management_submit_unknown",
            "one_or_more_close_submissions_unknown",
        ),
        (
            "partial_failed",
            "management_partial_failed",
            "one_or_more_close_submissions_failed",
        ),
        (
            "recovery_required",
            "management_recovery_required",
            "reconciliation_required",
        ),
    ],
)
def test_monitor_adapts_each_durable_management_failure_once(
    tmp_path,
    source_status,
    incident_type,
    reason_code,
):
    session_factory = create_session_factory(tmp_path / "management-source.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=2, text="close")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    batch = create_management_batch(
        session_factory,
        idempotency_fingerprint="9" * 64,
        raw_message_id=raw_id,
        recognition_decision_id=91,
        recognition_generation="g1",
        target_lifecycle_id=92,
        strategy_instance_id="deepcoin:1:2:BTC:long",
        execution_binding_id=93,
        intent="full_take_profit",
        effective_action="full_exit",
        requested_fraction=None,
        effective_fraction=1.0,
        partial_round_before=0,
        target_fingerprint="8" * 64,
        target_snapshot={"positions": []},
        legs=[
            ManagementLegCreate(
                execution_order_leg_id=94,
                pos_id="pos-1",
                leg_index=0,
                status=source_status,
                planned_close_size="0.02",
                last_error={"reason": "submission_outcome_unknown"},
            )
        ],
        status=source_status,
        reason_code=reason_code,
    )
    with session_factory() as session:
        persisted_batch = session.get(StrategyManagementBatch, batch.id)
        source_snapshot = (
            persisted_batch.status,
            persisted_batch.reason_code,
            persisted_batch.updated_at,
        )
    kwargs = {
        "expectations": EXPECTATIONS,
        "state_path": tmp_path / "state.json",
        "adapters": _RecordingAdapters(),
        "notify": False,
        "runtime_incident_session_factory": session_factory,
        "runtime_incident_config": RuntimeIncidentConfig(
            capture_types=frozenset({incident_type})
        ),
    }

    run_production_safety_monitor(
        **kwargs,
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
    )
    run_production_safety_monitor(
        **kwargs,
        now=datetime(2026, 7, 16, 0, 31, tzinfo=UTC),
    )
    run_production_safety_monitor(
        **kwargs,
        now=datetime(2026, 7, 16, 0, 32, tzinfo=UTC),
    )

    with session_factory() as session:
        row = session.query(RuntimeIncident).one()
        assert row.incident_type == incident_type
        assert row.source_record_id == str(batch.id)
        assert row.generation == 1
        assert row.repeat_count == 1
        persisted_batch = session.get(StrategyManagementBatch, batch.id)
        assert (
            persisted_batch.status,
            persisted_batch.reason_code,
            persisted_batch.updated_at,
        ) == source_snapshot


def test_durable_source_scan_failure_does_not_change_monitor_outcome(tmp_path):
    class FailingSessionFactory:
        def __call__(self):
            raise RuntimeError("runtime ledger unavailable")

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=tmp_path / "state.json",
        adapters=_RecordingAdapters(),
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
        notify=False,
        runtime_incident_session_factory=FailingSessionFactory(),
        runtime_incident_config=RuntimeIncidentConfig(
            capture_types=frozenset({"severe_protection_incident"})
        ),
    )

    assert outcome.result.healthy is True
    assert outcome.monitor_error is None


def test_management_source_scan_does_not_starve_enabled_type_beyond_limit(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "management-backlog.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=2, text="close")
        session.add(raw)
        session.flush()
        for index in range(101):
            enabled = index == 100
            batch = StrategyManagementBatch(
                idempotency_fingerprint=f"{index:064x}",
                raw_message_id=raw.id,
                recognition_decision_id=1000 + index,
                recognition_generation="g1",
                target_lifecycle_id=2000 + index,
                strategy_instance_id=f"strategy-{index}",
                execution_binding_id=3000 + index,
                intent="full_take_profit",
                effective_action="full_exit",
                execution_mode="live",
                partial_round_before=0,
                status="reconciling" if enabled else "partial_failed",
                reason_code=(
                    "submission_outcome_unknown"
                    if enabled
                    else "one_or_more_close_submissions_failed"
                ),
                target_fingerprint=f"{index + 1000:064x}",
                target_snapshot_json="{}",
                planned_at=datetime(2026, 7, 16, 0, 0),
            )
            session.add(batch)
            session.flush()
            session.add(
                StrategyManagementLeg(
                    management_batch_id=batch.id,
                    execution_order_leg_id=4000 + index,
                    pos_id=f"pos-{index}",
                    leg_index=0,
                    status="submit_unknown" if enabled else "failed",
                )
            )
        session.commit()

    run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=tmp_path / "state.json",
        adapters=_RecordingAdapters(),
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
        notify=False,
        runtime_incident_session_factory=session_factory,
        runtime_incident_config=RuntimeIncidentConfig(
            capture_types=frozenset({"management_submit_unknown"})
        ),
    )

    with session_factory() as session:
        row = session.query(RuntimeIncident).one()
        assert row.incident_type == "management_submit_unknown"
        assert row.source_record_id == "101"


def test_first_run_journal_failure_persists_original_window_for_recovery(tmp_path):
    state_path = tmp_path / "state.json"
    adapters = _RecordingAdapters()
    original_count = adapters.count_journal_errors
    journal_attempts = 0

    def fail_once(*, since):
        nonlocal journal_attempts
        journal_attempts += 1
        if journal_attempts == 1:
            adapters.calls.append(("journal", since))
            raise RuntimeError("raw journal failure")
        return original_count(since=since)

    adapters.count_journal_errors = fail_once
    first_at = datetime(2026, 7, 16, 0, 30, tzinfo=UTC)
    original_window_start = first_at - timedelta(minutes=35)

    first = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=adapters,
        now=first_at,
        notify=False,
    )
    persisted_after_failure = load_monitor_state(state_path)
    second = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=adapters,
        now=first_at + timedelta(minutes=30),
        notify=False,
    )

    assert first.result.reason_codes == ("adapter_failure",)
    assert persisted_after_failure.last_window_at == original_window_start.isoformat()
    assert second.exit_code == 0
    assert adapters.calls.count(("journal", original_window_start)) == 2
    assert adapters.calls.count(("events", original_window_start, 100)) == 2


def test_execution_event_failure_preserves_existing_window_for_recovery(tmp_path):
    state_path = tmp_path / "state.json"
    original_window_start = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    save_monitor_state(
        state_path,
        MonitorState(last_window_at=original_window_start.isoformat()),
    )
    adapters = _RecordingAdapters()
    original_reader = adapters.read_abnormal_events
    event_attempts = 0

    def fail_once(*, since, limit):
        nonlocal event_attempts
        event_attempts += 1
        if event_attempts == 1:
            adapters.calls.append(("events", since, limit))
            raise RuntimeError("raw event failure")
        return original_reader(since=since, limit=limit)

    adapters.read_abnormal_events = fail_once

    first = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=adapters,
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
        notify=False,
    )
    persisted_after_failure = load_monitor_state(state_path)
    second = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=adapters,
        now=datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
        notify=False,
    )

    assert first.result.reason_codes == ("adapter_failure",)
    assert persisted_after_failure.last_window_at == original_window_start.isoformat()
    assert second.exit_code == 0
    assert adapters.calls.count(("journal", original_window_start)) == 2
    assert adapters.calls.count(("events", original_window_start, 100)) == 2


def test_execution_event_adapter_uses_sqlite_read_only_uri_and_select_only(tmp_path):
    database_path = tmp_path / "research.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE execution_events (action TEXT, status TEXT, pos_id TEXT, created_at TEXT)"
        )
        connection.execute(
            "INSERT INTO execution_events VALUES "
            "('close_bound_position_market', 'submitted', 'pos-1', '2026-07-16 00:10:00')"
        )
        connection.commit()
    calls = []

    def connect(path, *, uri=False, **kwargs):
        calls.append((path, uri))
        connection = sqlite3.connect(path, uri=uri, **kwargs)
        connection.set_trace_callback(lambda statement: calls.append(statement))
        return connection

    events = read_abnormal_execution_events(
        database_path,
        since=datetime(2026, 7, 16, 0, 0, tzinfo=UTC),
        limit=20,
        connect=connect,
    )

    assert calls[0] == (f"file:{database_path}?mode=ro", True)
    statements = [call for call in calls[1:] if isinstance(call, str)]
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert "request_json" not in " ".join(statements).lower()
    assert events == ({"action": "close_bound_position_market", "status": "submitted", "pos_id": "pos-1"},)


def test_execution_event_adapter_fails_closed_when_limit_hides_safety_row(tmp_path):
    database_path = tmp_path / "research.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE execution_events (action TEXT, status TEXT, pos_id TEXT, created_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO execution_events VALUES (?, ?, ?, ?)",
            [
                ("open_market_position", "submit_unknown", "hidden", "2026-07-16 00:10:00"),
                ("close_bound_position_market", "submitted", "new-1", "2026-07-16 00:20:00"),
                ("close_bound_position_market", "submitted", "new-2", "2026-07-16 00:30:00"),
            ],
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="events_incomplete"):
        read_abnormal_execution_events(
            database_path,
            since=datetime(2026, 7, 16, 0, 0, tzinfo=UTC),
            limit=2,
        )


def test_malformed_existing_state_is_rebuilt_and_reported_as_fixed_anomaly(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{", encoding="utf-8")

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
        notify=False,
    )

    assert outcome.result.reason_codes == ("state_invalid",)
    assert load_monitor_state(state_path).last_window_at == datetime(
        2026, 7, 16, 0, 30, tzinfo=UTC
    ).isoformat()


def test_unreadable_existing_state_is_rebuilt_and_reported_without_raw_reason(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    state_path.write_text("existing", encoding="utf-8")
    real_read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == state_path:
            raise PermissionError("raw secret path detail")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
        notify=False,
    )

    assert outcome.result.reason_codes == ("state_invalid",)
    assert "secret" not in json.dumps(outcome.result.details)


def test_invalid_state_successful_delivery_sends_once_and_retains_repaired_progress(
    tmp_path,
):
    state_path = tmp_path / "state.json"
    state_path.write_text("{", encoding="utf-8")
    deliveries = []
    config = SimpleNamespace(bot_token="token", chat_id="chat")

    first = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )
    delivered_state = load_monitor_state(state_path)
    second = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )

    assert first.notification_status == "sent"
    assert first.result.reason_codes == ("state_invalid",)
    assert len(deliveries) == 1
    assert delivered_state.last_window_at == "2026-07-16T01:00:00+00:00"
    assert delivered_state.last_full_audit_date == "2026-07-16"
    assert delivered_state.anomaly_fingerprint is not None
    assert delivered_state.last_notification_at == "2026-07-16T01:00:00+00:00"
    assert second.exit_code == 0
    assert second.notification_status == "not_needed"
    assert len(deliveries) == 1


def test_invalid_state_delivery_failure_persists_pending_alert_and_retries(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{", encoding="utf-8")
    config = SimpleNamespace(bot_token="token", chat_id="chat")
    attempts = 0

    def fail_then_succeed(**payload):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("raw delivery secret")

    first = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=fail_then_succeed,
    )
    pending_state = load_monitor_state(state_path)
    second = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=fail_then_succeed,
    )

    assert first.notification_status == "delivery_failed"
    assert pending_state.last_window_at == "2026-07-16T01:00:00+00:00"
    assert pending_state.last_full_audit_date == "2026-07-16"
    assert pending_state.anomaly_fingerprint is not None
    assert pending_state.last_notification_at is None
    assert second.result.reason_codes == ("state_invalid",)
    assert second.notification_status == "sent"
    assert attempts == 2


def test_invalid_state_missing_config_retries_when_config_becomes_available(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{", encoding="utf-8")
    available_config = SimpleNamespace(bot_token="token", chat_id="chat")
    config_calls = 0
    deliveries = []

    def load_config():
        nonlocal config_calls
        config_calls += 1
        if config_calls == 1:
            return SimpleNamespace(bot_token="", chat_id="")
        return available_config

    first = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
        notify=True,
        load_bot_config=load_config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )
    pending_state = load_monitor_state(state_path)
    second = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
        notify=True,
        load_bot_config=load_config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )

    assert first.notification_status == "config_missing"
    assert pending_state.anomaly_fingerprint is not None
    assert pending_state.last_notification_at is None
    assert second.result.reason_codes == ("state_invalid",)
    assert second.notification_status == "sent"
    assert config_calls == 2
    assert len(deliveries) == 1


def test_invalid_state_no_notify_repairs_progress_but_keeps_delivery_pending(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{", encoding="utf-8")

    first = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
        notify=False,
    )
    pending_state = load_monitor_state(state_path)
    second = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(),
        now=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
        notify=False,
        load_bot_config=lambda: (_ for _ in ()).throw(
            AssertionError("disabled diagnostic loaded bot config")
        ),
    )

    assert first.notification_status == "disabled"
    assert pending_state.last_window_at == "2026-07-16T01:00:00+00:00"
    assert pending_state.last_full_audit_date == "2026-07-16"
    assert pending_state.anomaly_fingerprint is not None
    assert pending_state.last_notification_at is None
    assert second.result.reason_codes == ("state_invalid",)
    assert second.notification_status == "disabled"
    assert load_monitor_state(state_path).last_window_at == "2026-07-16T01:30:00+00:00"


def test_invalid_state_with_persistent_anomaly_sends_once_then_suppresses_for_six_hours(
    tmp_path,
):
    state_path = tmp_path / "state.json"
    state_path.write_text("{", encoding="utf-8")
    config = SimpleNamespace(bot_token="token", chat_id="chat")
    deliveries = []

    def run(now):
        return run_production_safety_monitor(
            expectations=EXPECTATIONS,
            state_path=state_path,
            adapters=_RecordingAdapters(service_state="inactive"),
            now=now,
            notify=True,
            load_bot_config=lambda: config,
            send_bot_message=lambda **payload: deliveries.append(payload),
        )

    first = run(datetime(2026, 7, 16, 1, 0, tzinfo=UTC))
    delivered_state = load_monitor_state(state_path)
    second = run(datetime(2026, 7, 16, 1, 30, tzinfo=UTC))
    third = run(datetime(2026, 7, 16, 6, 59, tzinfo=UTC))
    persistent_result = evaluate_monitor_snapshot(
        _snapshot(service_state="inactive"),
        EXPECTATIONS,
    )

    assert first.result.reason_codes == ("service_inactive", "state_invalid")
    assert first.notification_status == "sent"
    assert delivered_state.anomaly_fingerprint == fingerprint_monitor_result(
        persistent_result
    )
    assert second.result.reason_codes == ("service_inactive",)
    assert second.notification_status == "suppressed"
    assert third.notification_status == "suppressed"
    assert len(deliveries) == 1


@pytest.mark.parametrize("first_failure", ["delivery", "missing_config"])
def test_invalid_state_with_persistent_anomaly_retries_unsuccessful_delivery(
    tmp_path, first_failure
):
    state_path = tmp_path / "state.json"
    state_path.write_text("{", encoding="utf-8")
    available_config = SimpleNamespace(bot_token="token", chat_id="chat")
    attempts = 0
    config_calls = 0

    def load_config():
        nonlocal config_calls
        config_calls += 1
        if first_failure == "missing_config" and config_calls == 1:
            return SimpleNamespace(bot_token="", chat_id="")
        return available_config

    def send(**payload):
        nonlocal attempts
        attempts += 1
        if first_failure == "delivery" and attempts == 1:
            raise RuntimeError("raw delivery secret")

    first = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(service_state="inactive"),
        now=datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
        notify=True,
        load_bot_config=load_config,
        send_bot_message=send,
    )
    pending_state = load_monitor_state(state_path)
    second = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(service_state="inactive"),
        now=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
        notify=True,
        load_bot_config=load_config,
        send_bot_message=send,
    )

    assert first.notification_status in {"config_missing", "delivery_failed"}
    assert pending_state.anomaly_fingerprint is not None
    assert pending_state.last_notification_at is None
    assert second.result.reason_codes == ("service_inactive", "state_invalid")
    assert second.notification_status == "sent"
    assert attempts == (1 if first_failure == "missing_config" else 2)


def test_changed_persistent_anomaly_after_combined_delivery_notifies_immediately(
    tmp_path,
):
    state_path = tmp_path / "state.json"
    state_path.write_text("{", encoding="utf-8")
    config = SimpleNamespace(bot_token="token", chat_id="chat")
    deliveries = []

    first = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(service_state="inactive"),
        now=datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )
    changed = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(service_state="failed"),
        now=datetime(2026, 7, 16, 1, 30, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )

    assert first.notification_status == "sent"
    assert changed.result.reason_codes == ("service_inactive",)
    assert changed.result.details["service_state"] == "failed"
    assert changed.notification_status == "sent"
    assert len(deliveries) == 2


def test_persistent_low_priority_anomaly_suppresses_repeat_but_advances_window(
    tmp_path,
):
    state_path = tmp_path / "state.json"
    config = SimpleNamespace(bot_token="token", chat_id="chat")
    deliveries = []
    audit = _healthy_audit(counts={**_healthy_audit()["counts"], "blocked": 4})

    def run(now):
        return run_production_safety_monitor(
            expectations=EXPECTATIONS,
            state_path=state_path,
            adapters=_RecordingAdapters(audit=audit, head=OTHER_HEAD),
            now=now,
            notify=True,
            force_full_audit=True,
            load_bot_config=lambda: config,
            send_bot_message=lambda **payload: deliveries.append(payload),
        )

    first = run(datetime(2026, 7, 16, 1, 0, tzinfo=UTC))
    second = run(datetime(2026, 7, 17, 1, 0, tzinfo=UTC))
    state = load_monitor_state(state_path)

    assert first.notification_status == "sent"
    assert second.result.reason_codes == ("audit_abnormal",)
    assert second.notification_status == "suppressed"
    assert state.last_window_at == "2026-07-17T01:00:00+00:00"
    assert state.last_notification_at == "2026-07-16T01:00:00+00:00"
    assert len(deliveries) == 1


@pytest.mark.parametrize(
    ("now", "last_date", "expected"),
    [
        (datetime(2026, 7, 16, 0, 59, tzinfo=UTC), None, False),
        (datetime(2026, 7, 16, 1, 0, tzinfo=UTC), None, True),
        (datetime(2026, 7, 16, 2, 0, tzinfo=UTC), "2026-07-16", False),
        (datetime(2026, 7, 16, 2, 0, tzinfo=UTC), "2026-07-15", True),
    ],
)
def test_daily_audit_starts_only_after_nine_asia_shanghai(now, last_date, expected):
    assert should_run_daily_audit(now=now, last_successful_date=last_date) is expected


@pytest.mark.parametrize(
    "reason",
    [
        "source_snapshots_differ",
        "source_component_changed_during_read",
        "source_component_set_changed",
    ],
)
def test_transient_private_snapshot_reason_retries_exactly_once(reason):
    results = [
        {"snapshot_status": "snapshot_unstable", "snapshot_reason": reason},
        _healthy_audit(),
    ]
    calls = []

    audit = run_daily_management_audit(lambda: calls.append(1) or results.pop(0))

    assert audit == _healthy_audit()
    assert calls == [1, 1]


@pytest.mark.parametrize(
    "reason",
    [
        "source_component_changed_during_read",
        "source_component_set_changed",
    ],
)
def test_nonzero_transient_component_change_retries_once(monkeypatch, reason):
    import telegram_kol_research.production_safety_monitor as monitor_module

    results = [
        SimpleNamespace(
            returncode=1,
            output=json.dumps(
                {
                    "snapshot_status": "snapshot_unstable",
                    "snapshot_reason": reason,
                }
            ),
        ),
        SimpleNamespace(returncode=0, output=json.dumps(_healthy_audit())),
    ]
    monkeypatch.setattr(
        monitor_module,
        "_run_bounded_command",
        lambda *args, **kwargs: results.pop(0),
    )

    audit = run_daily_management_audit(
        ProductionSafetyAdapters(
            database_path=Path("data/research.db")
        ).run_management_audit
    )

    assert audit == _healthy_audit()
    assert results == []


def test_source_snapshots_differ_nonzero_first_then_zero_retry_can_succeed(monkeypatch):
    import telegram_kol_research.production_safety_monitor as monitor_module

    results = [
        SimpleNamespace(
            returncode=1,
            output=json.dumps(
                {
                    "snapshot_status": "snapshot_unstable",
                    "snapshot_reason": "source_snapshots_differ",
                }
            ),
        ),
        SimpleNamespace(returncode=0, output=json.dumps(_healthy_audit())),
    ]
    monkeypatch.setattr(
        monitor_module,
        "_run_bounded_command",
        lambda *args, **kwargs: results.pop(0),
    )
    adapters = ProductionSafetyAdapters(database_path=Path("data/research.db"))

    audit = run_daily_management_audit(adapters.run_management_audit)

    assert audit == _healthy_audit()
    assert results == []


@pytest.mark.parametrize("second_returncode", [1, 2])
def test_source_snapshots_differ_retry_nonzero_final_result_fails(
    monkeypatch, second_returncode
):
    import telegram_kol_research.production_safety_monitor as monitor_module

    results = [
        SimpleNamespace(
            returncode=1,
            output=json.dumps(
                {
                    "snapshot_status": "snapshot_unstable",
                    "snapshot_reason": "source_snapshots_differ",
                }
            ),
        ),
        SimpleNamespace(
            returncode=second_returncode,
            output=json.dumps(_healthy_audit()),
        ),
    ]
    monkeypatch.setattr(
        monitor_module,
        "_run_bounded_command",
        lambda *args, **kwargs: results.pop(0),
    )
    adapters = ProductionSafetyAdapters(database_path=Path("data/research.db"))

    with pytest.raises(RuntimeError, match="audit_command_failed"):
        run_daily_management_audit(adapters.run_management_audit)

    assert results == []


def test_nonzero_snapshot_mismatch_payload_never_reaches_health_evaluation(
    tmp_path, monkeypatch
):
    import telegram_kol_research.production_safety_monitor as monitor_module

    misleading_payload = {
        **_healthy_audit(),
        "snapshot_reason": "source_snapshots_differ",
    }
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(
            returncode=1,
            output=json.dumps(misleading_payload),
        )

    monkeypatch.setattr(monitor_module, "_run_bounded_command", run)
    state_path = tmp_path / "state.json"
    adapters = _RecordingAdapters()
    adapters.run_management_audit = ProductionSafetyAdapters(
        database_path=Path("data/research.db")
    ).run_management_audit

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=adapters,
        now=datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
        notify=False,
    )

    assert calls == [1, 1]
    assert outcome.exit_code == 1
    assert outcome.result.reason_codes == ("adapter_failure",)
    assert load_monitor_state(state_path).last_full_audit_date is None


@pytest.mark.parametrize(
    "first",
    [
        {"snapshot_status": "snapshot_unavailable", "snapshot_reason": "source_open_failed"},
        {"snapshot_status": "snapshot_unstable", "snapshot_reason": "rollback_journal_present"},
    ],
)
def test_other_audit_failures_are_not_retried(first):
    calls = []

    assert run_daily_management_audit(lambda: calls.append(1) or first) == first
    assert calls == [1]


def test_healthy_monitor_sends_nothing_even_when_notify_enabled(tmp_path):
    adapters = _RecordingAdapters()
    deliveries = []

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=tmp_path / "state.json",
        adapters=adapters,
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: (_ for _ in ()).throw(AssertionError("bot config loaded")),
        send_bot_message=lambda **kwargs: deliveries.append(kwargs),
    )

    assert outcome.exit_code == 0
    assert deliveries == []


def test_eligible_anomaly_sends_once_and_persists_delivery_dedupe(tmp_path):
    state_path = tmp_path / "state.json"
    adapters = _RecordingAdapters(service_state="inactive")
    config = SimpleNamespace(bot_token="token", chat_id="chat")
    deliveries = []
    kwargs = dict(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=adapters,
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )

    first = run_production_safety_monitor(**kwargs)
    second = run_production_safety_monitor(**kwargs)

    assert first.exit_code == 1
    assert second.exit_code == 1
    assert len(deliveries) == 1
    assert deliveries[0]["config"] is config
    assert deliveries[0]["text"].startswith("【🔴立即处理：自动交易服务未正常运行】")


@pytest.mark.parametrize("failure", ["missing", "delivery"])
def test_notification_failure_is_nonzero_without_successful_delivery_state(tmp_path, failure):
    state_path = tmp_path / "state.json"
    config = SimpleNamespace(
        bot_token="" if failure == "missing" else "token",
        chat_id="" if failure == "missing" else "chat",
    )

    def send(**kwargs):
        raise RuntimeError("raw delivery secret")

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=_RecordingAdapters(service_state="inactive"),
        now=datetime(2026, 7, 16, 0, 30, tzinfo=UTC),
        notify=True,
        load_bot_config=lambda: config,
        send_bot_message=send,
    )

    state = load_monitor_state(state_path)
    assert outcome.exit_code == 1
    assert outcome.notification_status in {"config_missing", "delivery_failed"}
    assert state.anomaly_fingerprint is None
    assert state.last_notification_at is None


def test_non_loopback_settings_url_is_rejected_before_http(monkeypatch):
    import telegram_kol_research.production_safety_monitor as monitor_module

    monkeypatch.setattr(
        monitor_module.httpx,
        "stream",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("HTTP called")),
    )

    with pytest.raises(ValueError, match="loopback"):
        read_loopback_settings("http://example.com/api/trading-settings")
    with pytest.raises(ValueError, match="loopback"):
        read_loopback_settings("https://127.0.0.1/api/trading-settings")


def test_monitor_incident_capture_projection_is_closed_and_bounded():
    projection = build_monitor_incident_capture_projection(
        checked_at=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
        reason_codes=("audit_abnormal", "adapter_failure", "audit_incomplete"),
        adapter_failures=("audit", "service", "unknown", "audit"),
        notification_status="config_missing",
        monitor_error="notification_config_missing",
    )

    assert projection == {
        "schema_version": 1,
        "checked_at": "2026-08-03T00:00:00+00:00",
        "reason_codes": ["adapter_failure", "audit_incomplete"],
        "adapter_failures": ["audit", "service"],
        "notification_error": "notification_config_missing",
    }


def test_monitor_incident_capture_client_is_fixed_loopback_no_proxy(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"accepted": True, "captured": 1}

    class Client:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            calls.append(("post", args, kwargs))
            return Response()

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setattr(monitor_module.httpx, "Client", Client)
    payload = {
        "schema_version": 1,
        "checked_at": "2026-08-03T00:00:00+00:00",
        "reason_codes": [],
        "adapter_failures": [],
        "notification_error": None,
    }

    assert send_monitor_incident_capture(
        "http://127.0.0.1:8000/api/runtime-incidents/monitor-capture",
        token="a" * 43,
        projection=payload,
    ) == 1
    assert calls == [
        ("init", {"timeout": 45.0, "trust_env": False}),
        (
            "post",
            ("http://127.0.0.1:8000/api/runtime-incidents/monitor-capture",),
            {
                "headers": {"x-monitor-capture-token": "a" * 43},
                "json": payload,
            },
        ),
    ]


def test_monitor_incident_capture_client_rejects_non_loopback_before_http(monkeypatch):
    monkeypatch.setattr(
        monitor_module.httpx,
        "Client",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("HTTP called")),
    )

    with pytest.raises(ValueError, match="loopback"):
        send_monitor_incident_capture(
            "http://example.com/api/runtime-incidents/monitor-capture",
            token="a" * 43,
            projection={},
        )
    with pytest.raises(ValueError, match="loopback"):
        send_monitor_incident_capture(
            "http://127.0.0.1:9000/api/runtime-incidents/monitor-capture",
            token="a" * 43,
            projection={},
        )


def test_loopback_settings_disable_environment_proxy_trust(monkeypatch):
    import telegram_kol_research.production_safety_monitor as monitor_module

    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield json.dumps(_snapshot().settings).encode("utf-8")

    def stream(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setattr(monitor_module.httpx, "stream", stream)

    assert read_loopback_settings(
        "http://127.0.0.1:8000/api/trading-settings"
    ) == {
        **_snapshot().settings,
        "entry_message_assembly_v2_mode": None,
        "entry_revision_v2_mode": None,
    }
    assert calls == [
        (
            ("GET", "http://127.0.0.1:8000/api/trading-settings"),
            {"timeout": 30.0, "trust_env": False},
        )
    ]


def test_message_operation_coverage_reader_is_authenticated_bounded_and_no_proxy(
    monkeypatch,
):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield json.dumps(_healthy_message_operation_coverage()).encode("utf-8")

    def stream(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setattr(monitor_module.httpx, "stream", stream)

    payload = read_message_operation_coverage(
        "http://127.0.0.1:8000/api/runtime-incidents/message-operation-coverage",
        token="c" * 43,
    )

    assert payload == _healthy_message_operation_coverage()
    assert calls == [
        (
            (
                "GET",
                "http://127.0.0.1:8000/api/runtime-incidents/message-operation-coverage",
            ),
            {
                "headers": {"x-monitor-capture-token": "c" * 43},
                "timeout": 30.0,
                "trust_env": False,
            },
        )
    ]

    with pytest.raises(ValueError, match="fixed loopback"):
        read_message_operation_coverage(
            "http://example.com/api/runtime-incidents/message-operation-coverage",
            token="c" * 43,
        )
    with pytest.raises(ValueError, match="token unavailable"):
        read_message_operation_coverage(
            "http://127.0.0.1:8000/api/runtime-incidents/message-operation-coverage",
            token=None,
        )


def test_message_operation_coverage_reader_rejects_oversized_response(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield b"x" * 32_769

    monkeypatch.setattr(
        monitor_module.httpx,
        "stream",
        lambda *args, **kwargs: Response(),
    )

    with pytest.raises(ValueError, match="too large"):
        read_message_operation_coverage(
            "http://127.0.0.1:8000/api/runtime-incidents/message-operation-coverage",
            token="c" * 43,
        )


def test_successful_daily_audit_records_shanghai_date(tmp_path):
    state_path = tmp_path / "state.json"
    adapters = _RecordingAdapters()

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=adapters,
        now=datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
        notify=False,
    )

    assert outcome.exit_code == 0
    assert adapters.calls[-1] == "audit"
    assert load_monitor_state(state_path).last_full_audit_date == "2026-07-16"


def test_nonzero_audit_with_healthy_json_is_unhealthy_and_not_recorded(
    tmp_path, monkeypatch
):
    import telegram_kol_research.production_safety_monitor as monitor_module

    monkeypatch.setattr(
        monitor_module,
        "_run_bounded_command",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            output=json.dumps(_healthy_audit()),
        ),
    )
    state_path = tmp_path / "state.json"
    adapters = _RecordingAdapters()
    adapters.run_management_audit = ProductionSafetyAdapters(
        database_path=Path("data/research.db")
    ).run_management_audit

    outcome = run_production_safety_monitor(
        expectations=EXPECTATIONS,
        state_path=state_path,
        adapters=adapters,
        now=datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
        notify=False,
    )

    assert outcome.exit_code == 1
    assert outcome.result.reason_codes == ("adapter_failure",)
    assert load_monitor_state(state_path).last_full_audit_date is None


def test_monitor_test_notification_has_fixed_prefix_and_no_dynamic_payload():
    config = SimpleNamespace(bot_token="token", chat_id="chat")
    deliveries = []

    status = send_monitor_test_notification(
        load_bot_config=lambda: config,
        send_bot_message=lambda **payload: deliveries.append(payload),
    )

    assert status == "sent"
    assert deliveries == [
        {"config": config, "text": MONITOR_TEST_NOTIFICATION_TEXT}
    ]
    assert MONITOR_TEST_NOTIFICATION_TEXT.startswith(
        "【监控测试】服务器安全监控通知链路验证"
    )


def test_subprocess_adapters_use_fixed_argv_timeouts_and_output_caps(monkeypatch):
    import telegram_kol_research.production_safety_monitor as monitor_module

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        output = {
            "git": REVIEWED_HEAD + "\n",
            "journalctl": (
                "recognition authority unavailable raw_message_id=123 "
                "reason=authoritative_processor_required\n"
                "unrelated error\n"
            ),
            sys.executable: json.dumps(_healthy_audit()),
        }[argv[0]]
        return SimpleNamespace(returncode=0, output=output)

    monkeypatch.setattr(monitor_module, "_run_bounded_command", run)
    adapters = ProductionSafetyAdapters(
        database_path=Path("data/research.db"),
        checkout_path=Path("/opt/telegram-kol-analyzer"),
    )

    monkeypatch.setattr(
        monitor_module,
        "read_loopback_settings",
        lambda url: _snapshot().settings,
    )
    assert adapters.read_service_state() == "active"
    assert adapters.read_git_head() == REVIEWED_HEAD
    assert adapters.count_journal_errors(
        since=datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    ) == 2
    assert adapters.read_journal_summary(
        since=datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    ) == {
        "generic_error_count": 1,
        "reason_codes": ("authoritative_processor_required",),
    }
    assert adapters.run_management_audit() == _healthy_audit()
    assert calls == [
        (
            (
                "git",
                "-c",
                "safe.directory=/opt/telegram-kol-analyzer",
                "rev-parse",
                "HEAD",
            ),
            {"timeout_seconds": 5, "cwd": Path("/opt/telegram-kol-analyzer")},
        ),
        (
            (
                "journalctl",
                "--unit",
                "telegram-kol.service",
                "--priority",
                "err",
                "--since",
                "2026-07-16T00:00:00+00:00",
                "--no-pager",
                "--output",
                "cat",
            ),
            {"timeout_seconds": 10, "max_output_bytes": 262_144},
        ),
        (
            (
                "journalctl",
                "--unit",
                "telegram-kol.service",
                "--priority",
                "err",
                "--since",
                "2026-07-16T00:00:00+00:00",
                "--no-pager",
                "--output",
                "cat",
            ),
            {"timeout_seconds": 10, "max_output_bytes": 262_144},
        ),
        (
            (
                sys.executable,
                "-m",
                "telegram_kol_research.cli",
                "audit-management-batches",
                "--database-path",
                "data/research.db",
                "--limit",
                "20",
                "--output-format",
                "json",
            ),
            {"timeout_seconds": 180},
        ),
    ]


def test_default_audit_invocation_is_bound_to_current_interpreter_with_stripped_path(
    monkeypatch,
):
    import telegram_kol_research.production_safety_monitor as monitor_module

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, output=json.dumps(_healthy_audit()))

    monkeypatch.setenv("PATH", "/usr/sbin:/usr/bin")
    monkeypatch.setattr(monitor_module, "_run_bounded_command", run)

    audit = ProductionSafetyAdapters(
        database_path=Path("data/research.db")
    ).run_management_audit()

    assert audit == _healthy_audit()
    assert calls[0][0][:3] == (
        sys.executable,
        "-m",
        "telegram_kol_research.cli",
    )


def test_bounded_subprocess_reader_terminates_output_above_hard_cap():
    import telegram_kol_research.production_safety_monitor as monitor_module

    with pytest.raises(RuntimeError, match="output_too_large"):
        monitor_module._run_bounded_command(
            (sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000)"),
            timeout_seconds=5,
            max_output_bytes=32,
        )

    result = monitor_module._run_bounded_command(
        (sys.executable, "-c", "print('bounded')"),
        timeout_seconds=5,
        max_output_bytes=32,
    )
    assert result.returncode == 0
    assert result.output == "bounded\n"
