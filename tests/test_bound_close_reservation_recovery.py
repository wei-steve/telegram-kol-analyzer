from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import copy
import gc
import json
import pickle
import signal
import sqlite3
import threading
import time
import weakref

import httpx
import pytest

import telegram_kol_research.bound_close_reservation_recovery as recovery_module

from telegram_kol_research.deepcoin_client import (
    DeepcoinCredentials,
    DeepcoinRestClient,
    DeepcoinRequestScope,
    _build_deepcoin_bound_close_reservation_recovery_client_from_env,
)
from telegram_kol_research.deepcoin_request_policy import RequestPriority
from telegram_kol_research.bound_close_reservation_recovery import (
    ACTIVE_REASONS,
    PROVEN_TERMINAL_REASONS,
    UNKNOWN_REASONS,
    BoundCloseReservationObservation,
    BoundCloseReservationRecoveryPlan,
    BoundCloseReservationSource,
    BoundCloseReservationExchangeReader,
    BoundCloseReservationExchangeConfigurationError,
    BoundCloseReservationExchangeDeadlineExceeded,
    ExchangeCloseOrderState,
    ExchangeFillEvidence,
    ExchangeOrderEvidence,
    ExchangePositionEvidence,
    ExchangePositionHistoryEvidence,
    ExchangePositionHistoryState,
    ExchangeReservationEvidence,
    LocalCloseMutationEvidence,
    LocalReservationEvidence,
    ReservationClassification,
    SealedRecoveryCapture,
    _claim_sealed_recovery_capture_for_apply,
    _canonical_json,
    _seal_bound_close_reservation_recovery_capture,
    _sha256_json,
    build_bound_close_reservation_recovery_plan,
    build_bound_close_reservation_exchange_reader_from_env,
    classify_bound_close_reservation,
    exchange_recovery_reason_from_error,
    load_bound_close_reservation_source,
    serialize_bound_close_reservation_recovery_plan,
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


CLASSIFICATION_TIME = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)


def _local_classification_evidence(
    *,
    binding_status: str = "closed",
    local_reason_code: str | None = None,
    mutation_status: str = "confirmed",
    mutation_order_ref: str | None = FINGERPRINT_D,
    reservation_created_at: datetime = CLASSIFICATION_TIME,
    event_created_at: datetime = CLASSIFICATION_TIME,
) -> LocalReservationEvidence:
    mutation = LocalCloseMutationEvidence(
        mutation_ref="e" * 64,
        status=mutation_status,
        order_ref=mutation_order_ref,
        reserved_at=CLASSIFICATION_TIME,
        submitted_at=(
            CLASSIFICATION_TIME
            if mutation_status in {"submitted", "confirmed"}
            else None
        ),
        confirmed_at=(
            CLASSIFICATION_TIME if mutation_status == "confirmed" else None
        ),
        created_at=CLASSIFICATION_TIME,
        updated_at=CLASSIFICATION_TIME,
        record_fingerprint="f" * 64,
    )
    return LocalReservationEvidence(
        reservation_ref=FINGERPRINT_A,
        source_status="submitted",
        local_reason_code=local_reason_code,
        reservation_created_at=reservation_created_at,
        reservation_updated_at=reservation_created_at,
        reservation_record_fingerprint=FINGERPRINT_B,
        binding_ref=FINGERPRINT_C,
        binding_status=binding_status,
        instrument_ref=FINGERPRINT_B,
        side="long",
        binding_record_fingerprint=FINGERPRINT_C,
        position_ref=FINGERPRINT_C,
        close_event_ref=FINGERPRINT_D,
        close_order_ref=FINGERPRINT_D,
        event_created_at=event_created_at,
        close_event_record_fingerprint="e" * 64,
        entry_leg=None,
        close_mutations=(mutation,),
    )


def _order_evidence(
    *,
    state: ExchangeCloseOrderState = ExchangeCloseOrderState.FILLED,
    instrument_ref: str = FINGERPRINT_B,
    side: str = "long",
    position_ref: str = FINGERPRINT_C,
    order_ref: str = FINGERPRINT_D,
    requested_quantity: Decimal = Decimal("1"),
    filled_quantity: Decimal = Decimal("1.00"),
    terminal_at: datetime | None = CLASSIFICATION_TIME + timedelta(minutes=1),
) -> ExchangeOrderEvidence:
    return ExchangeOrderEvidence(
        instrument_ref=instrument_ref,
        side=side,
        position_ref=position_ref,
        order_ref=order_ref,
        state=state,
        requested_quantity=requested_quantity,
        filled_quantity=filled_quantity,
        terminal_at=terminal_at,
    )


def _fill_evidence(
    *,
    instrument_ref: str = FINGERPRINT_B,
    side: str = "long",
    position_ref: str = FINGERPRINT_C,
    order_ref: str = FINGERPRINT_D,
    quantity: Decimal = Decimal("1.000"),
    filled_at: datetime = CLASSIFICATION_TIME + timedelta(minutes=1),
) -> ExchangeFillEvidence:
    return ExchangeFillEvidence(
        instrument_ref=instrument_ref,
        side=side,
        position_ref=position_ref,
        order_ref=order_ref,
        quantity=quantity,
        filled_at=filled_at,
    )


def _position_history_evidence(
    *,
    instrument_ref: str = FINGERPRINT_B,
    side: str = "long",
    position_ref: str = FINGERPRINT_C,
    closed_quantity: Decimal = Decimal("1.0"),
    closed_at: datetime = CLASSIFICATION_TIME + timedelta(minutes=2),
    state: ExchangePositionHistoryState = ExchangePositionHistoryState.CLOSED,
) -> ExchangePositionHistoryEvidence:
    return ExchangePositionHistoryEvidence(
        instrument_ref=instrument_ref,
        side=side,
        position_ref=position_ref,
        state=state,
        closed_quantity=closed_quantity,
        closed_at=closed_at,
    )


def _current_position_evidence(
    *,
    instrument_ref: str = FINGERPRINT_B,
    side: str = "long",
    position_ref: str = FINGERPRINT_C,
    quantity: Decimal = Decimal("1"),
) -> ExchangePositionEvidence:
    return ExchangePositionEvidence(
        instrument_ref=instrument_ref,
        side=side,
        position_ref=position_ref,
        quantity=quantity,
    )


def _terminal_exchange_evidence(**overrides) -> ExchangeReservationEvidence:
    values = {
        "capture_reason_code": None,
        "schema_valid": True,
        "current_positions_complete": True,
        "pending_orders_complete": True,
        "order_history_complete": True,
        "fills_complete": True,
        "position_history_complete": True,
        "order_history_at_limit": False,
        "fills_at_limit": False,
        "position_history_at_limit": False,
        "current_positions": (),
        "pending_orders": (),
        "order_history": (_order_evidence(),),
        "fills": (_fill_evidence(),),
        "position_history": (_position_history_evidence(),),
    }
    values.update(overrides)
    return ExchangeReservationEvidence(**values)


def _classify(
    exchange: ExchangeReservationEvidence,
    *,
    local: LocalReservationEvidence | None = None,
    capture_completed_at: datetime = CLASSIFICATION_TIME + timedelta(minutes=3),
) -> BoundCloseReservationObservation:
    return classify_bound_close_reservation(
        local or _local_classification_evidence(),
        exchange,
        capture_completed_at=capture_completed_at,
    )


