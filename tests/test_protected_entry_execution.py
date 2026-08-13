from dataclasses import FrozenInstanceError

import pytest

from telegram_kol_research.protected_entry_execution import (
    PROTECTED_ENTRY_ACTIONS,
    PROTECTED_ENTRY_EVENTS,
    PROTECTED_ENTRY_STATES,
    EntryTransition,
    ProtectedEntryDecisionError,
    ProtectedEntryFacts,
    decide_protected_entry_transition,
)


STATES = {
    "planned",
    "entry_prepared",
    "entry_submitting",
    "entry_pending_readback",
    "entry_unknown",
    "entry_rejected",
    "entry_confirmed",
    "protection_prepared",
    "protection_pending_readback",
    "protection_unknown",
    "protected",
    "next_leg_preflight",
    "pre_submit_deferred",
    "completed",
    "recovery_required",
    "submission_failed_no_exposure",
}

EVENTS = {
    "prepare_entry",
    "request_entry_submit",
    "entry_submission_accepted",
    "entry_submission_rejected",
    "entry_submission_unknown",
    "entry_readback_confirmed",
    "prepare_protection",
    "protection_submission_accepted",
    "protection_submission_rejected",
    "protection_submission_unknown",
    "protection_readback_confirmed",
    "start_next_leg_preflight",
    "next_leg_preflight_ready",
    "next_leg_preflight_deferred",
    "complete_entry_sequence",
    "require_recovery",
    "confirm_submission_failed_no_exposure",
}


def _facts(**overrides):
    values = {
        "live_exposure": False,
        "writer_attempted": False,
        "required_protection_count": 0,
        "confirmed_protection_count": 0,
        "snapshot_complete": False,
        "operation_deadline_expired": False,
    }
    values.update(overrides)
    return ProtectedEntryFacts(**values)


def _decide(state, event, **facts):
    return decide_protected_entry_transition(
        current_state=state,
        event=event,
        facts=_facts(**facts),
    )


def test_contract_exposes_only_the_approved_states_and_closed_events():
    assert PROTECTED_ENTRY_STATES == frozenset(STATES)
    assert PROTECTED_ENTRY_EVENTS == frozenset(EVENTS)
    assert PROTECTED_ENTRY_ACTIONS == frozenset(
        {"submit", "readback_only", "defer", "supervision_only", "none"}
    )


def test_facts_and_transition_results_are_immutable():
    facts = _facts()
    transition = EntryTransition(
        current_state="planned",
        event="prepare_entry",
        next_state="entry_prepared",
        allowed=True,
        reason_code="entry_intent_prepared",
        next_action="none",
    )

    with pytest.raises(FrozenInstanceError):
        facts.live_exposure = True
    with pytest.raises(FrozenInstanceError):
        transition.allowed = False


