from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    RawMessage,
)
from telegram_kol_research.runtime_agent_context_claim_recovery import (
    RuntimeAgentContextClaimRecovery,
    RuntimeAgentContextClaimRecoveryError,
)
from telegram_kol_research.runtime_incidents import record_runtime_incident


NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)


def _persist_stale_claim(
    session_factory,
    *,
    source_kind: str = "context_resolution_attempt",
    incident_type: str = "context_worker_exhausted",
    summary: dict[str, object] | None = None,
    claimed_at: datetime | None = None,
):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=200,
            text="context",
            posted_at=NOW - timedelta(hours=1),
        )
        session.add(raw)
        session.flush()
        attempt = ContextResolutionAttempt(
            raw_message_id=raw.id,
            context_fingerprint="sha256:context",
            state_fingerprint="sha256:state",
            model="test-model",
            prompt_versions_json="{}",
            request_summary_json="{}",
            decision_json='{"decision":"unresolved"}',
            status="running",
            error_class="TimeoutError",
            reanalysis_triggers_json='["message_edited"]',
            attempts=2,
            next_attempt_at=None,
            trigger_event_json='{"event_type":"message_edited"}',
            claim_token="stale-worker-claim",
            claimed_at=claimed_at or NOW - timedelta(minutes=6),
            last_error="TimeoutError",
            created_at=NOW - timedelta(hours=1),
            updated_at=NOW - timedelta(minutes=6),
        )
        session.add(attempt)
        session.commit()
        raw_id = raw.id
        attempt_id = attempt.id
    incident = record_runtime_incident(
        session_factory,
        source_kind=source_kind,
        source_record_id=str(attempt_id),
        incident_type=incident_type,
        severity="high",
        fingerprint="c" * 64,
        generation=1,
        redacted_summary=json.dumps(
            summary
            if summary is not None
            else {
                "claim_status": "stale",
                "claim_side_effect_class": "none",
            },
            sort_keys=True,
        ),
        occurred_at=NOW - timedelta(minutes=1),
        feature_policy_version="runtime-incident-phase-6-v1",
        prompt_version="runtime-agent-prompt-v7",
        tool_policy_version="runtime-agent-tools-v2",
    )
    return raw_id, attempt_id, incident


def _recovery(session_factory):
    return RuntimeAgentContextClaimRecovery(
        session_factory,
        clock=lambda: NOW,
    )


def test_recovery_returns_exact_stale_context_claim_to_safe_queue(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _, attempt_id, incident = _persist_stale_claim(session_factory)
    recovery = _recovery(session_factory)

    assert recovery.recover(
        incident_id=incident.id,
        idempotency_key=(
            f"runtime-incident:{incident.id}:"
            "recover-stale-side-effect-free-claim:v1"
        ),
        expected_fingerprint=incident.fingerprint,
    )

    with session_factory() as session:
        attempt = session.get(ContextResolutionAttempt, attempt_id)
        assert attempt.status == "pending_reanalysis"
        assert attempt.claim_token is None
        assert attempt.claimed_at is None
        assert attempt.next_attempt_at == NOW.replace(tzinfo=None)
        assert attempt.attempts == 2
        assert attempt.decision_json == '{"decision":"unresolved"}'
        assert attempt.last_error == "TimeoutError"
    assert recovery.consume_verification(incident_id=incident.id) == {
        "applicable": True,
        "safe_queue_restored": True,
        "claim_status": "pending",
        "business_write_owned": False,
        "context_attempt_id": attempt_id,
    }
    assert recovery.consume_verification(incident_id=incident.id) is None


@pytest.mark.parametrize(
    ("overrides", "fingerprint"),
    [
        (
            {"claimed_at": NOW - timedelta(minutes=4, seconds=59)},
            "c" * 64,
        ),
        (
            {"source_kind": "worker_job"},
            "c" * 64,
        ),
        (
            {"incident_type": "provider_retry_exhausted"},
            "c" * 64,
        ),
        (
            {
                "summary": {
                    "claim_status": "stale",
                    "claim_side_effect_class": "unknown",
                }
            },
            "c" * 64,
        ),
        ({}, "d" * 64),
    ],
)
def test_recovery_refuses_incomplete_or_mismatched_proof(
    tmp_path,
    overrides,
    fingerprint,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _, attempt_id, incident = _persist_stale_claim(
        session_factory,
        **overrides,
    )
    recovery = _recovery(session_factory)

    with pytest.raises(RuntimeAgentContextClaimRecoveryError):
        recovery.recover(
            incident_id=incident.id,
            idempotency_key="bounded-idempotency-key",
            expected_fingerprint=fingerprint,
        )

    with session_factory() as session:
        attempt = session.get(ContextResolutionAttempt, attempt_id)
        assert attempt.status == "running"
        assert attempt.claim_token == "stale-worker-claim"
    assert recovery.consume_verification(incident_id=incident.id) is None


def test_recovery_refuses_terminal_business_instruction(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, attempt_id, incident = _persist_stale_claim(session_factory)
    with session_factory() as session:
        session.execute(
            text(
                """
                INSERT INTO message_instruction_items (
                    raw_message_id, signal_candidate_id, sequence,
                    instruction_kind, idempotency_key, status,
                    visibility_retry_attempts, summary_notification_status,
                    created_at, updated_at
                ) VALUES (
                    :raw_id, 999, 1, 'management', :key, 'submitted',
                    0, 'pending', :now, :now
                )
                """
            ),
            {
                "raw_id": raw_id,
                "key": "i" * 64,
                "now": NOW.replace(tzinfo=None),
            },
        )
        session.commit()
    recovery = _recovery(session_factory)

    with pytest.raises(RuntimeAgentContextClaimRecoveryError):
        recovery.recover(
            incident_id=incident.id,
            idempotency_key="bounded-idempotency-key",
            expected_fingerprint=incident.fingerprint,
        )

    with session_factory() as session:
        assert (
            session.get(ContextResolutionAttempt, attempt_id).status
            == "running"
        )


def test_recovery_is_single_use_after_compare_and_set(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _, _, incident = _persist_stale_claim(session_factory)
    recovery = _recovery(session_factory)
    kwargs = {
        "incident_id": incident.id,
        "idempotency_key": "bounded-idempotency-key",
        "expected_fingerprint": incident.fingerprint,
    }

    assert recovery.recover(**kwargs) is True
    with pytest.raises(RuntimeAgentContextClaimRecoveryError):
        recovery.recover(**kwargs)


def test_recovery_bounds_unconsumed_proofs(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    recovery = _recovery(session_factory)
    incidents = []
    for index in range(33):
        _, _, incident = _persist_stale_claim(session_factory)
        with session_factory() as session:
            row = session.get(type(incident), incident.id)
            row.fingerprint = f"{index:064x}"
            session.commit()
            session.refresh(row)
            incident = row
        assert recovery.recover(
            incident_id=incident.id,
            idempotency_key=f"bounded-{index}",
            expected_fingerprint=incident.fingerprint,
        )
        incidents.append(incident)

    assert recovery.consume_verification(
        incident_id=incidents[0].id
    ) is None
    assert recovery.consume_verification(
        incident_id=incidents[-1].id
    )["safe_queue_restored"] is True
