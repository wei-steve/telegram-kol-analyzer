from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from telegram_kol_research.production_monitor_contract import (
    build_monitor_projection,
)
from telegram_kol_research.production_monitor_notifications import (
    MonitorAcceptance,
    MonitorIntakeError,
    format_fixed_fallback,
    parse_monitor_acceptance,
    request_monitor_acceptance,
    request_monitor_fallback,
    recheck_due_monitor_notifications_persisted,
    route_monitor_incident,
    route_monitor_incident_persisted,
)
from telegram_kol_research.production_monitor_state import (
    IncidentAcceptanceState,
    ProductionMonitorState,
    ProductionMonitorStateStore,
)
from telegram_kol_research.production_monitor_policy import CandidateObservation
from telegram_kol_research.production_monitor_sentinel import (
    SentinelObservation,
    run_production_monitor_sentinel,
)


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _projection():
    return build_monitor_projection(
        {
            "checked_at": NOW,
            "observation_generation": 7,
            "anomaly_fingerprint": "a" * 64,
            "execution_status": "COMPLETED",
            "observed_health": "UNHEALTHY",
            "reason_codes": ["audit_abnormal"],
            "adapter_failures": [],
            "fallback_reason": None,
        }
    )


def _projection_at(
    *, checked_at: datetime, generation: int, anomaly_fingerprint: str = "a" * 64
):
    return build_monitor_projection(
        {
            "checked_at": checked_at,
            "observation_generation": generation,
            "anomaly_fingerprint": anomaly_fingerprint,
            "execution_status": "COMPLETED",
            "observed_health": "UNHEALTHY",
            "reason_codes": ["audit_abnormal"],
            "adapter_failures": [],
            "fallback_reason": None,
        }
    )


def _acceptance(**overrides):
    values = {
        "submission_id": _projection()["submission_id"],
        "accepted_at": NOW,
        "notification_status": "pending",
        "notification_claimed_at": None,
        "notification_claim_expires_at": None,
        "notification_failed_at": None,
        "agent_status": "pending",
    }
    values.update(overrides)
    return MonitorAcceptance(**values)


def test_lost_response_recheck_records_semantic_acceptance_without_fallback():
    calls = []

    def submit(_projection):
        calls.append("submit_committed")
        raise MonitorIntakeError("transport_unavailable")

    def recheck(_projection):
        calls.append("recheck")
        return _acceptance()

    delivered = []
    outcome = route_monitor_incident(
        projection=_projection(),
        previous_state=ProductionMonitorState(),
        now=NOW,
        submit=submit,
        recheck=recheck,
        deliver_fallback=delivered.append,
    )

    assert calls == ["submit_committed", "recheck"]
    assert outcome.accepted is True
    assert outcome.fallback_status is None
    assert len(outcome.state.incident_acceptances) == 1
    assert delivered == []


def test_monitor_acceptance_response_is_strict_and_closed():
    payload = {
        "accepted": True,
        "submission_id": _projection()["submission_id"],
        "accepted_at": NOW.isoformat(),
        "notification_status": "pending",
        "notification_claimed_at": None,
        "notification_claim_expires_at": None,
        "notification_failed_at": None,
        "agent_status": "pending",
    }

    acceptance = parse_monitor_acceptance(payload, now=NOW)
    assert acceptance == _acceptance()

    with pytest.raises(MonitorIntakeError, match="schema_refused"):
        parse_monitor_acceptance({**payload, "incident_payload": "open"}, now=NOW)
    with pytest.raises(MonitorIntakeError, match="schema_refused"):
        parse_monitor_acceptance({**payload, "submission_id": "not-a-hash"}, now=NOW)


def test_loopback_request_is_exact_bounded_and_parses_acceptance():
    calls = []

    class Response:
        status_code = 200
        content = b"{}"

        def json(self):
            return {
                "accepted": True,
                "submission_id": _projection()["submission_id"],
                "accepted_at": NOW.isoformat(),
                "notification_status": "pending",
                "notification_claimed_at": None,
                "notification_claim_expires_at": None,
                "notification_failed_at": None,
                "agent_status": "pending",
            }

    acceptance = request_monitor_acceptance(
        url="http://127.0.0.1:8000/api/runtime-incidents/monitor-capture",
        token="m" * 43,
        projection=_projection(),
        now=NOW,
        request=lambda **kwargs: calls.append(kwargs) or Response(),
    )

    assert acceptance == _acceptance()
    assert calls[0]["headers"] == {"x-monitor-capture-token": "m" * 43}
    assert calls[0]["json"]["submission_id"] == _projection()["submission_id"]

    with pytest.raises(ValueError, match="exact loopback"):
        request_monitor_acceptance(
            url="https://example.com/incidents",
            token="m" * 43,
            projection=_projection(),
            now=NOW,
            request=lambda **kwargs: Response(),
        )