@pytest.mark.parametrize(
    (
        "current_state",
        "event",
        "facts",
        "next_state",
        "next_action",
        "reason_code",
    ),
    [
        (
            "planned",
            "prepare_entry",
            {},
            "entry_prepared",
            "none",
            "entry_intent_prepared",
        ),
        (
            "entry_prepared",
            "request_entry_submit",
            {},
            "entry_submitting",
            "submit",
            "entry_submit_authorized",
        ),
        (
            "entry_submitting",
            "entry_submission_accepted",
            {"writer_attempted": True},
            "entry_pending_readback",
            "readback_only",
            "entry_submission_accepted",
        ),
        (
            "entry_submitting",
            "entry_submission_unknown",
            {"writer_attempted": True},
            "entry_unknown",
            "readback_only",
            "entry_submission_unknown",
        ),
        (
            "entry_submitting",
            "entry_submission_rejected",
            {"writer_attempted": True},
            "entry_rejected",
            "none",
            "entry_submission_rejected",
        ),
        (
            "entry_pending_readback",
            "entry_readback_confirmed",
            {
                "writer_attempted": True,
                "live_exposure": True,
                "snapshot_complete": True,
            },
            "entry_confirmed",
            "none",
            "entry_readback_confirmed",
        ),
        (
            "entry_unknown",
            "entry_readback_confirmed",
            {
                "writer_attempted": True,
                "live_exposure": True,
                "snapshot_complete": True,
            },
            "entry_confirmed",
            "none",
            "entry_readback_confirmed",
        ),
        (
            "entry_confirmed",
            "prepare_protection",
            {
                "writer_attempted": True,
                "live_exposure": True,
                "required_protection_count": 2,
                "snapshot_complete": True,
            },
            "protection_prepared",
            "submit",
            "protection_intents_prepared",
        ),
        (
            "protection_prepared",
            "protection_submission_accepted",
            {
                "writer_attempted": True,
                "live_exposure": True,
                "required_protection_count": 2,
                "snapshot_complete": True,
            },
            "protection_pending_readback",
            "readback_only",
            "protection_submission_accepted",
        ),
        (
            "protection_prepared",
            "protection_submission_unknown",
            {
                "writer_attempted": True,
                "live_exposure": True,
                "required_protection_count": 2,
            },
            "protection_unknown",
            "readback_only",
            "protection_submission_unknown",
        ),
        (
            "protection_prepared",
            "protection_submission_rejected",
            {
                "writer_attempted": True,
                "live_exposure": True,
                "required_protection_count": 2,
            },
            "recovery_required",
            "supervision_only",
            "protection_submission_rejected",
        ),
        (
            "protection_pending_readback",
            "protection_readback_confirmed",
            {
                "writer_attempted": True,
                "live_exposure": True,
                "required_protection_count": 2,
                "confirmed_protection_count": 2,
                "snapshot_complete": True,
            },
            "protected",
            "none",
            "protection_fully_confirmed",
        ),
        (
            "protection_unknown",
            "protection_readback_confirmed",
            {
                "writer_attempted": True,
                "live_exposure": True,
                "required_protection_count": 2,
                "confirmed_protection_count": 2,
                "snapshot_complete": True,
            },
            "protected",
            "none",
            "protection_fully_confirmed",
        ),
        (
            "protected",
            "start_next_leg_preflight",
            {
                "writer_attempted": True,
                "live_exposure": True,
                "required_protection_count": 2,
                "confirmed_protection_count": 2,
                "snapshot_complete": True,
            },
            "next_leg_preflight",
            "readback_only",
            "next_leg_preflight_started",
        ),
        (
            "next_leg_preflight",
            "next_leg_preflight_ready",
            {
                "writer_attempted": True,
                "live_exposure": True,
                "required_protection_count": 2,
                "confirmed_protection_count": 2,
                "snapshot_complete": True,
            },
            "entry_prepared",
            "none",
            "next_leg_preflight_ready",
        ),
        (
            "next_leg_preflight",
            "next_leg_preflight_deferred",
            {
                "writer_attempted": True,
                "live_exposure": True,
                "required_protection_count": 2,
                "confirmed_protection_count": 2,
                "operation_deadline_expired": True,
            },
            "pre_submit_deferred",
            "defer",
            "next_leg_preflight_deferred",
        ),
        (
            "protected",
            "complete_entry_sequence",
            {
                "writer_attempted": True,
                "live_exposure": True,
                "required_protection_count": 2,
                "confirmed_protection_count": 2,
                "snapshot_complete": True,
            },
            "completed",
            "none",
            "entry_sequence_completed",
        ),
        (
            "entry_rejected",
            "confirm_submission_failed_no_exposure",
            {
                "writer_attempted": True,
                "snapshot_complete": True,
            },
            "submission_failed_no_exposure",
            "none",
            "submission_failed_no_exposure_confirmed",
        ),
        (
            "entry_unknown",
            "require_recovery",
            {"writer_attempted": True},
            "recovery_required",
            "supervision_only",
            "operation_recovery_required",
        ),
    ],
)
def test_valid_transition_table(
    current_state,
    event,
    facts,
    next_state,
    next_action,
    reason_code,
):
    transition = _decide(current_state, event, **facts)

    assert transition == EntryTransition(
        current_state=current_state,
        event=event,
        next_state=next_state,
        allowed=True,
        reason_code=reason_code,
        next_action=next_action,
    )


