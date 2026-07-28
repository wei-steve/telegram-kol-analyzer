from __future__ import annotations

from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RuntimeIncident
from telegram_kol_research.runtime_agent_tools import RuntimeAgentToolRegistry
from telegram_kol_research.runtime_agent_worker import (
    RuntimeAgentWorkerConfig,
    RuntimeAgentWorkerResult,
    run_runtime_agent_loop,
    run_runtime_agent_once,
)
from telegram_kol_research.runtime_incidents import record_runtime_incident


NOW = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)


def _record(session_factory, *, generation=1):
    return record_runtime_incident(
        session_factory,
        source_kind="worker_job",
        source_record_id=f"job-{generation}",
        incident_type="worker_retry_exhausted",
        severity="high",
        fingerprint="d" * 64,
        generation=generation,
        redacted_summary='{"error_type":"provider_timeout"}',
        occurred_at=NOW + timedelta(minutes=generation),
        feature_policy_version="runtime-incident-phase-3-v1",
        prompt_version="runtime-agent-prompt-v1",
        tool_policy_version="runtime-agent-tools-v1",
    )


def _final(incident_id):
    return {
        "final": {
            "incident_id": incident_id,
            "diagnosis_hypothesis": "Provider retries may have been exhausted.",
            "confidence": "medium",
            "evidence_references": [
                f"incident:{incident_id}",
                "worker-job:42",
            ],
            "missing_evidence": ["provider recovery state"],
            "recommended_playbook_name": None,
            "auto_handle_eligible": False,
            "codex_handoff_required": True,
            "remaining_risk": "The source job remains unresolved.",
        }
    }


def _registry(call_count):
    return RuntimeAgentToolRegistry(
        providers={
            "get_incident_summary": lambda incident_id: (
                call_count.append(incident_id)
                or {
                    "data": {"incident_id": incident_id, "status": "claimed"},
                    "evidence_refs": [
                        f"incident:{incident_id}",
                        "worker-job:42",
                    ],
                }
            )
        }
    )


def test_worker_is_dormant_by_default_and_does_not_claim_or_call_model(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    model_calls = []

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(),
        tools=_registry([]),
        model_turn=lambda **kwargs: model_calls.append(kwargs),
        now=NOW + timedelta(minutes=2),
    )

    assert result.status == "disabled"
    assert model_calls == []
    with session_factory() as session:
        assert session.get(RuntimeIncident, incident.id).status == "pending"


def test_sidecar_loop_drains_ready_incidents_then_polls_when_idle():
    results = iter(
        (
            RuntimeAgentWorkerResult(status="diagnosed", incident_id=1),
            RuntimeAgentWorkerResult(status="idle"),
        )
    )
    observed = []
    sleeps = []

    iterations = run_runtime_agent_loop(
        run_once=lambda: next(results),
        on_result=observed.append,
        poll_seconds=3.0,
        sleep=sleeps.append,
        max_iterations=2,
    )

    assert iterations == 2
    assert [result.status for result in observed] == ["diagnosed", "idle"]
    assert sleeps == [3.0]


def test_sidecar_loop_polls_after_a_normal_claim_race():
    results = iter(
        (
            RuntimeAgentWorkerResult(status="claim_lost", incident_id=1),
            RuntimeAgentWorkerResult(status="idle"),
        )
    )
    sleeps = []

    iterations = run_runtime_agent_loop(
        run_once=lambda: next(results),
        poll_seconds=2.0,
        sleep=sleeps.append,
        max_iterations=2,
    )

    assert iterations == 2
    assert sleeps == [2.0, 2.0]


def test_worker_runs_bounded_tool_loop_and_commits_structured_diagnosis(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    tool_calls = []
    turns = iter(
        (
            {
                "tool_call": {
                    "id": "call-1",
                    "name": "get_incident_summary",
                    "arguments": {"incident_id": incident.id},
                }
            },
            _final(incident.id),
        )
    )

    observed_messages = []

    def model_turn(**kwargs):
        observed_messages.append(list(kwargs["messages"]))
        return next(turns)

    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.notification_status = "delivered"
        row.notified_at = NOW
        session.commit()

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(enabled=True),
        tools=_registry(tool_calls),
        model_turn=model_turn,
        now=NOW + timedelta(minutes=2),
    )

    assert result.status == "diagnosed"
    assert result.tool_steps == 1
    assert tool_calls == [incident.id]
    assert result.handoff is not None
    tool_assistant = observed_messages[1][-2]
    assert tool_assistant["role"] == "assistant"
    assert "tool_call" not in tool_assistant
    assert tool_assistant["tool_calls"][0]["function"] == {
        "name": "get_incident_summary",
        "arguments": f'{{"incident_id":{incident.id}}}',
    }
    assert observed_messages[1][-1]["role"] == "tool"
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        assert row.status == "diagnosed"
        assert "Provider retries may have been exhausted." in row.diagnosis_json
        assert row.evidence_refs_json == (
            f'["incident:{incident.id}","worker-job:42"]'
        )
        assert row.notification_status == "pending"
        assert row.notified_at is None