@pytest.mark.parametrize(
    ("case", "local", "exchange", "expected", "reason"),
    [
        pytest.param(
            "exact terminal chain",
            _local_classification_evidence(),
            _terminal_exchange_evidence(),
            ReservationClassification.PROVEN_TERMINAL,
            "exact_close_and_position_terminal",
            id="exact-terminal-chain",
        ),
        pytest.param(
            "active parent with independently closed sibling",
            _local_classification_evidence(binding_status="active"),
            _terminal_exchange_evidence(),
            ReservationClassification.PROVEN_TERMINAL,
            "exact_close_and_position_terminal",
            id="active-parent-closed-sibling",
        ),
        pytest.param(
            "exact current position",
            _local_classification_evidence(),
            _terminal_exchange_evidence(
                current_positions=(_current_position_evidence(),)
            ),
            ReservationClassification.ACTIVE,
            "exact_position_currently_live",
            id="current-position",
        ),
        pytest.param(
            "exact pending order",
            _local_classification_evidence(),
            _terminal_exchange_evidence(
                pending_orders=(
                    _order_evidence(
                        state=ExchangeCloseOrderState.PENDING,
                        filled_quantity=Decimal("0"),
                        terminal_at=None,
                    ),
                )
            ),
            ReservationClassification.ACTIVE,
            "exact_close_order_currently_pending",
            id="pending-order",
        ),
        pytest.param(
            "exchange nonterminal order",
            _local_classification_evidence(),
            _terminal_exchange_evidence(
                order_history=(
                    _order_evidence(
                        state=ExchangeCloseOrderState.OPEN,
                        filled_quantity=Decimal("0"),
                        terminal_at=None,
                    ),
                )
            ),
            ReservationClassification.ACTIVE,
            "exact_close_order_nonterminal",
            id="nonterminal-order",
        ),
        pytest.param(
            "local evidence incomplete",
            _local_classification_evidence(
                local_reason_code="local_evidence_incomplete"
            ),
            _terminal_exchange_evidence(),
            ReservationClassification.UNKNOWN,
            "local_evidence_incomplete",
            id="local-incomplete",
        ),
        pytest.param(
            "local identity conflict",
            _local_classification_evidence(
                local_reason_code="local_identity_conflict"
            ),
            _terminal_exchange_evidence(),
            ReservationClassification.UNKNOWN,
            "local_identity_conflict",
            id="local-identity-conflict",
        ),
        pytest.param(
            "exchange schema invalid before active fact",
            _local_classification_evidence(),
            _terminal_exchange_evidence(
                schema_valid=False,
                current_positions=(_current_position_evidence(),),
            ),
            ReservationClassification.UNKNOWN,
            "exchange_schema_invalid",
            id="schema-invalid-precedes-active",
        ),
        pytest.param(
            "position callback delayed",
            _local_classification_evidence(),
            _terminal_exchange_evidence(position_history=()),
            ReservationClassification.UNKNOWN,
            "exchange_history_incomplete",
            id="callback-delay",
        ),
    ],
)
def test_classification_matrix(case, local, exchange, expected, reason):
    del case
    observation = _classify(exchange, local=local)
    assert observation.classification is expected
    assert observation.reason_code == reason
    assert observation.source_fingerprint == _sha256_json(local)
    assert observation.exchange_fingerprint == _sha256_json(exchange)


@pytest.mark.parametrize(
    ("state", "filled_quantity"),
    [
        (ExchangeCloseOrderState.REJECTED, Decimal("0")),
        (ExchangeCloseOrderState.CANCELLED, Decimal("0")),
        (ExchangeCloseOrderState.PARTIALLY_FILLED, Decimal("0.5")),
    ],
)
def test_rejected_cancelled_and_partial_history_are_unknown(
    state, filled_quantity
):
    exchange = _terminal_exchange_evidence(
        order_history=(
            _order_evidence(state=state, filled_quantity=filled_quantity),
        )
    )

    observation = _classify(exchange)

    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code == "exchange_state_conflict"


@pytest.mark.parametrize(
    "state",
    [
        ExchangeCloseOrderState.REJECTED,
        ExchangeCloseOrderState.CANCELLED,
        ExchangeCloseOrderState.PARTIALLY_FILLED,
        ExchangeCloseOrderState.FILLED,
    ],
)
def test_contradictory_order_state_in_pending_snapshot_is_unknown(state):
    exchange = _terminal_exchange_evidence(
        pending_orders=(
            _order_evidence(
                state=state,
                terminal_at=(
                    None
                    if state is ExchangeCloseOrderState.PARTIALLY_FILLED
                    else CLASSIFICATION_TIME + timedelta(minutes=1)
                ),
            ),
        )
    )

    observation = _classify(exchange)

    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code == "exchange_state_conflict"


@pytest.mark.parametrize(
    ("collection", "value"),
    [
        ("order_history", ()),
        ("order_history", (_order_evidence(), _order_evidence())),
        ("fills", ()),
        ("fills", (_fill_evidence(), _fill_evidence())),
        ("position_history", ()),
        (
            "position_history",
            (_position_history_evidence(), _position_history_evidence()),
        ),
    ],
)
def test_missing_or_duplicate_terminal_evidence_is_unknown(collection, value):
    observation = _classify(
        _terminal_exchange_evidence(**{collection: value})
    )
    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code in {
        "exchange_history_incomplete",
        "exchange_state_conflict",
    }


def test_nonterminal_position_history_is_unknown():
    observation = _classify(
        _terminal_exchange_evidence(
            position_history=(
                _position_history_evidence(
                    state=ExchangePositionHistoryState.OPEN,
                ),
            )
        )
    )
    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code == "exchange_state_conflict"


@pytest.mark.parametrize(
    ("collection", "value"),
    [
        (
            "current_positions",
            (_current_position_evidence(instrument_ref="9" * 64),),
        ),
        (
            "pending_orders",
            (
                _order_evidence(
                    state=ExchangeCloseOrderState.PENDING,
                    filled_quantity=Decimal("0"),
                    terminal_at=None,
                    side="short",
                ),
            ),
        ),
        (
            "order_history",
            (_order_evidence(order_ref="9" * 64),),
        ),
        (
            "fills",
            (_fill_evidence(position_ref="9" * 64),),
        ),
        (
            "position_history",
            (_position_history_evidence(position_ref="9" * 64),),
        ),
    ],
)
def test_any_exchange_identity_mismatch_is_unknown_before_active(
    collection, value
):
    observation = _classify(
        _terminal_exchange_evidence(**{collection: value})
    )
    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code == "exchange_identity_conflict"


@pytest.mark.parametrize(
    "overrides",
    [
        {"order_history_complete": False},
        {"fills_complete": False},
        {"position_history_complete": False},
        {"current_positions_complete": False},
        {"pending_orders_complete": False},
        {"order_history_at_limit": True},
        {"fills_at_limit": True},
        {"position_history_at_limit": True},
    ],
)
def test_incomplete_or_page_limit_ambiguous_exchange_capture_is_unknown(
    overrides,
):
    observation = _classify(_terminal_exchange_evidence(**overrides))
    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code == "exchange_history_incomplete"


@pytest.mark.parametrize(
    ("order_quantity", "filled_quantity", "fill_quantity", "closed_quantity"),
    [
        ("1", "0.999999999999999999", "1", "1"),
        ("1", "1", "0.999999999999999999", "1"),
        ("1", "1", "1", "1.000000000000000001"),
        ("0", "0", "0", "0"),
    ],
)
def test_terminal_quantities_require_positive_exact_decimal_equality(
    order_quantity, filled_quantity, fill_quantity, closed_quantity
):
    exchange = _terminal_exchange_evidence(
        order_history=(
            _order_evidence(
                requested_quantity=Decimal(order_quantity),
                filled_quantity=Decimal(filled_quantity),
            ),
        ),
        fills=(_fill_evidence(quantity=Decimal(fill_quantity)),),
        position_history=(
            _position_history_evidence(
                closed_quantity=Decimal(closed_quantity)
            ),
        ),
    )

    observation = _classify(exchange)

    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code == "exchange_state_conflict"


@pytest.mark.parametrize(
    ("local", "exchange", "capture_completed_at"),
    [
        (
            _local_classification_evidence(
                reservation_created_at=CLASSIFICATION_TIME + timedelta(minutes=2)
            ),
            _terminal_exchange_evidence(),
            CLASSIFICATION_TIME + timedelta(minutes=3),
        ),
        (
            _local_classification_evidence(
                event_created_at=CLASSIFICATION_TIME + timedelta(minutes=2)
            ),
            _terminal_exchange_evidence(),
            CLASSIFICATION_TIME + timedelta(minutes=3),
        ),
        (
            _local_classification_evidence(),
            _terminal_exchange_evidence(
                position_history=(
                    _position_history_evidence(
                        closed_at=CLASSIFICATION_TIME + timedelta(seconds=30)
                    ),
                )
            ),
            CLASSIFICATION_TIME + timedelta(minutes=3),
        ),
        (
            _local_classification_evidence(),
            _terminal_exchange_evidence(),
            CLASSIFICATION_TIME + timedelta(minutes=1, seconds=30),
        ),
    ],
)
def test_timestamp_inversion_is_unknown(local, exchange, capture_completed_at):
    observation = _classify(
        exchange,
        local=local,
        capture_completed_at=capture_completed_at,
    )
    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code == "exchange_state_conflict"


@pytest.mark.parametrize(
    "local",
    [
        _local_classification_evidence(mutation_status="rejected"),
        _local_classification_evidence(mutation_status="recovery_required"),
        _local_classification_evidence(mutation_order_ref="9" * 64),
    ],
)
def test_conflicting_close_mutation_is_unknown(local):
    observation = _classify(_terminal_exchange_evidence(), local=local)
    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code == "exchange_state_conflict"


@pytest.mark.parametrize(
    ("capture_reason", "expected"),
    [
        ("exchange_evidence_unavailable", "exchange_evidence_unavailable"),
        ("exchange_schema_invalid", "exchange_schema_invalid"),
        ("exchange_capture_timeout", "exchange_capture_timeout"),
        ("exchange_response_size_exceeded", "exchange_response_size_exceeded"),
    ],
)
def test_closed_capture_failure_reasons_precede_exchange_facts(
    capture_reason, expected
):
    observation = _classify(
        _terminal_exchange_evidence(
            capture_reason_code=capture_reason,
            current_positions=(_current_position_evidence(),),
        )
    )
    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code == expected