def test_one_of_two_confirmed_protections_does_not_make_aggregate_protected():
    transition = _decide(
        "protection_pending_readback",
        "protection_readback_confirmed",
        writer_attempted=True,
        live_exposure=True,
        required_protection_count=2,
        confirmed_protection_count=1,
        snapshot_complete=True,
    )

    assert transition.allowed is True
    assert transition.next_state == "protection_prepared"
    assert transition.next_action == "submit"
    assert transition.reason_code == "protection_partially_confirmed"


def test_partial_confirmation_after_unknown_writer_remains_readback_only():
    transition = _decide(
        "protection_unknown",
        "protection_readback_confirmed",
        writer_attempted=True,
        live_exposure=True,
        required_protection_count=2,
        confirmed_protection_count=1,
        snapshot_complete=True,
    )

    assert transition.allowed is True
    assert transition.next_state == "protection_unknown"
    assert transition.next_action == "readback_only"
    assert transition.reason_code == "protection_confirmation_partial_unknown"


def test_later_leg_submit_requires_current_complete_protection_and_fresh_writer():
    allowed = _decide(
        "entry_prepared",
        "request_entry_submit",
        live_exposure=True,
        required_protection_count=2,
        confirmed_protection_count=2,
        snapshot_complete=True,
    )
    incomplete = _decide(
        "entry_prepared",
        "request_entry_submit",
        live_exposure=True,
        required_protection_count=2,
        confirmed_protection_count=2,
        snapshot_complete=False,
    )

    assert allowed.allowed is True
    assert allowed.next_state == "entry_submitting"
    assert allowed.next_action == "submit"
    assert incomplete.allowed is False
    assert incomplete.reason_code == "protection_not_fully_confirmed"


def test_entry_confirmed_without_writer_fact_cannot_authorize_protection_submit():
    transition = _decide(
        "entry_confirmed",
        "prepare_protection",
        writer_attempted=False,
        live_exposure=True,
        required_protection_count=2,
        snapshot_complete=True,
    )

    assert transition.allowed is False
    assert transition.reason_code == "entry_writer_not_attempted"
    assert transition.next_action == "none"


@pytest.mark.parametrize(
    ("state", "event", "facts", "reason_code"),
    [
        (
            "entry_unknown",
            "request_entry_submit",
            {"writer_attempted": True},
            "illegal_transition",
        ),
        (
            "protection_pending_readback",
            "start_next_leg_preflight",
            {
                "live_exposure": True,
                "writer_attempted": True,
                "required_protection_count": 2,
                "confirmed_protection_count": 1,
                "snapshot_complete": True,
            },
            "illegal_transition",
        ),
        (
            "pre_submit_deferred",
            "request_entry_submit",
            {
                "live_exposure": True,
                "writer_attempted": True,
                "required_protection_count": 2,
                "confirmed_protection_count": 2,
                "snapshot_complete": True,
            },
            "illegal_transition",
        ),
        (
            "entry_rejected",
            "confirm_submission_failed_no_exposure",
            {
                "live_exposure": True,
                "writer_attempted": True,
                "snapshot_complete": True,
            },
            "live_exposure_present",
        ),
        (
            "protection_pending_readback",
            "protection_readback_confirmed",
            {
                "live_exposure": True,
                "writer_attempted": True,
                "required_protection_count": 2,
                "confirmed_protection_count": 2,
                "snapshot_complete": False,
            },
            "snapshot_incomplete",
        ),
        (
            "entry_prepared",
            "request_entry_submit",
            {"writer_attempted": True},
            "entry_writer_already_attempted",
        ),
        (
            "entry_prepared",
            "request_entry_submit",
            {"operation_deadline_expired": True},
            "operation_deadline_expired",
        ),
    ],
)
def test_high_risk_transitions_fail_closed(state, event, facts, reason_code):
    transition = _decide(state, event, **facts)

    assert transition.allowed is False
    assert transition.next_state == state
    assert transition.next_action == "none"
    assert transition.reason_code == reason_code


