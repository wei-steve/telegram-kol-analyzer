from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    RuntimeAgentRecoveryAttempt,
    RuntimeIncident,
)
from telegram_kol_research.runtime_agent_executor import (
    RuntimeAgentExecutorConfig,
    execute_low_risk_recovery,
)
import telegram_kol_research.runtime_agent_executor as executor_module
from telegram_kol_research.runtime_agent_policy import (
    evaluate_execution_playbook_nomination,
)
from telegram_kol_research.runtime_agent_tools import RuntimeAgentToolRegistry
from telegram_kol_research.runtime_incidents import (
    claim_runtime_incident,
    record_runtime_incident,
)


NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)


def _record(session_factory, *, fingerprint="f" * 64):
    return record_runtime_incident(
        session_factory,
        source_kind="strategy_management_batch",
        source_record_id="28",
        incident_type="management_partial_failed",
        severity="high",
        fingerprint=fingerprint,
        generation=1,
        redacted_summary='{"source_status":"partial_failed"}',
        occurred_at=NOW,
        feature_policy_version="runtime-incident-phase-6-v1",
        prompt_version="runtime-agent-prompt-v3",
        tool_policy_version="runtime-agent-tools-v2",
    )


def _claim(session_factory, incident, *, expires_at=None):
    token = f"claim-{incident.id}"
    claimed = claim_runtime_incident(
        session_factory,
        incident_id=incident.id,
        claim_token=token,
        claimed_at=NOW,
        claim_expires_at=expires_at or NOW + timedelta(minutes=5),
    )
    assert claimed is not None
    return token


def _tools(calls, *, coherent=True):
    def provider(name):
        def call(*, incident_id):
            calls.append((name, incident_id))
            if name == "compare_local_exchange":
                data = {
                    "incident_id": incident_id,
                    "comparison_kind": "local_vs_coherent_read_only_snapshot",
                    "applicable": True,
                    "coherent": coherent,
                    "complete": coherent,
                    "mismatches": 0 if coherent else 1,
                    "unknown": 0,
                }
            elif name == "get_service_audit_state":
                data = {
                    "incident_id": incident_id,
                    "available": coherent,
                    "audit_run_completed": coherent,
                    "complete": coherent,
                    "monitor_error": None,
                }
            else:
                data = {
                    "incident_id": incident_id,
                    "coherent": coherent,
                    "complete": coherent,
                }
            return {
                "data": data,
                "evidence_refs": [f"incident:{incident_id}", f"audit:{incident_id}"],
            }

        return call

    return RuntimeAgentToolRegistry(
        providers={
            "get_exchange_snapshot": provider("get_exchange_snapshot"),
            "compare_local_exchange": provider("compare_local_exchange"),
            "get_service_audit_state": provider("get_service_audit_state"),
            "get_incident_summary": provider("get_incident_summary"),
        }
    )


def _decision(incident, *, enabled=True):
    return evaluate_execution_playbook_nomination(
        incident={
            "id": incident.id,
            "incident_type": incident.incident_type,
            "redacted_summary": {"source_status": "partial_failed"},
        },
        nominated_playbook="refresh_read_only_exchange_snapshot",
        actions_enabled=enabled,
        enabled_playbooks=frozenset(
            {"refresh_read_only_exchange_snapshot"}
        ),
        evidence_references=(f"incident:{incident.id}",),
    )


def test_execution_policy_is_dormant_and_exact_allowlisted(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)

    dormant = _decision(incident, enabled=False)
    unknown = evaluate_execution_playbook_nomination(
        incident={
            "id": incident.id,
            "incident_type": incident.incident_type,
            "redacted_summary": {},
        },
        nominated_playbook="retry_business_instruction",
        actions_enabled=True,
        enabled_playbooks=frozenset({"retry_business_instruction"}),
        evidence_references=(f"incident:{incident.id}",),
    )

    assert dormant.accepted is False
    assert dormant.refusal_reasons == ("action_authority_disabled",)
    assert dormant.mode == "execute"
    assert dormant.would_execute is False
    assert dormant.executed is False
    assert unknown.refusal_reasons == ("unknown_playbook",)


def test_telegram_evidence_verification_requires_complete_exact_proof():
    complete = {
        "evidence_fetched": True,
        "evidence_available": True,
        "probe_complete": True,
        "endpoint_reachable": True,
        "bot_identity_available": True,
        "target_chat_available": True,
    }

    assert executor_module._verification_passed(
        "fetch_missing_telegram_evidence",
        complete,
        action_data={},
    )
    for field in (
        "probe_complete",
        "endpoint_reachable",
        "bot_identity_available",
        "target_chat_available",
    ):
        incomplete = dict(complete)
        incomplete.pop(field)
        assert not executor_module._verification_passed(
            "fetch_missing_telegram_evidence",
            incomplete,
            action_data={},
        )