def test_fixed_fallback_uses_authenticated_loopback_proxy_only():
    calls = []
    message = format_fixed_fallback(
        reason="incident_intake_unavailable",
        component="runtime_incident_intake",
        observed_at=NOW,
        deadline_at=NOW + timedelta(minutes=10),
        rechecked_at=NOW + timedelta(minutes=11),
    )

    class Response:
        status_code = 200
        content = b'{"delivered":true}'

        @staticmethod
        def json():
            return {"delivered": True}

    request_monitor_fallback(
        url="http://127.0.0.1:8000/api/runtime-incidents/monitor-fallback",
        token="m" * 43,
        message=message,
        request=lambda **kwargs: calls.append(kwargs) or Response(),
    )

    assert calls == [
        {
            "url": (
                "http://127.0.0.1:8000/api/runtime-incidents/monitor-fallback"
            ),
            "headers": {"x-monitor-capture-token": "m" * 43},
            "json": {
                "delivery_id": hashlib.sha256(
                    "\0".join(
                        (
                            "incident_intake_unavailable",
                            "runtime_incident_intake",
                            NOW.isoformat(),
                            (NOW + timedelta(minutes=10)).isoformat(),
                        )
                    ).encode()
                ).hexdigest(),
                "reason": "incident_intake_unavailable",
                "component": "runtime_incident_intake",
                "observed_at": NOW.isoformat(),
                "deadline_at": (NOW + timedelta(minutes=10)).isoformat(),
                "rechecked_at": (NOW + timedelta(minutes=11)).isoformat(),
            },
        }
    ]
    with pytest.raises(ValueError, match="fixed fallback"):
        request_monitor_fallback(
            url=(
                "http://127.0.0.1:8000/api/runtime-incidents/monitor-fallback"
            ),
            token="m" * 43,
            message="arbitrary incident or secret payload",
            request=lambda **_kwargs: Response(),
        )
    with pytest.raises(ValueError, match="loopback"):
        request_monitor_fallback(
            url="https://api.telegram.org/bot-secret/sendMessage",
            token="m" * 43,
            message=message,
            request=lambda **_kwargs: Response(),
        )
def test_later_acceptance_clears_pending_intake_fallback_state():
    unavailable = lambda _projection: (_ for _ in ()).throw(
        MonitorIntakeError("transport_unavailable")
    )
    tracking = route_monitor_incident(
        projection=_projection(),
        previous_state=ProductionMonitorState(),
        now=NOW,
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=lambda _message: None,
    )
    assert tracking.state.fallback is not None

    accepted = route_monitor_incident(
        projection=_projection(),
        previous_state=tracking.state,
        now=NOW + timedelta(minutes=1),
        submit=lambda _projection: _acceptance(),
        recheck=lambda _projection: _acceptance(),
        deliver_fallback=lambda _message: None,
    )

    assert accepted.accepted is True
    assert accepted.state.fallback is None


@pytest.mark.parametrize("error_code", ["schema_refused", "transport_unavailable"])
def test_intake_failure_requires_expired_deadline_and_failed_recheck(error_code):
    delivered = []

    def unavailable(_projection):
        raise MonitorIntakeError(error_code)

    first_failure = route_monitor_incident(
        projection=_projection(),
        previous_state=ProductionMonitorState(),
        now=NOW,
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=delivered.append,
    )
    assert first_failure.fallback_status is None
    assert first_failure.state.fallback.attempts == 0

    before = route_monitor_incident(
        projection=_projection(),
        previous_state=first_failure.state,
        now=NOW + timedelta(minutes=9, seconds=59),
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=delivered.append,
    )
    assert before.fallback_status is None
    assert delivered == []

    expired = route_monitor_incident(
        projection=_projection(),
        previous_state=before.state,
        now=NOW + timedelta(minutes=10),
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=delivered.append,
    )
    assert expired.accepted is False
    assert expired.fallback_reason == "incident_intake_unavailable"
    assert expired.fallback_status == "DELIVERED"
    assert len(delivered) == 1


