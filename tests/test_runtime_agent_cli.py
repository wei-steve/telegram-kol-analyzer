from __future__ import annotations

import json
from datetime import UTC, datetime

from typer.testing import CliRunner

from telegram_kol_research.cli import app, _build_runtime_agent_cli_tools
from telegram_kol_research.runtime_agent_tools import (
    READ_ONLY_RUNTIME_AGENT_TOOL_NAMES,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RuntimeIncident
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
