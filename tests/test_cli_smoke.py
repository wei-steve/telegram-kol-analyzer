import typer
from typer.testing import CliRunner
from copy import deepcopy
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tracemalloc
from types import SimpleNamespace

import pytest

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    list_execution_order_legs,
    upsert_execution_binding,
)
from telegram_kol_research.entry_assembly_fingerprint_repair import (
    canonical_fingerprint,
    derive_pre_finalization_fingerprint,
)
from telegram_kol_research.models import (
    EntryStrategyAssembly,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    TradeSignal,
    TradingSetting,
)
from telegram_kol_research.production_safety_monitor import (
    MonitorExpectations,
    MonitorSnapshot,
    evaluate_monitor_snapshot,
)
from telegram_kol_research.recovery_live_submit import (
    build_deepcoin_trigger_order_payload,
)
from telegram_kol_research.trading_settings import load_trading_settings


_ENTRY_REPAIR_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
_ENTRY_REPAIR_STRATEGY_ID = "deepcoin:-1001:55:BTC:long"


def test_mimo_v2_replay_cli_requires_explicit_ids_and_reports_result(
    tmp_path, monkeypatch
):
    from telegram_kol_research.mimo_v2_replay import (
        MimoV2ReplayResult,
        ReplayPerformance,
    )

    database = tmp_path / "production.db"
    database.touch()
    message_ids = tmp_path / "message-ids.txt"
    message_ids.write_text("7\n9\n", encoding="utf-8")
    artifact_dir = tmp_path / "artifacts"
    captured = {}

    def fake_replay(**kwargs):
        captured.update(kwargs)
        return MimoV2ReplayResult(
            processed=2,
            comparisons=(),
            unsafe_mismatches=0,
            production_writes=0,
            notifications_sent=0,
            performance=ReplayPerformance(
                v1_p95_ms=100.0,
                v2_p95_ms=110.0,
                adapter_p95_ms=1.0,
                passed=True,
                failure_reasons=(),
            ),
            passed=True,
        )

    monkeypatch.setattr(
        "telegram_kol_research.cli.run_mimo_v2_replay",
        fake_replay,
    )

    result = CliRunner().invoke(
        app,
        [
            "replay-mimo-v2",
            "--database",
            str(database),
            "--message-id-file",
            str(message_ids),
            "--artifact-dir",
            str(artifact_dir),
            "--max-messages",
            "20",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["passed"] is True
    assert captured["source_database"] == database
    assert captured["raw_message_ids"] == [7, 9]
    assert captured["artifact_dir"] == artifact_dir
    assert captured["max_messages"] == 20


def test_mimo_v2_replay_cli_exits_nonzero_when_gate_fails(tmp_path, monkeypatch):
    from telegram_kol_research.mimo_v2_replay import (
        MimoV2ReplayResult,
        ReplayPerformance,
    )

    database = tmp_path / "production.db"
    database.touch()
    message_ids = tmp_path / "message-ids.txt"
    message_ids.write_text("7\n", encoding="utf-8")

    monkeypatch.setattr(
        "telegram_kol_research.cli.run_mimo_v2_replay",
        lambda **kwargs: MimoV2ReplayResult(
            processed=1,
            comparisons=(),
            unsafe_mismatches=1,
            production_writes=0,
            notifications_sent=0,
            performance=ReplayPerformance(
                v1_p95_ms=100.0,
                v2_p95_ms=120.0,
                adapter_p95_ms=1.0,
                passed=False,
                failure_reasons=("v2_p95_above_115_percent_of_v1",),
            ),
            passed=False,
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "replay-mimo-v2",
            "--database",
            str(database),
            "--message-id-file",
            str(message_ids),
            "--artifact-dir",
            str(tmp_path / "artifacts"),
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.output)["passed"] is False


@pytest.mark.parametrize(
    "module_name",
    (
        "telegram_kol_research.deployment_preflight",
        "telegram_kol_research.deployment_preflight_cli",
        "telegram_kol_research.deployment_work_evidence",
        "telegram_kol_research.deployment_writer_surface",
    ),
)
def test_retired_deployment_gate_modules_are_absent(module_name):
    assert importlib.util.find_spec(module_name) is None


def test_legacy_deployment_preflight_cli_is_unknown(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "deployment-preflight",
            "--database-path",
            str(tmp_path / "unused.db"),
            "--expected-commit",
            "a" * 40,
            "--change-class",
            "code",
            "--output",
            str(tmp_path / "unused.json"),
        ],
    )

    assert result.exit_code == 2
    assert "No such command 'deployment-preflight'" in result.output
    assert not (tmp_path / "unused.json").exists()


def test_legacy_verify_deployment_preflight_cli_is_unknown(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "verify-deployment-preflight",
            "--input",
            str(tmp_path / "missing.json"),
            "--expected-commit",
            "b" * 40,
            "--change-class",
            "code",
        ],
    )

    assert result.exit_code == 2
    assert "No such command 'verify-deployment-preflight'" in result.output


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_legacy_bridge_cli_is_absent() -> None:
    result = CliRunner().invoke(app, ["bridge-reviewed-pending-entries"])

    assert result.exit_code == 2
    assert "No such command 'bridge-reviewed-pending-entries'" in result.output


def test_finalize_cancelled_entries_wires_exact_stopped_runtime_guard(
    tmp_path,
    monkeypatch,
) -> None:
    import telegram_kol_research.cli as cli_module

    events = []
    runtime = object()
    monkeypatch.setattr(
        cli_module,
        "create_existing_session_factory",
        lambda path: object(),
    )
    monkeypatch.setattr(
        cli_module,
        "build_deepcoin_client_from_env",
        lambda: object(),
    )
    monkeypatch.setattr(
        cli_module,
        "SystemRuntimeAdapter",
        lambda *, expected_uid: runtime,
    )
    monkeypatch.setattr(
        cli_module,
        "require_stopped_legacy_runtime_boundary",
        lambda value: events.append(value),
    )

    @contextmanager
    def runtime_lock(*, expected_uid):
        assert expected_uid == 0
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    monkeypatch.setattr(cli_module, "exclusive_runtime_control_lock", runtime_lock)

    def build_plan(*args, runtime_guard, **kwargs):
        runtime_guard()
        return SimpleNamespace(
            status="blocked",
            reason_code="pending_trigger_present",
            target_order_ids=("one",),
            evidence_sha256="a" * 64,
            fingerprint="",
        )

    monkeypatch.setattr(
        cli_module,
        "build_manual_pending_entry_reconciliation_plan",
        build_plan,
    )

    result = CliRunner().invoke(
        app,
        [
            "finalize-cancelled-pending-entries",
            "--database-path",
            str(tmp_path / "research.db"),
            "--backup-path",
            str(tmp_path / "backup.db"),
        ],
    )

    assert result.exit_code == 2
    assert events == ["lock-enter", runtime, "lock-exit"]


def test_finalize_cancelled_entries_holds_runtime_lock_through_apply(
    tmp_path,
    monkeypatch,
) -> None:
    import telegram_kol_research.cli as cli_module

    events = []
    runtime = object()
    monkeypatch.setattr(cli_module, "create_existing_session_factory", lambda path: object())
    monkeypatch.setattr(cli_module, "build_deepcoin_client_from_env", lambda: object())
    monkeypatch.setattr(cli_module, "SystemRuntimeAdapter", lambda *, expected_uid: runtime)
    monkeypatch.setattr(
        cli_module,
        "require_stopped_legacy_runtime_boundary",
        lambda value: events.append("guard"),
    )

    @contextmanager
    def runtime_lock(*, expected_uid):
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    def build_plan(*args, runtime_guard, **kwargs):
        assert events[-1] == "lock-enter"
        runtime_guard()
        return SimpleNamespace(
            status="ready",
            reason_code=None,
            target_order_ids=("one",),
            evidence_sha256="a" * 64,
            fingerprint="b" * 64,
        )

    def apply_plan(*args, runtime_guard, **kwargs):
        assert events[-1] == "guard"
        events.append("apply")
        runtime_guard()
        return SimpleNamespace(
            status="completed",
            terminalized_count=1,
            authority_seeded=True,
            backup_path=tmp_path / "backup.db",
            backup_sha256="c" * 64,
        )

    monkeypatch.setattr(cli_module, "exclusive_runtime_control_lock", runtime_lock)
    monkeypatch.setattr(cli_module, "build_manual_pending_entry_reconciliation_plan", build_plan)
    monkeypatch.setattr(cli_module, "apply_manual_pending_entry_reconciliation", apply_plan)

    result = CliRunner().invoke(
        app,
        [
            "finalize-cancelled-pending-entries",
            "--database-path",
            str(tmp_path / "research.db"),
            "--backup-path",
            str(tmp_path / "backup.db"),
            "--apply",
            "--expected-fingerprint",
            "b" * 64,
        ],
    )

    assert result.exit_code == 0
    assert events == ["lock-enter", "guard", "apply", "guard", "lock-exit"]


def test_entry_draft_revision_cli_is_dry_run_by_default(tmp_path):
    draft_path = tmp_path / "chen-draft.json"
    draft_path.write_text(json.dumps({
        "venue": "deepcoin",
        "strategy_instance_id": "deepcoin:chen:BTC:long",
        "instrument_id": "BTC-USDT-SWAP",
        "symbol": "BTC",
        "position_side": "long",
        "stop_loss": 63000,
        "take_profit_legs": [{"price": 66000, "allocation_pct": 100}],
        "risk_budget_usdt": 20,
        "execution_deadline_at": "2099-08-10T12:00:00+00:00",
        "order_legs": [
            {"order_type": "limit", "price": 64000, "client_order_id": "E1",
             "allocation_pct": 50, "risk_budget_usdt": 10, "quantity": 10,
             "side": "buy", "position_side": "long"},
            {"order_type": "limit", "price": 63800, "client_order_id": "E2",
             "allocation_pct": 50, "risk_budget_usdt": 10, "quantity": 12,
             "side": "buy", "position_side": "long"},
        ],
    }), encoding="utf-8")

    result = CliRunner().invoke(app, [
        "entry-draft-revision",
        "--draft-path", str(draft_path),
        "--market-price", "63950",
        "--leg-index", "1",
    ])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "dry_run"
    assert payload["authorized_leg_indices"] == [1]
    assert len(payload["leg_mappings"]) == 2
    assert payload["leg_mappings"][1]["original_client_order_id"] == "E2"


def _seed_entry_assembly_fingerprint_cli_case(tmp_path):
    database_path = tmp_path / "entry-repair.db"
    session_factory = create_session_factory(database_path)
    secret_text = "PRIVATE TELEGRAM MESSAGE MUST NOT APPEAR"
    final_evidence = {
        "chat_id": -1001,
        "strategy_raw_message_id": 10,
        "strategy_message_id": 55,
        "signal_candidate_id": 20,
        "symbol": "BTC",
        "side": "long",
        "fragment_ids": [30],
        "legacy_preamble_ids": [],
        "risk_multiplier": "0.5",
        "allocations": ["1"],
        "supplemental_prices": [],
        "cutoff": ["2026-08-08T11:59:00+00:00", 55, 10],
        "planned_entry_leg_count": 1,
        "order_draft_snapshot": {
            "strategy_instance_id": _ENTRY_REPAIR_STRATEGY_ID,
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
                "kol_code": "group-a",
                "chat_id": -1001,
                "message_id": 55,
            },
            "selected_entry_leg_indices": [1],
            "selected_entry_leg_count": 1,
            "order_legs": [
                {
                    "price": 64000,
                    "order_type": "limit",
                    "allocation_pct": 100,
                    "risk_budget_usdt": 10,
                    "quantity": 10,
                    "quantity_unit": "contracts",
                    "estimated_stop_loss_usdt": 10,
                    "client_order_id": "entry-1",
                    "base_asset_estimate": 0.01,
                    "side": "buy",
                    "position_side": "long",
                    "take_profit_leg": {"price": 66000, "allocation_pct": 100},
                }
            ],
        },
        "final_entry_leg_count": 1,
    }
    final_fingerprint = canonical_fingerprint(final_evidence)
    old_fingerprint = derive_pre_finalization_fingerprint(final_evidence)
    stale_evidence = {
        "assembly_id": 2,
        "strategy_instance_id": _ENTRY_REPAIR_STRATEGY_ID,
        "assembly_fingerprint": old_fingerprint,
    }
    draft = deepcopy(final_evidence["order_draft_snapshot"])
    draft["entry_preamble_assembly"] = deepcopy(stale_evidence)
    signal_payload = {
        "entry_preamble_assembly": deepcopy(stale_evidence),
        "deepcoin_order_draft": deepcopy(draft),
        "private_message": secret_text,
    }
    with session_factory() as session:
        session.add(RawMessage(
            id=10, chat_id=-1001, message_id=55, text=secret_text,
            archived_target_group=True, created_at=_ENTRY_REPAIR_NOW,
        ))
        session.add(SignalCandidate(
            id=20, raw_message_id=10, symbol="BTC", side="long",
            event_type="entry_signal", parse_source="mimo_authoritative",
            confidence=0.95, review_status="pending", created_at=_ENTRY_REPAIR_NOW,
        ))
        session.add(EntryStrategyAssembly(
            id=2, entry_preamble_id=None, strategy_raw_message_id=10,
            signal_candidate_id=20, strategy_instance_id=_ENTRY_REPAIR_STRATEGY_ID,
            risk_multiplier="0.5", evidence_json=_canonical_json(final_evidence),
            fingerprint=final_fingerprint, created_at=_ENTRY_REPAIR_NOW,
        ))
        session.add(TradeSignal(
            id=398, signal_uid="repair-signal",
            strategy_instance_id=_ENTRY_REPAIR_STRATEGY_ID,
            source_type="recovery", venue="deepcoin", kol_id="group:-1001",
            chat_id=-1001, message_id=55, symbol="BTC", side="long",
            action="open_position", status="submitted",
            payload_json=_canonical_json(signal_payload), processed_at=_ENTRY_REPAIR_NOW,
            created_at=_ENTRY_REPAIR_NOW, updated_at=_ENTRY_REPAIR_NOW,
        ))
        session.add(ExecutionBinding(
            id=266, strategy_instance_id=_ENTRY_REPAIR_STRATEGY_ID,
            kol_id="group:-1001", chat_id=-1001, message_id=55,
            symbol="BTC", side="long", venue="deepcoin",
            margin_mode="cross", position_mode="split",
            payload_json=_canonical_json({"draft": draft, "private": secret_text}),
            status="open", created_at=_ENTRY_REPAIR_NOW, updated_at=_ENTRY_REPAIR_NOW,
        ))
        leg = final_evidence["order_draft_snapshot"]["order_legs"][0]
        session.add(ExecutionOrderLeg(
            execution_binding_id=266, strategy_instance_id=_ENTRY_REPAIR_STRATEGY_ID,
            leg_index=1, purpose="entry", order_kind="trigger_limit",
            order_id="order-1", client_order_id=leg["client_order_id"],
            venue="deepcoin", status="open",
            request_json=_canonical_json(
                build_deepcoin_trigger_order_payload(draft, leg)
            ),
            created_at=_ENTRY_REPAIR_NOW, updated_at=_ENTRY_REPAIR_NOW,
        ))
        session.commit()
    return database_path, session_factory, secret_text


