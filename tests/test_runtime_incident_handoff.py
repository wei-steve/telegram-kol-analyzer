from __future__ import annotations

import json
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
            "expected_state": "The worker reaches a terminal state.",
            "observed_state": "The worker exhausted retries.",
            "classification": "code_defect",
            "affected_message_ids": [],
            "likely_code_paths": ["src/telegram_kol_research/runtime_agent_worker.py"],
            "likely_test_paths": ["tests/test_runtime_agent_worker.py"],
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
                "expected_state": "Expected terminal result.",
                "observed_state": "Observed missing result.",
                "classification": "insufficient_evidence",
                "affected_message_ids": [],
                "likely_code_paths": [],
                "likely_test_paths": [],
            },
            expected_incident_id=17,
        ),
        attempted_queries=("get_incident_summary",),
    )

    assert len(handoff["codex_prompt"]) <= 512


def test_handoff_accepts_one_real_message_operation_instruction_item():
    handoff = build_runtime_incident_handoff(
        incident={
            "id": 17,
            "incident_type": "message_operation_failure",
            "source_kind": "message_operation_violation",
            "source_record_id": "no_operation_created",
            "severity": "high",
            "redacted_summary": {
                "message_operation_snapshot": {
                    "affected_message_count": 1,
                    "truncated": False,
                    "affected_message_ids": [91],
                    "contracts": [
                        {
                            "contract_id": 4,
                            "intent_kind": "take_profit",
                            "expected_terminal_kind": "verified_management",
                            "observed_status": "violated",
                            "violation_code": "no_operation_created",
                            "evidence_refs": ["message_operation_contract:4"],
                            "items": [
                                {
                                    "instruction_kind": "take_profit",
                                    "expected_descendant_kind": "management_item",
                                    "expected_terminal_kind": "verified_management",
                                    "observed_terminal_kind": None,
                                    "status": "violated",
                                    "evidence_refs": ["message_operation_item:8"],
                                }
                            ],
                        }
                    ],
                    "message_evidence": [
                        {
                            "raw_message_id": 91,
                            "chat_id": 700,
                            "message_id": 8101,
                            "reply_to_message_id": 8000,
                            "source_status": "active",
                        }
                    ],
                    "timeline": [
                        {
                            "contract_id": 4,
                            "created_at": "2026-08-09T00:00:00",
                            "updated_at": "2026-08-09T00:01:00",
                            "status": "violated",
                        }
                    ],
                }
            },
        },
        diagnosis=_diagnosis(),
        attempted_queries=("investigate_message_evidence",),
    )

    assert handoff["instruction_items"][0]["status"] == "violated"


