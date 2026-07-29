from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RuntimeIncident
from telegram_kol_research.runtime_incidents import (
    MAX_DIAGNOSIS_JSON_LENGTH,
    MAX_REDACTED_SUMMARY_LENGTH,
    RuntimeIncidentBoundsError,
    claim_runtime_incident,
    list_claimable_runtime_incidents,
    record_runtime_incident,
    release_or_expire_runtime_incident_claim,
    transition_runtime_incident,
)


NOW = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)


def _record(session_factory, **overrides):
    values = {
        "source_kind": "worker_job",
        "source_record_id": "job-42",
        "incident_type": "worker_retry_exhausted",
        "severity": "high",
        "fingerprint": "a" * 64,
        "redacted_summary": '{"error_type":"provider_timeout"}',
        "occurred_at": NOW,
        "feature_policy_version": "incident-ledger-v1",
        "prompt_version": "none",
        "tool_policy_version": "none",
    }
    values.update(overrides)
    return record_runtime_incident(session_factory, **values)


def test_runtime_incident_table_has_additive_defaults_and_indexes(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    inspector = inspect(session_factory.kw["bind"])

    columns = {column["name"]: column for column in inspector.get_columns("runtime_incidents")}
    indexes = {index["name"] for index in inspector.get_indexes("runtime_incidents")}

    assert {
        "source_kind",
        "source_record_id",
        "incident_type",
        "severity",
        "fingerprint",
        "generation",
        "status",
        "repeat_count",
        "first_occurred_at",
        "last_occurred_at",
        "claim_token",
        "claimed_at",
        "claim_expires_at",
        "redacted_summary",
        "diagnosis_json",
        "evidence_refs_json",
        "notification_status",
        "notification_claim_token",
        "notification_claimed_at",
        "notified_at",
        "playbook_name",
        "recovery_status",
        "feature_policy_version",
        "prompt_version",
        "tool_policy_version",
        "created_at",
        "updated_at",
        "agent_attempt_count",
        "agent_next_attempt_at",
    } <= set(columns)
    assert columns["generation"]["default"] is not None
    assert columns["status"]["default"] is not None
    assert columns["repeat_count"]["default"] is not None
    assert columns["notification_status"]["default"] is not None
    assert columns["recovery_status"]["default"] is not None
    assert columns["agent_attempt_count"]["default"] is not None
    assert {
        "ix_runtime_incidents_claimable",
        "ix_runtime_incidents_source",
        "ix_runtime_incidents_fingerprint_generation",
    } <= indexes

    incident = _record(session_factory)
    assert incident.generation == 1
    assert incident.status == "pending"
    assert incident.repeat_count == 1
    assert incident.notification_status == "pending"
    assert incident.recovery_status == "not_requested"
    assert incident.agent_attempt_count == 0
    assert incident.agent_next_attempt_at is None


def test_record_same_fingerprint_deduplicates_and_increments_repeat_count(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first = _record(session_factory)
    second = _record(
        session_factory,
        source_record_id="job-43",
        occurred_at=NOW + timedelta(minutes=2),
    )

    assert second.id == first.id
    assert second.repeat_count == 2
    assert second.first_occurred_at.replace(tzinfo=UTC) == NOW
    assert second.last_occurred_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=2)
    with session_factory() as session:
        assert session.query(RuntimeIncident).count() == 1


def test_new_generation_is_distinct_for_same_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first = _record(session_factory)
    second = _record(session_factory, generation=2)

    assert second.id != first.id
    assert second.generation == 2


def test_deduplication_is_concurrent_and_does_not_regress_latest_state(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _record(
        session_factory,
        occurred_at=NOW + timedelta(minutes=10),
        severity="critical",
        redacted_summary='{"error_type":"latest"}',
    )

    def record_older(index: int):
        return _record(
            session_factory,
            source_record_id=f"job-{index}",
            occurred_at=NOW + timedelta(minutes=index),
            severity="low",
            redacted_summary=f'{{"error_type":"older_{index}"}}',
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(record_older, range(6)))

    with session_factory() as session:
        row = session.query(RuntimeIncident).one()
        assert row.repeat_count == 7
        assert row.first_occurred_at.replace(tzinfo=UTC) == NOW
        assert row.last_occurred_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=10)
        assert row.severity == "critical"
        assert row.redacted_summary == '{"error_type":"latest"}'


def test_record_rejects_unbounded_or_unredacted_fields(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with pytest.raises(RuntimeIncidentBoundsError, match="redacted_summary"):
        _record(
            session_factory,
            redacted_summary="x" * (MAX_REDACTED_SUMMARY_LENGTH + 1),
        )
    with pytest.raises(RuntimeIncidentBoundsError, match="diagnosis_json"):
        _record(
            session_factory,
            diagnosis_json="x" * (MAX_DIAGNOSIS_JSON_LENGTH + 1),
        )
    with pytest.raises(RuntimeIncidentBoundsError, match="sensitive"):
        _record(session_factory, redacted_summary='{"api_key":"secret"}')
    for evasion in (
        '{"error_type":"123456789:AAabcdefghijklmnopqrstuvwx"}',
        '{"error_type":"AKIAIOSFODNN7EXAMPLE"}',
        '{"error_type":"Ab3dEf6hIj9lMn2pQr5tUv8xYz1bCd4f"}',
        '{"error_type":"-----BEGIN PRIVATE KEY-----"}',
    ):
        with pytest.raises(RuntimeIncidentBoundsError, match="sensitive"):
            _record(session_factory, redacted_summary=evasion)
    safe = _record(
        session_factory,
        fingerprint="c" * 64,
        redacted_summary=(
            '{"component":"provider_auth","error_type":'
            '"authorization_provider_unavailable"}'
        ),
    )
    assert "authorization_provider_unavailable" in safe.redacted_summary
    for payload_name, payload in (
        ("diagnosis_json", '{"secret":"hunter2"}'),
        ("diagnosis_json", '{"client_secret":"hunter2"}'),
        ("diagnosis_json", '{"token":"hunter2"}'),
        ("diagnosis_json", '{"missing_evidence":[{"cookie":"short"}]}'),
        ("evidence_refs_json", '[{"credential":"short"}]'),
    ):
        with pytest.raises(RuntimeIncidentBoundsError, match="sensitive|stable references"):
            _record(session_factory, **{payload_name: payload})
    with pytest.raises(RuntimeIncidentBoundsError, match="source_record_id"):
        _record(session_factory, source_record_id=None)


def test_schema_rejects_unbounded_direct_writes(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory.kw["bind"].begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    """
                    INSERT INTO runtime_incidents (
                        source_kind, source_record_id, incident_type, severity,
                        fingerprint, generation, status, repeat_count,
                        first_occurred_at, last_occurred_at, redacted_summary,
                        notification_status, recovery_status,
                        feature_policy_version, prompt_version, tool_policy_version,
                        created_at, updated_at
                    ) VALUES (
                        :source_kind, 'source-1', 'worker_failed', 'high',
                        :fingerprint, 1, 'pending', 1,
                        :occurred_at, :occurred_at, '{}',
                        'pending', 'not_requested',
                        'policy-v1', 'none', 'none', :occurred_at, :occurred_at
                    )
                    """
                ),
                {
                    "source_kind": "x" * 65,
                    "fingerprint": "b" * 64,
                    "occurred_at": "2026-07-28 08:00:00",
                },
            )


def test_claim_is_compare_and_set_and_only_one_concurrent_worker_wins(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)

    def attempt_claim(index: int):
        return claim_runtime_incident(
            session_factory,
            incident_id=incident.id,
            claim_token=f"worker-{index}",
            claimed_at=NOW,
            claim_expires_at=NOW + timedelta(minutes=5),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(attempt_claim, range(8)))

    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].status == "claimed"
    assert winners[0].claim_token.startswith("worker-")