def test_terminal_classification_never_uses_age_or_callback_deadline():
    old = datetime(2020, 1, 1, tzinfo=UTC)
    local = _local_classification_evidence(
        reservation_created_at=old,
        event_created_at=old,
    )
    exchange = _terminal_exchange_evidence(
        order_history=(
            _order_evidence(terminal_at=old + timedelta(minutes=1)),
        ),
        fills=(_fill_evidence(filled_at=old + timedelta(minutes=1)),),
        position_history=(
            _position_history_evidence(closed_at=old + timedelta(minutes=2)),
        ),
    )

    assert _classify(exchange, local=local).classification is (
        ReservationClassification.PROVEN_TERMINAL
    )
    delayed = replace(exchange, position_history=())
    assert _classify(delayed, local=local).classification is (
        ReservationClassification.UNKNOWN
    )


@pytest.mark.parametrize(
    "filled_at",
    [
        CLASSIFICATION_TIME - timedelta(microseconds=1),
        CLASSIFICATION_TIME + timedelta(minutes=1, microseconds=1),
        CLASSIFICATION_TIME + timedelta(minutes=2, microseconds=1),
        CLASSIFICATION_TIME + timedelta(minutes=3, microseconds=1),
    ],
)
def test_fill_timestamp_outside_the_terminal_chain_is_unknown(filled_at):
    local = _local_classification_evidence()
    exchange = _terminal_exchange_evidence(
        fills=(_fill_evidence(filled_at=filled_at),)
    )

    observation = _classify(exchange, local=local)

    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code == "exchange_state_conflict"


@pytest.mark.parametrize(
    ("collection", "order"),
    [
        (
            "pending_orders",
            _order_evidence(
                state=ExchangeCloseOrderState.PENDING,
                filled_quantity=Decimal("0"),
                terminal_at=CLASSIFICATION_TIME,
            ),
        ),
        (
            "pending_orders",
            _order_evidence(
                state=ExchangeCloseOrderState.OPEN,
                requested_quantity=Decimal("0"),
                filled_quantity=Decimal("0"),
                terminal_at=None,
            ),
        ),
        (
            "pending_orders",
            _order_evidence(
                state=ExchangeCloseOrderState.PENDING,
                requested_quantity=Decimal("1"),
                filled_quantity=Decimal("2"),
                terminal_at=None,
            ),
        ),
        (
            "order_history",
            _order_evidence(
                state=ExchangeCloseOrderState.OPEN,
                filled_quantity=Decimal("0"),
                terminal_at=CLASSIFICATION_TIME,
            ),
        ),
        (
            "order_history",
            _order_evidence(
                state=ExchangeCloseOrderState.FILLED,
                terminal_at=None,
            ),
        ),
    ],
)
def test_malformed_order_state_shape_is_unknown_before_active(collection, order):
    observation = _classify(
        _terminal_exchange_evidence(**{collection: (order,)})
    )
    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code == "exchange_state_conflict"


def test_exchange_evidence_schema_is_frozen_closed_and_bounded():
    exchange = _terminal_exchange_evidence()
    assert not hasattr(exchange, "__dict__")
    with pytest.raises(FrozenInstanceError):
        exchange.schema_valid = False  # type: ignore[misc]
    with pytest.raises(TypeError, match="current_positions"):
        _terminal_exchange_evidence(  # type: ignore[arg-type]
            current_positions=[_current_position_evidence()]
        )
    with pytest.raises(ValueError, match="100"):
        _terminal_exchange_evidence(
            fills=tuple(_fill_evidence() for _ in range(101))
        )
    with pytest.raises(TypeError, match="schema_valid"):
        _terminal_exchange_evidence(schema_valid=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="capture_reason_code"):
        _terminal_exchange_evidence(capture_reason_code="future_reason")


def test_exchange_evidence_records_require_exact_decimals_and_aware_utc():
    with pytest.raises(TypeError, match="quantity"):
        _current_position_evidence(quantity=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        _fill_evidence(quantity=Decimal("NaN"))
    with pytest.raises(ValueError, match="aware UTC"):
        _fill_evidence(filled_at=CLASSIFICATION_TIME.replace(tzinfo=None))
    with pytest.raises(ValueError, match="side"):
        _fill_evidence(side="buy")


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1e33"),
        Decimal("1e-33"),
        Decimal("1234567890" * 7),
    ],
)
def test_exchange_decimal_schema_rejects_extreme_values_before_hashing(value):
    with pytest.raises(ValueError, match="bounded"):
        _fill_evidence(quantity=value)


def test_decimal_canonicalization_is_exact_and_context_independent():
    left = Decimal("1.000000000000000001")
    right = Decimal("1.000000000000000002")
    with localcontext() as context:
        context.prec = 3
        left_json = _canonical_json({"quantity": left})
        right_json = _canonical_json({"quantity": right})

    assert left_json == '{"quantity":"1.000000000000000001"}'
    assert right_json == '{"quantity":"1.000000000000000002"}'
    assert _sha256_json({"quantity": left}) != _sha256_json(
        {"quantity": right}
    )


def test_classifier_requires_strict_types_and_aware_utc_capture():
    with pytest.raises(TypeError, match="local"):
        classify_bound_close_reservation(  # type: ignore[arg-type]
            object(),
            _terminal_exchange_evidence(),
            capture_completed_at=CLASSIFICATION_TIME,
        )
    with pytest.raises(TypeError, match="exchange"):
        classify_bound_close_reservation(  # type: ignore[arg-type]
            _local_classification_evidence(),
            object(),
            capture_completed_at=CLASSIFICATION_TIME,
        )
    with pytest.raises(ValueError, match="aware UTC"):
        _classify(
            _terminal_exchange_evidence(),
            capture_completed_at=CLASSIFICATION_TIME.replace(tzinfo=None),
        )


def test_exchange_reader_errors_map_only_to_closed_unknown_reasons():
    from telegram_kol_research.deepcoin_request_policy import (
        ErrorCategory,
        FailureFact,
        OutcomeCertainty,
    )
    from telegram_kol_research.deepcoin_client import DeepcoinReadUnavailable

    def failure(safe_code):
        return DeepcoinReadUnavailable(
            safe_code,
            fact=FailureFact(
                category=ErrorCategory.SCHEMA_INVALID,
                outcome_certainty=OutcomeCertainty.UNKNOWN,
                retryable=False,
                safe_code=safe_code,
            ),
        )

    assert exchange_recovery_reason_from_error(
        BoundCloseReservationExchangeDeadlineExceeded("expired")
    ) == "exchange_capture_timeout"
    assert exchange_recovery_reason_from_error(
        failure("request_deadline_exceeded")
    ) == "exchange_capture_timeout"
    assert exchange_recovery_reason_from_error(
        failure("monitor_response_size_exceeded")
    ) == "exchange_response_size_exceeded"
    assert exchange_recovery_reason_from_error(
        failure("response_schema_invalid")
    ) == "exchange_schema_invalid"
    assert exchange_recovery_reason_from_error(
        failure("read_timeout")
    ) == "exchange_evidence_unavailable"
    assert exchange_recovery_reason_from_error(RuntimeError("future")) == (
        "exchange_evidence_unavailable"
    )


SOURCE_TIME = "2026-08-14 10:00:00.000000"


