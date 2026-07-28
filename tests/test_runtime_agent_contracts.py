from __future__ import annotations

import pytest

from telegram_kol_research.runtime_agent_contracts import (
    RuntimeAgentContractError,
    RuntimeAgentDiagnosis,
    RuntimeAgentToolCall,
)


DIAGNOSIS = {
    "incident_id": 17,
    "diagnosis_hypothesis": "The provider retry budget was exhausted.",
    "confidence": "medium",
    "evidence_references": ["incident:17", "worker-job:42"],
    "missing_evidence": ["provider health after the final retry"],
    "recommended_playbook_name": None,
    "auto_handle_eligible": False,
    "codex_handoff_required": True,
    "remaining_risk": "The failed job remains unresolved.",
}


def test_closed_diagnosis_contract_round_trips_to_ledger_fields():
    diagnosis = RuntimeAgentDiagnosis.from_mapping(DIAGNOSIS, expected_incident_id=17)

    assert diagnosis.incident_id == 17
    assert diagnosis.confidence == "medium"
    assert diagnosis.evidence_references == ("incident:17", "worker-job:42")
    assert diagnosis.to_ledger_mapping() == {
        "hypothesis": "The provider retry budget was exhausted.",
        "confidence": "medium",
        "missing_evidence": ["provider health after the final retry"],
        "recommended_playbook": None,
        "auto_handle_eligible": False,
        "codex_handoff_required": True,
        "remaining_risk": "The failed job remains unresolved.",
        "attempted_queries": [],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("actions", [{"name": "run_shell"}]),
        ("incident_id", 18),
        ("confidence", "certain"),
        ("evidence_references", ["not a stable reference"]),
        ("diagnosis_hypothesis", "x" * 513),
    ),
)
def test_closed_diagnosis_contract_rejects_extra_actions_and_invalid_values(
    field, value
):
    payload = dict(DIAGNOSIS)
    payload[field] = value

    with pytest.raises(RuntimeAgentContractError):
        RuntimeAgentDiagnosis.from_mapping(payload, expected_incident_id=17)


def test_tool_call_contract_rejects_unknown_tools_and_extra_arguments():
    allowed = frozenset({"get_incident_summary"})
    call = RuntimeAgentToolCall.from_mapping(
        {
            "id": "call-1",
            "name": "get_incident_summary",
            "arguments": {"incident_id": 17},
        },
        allowed_tools=allowed,
        expected_incident_id=17,
    )
    assert call.name == "get_incident_summary"

    with pytest.raises(RuntimeAgentContractError, match="unknown tool"):
        RuntimeAgentToolCall.from_mapping(
            {
                "id": "call-2",
                "name": "run_shell",
                "arguments": {"incident_id": 17},
            },
            allowed_tools=allowed,
            expected_incident_id=17,
        )
    with pytest.raises(RuntimeAgentContractError, match="arguments"):
        RuntimeAgentToolCall.from_mapping(
            {
                "id": "call-3",
                "name": "get_incident_summary",
                "arguments": {"incident_id": 17, "sql": "select *"},
            },
            allowed_tools=allowed,
            expected_incident_id=17,
        )