def test_scoped_handler_refuses_inapplicable_incident_before_reservation(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    handler_calls = []

    class ScopedHandler:
        def is_applicable(self, **kwargs):
            handler_calls.append(("applicable", kwargs))
            return False

        def __call__(self, **kwargs):
            handler_calls.append(("execute", kwargs))
            return True

    for index, incident_type in enumerate(
        ("context_worker_exhausted", "provider_retry_exhausted"),
        start=1,
    ):
        incident = record_runtime_incident(
            session_factory,
            source_kind="worker",
            source_record_id=str(index),
            incident_type=incident_type,
            severity="medium",
            fingerprint=f"{index:064x}",
            generation=1,
            redacted_summary='{"business_write_owned":false}',
            occurred_at=NOW,
            feature_policy_version="runtime-incident-phase-6-v1",
            prompt_version="runtime-agent-prompt-v7",
            tool_policy_version="runtime-agent-tools-v2",
        )
        decision = evaluate_execution_playbook_nomination(
            incident={
                "id": incident.id,
                "incident_type": incident.incident_type,
                "redacted_summary": {"business_write_owned": False},
            },
            nominated_playbook="fetch_missing_telegram_evidence",
            actions_enabled=True,
            enabled_playbooks=frozenset(
                {"fetch_missing_telegram_evidence"}
            ),
            evidence_references=(f"incident:{incident.id}",),
        )
        claim_token = _claim(session_factory, incident)

        result = execute_low_risk_recovery(
            session_factory,
            incident_id=incident.id,
            expected_fingerprint=incident.fingerprint,
            expected_claim_token=claim_token,
            decision=decision,
            config=RuntimeAgentExecutorConfig(enabled=True),
            tools=RuntimeAgentToolRegistry(providers={}),
            action_handlers={
                "fetch_missing_telegram_evidence": ScopedHandler()
            },
            now=NOW,
        )

        assert result.status == "refused"
        assert result.refusal_reasons == ("executor_not_configured",)
        with session_factory() as session:
            row = session.get(RuntimeIncident, incident.id)
            assert row.recovery_status != "action_frozen"
            assert (
                session.query(RuntimeAgentRecoveryAttempt)
                .filter_by(incident_id=incident.id)
                .count()
                == 0
            )

    assert all(call[0] == "applicable" for call in handler_calls)


def test_executor_requires_current_fingerprint_before_reserving_action(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim_token = _claim(session_factory, incident)
    calls = []

    result = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint="0" * 64,
        expected_claim_token=claim_token,
        decision=_decision(incident),
        config=RuntimeAgentExecutorConfig(enabled=True),
        tools=_tools(calls),
        action_handlers={
            "refresh_read_only_exchange_snapshot": lambda **kwargs: True
        },
        now=NOW,
    )

    assert result.status == "fingerprint_mismatch"
    assert result.executed is False
    assert calls == []
    with session_factory() as session:
        assert session.query(RuntimeAgentRecoveryAttempt).count() == 0


def test_executor_reserves_idempotency_executes_once_and_verifies(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim_token = _claim(session_factory, incident)
    calls = []
    decision = _decision(incident)
    config = RuntimeAgentExecutorConfig(enabled=True)
    action_calls = []
    handlers = {
        "refresh_read_only_exchange_snapshot": lambda **kwargs: (
            action_calls.append(kwargs) or True
        )
    }

    first = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=decision,
        config=config,
        tools=_tools(calls),
        action_handlers=handlers,
        now=NOW,
    )
    replay = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=decision,
        config=config,
        tools=_tools(calls),
        action_handlers=handlers,
        now=NOW,
    )

    assert first.status == "verified"
    assert first.executed is True
    assert first.verified is True
    assert replay.status == "already_verified"
    assert replay.executed is False
    assert len(action_calls) == 1
    assert calls == [("compare_local_exchange", incident.id)]
    with session_factory() as session:
        attempts = session.query(RuntimeAgentRecoveryAttempt).all()
        assert len(attempts) == 1
        assert attempts[0].idempotency_key == decision.idempotency_key
        assert attempts[0].status == "verified"
        assert json.loads(attempts[0].action_result_json)["data"][
            "handler_completed"
        ] is True
        assert json.loads(attempts[0].verification_result_json)["data"][
            "complete"
        ] is True