def _create_reservation_source_database(path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE bound_position_close_reservations (
            id INTEGER PRIMARY KEY,
            pos_id TEXT NOT NULL,
            execution_binding_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE execution_bindings (
            id INTEGER PRIMARY KEY,
            strategy_instance_id TEXT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            venue TEXT NOT NULL,
            pos_id TEXT,
            status TEXT NOT NULL,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE execution_events (
            id INTEGER PRIMARY KEY,
            execution_binding_id INTEGER,
            strategy_instance_id TEXT,
            venue TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            symbol TEXT,
            side TEXT,
            order_id TEXT,
            pos_id TEXT,
            before_json TEXT,
            after_json TEXT,
            request_json TEXT,
            response_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE execution_order_legs (
            id INTEGER PRIMARY KEY,
            execution_binding_id INTEGER NOT NULL,
            strategy_instance_id TEXT,
            leg_index INTEGER NOT NULL,
            purpose TEXT NOT NULL,
            order_kind TEXT NOT NULL,
            order_id TEXT,
            client_order_id TEXT,
            pos_id TEXT,
            venue TEXT NOT NULL,
            attribution_status TEXT NOT NULL,
            status TEXT NOT NULL,
            attribution_evidence_json TEXT,
            terminal_reason TEXT,
            request_json TEXT,
            response_json TEXT,
            last_verified_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE position_mutation_intents (
            id INTEGER PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            operation TEXT NOT NULL,
            strategy_instance_id TEXT NOT NULL,
            execution_binding_id INTEGER NOT NULL,
            execution_order_leg_id INTEGER NOT NULL,
            pos_id TEXT NOT NULL,
            order_id TEXT,
            authority_fingerprint TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            venue TEXT NOT NULL,
            status TEXT NOT NULL,
            request_json TEXT NOT NULL,
            response_json TEXT,
            error_json TEXT,
            reserved_at TEXT NOT NULL,
            submitted_at TEXT,
            confirmed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    return connection


def _seed_local_reservation(
    connection: sqlite3.Connection,
    *,
    row_id: int = 1,
    status: str = "submitted",
    include_binding: bool = True,
    include_event: bool = True,
    include_leg: bool = True,
    include_mutation: bool = True,
) -> None:
    pos_id = f"raw-pos-{row_id}"
    binding_id = row_id
    strategy_id = f"raw-strategy-{row_id}"
    order_id = f"raw-close-order-{row_id}"
    connection.execute(
        "INSERT INTO bound_position_close_reservations VALUES (?, ?, ?, ?, ?, ?, ?)",
        (row_id, pos_id, binding_id, status, None, SOURCE_TIME, SOURCE_TIME),
    )
    if include_binding:
        connection.execute(
            "INSERT INTO execution_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                binding_id,
                strategy_id,
                f"COIN{row_id}USDT",
                "long",
                "deepcoin",
                pos_id,
                "closed",
                '{"source":"test"}',
                SOURCE_TIME,
                SOURCE_TIME,
            ),
        )
    if include_event:
        connection.execute(
            "INSERT INTO execution_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                binding_id,
                strategy_id,
                "deepcoin",
                "close_bound_position_market",
                "submitted",
                f"COIN{row_id}USDT",
                "long",
                order_id,
                pos_id,
                '{"position_size":"1"}',
                '{"close_size":"1"}',
                '{"closePosId":"redacted-in-source-output"}',
                '{"code":"0"}',
                SOURCE_TIME,
            ),
        )
    if include_leg:
        connection.execute(
            "INSERT INTO execution_order_legs VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                binding_id,
                strategy_id,
                row_id,
                "entry",
                "market",
                f"raw-entry-order-{row_id}",
                f"raw-entry-client-{row_id}",
                pos_id,
                "deepcoin",
                "verified",
                "manually_closed",
                '{"proof":"local"}',
                "manual_position_missing",
                "{}",
                "{}",
                SOURCE_TIME,
                SOURCE_TIME,
                SOURCE_TIME,
            ),
        )
    if include_mutation:
        connection.execute(
            "INSERT INTO position_mutation_intents VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                f"raw-mutation-key-{row_id}",
                "close_position",
                strategy_id,
                binding_id,
                row_id,
                pos_id,
                order_id,
                "a" * 64,
                "b" * 64,
                "deepcoin",
                "confirmed",
                "{}",
                '{"code":"0"}',
                None,
                SOURCE_TIME,
                SOURCE_TIME,
                SOURCE_TIME,
                SOURCE_TIME,
                SOURCE_TIME,
            ),
        )


def test_source_loader_loads_all_closed_active_statuses_and_exact_descendants(
    tmp_path,
):
    database = tmp_path / "source.sqlite3"
    connection = _create_reservation_source_database(database)
    statuses = (
        "reserved",
        "submitted",
        "submit_unknown",
        "unknown_exchange_outcome",
        "recovery_required",
    )
    for row_id, status in enumerate(statuses, start=1):
        _seed_local_reservation(connection, row_id=row_id, status=status)
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)

    assert isinstance(source, BoundCloseReservationSource)
    assert len(source.reservations) == 5
    assert [item.source_status for item in source.reservations] == list(statuses)
    assert all(isinstance(item, LocalReservationEvidence) for item in source.reservations)
    assert all(item.local_reason_code is None for item in source.reservations)
    assert all(item.close_event_ref for item in source.reservations)
    assert all(item.close_order_ref for item in source.reservations)
    assert all(item.entry_leg is not None for item in source.reservations)
    assert all(len(item.close_mutations) == 1 for item in source.reservations)
    assert all(item.close_mutations[0].status == "confirmed" for item in source.reservations)
    assert source.source_fingerprint == _sha256_json(
        {"reservations": source.reservations}
    )


PLAN_CAPTURE_STARTED = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
PLAN_CAPTURE_COMPLETED = datetime(2026, 8, 15, 10, 1, tzinfo=UTC)


def _built_recovery_plan(
    observations: tuple[BoundCloseReservationObservation, ...],
    *,
    source_fingerprint: str = FINGERPRINT_D,
) -> BoundCloseReservationRecoveryPlan:
    return build_bound_close_reservation_recovery_plan(
        source_fingerprint=source_fingerprint,
        observations=observations,
    )


def test_plan_builder_sorts_unique_refs_conserves_counts_and_derives_seals():
    observations = (
        _observation(reservation_ref=FINGERPRINT_C),
        _observation(reservation_ref=FINGERPRINT_A),
    )

    plan = _built_recovery_plan(observations)
    document = serialize_bound_close_reservation_recovery_plan(
        plan,
        capture_started_at=PLAN_CAPTURE_STARTED,
        capture_completed_at=PLAN_CAPTURE_COMPLETED,
    )
    payload = json.loads(document)

    assert list(payload) == sorted(
        {
            "action_count",
            "capture_completed_at",
            "capture_identity",
            "capture_started_at",
            "confirmation_token",
            "counts",
            "database_writes",
            "evidence_fingerprint",
            "exchange_snapshot_fingerprint",
            "exchange_writes",
            "history_replays",
            "mode",
            "observations",
            "schema_version",
            "source_fingerprint",
            "status",
        }
    )
    assert list(payload["counts"]) == [
        "active",
        "proven_terminal",
        "total",
        "unknown",
    ]
    assert [item["reservation_ref"] for item in payload["observations"]] == [
        FINGERPRINT_A,
        FINGERPRINT_C,
    ]
    assert all(
        list(item) == [
            "classification",
            "exchange_fingerprint",
            "reason_code",
            "reservation_ref",
            "source_fingerprint",
        ]
        for item in payload["observations"]
    )
    assert payload["counts"] == {
        "active": 0,
        "proven_terminal": 2,
        "total": 2,
        "unknown": 0,
    }
    assert payload["action_count"] == 2
    assert payload["exchange_writes"] == 0
    assert payload["history_replays"] == 0
    assert payload["database_writes"] == 0
    assert payload["confirmation_token"].startswith("BOUND-CLOSE-")
    assert payload["confirmation_token"] != payload["evidence_fingerprint"]


def test_plan_builder_refuses_mixed_population_with_zero_actions():
    active = _observation(
        reservation_ref=FINGERPRINT_B,
        classification=ReservationClassification.ACTIVE,
        reason_code="exact_position_currently_live",
    )
    plan = _built_recovery_plan((_observation(), active))
    payload = json.loads(
        serialize_bound_close_reservation_recovery_plan(
            plan,
            capture_started_at=PLAN_CAPTURE_STARTED,
            capture_completed_at=PLAN_CAPTURE_COMPLETED,
        )
    )

    assert plan.status == "refused"
    assert plan.action_count == 0
    assert payload["counts"] == {
        "active": 1,
        "proven_terminal": 1,
        "total": 2,
        "unknown": 0,
    }


def test_plan_builder_rejects_duplicate_refs_and_serializer_rejects_forgery():
    with pytest.raises(ValueError, match="unique"):
        _built_recovery_plan((_observation(), _observation()))

    valid = _built_recovery_plan((_observation(),))
    forged = replace(valid, confirmation_token="BOUND-CLOSE-0000000000000000")
    with pytest.raises(ValueError, match="confirmation_token"):
        serialize_bound_close_reservation_recovery_plan(
            forged,
            capture_started_at=PLAN_CAPTURE_STARTED,
            capture_completed_at=PLAN_CAPTURE_COMPLETED,
        )


@pytest.mark.parametrize(
    ("started", "completed"),
    [
        (PLAN_CAPTURE_STARTED.replace(tzinfo=None), PLAN_CAPTURE_COMPLETED),
        (PLAN_CAPTURE_STARTED, PLAN_CAPTURE_COMPLETED.replace(tzinfo=None)),
        (PLAN_CAPTURE_COMPLETED, PLAN_CAPTURE_STARTED),
        (PLAN_CAPTURE_STARTED.astimezone(timezone(timedelta(hours=1))), PLAN_CAPTURE_COMPLETED),
    ],
)
def test_plan_serialization_requires_ordered_aware_utc_capture_times(
    started, completed
):
    with pytest.raises(ValueError, match="capture"):
        serialize_bound_close_reservation_recovery_plan(
            _built_recovery_plan((_observation(),)),
            capture_started_at=started,
            capture_completed_at=completed,
        )


def test_capture_times_change_identity_but_not_semantic_fingerprint():
    plan = _built_recovery_plan((_observation(),))
    first = json.loads(
        serialize_bound_close_reservation_recovery_plan(
            plan,
            capture_started_at=PLAN_CAPTURE_STARTED,
            capture_completed_at=PLAN_CAPTURE_COMPLETED,
        )
    )
    second = json.loads(
        serialize_bound_close_reservation_recovery_plan(
            plan,
            capture_started_at=PLAN_CAPTURE_STARTED + timedelta(minutes=2),
            capture_completed_at=PLAN_CAPTURE_COMPLETED + timedelta(minutes=2),
        )
    )

    assert first["evidence_fingerprint"] == second["evidence_fingerprint"]
    assert first["confirmation_token"] == second["confirmation_token"]
    assert first["capture_identity"] != second["capture_identity"]


def test_serialized_plan_never_contains_raw_operational_evidence():
    document = serialize_bound_close_reservation_recovery_plan(
        _built_recovery_plan((_observation(),)),
        capture_started_at=PLAN_CAPTURE_STARTED,
        capture_completed_at=PLAN_CAPTURE_COMPLETED,
    )
    for forbidden in (
        "raw-db-id-901",
        "raw-pos-901",
        "raw-order-901",
        "123.456789",
        "98765.43",
        "provider-secret-payload",
        "telegram source text",
        "DC-ACCESS-KEY",
        "credential-value",
    ):
        assert forbidden not in document


def _loaded_seal_source(tmp_path, *, row_count: int = 1):
    database = tmp_path / f"sealed-source-{row_count}.sqlite3"
    connection = _create_reservation_source_database(database)
    for offset in range(row_count):
        _seed_local_reservation(connection, row_id=901 + offset)
    connection.commit()
    connection.close()
    return load_bound_close_reservation_source(database)


def _terminal_exchange_for_local(
    local: LocalReservationEvidence,
) -> ExchangeReservationEvidence:
    return _terminal_exchange_evidence(
        order_history=(
            _order_evidence(
                instrument_ref=local.instrument_ref,
                side=local.side,
                position_ref=local.position_ref,
                order_ref=local.close_order_ref,
            ),
        ),
        fills=(
            _fill_evidence(
                instrument_ref=local.instrument_ref,
                side=local.side,
                position_ref=local.position_ref,
                order_ref=local.close_order_ref,
            ),
        ),
        position_history=(
            _position_history_evidence(
                instrument_ref=local.instrument_ref,
                side=local.side,
                position_ref=local.position_ref,
            ),
        ),
    )


def _classified_observation_for_source_local(
    local: LocalReservationEvidence,
    *,
    capture_completed_at: datetime = PLAN_CAPTURE_COMPLETED,
) -> BoundCloseReservationObservation:
    return classify_bound_close_reservation(
        local,
        _terminal_exchange_for_local(local),
        capture_completed_at=capture_completed_at,
    )


def test_public_plan_helpers_accept_documents_but_cannot_issue_apply_capability(
    tmp_path,
):
    source = _loaded_seal_source(tmp_path)
    local = source.reservations[0]
    forged = BoundCloseReservationObservation(
        reservation_ref=local.reservation_ref,
        classification=ReservationClassification.PROVEN_TERMINAL,
        reason_code="exact_close_and_position_terminal",
        source_fingerprint=_sha256_json(local),
        exchange_fingerprint=FINGERPRINT_C,
    )
    plan = build_bound_close_reservation_recovery_plan(
        source_fingerprint=source.source_fingerprint,
        observations=(forged,),
    )

    assert json.loads(
        serialize_bound_close_reservation_recovery_plan(
            plan,
            capture_started_at=PLAN_CAPTURE_STARTED,
            capture_completed_at=PLAN_CAPTURE_COMPLETED,
        )
    )["status"] == "ready"
    with pytest.raises(ValueError, match="classifier-issued provenance"):
        _seal_bound_close_reservation_recovery_capture(
            source=source,
            observations=(forged,),
            capture_started_at=PLAN_CAPTURE_STARTED,
            capture_completed_at=PLAN_CAPTURE_COMPLETED,
        )
    assert not hasattr(
        recovery_module,
        "seal_bound_close_reservation_recovery_capture",
    )


def test_private_seal_requires_exact_classifier_provenance_and_is_opaque(
    tmp_path,
):
    source = _loaded_seal_source(tmp_path)
    observation = _classified_observation_for_source_local(source.reservations[0])

    capture = _seal_bound_close_reservation_recovery_capture(
        source=source,
        observations=(observation,),
        capture_started_at=PLAN_CAPTURE_STARTED,
        capture_completed_at=PLAN_CAPTURE_COMPLETED,
    )

    assert isinstance(capture, SealedRecoveryCapture)
    assert not hasattr(capture, "__dict__")
    assert repr(capture) == "<SealedRecoveryCapture opaque>"
    assert "raw-pos-901" not in capture.serialized_plan
    with pytest.raises(TypeError):
        SealedRecoveryCapture()  # type: ignore[call-arg]


def test_private_seal_rejects_timestamp_mismatch_without_consuming_provenance(
    tmp_path,
):
    source = _loaded_seal_source(tmp_path)
    observation = _classified_observation_for_source_local(source.reservations[0])

    with pytest.raises(ValueError, match="capture_completed_at"):
        _seal_bound_close_reservation_recovery_capture(
            source=source,
            observations=(observation,),
            capture_started_at=PLAN_CAPTURE_STARTED,
            capture_completed_at=PLAN_CAPTURE_COMPLETED + timedelta(microseconds=1),
        )

    capture = _seal_bound_close_reservation_recovery_capture(
        source=source,
        observations=(observation,),
        capture_started_at=PLAN_CAPTURE_STARTED,
        capture_completed_at=PLAN_CAPTURE_COMPLETED,
    )
    assert capture.plan.status == "ready"


def test_private_seal_claims_classifier_observation_only_once(tmp_path):
    source = _loaded_seal_source(tmp_path)
    observation = _classified_observation_for_source_local(source.reservations[0])
    _seal_bound_close_reservation_recovery_capture(
        source=source,
        observations=(observation,),
        capture_started_at=PLAN_CAPTURE_STARTED,
        capture_completed_at=PLAN_CAPTURE_COMPLETED,
    )

    with pytest.raises(ValueError, match="classifier-issued provenance"):
        _seal_bound_close_reservation_recovery_capture(
            source=source,
            observations=(observation,),
            capture_started_at=PLAN_CAPTURE_STARTED,
            capture_completed_at=PLAN_CAPTURE_COMPLETED,
        )


def test_private_seal_uses_object_identity_not_dataclass_equality(tmp_path):
    source = _loaded_seal_source(tmp_path)
    observation = _classified_observation_for_source_local(source.reservations[0])
    equal_but_unissued = replace(observation)

    assert equal_but_unissued == observation
    assert equal_but_unissued is not observation
    with pytest.raises(ValueError, match="classifier-issued provenance"):
        _seal_bound_close_reservation_recovery_capture(
            source=source,
            observations=(equal_but_unissued,),
            capture_started_at=PLAN_CAPTURE_STARTED,
            capture_completed_at=PLAN_CAPTURE_COMPLETED,
        )

    assert _seal_bound_close_reservation_recovery_capture(
        source=source,
        observations=(observation,),
        capture_started_at=PLAN_CAPTURE_STARTED,
        capture_completed_at=PLAN_CAPTURE_COMPLETED,
    ).plan.status == "ready"


def test_private_seal_requires_the_exact_source_local_object(tmp_path):
    source = _loaded_seal_source(tmp_path)
    equal_but_detached_local = replace(source.reservations[0])
    observation = _classified_observation_for_source_local(equal_but_detached_local)

    assert equal_but_detached_local == source.reservations[0]
    assert equal_but_detached_local is not source.reservations[0]
    with pytest.raises(ValueError, match="local identity"):
        _seal_bound_close_reservation_recovery_capture(
            source=source,
            observations=(observation,),
            capture_started_at=PLAN_CAPTURE_STARTED,
            capture_completed_at=PLAN_CAPTURE_COMPLETED,
        )


def test_private_seal_rejects_observations_from_mixed_capture_times(tmp_path):
    source = _loaded_seal_source(tmp_path, row_count=2)
    first = _classified_observation_for_source_local(
        source.reservations[0],
        capture_completed_at=PLAN_CAPTURE_COMPLETED,
    )
    second = _classified_observation_for_source_local(
        source.reservations[1],
        capture_completed_at=PLAN_CAPTURE_COMPLETED + timedelta(microseconds=1),
    )

    with pytest.raises(ValueError, match="capture_completed_at"):
        _seal_bound_close_reservation_recovery_capture(
            source=source,
            observations=(first, second),
            capture_started_at=PLAN_CAPTURE_STARTED,
            capture_completed_at=PLAN_CAPTURE_COMPLETED,
        )

    replacement_second = _classified_observation_for_source_local(
        source.reservations[1],
        capture_completed_at=PLAN_CAPTURE_COMPLETED,
    )
    capture = _seal_bound_close_reservation_recovery_capture(
        source=source,
        observations=(first, replacement_second),
        capture_started_at=PLAN_CAPTURE_STARTED,
        capture_completed_at=PLAN_CAPTURE_COMPLETED,
    )
    assert capture.plan.action_count == 2


def test_classifier_provenance_registry_is_bounded_and_weakly_cleaned(tmp_path):
    source = _loaded_seal_source(tmp_path)
    local = source.reservations[0]
    exchange = _terminal_exchange_for_local(local)
    observations = [
        classify_bound_close_reservation(
            local,
            exchange,
            capture_completed_at=PLAN_CAPTURE_COMPLETED,
        )
        for _ in range(recovery_module._MAX_OBSERVATION_PROVENANCE + 1)
    ]
    first_identity = id(observations[0])
    last = observations[-1]
    last_identity = id(last)
    last_ref = weakref.ref(last)

    assert len(recovery_module._OBSERVATION_PROVENANCE) <= (
        recovery_module._MAX_OBSERVATION_PROVENANCE
    )
    assert first_identity not in recovery_module._OBSERVATION_PROVENANCE
    assert last_identity in recovery_module._OBSERVATION_PROVENANCE

    observations.clear()
    del last
    gc.collect()

    assert last_ref() is None
    assert last_identity not in recovery_module._OBSERVATION_PROVENANCE


def test_sealed_apply_capability_is_private_issuer_bound_and_one_shot(tmp_path):
    source = _loaded_seal_source(tmp_path)
    observation = _classified_observation_for_source_local(source.reservations[0])
    capture = _seal_bound_close_reservation_recovery_capture(
        source=source,
        observations=(observation,),
        capture_started_at=PLAN_CAPTURE_STARTED,
        capture_completed_at=PLAN_CAPTURE_COMPLETED,
    )

    claimed = _claim_sealed_recovery_capture_for_apply(capture)

    assert claimed.plan is capture.plan
    assert not hasattr(claimed, "__dict__")
    with pytest.raises(ValueError, match="already claimed"):
        _claim_sealed_recovery_capture_for_apply(capture)


def test_raw_and_sealed_capabilities_reject_pickle_copy_and_deepcopy(tmp_path):
    source = _loaded_seal_source(tmp_path)
    local = source.reservations[0]
    raw_source_capability = source._capability
    raw_reservation_capability = raw_source_capability._get(local.reservation_ref)
    observation = _classified_observation_for_source_local(local)
    sealed = _seal_bound_close_reservation_recovery_capture(
        source=source,
        observations=(observation,),
        capture_started_at=PLAN_CAPTURE_STARTED,
        capture_completed_at=PLAN_CAPTURE_COMPLETED,
    )

    for capability in (
        raw_reservation_capability,
        raw_source_capability,
        source,
        sealed,
    ):
        assert "raw-pos-901" not in repr(capability)
        assert "raw-close-order-901" not in repr(capability)
        for operation in (
            pickle.dumps,
            copy.copy,
            copy.deepcopy,
        ):
            with pytest.raises(TypeError) as raised:
                operation(capability)
            error_payload = str(raised.value)
            assert "raw-pos-901" not in error_payload
            assert "raw-close-order-901" not in error_payload


@pytest.mark.parametrize(
    ("status", "submitted_at", "confirmed_at"),
    [
        ("reserved", None, None),
        ("not_sent", None, None),
        ("submitting", None, None),
        ("submitted", "2026-08-14 10:01:00.000000", None),
        (
            "confirmed",
            "2026-08-14 10:01:00.000000",
            "2026-08-14 10:02:00.000000",
        ),
        ("rejected", None, None),
        (
            "rejected",
            "2026-08-14 10:01:00.000000",
            "2026-08-14 10:02:00.000000",
        ),
        ("recovery_required", None, None),
        ("recovery_required", "2026-08-14 10:01:00.000000", None),
        ("blocked", None, None),
        ("blocked", "2026-08-14 10:01:00.000000", None),
    ],
)
def test_source_loader_accepts_only_authoritative_mutation_time_shapes(
    tmp_path,
    status,
    submitted_at,
    confirmed_at,
):
    database = tmp_path / f"valid-mutation-{status}.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.execute(
        """
        UPDATE position_mutation_intents
        SET status = ?, submitted_at = ?, confirmed_at = ?,
            updated_at = '2026-08-14 10:03:00.000000'
        """,
        (status, submitted_at, confirmed_at),
    )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)

    assert source.reservations[0].local_reason_code is None
    assert source.reservations[0].close_mutations[0].status == status


