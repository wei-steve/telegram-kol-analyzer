from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone

import pytest

from telegram_kol_research.bound_close_reservation_recovery import (
    ACTIVE_REASONS,
    PROVEN_TERMINAL_REASONS,
    UNKNOWN_REASONS,
    BoundCloseReservationObservation,
    BoundCloseReservationRecoveryPlan,
    ReservationClassification,
    _canonical_json,
    _sha256_json,
)


FINGERPRINT_A = "a" * 64
FINGERPRINT_B = "b" * 64
FINGERPRINT_C = "c" * 64
FINGERPRINT_D = "d" * 64


def _observation(
    *,
    classification: ReservationClassification = (
        ReservationClassification.PROVEN_TERMINAL
    ),
    reason_code: str = "exact_close_and_position_terminal",
    reservation_ref: str = FINGERPRINT_A,
) -> BoundCloseReservationObservation:
    return BoundCloseReservationObservation(
        reservation_ref=reservation_ref,
        classification=classification,
        reason_code=reason_code,
        source_fingerprint=FINGERPRINT_B,
        exchange_fingerprint=FINGERPRINT_C,
    )


def _plan(
    *,
    observations: tuple[BoundCloseReservationObservation, ...] | None = None,
    status: str = "ready",
    schema_version: int = 1,
    action_count: int | None = None,
) -> BoundCloseReservationRecoveryPlan:
    selected = observations if observations is not None else (_observation(),)
    return BoundCloseReservationRecoveryPlan(
        schema_version=schema_version,
        status=status,
        observations=selected,
        source_fingerprint=FINGERPRINT_A,
        exchange_snapshot_fingerprint=FINGERPRINT_B,
        evidence_fingerprint=FINGERPRINT_C,
        confirmation_token=FINGERPRINT_D,
        action_count=len(selected) if action_count is None else action_count,
    )


def test_contract_exposes_only_the_three_closed_classifications():
    assert [(item.name, item.value) for item in ReservationClassification] == [
        ("PROVEN_TERMINAL", "PROVEN_TERMINAL"),
        ("ACTIVE", "ACTIVE"),
        ("UNKNOWN", "UNKNOWN"),
    ]


def test_contract_reason_code_sets_are_exact_and_closed():
    assert PROVEN_TERMINAL_REASONS == frozenset(
        {"exact_close_and_position_terminal"}
    )
    assert ACTIVE_REASONS == frozenset(
        {
            "exact_position_currently_live",
            "exact_close_order_currently_pending",
            "exact_close_order_nonterminal",
        }
    )
    assert UNKNOWN_REASONS == frozenset(
        {
            "local_evidence_incomplete",
            "local_identity_conflict",
            "exchange_evidence_unavailable",
            "exchange_schema_invalid",
            "exchange_identity_conflict",
            "exchange_history_incomplete",
            "exchange_capture_timeout",
            "exchange_response_size_exceeded",
            "exchange_state_conflict",
        }
    )


def test_contract_records_have_exact_fields_are_frozen_and_use_slots():
    observation = _observation()
    plan = _plan()

    assert [field.name for field in fields(observation)] == [
        "reservation_ref",
        "classification",
        "reason_code",
        "source_fingerprint",
        "exchange_fingerprint",
    ]
    assert [field.name for field in fields(plan)] == [
        "schema_version",
        "status",
        "observations",
        "source_fingerprint",
        "exchange_snapshot_fingerprint",
        "evidence_fingerprint",
        "confirmation_token",
        "action_count",
    ]
    assert not hasattr(observation, "__dict__")
    assert not hasattr(plan, "__dict__")
    with pytest.raises(FrozenInstanceError):
        observation.reason_code = "exchange_state_conflict"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.status = "refused"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("classification", "reason_code"),
    [
        (
            ReservationClassification.PROVEN_TERMINAL,
            "exact_close_and_position_terminal",
        ),
        (
            ReservationClassification.ACTIVE,
            "exact_position_currently_live",
        ),
        (
            ReservationClassification.ACTIVE,
            "exact_close_order_currently_pending",
        ),
        (
            ReservationClassification.ACTIVE,
            "exact_close_order_nonterminal",
        ),
        (
            ReservationClassification.UNKNOWN,
            "local_evidence_incomplete",
        ),
        (
            ReservationClassification.UNKNOWN,
            "exchange_state_conflict",
        ),
    ],
)
def test_observation_accepts_only_a_reason_for_its_classification(
    classification, reason_code
):
    observation = _observation(
        classification=classification,
        reason_code=reason_code,
    )
    assert observation.reason_code == reason_code


