from __future__ import annotations

import pytest

from telegram_kol_research.runtime_agent_contracts import RuntimeAgentDiagnosis
from telegram_kol_research.runtime_incident_handoff import (
    RuntimeIncidentHandoffError,
    build_runtime_incident_handoff,
    load_runtime_incident_handoff,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.runtime_incidents import record_runtime_incident
from telegram_kol_research.models import RuntimeIncident
from datetime import UTC, datetime


def _diagnosis():
    return RuntimeAgentDiagnosis.from_mapping(
        {
            "incident_id": 17,
            "diagnosis_hypothesis": "Worker retries may have been exhausted.",
            "confidence": "low",
            "evidence_references": ["incident:17", "worker-job:42"],
            "missing_evidence": ["current provider health"],
            "recommended_playbook_name": None,
            "auto_handle_eligible": False,
            "codex_handoff_required": True,
            "remaining_risk": "The source job remains unresolved.",
        },
        expected_incident_id=17,
    )


def test_handoff_keeps_evidence_and_hypothesis_separate_and_requires_verification():
    handoff = build_runtime_incident_handoff(
        incident={
            "id": 17,
            "incident_type": "worker_retry_exhausted",
            "source_kind": "worker_job",
            "source_record_id": "42",
            "redacted_summary": {"error_type": "provider_timeout"},
        },
        diagnosis=_diagnosis(),
        attempted_queries=("get_incident_summary", "get_worker_state"),
    )

    assert handoff["incident"]["id"] == 17
    assert handoff["evidence_references"] == ["incident:17", "worker-job:42"]
    assert handoff["agent_hypothesis"]["confidence"] == "low"
    assert "独立验证" in handoff["codex_prompt"]
    assert "AGENTS.md" in handoff["codex_prompt"]
    assert "禁止" in handoff["codex_prompt"]
    assert len(handoff["codex_prompt"]) <= 512


def test_handoff_bounds_codex_prompt_for_maximum_contract_text():
    handoff = build_runtime_incident_handoff(
        incident={
            "id": 17,
            "incident_type": "worker_retry_exhausted",
            "source_kind": "worker_job",
            "source_record_id": "42",
            "redacted_summary": {"error_type": "provider_timeout"},
        },
        diagnosis=RuntimeAgentDiagnosis.from_mapping(
            {
                "incident_id": 17,
                "diagnosis_hypothesis": "h" * 512,
                "confidence": "low",
                "evidence_references": ["incident:17"],
                "missing_evidence": ["m" * 512],
                "recommended_playbook_name": None,
                "auto_handle_eligible": False,
                "codex_handoff_required": True,
                "remaining_risk": "r" * 512,
            },
            expected_incident_id=17,
        ),
        attempted_queries=("get_incident_summary",),
    )

    assert len(handoff["codex_prompt"]) <= 512


def test_handoff_rejects_sensitive_or_mismatched_incident_material():
    with pytest.raises(RuntimeIncidentHandoffError):
        build_runtime_incident_handoff(
            incident={
                "id": 18,
                "incident_type": "worker_retry_exhausted",
                "source_kind": "worker_job",
                "source_record_id": "42",
                "redacted_summary": {"api_key": "secret"},
            },
            diagnosis=_diagnosis(),
            attempted_queries=(),
        )


def test_handoff_is_reproducible_from_the_durable_separate_ledger_fields(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = record_runtime_incident(
        session_factory,
        source_kind="worker_job",
        source_record_id="42",
        incident_type="worker_retry_exhausted",
        severity="high",
        fingerprint="e" * 64,
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
            '"remaining_risk":"Job unresolved",'
            '"attempted_queries":["get_incident_summary"]}'
        )
        row.evidence_refs_json = f'["incident:{incident.id}"]'
        session.commit()

    handoff = load_runtime_incident_handoff(
        session_factory, incident_id=incident.id
    )

    assert handoff["incident"]["id"] == incident.id
    assert handoff["agent_hypothesis"]["text"] == "Provider outage"
    assert handoff["attempted_queries"] == ["get_incident_summary"]
    assert "独立验证" in handoff["codex_prompt"]


def test_handoff_rebuilds_bounded_shadow_playbook_audit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = record_runtime_incident(
        session_factory,
        source_kind="worker_job",
        source_record_id="42",
        incident_type="worker_retry_exhausted",
        severity="high",
        fingerprint="f" * 64,
        redacted_summary='{"error_type":"provider_timeout"}',
        occurred_at=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        feature_policy_version="runtime-incident-phase-5-v1",
        prompt_version="runtime-agent-prompt-v3",
        tool_policy_version="runtime-agent-tools-v2",
    )
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.status = "diagnosed"
        row.diagnosis_json = (
            '{"hypothesis":"Provider outage","confidence":"medium",'
            '"missing_evidence":[],"recommended_playbook":'
            '"reschedule_non_writing_ai_job",'
            '"auto_handle_eligible":false,"codex_handoff_required":true,'
            '"remaining_risk":"Job unresolved","attempted_queries":[],'
            '"shadow_playbook_policy":{"mode":"shadow",'
            '"policy_version":"runtime-shadow-policy-v1",'
            '"nominated_playbook":"reschedule_non_writing_ai_job",'
            '"playbook_version":1,"accepted":false,'
            '"refusal_reasons":["business_write_absence_not_proven"],'
            '"verification_query":"get_worker_state",'
            '"would_execute":false,"action_executed":false}}'
        )
        row.evidence_refs_json = f'["incident:{incident.id}"]'
        session.commit()

    handoff = load_runtime_incident_handoff(
        session_factory, incident_id=incident.id
    )

    assert handoff["attempted_playbooks"][0]["name"] == (
        "reschedule_non_writing_ai_job"
    )
    assert handoff["attempted_playbooks"][0]["accepted"] is False
    assert handoff["attempted_playbooks"][0]["action_executed"] is False