def test_executor_opens_circuit_after_repeated_verification_mismatch(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    config = RuntimeAgentExecutorConfig(
        enabled=True,
        circuit_breaker_threshold=2,
    )
    calls = []
    incidents = [
        _record(session_factory, fingerprint=character * 64)
        for character in ("a", "b", "c")
    ]
    claim_tokens = [
        _claim(session_factory, incident) for incident in incidents
    ]

    first = execute_low_risk_recovery(
        session_factory,
        incident_id=incidents[0].id,
        expected_fingerprint=incidents[0].fingerprint,
        expected_claim_token=claim_tokens[0],
        decision=_decision(incidents[0]),
        config=config,
        tools=_tools(calls, coherent=False),
        action_handlers={
            "refresh_read_only_exchange_snapshot": lambda **kwargs: True
        },
        now=NOW,
    )
    second = execute_low_risk_recovery(
        session_factory,
        incident_id=incidents[1].id,
        expected_fingerprint=incidents[1].fingerprint,
        expected_claim_token=claim_tokens[1],
        decision=_decision(incidents[1]),
        config=config,
        tools=_tools(calls, coherent=False),
        action_handlers={
            "refresh_read_only_exchange_snapshot": lambda **kwargs: True
        },
        now=NOW,
    )
    blocked = execute_low_risk_recovery(
        session_factory,
        incident_id=incidents[2].id,
        expected_fingerprint=incidents[2].fingerprint,
        expected_claim_token=claim_tokens[2],
        decision=_decision(incidents[2]),
        config=config,
        tools=_tools(calls),
        action_handlers={
            "refresh_read_only_exchange_snapshot": lambda **kwargs: True
        },
        now=NOW,
    )

    assert first.status == "verification_failed"
    assert second.status == "verification_failed"
    assert blocked.status == "circuit_open"
    assert blocked.executed is False
    with session_factory() as session:
        assert (
            session.get(RuntimeIncident, incidents[1].id).recovery_status
            == "action_frozen"
        )


def test_executor_freezes_all_further_actions_for_failed_incident(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim_token = _claim(session_factory, incident)
    first_decision = _decision(incident)
    first = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=first_decision,
        config=RuntimeAgentExecutorConfig(
            enabled=True, circuit_breaker_threshold=5
        ),
        tools=_tools([], coherent=False),
        action_handlers={
            "refresh_read_only_exchange_snapshot": lambda **kwargs: True
        },
        now=NOW,
    )
    second_decision = evaluate_execution_playbook_nomination(
        incident={
            "id": incident.id,
            "incident_type": incident.incident_type,
            "redacted_summary": {"source_status": "partial_failed"},
        },
        nominated_playbook="rerun_production_audit",
        actions_enabled=True,
        enabled_playbooks=frozenset({"rerun_production_audit"}),
        evidence_references=(f"incident:{incident.id}",),
    )
    calls = []
    second = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=second_decision,
        config=RuntimeAgentExecutorConfig(
            enabled=True, circuit_breaker_threshold=5
        ),
        tools=_tools(calls),
        action_handlers={"rerun_production_audit": lambda **kwargs: True},
        now=NOW,
    )

    assert first.status == "verification_failed"
    assert second.status == "incident_action_frozen"
    assert second.executed is False
    assert calls == []


def test_executor_refuses_unfinished_idempotency_record_as_unknown_outcome(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim_token = _claim(session_factory, incident)
    decision = _decision(incident)
    with session_factory() as session:
        session.add(
            RuntimeAgentRecoveryAttempt(
                incident_id=incident.id,
                incident_fingerprint=incident.fingerprint,
                playbook_name=decision.nominated_playbook,
                playbook_version=decision.playbook_version,
                idempotency_key=decision.idempotency_key,
                status="reserved",
                attempt_number=1,
                policy_version=decision.policy_version,
                started_at=NOW - timedelta(minutes=5),
                created_at=NOW - timedelta(minutes=5),
                updated_at=NOW - timedelta(minutes=5),
            )
        )
        session.commit()

    result = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=decision,
        config=RuntimeAgentExecutorConfig(enabled=True),
        tools=_tools([]),
        action_handlers={
            "refresh_read_only_exchange_snapshot": lambda **kwargs: True
        },
        now=NOW,
    )

    assert result.status == "action_outcome_unknown"
    assert result.executed is False
    with session_factory() as session:
        assert session.get(RuntimeIncident, incident.id).recovery_status == (
            "action_frozen"
        )


