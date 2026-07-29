from __future__ import annotations

from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    RuntimeAgentRecoveryAttempt,
    RuntimeIncident,
)
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


def _shadow_final(incident_id):
    payload = _final(incident_id)
    payload["final"]["recommended_playbook_name"] = (
        "refresh_read_only_exchange_snapshot"
    )
    payload["final"]["auto_handle_eligible"] = True
    return payload


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
        assert row.prompt_version == "runtime-agent-prompt-v6"
        assert "Provider retries may have been exhausted." in row.diagnosis_json
        assert row.evidence_refs_json == (
            f'["incident:{incident.id}","worker-job:42"]'
        )
        assert row.notification_status == "pending"
        assert row.notified_at is None


def test_worker_records_shadow_policy_result_but_never_executes_playbook(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.incident_type = "management_partial_failed"
        session.commit()
    turns = iter(
        (
            {
                "tool_call": {
                    "id": "call-1",
                    "name": "get_incident_summary",
                    "arguments": {"incident_id": incident.id},
                }
            },
            _shadow_final(incident.id),
        )
    )

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(
            enabled=True,
            shadow_playbooks=frozenset(
                {"refresh_read_only_exchange_snapshot"}
            ),
        ),
        tools=_registry([]),
        model_turn=lambda **kwargs: next(turns),
        now=NOW + timedelta(minutes=2),
    )

    assert result.status == "diagnosed"
    assert result.shadow_policy is not None
    assert result.shadow_policy["accepted"] is True
    assert result.shadow_policy["action_executed"] is False
    assert result.handoff["attempted_playbooks"] == [
        {
            "name": "refresh_read_only_exchange_snapshot",
            "policy_version": "runtime-shadow-policy-v1",
            "accepted": True,
            "refusal_reasons": [],
            "action_executed": False,
        }
    ]
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        stored = __import__("json").loads(row.diagnosis_json)
        assert row.playbook_name == "refresh_read_only_exchange_snapshot"
        assert row.recovery_status == "shadow_accepted"
        assert stored["shadow_playbook_policy"]["accepted"] is True
        assert stored["shadow_playbook_policy"]["action_executed"] is False


def test_worker_executes_one_allowlisted_low_risk_playbook_and_records_verification(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.incident_type = "management_partial_failed"
        session.commit()
    calls = []

    def provider(name):
        def call(*, incident_id):
            calls.append(name)
            if name == "compare_local_exchange":
                data = {
                    "incident_id": incident_id,
                    "comparison_kind": "local_vs_coherent_read_only_snapshot",
                    "applicable": True,
                    "coherent": True,
                    "complete": True,
                    "mismatches": 0,
                    "unknown": 0,
                }
            else:
                data = {
                    "incident_id": incident_id,
                    "coherent": True,
                    "complete": True,
                }
            return {
                "data": data,
                "evidence_refs": [
                    f"incident:{incident_id}",
                    f"projection:{name}",
                    "worker-job:42",
                ],
            }

        return call

    tools = RuntimeAgentToolRegistry(
        providers={
            "get_incident_summary": provider("get_incident_summary"),
            "get_exchange_snapshot": provider("get_exchange_snapshot"),
            "compare_local_exchange": provider("compare_local_exchange"),
        }
    )
    turns = iter(
        (
            {
                "tool_call": {
                    "id": "call-1",
                    "name": "get_incident_summary",
                    "arguments": {"incident_id": incident.id},
                }
            },
            _shadow_final(incident.id),
        )
    )

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(
            enabled=True,
            shadow_playbooks=frozenset(
                {"refresh_read_only_exchange_snapshot"}
            ),
            actions_enabled=True,
            action_playbooks=frozenset(
                {"refresh_read_only_exchange_snapshot"}
            ),
        ),
        tools=tools,
        action_handlers={
            "refresh_read_only_exchange_snapshot": lambda **kwargs: True
        },
        model_turn=lambda **kwargs: next(turns),
        now=NOW + timedelta(minutes=2),
    )

    assert result.status == "diagnosed"
    assert calls == [
        "get_incident_summary",
        "compare_local_exchange",
    ]
    assert result.recovery_policy["action_executed"] is True
    assert result.recovery_policy["verification_status"] == "verified"
    assert result.handoff["attempted_playbooks"][-1]["mode"] == "execute"
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        stored = __import__("json").loads(row.diagnosis_json)
        assert row.recovery_status == "action_verified"
        assert stored["recovery_playbook_policy"]["action_executed"] is True


def test_worker_records_policy_refusal_for_unsafe_nomination(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    final = _final(incident.id)
    final["final"]["recommended_playbook_name"] = "retry_business_instruction"
    final["final"]["auto_handle_eligible"] = True
    final["final"]["evidence_references"] = [f"incident:{incident.id}"]

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(
            enabled=True,
            shadow_playbooks=frozenset({"retry_business_instruction"}),
        ),
        tools=_registry([]),
        model_turn=lambda **kwargs: final,
        now=NOW + timedelta(minutes=2),
    )

    assert result.status == "diagnosed"
    assert result.shadow_policy["accepted"] is False
    assert result.shadow_policy["refusal_reasons"] == ["unknown_playbook"]
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        assert row.recovery_status == "shadow_refused"


