from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import copy
import gc
import inspect
import json
from pathlib import Path
import pickle
import signal
import sqlite3
import subprocess
import sys
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
    BoundCloseReservationRecoveryConflict,
    BoundCloseReservationRecoveryResult,
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
    BOUND_CLOSE_RESERVATION_APPLY_AUTHORIZATION,
    BOUND_CLOSE_RESERVATION_CANONICAL_APPLY_AUTHORIZATION,
    BOUND_CLOSE_RESERVATION_TERMINAL_ONLY_AUTHORIZATION,
    _claim_sealed_recovery_capture_for_apply,
    _canonical_json,
    _sha256_json,
    apply_bound_close_reservation_recovery,
    build_bound_close_reservation_recovery_plan,
    build_bound_close_reservation_exchange_reader_from_env,
    capture_and_seal_bound_close_reservation_recovery,
    classify_bound_close_reservation,
    exchange_recovery_reason_from_error,
    load_bound_close_reservation_source,
    recapture_and_seal_applied_bound_close_reservation_recovery,
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
            client_order_id TEXT,
            pos_id TEXT,
            related_order_id TEXT,
            before_json TEXT,
            after_json TEXT,
            request_json TEXT,
            response_json TEXT,
            notification_fingerprint TEXT UNIQUE,
            notification_attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE trading_settings (
            id INTEGER PRIMARY KEY,
            key TEXT NOT NULL UNIQUE,
            value_json TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT
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
            """INSERT INTO execution_events (
                   id, execution_binding_id, strategy_instance_id, venue,
                   action, status, symbol, side, order_id, pos_id, before_json,
                   after_json, request_json, response_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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


def test_pure_classifier_outputs_and_plan_documents_cannot_issue_apply_capability(
    tmp_path,
):
    source = _loaded_seal_source(tmp_path)
    local = source.reservations[0]
    exchange = _terminal_exchange_for_local(local)
    first = classify_bound_close_reservation(
        local,
        exchange,
        capture_completed_at=PLAN_CAPTURE_COMPLETED,
    )
    second = classify_bound_close_reservation(
        local,
        exchange,
        capture_completed_at=PLAN_CAPTURE_COMPLETED + timedelta(seconds=1),
    )
    plan = build_bound_close_reservation_recovery_plan(
        source_fingerprint=source.source_fingerprint,
        observations=(first,),
    )

    assert first == second
    assert json.loads(
        serialize_bound_close_reservation_recovery_plan(
            plan,
            capture_started_at=PLAN_CAPTURE_STARTED,
            capture_completed_at=PLAN_CAPTURE_COMPLETED,
        )
    )["status"] == "ready"
    assert not hasattr(
        recovery_module,
        "seal_bound_close_reservation_recovery_capture",
    )
    assert not hasattr(
        recovery_module,
        "_seal_bound_close_reservation_recovery_capture",
    )
    assert not hasattr(recovery_module, "_SEALED_CAPTURE_CONSTRUCTOR")
    assert not hasattr(recovery_module, "_SEALED_CAPTURE_ISSUANCE")
    with pytest.raises(TypeError, match="cannot be constructed directly"):
        SealedRecoveryCapture(
            plan=plan,
            serialized_plan="{}",
            source_capability=source._capability,
            _constructor=object(),
        )
    forged = object.__new__(SealedRecoveryCapture)
    forged._SealedRecoveryCapture__apply_claimed = False
    forged._SealedRecoveryCapture__apply_claim_lock = threading.Lock()
    forged._SealedRecoveryCapture__plan = plan
    forged._SealedRecoveryCapture__serialized_plan = "{}"
    forged._SealedRecoveryCapture__source_capability = source._capability
    with pytest.raises(ValueError, match="privately issued"):
        _claim_sealed_recovery_capture_for_apply(forged)


def _provider_milliseconds(value: datetime) -> str:
    return str(int(value.timestamp() * 1000))


def _terminal_provider_responses(source: BoundCloseReservationSource):
    order_time = CLASSIFICATION_TIME + timedelta(minutes=1)
    close_time = CLASSIFICATION_TIME + timedelta(minutes=2)
    raw_rows = sorted(
        (
            (local, source._capability._get(local.reservation_ref))
            for local in source.reservations
        ),
        key=lambda item: (item[1].instrument_id, item[1].order_id),
    )
    responses = [
        {"code": "0", "data": []},
        {"code": "0", "data": []},
    ]
    for local, raw in raw_rows:
        identity = {
            "instId": raw.instrument_id,
            "closePosId": raw.position_id,
            "posSide": local.side,
        }
        responses.extend((
            {
            "code": "0",
            "data": [
                {
                    **identity,
                    "ordId": raw.order_id,
                    "status": "filled",
                    "sz": "1",
                    "accFillSz": "1.0",
                    "cTime": _provider_milliseconds(order_time - timedelta(seconds=1)),
                    "uTime": _provider_milliseconds(order_time),
                }
            ],
            },
            {
            "code": "0",
            "data": [
                {
                    **identity,
                    "ordId": raw.order_id,
                    "fillSz": "1.00",
                    "cTime": _provider_milliseconds(order_time - timedelta(seconds=1)),
                    "fillTime": _provider_milliseconds(order_time),
                    "uTime": _provider_milliseconds(order_time + timedelta(seconds=1)),
                }
            ],
            },
        ))
    for local, raw in sorted(
        raw_rows,
        key=lambda item: (item[1].instrument_id, item[1].position_id),
    ):
        responses.append({
            "code": "0",
            "data": [
                {
                    "instId": raw.instrument_id,
                    "posId": raw.position_id,
                    "posSide": local.side,
                    "pos": "1",
                    "closePos": "1.000",
                    "cTime": _provider_milliseconds(order_time),
                    "uTime": _provider_milliseconds(close_time),
                }
            ],
        })
    return responses


def _capture_with_provider_responses(monkeypatch, source, responses):
    http_client = _RecoveryReaderHttpClient(responses=responses)
    reader, _transport, _selected = _factory_recovery_reader(
        monkeypatch,
        http_client=http_client,
    )
    capture = capture_and_seal_bound_close_reservation_recovery(
        source,
        reader,
        deadline_monotonic=time.monotonic() + 5.0,
    )
    return capture, reader, http_client


def test_authoritative_capture_reads_every_exact_collection_and_seals_terminal(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path)
    responses = _terminal_provider_responses(source)
    responses[2]["data"][0]["px"] = "98765.43"
    responses[2]["data"][0]["providerSecret"] = "never-serialize-provider-payload"

    capture, _reader, http_client = _capture_with_provider_responses(
        monkeypatch,
        source,
        responses,
    )

    assert isinstance(capture, SealedRecoveryCapture)
    assert capture.plan.status == "ready"
    assert capture.plan.action_count == 1
    assert len(http_client.requests) == 5
    assert {request[0] for request in http_client.requests} == {"GET"}
    assert sum("account/positions?" in row[1] for row in http_client.requests) == 1
    assert sum("orders-pending?" in row[1] for row in http_client.requests) == 1
    assert sum("orders-history?" in row[1] for row in http_client.requests) == 1
    assert sum("trade/fills?" in row[1] for row in http_client.requests) == 1
    assert sum("positions-history?" in row[1] for row in http_client.requests) == 1
    assert "raw-pos-901" not in capture.serialized_plan
    assert "raw-close-order-901" not in capture.serialized_plan
    assert "98765.43" not in capture.serialized_plan
    assert "never-serialize-provider-payload" not in capture.serialized_plan
    assert repr(capture) == "<SealedRecoveryCapture opaque>"


def test_authoritative_capture_accepts_consistent_closed_semantic_fields(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path)
    responses = _terminal_provider_responses(source)
    order_row = responses[2]["data"][0]
    order_row["posId"] = order_row["closePosId"]
    order_row["state"] = "FILLED"
    position_row = responses[4]["data"][0]
    position_row["closePosId"] = position_row["posId"]

    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        responses,
    )

    assert capture.plan.status == "ready"
    assert capture.plan.observations[0].classification is (
        ReservationClassification.PROVEN_TERMINAL
    )


def test_authoritative_capture_deduplicates_only_exact_raw_queries(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path, row_count=2)

    capture, _reader, http_client = _capture_with_provider_responses(
        monkeypatch,
        source,
        _terminal_provider_responses(source),
    )

    assert capture.plan.status == "ready"
    assert capture.plan.action_count == 2
    assert len(http_client.requests) == 8


def test_authoritative_capture_reports_active_current_position(tmp_path, monkeypatch):
    source = _loaded_seal_source(tmp_path)
    responses = _terminal_provider_responses(source)
    raw = source._capability._get(source.reservations[0].reservation_ref)
    responses[0] = {
        "code": "0",
        "data": [
            {
                "instId": raw.instrument_id,
                "posId": raw.position_id,
                "posSide": "long",
                "pos": "1",
            }
        ],
    }

    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        responses,
    )

    assert capture.plan.status == "refused"
    assert capture.plan.observations[0].classification is (
        ReservationClassification.ACTIVE
    )
    assert capture.plan.observations[0].reason_code == (
        "exact_position_currently_live"
    )


def test_authoritative_capture_rejects_old_sz_only_current_position_shape(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path)
    responses = _terminal_provider_responses(source)
    raw = source._capability._get(source.reservations[0].reservation_ref)
    responses[0] = {
        "code": "0",
        "data": [
            {
                "instId": raw.instrument_id,
                "posId": raw.position_id,
                "posSide": "long",
                "sz": "1",
            }
        ],
    }

    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        responses,
    )

    assert capture.plan.status == "refused"
    assert capture.plan.observations[0].classification is (
        ReservationClassification.UNKNOWN
    )
    assert capture.plan.observations[0].reason_code == "exchange_schema_invalid"


def test_authoritative_capture_reports_exact_pending_close_order(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path)
    responses = _terminal_provider_responses(source)
    local = source.reservations[0]
    raw = source._capability._get(local.reservation_ref)
    responses[1] = {
        "code": "0",
        "data": [
            {
                "instId": raw.instrument_id,
                "closePosId": raw.position_id,
                "posSide": local.side,
                "ordId": raw.order_id,
                "status": "live",
                "sz": "1",
                "accFillSz": "0",
            }
        ],
    }

    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        responses,
    )

    assert capture.plan.status == "refused"
    assert capture.plan.observations[0].classification is (
        ReservationClassification.ACTIVE
    )
    assert capture.plan.observations[0].reason_code == (
        "exact_close_order_currently_pending"
    )


def test_authoritative_capture_closes_on_read_error_without_echoing_payload(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path)
    capture, _reader, http_client = _capture_with_provider_responses(
        monkeypatch,
        source,
        [
            {
                "code": "50001",
                "msg": "raw-provider-error-secret",
                "data": [{"posId": "raw-provider-position-secret"}],
            }
        ],
    )

    assert len(http_client.requests) == 1
    assert capture.plan.status == "refused"
    assert capture.plan.action_count == 0
    assert capture.plan.observations[0].classification is (
        ReservationClassification.UNKNOWN
    )
    assert capture.plan.observations[0].reason_code == (
        "exchange_evidence_unavailable"
    )
    assert "raw-provider-error-secret" not in capture.serialized_plan
    assert "raw-provider-position-secret" not in capture.serialized_plan


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("callback_missing", "exchange_history_incomplete"),
        ("page_limit", "exchange_history_incomplete"),
        ("identity_mismatch", "exchange_identity_conflict"),
        ("missing_identity", "exchange_schema_invalid"),
        ("duplicate_exact", "exchange_state_conflict"),
        ("ambiguous_alias", "exchange_schema_invalid"),
        ("conflicting_order_state", "exchange_schema_invalid"),
        ("inverted_order_time", "exchange_schema_invalid"),
        ("inverted_fill_time", "exchange_schema_invalid"),
        ("inverted_position_time", "exchange_schema_invalid"),
        ("conflicting_position_identity", "exchange_schema_invalid"),
        ("partial_position_close", "exchange_schema_invalid"),
        ("old_artificial_position_history_shape", "exchange_schema_invalid"),
        ("malformed_time", "exchange_schema_invalid"),
        ("malformed_decimal", "exchange_schema_invalid"),
    ],
)
def test_authoritative_capture_fails_closed_on_incomplete_or_malformed_exchange(
    tmp_path, monkeypatch, mutation, expected_reason
):
    source = _loaded_seal_source(tmp_path)
    responses = _terminal_provider_responses(source)
    if mutation == "callback_missing":
        responses[4] = {"code": "0", "data": []}
    elif mutation == "page_limit":
        responses[2]["data"] = [dict(responses[2]["data"][0]) for _ in range(100)]
    elif mutation == "identity_mismatch":
        responses[2]["data"][0]["ordId"] = "unrelated-order"
    elif mutation == "missing_identity":
        del responses[2]["data"][0]["ordId"]
    elif mutation == "duplicate_exact":
        responses[2]["data"].append(dict(responses[2]["data"][0]))
    elif mutation == "ambiguous_alias":
        responses[2]["data"][0]["fillSz"] = "1"
    elif mutation == "conflicting_order_state":
        responses[2]["data"][0]["state"] = "rejected"
    elif mutation == "inverted_order_time":
        responses[2]["data"][0]["cTime"] = _provider_milliseconds(
            CLASSIFICATION_TIME + timedelta(minutes=2)
        )
    elif mutation == "inverted_fill_time":
        responses[3]["data"][0]["uTime"] = _provider_milliseconds(
            CLASSIFICATION_TIME
        )
    elif mutation == "inverted_position_time":
        responses[4]["data"][0]["cTime"] = _provider_milliseconds(
            CLASSIFICATION_TIME + timedelta(minutes=3)
        )
    elif mutation == "conflicting_position_identity":
        responses[4]["data"][0]["closePosId"] = "different-position"
    elif mutation == "partial_position_close":
        responses[4]["data"][0]["pos"] = "2"
    elif mutation == "old_artificial_position_history_shape":
        row = responses[4]["data"][0]
        del row["pos"]
        del row["closePos"]
        row["state"] = "closed"
        row["closedSize"] = "1"
    elif mutation == "malformed_time":
        responses[2]["data"][0]["uTime"] = "1"
    elif mutation == "malformed_decimal":
        responses[3]["data"][0]["fillSz"] = "not-a-decimal"

    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        responses,
    )

    assert capture.plan.status == "refused"
    assert capture.plan.action_count == 0
    assert capture.plan.observations[0].classification is (
        ReservationClassification.UNKNOWN
    )
    assert capture.plan.observations[0].reason_code == expected_reason


def test_authoritative_capture_requires_fresh_reader_for_each_stable_window(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path)
    clock = iter(
        (
            PLAN_CAPTURE_STARTED,
            PLAN_CAPTURE_COMPLETED,
            PLAN_CAPTURE_STARTED + timedelta(minutes=2),
            PLAN_CAPTURE_COMPLETED + timedelta(minutes=2),
            PLAN_CAPTURE_STARTED + timedelta(minutes=4),
            PLAN_CAPTURE_COMPLETED + timedelta(minutes=4),
        )
    )
    monkeypatch.setattr(recovery_module, "_recovery_capture_now", lambda: next(clock))

    first_http = _RecoveryReaderHttpClient(
        responses=_terminal_provider_responses(source)
    )
    first_reader, _transport, _http = _factory_recovery_reader(
        monkeypatch,
        http_client=first_http,
    )
    first = capture_and_seal_bound_close_reservation_recovery(
        source,
        first_reader,
        deadline_monotonic=time.monotonic() + 5.0,
    )
    reused = capture_and_seal_bound_close_reservation_recovery(
        source,
        first_reader,
        deadline_monotonic=time.monotonic() + 5.0,
    )
    assert reused.plan.status == "refused"
    assert len(first_http.requests) == 5

    second_http = _RecoveryReaderHttpClient(
        responses=_terminal_provider_responses(source)
    )
    second_reader, _transport, _http = _factory_recovery_reader(
        monkeypatch,
        http_client=second_http,
    )
    second = capture_and_seal_bound_close_reservation_recovery(
        source,
        second_reader,
        deadline_monotonic=time.monotonic() + 5.0,
    )

    first_payload = json.loads(first.serialized_plan)
    second_payload = json.loads(second.serialized_plan)
    assert first.plan.evidence_fingerprint == second.plan.evidence_fingerprint
    assert first_payload["capture_identity"] != second_payload["capture_identity"]
    assert len(second_http.requests) == 5

    first_file = tmp_path / "first-actual-capture.json"
    second_file = tmp_path / "second-actual-capture.json"
    first_file.write_text(first.serialized_plan, encoding="utf-8")
    second_file.write_text(second.serialized_plan, encoding="utf-8")
    first_file.chmod(0o600)
    second_file.chmod(0o600)
    comparison = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "compare_bound_close_reservation_dry_runs.py"
            ),
            str(first_file),
            str(second_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert comparison.returncode == 0
    assert comparison.stdout == '{"status":"stable"}\n'
    assert comparison.stderr == ""


def test_authoritative_capture_public_api_has_no_timestamp_or_evidence_injection():
    parameters = inspect.signature(
        capture_and_seal_bound_close_reservation_recovery
    ).parameters

    assert set(parameters) == {"source", "reader", "deadline_monotonic"}
    assert "now_provider" not in parameters
    assert "observations" not in parameters
    assert "exchange_evidence" not in parameters
    assert "capture_completed_at" not in parameters


def test_authoritative_capture_rejects_non_dedicated_reader_before_reads(tmp_path):
    source = _loaded_seal_source(tmp_path)

    with pytest.raises(TypeError, match="dedicated recovery reader"):
        capture_and_seal_bound_close_reservation_recovery(
            source,
            object(),  # type: ignore[arg-type]
            deadline_monotonic=time.monotonic() + 5.0,
        )


def test_sealed_apply_capability_is_private_issuer_bound_and_one_shot(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path)
    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        _terminal_provider_responses(source),
    )

    claimed = _claim_sealed_recovery_capture_for_apply(capture)

    assert claimed.plan is capture.plan
    assert not hasattr(claimed, "__dict__")
    with pytest.raises(ValueError, match="already claimed"):
        _claim_sealed_recovery_capture_for_apply(capture)


def test_failed_claim_validation_does_not_consume_issued_capture(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path)
    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        _terminal_provider_responses(source),
    )
    original_source_capability = (
        capture._SealedRecoveryCapture__source_capability
    )
    capture._SealedRecoveryCapture__source_capability = object()

    with pytest.raises(ValueError, match="issued contents"):
        _claim_sealed_recovery_capture_for_apply(capture)

    capture._SealedRecoveryCapture__source_capability = original_source_capability
    assert _claim_sealed_recovery_capture_for_apply(capture).plan is capture.plan


def test_issued_capture_rejects_plan_source_and_serialized_substitution(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path)
    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        _terminal_provider_responses(source),
    )
    original_plan = capture.plan
    original_serialized = capture.serialized_plan
    original_source_capability = capture._SealedRecoveryCapture__source_capability
    substituted_observation = replace(
        original_plan.observations[0],
        exchange_fingerprint="f" * 64,
    )
    substituted_plan = build_bound_close_reservation_recovery_plan(
        source_fingerprint=original_plan.source_fingerprint,
        observations=(substituted_observation,),
    )
    other_directory = tmp_path / "other"
    other_directory.mkdir()
    other_source = _loaded_seal_source(other_directory)

    capture._SealedRecoveryCapture__plan = substituted_plan
    with pytest.raises(ValueError, match="issued contents"):
        _claim_sealed_recovery_capture_for_apply(capture)
    capture._SealedRecoveryCapture__plan = original_plan

    capture._SealedRecoveryCapture__source_capability = other_source._capability
    with pytest.raises(ValueError, match="issued contents"):
        _claim_sealed_recovery_capture_for_apply(capture)
    capture._SealedRecoveryCapture__source_capability = original_source_capability

    capture._SealedRecoveryCapture__serialized_plan = "{}"
    with pytest.raises(ValueError, match="issued contents"):
        _claim_sealed_recovery_capture_for_apply(capture)
    capture._SealedRecoveryCapture__serialized_plan = original_serialized

    assert _claim_sealed_recovery_capture_for_apply(capture).plan is original_plan


def test_issued_refused_capture_rejects_in_place_plan_mutation_to_ready(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path)
    responses = _terminal_provider_responses(source)
    responses[4] = {"code": "0", "data": []}
    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        responses,
    )
    local = source.reservations[0]
    manual_observation = classify_bound_close_reservation(
        local,
        _terminal_exchange_for_local(local),
        capture_completed_at=PLAN_CAPTURE_COMPLETED,
    )
    manual_ready = build_bound_close_reservation_recovery_plan(
        source_fingerprint=source.source_fingerprint,
        observations=(manual_observation,),
    )
    assert capture.plan.status == "refused"
    for field in fields(BoundCloseReservationRecoveryPlan):
        object.__setattr__(
            capture.plan,
            field.name,
            getattr(manual_ready, field.name),
        )
    assert capture.plan.status == "ready"

    with pytest.raises(ValueError, match="issued contents"):
        _claim_sealed_recovery_capture_for_apply(capture)

    assert id(capture) in recovery_module._SEALED_CAPTURE_ISSUANCE_REGISTRY


_RAW_AUTHORITY_FIELDS = (
    "reservation_id",
    "source_status",
    "binding_id",
    "event_id",
    "position_id",
    "order_id",
    "instrument_id",
    "entry_leg_id",
    "mutation_ids",
)


def _changed_raw_authority_value(raw, field_name):
    original = getattr(raw, field_name)
    if field_name in {"reservation_id", "binding_id", "event_id"}:
        return original + 1000
    if field_name == "entry_leg_id":
        return 1000 if original is None else original + 1000
    if field_name == "mutation_ids":
        return (*original, 1000)
    if field_name == "source_status":
        return "submitted" if original != "submitted" else "reserved"
    return f"{original}-changed"


@pytest.mark.parametrize("field_name", _RAW_AUTHORITY_FIELDS)
def test_raw_reservation_authority_is_immutable(tmp_path, field_name):
    source = _loaded_seal_source(tmp_path)
    raw = source._capability._get(source.reservations[0].reservation_ref)

    with pytest.raises(AttributeError, match="immutable"):
        setattr(raw, field_name, _changed_raw_authority_value(raw, field_name))


@pytest.mark.parametrize("field_name", _RAW_AUTHORITY_FIELDS)
def test_raw_reservation_authority_cannot_be_deleted(tmp_path, field_name):
    source = _loaded_seal_source(tmp_path)
    raw = source._capability._get(source.reservations[0].reservation_ref)

    with pytest.raises(AttributeError, match="immutable"):
        delattr(raw, field_name)


def test_claim_binds_complete_canonical_raw_source_snapshot(tmp_path, monkeypatch):
    source = _loaded_seal_source(tmp_path)
    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        _terminal_provider_responses(source),
    )
    raw = source._capability._get(source.reservations[0].reservation_ref)

    for field_name in _RAW_AUTHORITY_FIELDS:
        original = getattr(raw, field_name)
        object.__setattr__(
            raw,
            field_name,
            _changed_raw_authority_value(raw, field_name),
        )
        try:
            with pytest.raises(ValueError, match="raw source authority"):
                _claim_sealed_recovery_capture_for_apply(capture)
        finally:
            object.__setattr__(raw, field_name, original)

    object.__setattr__(raw, "_RawReservationCapability__sealed", False)
    try:
        with pytest.raises(ValueError, match="raw source authority"):
            _claim_sealed_recovery_capture_for_apply(capture)
    finally:
        object.__setattr__(raw, "_RawReservationCapability__sealed", True)

    assert _claim_sealed_recovery_capture_for_apply(capture).plan is capture.plan


def test_claim_binds_sorted_raw_source_reference_population(tmp_path, monkeypatch):
    source = _loaded_seal_source(tmp_path, row_count=2)
    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        _terminal_provider_responses(source),
    )
    raw_mapping = (
        source._capability
        ._BoundCloseReservationSourceCapability__raw_by_reservation_ref
    )
    issued = recovery_module._SEALED_CAPTURE_ISSUANCE_REGISTRY[id(capture)]
    first_ref = next(iter(raw_mapping))
    first_raw = raw_mapping.pop(first_ref)
    raw_mapping[first_ref] = first_raw
    assert recovery_module._raw_source_snapshot_fingerprint(
        source._capability
    ) == issued.raw_source_snapshot_fingerprint
    raw = next(iter(raw_mapping.values()))
    forged_ref = "f" * 64
    raw_mapping[forged_ref] = raw

    with pytest.raises(ValueError, match="raw source authority"):
        _claim_sealed_recovery_capture_for_apply(capture)

    raw_mapping.pop(forged_ref)
    assert _claim_sealed_recovery_capture_for_apply(capture).plan is capture.plan


def test_sealed_capture_issuance_registry_uses_exact_weak_identity(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path)
    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        _terminal_provider_responses(source),
    )
    capture_id = id(capture)
    capture_ref = weakref.ref(capture)

    issued = recovery_module._SEALED_CAPTURE_ISSUANCE_REGISTRY[capture_id]
    assert issued.capture_ref() is capture
    assert issued.plan is capture.plan
    assert len(recovery_module._SEALED_CAPTURE_ISSUANCE_REGISTRY) <= (
        recovery_module._MAX_SEALED_CAPTURE_ISSUANCE
    )

    del capture
    gc.collect()

    assert capture_ref() is None
    assert capture_id not in recovery_module._SEALED_CAPTURE_ISSUANCE_REGISTRY


def test_sealed_capture_issuance_registry_is_bounded_and_evicts_closed(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path)
    captures = []
    for _ in range(recovery_module._MAX_SEALED_CAPTURE_ISSUANCE + 1):
        capture, _reader, _http = _capture_with_provider_responses(
            monkeypatch,
            source,
            _terminal_provider_responses(source),
        )
        captures.append(capture)

    assert len(recovery_module._SEALED_CAPTURE_ISSUANCE_REGISTRY) == (
        recovery_module._MAX_SEALED_CAPTURE_ISSUANCE
    )
    with pytest.raises(ValueError, match="privately issued"):
        _claim_sealed_recovery_capture_for_apply(captures[0])
    assert _claim_sealed_recovery_capture_for_apply(captures[-1]).plan.status == (
        "ready"
    )


def test_concurrent_claim_consumes_exact_issued_capture_once(tmp_path, monkeypatch):
    source = _loaded_seal_source(tmp_path)
    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        _terminal_provider_responses(source),
    )
    barrier = threading.Barrier(3)
    claims = []
    errors = []
    result_lock = threading.Lock()

    def claim() -> None:
        barrier.wait()
        try:
            result = _claim_sealed_recovery_capture_for_apply(capture)
        except Exception as exc:
            with result_lock:
                errors.append(exc)
        else:
            with result_lock:
                claims.append(result)

    workers = [threading.Thread(target=claim) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()

    assert len(claims) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)


def test_raw_and_sealed_capabilities_reject_pickle_copy_and_deepcopy(
    tmp_path, monkeypatch
):
    source = _loaded_seal_source(tmp_path)
    local = source.reservations[0]
    raw_source_capability = source._capability
    raw_reservation_capability = raw_source_capability._get(local.reservation_ref)
    sealed, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        _terminal_provider_responses(source),
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
            "INSERT INTO execution_events (id, execution_binding_id, "
            "strategy_instance_id, venue, action, status, symbol, side, "
            "order_id, pos_id, before_json, after_json, request_json, "
            "response_json, created_at) "
            "SELECT 99, execution_binding_id, "
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
    def __init__(self, *, stream_mode="normal", responses=None):
        self.requests = []
        self.close_calls = 0
        self.stream_mode = stream_mode
        self.yielded = 0
        self.responses = list(responses or [])

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
                payload = (
                    self.responses.pop(0)
                    if self.responses
                    else {"code": "0", "data": []}
                )
                yield json.dumps(payload, separators=(",", ":")).encode("utf-8")

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


APPLY_TIME = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _apply_authorization() -> str:
    return BOUND_CLOSE_RESERVATION_CANONICAL_APPLY_AUTHORIZATION


def test_apply_authorization_is_one_exact_canonical_two_line_value():
    canonical = (
        BOUND_CLOSE_RESERVATION_APPLY_AUTHORIZATION
        + "\n"
        + BOUND_CLOSE_RESERVATION_TERMINAL_ONLY_AUTHORIZATION
    )
    assert BOUND_CLOSE_RESERVATION_CANONICAL_APPLY_AUTHORIZATION == canonical
    assert canonical.splitlines() == [
        "I_APPROVE_BOUND_CLOSE_RESERVATIONS_ALL_DB_UNITS_STOPPED_APPLY_CAPTURE",
        "I_AUTHORIZE_BOUND_CLOSE_RESERVATIONS_PROVEN_TERMINAL_ONLY",
    ]


def _apply_database(tmp_path, *, name: str = "apply.sqlite3", row_count: int = 1):
    database = tmp_path / name
    connection = _create_reservation_source_database(database)
    for offset in range(row_count):
        _seed_local_reservation(connection, row_id=901 + offset)
    connection.execute(
        "INSERT INTO trading_settings "
        "(id, key, value_json, created_at, updated_at) VALUES (1, 'global', ?, ?, ?)",
        ('{"mimo_contract_mode":"v1"}', SOURCE_TIME, SOURCE_TIME),
    )
    connection.commit()
    connection.close()
    return database


def _ready_apply_capture(monkeypatch, database):
    monkeypatch.setattr(
        recovery_module,
        "_recovery_capture_now",
        lambda: APPLY_TIME - timedelta(minutes=1),
    )
    source = load_bound_close_reservation_source(database)
    capture, _reader, http_client = _capture_with_provider_responses(
        monkeypatch,
        source,
        _terminal_provider_responses(source),
    )
    assert capture.plan.status == "ready"
    return capture, http_client


def _apply_ready(database, capture, **overrides):
    plan = overrides.pop("plan", capture.plan)
    arguments = {
        "plan": plan,
        "capture": capture,
        "expected_fingerprint": plan.evidence_fingerprint,
        "expected_action_count": plan.action_count,
        "confirmation_token": plan.confirmation_token,
        "authorization": _apply_authorization(),
        "applied_at": APPLY_TIME,
    }
    arguments.update(overrides)
    return apply_bound_close_reservation_recovery(database, **arguments)


def _table_rows(database, table_name: str):
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                f'SELECT * FROM "{table_name}" ORDER BY id'
            ).fetchall()
        ]


@pytest.mark.parametrize(
    "override",
    [
        {"expected_fingerprint": "0" * 64},
        {"expected_action_count": 0},
        {"expected_action_count": True},
        {"confirmation_token": "wrong"},
        {"authorization": BOUND_CLOSE_RESERVATION_APPLY_AUTHORIZATION},
        {"authorization": BOUND_CLOSE_RESERVATION_TERMINAL_ONLY_AUTHORIZATION},
        {"authorization": ""},
        {"authorization": _apply_authorization().replace("\n", "\r\n")},
        {"authorization": "\n".join(reversed(_apply_authorization().splitlines()))},
        {"authorization": _apply_authorization() + "\n"},
        {"authorization": _apply_authorization() + "\n" + _apply_authorization()},
        {"applied_at": APPLY_TIME.replace(tzinfo=None)},
        {"applied_at": datetime(2020, 1, 1, tzinfo=UTC)},
    ],
)
def test_apply_refuses_public_gate_mismatch_before_writable_open(
    tmp_path, monkeypatch, override
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    opened = []
    real_open = recovery_module._open_bound_close_reservation_writable_connection

    def record_open(path):
        opened.append(path)
        return real_open(path)

    monkeypatch.setattr(
        recovery_module,
        "_open_bound_close_reservation_writable_connection",
        record_open,
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict):
        _apply_ready(database, capture, **override)

    assert opened == []
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"


def test_apply_requires_privately_issued_unclaimed_fresh_capture_before_open(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    _claim_sealed_recovery_capture_for_apply(capture)
    opened = []
    monkeypatch.setattr(
        recovery_module,
        "_open_bound_close_reservation_writable_connection",
        lambda path: opened.append(path),
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="capture"):
        _apply_ready(database, capture)

    assert opened == []


def test_apply_refuses_missing_database_without_creating_a_writable_file(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    missing = tmp_path / "must-not-be-created.sqlite3"

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="database_path"):
        _apply_ready(missing, capture)

    assert not missing.exists()
    assert _claim_sealed_recovery_capture_for_apply(capture).plan is capture.plan


def test_apply_refuses_plan_capture_exchange_drift_before_writable_open(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    first, _http = _ready_apply_capture(monkeypatch, database)
    source = load_bound_close_reservation_source(database)
    changed = _terminal_provider_responses(source)
    changed[2]["data"][0]["uTime"] = _provider_milliseconds(
        CLASSIFICATION_TIME + timedelta(minutes=1, seconds=1)
    )
    second, _reader, _http2 = _capture_with_provider_responses(
        monkeypatch, source, changed
    )
    assert second.plan.exchange_snapshot_fingerprint != (
        first.plan.exchange_snapshot_fingerprint
    )
    opened = []
    monkeypatch.setattr(
        recovery_module,
        "_open_bound_close_reservation_writable_connection",
        lambda path: opened.append(path),
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="exchange"):
        _apply_ready(database, second, plan=first.plan)

    assert opened == []
    assert _claim_sealed_recovery_capture_for_apply(second).plan is second.plan


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            "UPDATE trading_settings SET value_json = "
            "'{\"mimo_contract_mode\":\"v2_live_adapter\"}' WHERE key = 'global'",
            "mimo_contract_mode",
        ),
        (
            "UPDATE execution_bindings SET status = 'active' WHERE id = 901",
            "source",
        ),
        (
            "UPDATE bound_position_close_reservations SET status = 'confirmed' "
            "WHERE id = 901",
            "state",
        ),
        (
            "INSERT INTO execution_events "
            "(id, venue, action, status, notification_fingerprint, created_at) "
            "VALUES (9999, 'deepcoin', "
            "'bound_close_reservation_history_converged', 'succeeded', "
            "'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff', "
            "'2026-08-15 10:00:00.000000')",
            "audit",
        ),
    ],
)
def test_apply_fails_closed_on_locked_database_drift(
    tmp_path, monkeypatch, mutation, reason
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    with sqlite3.connect(database) as connection:
        connection.execute(mutation)
        connection.commit()

    with pytest.raises(BoundCloseReservationRecoveryConflict, match=reason):
        _apply_ready(database, capture)

    rows = _table_rows(database, "bound_position_close_reservations")
    assert all(row["updated_at"] == SOURCE_TIME for row in rows)


def test_apply_refuses_new_or_mixed_reservation_population(tmp_path, monkeypatch):
    for suffix, mutate in (
        (
            "new",
            lambda connection: _seed_local_reservation(connection, row_id=902),
        ),
        (
            "mixed",
            lambda connection: connection.execute(
                "UPDATE bound_position_close_reservations "
                "SET status = 'confirmed' WHERE id = 901"
            ),
        ),
    ):
        database = _apply_database(
            tmp_path,
            name=f"{suffix}.sqlite3",
            row_count=2 if suffix == "mixed" else 1,
        )
        capture, _http = _ready_apply_capture(monkeypatch, database)
        with sqlite3.connect(database) as connection:
            mutate(connection)
            connection.commit()
        with pytest.raises(BoundCloseReservationRecoveryConflict, match="state|source"):
            _apply_ready(database, capture)


def test_apply_changes_only_exact_reservations_and_one_redacted_aggregate_event(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path, row_count=2)
    capture, http_client = _ready_apply_capture(monkeypatch, database)
    unchanged_tables = (
        "execution_bindings",
        "execution_order_legs",
        "position_mutation_intents",
        "trading_settings",
    )
    before = {table: _table_rows(database, table) for table in unchanged_tables}
    original_events = _table_rows(database, "execution_events")
    exchange_call_count = len(http_client.requests)
    statements = []
    real_open = recovery_module._open_bound_close_reservation_writable_connection

    def traced_open(path):
        connection = real_open(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(
        recovery_module,
        "_open_bound_close_reservation_writable_connection",
        traced_open,
    )

    result = _apply_ready(database, capture)

    assert isinstance(result, BoundCloseReservationRecoveryResult)
    assert result.status == "applied"
    assert result.evidence_fingerprint == capture.plan.evidence_fingerprint
    assert result.action_count == 2
    assert statements[0] == "BEGIN IMMEDIATE"
    assert statements[-1] == "COMMIT"
    reservations = _table_rows(database, "bound_position_close_reservations")
    assert [row["status"] for row in reservations] == ["confirmed", "confirmed"]
    assert all(row["last_error"] is None for row in reservations)
    assert all(row["updated_at"] == "2026-08-15 12:00:00.000000" for row in reservations)
    assert all(row["created_at"] == SOURCE_TIME for row in reservations)
    for table in unchanged_tables:
        assert _table_rows(database, table) == before[table]
    assert len(http_client.requests) == exchange_call_count

    events = _table_rows(database, "execution_events")
    assert events[:-1] == original_events
    event = events[-1]
    assert event["action"] == "bound_close_reservation_history_converged"
    assert event["status"] == "succeeded"
    assert event["order_id"] is None
    assert event["client_order_id"] is None
    assert event["pos_id"] is None
    assert event["related_order_id"] is None
    assert event["notification_fingerprint"] == capture.plan.evidence_fingerprint
    assert event["notification_attempts"] == 0
    assert event["execution_binding_id"] is None
    assert len(event["before_json"].encode("utf-8")) <= 65_536
    assert len(event["after_json"].encode("utf-8")) <= 65_536
    audit_text = event["before_json"] + event["after_json"]
    assert "raw-pos" not in audit_text
    assert "raw-close-order" not in audit_text
    assert "provider" not in audit_text
    assert json.loads(event["before_json"])["action_count"] == 2
    assert json.loads(event["after_json"])["status"] == "confirmed"
    before_items = json.loads(event["before_json"])["reservations"]
    assert all(
        set(item) == {
            "durable_invariant_fingerprint",
            "reservation_ref",
            "status",
        }
        and len(item["durable_invariant_fingerprint"]) == 64
        for item in before_items
    )


@pytest.mark.parametrize("fail_statement", [1, 2, 3])
def test_apply_rolls_back_every_mutation_statement_boundary(
    tmp_path, monkeypatch, fail_statement
):
    database = _apply_database(
        tmp_path,
        name=f"rollback-{fail_statement}.sqlite3",
        row_count=2,
    )
    capture, _http = _ready_apply_capture(monkeypatch, database)
    before_reservations = _table_rows(database, "bound_position_close_reservations")
    before_events = _table_rows(database, "execution_events")
    real_execute = recovery_module._execute_bound_close_reservation_apply_statement
    calls = 0

    def fail_at_boundary(connection, sql, parameters=()):
        nonlocal calls
        calls += 1
        if calls == fail_statement:
            raise sqlite3.OperationalError("injected apply statement failure")
        return real_execute(connection, sql, parameters)

    monkeypatch.setattr(
        recovery_module,
        "_execute_bound_close_reservation_apply_statement",
        fail_at_boundary,
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="transaction"):
        _apply_ready(database, capture)

    assert _table_rows(database, "bound_position_close_reservations") == (
        before_reservations
    )
    assert _table_rows(database, "execution_events") == before_events


def test_apply_rejects_a_capture_issued_before_the_committed_audit_as_fresh(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    first, _http1 = _ready_apply_capture(monkeypatch, database)
    second, _http2 = _ready_apply_capture(monkeypatch, database)
    assert first is not second
    assert first.plan == second.plan

    applied = _apply_ready(database, first)
    with pytest.raises(BoundCloseReservationRecoveryConflict, match="fresh_capture"):
        _apply_ready(database, second)

    assert applied.status == "applied"
    events = [
        row
        for row in _table_rows(database, "execution_events")
        if row["action"] == "bound_close_reservation_history_converged"
    ]
    assert len(events) == 1


def test_expired_capture_refuses_before_writable_open_without_consuming_issuance(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    deadline = capture._SealedRecoveryCapture__deadline_monotonic
    opened = []
    monkeypatch.setattr(recovery_module.time, "monotonic", lambda: deadline + 1.0)
    monkeypatch.setattr(
        recovery_module,
        "_open_bound_close_reservation_writable_connection",
        lambda path: opened.append(path),
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="expired"):
        _apply_ready(database, capture)

    assert opened == []
    assert capture._SealedRecoveryCapture__apply_claimed is False
    assert id(capture) in recovery_module._SEALED_CAPTURE_ISSUANCE_REGISTRY


def test_claim_then_begin_immediate_delay_crossing_deadline_rolls_back_without_writes(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    deadline = capture._SealedRecoveryCapture__deadline_monotonic
    clock = {"now": deadline - 0.5}
    real_open = recovery_module._open_bound_close_reservation_writable_connection

    class DelayedBeginConnection:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def execute(self, sql, parameters=()):
            result = self._connection.execute(sql, parameters)
            if str(sql).strip().upper() == "BEGIN IMMEDIATE":
                clock["now"] = deadline + 0.5
            return result

    monkeypatch.setattr(recovery_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        recovery_module,
        "_open_bound_close_reservation_writable_connection",
        lambda path: DelayedBeginConnection(real_open(path)),
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="expired"):
        _apply_ready(database, capture)

    assert capture._SealedRecoveryCapture__apply_claimed is True
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"
    assert not [
        row
        for row in _table_rows(database, "execution_events")
        if row["action"] == "bound_close_reservation_history_converged"
    ]


def test_deadline_crossing_after_first_update_rolls_back_all_mutations(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path, row_count=2)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    deadline = capture._SealedRecoveryCapture__deadline_monotonic
    clock = {"now": deadline - 0.5}
    real_execute = recovery_module._execute_bound_close_reservation_apply_statement
    calls = 0

    def advance_after_first(connection, sql, parameters=()):
        nonlocal calls
        result = real_execute(connection, sql, parameters)
        calls += 1
        if calls == 1:
            clock["now"] = deadline + 0.5
        return result

    monkeypatch.setattr(recovery_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        recovery_module,
        "_execute_bound_close_reservation_apply_statement",
        advance_after_first,
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="expired"):
        _apply_ready(database, capture)

    assert calls == 1
    assert [
        row["status"]
        for row in _table_rows(database, "bound_position_close_reservations")
    ] == ["submitted", "submitted"]


def test_future_applied_at_refuses_before_claim_or_writable_open(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    opened = []
    monkeypatch.setattr(
        recovery_module,
        "_open_bound_close_reservation_writable_connection",
        lambda path: opened.append(path),
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="future"):
        _apply_ready(
            database,
            capture,
            applied_at=datetime(2100, 1, 1, tzinfo=UTC),
        )

    assert opened == []
    assert capture._SealedRecoveryCapture__apply_claimed is False
    assert id(capture) in recovery_module._SEALED_CAPTURE_ISSUANCE_REGISTRY


def test_post_apply_fresh_recapture_reloads_db_repeats_gets_and_is_idempotent(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    source = load_bound_close_reservation_source(database)
    first, first_http = _ready_apply_capture(monkeypatch, database)
    first_result = _apply_ready(database, first)
    assert first_result.status == "applied"
    assert load_bound_close_reservation_source(database).reservations == ()

    second_http = _RecoveryReaderHttpClient(
        responses=_terminal_provider_responses(source)
    )
    second_reader, _transport, _selected = _factory_recovery_reader(
        monkeypatch,
        http_client=second_http,
    )
    monkeypatch.setattr(
        recovery_module,
        "_recovery_capture_now",
        lambda: APPLY_TIME + timedelta(minutes=1),
    )
    second = recapture_and_seal_applied_bound_close_reservation_recovery(
        database,
        approved_plan=first.plan,
        reader=second_reader,
        deadline_monotonic=time.monotonic() + 5.0,
    )

    assert second is not first
    assert second.plan == first.plan
    assert len(first_http.requests) == 5
    assert len(second_http.requests) == 5
    assert {request[0] for request in second_http.requests} == {"GET"}
    repeated = _apply_ready(
        database,
        second,
        applied_at=APPLY_TIME + timedelta(minutes=2),
    )
    assert repeated.status == "already_applied"
    assert repeated.audit_event_id == first_result.audit_event_id
    events = [
        row
        for row in _table_rows(database, "execution_events")
        if row["action"] == "bound_close_reservation_history_converged"
    ]
    assert len(events) == 1


def test_already_applied_never_trusts_a_writer_connected_to_another_database(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path, name="idempotent-authority.sqlite3")
    source = load_bound_close_reservation_source(database)
    first, _http = _ready_apply_capture(monkeypatch, database)
    _apply_ready(database, first)

    second_http = _RecoveryReaderHttpClient(
        responses=_terminal_provider_responses(source)
    )
    second_reader, _transport, _selected = _factory_recovery_reader(
        monkeypatch,
        http_client=second_http,
    )
    monkeypatch.setattr(
        recovery_module,
        "_recovery_capture_now",
        lambda: APPLY_TIME + timedelta(minutes=1),
    )
    second = recapture_and_seal_applied_bound_close_reservation_recovery(
        database,
        approved_plan=first.plan,
        reader=second_reader,
        deadline_monotonic=time.monotonic() + 5.0,
    )

    swapped_database = tmp_path / "idempotent-swapped.sqlite3"
    with sqlite3.connect(database) as source_connection, sqlite3.connect(
        swapped_database
    ) as destination_connection:
        source_connection.backup(destination_connection)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE bound_position_close_reservations "
            "SET last_error = 'drift' WHERE id = 901"
        )
        connection.commit()

    real_open = recovery_module._open_bound_close_reservation_writable_connection
    real_identity_match = recovery_module._connection_matches_database_identity
    writer_connection = None

    def open_swapped(path):
        nonlocal writer_connection
        del path
        writer_connection = real_open(swapped_database)
        return writer_connection

    def simulate_unobservable_writer_identity(connection, *, expected):
        if connection is writer_connection:
            return True
        return real_identity_match(connection, expected=expected)

    monkeypatch.setattr(
        recovery_module,
        "_open_bound_close_reservation_writable_connection",
        open_swapped,
    )
    monkeypatch.setattr(
        recovery_module,
        "_connection_matches_database_identity",
        simulate_unobservable_writer_identity,
    )

    with pytest.raises(
        BoundCloseReservationRecoveryConflict,
        match="authority|outcome",
    ):
        _apply_ready(
            database,
            second,
            applied_at=APPLY_TIME + timedelta(minutes=2),
        )

    assert _table_rows(database, "bound_position_close_reservations")[0][
        "last_error"
    ] == "drift"
    assert len(
        [
            row
            for row in _table_rows(swapped_database, "execution_events")
            if row["action"] == "bound_close_reservation_history_converged"
        ]
    ) == 1


def test_post_apply_recapture_refuses_confirmed_row_drift_before_exchange_gets(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    source = load_bound_close_reservation_source(database)
    first, _http = _ready_apply_capture(monkeypatch, database)
    _apply_ready(database, first)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE bound_position_close_reservations "
            "SET updated_at = '2026-08-15 12:00:01.000000' WHERE id = 901"
        )
        connection.commit()
    http_client = _RecoveryReaderHttpClient(
        responses=_terminal_provider_responses(source)
    )
    reader, _transport, _selected = _factory_recovery_reader(
        monkeypatch,
        http_client=http_client,
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="source"):
        recapture_and_seal_applied_bound_close_reservation_recovery(
            database,
            approved_plan=first.plan,
            reader=reader,
            deadline_monotonic=time.monotonic() + 5.0,
        )

    assert http_client.requests == []


@pytest.mark.parametrize(
    "descendant_drift",
    [
        "UPDATE execution_bindings SET payload_json = '{\"changed\":true}' "
        "WHERE id = 901",
        "UPDATE execution_events SET response_json = '{\"changed\":true}' "
        "WHERE id = 901",
        "UPDATE execution_order_legs SET terminal_reason = 'changed' "
        "WHERE id = 901",
        "UPDATE position_mutation_intents SET response_json = '{\"changed\":true}' "
        "WHERE id = 901",
    ],
)
def test_post_apply_recapture_refuses_any_descendant_authority_drift(
    tmp_path, monkeypatch, descendant_drift
):
    database = _apply_database(tmp_path)
    source = load_bound_close_reservation_source(database)
    first, _http = _ready_apply_capture(monkeypatch, database)
    _apply_ready(database, first)
    with sqlite3.connect(database) as connection:
        connection.execute(descendant_drift)
        connection.commit()
    http_client = _RecoveryReaderHttpClient(
        responses=_terminal_provider_responses(source)
    )
    reader, _transport, _selected = _factory_recovery_reader(
        monkeypatch,
        http_client=http_client,
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="invariant"):
        recapture_and_seal_applied_bound_close_reservation_recovery(
            database,
            approved_plan=first.plan,
            reader=reader,
            deadline_monotonic=time.monotonic() + 5.0,
        )

    assert http_client.requests == []


@pytest.mark.parametrize(
    "forged_local",
    [
        lambda local: replace(
            local,
            event_created_at=local.event_created_at - timedelta(seconds=1),
        ),
        lambda local: replace(local, binding_status="active"),
    ],
)
def test_capture_rejects_replaced_loader_source_local_facts_before_any_get(
    tmp_path, monkeypatch, forged_local
):
    source = load_bound_close_reservation_source(
        _apply_database(tmp_path)
    )
    forged = replace(
        source,
        reservations=(forged_local(source.reservations[0]),),
    )
    http_client = _RecoveryReaderHttpClient(
        responses=_terminal_provider_responses(source)
    )
    reader, _transport, _selected = _factory_recovery_reader(
        monkeypatch,
        http_client=http_client,
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="source_authority"):
        capture_and_seal_bound_close_reservation_recovery(
            forged,
            reader,
            deadline_monotonic=time.monotonic() + 5.0,
        )

    assert http_client.requests == []


def test_capture_rejects_manual_source_and_mutated_issued_source_before_get(
    tmp_path, monkeypatch
):
    for case in ("manual", "mutated"):
        source = load_bound_close_reservation_source(
            _apply_database(tmp_path, name=f"source-{case}.sqlite3")
        )
        if case == "manual":
            candidate = BoundCloseReservationSource(
                reservations=source.reservations,
                source_fingerprint=source.source_fingerprint,
                _capability=source._capability,
            )
        else:
            candidate = source
            object.__setattr__(candidate, "source_fingerprint", "d" * 64)
        http_client = _RecoveryReaderHttpClient(
            responses=_terminal_provider_responses(source)
        )
        reader, _transport, _selected = _factory_recovery_reader(
            monkeypatch,
            http_client=http_client,
        )

        with pytest.raises(
            BoundCloseReservationRecoveryConflict,
            match="source_authority",
        ):
            capture_and_seal_bound_close_reservation_recovery(
                candidate,
                reader,
                deadline_monotonic=time.monotonic() + 5.0,
            )
        assert http_client.requests == []


def test_locked_plan_observation_binding_rejects_forged_local_fingerprint(
    tmp_path,
):
    database = _apply_database(tmp_path)
    source = load_bound_close_reservation_source(database)
    local = source.reservations[0]
    forged_observation = BoundCloseReservationObservation(
        reservation_ref=local.reservation_ref,
        classification=ReservationClassification.PROVEN_TERMINAL,
        reason_code="exact_close_and_position_terminal",
        source_fingerprint="d" * 64,
        exchange_fingerprint="e" * 64,
    )
    forged_plan = build_bound_close_reservation_recovery_plan(
        source_fingerprint=source.source_fingerprint,
        observations=(forged_observation,),
    )

    assert not recovery_module._locked_plan_observations_match_source(
        plan=forged_plan,
        source=source,
    )


def test_one_loader_issued_source_remains_valid_for_two_independent_captures(
    tmp_path, monkeypatch
):
    source = load_bound_close_reservation_source(_apply_database(tmp_path))
    first, _reader1, first_http = _capture_with_provider_responses(
        monkeypatch,
        source,
        _terminal_provider_responses(source),
    )
    second, _reader2, second_http = _capture_with_provider_responses(
        monkeypatch,
        source,
        _terminal_provider_responses(source),
    )

    assert first.plan == second.plan
    assert len(first_http.requests) == 5
    assert len(second_http.requests) == 5


def test_capture_from_database_a_cannot_apply_identical_database_b(
    tmp_path, monkeypatch
):
    database_a = _apply_database(tmp_path, name="authority-a.sqlite3")
    database_b = tmp_path / "authority-b.sqlite3"
    with sqlite3.connect(database_a) as source_connection, sqlite3.connect(
        database_b
    ) as destination_connection:
        source_connection.backup(destination_connection)
    monkeypatch.setattr(
        recovery_module,
        "_recovery_capture_now",
        lambda: APPLY_TIME - timedelta(minutes=1),
    )
    source = load_bound_close_reservation_source(database_a)
    capture, _reader, _http = _capture_with_provider_responses(
        monkeypatch,
        source,
        _terminal_provider_responses(source),
    )
    opened = []
    real_open = recovery_module._open_bound_close_reservation_writable_connection

    def record_open(path):
        opened.append(path)
        return real_open(path)

    monkeypatch.setattr(
        recovery_module,
        "_open_bound_close_reservation_writable_connection",
        record_open,
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="database_authority"):
        _apply_ready(database_b, capture)

    assert opened == []
    assert capture._SealedRecoveryCapture__apply_claimed is True
    assert _table_rows(database_b, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"


def test_apply_never_reports_success_for_open_time_database_aba_swap(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path, name="authority.sqlite3")
    swapped_database = tmp_path / "swapped-open.sqlite3"
    with sqlite3.connect(database) as source_connection, sqlite3.connect(
        swapped_database
    ) as destination_connection:
        source_connection.backup(destination_connection)
    monkeypatch.setattr(
        recovery_module,
        "_recovery_capture_now",
        lambda: APPLY_TIME - timedelta(minutes=1),
    )
    capture, _http = _ready_apply_capture(monkeypatch, database)
    real_open = recovery_module._open_bound_close_reservation_writable_connection
    real_identity_match = recovery_module._connection_matches_database_identity
    writer_connection = None

    def open_swapped(path):
        nonlocal writer_connection
        del path
        writer_connection = real_open(swapped_database)
        return writer_connection

    def simulate_unobservable_writer_identity(connection, *, expected):
        if connection is writer_connection:
            return True
        return real_identity_match(connection, expected=expected)

    monkeypatch.setattr(
        recovery_module,
        "_open_bound_close_reservation_writable_connection",
        open_swapped,
    )
    monkeypatch.setattr(
        recovery_module,
        "_connection_matches_database_identity",
        simulate_unobservable_writer_identity,
    )

    with pytest.raises(
        BoundCloseReservationRecoveryConflict,
        match="outcome|authority",
    ):
        _apply_ready(database, capture)

    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"
    assert _table_rows(swapped_database, "bound_position_close_reservations")[0][
        "status"
    ] == "confirmed"
    assert not [
        row
        for row in _table_rows(database, "execution_events")
        if row["action"] == "bound_close_reservation_history_converged"
    ]
    assert len(
        [
            row
            for row in _table_rows(swapped_database, "execution_events")
            if row["action"] == "bound_close_reservation_history_converged"
        ]
    ) == 1


@pytest.mark.parametrize(
    "trigger_sql",
    [
        """
        CREATE TRIGGER mutate_binding_after_reservation_update
        AFTER UPDATE ON bound_position_close_reservations
        BEGIN
          UPDATE execution_bindings SET status = 'active' WHERE id = NEW.id;
        END
        """,
        """
        CREATE TRIGGER mutate_settings_after_audit_insert
        AFTER INSERT ON execution_events
        WHEN NEW.action = 'bound_close_reservation_history_converged'
        BEGIN
          UPDATE trading_settings SET value_json = '{}'
          WHERE key = 'global';
        END
        """,
    ],
)
def test_apply_rejects_user_triggers_before_any_recovery_mutation(
    tmp_path, monkeypatch, trigger_sql
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    with sqlite3.connect(database) as connection:
        connection.executescript(trigger_sql)
        connection.commit()

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="trigger"):
        _apply_ready(database, capture)

    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"
    assert not [
        row
        for row in _table_rows(database, "execution_events")
        if row["action"] == "bound_close_reservation_history_converged"
    ]


def test_apply_authorizer_denies_unplanned_table_write_and_rolls_back(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    real_execute = recovery_module._execute_bound_close_reservation_apply_statement
    attempted = False

    def attempt_unplanned_write(connection, sql, parameters=()):
        nonlocal attempted
        if not attempted:
            attempted = True
            connection.execute(
                "UPDATE execution_bindings SET status = 'active' WHERE id = 901"
            )
        return real_execute(connection, sql, parameters)

    monkeypatch.setattr(
        recovery_module,
        "_execute_bound_close_reservation_apply_statement",
        attempt_unplanned_write,
    )

    with pytest.raises(
        BoundCloseReservationRecoveryConflict,
        match="transaction",
    ):
        _apply_ready(database, capture)

    assert attempted is True
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"
    assert _table_rows(database, "execution_bindings")[0]["status"] != "active"
    assert not [
        row
        for row in _table_rows(database, "execution_events")
        if row["action"] == "bound_close_reservation_history_converged"
    ]


@pytest.mark.parametrize("pragma_name", ["user_version", "application_id"])
def test_apply_authorizer_denies_header_pragma_write_and_rolls_back(
    tmp_path, monkeypatch, pragma_name
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    real_execute = recovery_module._execute_bound_close_reservation_apply_statement
    attempted = False

    def attempt_pragma_write(connection, sql, parameters=()):
        nonlocal attempted
        if not attempted:
            attempted = True
            connection.execute(f"PRAGMA {pragma_name} = 4242")
        return real_execute(connection, sql, parameters)

    monkeypatch.setattr(
        recovery_module,
        "_execute_bound_close_reservation_apply_statement",
        attempt_pragma_write,
    )

    with pytest.raises(
        BoundCloseReservationRecoveryConflict,
        match="transaction",
    ):
        _apply_ready(database, capture)

    assert attempted is True
    with sqlite3.connect(database) as connection:
        assert connection.execute(f"PRAGMA {pragma_name}").fetchone()[0] == 0
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"


def test_apply_authorizer_denies_attach_and_rolls_back(tmp_path, monkeypatch):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    real_execute = recovery_module._execute_bound_close_reservation_apply_statement
    attempted = False

    def attempt_attach(connection, sql, parameters=()):
        nonlocal attempted
        if not attempted:
            attempted = True
            connection.execute("ATTACH DATABASE ':memory:' AS forbidden")
        return real_execute(connection, sql, parameters)

    monkeypatch.setattr(
        recovery_module,
        "_execute_bound_close_reservation_apply_statement",
        attempt_attach,
    )

    with pytest.raises(
        BoundCloseReservationRecoveryConflict,
        match="transaction",
    ):
        _apply_ready(database, capture)

    assert attempted is True
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"


@pytest.mark.parametrize(
    "action",
    [
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_REINDEX,
        sqlite3.SQLITE_ANALYZE,
        sqlite3.SQLITE_CREATE_VTABLE,
    ],
)
def test_recovery_authorizer_default_denies_schema_actions(action):
    assert recovery_module._bound_close_reservation_write_authorizer(
        action,
        "forbidden",
        None,
        "main",
        None,
    ) == sqlite3.SQLITE_DENY


@pytest.mark.parametrize(
    "action",
    [sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT],
)
def test_recovery_authorizer_denies_early_transaction_control(action):
    assert recovery_module._bound_close_reservation_write_authorizer(
        action,
        "COMMIT",
        None,
        None,
        None,
    ) == sqlite3.SQLITE_DENY


def test_apply_denies_early_commit_and_rolls_back_partial_update(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    real_execute = recovery_module._execute_bound_close_reservation_apply_statement
    attempted = False

    def commit_after_first_update(connection, sql, parameters=()):
        nonlocal attempted
        cursor = real_execute(connection, sql, parameters)
        if not attempted and sql.lstrip().startswith("UPDATE"):
            attempted = True
            connection.commit()
            raise sqlite3.OperationalError("injected failure after early commit")
        return cursor

    monkeypatch.setattr(
        recovery_module,
        "_execute_bound_close_reservation_apply_statement",
        commit_after_first_update,
    )

    with pytest.raises(
        BoundCloseReservationRecoveryConflict,
        match="transaction",
    ):
        _apply_ready(database, capture)

    assert attempted is True
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"
    assert not [
        row
        for row in _table_rows(database, "execution_events")
        if row["action"] == "bound_close_reservation_history_converged"
    ]


def test_apply_total_changes_rejects_extra_allowed_column_write(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    real_execute = recovery_module._execute_bound_close_reservation_apply_statement
    injected = False

    def execute_with_extra_allowed_write(connection, sql, parameters=()):
        nonlocal injected
        cursor = real_execute(connection, sql, parameters)
        if not injected and sql.lstrip().startswith("UPDATE"):
            injected = True
            connection.execute(
                "UPDATE bound_position_close_reservations "
                "SET updated_at = updated_at WHERE id = 901"
            )
        return cursor

    monkeypatch.setattr(
        recovery_module,
        "_execute_bound_close_reservation_apply_statement",
        execute_with_extra_allowed_write,
    )

    with pytest.raises(
        BoundCloseReservationRecoveryConflict,
        match="changes",
    ):
        _apply_ready(database, capture)

    assert injected is True
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"
    assert not [
        row
        for row in _table_rows(database, "execution_events")
        if row["action"] == "bound_close_reservation_history_converged"
    ]


def test_apply_postwrite_authorizer_rolls_back_injected_descendant_drift(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    real_fingerprints = recovery_module._durable_invariant_fingerprints
    calls = 0

    def inject_before_postwrite_recheck(connection, *, raw_rows):
        nonlocal calls
        calls += 1
        if calls == 2:
            connection.execute(
                "UPDATE execution_bindings "
                "SET payload_json = '{\"drift\":true}' WHERE id = 901"
            )
        return real_fingerprints(connection, raw_rows=raw_rows)

    monkeypatch.setattr(
        recovery_module,
        "_durable_invariant_fingerprints",
        inject_before_postwrite_recheck,
    )

    with pytest.raises(
        BoundCloseReservationRecoveryConflict,
        match="transaction",
    ):
        _apply_ready(database, capture)

    assert calls == 2
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"
    assert _table_rows(database, "execution_bindings")[0][
        "payload_json"
    ] != '{"drift":true}'
    assert not [
        row
        for row in _table_rows(database, "execution_events")
        if row["action"] == "bound_close_reservation_history_converged"
    ]


def test_apply_authorizer_remains_active_during_postwrite_pragma_attempt(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    real_fingerprints = recovery_module._durable_invariant_fingerprints
    calls = 0

    def attempt_postwrite_pragma(connection, *, raw_rows):
        nonlocal calls
        calls += 1
        if calls == 2:
            connection.execute("PRAGMA user_version = 4242")
        return real_fingerprints(connection, raw_rows=raw_rows)

    monkeypatch.setattr(
        recovery_module,
        "_durable_invariant_fingerprints",
        attempt_postwrite_pragma,
    )

    with pytest.raises(
        BoundCloseReservationRecoveryConflict,
        match="transaction",
    ):
        _apply_ready(database, capture)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"


def test_apply_final_total_changes_rejects_postwrite_noop_update(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    real_fingerprints = recovery_module._durable_invariant_fingerprints
    calls = 0

    def inject_postwrite_noop(connection, *, raw_rows):
        nonlocal calls
        calls += 1
        if calls == 2:
            connection.execute(
                "UPDATE bound_position_close_reservations "
                "SET updated_at = updated_at WHERE id = 901"
            )
        return real_fingerprints(connection, raw_rows=raw_rows)

    monkeypatch.setattr(
        recovery_module,
        "_durable_invariant_fingerprints",
        inject_postwrite_noop,
    )

    with pytest.raises(
        BoundCloseReservationRecoveryConflict,
        match="changes",
    ):
        _apply_ready(database, capture)

    assert calls == 2
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"


def test_commit_returning_after_deadline_reports_verified_late_commit(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    deadline = capture._SealedRecoveryCapture__deadline_monotonic
    clock = {"now": deadline - 0.5}
    real_commit = recovery_module._commit_bound_close_reservation_apply

    def commit_then_cross_deadline(connection):
        real_commit(connection)
        clock["now"] = deadline + 0.5

    monkeypatch.setattr(recovery_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        recovery_module,
        "_commit_bound_close_reservation_apply",
        commit_then_cross_deadline,
    )

    result = _apply_ready(database, capture)

    assert result.status == "applied_after_deadline_verified"
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "confirmed"


def test_commit_crossing_deadline_without_commit_reports_failure_and_no_change(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    deadline = capture._SealedRecoveryCapture__deadline_monotonic
    clock = {"now": deadline - 0.5}

    def cross_deadline_without_commit(connection):
        del connection
        clock["now"] = deadline + 0.5
        raise BoundCloseReservationExchangeDeadlineExceeded("injected deadline")

    monkeypatch.setattr(recovery_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        recovery_module,
        "_commit_bound_close_reservation_apply",
        cross_deadline_without_commit,
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="expired|outcome"):
        _apply_ready(database, capture)

    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"


@pytest.mark.parametrize("exception_type", [OSError, RuntimeError])
def test_guard_teardown_failure_after_commit_uses_read_only_exact_verification(
    tmp_path, monkeypatch, exception_type
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)

    @contextmanager
    def teardown_failure(*, deadline_monotonic):
        del deadline_monotonic
        yield
        raise exception_type("injected timer teardown failure")

    monkeypatch.setattr(
        recovery_module,
        "_recovery_wall_clock_guard",
        teardown_failure,
    )

    result = _apply_ready(database, capture)

    assert result.status == "applied"
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "confirmed"
    assert len(
        [
            row
            for row in _table_rows(database, "execution_events")
            if row["action"] == "bound_close_reservation_history_converged"
        ]
    ) == 1


def test_system_interrupt_after_commit_is_verified_and_annotated(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    real_verify = (
        recovery_module._verify_committed_bound_close_reservation_apply_read_only
    )
    verify_calls = 0

    def record_verify(*args, **kwargs):
        nonlocal verify_calls
        verify_calls += 1
        return real_verify(*args, **kwargs)

    @contextmanager
    def interrupt_on_teardown(*, deadline_monotonic):
        del deadline_monotonic
        yield
        raise KeyboardInterrupt("injected timer teardown interrupt")

    monkeypatch.setattr(
        recovery_module,
        "_verify_committed_bound_close_reservation_apply_read_only",
        record_verify,
    )
    monkeypatch.setattr(
        recovery_module,
        "_recovery_wall_clock_guard",
        interrupt_on_teardown,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        _apply_ready(database, capture)

    assert verify_calls == 1
    assert any(
        "verified" in note
        for note in getattr(captured.value, "__notes__", ())
    )
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "confirmed"


def test_commit_then_system_interrupt_is_verified_annotated_and_reraised(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    real_commit = recovery_module._commit_bound_close_reservation_apply

    def commit_then_interrupt(connection):
        real_commit(connection)
        raise KeyboardInterrupt("injected interrupt after commit")

    monkeypatch.setattr(
        recovery_module,
        "_commit_bound_close_reservation_apply",
        commit_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        _apply_ready(database, capture)

    assert any(
        "verified" in note
        for note in getattr(captured.value, "__notes__", ())
    )
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "confirmed"


def test_system_interrupt_before_commit_is_annotated_unresolved_and_reraised(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)

    def interrupt_before_commit(connection):
        del connection
        raise KeyboardInterrupt("injected interrupt before commit")

    monkeypatch.setattr(
        recovery_module,
        "_commit_bound_close_reservation_apply",
        interrupt_before_commit,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        _apply_ready(database, capture)

    assert any(
        "could not be verified" in note
        for note in getattr(captured.value, "__notes__", ())
    )
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "submitted"


def test_close_failure_after_committed_apply_does_not_replace_success(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    real_open = recovery_module._open_bound_close_reservation_writable_connection

    class CloseFailureConnection:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def close(self):
            self._connection.close()
            raise sqlite3.OperationalError("injected close failure")

    monkeypatch.setattr(
        recovery_module,
        "_open_bound_close_reservation_writable_connection",
        lambda path: CloseFailureConnection(real_open(path)),
    )

    result = _apply_ready(database, capture)

    assert result.status == "applied"
    assert _table_rows(database, "bound_position_close_reservations")[0][
        "status"
    ] == "confirmed"


def test_commit_exception_after_real_commit_is_resolved_by_read_only_verification(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    real_commit = recovery_module._commit_bound_close_reservation_apply

    def commit_then_raise(connection):
        real_commit(connection)
        raise sqlite3.OperationalError("injected ambiguous commit acknowledgement")

    monkeypatch.setattr(
        recovery_module,
        "_commit_bound_close_reservation_apply",
        commit_then_raise,
    )

    result = _apply_ready(database, capture)

    assert result.status == "applied"
    assert len(
        [
            row
            for row in _table_rows(database, "execution_events")
            if row["action"] == "bound_close_reservation_history_converged"
        ]
    ) == 1


def test_commit_exception_before_commit_rolls_back_and_reports_unresolved_outcome(
    tmp_path, monkeypatch
):
    database = _apply_database(tmp_path)
    capture, _http = _ready_apply_capture(monkeypatch, database)
    before_reservations = _table_rows(database, "bound_position_close_reservations")
    before_events = _table_rows(database, "execution_events")
    monkeypatch.setattr(
        recovery_module,
        "_commit_bound_close_reservation_apply",
        lambda connection: (_ for _ in ()).throw(
            sqlite3.OperationalError("injected failed commit")
        ),
    )

    with pytest.raises(BoundCloseReservationRecoveryConflict, match="commit_outcome"):
        _apply_ready(database, capture)

    assert _table_rows(database, "bound_position_close_reservations") == (
        before_reservations
    )
    assert _table_rows(database, "execution_events") == before_events


@pytest.mark.parametrize(
    "status",
    ["applied", "applied_after_deadline_verified", "already_applied"],
)
def test_apply_result_serialization_is_exact_bounded_canonical_json(status):
    from telegram_kol_research.bound_close_reservation_recovery import (
        MAX_RECOVERY_PLAN_BYTES,
        BoundCloseReservationRecoveryResult,
        serialize_bound_close_reservation_recovery_result,
    )

    result = BoundCloseReservationRecoveryResult(
        status=status,
        evidence_fingerprint="a" * 64,
        action_count=2,
        audit_event_id=17,
    )

    serialized = serialize_bound_close_reservation_recovery_result(result)

    assert serialized == (
        '{"action_count":2,"audit_event_id":17,'
        '"evidence_fingerprint":"' + "a" * 64 + '","mode":"apply",'
        '"schema_version":1,"status":"' + status + '"}'
    )
    assert len(serialized.encode("utf-8")) <= MAX_RECOVERY_PLAN_BYTES