VALID_SOURCE_EVENTS = {
    ("planned", "prepare_entry"),
    ("entry_prepared", "request_entry_submit"),
    ("entry_submitting", "entry_submission_accepted"),
    ("entry_submitting", "entry_submission_rejected"),
    ("entry_submitting", "entry_submission_unknown"),
    ("entry_pending_readback", "entry_readback_confirmed"),
    ("entry_unknown", "entry_readback_confirmed"),
    ("entry_rejected", "confirm_submission_failed_no_exposure"),
    ("entry_confirmed", "prepare_protection"),
    ("protection_prepared", "protection_submission_accepted"),
    ("protection_prepared", "protection_submission_rejected"),
    ("protection_prepared", "protection_submission_unknown"),
    ("protection_pending_readback", "protection_readback_confirmed"),
    ("protection_unknown", "protection_readback_confirmed"),
    ("protected", "start_next_leg_preflight"),
    ("next_leg_preflight", "next_leg_preflight_ready"),
    ("next_leg_preflight", "next_leg_preflight_deferred"),
    ("protected", "complete_entry_sequence"),
}


def test_every_unlisted_state_event_pair_is_refused():
    for state in sorted(STATES):
        for event in sorted(EVENTS - {"require_recovery"}):
            if (state, event) in VALID_SOURCE_EVENTS:
                continue
            transition = _decide(state, event)
            assert transition.allowed is False, (state, event, transition)
            assert transition.next_state == state
            assert transition.next_action == "none"
            assert transition.reason_code == "illegal_transition"


@pytest.mark.parametrize(
    ("facts", "code"),
    [
        (
            {
                "live_exposure": 1,
                "writer_attempted": False,
                "required_protection_count": 0,
                "confirmed_protection_count": 0,
                "snapshot_complete": False,
                "operation_deadline_expired": False,
            },
            "live_exposure_invalid",
        ),
        (
            {
                "live_exposure": False,
                "writer_attempted": False,
                "required_protection_count": 1,
                "confirmed_protection_count": 2,
                "snapshot_complete": False,
                "operation_deadline_expired": False,
            },
            "protection_count_invalid",
        ),
    ],
)
def test_invalid_fact_types_and_counts_raise_only_bounded_codes(facts, code):
    with pytest.raises(ProtectedEntryDecisionError) as raised:
        decide_protected_entry_transition(
            current_state="planned",
            event="prepare_entry",
            facts=ProtectedEntryFacts(**facts),
        )
    assert str(raised.value) == code


@pytest.mark.parametrize(
    ("state", "event", "code"),
    [
        ("planned; submit now", "prepare_entry", "current_state_invalid"),
        ("planned", "timeout, retry please", "event_invalid"),
    ],
)
def test_unknown_control_strings_raise_only_bounded_codes(state, event, code):
    with pytest.raises(ProtectedEntryDecisionError) as raised:
        decide_protected_entry_transition(
            current_state=state,
            event=event,
            facts=_facts(),
        )
    assert str(raised.value) == code


def test_recovery_requires_an_unknown_writer_or_live_exposure():
    refused = _decide("entry_prepared", "require_recovery")
    allowed = _decide(
        "entry_submitting",
        "require_recovery",
        writer_attempted=True,
    )

    assert refused.allowed is False
    assert refused.reason_code == "recovery_not_required"
    assert allowed.allowed is True
    assert allowed.next_state == "recovery_required"
    assert allowed.next_action == "supervision_only"


@pytest.mark.parametrize(
    "state",
    [
        "pre_submit_deferred",
        "completed",
        "recovery_required",
        "submission_failed_no_exposure",
    ],
)
def test_terminal_states_cannot_be_reactivated_by_any_event(state):
    facts = {
        "live_exposure": state != "submission_failed_no_exposure",
        "writer_attempted": True,
        "required_protection_count": 2,
        "confirmed_protection_count": 2,
        "snapshot_complete": True,
    }
    for event in sorted(EVENTS):
        transition = _decide(state, event, **facts)
        assert transition.allowed is False, (state, event, transition)
        assert transition.next_state == state
        assert transition.next_action == "none"
        assert transition.reason_code == "illegal_transition"
