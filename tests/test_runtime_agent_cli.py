from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from telegram_kol_research.cli import (
    _build_runtime_agent_action_handlers,
    _build_runtime_agent_cli_tools,
    app,
)
from telegram_kol_research.runtime_agent_tools import (
    READ_ONLY_RUNTIME_AGENT_TOOL_NAMES,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionIncident,
    RuntimeIncident,
    StrategyManagementBatch,
)
from telegram_kol_research.runtime_incidents import record_runtime_incident


def test_runtime_agent_cli_is_dormant_without_feature_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_KOL_RUNTIME_AGENT_ENABLED", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "runtime-incident-agent-once",
            "--database-path",
            str(tmp_path / "research.db"),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "incident_id": None,
        "status": "disabled",
        "tool_steps": 0,
    }


def test_runtime_agent_cli_rejects_missing_dedicated_provider_before_claim(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    incident = record_runtime_incident(
        session_factory,
        source_kind="worker_job",
        source_record_id="dedicated-provider",
        incident_type="worker_retry_exhausted",
        severity="high",
        fingerprint="d" * 64,
        redacted_summary='{"error_type":"provider_timeout"}',
        occurred_at=datetime(2026, 7, 29, 9, 0, tzinfo=UTC),
        feature_policy_version="runtime-incident-phase-6-v1",
        prompt_version="runtime-agent-prompt-v4",
        tool_policy_version="runtime-agent-tools-v2",
    )
    shared_key = "shared-key-must-not-leak"
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_AGENT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_KOL_LLM_API_KEY", shared_key)
    monkeypatch.delenv(
        "TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY", raising=False
    )

    result = CliRunner().invoke(
        app,
        [
            "runtime-incident-agent-once",
            "--database-path",
            str(database_path),
        ],
    )

    assert result.exit_code != 0
    assert (
        "dedicated Runtime Agent provider configuration is invalid"
        in str(result.exception)
    )
    assert shared_key not in result.output
    assert shared_key not in str(result.exception)
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        assert row.status == "pending"
        assert row.claim_token is None
        assert row.agent_attempt_count == 0


def test_runtime_agent_worker_rejects_missing_dedicated_provider_before_claim(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    incident = record_runtime_incident(
        session_factory,
        source_kind="worker_job",
        source_record_id="dedicated-provider-worker",
        incident_type="worker_retry_exhausted",
        severity="high",
        fingerprint="e" * 64,
        redacted_summary='{"error_type":"provider_timeout"}',
        occurred_at=datetime(2026, 7, 29, 9, 0, tzinfo=UTC),
        feature_policy_version="runtime-incident-phase-6-v1",
        prompt_version="runtime-agent-prompt-v4",
        tool_policy_version="runtime-agent-tools-v2",
    )
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_AGENT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_KOL_LLM_API_KEY", "shared-key")
    monkeypatch.delenv(
        "TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY", raising=False
    )

    result = CliRunner().invoke(
        app,
        [
            "runtime-incident-agent-worker",
            "--database-path",
            str(database_path),
        ],
    )

    assert result.exit_code != 0
    assert (
        "dedicated Runtime Agent provider configuration is invalid"
        in str(result.exception)
    )
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        assert row.status == "pending"
        assert row.claim_token is None
        assert row.agent_attempt_count == 0


def test_runtime_agent_cli_prints_reproducible_handoff(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    incident = record_runtime_incident(
        session_factory,
        source_kind="worker_job",
        source_record_id="42",
        incident_type="worker_retry_exhausted",
        severity="high",
        fingerprint="f" * 64,
        redacted_summary='{"error_type":"provider_timeout"}',
        occurred_at=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        feature_policy_version="runtime-incident-phase-3-v1",
        prompt_version="runtime-agent-prompt-v1",
        tool_policy_version="runtime-agent-tools-v1",
    )
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.status = "diagnosed"
        row.diagnosis_json = (
            '{"hypothesis":"Provider outage","confidence":"medium",'
            '"missing_evidence":[],"recommended_playbook":null,'
            '"auto_handle_eligible":false,"codex_handoff_required":true,'
            '"remaining_risk":"Job unresolved","attempted_queries":[]}'
        )
        row.evidence_refs_json = f'["incident:{incident.id}"]'
        session.commit()

    result = CliRunner().invoke(
        app,
        [
            "runtime-incident-handoff",
            str(incident.id),
            "--database-path",
            str(database_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["incident"]["id"] == incident.id
    assert "独立验证" in payload["codex_prompt"]


def test_runtime_agent_cli_configures_every_phase3_read_only_projection(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    incident = record_runtime_incident(
        session_factory,
        source_kind="worker_job",
        source_record_id="42",
        incident_type="worker_retry_exhausted",
        severity="high",
        fingerprint="a" * 64,
        redacted_summary='{"error_type":"provider_timeout"}',
        occurred_at=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        feature_policy_version="runtime-incident-phase-3-v1",
        prompt_version="runtime-agent-prompt-v1",
        tool_policy_version="runtime-agent-tools-v1",
    )
    monitor_state_path = tmp_path / "monitor-state.json"
    monitor_state_path.write_text(
        '{"last_full_audit_date":"2026-07-28",'
        '"last_window_at":"2026-07-28T09:00:00",'
        '"last_notification_at":null,"anomaly_fingerprint":null}',
        encoding="utf-8",
    )
    registry = _build_runtime_agent_cli_tools(
        session_factory,
        max_output_bytes=8192,
        monitor_state_path=monitor_state_path,
        journal_reader=lambda: (
            {"priority": "3", "timestamp": "2026-07-28T09:00:00"},
            {"priority": "6", "timestamp": "2026-07-28T09:01:00"},
        ),
    )

    assert registry.allowed_tools == READ_ONLY_RUNTIME_AGENT_TOOL_NAMES
    for name in sorted(READ_ONLY_RUNTIME_AGENT_TOOL_NAMES):
        result = registry.execute(
            name,
            {"incident_id": incident.id},
            expected_incident_id=incident.id,
        )
        assert result.evidence_refs
        assert isinstance(result.data, dict)


def test_phase6_production_handlers_wire_only_a_real_read_only_plan(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        batch = StrategyManagementBatch(
            idempotency_fingerprint="phase6-plan-handler",
            raw_message_id=1,
            recognition_decision_id=1,
            recognition_generation="fixture",
            target_lifecycle_id=1,
            strategy_instance_id="fixture",
            execution_binding_id=1,
            intent="close",
            effective_action="close",
            target_fingerprint="target",
            target_snapshot_json="{}",
            status="partial_failed",
        )
        session.add(batch)
        session.commit()
        batch_id = batch.id
    incident = record_runtime_incident(
        session_factory,
        source_kind="strategy_management_batch",
        source_record_id=str(batch_id),
        incident_type="management_partial_failed",
        severity="high",
        fingerprint="c" * 64,
        redacted_summary='{"source_status":"partial_failed"}',
        occurred_at=datetime(2026, 7, 29, 9, 0, tzinfo=UTC),
        feature_policy_version="runtime-incident-phase-6-v1",
        prompt_version="runtime-agent-prompt-v4",
        tool_policy_version="runtime-agent-tools-v2",
    )
    tools = _build_runtime_agent_cli_tools(
        session_factory,
        max_output_bytes=8192,
        monitor_state_path=tmp_path / "missing-monitor-state.json",
        journal_reader=lambda: (),
    )
    handlers = _build_runtime_agent_action_handlers(tools)

    assert set(handlers) == {"build_read_only_reconciliation_plan"}
    assert handlers["build_read_only_reconciliation_plan"](
        incident_id=incident.id,
        idempotency_key="runtime-incident:1:plan:v1",
        expected_fingerprint=incident.fingerprint,
    ) is True


def test_phase4_protection_projection_is_bounded_and_omits_order_ids(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="fixture",
            chat_id=1,
            message_id=2,
            symbol="BTCUSDT",
            side="long",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            leg_index=0,
            purpose="entry",
            pos_id="fixture-position",
        )
        session.add(leg)
        session.flush()
        protection = PositionProtectionIncident(
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            pos_id="fixture-position",
            incident_type="missing_stop_loss",
            fingerprint="p" * 64,
            evidence_json=(
                '{"reason_code":"missing_stop_loss",'
                '"missing_stop_loss":true,'
                '"take_profit_order_ids":["tp-secret-reference"],'
                '"stop_loss_order_ids":[],"position_size":"1.0"}'
            ),
        )
        session.add(protection)
        session.commit()
        protection_id = protection.id

    incident = record_runtime_incident(
        session_factory,
        source_kind="position_protection_incident",
        source_record_id=str(protection_id),
        incident_type="severe_protection_incident",
        severity="critical",
        fingerprint="b" * 64,
        redacted_summary='{"reason_code":"missing_stop_loss"}',
        occurred_at=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        feature_policy_version="runtime-incident-phase-4-v1",
        prompt_version="runtime-agent-prompt-v1",
        tool_policy_version="runtime-agent-tools-v2",
    )
    registry = _build_runtime_agent_cli_tools(
        session_factory,
        max_output_bytes=8192,
        monitor_state_path=tmp_path / "missing-monitor-state.json",
        journal_reader=lambda: (),
    )

    result = registry.execute(
        "get_protection_summary",
        {"incident_id": incident.id},
        expected_incident_id=incident.id,
    )

    assert result.data["applicable"] is True
    assert result.data["protection"]["missing_stop_loss"] is True
    assert result.data["protection"]["take_profit_count"] == 1
    assert "tp-secret-reference" not in str(result.as_model_payload())


def test_phase4_offline_evaluation_cli_reports_reviewed_corpus_metrics():
    corpus = Path(__file__).parent / "fixtures" / "runtime_incidents"

    result = CliRunner().invoke(
        app,
        ["runtime-incident-agent-evaluate", "--corpus-path", str(corpus)],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["case_count"] == 7
    assert payload["all_passed"] is True
    assert payload["unsafe_recommendation_refusal_rate"] == 1.0
