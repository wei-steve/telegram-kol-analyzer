from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RuntimeIncident, RuntimeIncidentHandoffArtifact
from telegram_kol_research.runtime_agent_metrics import (
    RuntimeAgentBudgetExceeded,
    build_confirmed_incident_fixture,
    get_runtime_agent_metrics,
    fingerprint_runtime_agent_diagnosis,
    validate_runtime_agent_regression_manifest,
    record_diagnosis_review,
    reserve_model_tokens,
    settle_model_tokens,
)
from telegram_kol_research.web_queries import load_runtime_agent_metrics
from telegram_kol_research.runtime_agent_evaluation import (
    evaluate_runtime_agent_case,
    load_runtime_agent_corpus,
)


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _incident(session_factory, *, status="pending", created_at=NOW):
    fingerprint = f"{int(created_at.timestamp()):064x}"[-64:]
    with session_factory() as session:
        row = RuntimeIncident(
            source_kind="worker",
            source_record_id="fixture-1",
            incident_type="provider_retry_exhausted",
            severity="high",
            fingerprint=fingerprint,
            generation=1,
            status=status,
            repeat_count=1,
            agent_attempt_count=1,
            first_occurred_at=created_at,
            last_occurred_at=created_at,
            redacted_summary='{"error_type":"provider_timeout"}',
            diagnosis_json=(
                '{"diagnosis_hypothesis":"provider unavailable",'
                '"attempted_queries":["get_incident_summary"]}'
                if status == "diagnosed"
                else None
            ),
            evidence_refs_json='["incident:1"]' if status == "diagnosed" else None,
            notification_status="pending",
            recovery_status="not_requested",
            feature_policy_version="policy-v1",
            prompt_version="prompt-v8",
            tool_policy_version="tools-v2",
            created_at=created_at,
            updated_at=created_at + timedelta(seconds=8),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def _diagnosis_fingerprint(session_factory, incident_id):
    with session_factory() as session:
        return fingerprint_runtime_agent_diagnosis(
            session.get(RuntimeIncident, incident_id).diagnosis_json
        )


def _handoff(session_factory, incident_id, *, outcome_kind="diagnosed", created_at=NOW + timedelta(seconds=8)):
    with session_factory() as session:
        session.add(
            RuntimeIncidentHandoffArtifact(
                runtime_incident_id=incident_id,
                diagnosis_revision=1,
                outcome_kind=outcome_kind,
                content_json="{}",
                codex_prompt="review",
                evidence_document_json="{}",
                content_fingerprint=f"{incident_id:064x}"[-64:],
                status="pending",
                attempt_count=0,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        incident = session.get(RuntimeIncident, incident_id)
        incident.updated_at = created_at + timedelta(days=10)
        session.commit()


def test_token_reservation_is_atomic_idempotent_and_enforces_both_budgets(tmp_path):
    session_factory = create_session_factory(tmp_path / "metrics.db")
    incident = _incident(session_factory)

    first = reserve_model_tokens(
        session_factory,
        incident_id=incident.id,
        incident_fingerprint=incident.fingerprint,
        call_key="attempt-1-turn-0",
        reserved_tokens=600,
        per_incident_limit=1000,
        daily_limit=1200,
        now=NOW,
    )
    duplicate = reserve_model_tokens(
        session_factory,
        incident_id=incident.id,
        incident_fingerprint=incident.fingerprint,
        call_key="attempt-1-turn-0",
        reserved_tokens=600,
        per_incident_limit=1000,
        daily_limit=1200,
        now=NOW,
    )
    assert duplicate.id == first.id

    with pytest.raises(RuntimeAgentBudgetExceeded, match="incident"):
        reserve_model_tokens(
            session_factory,
            incident_id=incident.id,
            incident_fingerprint=incident.fingerprint,
            call_key="attempt-1-turn-1",
            reserved_tokens=500,
            per_incident_limit=1000,
            daily_limit=1200,
            now=NOW,
        )

    other = _incident(session_factory, created_at=NOW + timedelta(seconds=1))
    with pytest.raises(RuntimeAgentBudgetExceeded, match="daily"):
        reserve_model_tokens(
            session_factory,
            incident_id=other.id,
            incident_fingerprint=other.fingerprint,
            call_key="attempt-1-turn-0-other",
            reserved_tokens=700,
            per_incident_limit=1000,
            daily_limit=1200,
            now=NOW,
        )


def test_usage_settlement_is_exact_and_metrics_are_bounded(tmp_path):
    session_factory = create_session_factory(tmp_path / "metrics.db")
    incident = _incident(session_factory, status="diagnosed")
    _handoff(session_factory, incident.id)
    reservation = reserve_model_tokens(
        session_factory,
        incident_id=incident.id,
        incident_fingerprint=incident.fingerprint,
        call_key="attempt-1-turn-0",
        reserved_tokens=900,
        per_incident_limit=2000,
        daily_limit=4000,
        now=NOW,
    )
    settle_model_tokens(
        session_factory,
        reservation_id=reservation.id,
        prompt_tokens=400,
        completion_tokens=200,
        total_tokens=600,
        now=NOW + timedelta(seconds=2),
    )
    record_diagnosis_review(
        session_factory,
        incident_id=incident.id,
        verdict="confirmed",
        diagnosis_fingerprint=_diagnosis_fingerprint(session_factory, incident.id),
        fixture_case_id="provider-timeout-confirmed-001",
        now=NOW + timedelta(seconds=3),
    )

    metrics = get_runtime_agent_metrics(
        session_factory,
        since=NOW - timedelta(minutes=1),
        until=NOW + timedelta(minutes=1),
        max_incidents=100,
    )
    assert metrics["bounded"] is True
    assert metrics["incident_count"] == 1
    assert metrics["diagnosis_outcomes"] == {"diagnosed": 1}
    assert metrics["token_usage"] == {
        "model_calls": 1,
        "prompt_tokens": 400,
        "completion_tokens": 200,
        "total_tokens": 600,
        "reserved_tokens": 900,
    }
    assert metrics["average_latency_ms"] == 8000
    assert metrics["average_tool_steps"] == 1.0
    assert metrics["codex_hypothesis_accuracy"] == {
        "reviewed": 1,
        "confirmed": 1,
        "rate": 1.0,
    }


def test_confirmed_fixture_export_is_redacted_bounded_and_review_gated(tmp_path):
    session_factory = create_session_factory(tmp_path / "metrics.db")
    incident = _incident(session_factory, status="diagnosed")
    with pytest.raises(ValueError, match="confirmed review"):
        build_confirmed_incident_fixture(
            session_factory,
            incident_id=incident.id,
            case_id="confirmed-001",
        )

    record_diagnosis_review(
        session_factory,
        incident_id=incident.id,
        verdict="confirmed",
        diagnosis_fingerprint=_diagnosis_fingerprint(session_factory, incident.id),
        fixture_case_id="confirmed-001",
        now=NOW,
    )
    fixture = build_confirmed_incident_fixture(
        session_factory,
        incident_id=incident.id,
        case_id="confirmed-001",
    )
    assert fixture["schema_version"] == 1
    assert fixture["redacted"] is True
    assert fixture["reviewed_output"]["classification"] == "provider_retry_exhausted"
    assert "provider unavailable" not in str(fixture)
    assert len(str(fixture).encode()) < 16_384
    fixture_dir = tmp_path / "corpus"
    fixture_dir.mkdir()
    (fixture_dir / "confirmed-001.json").write_text(
        __import__("json").dumps(fixture), encoding="utf-8"
    )
    case = load_runtime_agent_corpus(fixture_dir)[0]
    assert evaluate_runtime_agent_case(case, case.reviewed_output).passed is True

    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.redacted_summary = '{"api_key":"must-not-export"}'
        session.commit()
    with pytest.raises(ValueError, match="not redacted"):
        build_confirmed_incident_fixture(
            session_factory,
            incident_id=incident.id,
            case_id="confirmed-001",
        )


def test_metrics_use_terminal_handoff_outcomes_not_mutable_incident_status(tmp_path):
    session_factory = create_session_factory(tmp_path / "outcomes.db")
    expected = ["reused", "provider_failed", "evidence_incomplete"]
    for index, outcome in enumerate(expected):
        incident = _incident(
            session_factory,
            status="escalated",
            created_at=NOW + timedelta(seconds=index),
        )
        _handoff(
            session_factory,
            incident.id,
            outcome_kind=outcome,
            created_at=NOW + timedelta(seconds=index + 5),
        )
    metrics = get_runtime_agent_metrics(
        session_factory,
        since=NOW - timedelta(minutes=1),
        until=NOW + timedelta(minutes=1),
    )
    assert metrics["diagnosis_outcomes"] == {outcome: 1 for outcome in expected}
    assert metrics["average_latency_ms"] == 5000


def test_metrics_fail_closed_when_scan_would_truncate(tmp_path):
    session_factory = create_session_factory(tmp_path / "metrics.db")
    _incident(session_factory)
    _incident(session_factory, created_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="truncated"):
        get_runtime_agent_metrics(
            session_factory,
            since=NOW - timedelta(minutes=1),
            until=NOW + timedelta(minutes=1),
            max_incidents=1,
        )


def test_web_projection_exposes_only_bounded_aggregate_metrics(tmp_path):
    session_factory = create_session_factory(tmp_path / "metrics.db")
    _incident(session_factory)
    projection = load_runtime_agent_metrics(
        session_factory,
        since=NOW - timedelta(minutes=1),
        until=NOW + timedelta(minutes=1),
    )
    assert projection["bounded"] is True
    assert projection["incident_count"] == 1
    assert "redacted_summary" not in str(projection)


def test_prompt_tool_policy_and_playbook_changes_require_reviewed_corpus_manifest():
    validate_runtime_agent_regression_manifest(
        project_root=__import__("pathlib").Path(__file__).parents[1],
        manifest_path="tests/fixtures/runtime_agent_regression_manifest.json",
    )