def test_claimable_list_and_stale_claim_release_require_matching_token(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    assert [row.id for row in list_claimable_runtime_incidents(session_factory, now=NOW)] == [
        incident.id
    ]

    claim = claim_runtime_incident(
        session_factory,
        incident_id=incident.id,
        claim_token="worker-a",
        claimed_at=NOW,
        claim_expires_at=NOW + timedelta(minutes=5),
    )
    assert claim is not None
    assert list_claimable_runtime_incidents(session_factory, now=NOW) == []
    assert not release_or_expire_runtime_incident_claim(
        session_factory,
        incident_id=incident.id,
        claim_token="worker-b",
        now=NOW + timedelta(minutes=6),
    )
    assert release_or_expire_runtime_incident_claim(
        session_factory,
        incident_id=incident.id,
        claim_token="worker-a",
        now=NOW + timedelta(minutes=6),
    )
    assert [row.id for row in list_claimable_runtime_incidents(
        session_factory, now=NOW + timedelta(minutes=6)
    )] == [incident.id]


def test_transition_requires_expected_status_and_claim_token(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim = claim_runtime_incident(
        session_factory,
        incident_id=incident.id,
        claim_token="worker-a",
        claimed_at=NOW,
        claim_expires_at=NOW + timedelta(minutes=5),
    )
    assert claim is not None

    assert not transition_runtime_incident(
        session_factory,
        incident_id=incident.id,
        from_status="claimed",
        to_status="diagnosed",
        claim_token="wrong",
        now=NOW,
    )
    assert transition_runtime_incident(
        session_factory,
        incident_id=incident.id,
        from_status="claimed",
        to_status="diagnosed",
        claim_token="worker-a",
        now=NOW,
        diagnosis_json='{"hypothesis":"provider unavailable"}',
        evidence_refs_json='["worker-job:42"]',
    )
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        assert row.status == "diagnosed"
        assert row.claim_token is None
        assert row.diagnosis_json == '{"hypothesis":"provider unavailable"}'


def test_expired_claim_cannot_commit_a_transition(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim = claim_runtime_incident(
        session_factory,
        incident_id=incident.id,
        claim_token="worker-a",
        claimed_at=NOW,
        claim_expires_at=NOW + timedelta(minutes=5),
    )
    assert claim is not None

    assert not transition_runtime_incident(
        session_factory,
        incident_id=incident.id,
        from_status="claimed",
        to_status="diagnosed",
        claim_token="worker-a",
        now=NOW + timedelta(minutes=5),
        diagnosis_json='{"hypothesis":"stale"}',
    )


@pytest.mark.parametrize(
    "shadow_policy",
    [
        {
            "mode": "execute",
            "policy_version": "runtime-shadow-policy-v1",
            "nominated_playbook": "rerun_production_audit",
            "playbook_version": 1,
            "accepted": True,
            "refusal_reasons": [],
            "verification_query": "get_service_audit_state",
            "would_execute": False,
            "action_executed": False,
        },
        {
            "mode": "shadow",
            "policy_version": "runtime-shadow-policy-v1",
            "nominated_playbook": "rerun_production_audit",
            "playbook_version": 1,
            "accepted": True,
            "refusal_reasons": [],
            "verification_query": "get_service_audit_state",
            "would_execute": True,
            "action_executed": False,
        },
        {
            "mode": "shadow",
            "policy_version": "runtime-shadow-policy-v1",
            "nominated_playbook": "rerun_production_audit",
            "playbook_version": 1,
            "accepted": True,
            "refusal_reasons": [],
            "verification_query": "get_service_audit_state",
            "would_execute": False,
            "action_executed": True,
        },
        {
            "mode": "shadow",
            "policy_version": "runtime-shadow-policy-v1",
            "nominated_playbook": "retry_business_instruction",
            "playbook_version": 1,
            "accepted": True,
            "refusal_reasons": [],
            "verification_query": "get_worker_state",
            "would_execute": False,
            "action_executed": False,
        },
    ],
)
def test_phase5_ledger_rejects_non_shadow_or_executed_policy(
    tmp_path, shadow_policy
):
    session_factory = create_session_factory(tmp_path / "research.db")
    incident = _record(session_factory)
    claim = claim_runtime_incident(
        session_factory,
        incident_id=incident.id,
        claim_token="worker-a",
        claimed_at=NOW,
        claim_expires_at=NOW + timedelta(minutes=5),
    )
    assert claim is not None

    diagnosis = json.dumps(
        {
            "hypothesis": "audit required",
            "confidence": "medium",
            "missing_evidence": [],
            "recommended_playbook": "rerun_production_audit",
            "auto_handle_eligible": True,
            "codex_handoff_required": False,
            "remaining_risk": "audit not rerun",
            "attempted_queries": [],
            "shadow_playbook_policy": shadow_policy,
        },
        sort_keys=True,
    )

    with pytest.raises(RuntimeIncidentBoundsError, match="shadow policy"):
        transition_runtime_incident(
            session_factory,
            incident_id=incident.id,
            from_status="claimed",
            to_status="diagnosed",
            claim_token="worker-a",
            now=NOW,
            diagnosis_json=diagnosis,
        )