@pytest.mark.parametrize(
    "agent_status", ["pending", "claimed", "retry_pending", "timed_out"]
)
def test_normal_agent_queue_or_timeout_never_enables_fallback(agent_status):
    delivered = []
    acceptance = _acceptance(
        agent_status=agent_status,
        notification_status="delivered",
    )
    outcome = route_monitor_incident(
        projection=_projection(),
        previous_state=ProductionMonitorState(),
        now=NOW + timedelta(hours=4),
        submit=lambda _projection: acceptance,
        recheck=lambda _projection: acceptance,
        deliver_fallback=delivered.append,
    )

    assert outcome.accepted is True
    assert outcome.fallback_status is None
    assert delivered == []


def test_notification_pipeline_pending_past_sla_enables_fixed_fallback():
    delivered = []
    pending = _acceptance(notification_status="pending")
    accepted = route_monitor_incident(
        projection=_projection(),
        previous_state=ProductionMonitorState(),
        now=NOW,
        submit=lambda _projection: pending,
        recheck=lambda _projection: pending,
        deliver_fallback=delivered.append,
    )

    outcome = route_monitor_incident(
        projection=_projection(),
        previous_state=accepted.state,
        now=NOW + timedelta(minutes=10),
        submit=lambda _projection: pending,
        recheck=lambda _projection: pending,
        deliver_fallback=delivered.append,
    )

    assert outcome.accepted is True
    assert outcome.fallback_reason == "deterministic_notification_unavailable"
    assert outcome.fallback_status == "DELIVERED"
    assert len(delivered) == 1


@pytest.mark.parametrize(
    ("claimed_at", "claim_expires_at", "expected_status"),
    [
        (
            NOW + timedelta(minutes=9, seconds=59),
            NOW + timedelta(minutes=11, seconds=59),
            None,
        ),
        (
            NOW + timedelta(minutes=7),
            NOW + timedelta(minutes=9),
            "DELIVERED",
        ),
        (
            NOW + timedelta(minutes=7),
            NOW + timedelta(minutes=11),
            None,
        ),
    ],
)
def test_delivering_notification_only_suppresses_fallback_while_claim_is_fresh(
    claimed_at, claim_expires_at, expected_status
):
    delivered = []
    accepted = route_monitor_incident(
        projection=_projection(),
        previous_state=ProductionMonitorState(),
        now=NOW,
        submit=lambda _projection: _acceptance(),
        recheck=lambda _projection: _acceptance(),
        deliver_fallback=delivered.append,
    )
    delivering = _acceptance(
        notification_status="delivering",
        notification_claimed_at=claimed_at,
        notification_claim_expires_at=claim_expires_at,
    )
    outcome = route_monitor_incident(
        projection=_projection(),
        previous_state=accepted.state,
        now=NOW + timedelta(minutes=10),
        submit=lambda _projection: delivering,
        recheck=lambda _projection: delivering,
        deliver_fallback=delivered.append,
    )

    assert outcome.fallback_status == expected_status
    assert len(delivered) == (1 if expected_status == "DELIVERED" else 0)


def test_notification_failure_needs_its_own_sla_and_failed_recheck():
    delivered = []
    failed_at = NOW + timedelta(minutes=1)
    failed = _acceptance(
        notification_status="failed",
        notification_failed_at=failed_at,
        agent_status="diagnosed",
    )
    accepted = route_monitor_incident(
        projection=_projection(),
        previous_state=ProductionMonitorState(),
        now=NOW,
        submit=lambda _projection: _acceptance(),
        recheck=lambda _projection: _acceptance(),
        deliver_fallback=delivered.append,
    )
    before = route_monitor_incident(
        projection=_projection(),
        previous_state=accepted.state,
        now=NOW + timedelta(minutes=9, seconds=59),
        submit=lambda _projection: failed,
        recheck=lambda _projection: failed,
        deliver_fallback=delivered.append,
    )
    assert before.fallback_status is None

    expired = route_monitor_incident(
        projection=_projection(),
        previous_state=before.state,
        now=NOW + timedelta(minutes=10),
        submit=lambda _projection: failed,
        recheck=lambda _projection: failed,
        deliver_fallback=delivered.append,
    )
    assert expired.fallback_reason == "deterministic_notification_unavailable"
    assert expired.fallback_status == "DELIVERED"
    assert len(delivered) == 1