def test_worker_reserves_final_turn_after_three_evidence_tools(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    observed_tool_schemas = []
    tool_names = (
        "get_incident_summary",
        "get_worker_state",
        "get_service_audit_state",
    )
    turns = iter(
        [
            {
                "tool_call": {
                    "id": f"call-{index}",
                    "name": name,
                    "arguments": {"incident_id": incident.id},
                }
            }
            for index, name in enumerate(tool_names, start=1)
        ]
        + [_final(incident.id)]
    )
    registry = RuntimeAgentToolRegistry(
        providers={
            name: lambda incident_id, name=name: {
                "data": {"incident_id": incident_id, "projection": name},
                "evidence_refs": [
                    f"incident:{incident_id}",
                    "worker-job:42",
                ],
            }
            for name in tool_names
        }
    )

    def model_turn(**kwargs):
        observed_tool_schemas.append(kwargs["tool_schemas"])
        return next(turns)

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(enabled=True, max_tool_steps=4),
        tools=registry,
        model_turn=model_turn,
        now=NOW + timedelta(minutes=2),
    )

    assert result.status == "diagnosed"
    assert result.tool_steps == 3
    assert all(observed_tool_schemas[:3])
    assert observed_tool_schemas[3] == []


def test_worker_refuses_repeated_tool_without_reexecuting_provider(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    provider_calls = []
    repeated = {
        "tool_call": {
            "id": "call-1",
            "name": "get_incident_summary",
            "arguments": {"incident_id": incident.id},
        }
    }
    turns = iter((repeated, repeated, _final(incident.id)))

    observed_messages = []

    def model_turn(**kwargs):
        observed_messages.append(list(kwargs["messages"]))
        return next(turns)

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(enabled=True),
        tools=_registry(provider_calls),
        model_turn=model_turn,
        now=NOW + timedelta(minutes=2),
    )

    assert result.status == "diagnosed"
    assert provider_calls == [incident.id]
    assert result.refused_tool_calls == 1
    assert observed_messages[2][-2]["role"] == "assistant"
    assert "tool_calls" in observed_messages[2][-2]
    assert observed_messages[2][-1]["role"] == "tool"


def test_worker_safely_releases_claim_for_retry_after_provider_failure(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)

    def fail(**kwargs):
        raise TimeoutError("provider timeout")

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(enabled=True),
        tools=_registry([]),
        model_turn=fail,
        now=NOW + timedelta(minutes=2),
    )

    assert result.status == "retry_pending"
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        assert row.status == "retry_pending"
        assert row.claim_token is None
        assert row.diagnosis_json is None
        assert row.agent_attempt_count == 1
        assert row.agent_next_attempt_at.replace(tzinfo=UTC) == (
            NOW + timedelta(minutes=2, seconds=5)
        )

    immediate = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(enabled=True),
        tools=_registry([]),
        model_turn=fail,
        now=NOW + timedelta(minutes=2, seconds=4),
    )
    second = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(enabled=True),
        tools=_registry([]),
        model_turn=fail,
        now=NOW + timedelta(minutes=2, seconds=5),
    )
    third = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(enabled=True),
        tools=_registry([]),
        model_turn=fail,
        now=NOW + timedelta(minutes=2, seconds=15),
    )

    assert immediate.status == "idle"
    assert second.status == "retry_pending"
    assert third.status == "escalated"
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        assert row.agent_attempt_count == 3
        assert row.agent_next_attempt_at is None


def test_worker_enforces_prompt_budget_again_after_tool_output(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    model_calls = []
    registry = RuntimeAgentToolRegistry(
        providers={
            "get_incident_summary": lambda incident_id: {
                "data": {"entries": ["x" * 400, "y" * 400, "z" * 400]},
                "evidence_refs": [f"incident:{incident_id}"],
            }
        },
        max_output_bytes=2048,
    )

    def model_turn(**kwargs):
        model_calls.append(kwargs)
        return {
            "tool_call": {
                "id": "call-1",
                "name": "get_incident_summary",
                "arguments": {"incident_id": incident.id},
            }
        }

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(
            enabled=True,
            max_prompt_bytes=2200,
        ),
        tools=registry,
        model_turn=model_turn,
        now=NOW + timedelta(minutes=2),
    )

    assert result.status == "retry_pending"
    assert len(model_calls) == 1


def test_worker_escalates_before_model_if_crash_reclaim_exceeds_attempt_budget(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.agent_attempt_count = 3
        session.commit()
    model_calls = []

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(
            enabled=True,
            max_agent_attempts=3,
        ),
        tools=_registry([]),
        model_turn=lambda **kwargs: model_calls.append(kwargs),
        now=NOW + timedelta(minutes=2),
    )

    assert result.status == "escalated"
    assert model_calls == []
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        assert row.agent_attempt_count == 4
        assert row.status == "escalated"


def test_worker_reuses_same_fingerprint_diagnosis_without_model_call(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first = _record(session_factory, generation=1)
    with session_factory() as session:
        row = session.get(RuntimeIncident, first.id)
        row.status = "diagnosed"
        row.diagnosis_json = (
            '{"hypothesis":"Known provider outage","confidence":"high",'
            '"missing_evidence":[],"recommended_playbook":null,'
            '"auto_handle_eligible":false,"codex_handoff_required":true,'
            '"remaining_risk":"Job unresolved","attempted_queries":[]}'
        )
        row.evidence_refs_json = f'["incident:{first.id}"]'
        session.commit()
    second = _record(session_factory, generation=2)
    model_calls = []

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(enabled=True),
        tools=_registry([]),
        model_turn=lambda **kwargs: model_calls.append(kwargs),
        now=NOW + timedelta(minutes=4),
    )

    assert result.status == "reused"
    assert model_calls == []
    with session_factory() as session:
        row = session.get(RuntimeIncident, second.id)
        assert row.status == "diagnosed"
        assert "Known provider outage" in row.diagnosis_json
        assert f"incident:{second.id}" in row.evidence_refs_json
