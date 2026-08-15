from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.production_monitor_contract import (
    MONITOR_POLICY_NAMES,
    SENTINEL_REASON_CODES,
)
from telegram_kol_research.production_monitor_policy import (
    EVIDENCE_UNKNOWN,
    IMMEDIATE,
    SETTLING,
    REASON_POLICIES,
    CandidateContext,
    CandidateObservation,
    classify_candidate,
)


NOW = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64
SETTLING_REASON = "composite_position_without_verified_stop"


def _candidate(**overrides):
    value = {
        "reason_code": SETTLING_REASON,
        "fingerprint": FINGERPRINT,
        "observed_at": NOW,
        "anomaly_present": True,
        "evidence_complete": True,
        "snapshot_generation": 1,
        "snapshot_completed_at": NOW,
        "last_progress_at": NOW - timedelta(minutes=10),
        "execution_deadline_at": NOW - timedelta(minutes=5),
        "durable_terminal_fact": False,
    }
    value.update(overrides)
    value.setdefault("snapshot_started_at", value["snapshot_completed_at"])
    return CandidateObservation(**value)


def _context(*, now=NOW, previous=None):
    return CandidateContext(now=now, previous=previous)


def _next(
    previous,
    *,
    at,
    generation,
    anomaly_present=True,
    durable_terminal_fact=False,
):
    return classify_candidate(
        _candidate(
            observed_at=at,
            snapshot_generation=generation,
            snapshot_started_at=at,
            snapshot_completed_at=at,
            anomaly_present=anomaly_present,
            durable_terminal_fact=durable_terminal_fact,
        ),
        _context(now=at, previous=previous),
    )


def test_every_reason_has_exactly_one_explicit_policy():
    assert set(REASON_POLICIES) == set(SENTINEL_REASON_CODES)
    assert {policy.classification for policy in REASON_POLICIES.values()} == {
        IMMEDIATE,
        SETTLING,
        EVIDENCE_UNKNOWN,
    }
    assert {
        policy.classification for policy in REASON_POLICIES.values()
    } == MONITOR_POLICY_NAMES


def test_reason_policy_authority_is_immutable():
    try:
        with pytest.raises(TypeError):
            REASON_POLICIES["future_reason"] = REASON_POLICIES["adapter_failure"]
    finally:
        if "future_reason" in REASON_POLICIES:
            del REASON_POLICIES["future_reason"]


def test_durable_high_level_reasons_cover_submit_unknown_and_recovery_required():
    assert REASON_POLICIES["audit_abnormal"].classification == IMMEDIATE
    assert REASON_POLICIES["event_unknown_status"].classification == IMMEDIATE
    assert REASON_POLICIES["event_recovery_status"].classification == IMMEDIATE