@pytest.mark.parametrize(
    ("status", "submitted_at", "confirmed_at"),
    [
        ("reserved", "2026-08-14 10:01:00.000000", None),
        ("submitted", None, None),
        (
            "submitted",
            "2026-08-14 10:01:00.000000",
            "2026-08-14 10:02:00.000000",
        ),
        ("confirmed", None, "2026-08-14 10:02:00.000000"),
        ("confirmed", "2026-08-14 10:01:00.000000", None),
        (
            "confirmed",
            "2026-08-14 10:02:00.000000",
            "2026-08-14 10:01:00.000000",
        ),
        ("rejected", None, "2026-08-14 10:02:00.000000"),
        ("recovery_required", None, "2026-08-14 10:02:00.000000"),
    ],
)
def test_source_loader_rejects_contradictory_mutation_time_shapes_without_capability(
    tmp_path,
    status,
    submitted_at,
    confirmed_at,
):
    database = tmp_path / f"invalid-mutation-{status}.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.execute(
        """
        UPDATE position_mutation_intents
        SET status = ?, submitted_at = ?, confirmed_at = ?,
            updated_at = '2026-08-14 10:03:00.000000'
        """,
        (status, submitted_at, confirmed_at),
    )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)
    evidence = source.reservations[0]

    assert evidence.local_reason_code == "local_evidence_incomplete"
    assert evidence.close_mutations == ()
    with pytest.raises(KeyError):
        source._capability._get(evidence.reservation_ref)