def test_recheck_recovery_suppresses_fallback_after_initial_failure():
    delivered = []
    failed = _acceptance(
        notification_status="failed",
        notification_failed_at=NOW,
    )
    delivered_acceptance = replace(
        failed,
        notification_status="delivered",
        notification_failed_at=None,
    )
    accepted = route_monitor_incident(
        projection=_projection(),
        previous_state=ProductionMonitorState(),
        now=NOW,
        submit=lambda _projection: _acceptance(),
        recheck=lambda _projection: _acceptance(),
        deliver_fallback=delivered.append,
    )
    outcome = route_monitor_incident(
        projection=_projection(),
        previous_state=accepted.state,
        now=NOW + timedelta(minutes=10),
        submit=lambda _projection: failed,
        recheck=lambda _projection: delivered_acceptance,
        deliver_fallback=delivered.append,
    )

    assert outcome.accepted is True
    assert outcome.fallback_status is None
    assert delivered == []


def test_failed_fallback_is_pending_and_retries_without_changing_incident_acceptance():
    attempts = []

    def fail_delivery(message):
        attempts.append(message)
        raise RuntimeError("offline")

    unavailable = lambda _projection: (_ for _ in ()).throw(
        MonitorIntakeError("transport_unavailable")
    )
    tracking = route_monitor_incident(
        projection=_projection(),
        previous_state=ProductionMonitorState(),
        now=NOW,
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=fail_delivery,
    )
    first = route_monitor_incident(
        projection=_projection(),
        previous_state=tracking.state,
        now=NOW + timedelta(minutes=10),
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=fail_delivery,
    )
    assert first.fallback_status == "PENDING"
    assert first.state.fallback.status == "PENDING"
    assert first.state.incident_acceptances == ()

    second = route_monitor_incident(
        projection=_projection(),
        previous_state=first.state,
        now=NOW + timedelta(minutes=14, seconds=59),
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=lambda message: attempts.append(message),
    )
    assert second.fallback_status == "PENDING"
    assert len(attempts) == 1

    third = route_monitor_incident(
        projection=_projection(),
        previous_state=second.state,
        now=NOW + timedelta(minutes=15),
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=lambda message: attempts.append(message),
    )
    assert third.fallback_status == "DELIVERED"
    assert len(attempts) == 2

    deduplicated = route_monitor_incident(
        projection=_projection(),
        previous_state=third.state,
        now=NOW + timedelta(hours=1),
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=lambda message: attempts.append(message),
    )
    assert deduplicated.fallback_status == "DELIVERED"
    assert len(attempts) == 2


def test_fixed_fallback_is_closed_bounded_and_contains_no_incident_payload():
    message = format_fixed_fallback(
        reason="incident_intake_unavailable",
        component="runtime_incident_intake",
        observed_at=NOW,
        deadline_at=NOW + timedelta(minutes=10),
        rechecked_at=NOW + timedelta(minutes=11),
    )
    assert len(message.encode("utf-8")) <= 512
    assert "audit_abnormal" not in message
    assert "submission" not in message.lower()
    assert "incident_intake_unavailable" in message

    with pytest.raises(ValueError, match="closed"):
        format_fixed_fallback(
            reason="token=secret-value",
            component="runtime_incident_intake",
            observed_at=NOW,
            deadline_at=NOW,
            rechecked_at=NOW,
        )


def test_delivery_failure_atomically_persists_fallback_pending(tmp_path):
    operation_now = [NOW]
    store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json",
        now_factory=lambda: operation_now[0],
    )

    def unavailable(_projection):
        raise MonitorIntakeError("transport_unavailable")

    tracking = route_monitor_incident_persisted(
        state_store=store,
        projection=_projection(),
        now=operation_now[0],
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=lambda _message: (_ for _ in ()).throw(
            RuntimeError("delivery failed")
        ),
    )
    assert tracking.state.fallback.attempts == 0
    operation_now[0] = NOW + timedelta(minutes=10)
    outcome = route_monitor_incident_persisted(
        state_store=store,
        projection=_projection(),
        now=operation_now[0],
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=lambda _message: (_ for _ in ()).throw(
            RuntimeError("delivery failed")
        ),
    )

    assert outcome.fallback_status == "PENDING"
    assert store.load().fallback.status == "PENDING"


