from __future__ import annotations

import pytest

from telegram_kol_research.runtime_agent_tools import (
    READ_ONLY_RUNTIME_AGENT_TOOL_NAMES,
    RuntimeAgentToolError,
    RuntimeAgentToolRegistry,
    build_local_exchange_comparison,
    build_prior_attempts_summary,
    build_protection_summary,
    build_worker_history_summary,
    build_broker_tool_provider,
)
from telegram_kol_research.runtime_agent_investigation_broker import (
    InvestigationBroker,
)
from datetime import UTC, datetime


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


def test_phase4_evidence_helpers_build_coherent_bounded_projections():
    comparison = build_local_exchange_comparison(
        [
            {
                "record_id": 1,
                "local_status": "partial_failed",
                "exchange_status": "filled",
                "exchange_size": "0",
            },
            {
                "record_id": 2,
                "local_status": "pending",
                "exchange_status": None,
                "exchange_size": None,
            },
        ]
    )
    assert comparison == {
        "total": 2,
        "comparable": 1,
        "matches": 0,
        "mismatches": 1,
        "unknown": 1,
        "rows": [
            {
                "record_id": 1,
                "local_status": "partial_failed",
                "exchange_status": "filled",
                "exchange_size": "0",
                "comparison": "mismatch",
            },
            {
                "record_id": 2,
                "local_status": "pending",
                "exchange_status": None,
                "exchange_size": None,
                "comparison": "unknown",
            },
        ],
    }

    worker = build_worker_history_summary(
        [
            {"record_id": 9, "status": "exhausted", "attempts": 3},
            {"record_id": 8, "status": "retry_pending", "attempts": 2},
        ]
    )
    assert worker["total"] == 2
    assert worker["status_counts"] == {"exhausted": 1, "retry_pending": 1}
    assert worker["attempts_total"] == 5

    prior = build_prior_attempts_summary(
        [
            {
                "incident_id": 4,
                "generation": 2,
                "status": "diagnosed",
                "recovery_status": "not_requested",
                "agent_attempt_count": 1,
            }
        ]
    )
    assert prior["diagnosed"] == 1
    assert prior["agent_attempts_total"] == 1


def test_phase4_protection_summary_exposes_counts_not_raw_order_evidence():
    summary = build_protection_summary(
        {
            "reason_code": "protection_recovery_required",
            "missing_stop_loss": True,
            "take_profit_order_ids": ["tp-sensitive-1", "tp-sensitive-2"],
            "stop_loss_order_ids": [],
            "position_size": "2.5",
        }
    )

    assert summary == {
        "reason_code": "protection_recovery_required",
        "missing_stop_loss": True,
        "missing_take_profit": False,
        "take_profit_count": 2,
        "stop_loss_count": 0,
        "position_size_present": True,
    }
    assert "tp-sensitive" not in str(summary)

    unavailable = build_protection_summary({})
    assert unavailable["missing_stop_loss"] is None
    assert unavailable["missing_take_profit"] is None


def test_broker_tool_provider_uses_closed_incident_bound_request():
    requests = []
    broker = InvestigationBroker(
        providers={
            "processing_timeline": lambda request: (
                requests.append(request)
                or {
                    "data": {"events": 2},
                    "evidence_refs": [f"timeline:{request.incident_id}"],
                }
            )
        },
        incident_exists=lambda incident_id: incident_id == 17,
        audit_recorder=lambda record: None,
        clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
    )

    provider = build_broker_tool_provider(
        broker,
        evidence_kind="processing_timeline",
        query="message-operation-v1",
    )
    assert provider(incident_id=17) == {
        "data": {"events": 2},
        "evidence_refs": ["timeline:17"],
    }
    assert requests[0].incident_id == 17
    assert requests[0].evidence_kind == "processing_timeline"
    assert requests[0].query == "message-operation-v1"