def test_executor_runs_only_injected_operational_handler_after_non_write_proof(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = record_runtime_incident(
        session_factory,
        source_kind="model_provider_job",
        source_record_id="job-7",
        incident_type="provider_retry_exhausted",
        severity="high",
        fingerprint="e" * 64,
        generation=1,
        redacted_summary='{"business_write_owned":false}',
        occurred_at=NOW,
        feature_policy_version="runtime-incident-phase-6-v1",
        prompt_version="runtime-agent-prompt-v3",
        tool_policy_version="runtime-agent-tools-v2",
    )
    decision = evaluate_execution_playbook_nomination(
        incident={
            "id": incident.id,
            "incident_type": incident.incident_type,
            "redacted_summary": {"business_write_owned": False},
        },
        nominated_playbook="reschedule_non_writing_ai_job",
        actions_enabled=True,
        enabled_playbooks=frozenset({"reschedule_non_writing_ai_job"}),
        evidence_references=(f"incident:{incident.id}",),
    )
    claim_token = _claim(session_factory, incident)
    handler_calls = []
    tools = RuntimeAgentToolRegistry(
        providers={
            "get_worker_state": lambda *, incident_id: {
                "data": {
                    "incident_id": incident_id,
                    "job_rescheduled": True,
                    "business_write_owned": False,
                },
                "evidence_refs": [f"incident:{incident_id}"],
            }
        }
    )

    result = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=decision,
        config=RuntimeAgentExecutorConfig(enabled=True),
        tools=tools,
        action_handlers={
            "reschedule_non_writing_ai_job": lambda **kwargs: (
                handler_calls.append(kwargs) or True
            )
        },
        now=NOW,
    )

    assert result.status == "verified"
    assert handler_calls == [
        {
            "incident_id": incident.id,
            "idempotency_key": decision.idempotency_key,
            "expected_fingerprint": incident.fingerprint,
        }
    ]