@pytest.mark.parametrize(
    ("classification", "reason_code"),
    [
        (ReservationClassification.PROVEN_TERMINAL, "exchange_state_conflict"),
        (ReservationClassification.ACTIVE, "exact_close_and_position_terminal"),
        (ReservationClassification.UNKNOWN, "exact_position_currently_live"),
        (ReservationClassification.UNKNOWN, "future_extension"),
    ],
)
def test_observation_rejects_unknown_or_cross_classification_reason_codes(
    classification, reason_code
):
    with pytest.raises(ValueError, match="reason_code"):
        _observation(classification=classification, reason_code=reason_code)


def test_observation_requires_the_exact_enum_type():
    with pytest.raises(TypeError, match="classification"):
        _observation(  # type: ignore[arg-type]
            classification="PROVEN_TERMINAL",
        )


@pytest.mark.parametrize(
    "invalid",
    [
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        "",
        1,
        True,
        None,
    ],
)
@pytest.mark.parametrize(
    "field_name",
    [
        "reservation_ref",
        "source_fingerprint",
        "exchange_fingerprint",
    ],
)
def test_observation_requires_lowercase_64_hex_references_and_fingerprints(
    field_name, invalid
):
    values = {
        "reservation_ref": FINGERPRINT_A,
        "classification": ReservationClassification.PROVEN_TERMINAL,
        "reason_code": "exact_close_and_position_terminal",
        "source_fingerprint": FINGERPRINT_B,
        "exchange_fingerprint": FINGERPRINT_C,
    }
    values[field_name] = invalid

    with pytest.raises((TypeError, ValueError), match=field_name):
        BoundCloseReservationObservation(**values)


@pytest.mark.parametrize(
    "field_name",
    [
        "source_fingerprint",
        "exchange_snapshot_fingerprint",
        "evidence_fingerprint",
    ],
)
def test_plan_requires_lowercase_64_hex_fingerprints(field_name):
    values = {
        "schema_version": 1,
        "status": "ready",
        "observations": (_observation(),),
        "source_fingerprint": FINGERPRINT_A,
        "exchange_snapshot_fingerprint": FINGERPRINT_B,
        "evidence_fingerprint": FINGERPRINT_C,
        "confirmation_token": FINGERPRINT_D,
        "action_count": 1,
    }
    values[field_name] = "F" * 64

    with pytest.raises(ValueError, match=field_name):
        BoundCloseReservationRecoveryPlan(**values)