def test_classifier_rejects_confirmed_mutation_with_inverted_times():
    local = _local_classification_evidence()
    mutation = replace(
        local.close_mutations[0],
        submitted_at=CLASSIFICATION_TIME + timedelta(minutes=2),
        confirmed_at=CLASSIFICATION_TIME + timedelta(minutes=1),
        updated_at=CLASSIFICATION_TIME + timedelta(minutes=3),
    )

    observation = _classify(
        _terminal_exchange_evidence(),
        local=replace(local, close_mutations=(mutation,)),
        capture_completed_at=CLASSIFICATION_TIME + timedelta(minutes=4),
    )

    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code == "local_evidence_incomplete"


def test_classifier_rejects_mutation_timestamp_after_capture():
    local = _local_classification_evidence()
    future = CLASSIFICATION_TIME + timedelta(minutes=5)
    mutation = replace(
        local.close_mutations[0],
        submitted_at=CLASSIFICATION_TIME + timedelta(minutes=1),
        confirmed_at=future,
        updated_at=future,
    )

    observation = _classify(
        _terminal_exchange_evidence(),
        local=replace(local, close_mutations=(mutation,)),
        capture_completed_at=CLASSIFICATION_TIME + timedelta(minutes=4),
    )

    assert observation.classification is ReservationClassification.UNKNOWN
    assert observation.reason_code == "local_evidence_incomplete"