def test_concurrent_replay_does_not_corrupt_active_attempt(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim_token = _claim(session_factory, incident)
    entered = Event()
    release = Event()

    def handler(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return True

    kwargs = {
        "incident_id": incident.id,
        "expected_fingerprint": incident.fingerprint,
        "expected_claim_token": claim_token,
        "decision": _decision(incident),
        "config": RuntimeAgentExecutorConfig(enabled=True),
        "tools": _tools([]),
        "action_handlers": {
            "refresh_read_only_exchange_snapshot": handler
        },
        "now": NOW,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(
            execute_low_risk_recovery, session_factory, **kwargs
        )
        assert entered.wait(timeout=5)
        replay = execute_low_risk_recovery(session_factory, **kwargs)
        release.set()
        first = first_future.result(timeout=5)

    assert replay.status == "action_in_progress"
    assert first.status == "verified"
    with session_factory() as session:
        assert session.query(RuntimeAgentRecoveryAttempt).one().status == (
            "verified"
        )
        assert session.get(RuntimeIncident, incident.id).recovery_status == (
            "action_verified"
        )


def test_stale_global_slot_is_frozen_and_released_for_next_action(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    blocker = _record(session_factory, fingerprint="b" * 64)
    with session_factory() as session:
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
    incident = _record(session_factory, fingerprint="c" * 64)
    claim_token = _claim(session_factory, incident)

    result = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=_decision(incident),
        config=RuntimeAgentExecutorConfig(enabled=True),
        tools=_tools([]),
        action_handlers={
            "refresh_read_only_exchange_snapshot": lambda **kwargs: True
        },
        now=NOW + timedelta(seconds=121),
    )

    assert result.status == "verified"
    with session_factory() as session:
        attempts = session.query(RuntimeAgentRecoveryAttempt).order_by(
            RuntimeAgentRecoveryAttempt.id
        ).all()
        assert [attempt.status for attempt in attempts] == [
            "action_outcome_unknown",
            "verified",
        ]
        assert session.get(RuntimeIncident, blocker.id).recovery_status == (
            "action_frozen"
        )


def test_executor_refuses_expired_or_lost_claim_before_action(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim_token = _claim(
        session_factory, incident, expires_at=NOW + timedelta(seconds=1)
    )
    calls = []

    result = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=_decision(incident),
        config=RuntimeAgentExecutorConfig(enabled=True),
        tools=_tools([]),
        action_handlers={
            "refresh_read_only_exchange_snapshot": lambda **kwargs: (
                calls.append(kwargs) or True
            )
        },
        now=NOW + timedelta(seconds=2),
    )

    assert result.status == "claim_lost"
    assert calls == []


def test_executor_freezes_unknown_outcome_if_claim_changes_during_action(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim_token = _claim(session_factory, incident)

    def reclaim(**kwargs):
        with session_factory() as session:
            row = session.get(RuntimeIncident, incident.id)
            row.claim_token = "replacement-worker"
            row.claim_expires_at = NOW + timedelta(minutes=10)
            session.commit()
        return True

    result = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=_decision(incident),
        config=RuntimeAgentExecutorConfig(enabled=True),
        tools=_tools([]),
        action_handlers={"refresh_read_only_exchange_snapshot": reclaim},
        now=NOW,
    )

    assert result.status == "action_outcome_unknown"
    assert result.executed is True
    with session_factory() as session:
        assert session.query(RuntimeAgentRecoveryAttempt).one().status == (
            "action_outcome_unknown"
        )
        assert session.get(RuntimeIncident, incident.id).recovery_status == (
            "action_frozen"
        )


def test_executor_detects_pure_wall_clock_lease_expiry_during_action(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim_token = _claim(
        session_factory,
        incident,
        expires_at=NOW + timedelta(seconds=1),
    )

    result = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=_decision(incident),
        config=RuntimeAgentExecutorConfig(enabled=True),
        tools=_tools([]),
        action_handlers={
            "refresh_read_only_exchange_snapshot": lambda **kwargs: True
        },
        now=NOW,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    assert result.status == "action_outcome_unknown"
    with session_factory() as session:
        assert session.query(RuntimeAgentRecoveryAttempt).one().status == (
            "action_outcome_unknown"
        )


def test_atomic_finalization_refuses_claim_reclaimed_after_last_read_check(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim_token = _claim(session_factory, incident)
    monkeypatch.setattr(
        executor_module,
        "_claim_is_current",
        lambda *args, **kwargs: True,
    )

    def verification(*, incident_id):
        with session_factory() as session:
            row = session.get(RuntimeIncident, incident_id)
            row.claim_token = "replacement-worker"
            row.claim_expires_at = NOW + timedelta(minutes=10)
            session.commit()
        return {
            "data": {
                "incident_id": incident_id,
                "comparison_kind": "local_vs_coherent_read_only_snapshot",
                "applicable": True,
                "coherent": True,
                "complete": True,
                "mismatches": 0,
                "unknown": 0,
            },
            "evidence_refs": [f"incident:{incident_id}"],
        }

    result = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=_decision(incident),
        config=RuntimeAgentExecutorConfig(enabled=True),
        tools=RuntimeAgentToolRegistry(
            providers={"compare_local_exchange": verification}
        ),
        action_handlers={
            "refresh_read_only_exchange_snapshot": lambda **kwargs: True
        },
        now=NOW,
    )

    assert result.status == "action_outcome_unknown"
    with session_factory() as session:
        assert session.query(RuntimeAgentRecoveryAttempt).one().status == (
            "action_outcome_unknown"
        )
        assert session.get(RuntimeIncident, incident.id).recovery_status == (
            "action_frozen"
        )


def test_executor_catches_unexpected_handler_exception_and_freezes(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim_token = _claim(session_factory, incident)

    def explode(**kwargs):
        raise KeyError("unexpected")

    result = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=_decision(incident),
        config=RuntimeAgentExecutorConfig(enabled=True),
        tools=_tools([]),
        action_handlers={"refresh_read_only_exchange_snapshot": explode},
        now=NOW,
    )

    assert result.status == "failed"
    with session_factory() as session:
        assert session.query(RuntimeAgentRecoveryAttempt).one().status == "failed"
        assert session.get(RuntimeIncident, incident.id).recovery_status == (
            "action_frozen"
        )


def test_verification_fails_closed_for_passive_durable_snapshot(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim_token = _claim(session_factory, incident)
    tools = RuntimeAgentToolRegistry(
        providers={
            "compare_local_exchange": lambda *, incident_id: {
                "data": {
                    "incident_id": incident_id,
                    "comparison_kind": "local_vs_durable_last_observed",
                    "applicable": True,
                    "mismatches": 0,
                    "unknown": 0,
                },
                "evidence_refs": [f"incident:{incident_id}"],
            }
        }
    )

    result = execute_low_risk_recovery(
        session_factory,
        incident_id=incident.id,
        expected_fingerprint=incident.fingerprint,
        expected_claim_token=claim_token,
        decision=_decision(incident),
        config=RuntimeAgentExecutorConfig(enabled=True),
        tools=tools,
        action_handlers={
            "refresh_read_only_exchange_snapshot": lambda **kwargs: True
        },
        now=NOW,
    )

    assert result.status == "verification_failed"