def test_unknown_reason_fails_closed():
    result = classify_candidate(
        _candidate(reason_code="future_reason"),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.incident_eligible is False
    assert result.deployment_blocking is True
    assert result.candidate_state is None


def test_unbounded_candidate_fingerprint_fails_closed_before_state_creation():
    result = classify_candidate(
        _candidate(fingerprint="unsafe"),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.incident_eligible is False
    assert result.candidate_state is None
    assert result.decision_reason == "FINGERPRINT_INVALID"


def test_invalid_fingerprint_preserves_confirmed_candidate_sticky_state():
    first = _next(None, at=NOW - timedelta(minutes=2), generation=7)
    confirmed = _next(
        first.candidate_state,
        at=NOW - timedelta(minutes=1),
        generation=8,
    )

    result = classify_candidate(
        _candidate(fingerprint="unsafe"),
        _context(previous=confirmed.candidate_state),
    )

    assert result.observed_health == "UNHEALTHY"
    assert result.incident_eligible is True
    assert result.deployment_blocking is True
    assert result.candidate_state == confirmed.candidate_state
    assert result.decision_reason == "FINGERPRINT_INVALID"


def test_immediate_complete_fact_confirms_without_exchange_generations():
    result = classify_candidate(
        _candidate(
            reason_code="event_recovery_status",
            snapshot_generation=None,
            snapshot_completed_at=None,
            last_progress_at=None,
            execution_deadline_at=None,
        ),
        _context(),
    )

    assert result.observed_health == "UNHEALTHY"
    assert result.incident_eligible is True
    assert result.deployment_blocking is True
    assert result.candidate_state.lifecycle == "CONFIRMED"
    assert result.candidate_state.confirmation_evidence_class == "DURABLE_FACT"


def test_evidence_unknown_policy_never_guesses_an_incident():
    result = classify_candidate(
        _candidate(
            reason_code="adapter_failure",
            snapshot_generation=None,
            snapshot_completed_at=None,
            last_progress_at=None,
            execution_deadline_at=None,
        ),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.incident_eligible is False
    assert result.deployment_blocking is True
    assert result.candidate_state.lifecycle == "SETTLING"


def test_complete_clear_evidence_unknown_candidate_resolves_healthy():
    result = classify_candidate(
        _candidate(
            reason_code="adapter_failure",
            anomaly_present=False,
            snapshot_generation=None,
            snapshot_completed_at=None,
            last_progress_at=None,
            execution_deadline_at=None,
        ),
        _context(),
    )

    assert result.observed_health == "HEALTHY"
    assert result.incident_eligible is False
    assert result.deployment_blocking is False
    assert result.candidate_state.lifecycle == "RESOLVED"
    assert result.candidate_state.resolution_evidence_class == (
        "COMPLETE_EVIDENCE_NO_ANOMALY"
    )


@pytest.mark.parametrize("invalid_value", [None, 0, 1, "true"])
def test_durable_terminal_fact_requires_exact_bool(invalid_value):
    result = classify_candidate(
        _candidate(durable_terminal_fact=invalid_value),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.incident_eligible is False
    assert result.candidate_state is None
    assert result.decision_reason == "DURABLE_TERMINAL_FACT_INVALID"


@pytest.mark.parametrize(
    ("deadline", "expected_decision"),
    [
        (NOW + timedelta(seconds=1), "BEFORE_DURABLE_DEADLINE"),
        (NOW, "SNAPSHOT_NOT_AFTER_DURABLE_DEADLINE"),
        (NOW - timedelta(seconds=1), "AWAITING_DISTINCT_BAD_GENERATIONS"),
    ],
)
def test_settling_uses_the_row_durable_deadline(deadline, expected_decision):
    result = classify_candidate(
        _candidate(execution_deadline_at=deadline),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.incident_eligible is False
    assert result.decision_reason == expected_decision
    assert result.candidate_state.earliest_confirmation_at == deadline


def test_settling_missing_row_deadline_is_unknown_not_global_timeout():
    result = classify_candidate(
        _candidate(execution_deadline_at=None),
        _context(now=NOW + timedelta(days=2)),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.incident_eligible is False
    assert result.decision_reason == "DURABLE_DEADLINE_MISSING"


def test_snapshot_before_local_progress_cannot_confirm():
    result = classify_candidate(
        _candidate(snapshot_completed_at=NOW - timedelta(minutes=11)),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.candidate_state.anomaly_generations == ()
    assert result.decision_reason == "SNAPSHOT_NOT_AFTER_PROGRESS"


@pytest.mark.parametrize(
    ("completed_at", "expected_decision"),
    [
        (
            NOW - timedelta(minutes=5, seconds=1),
            "SNAPSHOT_BEFORE_DURABLE_DEADLINE",
        ),
        (
            NOW - timedelta(minutes=5),
            "SNAPSHOT_NOT_AFTER_DURABLE_DEADLINE",
        ),
        (
            NOW - timedelta(minutes=4, seconds=59),
            "AWAITING_DISTINCT_BAD_GENERATIONS",
        ),
    ],
)
def test_settling_snapshot_must_complete_strictly_after_durable_deadline(
    completed_at, expected_decision
):
    result = classify_candidate(
        _candidate(snapshot_completed_at=completed_at),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.candidate_state.anomaly_generations == (
        () if completed_at <= NOW - timedelta(minutes=5) else (1,)
    )
    assert result.decision_reason == expected_decision


@pytest.mark.parametrize(
    ("started_at", "expected_decision", "expected_generations"),
    [
        (
            NOW - timedelta(minutes=5, seconds=1),
            "SNAPSHOT_BEFORE_DURABLE_DEADLINE",
            (),
        ),
        (
            NOW - timedelta(minutes=5),
            "SNAPSHOT_NOT_AFTER_DURABLE_DEADLINE",
            (),
        ),
        (
            NOW - timedelta(minutes=4, seconds=59),
            "AWAITING_DISTINCT_BAD_GENERATIONS",
            (1,),
        ),
    ],
)
def test_settling_snapshot_must_start_strictly_after_durable_deadline(
    started_at, expected_decision, expected_generations
):
    result = classify_candidate(
        _candidate(
            snapshot_started_at=started_at,
            snapshot_completed_at=NOW - timedelta(minutes=4),
        ),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.candidate_state.anomaly_generations == expected_generations
    assert result.decision_reason == expected_decision


def test_snapshot_started_before_deadline_and_completed_after_does_not_count():
    result = classify_candidate(
        _candidate(
            snapshot_started_at=NOW - timedelta(minutes=5, seconds=1),
            snapshot_completed_at=NOW - timedelta(minutes=4, seconds=59),
        ),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.candidate_state.anomaly_generations == ()
    assert result.decision_reason == "SNAPSHOT_BEFORE_DURABLE_DEADLINE"


@pytest.mark.parametrize(
    ("started_at", "expected_decision", "expected_generations"),
    [
        (
            NOW - timedelta(minutes=10, seconds=1),
            "SNAPSHOT_NOT_AFTER_PROGRESS",
            (),
        ),
        (
            NOW - timedelta(minutes=10),
            "SNAPSHOT_NOT_AFTER_PROGRESS",
            (),
        ),
        (
            NOW - timedelta(minutes=9, seconds=59),
            "AWAITING_DISTINCT_BAD_GENERATIONS",
            (1,),
        ),
    ],
)
def test_settling_snapshot_must_start_strictly_after_local_progress(
    started_at, expected_decision, expected_generations
):
    result = classify_candidate(
        _candidate(
            snapshot_started_at=started_at,
            snapshot_completed_at=NOW - timedelta(minutes=9),
            execution_deadline_at=NOW - timedelta(minutes=10),
        ),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.candidate_state.anomaly_generations == expected_generations
    assert result.decision_reason == expected_decision


def test_snapshot_started_after_completed_fails_closed():
    result = classify_candidate(
        _candidate(
            snapshot_started_at=NOW - timedelta(seconds=1),
            snapshot_completed_at=NOW - timedelta(seconds=2),
        ),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.incident_eligible is False
    assert result.candidate_state.anomaly_generations == ()
    assert result.decision_reason == "SNAPSHOT_TIMESTAMP_ORDER_INVALID"


@pytest.mark.parametrize(
    ("missing_field", "expected_decision"),
    [
        ("snapshot_generation", "SNAPSHOT_GENERATION_INVALID"),
        ("snapshot_started_at", "SNAPSHOT_TIMESTAMP_INVALID"),
        ("snapshot_completed_at", "SNAPSHOT_TIMESTAMP_INVALID"),
    ],
)
def test_snapshot_required_fields_must_be_present(
    missing_field, expected_decision
):
    values = {
        "snapshot_generation": 1,
        "snapshot_started_at": NOW,
        "snapshot_completed_at": NOW,
    }
    values[missing_field] = None

    result = classify_candidate(_candidate(**values), _context())

    assert result.observed_health == "UNKNOWN"
    assert result.incident_eligible is False
    assert result.candidate_state.anomaly_generations == ()
    assert result.decision_reason == expected_decision


def test_naive_snapshot_start_fails_closed():
    result = classify_candidate(
        _candidate(snapshot_started_at=NOW.replace(tzinfo=None)),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.incident_eligible is False
    assert result.candidate_state.anomaly_generations == ()
    assert result.decision_reason == "SNAPSHOT_TIMESTAMP_INVALID"


def test_future_snapshot_start_fails_closed_without_counting_generation():
    result = classify_candidate(
        _candidate(
            snapshot_started_at=NOW + timedelta(seconds=1),
            snapshot_completed_at=NOW + timedelta(seconds=2),
        ),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.incident_eligible is False
    assert result.candidate_state.anomaly_generations == ()
    assert result.decision_reason == "FUTURE_TIMESTAMP"


def test_repeated_generation_does_not_count_twice():
    first = _next(None, at=NOW - timedelta(minutes=1), generation=7)
    repeated = _next(first.candidate_state, at=NOW, generation=7)

    assert repeated.observed_health == "UNKNOWN"
    assert repeated.incident_eligible is False
    assert repeated.candidate_state.lifecycle == "SETTLING"
    assert repeated.candidate_state.anomaly_generations == (7,)
    assert repeated.decision_reason == "SNAPSHOT_GENERATION_REPEATED"


def test_two_distinct_complete_post_progress_bad_generations_confirm():
    first = _next(None, at=NOW - timedelta(minutes=1), generation=7)
    second = _next(first.candidate_state, at=NOW, generation=8)

    assert second.observed_health == "UNHEALTHY"
    assert second.incident_eligible is True
    assert second.candidate_state.lifecycle == "CONFIRMED"
    assert second.candidate_state.anomaly_generations == (7, 8)
    assert second.candidate_state.confirmation_evidence_class == (
        "TWO_DISTINCT_COMPLETE_POST_PROGRESS_GENERATIONS"
    )


def test_bad_then_good_resolves_without_incident():
    first = _next(None, at=NOW - timedelta(minutes=1), generation=7)
    good = _next(
        first.candidate_state,
        at=NOW,
        generation=8,
        anomaly_present=False,
    )

    assert good.observed_health == "HEALTHY"
    assert good.incident_eligible is False
    assert good.deployment_blocking is False
    assert good.candidate_state.lifecycle == "RESOLVED"


def test_confirmed_recovery_requires_two_distinct_healthy_generations():
    first = _next(None, at=NOW - timedelta(minutes=3), generation=7)
    confirmed = _next(
        first.candidate_state,
        at=NOW - timedelta(minutes=2),
        generation=8,
    )
    one_good = _next(
        confirmed.candidate_state,
        at=NOW - timedelta(minutes=1),
        generation=9,
        anomaly_present=False,
    )
    two_good = _next(
        one_good.candidate_state,
        at=NOW,
        generation=10,
        anomaly_present=False,
    )

    assert confirmed.candidate_state.lifecycle == "CONFIRMED"
    assert one_good.observed_health == "UNHEALTHY"
    assert one_good.candidate_state.lifecycle == "CONFIRMED"
    assert one_good.candidate_state.healthy_generations == (9,)
    assert two_good.observed_health == "HEALTHY"
    assert two_good.incident_eligible is False
    assert two_good.deployment_blocking is False
    assert two_good.candidate_state.lifecycle == "RESOLVED"
    assert two_good.candidate_state.resolution_evidence_class == (
        "TWO_DISTINCT_COMPLETE_HEALTHY_GENERATIONS"
    )


def test_confirmed_flap_back_to_bad_stays_sticky_and_resets_recovery_run():
    first = _next(None, at=NOW - timedelta(minutes=4), generation=7)
    confirmed = _next(
        first.candidate_state,
        at=NOW - timedelta(minutes=3),
        generation=8,
    )
    one_good = _next(
        confirmed.candidate_state,
        at=NOW - timedelta(minutes=2),
        generation=9,
        anomaly_present=False,
    )
    bad_again = _next(
        one_good.candidate_state,
        at=NOW - timedelta(minutes=1),
        generation=10,
    )

    assert bad_again.observed_health == "UNHEALTHY"
    assert bad_again.incident_eligible is True
    assert bad_again.candidate_state.lifecycle == "CONFIRMED"
    assert bad_again.candidate_state.anomaly_generations == (10,)
    assert bad_again.candidate_state.healthy_generations == ()
    assert bad_again.candidate_state.last_observation_anomalous is True
    assert bad_again.decision_reason == "CONFIRMED_STILL_PRESENT"


@pytest.mark.parametrize("authority_field", ["last_progress_at", "execution_deadline_at"])
def test_authority_change_starts_a_new_bad_generation_run(authority_field):
    first = _next(None, at=NOW - timedelta(minutes=2), generation=7)
    changed_authority = {
        "last_progress_at": NOW - timedelta(minutes=9),
        "execution_deadline_at": NOW - timedelta(minutes=4),
    }

    changed = classify_candidate(
        _candidate(
            observed_at=NOW - timedelta(minutes=1),
            snapshot_generation=8,
            snapshot_completed_at=NOW - timedelta(minutes=1),
            **{authority_field: changed_authority[authority_field]},
        ),
        _context(now=NOW - timedelta(minutes=1), previous=first.candidate_state),
    )

    assert changed.observed_health == "UNKNOWN"
    assert changed.candidate_state.lifecycle == "SETTLING"
    assert changed.candidate_state.anomaly_generations == (8,)
    assert changed.candidate_state.healthy_generations == ()
    assert changed.candidate_state.first_observed_at == NOW - timedelta(minutes=1)
    assert changed.decision_reason == "AWAITING_DISTINCT_BAD_GENERATIONS"


def test_confirmed_progress_authority_change_is_sticky_without_resolution():
    first = _next(None, at=NOW - timedelta(minutes=2), generation=7)
    confirmed = _next(
        first.candidate_state,
        at=NOW - timedelta(minutes=1),
        generation=8,
    )

    changed = classify_candidate(
        _candidate(
            anomaly_present=False,
            last_progress_at=NOW - timedelta(minutes=9),
            snapshot_generation=9,
        ),
        _context(previous=confirmed.candidate_state),
    )

    assert changed.observed_health == "UNHEALTHY"
    assert changed.incident_eligible is True
    assert changed.candidate_state.lifecycle == "CONFIRMED"
    assert changed.candidate_state.last_progress_at == NOW - timedelta(minutes=9)
    assert changed.candidate_state.execution_deadline_at == NOW - timedelta(minutes=5)
    assert changed.candidate_state.anomaly_generations == ()
    assert changed.candidate_state.healthy_generations == (9,)
    assert changed.candidate_state.first_observed_at == NOW
    assert changed.decision_reason == "AWAITING_RECOVERY_HYSTERESIS"


def test_confirmed_authority_change_does_not_reuse_old_healthy_generation():
    first = _next(None, at=NOW - timedelta(minutes=4), generation=7)
    confirmed = _next(
        first.candidate_state,
        at=NOW - timedelta(minutes=3),
        generation=8,
    )
    old_authority_healthy = _next(
        confirmed.candidate_state,
        at=NOW - timedelta(minutes=2),
        generation=9,
        anomaly_present=False,
    )

    new_authority_healthy = classify_candidate(
        _candidate(
            observed_at=NOW - timedelta(minutes=1),
            anomaly_present=False,
            snapshot_generation=10,
            snapshot_completed_at=NOW - timedelta(minutes=1),
            last_progress_at=NOW - timedelta(minutes=9),
        ),
        _context(
            now=NOW - timedelta(minutes=1),
            previous=old_authority_healthy.candidate_state,
        ),
    )
    recovered = classify_candidate(
        _candidate(
            anomaly_present=False,
            snapshot_generation=11,
            last_progress_at=NOW - timedelta(minutes=9),
        ),
        _context(previous=new_authority_healthy.candidate_state),
    )

    assert new_authority_healthy.observed_health == "UNHEALTHY"
    assert new_authority_healthy.candidate_state.lifecycle == "CONFIRMED"
    assert new_authority_healthy.candidate_state.anomaly_generations == ()
    assert new_authority_healthy.candidate_state.healthy_generations == (10,)
    assert recovered.observed_health == "HEALTHY"
    assert recovered.candidate_state.lifecycle == "RESOLVED"
    assert recovered.candidate_state.healthy_generations == (10, 11)


def test_authority_rebase_rejects_consumed_generation_before_recovery():
    first = _next(None, at=NOW - timedelta(minutes=4), generation=7)
    confirmed = _next(
        first.candidate_state,
        at=NOW - timedelta(minutes=3),
        generation=8,
    )
    old_authority_healthy = _next(
        confirmed.candidate_state,
        at=NOW - timedelta(minutes=2),
        generation=9,
        anomaly_present=False,
    )

    repeated = classify_candidate(
        _candidate(
            observed_at=NOW - timedelta(minutes=1),
            anomaly_present=False,
            snapshot_generation=9,
            snapshot_completed_at=NOW - timedelta(minutes=1),
            last_progress_at=NOW - timedelta(minutes=9),
        ),
        _context(
            now=NOW - timedelta(minutes=1),
            previous=old_authority_healthy.candidate_state,
        ),
    )
    one_new_healthy = classify_candidate(
        _candidate(
            observed_at=NOW,
            anomaly_present=False,
            snapshot_generation=10,
            snapshot_completed_at=NOW,
            last_progress_at=NOW - timedelta(minutes=9),
        ),
        _context(
            now=NOW,
            previous=repeated.candidate_state,
        ),
    )
    recovered = classify_candidate(
        _candidate(
            observed_at=NOW + timedelta(minutes=1),
            snapshot_completed_at=NOW + timedelta(minutes=1),
            anomaly_present=False,
            snapshot_generation=11,
            last_progress_at=NOW - timedelta(minutes=9),
        ),
        _context(
            now=NOW + timedelta(minutes=1),
            previous=one_new_healthy.candidate_state,
        ),
    )

    assert repeated.observed_health == "UNHEALTHY"
    assert repeated.candidate_state.lifecycle == "CONFIRMED"
    assert repeated.candidate_state.anomaly_generations == ()
    assert repeated.candidate_state.healthy_generations == ()
    assert repeated.candidate_state.snapshot_generation_watermark == 9
    assert repeated.decision_reason == "SNAPSHOT_GENERATION_REPEATED"
    assert one_new_healthy.candidate_state.healthy_generations == (10,)
    assert one_new_healthy.candidate_state.snapshot_generation_watermark == 10
    assert recovered.observed_health == "HEALTHY"
    assert recovered.candidate_state.healthy_generations == (10, 11)
    assert recovered.candidate_state.snapshot_generation_watermark == 11


def test_resolved_cycle_rejects_consumed_generation_before_reconfirmation():
    first = _next(None, at=NOW - timedelta(minutes=4), generation=7)
    confirmed = _next(
        first.candidate_state,
        at=NOW - timedelta(minutes=3),
        generation=8,
    )
    one_healthy = _next(
        confirmed.candidate_state,
        at=NOW - timedelta(minutes=2),
        generation=9,
        anomaly_present=False,
    )
    resolved = _next(
        one_healthy.candidate_state,
        at=NOW - timedelta(minutes=1),
        generation=10,
        anomaly_present=False,
    )
    repeated = _next(
        resolved.candidate_state,
        at=NOW,
        generation=10,
    )
    first_new = _next(
        repeated.candidate_state,
        at=NOW + timedelta(minutes=1),
        generation=11,
    )
    reconfirmed = _next(
        first_new.candidate_state,
        at=NOW + timedelta(minutes=2),
        generation=12,
    )

    assert resolved.candidate_state.lifecycle == "RESOLVED"
    assert resolved.candidate_state.snapshot_generation_watermark == 10
    assert repeated.observed_health == "UNKNOWN"
    assert repeated.candidate_state.lifecycle == "SETTLING"
    assert repeated.candidate_state.anomaly_generations == ()
    assert repeated.candidate_state.snapshot_generation_watermark == 10
    assert repeated.decision_reason == "SNAPSHOT_GENERATION_REPEATED"
    assert first_new.candidate_state.anomaly_generations == (11,)
    assert reconfirmed.observed_health == "UNHEALTHY"
    assert reconfirmed.candidate_state.anomaly_generations == (11, 12)
    assert reconfirmed.candidate_state.snapshot_generation_watermark == 12


def test_resolved_candidate_with_new_future_deadline_starts_clean_settling_cycle():
    cleared = classify_candidate(
        _candidate(anomaly_present=False),
        _context(),
    )
    future_deadline = NOW + timedelta(minutes=5)

    restarted = classify_candidate(
        _candidate(
            observed_at=NOW + timedelta(minutes=1),
            snapshot_generation=2,
            snapshot_completed_at=NOW + timedelta(minutes=1),
            last_progress_at=NOW + timedelta(seconds=30),
            execution_deadline_at=future_deadline,
        ),
        _context(
            now=NOW + timedelta(minutes=1),
            previous=cleared.candidate_state,
        ),
    )

    assert restarted.observed_health == "UNKNOWN"
    assert restarted.candidate_state.lifecycle == "SETTLING"
    assert restarted.candidate_state.anomaly_generations == ()
    assert restarted.candidate_state.healthy_generations == ()
    assert restarted.candidate_state.first_observed_at == NOW + timedelta(minutes=1)
    assert restarted.candidate_state.execution_deadline_at == future_deadline
    assert restarted.decision_reason == "BEFORE_DURABLE_DEADLINE"


def test_durable_terminal_fact_resolves_confirmed_candidate_immediately():
    first = _next(None, at=NOW - timedelta(minutes=2), generation=7)
    confirmed = _next(
        first.candidate_state,
        at=NOW - timedelta(minutes=1),
        generation=8,
    )
    terminal = _next(
        confirmed.candidate_state,
        at=NOW,
        generation=None,
        anomaly_present=False,
        durable_terminal_fact=True,
    )

    assert terminal.observed_health == "HEALTHY"
    assert terminal.candidate_state.lifecycle == "RESOLVED"
    assert terminal.candidate_state.resolution_evidence_class == "DURABLE_TERMINAL"


@pytest.mark.parametrize(
    ("deadline", "starts_new_evidence_run"),
    [
        (None, False),
        (NOW + timedelta(minutes=5), True),
        (NOW - timedelta(minutes=11), False),
    ],
)
def test_confirmed_candidate_is_sticky_when_deadline_is_missing_or_changed(
    deadline, starts_new_evidence_run
):
    first = _next(None, at=NOW - timedelta(minutes=2), generation=7)
    confirmed = _next(
        first.candidate_state,
        at=NOW - timedelta(minutes=1),
        generation=8,
    )

    result = classify_candidate(
        _candidate(
            execution_deadline_at=deadline,
            snapshot_generation=9,
        ),
        _context(previous=confirmed.candidate_state),
    )

    assert result.observed_health == "UNHEALTHY"
    assert result.deployment_blocking is True
    assert result.candidate_state.lifecycle == "CONFIRMED"
    if starts_new_evidence_run:
        assert result.candidate_state.execution_deadline_at == deadline
        assert result.candidate_state.anomaly_generations == ()
        assert result.candidate_state.healthy_generations == ()
        assert result.candidate_state.first_observed_at == NOW
    else:
        assert result.candidate_state == confirmed.candidate_state


def test_rebased_confirmed_candidate_stays_confirmed_until_new_deadline():
    first = _next(None, at=NOW - timedelta(minutes=2), generation=7)
    confirmed = _next(
        first.candidate_state,
        at=NOW - timedelta(minutes=1),
        generation=8,
    )
    future_deadline = NOW + timedelta(minutes=5)
    rebased = classify_candidate(
        _candidate(execution_deadline_at=future_deadline, snapshot_generation=9),
        _context(previous=confirmed.candidate_state),
    )
    still_waiting = classify_candidate(
        _candidate(
            observed_at=NOW + timedelta(minutes=1),
            execution_deadline_at=future_deadline,
            snapshot_generation=10,
            snapshot_completed_at=NOW + timedelta(minutes=1),
        ),
        _context(
            now=NOW + timedelta(minutes=1),
            previous=rebased.candidate_state,
        ),
    )

    assert still_waiting.observed_health == "UNHEALTHY"
    assert still_waiting.incident_eligible is True
    assert still_waiting.candidate_state.lifecycle == "CONFIRMED"
    assert still_waiting.candidate_state.anomaly_generations == ()
    assert still_waiting.candidate_state.healthy_generations == ()
    assert still_waiting.decision_reason == "BEFORE_DURABLE_DEADLINE"


def test_durable_terminal_resolution_precedes_missing_deadline_branch():
    first = _next(None, at=NOW - timedelta(minutes=2), generation=7)
    confirmed = _next(
        first.candidate_state,
        at=NOW - timedelta(minutes=1),
        generation=8,
    )

    terminal = classify_candidate(
        _candidate(
            anomaly_present=False,
            durable_terminal_fact=True,
            execution_deadline_at=None,
            snapshot_generation=None,
            snapshot_completed_at=None,
        ),
        _context(previous=confirmed.candidate_state),
    )

    assert terminal.observed_health == "HEALTHY"
    assert terminal.candidate_state.lifecycle == "RESOLVED"
    assert terminal.candidate_state.resolution_evidence_class == "DURABLE_TERMINAL"


def test_terminal_resolution_keeps_previous_paired_progress_deadline_authority():
    first = _next(None, at=NOW - timedelta(minutes=2), generation=7)
    confirmed = _next(
        first.candidate_state,
        at=NOW - timedelta(minutes=1),
        generation=8,
    )

    terminal = classify_candidate(
        _candidate(
            anomaly_present=False,
            durable_terminal_fact=True,
            last_progress_at=NOW,
            execution_deadline_at=None,
            snapshot_generation=None,
            snapshot_completed_at=None,
        ),
        _context(previous=confirmed.candidate_state),
    )

    assert terminal.observed_health == "HEALTHY"
    assert terminal.candidate_state.lifecycle == "RESOLVED"
    assert terminal.candidate_state.last_progress_at == (
        confirmed.candidate_state.last_progress_at
    )
    assert terminal.candidate_state.execution_deadline_at == (
        confirmed.candidate_state.execution_deadline_at
    )
    assert terminal.candidate_state.earliest_confirmation_at == (
        confirmed.candidate_state.execution_deadline_at
    )


@pytest.mark.parametrize("anomaly_present", [False, True])
def test_evidence_unknown_rejects_deadline_before_progress(anomaly_present):
    result = classify_candidate(
        _candidate(
            reason_code="adapter_failure",
            anomaly_present=anomaly_present,
            last_progress_at=NOW - timedelta(minutes=1),
            execution_deadline_at=NOW - timedelta(minutes=2),
            snapshot_generation=None,
            snapshot_completed_at=None,
        ),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.incident_eligible is False
    assert result.deployment_blocking is True
    assert result.candidate_state is None
    assert result.decision_reason == "TEMPORAL_ORDER_INVALID"


def test_last_progress_after_observation_fails_closed():
    result = classify_candidate(
        _candidate(
            observed_at=NOW - timedelta(seconds=1),
            last_progress_at=NOW,
            execution_deadline_at=NOW,
            snapshot_completed_at=NOW - timedelta(seconds=1),
        ),
        _context(),
    )

    assert result.observed_health == "UNKNOWN"
    assert result.incident_eligible is False
    assert result.candidate_state is None
    assert result.decision_reason == "LAST_PROGRESS_AFTER_OBSERVATION"


def test_future_snapshot_fails_closed_without_changing_previous_evidence():
    first = _next(None, at=NOW - timedelta(minutes=1), generation=7)
    future = classify_candidate(
        _candidate(
            snapshot_generation=8,
            snapshot_completed_at=NOW + timedelta(seconds=1),
        ),
        _context(previous=first.candidate_state),
    )

    assert future.observed_health == "UNKNOWN"
    assert future.incident_eligible is False
    assert future.candidate_state == first.candidate_state
    assert future.decision_reason == "FUTURE_TIMESTAMP"


def test_out_of_order_generation_fails_closed_without_rewriting_history():
    first = _next(None, at=NOW - timedelta(minutes=1), generation=8)
    out_of_order = _next(first.candidate_state, at=NOW, generation=7)

    assert out_of_order.observed_health == "UNKNOWN"
    assert out_of_order.candidate_state == first.candidate_state
    assert out_of_order.decision_reason == "SNAPSHOT_GENERATION_OUT_OF_ORDER"


def test_clock_rollback_fails_closed_without_rewriting_history():
    first = _next(None, at=NOW, generation=7)
    rollback = classify_candidate(
        replace(_candidate(), observed_at=NOW - timedelta(seconds=1)),
        _context(now=NOW - timedelta(seconds=1), previous=first.candidate_state),
    )

    assert rollback.observed_health == "UNKNOWN"
    assert rollback.candidate_state == first.candidate_state
    assert rollback.decision_reason == "CLOCK_ROLLBACK"