def test_worker_records_execution_refusal_for_unknown_nomination(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    final = _final(incident.id)
    final["final"]["recommended_playbook_name"] = "retry_business_instruction"
    final["final"]["auto_handle_eligible"] = True
    final["final"]["evidence_references"] = [f"incident:{incident.id}"]

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(
            enabled=True,
            actions_enabled=True,
            action_playbooks=frozenset({"retry_business_instruction"}),
        ),
        tools=_registry([]),
        model_turn=lambda **kwargs: final,
        now=NOW + timedelta(minutes=2),
    )

    assert result.status == "diagnosed"
    assert result.recovery_policy["accepted"] is False
    assert result.recovery_policy["refusal_reasons"] == ["unknown_playbook"]
    assert result.recovery_policy["action_executed"] is False
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        assert row.recovery_status == "action_refused"


def test_worker_retries_instead_of_consuming_incident_on_global_action_contention(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    blocker = record_runtime_incident(
        session_factory,
        source_kind="strategy_management_batch",
        source_record_id="blocker",
        incident_type="management_partial_failed",
        severity="high",
        fingerprint="b" * 64,
        generation=1,
        redacted_summary='{"source_status":"partial_failed"}',
        occurred_at=NOW,
        feature_policy_version="runtime-incident-phase-6-v1",
        prompt_version="runtime-agent-prompt-v4",
        tool_policy_version="runtime-agent-tools-v2",
    )
    with session_factory() as session:
        session.get(RuntimeIncident, blocker.id).status = "diagnosed"
        session.add(
            RuntimeAgentRecoveryAttempt(
                incident_id=blocker.id,
                incident_fingerprint=blocker.fingerprint,
                playbook_name="refresh_read_only_exchange_snapshot",
                playbook_version=1,
                idempotency_key="runtime-incident:blocker:refresh:v1",
                status="reserved",
                attempt_number=1,
                policy_version="runtime-execution-policy-v1",
                started_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    incident = _record(session_factory, generation=2)
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.incident_type = "management_partial_failed"
        session.commit()
    final = _shadow_final(incident.id)
    final["final"]["evidence_references"] = [f"incident:{incident.id}"]

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(
            enabled=True,
            actions_enabled=True,
            action_playbooks=frozenset(
                {"refresh_read_only_exchange_snapshot"}
            ),
        ),
        tools=_registry([]),
        action_handlers={
            "refresh_read_only_exchange_snapshot": lambda **kwargs: True
        },
        model_turn=lambda **kwargs: final,
        now=NOW + timedelta(minutes=1),
    )

    assert result.status == "action_deferred"
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        assert row.status == "retry_pending"
        assert row.diagnosis_json is None
        assert row.agent_attempt_count == 0
        assert row.agent_next_attempt_at.replace(tzinfo=UTC) == (
            NOW + timedelta(minutes=3)
        )
        assert (
            session.get(RuntimeAgentRecoveryAttempt, 1).status == "reserved"
        )


def test_worker_accepts_operational_shadow_only_with_durable_non_writing_proof(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = record_runtime_incident(
        session_factory,
        source_kind="model_provider_job",
        source_record_id="job-safe",
        incident_type="provider_retry_exhausted",
        severity="high",
        fingerprint="e" * 64,
        generation=1,
        redacted_summary=(
            '{"business_write_owned":false,'
            '"error_type":"provider_timeout"}'
        ),
        occurred_at=NOW,
        feature_policy_version="runtime-incident-phase-5-v1",
        prompt_version="runtime-agent-prompt-v3",
        tool_policy_version="runtime-agent-tools-v2",
    )
    final = _final(incident.id)
    final["final"]["recommended_playbook_name"] = (
        "reschedule_non_writing_ai_job"
    )
    final["final"]["auto_handle_eligible"] = True
    final["final"]["evidence_references"] = [f"incident:{incident.id}"]

    result = run_runtime_agent_once(
        session_factory,
        config=RuntimeAgentWorkerConfig(
            enabled=True,
            shadow_playbooks=frozenset(
                {"reschedule_non_writing_ai_job"}
            ),
        ),
        tools=_registry([]),
        model_turn=lambda **kwargs: final,
        now=NOW + timedelta(minutes=2),
    )

    assert result.status == "diagnosed"
    assert result.shadow_policy["accepted"] is True
    assert result.shadow_policy["action_executed"] is False


def test_worker_reserves_final_turn_after_three_evidence_tools(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    observed_tool_schemas = []
    observed_messages = []
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
        observed_messages.append(kwargs["messages"])
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
    assert "Evidence collection is complete" in observed_messages[3][-1]["content"]
    assert "Return only the final JSON object" in (
        observed_messages[3][-1]["content"]
    )


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
        assert row.prompt_version == first.prompt_version
        assert "Known provider outage" in row.diagnosis_json
        assert f"incident:{second.id}" in row.evidence_refs_json