def test_handoff_compacts_oversized_message_evidence_and_reserves_document_space(
    tmp_path,
):
    from telegram_kol_research.runtime_incident_handoff import (
        build_runtime_incident_failure_handoff,
        persist_runtime_incident_handoff,
    )

    contracts = [
        {
            "contract_id": contract_index + 1,
            "intent_kind": "take_profit",
            "expected_terminal_kind": "verified_management",
            "observed_status": "violated",
            "violation_code": "no_operation_created",
            "evidence_refs": [
                f"contract-evidence:{contract_index}:{ref_index}:" + ("c" * 88)
                for ref_index in range(20)
            ],
            "items": [
                {
                    "instruction_kind": "take_profit",
                    "expected_descendant_kind": "management_item",
                    "expected_terminal_kind": "verified_management",
                    "observed_terminal_kind": None,
                    "status": "violated",
                    "evidence_refs": [
                        f"item-evidence:{contract_index}:{ref_index}:" + ("x" * 96)
                        for ref_index in range(20)
                    ],
                }
            ],
        }
        for contract_index in range(32)
    ]
    handoff = build_runtime_incident_handoff(
        incident={
            "id": 17,
            "incident_type": "message_operation_failure",
            "source_kind": "message_operation_violation",
            "source_record_id": "no_operation_created",
            "severity": "high",
            "redacted_summary": {
                "component": "message_operation_supervisor",
                "message_operation_snapshot": {
                    "affected_message_count": 1,
                    "truncated": False,
                    "affected_message_ids": [91],
                    "contracts": contracts,
                    "message_evidence": [],
                    "timeline": [],
                },
            },
        },
        diagnosis=_diagnosis(),
        attempted_queries=("investigate_message_evidence",),
    )

    assert "message_operation_snapshot" not in handoff["incident"]["redacted_summary"]
    assert handoff["compaction"]["omitted_instruction_evidence_refs"] > 0
    assert handoff["compaction"]["omitted_contract_evidence_refs"] > 0
    assert handoff["compaction"]["omitted_contracts"] == 16
    assert handoff["compaction"]["omitted_instruction_items"] == 16
    assert len(json.dumps(handoff, ensure_ascii=False).encode("utf-8")) <= 24576
    changed_contracts = json.loads(json.dumps(contracts))
    changed_contracts[0]["evidence_refs"][1] = (
        "contract-evidence:changed-omitted:" + ("z" * 88)
    )
    changed_source_handoff = build_runtime_incident_handoff(
        incident={
            "id": 17,
            "incident_type": "message_operation_failure",
            "source_kind": "message_operation_violation",
            "source_record_id": "no_operation_created",
            "severity": "high",
            "redacted_summary": {
                "component": "message_operation_supervisor",
                "message_operation_snapshot": {"contracts": changed_contracts},
            },
        },
        diagnosis=_diagnosis(),
        attempted_queries=("investigate_message_evidence",),
    )
    assert (
        changed_source_handoff["source_evidence_fingerprint"]
        != handoff["source_evidence_fingerprint"]
    )
    failure_handoff = build_runtime_incident_failure_handoff(
        incident={
            "id": 17,
            "incident_type": "message_operation_failure",
            "source_kind": "message_operation_violation",
            "source_record_id": "no_operation_created",
            "severity": "high",
            "redacted_summary": {
                "message_operation_snapshot": {"contracts": contracts}
            },
        },
        outcome_kind="evidence_incomplete",
        attempted_queries=("investigate_message_evidence",),
    )
    assert failure_handoff["compaction"]["omitted_contracts"] == 16
    assert len(
        json.dumps(failure_handoff, ensure_ascii=False).encode("utf-8")
    ) <= 24576

    session_factory = create_session_factory(tmp_path / "oversized-handoff.db")
    incident = record_runtime_incident(
        session_factory,
        source_kind="message_operation_violation",
        source_record_id="no_operation_created",
        incident_type="message_operation_failure",
        severity="high",
        fingerprint="b" * 64,
        redacted_summary='{"component":"message_operation_supervisor"}',
        occurred_at=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        feature_policy_version="runtime-incident-phase-8r-v1",
        prompt_version="runtime-agent-prompt-v8",
        tool_policy_version="runtime-agent-tools-v2",
    )
    artifact = persist_runtime_incident_handoff(
        session_factory,
        incident_id=incident.id,
        outcome_kind="evidence_incomplete",
        handoff={
            **handoff,
            "incident": {**handoff["incident"], "id": incident.id},
        },
        created_at=datetime(2026, 7, 28, 9, 1, tzinfo=UTC),
    )
    changed_artifact = persist_runtime_incident_handoff(
        session_factory,
        incident_id=incident.id,
        outcome_kind="evidence_incomplete",
        handoff={
            **changed_source_handoff,
            "incident": {
                **changed_source_handoff["incident"],
                "id": incident.id,
            },
        },
        created_at=datetime(2026, 7, 28, 9, 2, tzinfo=UTC),
    )
    assert len(artifact.evidence_document_json.encode("utf-8")) <= 32768
    assert changed_artifact.diagnosis_revision == 2


def test_omitted_diagnosis_change_creates_a_new_handoff_revision(tmp_path):
    from telegram_kol_research.runtime_incident_handoff import (
        persist_runtime_incident_handoff,
    )

    session_factory = create_session_factory(tmp_path / "diagnosis-digest.db")
    incident = record_runtime_incident(
        session_factory,
        source_kind="worker_job",
        source_record_id="44",
        incident_type="worker_retry_exhausted",
        severity="high",
        fingerprint="c" * 64,
        redacted_summary='{"error_type":"provider_timeout"}',
        occurred_at=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        feature_policy_version="runtime-incident-phase-8r-v1",
        prompt_version="runtime-agent-prompt-v8",
        tool_policy_version="runtime-agent-tools-v2",
    )

    def diagnosis(last_missing: str):
        return RuntimeAgentDiagnosis.from_mapping(
            {
                "incident_id": incident.id,
                "diagnosis_hypothesis": "Worker retries may have been exhausted.",
                "confidence": "low",
                "evidence_references": [f"incident:{incident.id}"],
                "missing_evidence": [
                    *[f"missing-{index}" for index in range(8)],
                    last_missing,
                ],
                "recommended_playbook_name": None,
                "auto_handle_eligible": False,
                "codex_handoff_required": True,
                "remaining_risk": "The source job remains unresolved.",
                "expected_state": "The worker reaches a terminal state.",
                "observed_state": "The worker exhausted retries.",
                "classification": "code_defect",
                "affected_message_ids": [],
                "likely_code_paths": [],
                "likely_test_paths": [],
            },
            expected_incident_id=incident.id,
        )

    common_incident = {
        "id": incident.id,
        "incident_type": incident.incident_type,
        "source_kind": incident.source_kind,
        "source_record_id": incident.source_record_id,
        "redacted_summary": {"error_type": "provider_timeout"},
        "severity": "high",
    }
    first_handoff = build_runtime_incident_handoff(
        incident=common_incident,
        diagnosis=diagnosis("omitted-a"),
        attempted_queries=(),
    )
    second_handoff = build_runtime_incident_handoff(
        incident=common_incident,
        diagnosis=diagnosis("omitted-b"),
        attempted_queries=(),
    )
    assert first_handoff["agent_hypothesis"] == second_handoff["agent_hypothesis"]
    assert (
        first_handoff["source_diagnosis_fingerprint"]
        != second_handoff["source_diagnosis_fingerprint"]
    )
    first = persist_runtime_incident_handoff(
        session_factory,
        incident_id=incident.id,
        outcome_kind="diagnosed",
        handoff=first_handoff,
        created_at=datetime(2026, 7, 28, 9, 1, tzinfo=UTC),
    )
    second = persist_runtime_incident_handoff(
        session_factory,
        incident_id=incident.id,
        outcome_kind="diagnosed",
        handoff=second_handoff,
        created_at=datetime(2026, 7, 28, 9, 2, tzinfo=UTC),
    )
    assert first.diagnosis_revision == 1
    assert second.diagnosis_revision == 2


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