@pytest.mark.parametrize("invalid", [True, 1.0, "1", 0, 2])
def test_plan_schema_version_is_the_exact_integer_one(invalid):
    with pytest.raises((TypeError, ValueError), match="schema_version"):
        _plan(schema_version=invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid", [True, 1.0, "1", -1])
def test_plan_action_count_is_a_strict_nonnegative_integer(invalid):
    with pytest.raises((TypeError, ValueError), match="action_count"):
        _plan(action_count=invalid)  # type: ignore[arg-type]


def test_ready_plan_requires_a_nonempty_all_terminal_population():
    assert _plan().status == "ready"

    with pytest.raises(ValueError, match="status"):
        _plan(observations=(), status="ready", action_count=0)
    with pytest.raises(ValueError, match="status"):
        _plan(
            observations=(
                _observation(
                    classification=ReservationClassification.ACTIVE,
                    reason_code="exact_position_currently_live",
                ),
            ),
            status="ready",
            action_count=1,
        )
    with pytest.raises(ValueError, match="status"):
        _plan(status="refused", action_count=0)


@pytest.mark.parametrize(
    "observations",
    [
        (),
        (
            _observation(
                classification=ReservationClassification.ACTIVE,
                reason_code="exact_position_currently_live",
            ),
        ),
        (
            _observation(
                classification=ReservationClassification.UNKNOWN,
                reason_code="exchange_history_incomplete",
            ),
        ),
    ],
)
def test_refused_plan_is_required_for_every_not_ready_population(observations):
    plan = _plan(
        observations=observations,
        status="refused",
        action_count=0,
    )
    assert plan.status == "refused"
    assert plan.action_count == 0


def test_ready_action_count_equals_the_number_of_observations():
    with pytest.raises(ValueError, match="action_count"):
        _plan(action_count=0)
    with pytest.raises(ValueError, match="action_count"):
        _plan(action_count=2)


def test_refused_action_count_must_be_zero():
    active = _observation(
        classification=ReservationClassification.ACTIVE,
        reason_code="exact_close_order_nonterminal",
    )
    with pytest.raises(ValueError, match="action_count"):
        _plan(observations=(active,), status="refused", action_count=1)


def test_plan_requires_an_exact_tuple_and_at_most_64_observations():
    with pytest.raises(TypeError, match="observations"):
        _plan(observations=[_observation()])  # type: ignore[arg-type]

    observations = tuple(
        _observation(reservation_ref=f"{index:064x}") for index in range(64)
    )
    assert _plan(observations=observations).action_count == 64

    with pytest.raises(ValueError, match="64"):
        _plan(observations=observations + (_observation(),))


def test_contract_constructors_reject_unknown_fields():
    with pytest.raises(TypeError, match="unexpected"):
        BoundCloseReservationObservation(  # type: ignore[call-arg]
            reservation_ref=FINGERPRINT_A,
            classification=ReservationClassification.PROVEN_TERMINAL,
            reason_code="exact_close_and_position_terminal",
            source_fingerprint=FINGERPRINT_B,
            exchange_fingerprint=FINGERPRINT_C,
            unexpected="not-allowed",
        )
    with pytest.raises(TypeError, match="unexpected"):
        BoundCloseReservationRecoveryPlan(  # type: ignore[call-arg]
            schema_version=1,
            status="ready",
            observations=(_observation(),),
            source_fingerprint=FINGERPRINT_A,
            exchange_snapshot_fingerprint=FINGERPRINT_B,
            evidence_fingerprint=FINGERPRINT_C,
            confirmation_token=FINGERPRINT_D,
            action_count=1,
            unexpected="not-allowed",
        )


def test_canonical_json_is_stable_and_accepts_only_aware_utc_times():
    captured_at = datetime(2026, 8, 15, 12, 34, 56, 123456, tzinfo=UTC)
    left = {"captured_at": captured_at, "enabled": True, "count": 1}
    right = {"count": 1, "enabled": True, "captured_at": captured_at}

    assert _canonical_json(left) == (
        '{"captured_at":"2026-08-15T12:34:56.123456Z",'
        '"count":1,"enabled":true}'
    )
    assert _sha256_json(left) == _sha256_json(right)

    with pytest.raises(ValueError, match="aware UTC"):
        _canonical_json({"captured_at": captured_at.replace(tzinfo=None)})
    with pytest.raises(ValueError, match="aware UTC"):
        _canonical_json(
            {
                "captured_at": captured_at.astimezone(
                    timezone(timedelta(hours=1))
                )
            }
        )


def test_confirmation_token_must_be_a_nonempty_string():
    values = {
        "schema_version": 1,
        "status": "ready",
        "observations": (_observation(),),
        "source_fingerprint": FINGERPRINT_A,
        "exchange_snapshot_fingerprint": FINGERPRINT_B,
        "evidence_fingerprint": FINGERPRINT_C,
        "confirmation_token": "",
        "action_count": 1,
    }
    with pytest.raises(ValueError, match="confirmation_token"):
        BoundCloseReservationRecoveryPlan(**values)
    values["confirmation_token"] = True
    with pytest.raises(TypeError, match="confirmation_token"):
        BoundCloseReservationRecoveryPlan(**values)