def test_source_loader_accepts_exact_closed_leg_of_an_active_split_binding(
    tmp_path,
):
    database = tmp_path / "split-binding.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.execute(
        "UPDATE execution_bindings SET status = 'active', pos_id = 'raw-live-sibling'"
    )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)

    assert len(source.reservations) == 1
    assert source.reservations[0].binding_status == "active"
    assert source.reservations[0].local_reason_code is None


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing_binding", "local_evidence_incomplete"),
        ("missing_event", "local_evidence_incomplete"),
        ("duplicate_event", "local_identity_conflict"),
        ("mismatched_event_binding", "local_identity_conflict"),
        ("malformed_event_json", "local_evidence_incomplete"),
        ("malformed_reservation_time", "local_evidence_incomplete"),
        ("unknown_status", "local_evidence_incomplete"),
    ],
)
def test_source_loader_fail_closes_bad_rows_as_unknown_local_facts(
    tmp_path, mutation, expected_reason
):
    database = tmp_path / f"{mutation}.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(
        connection,
        include_binding=mutation != "missing_binding",
        include_event=mutation != "missing_event",
        status="future_status" if mutation == "unknown_status" else "submitted",
    )
    if mutation == "duplicate_event":
        connection.execute(
            "INSERT INTO execution_events SELECT 99, execution_binding_id, "
            "strategy_instance_id, venue, action, status, symbol, side, "
            "'second-order', pos_id, before_json, after_json, request_json, "
            "response_json, created_at FROM execution_events"
        )
    elif mutation == "mismatched_event_binding":
        connection.execute(
            "UPDATE execution_events SET execution_binding_id = 999"
        )
    elif mutation == "malformed_event_json":
        connection.execute("UPDATE execution_events SET response_json = '{'")
    elif mutation == "malformed_reservation_time":
        connection.execute(
            "UPDATE bound_position_close_reservations SET updated_at = 'not-a-time'"
        )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)

    assert len(source.reservations) == 1
    assert source.reservations[0].local_reason_code == expected_reason


def test_source_loader_fail_closes_65_rows_without_returning_a_partial_population(
    tmp_path,
):
    database = tmp_path / "overflow.sqlite3"
    connection = _create_reservation_source_database(database)
    for row_id in range(1, 66):
        _seed_local_reservation(
            connection,
            row_id=row_id,
            include_leg=False,
            include_mutation=False,
        )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)

    assert len(source.reservations) == 1
    assert source.reservations[0].local_reason_code == "local_evidence_incomplete"
    assert source.reservations[0].source_status == "source_overflow"


def test_source_loader_fail_closes_schema_drift_instead_of_raising(tmp_path):
    database = tmp_path / "schema.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.execute("ALTER TABLE execution_events RENAME TO old_execution_events")
    connection.execute(
        "CREATE TABLE execution_events AS SELECT id, action, pos_id FROM old_execution_events"
    )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)

    assert len(source.reservations) == 1
    assert source.reservations[0].source_status == "source_schema_invalid"
    assert source.reservations[0].local_reason_code == "local_evidence_incomplete"


def test_source_loader_uses_one_read_only_wal_snapshot(
    tmp_path, monkeypatch
):
    database = tmp_path / "wal.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.commit()
    connection.close()
    baseline = load_bound_close_reservation_source(database)

    import telegram_kol_research.bound_close_reservation_recovery as recovery

    real_connect = sqlite3.connect
    opened = {}

    def recording_connect(*args, **kwargs):
        result = real_connect(*args, **kwargs)
        if kwargs.get("uri") is True and "mode=ro" in str(args[0]):
            opened["reader"] = result
        return result

    monkeypatch.setattr(recovery.sqlite3, "connect", recording_connect)

    def writer_between_selects():
        with pytest.raises(sqlite3.OperationalError):
            opened["reader"].execute(
                "UPDATE execution_bindings SET symbol = 'ILLEGAL' WHERE id = 1"
            )
        writer = real_connect(database)
        writer.execute(
            "UPDATE execution_bindings SET symbol = 'CHANGEDUSDT' WHERE id = 1"
        )
        writer.commit()
        writer.close()

    captured = load_bound_close_reservation_source(
        database,
        between_selects_hook=writer_between_selects,
    )
    changed = load_bound_close_reservation_source(database)

    assert captured.source_fingerprint == baseline.source_fingerprint
    assert changed.source_fingerprint != baseline.source_fingerprint


def test_source_loader_never_serializes_or_reprs_raw_identifiers(tmp_path):
    database = tmp_path / "redaction.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)
    serialized = _canonical_json({"reservations": source.reservations})
    rendered = repr(source)

    for raw_value in (
        "raw-pos-1",
        "raw-strategy-1",
        "raw-close-order-1",
        "raw-entry-order-1",
        "raw-entry-client-1",
        "raw-mutation-key-1",
    ):
        assert raw_value not in serialized
        assert raw_value not in rendered
    assert source.reservations[0].reservation_ref != "1"
    assert len(source.reservations[0].reservation_ref) == 64


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("execution_bindings", "status"),
        ("execution_events", "status"),
        ("execution_order_legs", "status"),
        ("execution_order_legs", "attribution_status"),
        ("position_mutation_intents", "status"),
    ],
)
def test_source_loader_fail_closes_future_local_status_without_raw_capability(
    tmp_path,
    table_name,
    column_name,
):
    database = tmp_path / f"future-{table_name}-{column_name}.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.execute(
        f'UPDATE "{table_name}" SET "{column_name}" = ?',
        ("future_typo_status",),
    )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)
    evidence = source.reservations[0]

    assert evidence.local_reason_code == "local_evidence_incomplete"
    with pytest.raises(KeyError):
        source._capability._get(evidence.reservation_ref)


@pytest.mark.parametrize(
    ("table_name", "missing_column"),
    [
        ("execution_order_legs", "leg_index"),
        ("execution_order_legs", "order_kind"),
        ("execution_order_legs", "client_order_id"),
        ("execution_order_legs", "terminal_reason"),
        ("position_mutation_intents", "idempotency_key"),
        ("position_mutation_intents", "authority_fingerprint"),
        ("position_mutation_intents", "request_fingerprint"),
    ],
)
def test_source_loader_requires_every_real_identity_column(
    tmp_path,
    table_name,
    missing_column,
):
    database = tmp_path / f"missing-{table_name}-{missing_column}.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    columns = [
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{table_name}")')
        if row[1] != missing_column
    ]
    projection = ", ".join(f'"{column}"' for column in columns)
    connection.execute(f'ALTER TABLE "{table_name}" RENAME TO "old_{table_name}"')
    connection.execute(
        f'CREATE TABLE "{table_name}" AS '
        f'SELECT {projection} FROM "old_{table_name}"'
    )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)

    assert len(source.reservations) == 1
    assert source.reservations[0].source_status == "source_schema_invalid"
    assert source.reservations[0].local_reason_code == "local_evidence_incomplete"


@pytest.mark.parametrize(
    ("table_name", "column_name", "changed_value"),
    [
        ("execution_order_legs", "leg_index", 9),
        ("execution_order_legs", "order_kind", "trigger_limit"),
        ("execution_order_legs", "client_order_id", "raw-other-client"),
        ("execution_order_legs", "terminal_reason", "raw-other-terminal"),
        (
            "position_mutation_intents",
            "idempotency_key",
            "raw-other-idempotency",
        ),
        ("position_mutation_intents", "authority_fingerprint", "c" * 64),
        ("position_mutation_intents", "request_fingerprint", "d" * 64),
    ],
)
def test_source_identity_field_drift_changes_fingerprint_without_raw_disclosure(
    tmp_path,
    table_name,
    column_name,
    changed_value,
):
    database = tmp_path / f"drift-{table_name}-{column_name}.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.commit()
    baseline = load_bound_close_reservation_source(database)
    connection.execute(
        f'UPDATE "{table_name}" SET "{column_name}" = ?',
        (changed_value,),
    )
    connection.commit()
    connection.close()

    changed = load_bound_close_reservation_source(database)
    serialized = _canonical_json({"reservations": changed.reservations})

    assert changed.source_fingerprint != baseline.source_fingerprint
    if isinstance(changed_value, str):
        assert changed_value not in serialized
        assert changed_value not in repr(changed)


class _RecoveryReaderHttpClient:
    def __init__(self, *, stream_mode="normal"):
        self.requests = []
        self.close_calls = 0
        self.stream_mode = stream_mode
        self.yielded = 0

    def request(self, method, request_path, content="", headers=None, timeout=None):
        raise AssertionError("bounded recovery reader must use streaming HTTP")

    def stream(self, method, request_path, content="", headers=None, timeout=None):
        from contextlib import contextmanager

        @contextmanager
        def response_context():
            self.requests.append((method, request_path, timeout))

            def iter_raw(_response):
                if self.stream_mode == "blocking":
                    time.sleep(0.5)
                elif self.stream_mode == "slow_drip":
                    for _ in range(50):
                        time.sleep(0.01)
                        self.yielded += 1
                        yield b" "
                self.yielded += 1
                yield b'{"code":"0","data":[]}'

            yield type(
                "Response",
                (),
                {
                    "status_code": 200,
                    "headers": {},
                    "iter_raw": iter_raw,
                },
            )()

        return response_context()

    def close(self):
        self.close_calls += 1


def _factory_recovery_reader(monkeypatch, http_client=None):
    selected_http_client = http_client or _RecoveryReaderHttpClient()
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_kwargs: selected_http_client,
    )
    transport = _build_deepcoin_bound_close_reservation_recovery_client_from_env(
        environ={
            "DEEPCOIN_API_KEY": "recovery-key",
            "DEEPCOIN_API_SECRET": "recovery-secret",
            "DEEPCOIN_API_PASSPHRASE": "recovery-passphrase",
        },
        env_file_paths=[],
    )
    return (
        BoundCloseReservationExchangeReader(transport),
        transport,
        selected_http_client,
    )