def test_durable_handoff_artifact_survives_restart_and_contains_copyable_evidence(tmp_path):
    from telegram_kol_research.models import RuntimeIncidentHandoffArtifact
    from telegram_kol_research.runtime_incident_handoff import (
        persist_runtime_incident_handoff,
    )

    database_path = tmp_path / "durable-handoff.db"
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
        feature_policy_version="runtime-incident-phase-8r-v1",
        prompt_version="runtime-agent-prompt-v8",
        tool_policy_version="runtime-agent-tools-v2",
    )
    handoff = build_runtime_incident_handoff(
        incident={
            "id": incident.id,
            "incident_type": incident.incident_type,
            "source_kind": incident.source_kind,
            "source_record_id": incident.source_record_id,
            "redacted_summary": {"error_type": "provider_timeout"},
            "severity": "high",
        },
        diagnosis=RuntimeAgentDiagnosis.from_mapping(
            {
                "incident_id": incident.id,
                "diagnosis_hypothesis": "Worker retries may have been exhausted.",
                "confidence": "low",
                "evidence_references": [f"incident:{incident.id}"],
                "missing_evidence": ["current provider health"],
                "recommended_playbook_name": None,
                "auto_handle_eligible": False,
                "codex_handoff_required": True,
                "remaining_risk": "The source job remains unresolved.",
                "expected_state": "The worker reaches a terminal state.",
                "observed_state": "The worker exhausted retries.",
                "classification": "code_defect",
                "affected_message_ids": [],
                "likely_code_paths": [
                    "src/telegram_kol_research/runtime_agent_worker.py"
                ],
                "likely_test_paths": ["tests/test_runtime_agent_worker.py"],
            },
            expected_incident_id=incident.id,
        ),
        attempted_queries=("investigate_worker_jobs",),
    )
    artifact = persist_runtime_incident_handoff(
        session_factory,
        incident_id=incident.id,
        outcome_kind="diagnosed",
        handoff=handoff,
        created_at=datetime(2026, 7, 28, 9, 1, tzinfo=UTC),
    )
    unchanged = persist_runtime_incident_handoff(
        session_factory,
        incident_id=incident.id,
        outcome_kind="diagnosed",
        handoff=handoff,
        created_at=datetime(2026, 7, 28, 9, 2, tzinfo=UTC),
    )
    changed_handoff = {
        **handoff,
        "incident": {**handoff["incident"], "severity": "critical"},
    }
    changed = persist_runtime_incident_handoff(
        session_factory,
        incident_id=incident.id,
        outcome_kind="diagnosed",
        handoff=changed_handoff,
        created_at=datetime(2026, 7, 28, 9, 3, tzinfo=UTC),
    )

    restarted_factory = create_session_factory(database_path)
    rebuilt = load_runtime_incident_handoff(
        restarted_factory, incident_id=incident.id
    )
    with restarted_factory() as session:
        stored = session.get(RuntimeIncidentHandoffArtifact, artifact.id)
        assert stored.diagnosis_revision == 1
        assert stored.status == "pending"
        assert len(stored.content_fingerprint) == 64
        assert f"incident_id={incident.id}" in stored.codex_prompt
        assert "AGENTS.md" in stored.codex_prompt
        assert "stable_handoff_id" in stored.evidence_document_json
        assert session.query(RuntimeIncidentHandoffArtifact).count() == 2
    assert unchanged.id == artifact.id
    assert changed.diagnosis_revision == 2
    assert rebuilt == changed_handoff
