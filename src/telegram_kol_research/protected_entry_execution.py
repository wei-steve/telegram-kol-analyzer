"""Pure, closed transition policy for future protected-entry operations."""

from __future__ import annotations

from dataclasses import dataclass


PROTECTED_ENTRY_STATES = frozenset(
    {
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
)

PROTECTED_ENTRY_EVENTS = frozenset(
    {
        "prepare_entry",
        "request_entry_submit",
        "entry_submission_accepted",
        "entry_submission_rejected",
        "entry_submission_unknown",
        "entry_readback_confirmed",
        "prepare_protection",
        "request_protection_submit",
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
)

PROTECTED_ENTRY_ACTIONS = frozenset(
    {"submit", "readback_only", "defer", "supervision_only", "none"}
)

_TERMINAL_STATES = frozenset(
    {
        "pre_submit_deferred",
        "completed",
        "recovery_required",
        "submission_failed_no_exposure",
    }
)


class ProtectedEntryDecisionError(ValueError):
    """The decision input is outside the closed protected-entry contract."""


@dataclass(frozen=True, slots=True)
class ProtectedEntryFacts:
    """Closed facts for the operation phase handling the current event.

    ``writer_attempted`` is phase-local.  A caller entering a newly prepared
    child operation must load that child's durable writer fact instead of
    inheriting the parent entry or protection attempt.
    """

    live_exposure: bool
    writer_attempted: bool
    required_protection_count: int
    confirmed_protection_count: int
    snapshot_complete: bool
    operation_deadline_expired: bool


@dataclass(frozen=True, slots=True)
class EntryTransition:
    current_state: str
    event: str
    next_state: str
    allowed: bool
    reason_code: str
    next_action: str


def decide_protected_entry_transition(
    *,
    current_state: str,
    event: str,
    facts: ProtectedEntryFacts,
) -> EntryTransition:
    """Decide one transition from closed durable facts without performing I/O."""

    state = _closed_control_value(
        current_state,
        allowed=PROTECTED_ENTRY_STATES,
        code="current_state_invalid",
    )
    transition_event = _closed_control_value(
        event,
        allowed=PROTECTED_ENTRY_EVENTS,
        code="event_invalid",
    )
    _validate_facts(facts)

    if transition_event == "require_recovery":
        return _decide_recovery(state=state, event=transition_event, facts=facts)

    handler = _TRANSITION_HANDLERS.get((state, transition_event))
    if handler is None:
        return _refused(state, transition_event, "illegal_transition")
    return handler(state, transition_event, facts)


def _prepare_entry(state: str, event: str, facts: ProtectedEntryFacts) -> EntryTransition:
    if facts.live_exposure or facts.writer_attempted:
        return _refused(state, event, "entry_plan_facts_inconsistent")
    return _allowed(state, event, "entry_prepared", "entry_intent_prepared")


def _request_entry_submit(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    if facts.writer_attempted:
        return _refused(state, event, "entry_writer_already_attempted")
    if facts.operation_deadline_expired:
        return _refused(state, event, "operation_deadline_expired")
    if facts.live_exposure:
        return _refused(state, event, "unexpected_live_exposure")
    return _allowed(
        state,
        event,
        "entry_submitting",
        "entry_submit_authorized",
        next_action="submit",
    )


def _entry_submission_accepted(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    if not facts.writer_attempted:
        return _refused(state, event, "entry_writer_not_attempted")
    return _allowed(
        state,
        event,
        "entry_pending_readback",
        "entry_submission_accepted",
        next_action="readback_only",
    )


def _entry_submission_rejected(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    if not facts.writer_attempted:
        return _refused(state, event, "entry_writer_not_attempted")
    return _allowed(
        state,
        event,
        "entry_rejected",
        "entry_submission_rejected",
    )


def _entry_submission_unknown(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    if not facts.writer_attempted:
        return _refused(state, event, "entry_writer_not_attempted")
    return _allowed(
        state,
        event,
        "entry_unknown",
        "entry_submission_unknown",
        next_action="readback_only",
    )


def _entry_readback_confirmed(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    if not facts.writer_attempted:
        return _refused(state, event, "entry_writer_not_attempted")
    if not facts.snapshot_complete:
        return _refused(state, event, "snapshot_incomplete")
    if not facts.live_exposure:
        return _refused(state, event, "live_exposure_not_confirmed")
    return _allowed(
        state,
        event,
        "entry_confirmed",
        "entry_readback_confirmed",
    )


def _prepare_protection(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    if not facts.writer_attempted:
        return _refused(state, event, "entry_writer_not_attempted")
    if not facts.live_exposure:
        return _refused(state, event, "live_exposure_not_confirmed")
    if facts.operation_deadline_expired:
        return _allowed(
            state,
            event,
            "recovery_required",
            "protection_deadline_expired",
            next_action="supervision_only",
        )
    refusal = _protection_base_refusal(facts, require_complete_snapshot=True)
    if refusal is not None:
        return _refused(state, event, refusal)
    if facts.confirmed_protection_count >= facts.required_protection_count:
        return _refused(state, event, "protection_already_confirmed")
    return _allowed(
        state,
        event,
        "protection_prepared",
        "protection_intents_prepared",
    )


def _request_protection_submit(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    if facts.writer_attempted:
        return _refused(state, event, "protection_writer_already_attempted")
    if not facts.live_exposure:
        return _refused(state, event, "live_exposure_not_confirmed")
    if facts.operation_deadline_expired:
        return _allowed(
            state,
            event,
            "recovery_required",
            "protection_deadline_expired",
            next_action="supervision_only",
        )
    refusal = _protection_base_refusal(facts, require_complete_snapshot=True)
    if refusal is not None:
        return _refused(state, event, refusal)
    if facts.confirmed_protection_count >= facts.required_protection_count:
        return _refused(state, event, "protection_already_confirmed")
    return _allowed(
        state,
        event,
        "protection_prepared",
        "protection_submit_authorized",
        next_action="submit",
    )


def _protection_submission_accepted(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    refusal = _protection_writer_refusal(facts)
    if refusal is not None:
        return _refused(state, event, refusal)
    return _allowed(
        state,
        event,
        "protection_pending_readback",
        "protection_submission_accepted",
        next_action="readback_only",
    )


def _protection_submission_unknown(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    refusal = _protection_writer_refusal(facts)
    if refusal is not None:
        return _refused(state, event, refusal)
    return _allowed(
        state,
        event,
        "protection_unknown",
        "protection_submission_unknown",
        next_action="readback_only",
    )


def _protection_submission_rejected(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    refusal = _protection_writer_refusal(facts)
    if refusal is not None:
        return _refused(state, event, refusal)
    return _allowed(
        state,
        event,
        "recovery_required",
        "protection_submission_rejected",
        next_action="supervision_only",
    )


def _protection_readback_confirmed(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    refusal = _protection_base_refusal(facts, require_complete_snapshot=True)
    if refusal is not None:
        return _refused(state, event, refusal)
    if not facts.writer_attempted:
        return _refused(state, event, "protection_writer_not_attempted")
    if facts.confirmed_protection_count == 0:
        return _refused(state, event, "protection_confirmation_missing")
    if facts.confirmed_protection_count == facts.required_protection_count:
        return _allowed(
            state,
            event,
            "protected",
            "protection_fully_confirmed",
        )
    if facts.operation_deadline_expired:
        return _allowed(
            state,
            event,
            "recovery_required",
            "protection_deadline_expired",
            next_action="supervision_only",
        )
    if state == "protection_unknown":
        return _allowed(
            state,
            event,
            "protection_unknown",
            "protection_confirmation_partial_unknown",
            next_action="readback_only",
        )
    return _allowed(
        state,
        event,
        "protection_prepared",
        "protection_partially_confirmed",
    )


def _start_next_leg_preflight(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    refusal = _protected_action_refusal(facts)
    if refusal is not None:
        return _refused(state, event, refusal)
    if facts.operation_deadline_expired:
        return _refused(state, event, "operation_deadline_expired")
    return _allowed(
        state,
        event,
        "next_leg_preflight",
        "next_leg_preflight_started",
        next_action="readback_only",
    )


def _next_leg_preflight_ready(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    refusal = _protected_action_refusal(facts)
    if refusal is not None:
        return _refused(state, event, refusal)
    if facts.writer_attempted:
        return _refused(state, event, "entry_writer_already_attempted")
    if facts.operation_deadline_expired:
        return _refused(state, event, "operation_deadline_expired")
    return _allowed(
        state,
        event,
        "entry_submitting",
        "next_leg_submit_authorized",
        next_action="submit",
    )


def _next_leg_preflight_deferred(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    if not facts.live_exposure:
        return _refused(state, event, "live_exposure_not_confirmed")
    if facts.writer_attempted:
        return _refused(state, event, "entry_writer_already_attempted")
    if not _protection_counts_complete(facts):
        return _refused(state, event, "protection_not_fully_confirmed")
    if not facts.operation_deadline_expired:
        return _refused(state, event, "operation_deadline_active")
    return _allowed(
        state,
        event,
        "pre_submit_deferred",
        "next_leg_preflight_deferred",
        next_action="defer",
    )


def _complete_entry_sequence(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    refusal = _protected_action_refusal(facts)
    if refusal is not None:
        return _refused(state, event, refusal)
    return _allowed(
        state,
        event,
        "completed",
        "entry_sequence_completed",
    )


def _confirm_submission_failed_no_exposure(
    state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    if not facts.writer_attempted:
        return _refused(state, event, "entry_writer_not_attempted")
    if not facts.snapshot_complete:
        return _refused(state, event, "snapshot_incomplete")
    if facts.live_exposure:
        return _refused(state, event, "live_exposure_present")
    return _allowed(
        state,
        event,
        "submission_failed_no_exposure",
        "submission_failed_no_exposure_confirmed",
    )


def _decide_recovery(
    *, state: str, event: str, facts: ProtectedEntryFacts
) -> EntryTransition:
    if state in _TERMINAL_STATES:
        return _refused(state, event, "illegal_transition")
    if not facts.writer_attempted and not facts.live_exposure:
        return _refused(state, event, "recovery_not_required")
    return _allowed(
        state,
        event,
        "recovery_required",
        "operation_recovery_required",
        next_action="supervision_only",
    )


def _protection_writer_refusal(facts: ProtectedEntryFacts) -> str | None:
    refusal = _protection_base_refusal(facts, require_complete_snapshot=False)
    if refusal is not None:
        return refusal
    if not facts.writer_attempted:
        return "protection_writer_not_attempted"
    if facts.confirmed_protection_count >= facts.required_protection_count:
        return "protection_already_confirmed"
    return None


def _protection_base_refusal(
    facts: ProtectedEntryFacts, *, require_complete_snapshot: bool
) -> str | None:
    if not facts.live_exposure:
        return "live_exposure_not_confirmed"
    if facts.required_protection_count == 0:
        return "protection_requirements_missing"
    if require_complete_snapshot and not facts.snapshot_complete:
        return "snapshot_incomplete"
    return None


def _protected_action_refusal(facts: ProtectedEntryFacts) -> str | None:
    if not facts.live_exposure:
        return "live_exposure_not_confirmed"
    if not facts.snapshot_complete:
        return "snapshot_incomplete"
    if not _protection_counts_complete(facts):
        return "protection_not_fully_confirmed"
    return None


def _protection_counts_complete(facts: ProtectedEntryFacts) -> bool:
    return (
        facts.required_protection_count > 0
        and facts.confirmed_protection_count == facts.required_protection_count
    )


def _validate_facts(facts: ProtectedEntryFacts) -> None:
    if not isinstance(facts, ProtectedEntryFacts):
        raise ProtectedEntryDecisionError("facts_invalid")
    for name in (
        "live_exposure",
        "writer_attempted",
        "snapshot_complete",
        "operation_deadline_expired",
    ):
        if type(getattr(facts, name)) is not bool:
            raise ProtectedEntryDecisionError(f"{name}_invalid")
    required = facts.required_protection_count
    confirmed = facts.confirmed_protection_count
    if (
        type(required) is not int
        or type(confirmed) is not int
        or required < 0
        or confirmed < 0
        or confirmed > required
    ):
        raise ProtectedEntryDecisionError("protection_count_invalid")


def _closed_control_value(value: object, *, allowed: frozenset[str], code: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ProtectedEntryDecisionError(code)
    return value


def _allowed(
    current_state: str,
    event: str,
    next_state: str,
    reason_code: str,
    *,
    next_action: str = "none",
) -> EntryTransition:
    return EntryTransition(
        current_state=current_state,
        event=event,
        next_state=next_state,
        allowed=True,
        reason_code=reason_code,
        next_action=next_action,
    )


def _refused(current_state: str, event: str, reason_code: str) -> EntryTransition:
    return EntryTransition(
        current_state=current_state,
        event=event,
        next_state=current_state,
        allowed=False,
        reason_code=reason_code,
        next_action="none",
    )


_TRANSITION_HANDLERS = {
    ("planned", "prepare_entry"): _prepare_entry,
    ("entry_prepared", "request_entry_submit"): _request_entry_submit,
    ("entry_submitting", "entry_submission_accepted"): _entry_submission_accepted,
    ("entry_submitting", "entry_submission_rejected"): _entry_submission_rejected,
    ("entry_submitting", "entry_submission_unknown"): _entry_submission_unknown,
    ("entry_pending_readback", "entry_readback_confirmed"): (
        _entry_readback_confirmed
    ),
    ("entry_unknown", "entry_readback_confirmed"): _entry_readback_confirmed,
    ("entry_rejected", "confirm_submission_failed_no_exposure"): (
        _confirm_submission_failed_no_exposure
    ),
    ("entry_confirmed", "prepare_protection"): _prepare_protection,
    ("protection_prepared", "request_protection_submit"): (
        _request_protection_submit
    ),
    ("protection_prepared", "protection_submission_accepted"): (
        _protection_submission_accepted
    ),
    ("protection_prepared", "protection_submission_rejected"): (
        _protection_submission_rejected
    ),
    ("protection_prepared", "protection_submission_unknown"): (
        _protection_submission_unknown
    ),
    ("protection_pending_readback", "protection_readback_confirmed"): (
        _protection_readback_confirmed
    ),
    ("protection_unknown", "protection_readback_confirmed"): (
        _protection_readback_confirmed
    ),
    ("protected", "start_next_leg_preflight"): _start_next_leg_preflight,
    ("next_leg_preflight", "next_leg_preflight_ready"): (
        _next_leg_preflight_ready
    ),
    ("next_leg_preflight", "next_leg_preflight_deferred"): (
        _next_leg_preflight_deferred
    ),
    ("protected", "complete_entry_sequence"): _complete_entry_sequence,
}
