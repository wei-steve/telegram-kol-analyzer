from __future__ import annotations

import pytest

from telegram_kol_research.runtime_agent_tools import (
    READ_ONLY_RUNTIME_AGENT_TOOL_NAMES,
    RuntimeAgentToolError,
    RuntimeAgentToolRegistry,
)


def test_read_only_registry_exposes_only_closed_bounded_tools():
    calls = []

    def incident_summary(incident_id):
        calls.append(incident_id)
        return {
            "data": {"incident_id": incident_id, "status": "pending"},
            "evidence_refs": [f"incident:{incident_id}"],
        }

    registry = RuntimeAgentToolRegistry(
        providers={"get_incident_summary": incident_summary},
        max_output_bytes=512,
    )

    assert "run_shell" not in READ_ONLY_RUNTIME_AGENT_TOOL_NAMES
    assert "execute_sql" not in READ_ONLY_RUNTIME_AGENT_TOOL_NAMES
    result = registry.execute(
        "get_incident_summary",
        {"incident_id": 17},
        expected_incident_id=17,
    )

    assert calls == [17]
    assert result.data == {"incident_id": 17, "status": "pending"}
    assert result.evidence_refs == ("incident:17",)
    assert registry.allowed_tools == frozenset({"get_incident_summary"})
    assert [
        schema["function"]["name"] for schema in registry.tool_schemas()
    ] == ["get_incident_summary"]


def test_read_only_registry_fails_closed_for_missing_unknown_or_sensitive_output():
    registry = RuntimeAgentToolRegistry(
        providers={
            "get_incident_summary": lambda incident_id: {
                "data": {"api_key": "secret"},
                "evidence_refs": [f"incident:{incident_id}"],
            },
            "get_worker_state": lambda incident_id: {
                "data": {"entries": ["x" * 600]},
                "evidence_refs": [f"worker-job:{incident_id}"],
            },
        },
        max_output_bytes=256,
    )

    with pytest.raises(RuntimeAgentToolError, match="unknown tool"):
        registry.execute("run_shell", {"incident_id": 17}, expected_incident_id=17)
    with pytest.raises(RuntimeAgentToolError, match="not configured"):
        registry.execute(
            "get_journal_summary",
            {"incident_id": 17},
            expected_incident_id=17,
        )
    with pytest.raises(RuntimeAgentToolError, match="sensitive"):
        registry.execute(
            "get_incident_summary",
            {"incident_id": 17},
            expected_incident_id=17,
        )
    with pytest.raises(RuntimeAgentToolError, match="bounded"):
        registry.execute(
            "get_worker_state",
            {"incident_id": 17},
            expected_incident_id=17,
        )