def test_intake_outage_sla_does_not_reset_across_polling_generations():
    delivered = []

    def unavailable(_projection):
        raise MonitorIntakeError("transport_unavailable")

    first = route_monitor_incident(
        projection=_projection_at(checked_at=NOW, generation=7),
        previous_state=ProductionMonitorState(),
        now=NOW,
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=delivered.append,
    )
    second = route_monitor_incident(
        projection=_projection_at(
            checked_at=NOW + timedelta(minutes=5), generation=8
        ),
        previous_state=first.state,
        now=NOW + timedelta(minutes=5),
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=delivered.append,
    )

    assert second.state.fallback is not None
    assert second.state.fallback.next_attempt_at == NOW + timedelta(minutes=10)

    expired = route_monitor_incident(
        projection=_projection_at(
            checked_at=NOW + timedelta(minutes=10), generation=9
        ),
        previous_state=second.state,
        now=NOW + timedelta(minutes=10),
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=delivered.append,
    )
    assert expired.fallback_status == "DELIVERED"
    assert len(delivered) == 1


def test_delivered_fallback_does_not_suppress_a_later_anomaly_episode():
    delivered = []

    def unavailable(_projection):
        raise MonitorIntakeError("transport_unavailable")

    episode_a = route_monitor_incident(
        projection=_projection_at(checked_at=NOW, generation=7),
        previous_state=ProductionMonitorState(),
        now=NOW,
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=delivered.append,
    )
    episode_a_expired = route_monitor_incident(
        projection=_projection_at(checked_at=NOW, generation=7),
        previous_state=episode_a.state,
        now=NOW + timedelta(minutes=10),
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=delivered.append,
    )
    assert episode_a_expired.fallback_status == "DELIVERED"

    episode_b = route_monitor_incident(
        projection=_projection_at(
            checked_at=NOW + timedelta(minutes=20),
            generation=8,
            anomaly_fingerprint="b" * 64,
        ),
        previous_state=episode_a_expired.state,
        now=NOW + timedelta(minutes=20),
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=delivered.append,
    )
    episode_b_expired = route_monitor_incident(
        projection=_projection_at(
            checked_at=NOW + timedelta(minutes=20),
            generation=8,
            anomaly_fingerprint="b" * 64,
        ),
        previous_state=episode_b.state,
        now=NOW + timedelta(minutes=30),
        submit=unavailable,
        recheck=unavailable,
        deliver_fallback=delivered.append,
    )

    assert episode_b_expired.fallback_status == "DELIVERED"
    assert len(delivered) == 2


def test_sentinel_timer_rechecks_accepted_episode_until_notification_sla(
    tmp_path,
):
    operation_now = [NOW]
    state_store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json",
        now_factory=lambda: operation_now[0],
    )
    delivered = []
    routed_submission_ids = []

    def acceptance(projection):
        routed_submission_ids.append(projection["submission_id"])
        return MonitorAcceptance(
            submission_id=projection["submission_id"],
            accepted_at=NOW,
            notification_status="pending",
            notification_claimed_at=None,
            notification_claim_expires_at=None,
            notification_failed_at=None,
            agent_status="pending",
        )

    def route(projection):
        return route_monitor_incident_persisted(
            state_store=state_store,
            projection=projection,
            now=operation_now[0],
            submit=acceptance,
            recheck=acceptance,
            deliver_fallback=delivered.append,
        )

    def observation():
        return SentinelObservation(
            checked_at=operation_now[0],
            candidates=(
                CandidateObservation(
                    reason_code="audit_abnormal",
                    fingerprint="a" * 64,
                    observed_at=operation_now[0],
                    anomaly_present=True,
                    evidence_complete=True,
                    snapshot_generation=None,
                    snapshot_started_at=None,
                    snapshot_completed_at=None,
                    last_progress_at=None,
                    execution_deadline_at=None,
                    durable_terminal_fact=True,
                ),
            ),
        )

    first = run_production_monitor_sentinel(
        state_store=state_store,
        observation_collector=observation,
        incident_router=route,
    )
    operation_now[0] = NOW + timedelta(minutes=10)
    second = run_production_monitor_sentinel(
        state_store=state_store,
        observation_collector=observation,
        incident_router=route,
    )

    assert first.exit_code == second.exit_code == 0
    assert len(routed_submission_ids) == 3
    assert routed_submission_ids[0] != routed_submission_ids[1]
    assert len(delivered) == 1
    assert state_store.load().fallback.status == "DELIVERED"