def _entry_repair_args(database_path, *extra):
    return [
        "repair-entry-assembly-fingerprint",
        "--database-path", str(database_path),
        "--assembly-id", "2",
        "--execution-binding-id", "266",
        *extra,
    ]


def test_entry_assembly_fingerprint_repair_help_exposes_bounded_inputs():
    root = CliRunner().invoke(app, ["--help"])
    command = CliRunner().invoke(
        app, ["repair-entry-assembly-fingerprint", "--help"]
    )

    assert root.exit_code == 0
    assert "repair-entry-assembly-fingerprint" in root.output
    assert command.exit_code == 0
    for option in (
        "--database-path", "--assembly-id", "--execution-binding-id",
        "--apply", "--expected-plan-fingerprint",
    ):
        assert option in command.output


def test_entry_assembly_fingerprint_repair_dry_run_is_redacted_and_read_only(
    tmp_path, monkeypatch
):
    database_path, session_factory, secret_text = (
        _seed_entry_assembly_fingerprint_cli_case(tmp_path)
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        draft = json.loads(binding.payload_json)["draft"]
        request = json.loads(
            session.query(ExecutionOrderLeg).filter_by(
                execution_binding_id=266, leg_index=1
            ).one().request_json
        )
    assert request == build_deepcoin_trigger_order_payload(
        draft, draft["order_legs"][0]
    )
    before = database_path.read_bytes()
    import telegram_kol_research.cli as cli_module
    monkeypatch.setattr(
        cli_module, "build_deepcoin_client_from_env",
        lambda: pytest.fail("Deepcoin client must not be constructed"),
    )
    monkeypatch.setattr(
        cli_module, "create_telegram_client",
        lambda *args, **kwargs: pytest.fail("Telegram client must not be constructed"),
    )

    result = CliRunner().invoke(app, _entry_repair_args(database_path))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "dry_run"
    assert payload["plan"]["conflicts"] == []
    assert payload["plan"]["action"]["assembly_id"] == 2
    assert len(payload["plan"]["fingerprint"]) == 64
    assert secret_text not in result.output
    assert "private_message" not in result.output
    assert _ENTRY_REPAIR_STRATEGY_ID not in result.output
    with session_factory() as session:
        assert session.query(ExecutionEvent).count() == 0
    assert database_path.read_bytes() == before


def test_entry_assembly_fingerprint_repair_apply_requires_reviewed_fingerprint(
    tmp_path,
):
    database_path, session_factory, _ = _seed_entry_assembly_fingerprint_cli_case(tmp_path)

    result = CliRunner().invoke(
        app, _entry_repair_args(database_path, "--apply")
    )

    assert result.exit_code == 2
    assert "--expected-plan-fingerprint" in result.output + result.stderr
    with session_factory() as session:
        assert session.query(ExecutionEvent).count() == 0


def test_entry_assembly_fingerprint_repair_conflicting_dry_run_fails_redacted(
    tmp_path,
):
    database_path, session_factory, secret_text = (
        _seed_entry_assembly_fingerprint_cli_case(tmp_path)
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, 266)
        binding.strategy_instance_id = "conflicting-private-strategy"
        session.commit()
    before = database_path.read_bytes()

    result = CliRunner().invoke(app, _entry_repair_args(database_path))

    assert result.exit_code == 2
    payload = json.loads(result.output.splitlines()[0])
    assert payload["mode"] == "dry_run"
    assert payload["plan"]["action"] is None
    assert "binding_strategy_mismatch" in payload["plan"]["conflicts"]
    assert secret_text not in result.output
    assert "conflicting-private-strategy" not in result.output
    with session_factory() as session:
        assert session.query(ExecutionEvent).count() == 0
    assert database_path.read_bytes() == before


def test_entry_assembly_fingerprint_repair_apply_rejects_changed_fingerprint(tmp_path):
    database_path, session_factory, _ = _seed_entry_assembly_fingerprint_cli_case(tmp_path)

    result = CliRunner().invoke(
        app,
        _entry_repair_args(
            database_path,
            "--apply",
            "--expected-plan-fingerprint", "0" * 64,
        ),
    )

    assert result.exit_code == 2
    assert "repair_plan_fingerprint_mismatch" in result.output + result.stderr
    with session_factory() as session:
        assert session.query(ExecutionEvent).count() == 0


def test_entry_assembly_fingerprint_repair_exact_apply_appends_one_event(
    tmp_path, monkeypatch
):
    database_path, session_factory, secret_text = (
        _seed_entry_assembly_fingerprint_cli_case(tmp_path)
    )
    import telegram_kol_research.cli as cli_module
    monkeypatch.setattr(
        cli_module, "build_deepcoin_client_from_env",
        lambda: pytest.fail("Deepcoin client must not be constructed"),
    )
    monkeypatch.setattr(
        cli_module, "create_telegram_client",
        lambda *args, **kwargs: pytest.fail("Telegram client must not be constructed"),
    )
    dry_run = CliRunner().invoke(app, _entry_repair_args(database_path))
    fingerprint = json.loads(dry_run.output)["plan"]["fingerprint"]

    applied = CliRunner().invoke(
        app,
        _entry_repair_args(
            database_path,
            "--apply",
            "--expected-plan-fingerprint", fingerprint,
        ),
    )

    assert applied.exit_code == 0, applied.output
    outputs = [json.loads(line) for line in applied.output.splitlines()]
    assert outputs[0]["mode"] == "apply_plan"
    assert outputs[0]["plan"]["fingerprint"] == fingerprint
    assert outputs[1]["mode"] == "apply"
    assert isinstance(outputs[1]["event_id"], int)
    assert secret_text not in applied.output
    assert "private_message" not in applied.output
    with session_factory() as session:
        assert session.query(ExecutionEvent).count() == 1
        assert session.get(ExecutionEvent, outputs[1]["event_id"]) is not None


def test_reviewed_legacy_conditional_cancel_help_exposes_fail_closed_inputs():
    result = CliRunner().invoke(
        app,
        ["cancel-reviewed-legacy-conditionals", "--help"],
    )

    assert result.exit_code == 0
    assert "--expected-fingerprint" in result.output
    assert "--confirmation-token" in result.output
    assert "--action-id" in result.output
    assert "--pos-id" in result.output


def test_cli_help_renders():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "sync" in result.stdout
    assert "report" in result.stdout
    assert "recovery-dry-run" in result.stdout
    assert "repair-position-attribution" in result.stdout
    assert "repair-entry-protection-ledger" in result.stdout
    assert "repair-take-profit-protection-leg" in result.stdout
    assert "repair-position-management" in result.stdout
    assert "recover-position-management-liveness" in result.stdout
    assert "audit-management-batches" in result.stdout
    assert "archive-unbound-holdings" in result.stdout
    assert "monitor-production-safety" in result.stdout
    assert "audit-tpsl-ownership" in result.stdout
    assert "audit-kol-pnl" in result.stdout
    assert "backfill-canonical-tpsl-ledger" in result.stdout


def test_take_profit_protection_leg_repair_help_exposes_review_gates():
    result = CliRunner().invoke(
        app, ["repair-take-profit-protection-leg", "--help"]
    )

    assert result.exit_code == 0
    assert "--database-path" in result.output
    assert "--apply" in result.output
    assert "--action-id" in result.output
    assert "--expected-fingerprint" in result.output
    assert "--confirmation-token" in result.output


def test_recover_position_management_liveness_help_requires_exact_review_inputs():
    result = CliRunner().invoke(
        app, ["recover-position-management-liveness", "--help"]
    )

    assert result.exit_code == 0
    assert "--database-path" in result.output
    assert "--pos-id" in result.output
    assert "--apply" in result.output
    assert "--expected-fingerprint" in result.output


def test_recover_position_management_liveness_apply_requires_fingerprint(tmp_path):
    database_path = tmp_path / "recovery.db"
    create_session_factory(database_path)

    result = CliRunner().invoke(
        app,
        [
            "recover-position-management-liveness",
            "--database-path", str(database_path),
            "--pos-id", "pos-1",
            "--apply",
        ],
    )

    assert result.exit_code == 2
    assert "--expected-fingerprint" in result.output


def test_repair_entry_protection_ledger_apply_requires_bounded_trigger_identity(
    tmp_path,
):
    result = CliRunner().invoke(
        app,
        [
            "repair-entry-protection-ledger",
            "--database-path",
            str(tmp_path / "research.db"),
            "--apply",
        ],
    )

    output = result.stdout + result.stderr
    assert result.exit_code == 2
    assert "--include-trigger-entries" in output
    assert "--binding-id" in output
    assert "--pos-id" in output
    assert "--action-id" in output
    assert "--expected-fingerprint" in output
    assert "--confirmation-token" in output
    assert not (tmp_path / "research.db").exists()


def test_repair_position_management_apply_requires_exact_action_and_fingerprint(
    tmp_path,
):
    result = CliRunner().invoke(
        app,
        [
            "repair-position-management",
            "--database-path",
            str(tmp_path / "research.db"),
            "--apply",
        ],
    )

    assert result.exit_code == 2
    assert "--action-id" in result.stdout + result.stderr
    assert "--expected-fingerprint" in result.stdout + result.stderr


def test_repair_position_management_dry_run_never_creates_database(tmp_path):
    database_path = tmp_path / "missing.db"

    result = CliRunner().invoke(
        app,
        [
            "repair-position-management",
            "--database-path",
            str(database_path),
        ],
    )

    assert result.exit_code == 2
    assert "no file was created" in result.stdout + result.stderr
    assert not database_path.exists()


def test_archive_unbound_holdings_dry_run_then_apply(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 18, 1, 0),
            entered_at=datetime(2026, 7, 18, 1, 5),
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    dry_run = CliRunner().invoke(
        app,
        [
            "archive-unbound-holdings",
            "--database-path",
            str(database_path),
            "--lifecycle-id",
            str(lifecycle_id),
        ],
    )

    assert dry_run.exit_code == 0, dry_run.stdout
    dry_run_payload = json.loads(dry_run.stdout)
    assert dry_run_payload["mode"] == "dry_run"
    assert dry_run_payload["applied"] == 0
    assert dry_run_payload["rows"][0]["status"] == "would_archive"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "entered"

    applied = CliRunner().invoke(
        app,
        [
            "archive-unbound-holdings",
            "--database-path",
            str(database_path),
            "--lifecycle-id",
            str(lifecycle_id),
            "--expected-count",
            "1",
            "--apply",
        ],
    )

    assert applied.exit_code == 0, applied.stdout
    payload = json.loads(applied.stdout)
    assert payload["mode"] == "apply"
    assert payload["applied"] == 1
    assert payload["rows"][0]["status"] == "archived"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "invalidated"
        assert lifecycle.exit_reason == "context_invalidated"
        assert lifecycle.management_action == "operator_archived_unbound_holding"


def test_archive_unbound_holdings_refuses_deepcoin_binding(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 18, 1, 0),
            entered_at=datetime(2026, 7, 18, 1, 5),
        )
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="active",
        )
        session.add_all([lifecycle, binding])
        session.commit()
        lifecycle_id = lifecycle.id

    result = CliRunner().invoke(
        app,
        [
            "archive-unbound-holdings",
            "--database-path",
            str(database_path),
            "--lifecycle-id",
            str(lifecycle_id),
            "--expected-count",
            "1",
            "--apply",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["applied"] == 0
    assert payload["rows"][0]["status"] == "refused"
    assert payload["rows"][0]["reasons"] == ["matching_deepcoin_binding_exists"]
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "entered"


def test_archive_unbound_holdings_apply_requires_expected_count(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 18, 1, 0),
            entered_at=datetime(2026, 7, 18, 1, 5),
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    result = CliRunner().invoke(
        app,
        [
            "archive-unbound-holdings",
            "--database-path",
            str(database_path),
            "--lifecycle-id",
            str(lifecycle_id),
            "--apply",
        ],
    )

    assert result.exit_code == 2
    assert "--expected-count is required" in result.stderr
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        assert lifecycle.lifecycle_status == "entered"


def test_monitor_production_safety_help_has_required_flags():
    result = CliRunner().invoke(
        app,
        ["monitor-production-safety", "--help"],
        env={"COLUMNS": "240"},
    )

    assert result.exit_code == 0, result.stdout
    for flag in (
        "--expected-auto-trade-enabled",
        "--expected-management-mode",
        "--expected-entry-preamble-mode",
        "--expected-max-concurrent-positions",
        "--notify",
        "--force-full-audit",
        "--test-notification",
    ):
        assert flag in result.stdout
    for removed_flag in (
        "--expected-head",
        "--expected-release-commit",
        "--expected-release-manifest-sha256",
        "--release-path",
    ):
        assert removed_flag not in result.stdout


def test_monitor_production_test_notification_requires_notify():
    result = CliRunner().invoke(
        app,
        [
            "monitor-production-safety",
            "--expected-auto-trade-enabled",
            "--expected-management-mode",
            "live",
            "--expected-entry-preamble-mode",
            "live",
            "--expected-max-concurrent-positions",
            "4",
            "--test-notification",
        ],
    )

    assert result.exit_code != 0
    assert "--notify" in result.stderr


def test_monitor_production_test_notification_uses_fixed_text_only(monkeypatch):
    import telegram_kol_research.cli as cli_module

    calls = []
    monkeypatch.setattr(
        cli_module,
        "send_monitor_test_notification",
        lambda: calls.append("test") or "sent",
    )
    monkeypatch.setattr(
        cli_module,
        "run_production_safety_monitor",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("monitor adapters called")),
    )

    result = CliRunner().invoke(
        app,
        [
            "monitor-production-safety",
            "--expected-auto-trade-enabled",
            "--expected-management-mode",
            "live",
            "--expected-entry-preamble-mode",
            "live",
            "--expected-max-concurrent-positions",
            "4",
            "--notify",
            "--test-notification",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert calls == ["test"]
    assert json.loads(result.stdout) == {
        "healthy": True,
        "mode": "test_notification",
        "notification_status": "sent",
    }


def test_monitor_production_prints_compact_fixed_summary_and_exits_nonzero(monkeypatch):
    import telegram_kol_research.cli as cli_module

    existing_factory = object()
    requested_paths = []
    monkeypatch.setattr(
        cli_module,
        "create_existing_session_factory",
        lambda path: requested_paths.append(path) or existing_factory,
    )
    observed_kwargs = {}
    monkeypatch.setattr(
        cli_module,
        "run_production_safety_monitor",
        lambda **kwargs: observed_kwargs.update(kwargs) or SimpleNamespace(
            audit_ran=False,
            exit_code=1,
            monitor_error="notification_delivery_failed",
            notification_status="delivery_failed",
            result=SimpleNamespace(
                healthy=False,
                reason_codes=("service_inactive",),
            ),
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "monitor-production-safety",
            "--expected-auto-trade-enabled",
            "--expected-management-mode",
            "live",
            "--expected-entry-preamble-mode",
            "live",
            "--expected-max-concurrent-positions",
            "4",
            "--database-path",
            "/tmp/monitor-read-only.db",
        ],
    )

    assert result.exit_code == 1
    assert requested_paths == [Path("/tmp/monitor-read-only.db")]
    assert observed_kwargs["runtime_incident_session_factory"] is existing_factory
    assert json.loads(result.stdout) == {
        "audit_ran": False,
        "healthy": False,
        "monitor_error": "notification_delivery_failed",
        "notification_status": "delivery_failed",
        "reason_codes": ["service_inactive"],
    }


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

    before = database_path.read_bytes(), database_path.stat()
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
    assert payload["actionable_batches"] == {
        "total": 2,
        "returned": 2,
        "truncated": False,
        "items": [
            {
                "batch_ref": "batch:982134701",
                "states": ["submit_unknown", "recovery_required"],
            },
            {"batch_ref": "batch:982134702", "states": ["blocked"]},
        ],
    }
    assert "pos-secret" not in result.stdout
    assert "strategy-secret" not in result.stdout
    assert "kol-secret" not in result.stdout
    for identity in (
        "-1001234567890",
        "398475612",
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
    assert database_path.read_bytes() == before[0]
    assert database_path.stat() == before[1]


def test_audit_protection_incidents_cli_is_read_only_and_has_no_apply(
    tmp_path, monkeypatch
):
    import telegram_kol_research.cli as cli_module

    database_path = tmp_path / "protection.db"
    create_session_factory(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    monkeypatch.setattr(
        cli_module,
        "build_deepcoin_client_from_env",
        lambda: SimpleNamespace(
            list_positions=lambda: [],
            list_open_orders=lambda: [],
            list_trigger_orders_pending=lambda **_kwargs: [],
            list_order_history=lambda **_kwargs: [],
            list_trade_fills=lambda **_kwargs: [],
            list_trigger_order_history=lambda **_kwargs: [],
        ),
    )

    help_result = CliRunner().invoke(app, ["audit-protection-incidents", "--help"])
    assert help_result.exit_code == 0
    assert "--apply" not in help_result.stdout

    before = database_path.read_bytes(), database_path.stat()
    result = CliRunner().invoke(
        app,
        [
            "audit-protection-incidents",
            "--database-path",
            str(database_path),
            "--limit",
            "100",
            "--output-format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["counts"] == {
        "current_risk": 0,
        "evidence_insufficient": 0,
        "historical_terminal": 0,
        "resolved_by_current_exchange_evidence": 0,
    }
    assert payload["output_complete"] is True
    after = database_path.read_bytes(), database_path.stat()
    assert after == before


def test_audit_management_batches_classifies_informational_hold_as_non_alerting(
    tmp_path,
):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO raw_messages "
            "(id, chat_id, message_id, archived_target_group, created_at) "
            "VALUES (101, 100, 201, 1, '2026-07-17 00:00:00')"
        )
        connection.execute(
            "INSERT INTO strategy_management_batches "
            "(id, idempotency_fingerprint, raw_message_id, recognition_decision_id, "
            "recognition_generation, target_lifecycle_id, strategy_instance_id, "
            "execution_binding_id, intent, effective_action, execution_mode, "
            "partial_round_before, status, reason_code, target_fingerprint, "
            "target_snapshot_json, planned_at, created_at, updated_at) "
            "VALUES (301, 'hold-fingerprint', 101, 401, 'hold-generation', 501, "
            "'hold-strategy', 601, 'hold_update', 'hold_update', 'live', 0, "
            "'blocked', 'management_intent_not_supported', 'hold-target', '{}', "
            "'2026-07-17 00:00:00', '2026-07-17 00:00:00', '2026-07-17 00:00:00')"
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
    counts = json.loads(result.stdout)["counts"]
    assert counts["batches_total"] == 1
    assert counts["informational_noop"] == 1
    assert counts["blocked"] == 0

    text_result = CliRunner().invoke(
        app,
        [
            "audit-management-batches",
            "--database-path",
            str(database_path),
            "--output-format",
            "text",
        ],
    )
    assert text_result.exit_code == 0, text_result.stdout
    assert "informational_noop=1" in text_result.stdout


@pytest.mark.parametrize(
    ("intent", "reason_code", "add_leg"),
    [
        ("risk_update", "management_intent_not_supported", False),
        ("hold_update", "target_position_not_verified", False),
        ("hold_update", None, False),
        ("hold_update", "management_intent_not_supported", True),
    ],
)
def test_audit_management_batches_keeps_near_match_holds_alerting(
    tmp_path, intent, reason_code, add_leg
):
    import telegram_kol_research.production_safety_monitor as monitor_module

    database_path = tmp_path / "research.db"
    create_session_factory(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO raw_messages "
            "(id, chat_id, message_id, archived_target_group, created_at) "
            "VALUES (102, 100, 202, 1, '2026-07-17 00:00:00')"
        )
        connection.execute(
            "INSERT INTO strategy_management_batches "
            "(id, idempotency_fingerprint, raw_message_id, recognition_decision_id, "
            "recognition_generation, target_lifecycle_id, strategy_instance_id, "
            "execution_binding_id, intent, effective_action, execution_mode, "
            "partial_round_before, status, reason_code, target_fingerprint, "
            "target_snapshot_json, planned_at, created_at, updated_at) "
            "VALUES (302, 'near-fingerprint', 102, 402, 'near-generation', 502, "
            "'near-strategy', 602, ?, ?, 'live', 0, 'blocked', ?, "
            "'near-target', '{}', '2026-07-17 00:00:00', "
            "'2026-07-17 00:00:00', '2026-07-17 00:00:00')",
            (intent, intent, reason_code),
        )
        if add_leg:
            connection.execute(
                "INSERT INTO strategy_management_legs "
                "(id, management_batch_id, execution_order_leg_id, pos_id, leg_index, "
                "status, created_at, updated_at) "
                "VALUES (702, 302, 802, 'pos-near', 1, 'planned', "
                "'2026-07-17 00:00:00', '2026-07-17 00:00:00')"
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
    audit = json.loads(result.stdout)
    assert audit["counts"]["informational_noop"] == 0
    assert audit["counts"]["blocked"] == 1
    reasons = set()
    details = {}
    monitor_module._evaluate_audit(audit, reasons, details)
    assert "audit_abnormal" in reasons
    assert details["audit_abnormal_count"] == 1


@pytest.mark.parametrize("leg_status", [None, "planned", "failed"])
def test_audit_management_batches_classifies_completed_safe_block_as_history(
    tmp_path, leg_status
):
    database_path = tmp_path / "terminal-blocked.db"
    create_session_factory(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO strategy_management_batches "
            "(id, idempotency_fingerprint, raw_message_id, recognition_decision_id, "
            "recognition_generation, target_lifecycle_id, strategy_instance_id, "
            "execution_binding_id, intent, effective_action, execution_mode, "
            "partial_round_before, status, reason_code, target_fingerprint, "
            "target_snapshot_json, planned_at, completed_at, created_at, updated_at) "
            "VALUES (303, 'terminal-fingerprint', 103, 403, 'terminal-generation', "
            "503, 'terminal-strategy', 603, 'full_exit', 'full_exit', 'live', 0, "
            "'blocked', 'safe_preflight_refusal', 'terminal-target', '{}', "
            "'2026-07-17 00:00:00', '2026-07-17 00:00:01', "
            "'2026-07-17 00:00:00', '2026-07-17 00:00:01')"
        )
        if leg_status is not None:
            connection.execute(
                "INSERT INTO strategy_management_legs "
                "(id, management_batch_id, execution_order_leg_id, pos_id, leg_index, "
                "status, created_at, updated_at) "
                "VALUES (703, 303, 803, 'pos-terminal', 1, ?, "
                "'2026-07-17 00:00:00', '2026-07-17 00:00:01')",
                (leg_status,),
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
    counts = payload["counts"]
    assert counts["terminal_blocked"] == 1
    assert counts["blocked"] == 0
    assert payload["actionable_batches"] == {
        "total": 0,
        "returned": 0,
        "truncated": False,
        "items": [],
    }


@pytest.mark.parametrize(
    "leg_status",
    [
        "reserved",
        "submitted",
        "submit_unknown",
        "partial",
        "inconsistent",
        "partial_failed",
        "recovery_required",
    ],
)
def test_audit_management_batches_keeps_completed_block_with_actionable_leg(
    tmp_path, leg_status
):
    database_path = tmp_path / "actionable-blocked.db"
    create_session_factory(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO strategy_management_batches "
            "(id, idempotency_fingerprint, raw_message_id, recognition_decision_id, "
            "recognition_generation, target_lifecycle_id, strategy_instance_id, "
            "execution_binding_id, intent, effective_action, execution_mode, "
            "partial_round_before, status, reason_code, target_fingerprint, "
            "target_snapshot_json, planned_at, completed_at, created_at, updated_at) "
            "VALUES (304, 'actionable-fingerprint', 104, 404, 'actionable-generation', "
            "504, 'actionable-strategy', 604, 'full_exit', 'full_exit', 'live', 0, "
            "'blocked', 'blocked_with_actionable_leg', 'actionable-target', '{}', "
            "'2026-07-17 00:00:00', '2026-07-17 00:00:01', "
            "'2026-07-17 00:00:00', '2026-07-17 00:00:01')"
        )
        connection.execute(
            "INSERT INTO strategy_management_legs "
            "(id, management_batch_id, execution_order_leg_id, pos_id, leg_index, "
            "status, created_at, updated_at) "
            "VALUES (704, 304, 804, 'pos-actionable', 1, ?, "
            "'2026-07-17 00:00:00', '2026-07-17 00:00:01')",
            (leg_status,),
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
    counts = payload["counts"]
    assert counts["terminal_blocked"] == 0
    assert counts["blocked"] == 1
    expected_states = ["blocked"]
    if leg_status in {"submit_unknown", "partial_failed", "recovery_required"}:
        expected_states.append(leg_status)
    assert payload["actionable_batches"] == {
        "total": 1,
        "returned": 1,
        "truncated": False,
        "items": [{"batch_ref": "batch:304", "states": expected_states}],
    }


def test_audit_management_batches_bounds_actionable_batch_references(tmp_path):
    database_path = tmp_path / "bounded-actionable.db"
    create_session_factory(database_path)
    with sqlite3.connect(database_path) as connection:
        for batch_id in range(1, 13):
            connection.execute(
                "INSERT INTO strategy_management_batches "
                "(id, idempotency_fingerprint, raw_message_id, recognition_decision_id, "
                "recognition_generation, target_lifecycle_id, strategy_instance_id, "
                "execution_binding_id, intent, effective_action, execution_mode, "
                "partial_round_before, status, reason_code, target_fingerprint, "
                "target_snapshot_json, planned_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'full_exit', 'full_exit', 'live', "
                "0, 'blocked', 'manual_review_required', ?, '{}', "
                "'2026-07-17 00:00:00', '2026-07-17 00:00:00', "
                "'2026-07-17 00:00:00')",
                (
                    batch_id,
                    f"bounded-fingerprint-{batch_id}",
                    100 + batch_id,
                    200 + batch_id,
                    f"bounded-generation-{batch_id}",
                    300 + batch_id,
                    f"bounded-strategy-{batch_id}",
                    400 + batch_id,
                    f"bounded-target-{batch_id}",
                ),
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
    actionable = json.loads(result.stdout)["actionable_batches"]
    assert actionable == {
        "total": 12,
        "returned": 10,
        "truncated": True,
        "items": [
            {"batch_ref": f"batch:{batch_id}", "states": ["blocked"]}
            for batch_id in range(1, 11)
        ],
    }
    assert "manual_review_required" not in result.stdout
    assert "bounded-fingerprint" not in result.stdout


def _audit_payload_with_management_history(
    tmp_path,
    *,
    oldest_status: str = "succeeded",
    oldest_target_snapshot_json: str = "{}",
    oldest_leg_count: int = 0,
) -> dict:
    database_path = tmp_path / "management-history.db"
    create_session_factory(database_path)
    with sqlite3.connect(database_path) as connection:
        for index in range(21):
            status = oldest_status if index == 0 else "succeeded"
            connection.execute(
                "INSERT INTO raw_messages "
                "(id, chat_id, message_id, archived_target_group, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (
                    2000 + index,
                    -1000000 - index,
                    7000 + index,
                    f"2026-07-{index + 1:02d} 10:00:00",
                ),
            )
            connection.execute(
                "INSERT INTO strategy_management_batches "
                "(id, idempotency_fingerprint, raw_message_id, recognition_decision_id, "
                "recognition_generation, target_lifecycle_id, strategy_instance_id, "
                "execution_binding_id, intent, effective_action, execution_mode, "
                "partial_round_before, status, target_fingerprint, target_snapshot_json, "
                "planned_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'partial_take_profit', "
                "'partial_close', 'shadow', 0, ?, ?, ?, ?, ?, ?)",
                (
                    1000 + index,
                    f"fingerprint-{index}",
                    2000 + index,
                    3000 + index,
                    f"generation-{index}",
                    4000 + index,
                    5000 + index,
                    6000 + index,
                    status,
                    f"target-{index}",
                    oldest_target_snapshot_json if index == 0 else "{}",
                    f"2026-07-{index + 1:02d} 10:00:00",
                    f"2026-07-{index + 1:02d} 10:00:00",
                    f"2026-07-{index + 1:02d} 10:00:00",
                ),
            )
        for leg_index in range(oldest_leg_count):
            connection.execute(
                "INSERT INTO strategy_management_legs "
                "(id, management_batch_id, execution_order_leg_id, pos_id, leg_index, "
                "status, preflight_size, planned_close_size, last_error, created_at, updated_at) "
                "VALUES (?, 1000, ?, ?, ?, 'succeeded', '1', '1', NULL, "
                "'2026-07-01 10:00:00', '2026-07-01 10:00:00')",
                (
                    8000 + leg_index,
                    9000 + leg_index,
                    f"pos-{leg_index}",
                    leg_index + 1,
                ),
            )
        connection.commit()

    result = CliRunner().invoke(
        app,
        [
            "audit-management-batches",
            "--database-path",
            str(database_path),
            "--limit",
            "20",
            "--output-format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


def _evaluate_real_audit_payload(payload: dict):
    return evaluate_monitor_snapshot(
        MonitorSnapshot(
            service_state="active",
            settings={
                "auto_trade_enabled": False,
                "management_execution_mode": "disabled",
                "max_concurrent_positions": 4,
                "entry_preamble_mode": "disabled",
            },
            journal_error_count=0,
            abnormal_events=(),
            audit=payload,
        ),
        MonitorExpectations(
            auto_trade_enabled=False,
            management_execution_mode="disabled",
            max_concurrent_positions=4,
            entry_preamble_mode="disabled",
        ),
    )


def test_monitor_accepts_benign_truncation_of_normal_batch_display_rows(tmp_path):
    payload = _audit_payload_with_management_history(tmp_path)

    assert payload["counts"]["batches_total"] == 21
    assert payload["batches_returned"] == 20
    assert payload["batches_truncated"] is True
    assert payload["output_complete"] is False

    monitor_result = _evaluate_real_audit_payload(payload)

    assert monitor_result.healthy is True
    assert monitor_result.reason_codes == ()


def test_monitor_catches_abnormal_batch_outside_returned_display_window(tmp_path):
    payload = _audit_payload_with_management_history(
        tmp_path,
        oldest_status="recovery_required",
    )

    assert payload["counts"]["recovery_required"] == 1
    assert all(batch["status"] == "succeeded" for batch in payload["batches"])

    monitor_result = _evaluate_real_audit_payload(payload)

    assert monitor_result.healthy is False
    assert monitor_result.reason_codes == ("audit_abnormal",)
    assert monitor_result.details["audit_abnormal_count"] == 1


def test_monitor_catches_malformed_batch_outside_returned_display_window(tmp_path):
    payload = _audit_payload_with_management_history(
        tmp_path,
        oldest_target_snapshot_json="{malformed",
    )

    assert all(batch["status"] == "succeeded" for batch in payload["batches"])
    assert payload["malformed_row_count"] == 1
    assert payload["malformed_field_count"] == 1

    monitor_result = _evaluate_real_audit_payload(payload)

    assert monitor_result.healthy is False
    assert monitor_result.reason_codes == ("audit_abnormal",)


def test_monitor_rejects_leg_truncation_outside_returned_display_window(tmp_path):
    payload = _audit_payload_with_management_history(
        tmp_path,
        oldest_leg_count=101,
    )

    assert all(batch["status"] == "succeeded" for batch in payload["batches"])
    assert payload.get("all_history_legs_complete") is False

    monitor_result = _evaluate_real_audit_payload(payload)

    assert monitor_result.healthy is False
    assert monitor_result.reason_codes == ("audit_incomplete",)


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
                    status TEXT, target_snapshot_json TEXT, planned_at TEXT,
                    completed_at TEXT
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
            assert path.stat() == before[path.name][1]
            assert path.read_bytes() == before[path.name][0]
    finally:
        tmp_path.chmod(0o755)
        for suffix in ("", "-wal", "-shm"):
            path = database_path.with_name(database_path.name + suffix)
            if path.exists():
                path.chmod(0o644)
        connection.close()


def test_audit_management_batches_recovers_when_snapshot_changes(
    tmp_path, monkeypatch
):
    import telegram_kol_research.cli as cli_module

    database_path = tmp_path / "changing.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE harmless (id INTEGER PRIMARY KEY)")
        connection.commit()
    original = cli_module._stream_snapshot_component
    calls = 0

    def changing_read(path, destination):
        nonlocal calls
        result = original(path, destination)
        calls += 1
        if calls == 1:
            with open(database_path, "ab") as stream:
                stream.write(b"changed")
        return result

    monkeypatch.setattr(cli_module, "_stream_snapshot_component", changing_read)
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

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["snapshot_status"] == "stable"
    assert payload["snapshot_components"] == ["sqlite_online_backup"]


def test_audit_management_batches_uses_online_backup_after_component_churn(
    tmp_path, monkeypatch
):
    import telegram_kol_research.cli as cli_module

    database_path = tmp_path / "churning.db"
    session_factory = create_session_factory(database_path)
    session_factory.kw["bind"].dispose()
    before = database_path.read_bytes(), database_path.stat()
    monkeypatch.setattr(
        cli_module,
        "_capture_source_components",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            cli_module.ManagementAuditSnapshotError(
                "source_component_changed_during_read"
            )
        ),
    )

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
    assert payload["snapshot_components"] == ["sqlite_online_backup"]
    assert database_path.read_bytes() == before[0]
    assert database_path.stat() == before[1]


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


def test_linux_noatime_open_failure_refuses_before_source_read(tmp_path, monkeypatch):
    import telegram_kol_research.cli as cli_module

    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    source.write_bytes(b"source-bytes")
    before = source.read_bytes(), source.stat(), sorted(p.name for p in tmp_path.iterdir())
    real_open = os.open
    noatime_flag = 0x40000

    def refusing_open(path, flags, *args, **kwargs):
        if os.fspath(path) == os.fspath(source) and flags & noatime_flag:
            raise PermissionError("forced O_NOATIME failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refusing_open)
    try:
        cli_module._stream_linux_noatime_component(
            source, destination, noatime_flag=noatime_flag
        )
    except cli_module.ManagementAuditSnapshotError as exc:
        assert exc.status == "snapshot_unavailable"
        assert exc.reason == "noatime_open_failed"
    else:
        raise AssertionError("expected fail-closed no-atime refusal")

    assert destination.exists() is False
    assert source.read_bytes() == before[0]
    assert source.stat() == before[1]
    assert sorted(p.name for p in tmp_path.iterdir()) == before[2]


def test_linux_noatime_permission_fallback_reads_only_verified_readonly_mount(
    tmp_path, monkeypatch
):
    import telegram_kol_research.cli as cli_module

    source = tmp_path / "source.db"
    destination = tmp_path / "copy.db"
    source.write_bytes(b"readonly snapshot")
    source_stat = source.stat()
    os.utime(
        source,
        ns=(source_stat.st_mtime_ns + 1_000_000_000, source_stat.st_mtime_ns),
    )
    real_open = os.open
    noatime_flag = 0x40000

    def guarded_open(path, flags, *args, **kwargs):
        if os.fspath(path) == os.fspath(source) and flags & noatime_flag:
            raise PermissionError("unprivileged O_NOATIME")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: SimpleNamespace(f_flag=os.ST_RDONLY),
    )

    evidence = cli_module._stream_linux_noatime_component(
        source, destination, noatime_flag=noatime_flag
    )

    assert destination.read_bytes() == b"readonly snapshot"
    assert evidence["size"] == len(b"readonly snapshot")


def test_snapshot_capture_streams_to_files_and_returns_metadata_only(tmp_path):
    import telegram_kol_research.cli as cli_module

    database_path = tmp_path / "stream.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE harmless (id INTEGER PRIMARY KEY)")
        connection.commit()
    snapshot_root = tmp_path / "private"

    metadata = cli_module._capture_source_components(database_path, snapshot_root)

    assert metadata["main"]["size"] == database_path.stat().st_size
    assert len(metadata["main"]["sha256"]) == 64
    assert all(not isinstance(value, (bytes, bytearray)) for value in metadata.values())
    assert (snapshot_root / "audit.db").read_bytes() == database_path.read_bytes()


def test_snapshot_component_large_sparse_file_has_chunk_bounded_memory(tmp_path):
    import telegram_kol_research.cli as cli_module

    source = tmp_path / "large.db"
    destination = tmp_path / "private.db"
    with source.open("wb") as stream:
        stream.seek(32 * 1024 * 1024 - 1)
        stream.write(b"x")

    tracemalloc.start()
    try:
        metadata = cli_module._stream_snapshot_component(source, destination)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert metadata["size"] == 32 * 1024 * 1024
    assert destination.stat().st_size == metadata["size"]
    assert peak < 8 * 1024 * 1024


def test_audit_management_batches_maps_top_level_data_error_safely(
    tmp_path, monkeypatch
):
    import telegram_kol_research.cli as cli_module

    database_path = tmp_path / "data-error.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE harmless (id INTEGER PRIMARY KEY)")
        connection.commit()
    monkeypatch.setattr(
        cli_module,
        "_audit_management_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(MemoryError("secret-value")),
    )

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
    assert payload["snapshot_status"] == "snapshot_unavailable"
    assert payload["snapshot_reason"] == "audit_data_validation_failed"
    assert "secret-value" not in result.stdout
    assert "MemoryError" not in result.stdout


def test_audit_management_batches_resource_attack_values_are_malformed(tmp_path):
    database_path = tmp_path / "resource-attacks.db"
    create_session_factory(database_path)
    deep_payload = "[" * 2000 + "0" + "]" * 2000
    huge_id_payload = json.dumps({"management_batch_id": "9" * 5000})
    oversized_payload = "x" * 70_000
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO strategy_management_batches "
            "(id, idempotency_fingerprint, raw_message_id, recognition_decision_id, "
            "recognition_generation, target_lifecycle_id, strategy_instance_id, "
            "execution_binding_id, intent, effective_action, execution_mode, "
            "partial_round_before, status, target_fingerprint, target_snapshot_json, "
            "planned_at, created_at, updated_at) "
            "VALUES (901, 'fp-901', 902, 903, 'gen', 904, 'strategy', 905, "
            "'partial_take_profit', 'partial_close', 'shadow', 0, 'ready', 'target', "
            "'{}', '2026-07-15 10:00:00', '2026-07-15 10:00:00', "
            "'2026-07-15 10:00:00')"
        )
        for leg_id, size in ((910, "1E+100000000"), (911, "1E-100000000")):
            connection.execute(
                "INSERT INTO strategy_management_legs "
                "(id, management_batch_id, execution_order_leg_id, pos_id, leg_index, "
                "status, preflight_size, planned_close_size, created_at, updated_at) "
                "VALUES (?, 901, ?, ?, ?, 'planned', ?, ?, "
                "'2026-07-15 10:00:00', '2026-07-15 10:00:00')",
                (leg_id, leg_id + 100, f"pos-{leg_id}", leg_id - 909, size, size),
            )
        for signal_id, payload in enumerate(
            (huge_id_payload, deep_payload, oversized_payload), start=920
        ):
            connection.execute(
                "INSERT INTO trade_signals "
                "(id, signal_uid, source_type, venue, kol_id, chat_id, message_id, "
                "symbol, side, action, status, payload_json, attempts, created_at, updated_at) "
                "VALUES (?, ?, 'automatic', 'deepcoin', 'kol', -1001, ?, 'BTC', "
                "'short', 'close_position', 'pending', ?, 0, "
                "'2026-07-15 10:00:00', '2026-07-15 10:00:00')",
                (signal_id, f"signal-{signal_id}", signal_id, payload),
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
    assert payload["legacy_pending_management"]["total"] == 3
    assert payload["legacy_pending_management"]["malformed_payload_count"] == 3
    assert all(leg["preflight_size"] is None for leg in payload["batches"][0]["legs"])
    assert all(
        leg["planned_close_size"] is None for leg in payload["batches"][0]["legs"]
    )
    assert "100000000" not in result.stdout
    assert "9" * 200 not in result.stdout


def test_audit_management_batches_bounds_batch_and_leg_json_fields(tmp_path):
    database_path = tmp_path / "bounded-json-fields.db"
    create_session_factory(database_path)
    oversized_marker = "oversized-secret-" + "x" * 70_000
    deeply_nested = "[" * 100_000 + "0" + "]" * 100_000
    depth_over_limit = "[" * 1000 + "0" + "]" * 1000
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO strategy_management_batches "
            "(id, idempotency_fingerprint, raw_message_id, recognition_decision_id, "
            "recognition_generation, target_lifecycle_id, strategy_instance_id, "
            "execution_binding_id, intent, effective_action, execution_mode, "
            "partial_round_before, status, target_fingerprint, target_snapshot_json, "
            "planned_at, created_at, updated_at) "
            "VALUES (1201, 'fp-1201', 1202, 1203, 'gen', 1204, 'strategy', 1205, "
            "'partial_take_profit', 'partial_close', 'shadow', 0, 'ready', 'target', ?, "
            "'2026-07-15 10:00:00', '2026-07-15 10:00:00', "
            "'2026-07-15 10:00:00')",
            (oversized_marker,),
        )
        connection.execute(
            "INSERT INTO strategy_management_legs "
            "(id, management_batch_id, execution_order_leg_id, pos_id, leg_index, "
            "status, preflight_size, planned_close_size, last_error, created_at, updated_at) "
            "VALUES (1210, 1201, 1211, 'pos-1210', 1, 'planned', '0.02', '0.01', ?, "
            "'2026-07-15 10:00:00', '2026-07-15 10:00:00')",
            (deeply_nested,),
        )
        connection.execute(
            "INSERT INTO strategy_management_legs "
            "(id, management_batch_id, execution_order_leg_id, pos_id, leg_index, "
            "status, preflight_size, planned_close_size, last_error, created_at, updated_at) "
            "VALUES (1212, 1201, 1213, 'pos-1212', 2, 'planned', '0.02', '0.01', ?, "
            "'2026-07-15 10:00:00', '2026-07-15 10:00:00')",
            (depth_over_limit,),
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
    batch = payload["batches"][0]
    assert batch["malformed_json_fields"] == ["target_snapshot_json"]
    assert all(
        leg["malformed_json_fields"] == ["last_error"] for leg in batch["legs"]
    )
    assert payload["malformed_row_count"] >= 3
    assert payload["malformed_field_count"] >= 3
    assert "oversized-secret" not in result.stdout
    assert "[[[[[[[[[[" not in result.stdout


def test_bounded_json_validator_catches_parser_memory_error(monkeypatch):
    import telegram_kol_research.cli as cli_module

    monkeypatch.setattr(
        cli_module.json,
        "loads",
        lambda value: (_ for _ in ()).throw(MemoryError("secret parser value")),
    )

    value, malformed = cli_module._bounded_json_value('{"safe": true}')

    assert value is None
    assert malformed is True


def test_audit_management_batches_private_snapshot_oserrors_are_safe(
    tmp_path, monkeypatch
):
    import telegram_kol_research.cli as cli_module

    database_path = tmp_path / "snapshot-errors.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE harmless (id INTEGER PRIMARY KEY)")
        connection.commit()

    scenarios = (
        ("temporary_directory", "private_snapshot_unavailable"),
        ("source_copy", "source_copy_failed"),
        ("fsync", "private_snapshot_unavailable"),
    )
    for name, expected_reason in scenarios:
        with monkeypatch.context() as scoped:
            # Each installer closes over the test's monkeypatch; rebind the
            # target through this isolated context to avoid leaking scenarios.
            if name == "temporary_directory":
                scoped.setattr(
                    cli_module.tempfile,
                    "TemporaryDirectory",
                    lambda *args, **kwargs: (_ for _ in ()).throw(
                        OSError("secret temp failure")
                    ),
                )
            elif name == "source_copy":
                scoped.setattr(
                    cli_module,
                    "_stream_snapshot_component",
                    lambda *args, **kwargs: (_ for _ in ()).throw(
                        OSError("secret write failure")
                    ),
                )
            else:
                scoped.setattr(
                    cli_module.os,
                    "fsync",
                    lambda *args, **kwargs: (_ for _ in ()).throw(
                        OSError("secret fsync failure")
                    ),
                )
            for output_format in ("json", "text"):
                result = CliRunner().invoke(
                    app,
                    [
                        "audit-management-batches",
                        "--database-path",
                        str(database_path),
                        "--output-format",
                        output_format,
                    ],
                )
                assert result.exit_code == 1, (name, result.stdout)
                assert expected_reason in result.stdout
                assert "secret" not in result.stdout
                assert "Traceback" not in result.stdout


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
                    status TEXT, target_snapshot_json TEXT, planned_at TEXT,
                    completed_at TEXT
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
                    'unknown state !', '{bad', 'not-a-date', 'also-not-a-date'
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