def test_recovery_reader_exposes_only_closed_get_capabilities_and_one_scope(
    monkeypatch,
):
    reader, _transport, http_client = _factory_recovery_reader(monkeypatch)

    public_names = {name for name in dir(reader) if not name.startswith("_")}
    assert public_names == {
        "read_open_orders",
        "read_order_history",
        "read_position_history",
        "read_positions",
        "read_trade_fills",
        "request_scope",
    }
    for forbidden in (
        "place_order",
        "cancel_order",
        "cancel_trigger_order",
        "replace_order_sltp",
        "transport",
        "client",
    ):
        assert not hasattr(reader, forbidden)

    with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
        assert reader.read_positions() == {"code": "0", "data": []}
        assert reader.read_open_orders() == {"code": "0", "data": []}
        assert reader.read_order_history(
            inst_id="BTC-USDT-SWAP", order_id="order-1", limit=100
        ) == {"code": "0", "data": []}
        assert reader.read_trade_fills(
            inst_id="BTC-USDT-SWAP", order_id="order-1", limit=100
        ) == {"code": "0", "data": []}
        assert reader.read_position_history(
            inst_id="BTC-USDT-SWAP", pos_id="position-1"
        ) == {"code": "0", "data": []}

    assert len(http_client.requests) == 5
    assert {request[0] for request in http_client.requests} == {"GET"}


def test_recovery_reader_scope_cleanup_closes_transport_after_failure(monkeypatch):
    reader, _transport, _http_client = _factory_recovery_reader(monkeypatch)

    with pytest.raises(RuntimeError, match="capture failed"):
        with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
            raise RuntimeError("capture failed")

    with pytest.raises(
        BoundCloseReservationExchangeConfigurationError,
        match="active recovery scope",
    ):
        reader.read_positions()


def test_recovery_reader_builder_returns_capability_not_raw_transport(monkeypatch):
    http_client = _RecoveryReaderHttpClient()
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: http_client)

    reader = build_bound_close_reservation_exchange_reader_from_env(
        environ={
            "DEEPCOIN_API_KEY": "key",
            "DEEPCOIN_API_SECRET": "secret",
            "DEEPCOIN_API_PASSPHRASE": "passphrase",
        },
        env_file_paths=[],
    )

    assert isinstance(reader, BoundCloseReservationExchangeReader)
    assert http_client.requests == []


def test_recovery_reader_rejects_non_factory_transport_before_http():
    http_client = _RecoveryReaderHttpClient()
    unsafe_transport = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        read_only=True,
        trust_env=False,
    )

    with pytest.raises(
        BoundCloseReservationExchangeConfigurationError,
        match="dedicated recovery factory",
    ):
        BoundCloseReservationExchangeReader(unsafe_transport)

    assert http_client.requests == []


def test_recovery_reader_refuses_reads_outside_owned_scope_and_external_scope(
    monkeypatch,
):
    reader, transport, http_client = _factory_recovery_reader(monkeypatch)

    with pytest.raises(
        BoundCloseReservationExchangeConfigurationError,
        match="active recovery scope",
    ):
        reader.read_positions()

    with transport.request_scope(
        DeepcoinRequestScope(
            phase="bound_close_reservation_recovery",
            priority=RequestPriority.BACKGROUND,
            deadline_monotonic=time.monotonic() + 1.0,
            max_response_bytes=1024 * 1024,
        )
    ):
        with pytest.raises(
            BoundCloseReservationExchangeConfigurationError,
            match="active recovery scope",
        ):
            reader.read_positions()

    assert http_client.requests == []
    transport.close()


def test_recovery_reader_refuses_nested_scope_and_reuse(monkeypatch):
    reader, _transport, http_client = _factory_recovery_reader(monkeypatch)

    with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
        with pytest.raises(
            BoundCloseReservationExchangeConfigurationError,
            match="already active",
        ):
            with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
                pass

    with pytest.raises(
        BoundCloseReservationExchangeConfigurationError,
        match="already consumed",
    ):
        with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
            pass

    assert http_client.requests == []


@pytest.mark.skipif(
    not all(
        hasattr(signal, name)
        for name in ("SIGALRM", "ITIMER_REAL", "getitimer", "setitimer")
    ),
    reason="POSIX interval timers are unavailable",
)
@pytest.mark.parametrize("stream_mode", ["blocking", "slow_drip"])
def test_recovery_reader_interrupts_real_blocking_stream_at_50ms(
    monkeypatch, stream_mode
):
    http_client = _RecoveryReaderHttpClient(stream_mode=stream_mode)
    reader, _transport, _http_client = _factory_recovery_reader(
        monkeypatch, http_client
    )

    started = time.monotonic()
    with pytest.raises(BoundCloseReservationExchangeDeadlineExceeded):
        with reader.request_scope(deadline_monotonic=started + 0.05):
            reader.read_positions()
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    if stream_mode == "slow_drip":
        assert http_client.yielded < 25
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is signal.SIG_DFL
    with pytest.raises(
        BoundCloseReservationExchangeConfigurationError,
        match="already consumed",
    ):
        with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
            pass


@pytest.mark.skipif(
    not all(
        hasattr(signal, name)
        for name in ("SIGALRM", "ITIMER_REAL", "getitimer", "setitimer")
    ),
    reason="POSIX interval timers are unavailable",
)
@pytest.mark.parametrize("stage", ["json_decode", "collection_parse"])
def test_recovery_deadline_covers_decode_and_caller_collection_parse(
    monkeypatch, stage
):
    reader, _transport, _http_client = _factory_recovery_reader(monkeypatch)
    if stage == "json_decode":
        import telegram_kol_research.deepcoin_client as deepcoin_client

        real_json_loads = deepcoin_client.json.loads

        def slow_json_loads(*args, **kwargs):
            time.sleep(0.5)
            return real_json_loads(*args, **kwargs)

        monkeypatch.setattr(deepcoin_client.json, "loads", slow_json_loads)

    started = time.monotonic()
    with pytest.raises(BoundCloseReservationExchangeDeadlineExceeded):
        with reader.request_scope(deadline_monotonic=started + 0.05):
            reader.read_positions()
            if stage == "collection_parse":
                time.sleep(0.5)

    assert time.monotonic() - started < 0.25


def test_recovery_reader_fails_closed_off_main_thread_before_http(monkeypatch):
    reader, _transport, http_client = _factory_recovery_reader(monkeypatch)
    captured = []

    def run():
        try:
            with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
                reader.read_positions()
        except BaseException as exc:
            captured.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert len(captured) == 1
    assert isinstance(captured[0], BoundCloseReservationExchangeConfigurationError)
    assert "main thread" in str(captured[0])
    assert http_client.requests == []


@pytest.mark.skipif(
    not all(
        hasattr(signal, name)
        for name in ("SIGALRM", "ITIMER_REAL", "getitimer", "setitimer")
    ),
    reason="POSIX interval timers are unavailable",
)
def test_recovery_reader_fails_closed_on_existing_timer_or_custom_handler(
    monkeypatch,
):
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        pytest.skip("test process already owns the real-time interval timer")

    reader_with_timer, _transport, timer_http = _factory_recovery_reader(monkeypatch)
    signal.setitimer(signal.ITIMER_REAL, 5.0)
    try:
        with pytest.raises(
            BoundCloseReservationExchangeConfigurationError,
            match="timer conflict",
        ):
            with reader_with_timer.request_scope(
                deadline_monotonic=time.monotonic() + 1.0
            ):
                reader_with_timer.read_positions()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
    assert timer_http.requests == []

    reader_with_handler, _transport, handler_http = _factory_recovery_reader(
        monkeypatch
    )
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, lambda _signum, _frame: None)
    try:
        with pytest.raises(
            BoundCloseReservationExchangeConfigurationError,
            match="timer conflict",
        ):
            with reader_with_handler.request_scope(
                deadline_monotonic=time.monotonic() + 1.0
            ):
                reader_with_handler.read_positions()
    finally:
        signal.signal(signal.SIGALRM, previous_handler)
    assert handler_http.requests == []


def test_recovery_reader_fails_closed_without_posix_timer_capability(
    monkeypatch,
):
    import telegram_kol_research.bound_close_reservation_recovery as recovery

    reader, _transport, http_client = _factory_recovery_reader(monkeypatch)
    monkeypatch.setattr(recovery.signal, "getitimer", None)

    with pytest.raises(
        BoundCloseReservationExchangeConfigurationError,
        match="unavailable",
    ):
        with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
            reader.read_positions()

    assert http_client.requests == []