def test_multi_candidate_subset_still_rechecks_original_pending_episode(
    tmp_path,
):
    operation_now = [NOW]
    state_store = ProductionMonitorStateStore(
        tmp_path / "sentinel-v2.json",
        now_factory=lambda: operation_now[0],
    )
    delivered = []
    submissions = []

    def acceptance(projection):
        submissions.append(projection["submission_id"])
        return MonitorAcceptance(
            submission_id=projection["submission_id"],
            accepted_at=NOW,
            notification_status="pending",
            notification_claimed_at=None,
            notification_claim_expires_at=None,
            notification_failed_at=None,
            agent_status="pending",
        )

    def route(projection):
        return route_monitor_incident_persisted(
            state_store=state_store,
            projection=projection,
            now=operation_now[0],
            submit=acceptance,
            recheck=acceptance,
            deliver_fallback=delivered.append,
        )

    def candidate(fingerprint, *, present=True):
        return CandidateObservation(
            reason_code="audit_abnormal",
            fingerprint=fingerprint,
            observed_at=operation_now[0],
            anomaly_present=present,
            evidence_complete=True,
            snapshot_generation=None,
            snapshot_started_at=None,
            snapshot_completed_at=None,
            last_progress_at=None,
            execution_deadline_at=None,
            durable_terminal_fact=not present,
        )

    first = run_production_monitor_sentinel(
        state_store=state_store,
        observation_collector=lambda: SentinelObservation(
            checked_at=operation_now[0],
            candidates=(candidate("a" * 64), candidate("b" * 64)),
        ),
        incident_router=route,
    )
    operation_now[0] = NOW + timedelta(minutes=10)
    second = run_production_monitor_sentinel(
        state_store=state_store,
        observation_collector=lambda: SentinelObservation(
            checked_at=operation_now[0],
            candidates=(
                candidate("a" * 64, present=False),
                candidate("b" * 64),
            ),
        ),
        incident_router=route,
    )
    assert first.result.incident_projection is not None
    assert second.result.incident_projection is None

    outcomes = recheck_due_monitor_notifications_persisted(
        state_store=state_store,
        now=operation_now[0],
        recheck=acceptance,
        deliver_fallback=delivered.append,
    )

    assert len(outcomes) == 1
    assert outcomes[0].fallback_status == "DELIVERED"
    assert len(submissions) == 3
    assert submissions[0] == submissions[1] == submissions[2]
    assert len(delivered) == 1


def test_more_than_129_pending_aggregate_receipts_keep_fresh_acceptance():
    receipts = []
    for generation in range(1, 141):
        anomaly_fingerprint = hashlib.sha256(str(generation).encode()).hexdigest()
        projection = build_monitor_projection(
            {
                "checked_at": NOW,
                "observation_generation": generation,
                "anomaly_fingerprint": anomaly_fingerprint,
                "execution_status": "COMPLETED",
                "observed_health": "UNHEALTHY",
                "reason_codes": ["audit_abnormal"],
                "adapter_failures": [],
                "fallback_reason": None,
            }
        )
        receipts.append(
            IncidentAcceptanceState(
                candidate_fingerprint=anomaly_fingerprint,
                submission_id=projection["submission_id"],
                accepted_at=NOW,
                projection_json=json.dumps(
                    projection,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        )
    fresh_projection = build_monitor_projection(
        {
            "checked_at": NOW,
            "observation_generation": 141,
            "anomaly_fingerprint": "f" * 64,
            "execution_status": "COMPLETED",
            "observed_health": "UNHEALTHY",
            "reason_codes": ["audit_abnormal"],
            "adapter_failures": [],
            "fallback_reason": None,
        }
    )

    outcome = route_monitor_incident(
        projection=fresh_projection,
        previous_state=ProductionMonitorState(
            incident_acceptances=tuple(receipts)
        ),
        now=NOW,
        submit=lambda projection: MonitorAcceptance(
            submission_id=projection["submission_id"],
            accepted_at=NOW,
            notification_status="pending",
            notification_claimed_at=None,
            notification_claim_expires_at=None,
            notification_failed_at=None,
            agent_status="pending",
        ),
        recheck=lambda _projection: (_ for _ in ()).throw(
            AssertionError("not due")
        ),
        deliver_fallback=lambda _message: None,
    )

    pending = {
        item.candidate_fingerprint
        for item in outcome.state.incident_acceptances
        if item.projection_json is not None and not item.routing_terminal
    }
    assert len(pending) == 141
    assert fresh_projection["anomaly_fingerprint"] in pending
