"""Closed contract for bound-position close-reservation recovery."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
import hashlib
import json
import math
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

from telegram_kol_research.deepcoin_client import (
    DEEPCOIN_BOUND_CLOSE_RESERVATION_RECOVERY_PHASE,
    DeepcoinReadUnavailable,
    DeepcoinRequestScope,
    DeepcoinRestClient,
    _build_deepcoin_bound_close_reservation_recovery_client_from_env,
    _claim_bound_close_reservation_recovery_transport,
)
from telegram_kol_research.deepcoin_request_policy import RequestPriority
from telegram_kol_research.deepcoin_snapshot_authority import (
    ExchangeCollectionEvidence,
    build_exchange_collection_evidence,
)


RECOVERY_SCHEMA_VERSION = 1
MAX_RESERVATION_OBSERVATIONS = 64
MAX_RECOVERY_PLAN_BYTES = 65_536
_MAX_RECOVERY_PLAN_DEPTH = 4
_MAX_RECOVERY_PLAN_ITEMS = 1_024
_MAX_RECOVERY_PLAN_STRING_BYTES = 256

PROVEN_TERMINAL_REASONS = frozenset(
    {
        "exact_close_and_position_terminal",
    }
)
ACTIVE_REASONS = frozenset(
    {
        "exact_position_currently_live",
        "exact_close_order_currently_pending",
        "exact_close_order_nonterminal",
    }
)
UNKNOWN_REASONS = frozenset(
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

_LOWER_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")

ACTIVE_CLOSE_RESERVATION_STATUSES = frozenset(
    {
        "reserved",
        "submitted",
        "submit_unknown",
        "unknown_exchange_outcome",
        "recovery_required",
    }
)

# Closed from the actual persistence writers/reconcilers. A new status must be
# reviewed here before recovery can treat its local identity as usable.
_EXECUTION_BINDING_STATUSES = frozenset(
    {"open", "active", "stale", "unknown", "closed"}
)
_BOUND_CLOSE_EVENT_STATUSES = frozenset({"submitted"})
_ENTRY_LEG_STATUSES = frozenset(
    {
        "submitting",
        "submitted",
        "open",
        "active",
        "pending",
        "unknown",
        "filled",
        "partially_filled",
        "cancelled",
        "manually_cancelled",
        "exchange_cancelled",
        "manually_closed",
        "closed",
        "expired",
        "invalidated",
    }
)
_ENTRY_LEG_ATTRIBUTION_STATUSES = frozenset(
    {
        "unassigned",
        "verified",
        "evidence_unavailable",
        "attribution_conflict",
        "protection_adoption_refused",
    }
)
_POSITION_MUTATION_STATUSES = frozenset(
    {
        "reserved",
        "not_sent",
        "submitting",
        "submitted",
        "confirmed",
        "rejected",
        "recovery_required",
        "blocked",
    }
)

_MAX_LOCAL_DESCENDANTS = 256
_MAX_LOCAL_JSON_BYTES = 1_048_576
RECOVERY_RESPONSE_MAX_BYTES = 1_048_576


class BoundCloseReservationExchangeConfigurationError(RuntimeError):
    """The recovery reader cannot establish its closed safety boundary."""


class BoundCloseReservationExchangeDeadlineExceeded(RuntimeError):
    """The one absolute recovery capture deadline expired."""


class _OpaqueUncopyableCapability:
    """Prevent accidental capability duplication or raw-state serialization."""

    __slots__ = ()

    @staticmethod
    def _copy_error() -> TypeError:
        return TypeError("opaque recovery capability cannot be copied or serialized")

    def __reduce__(self):
        raise self._copy_error()

    def __reduce_ex__(self, protocol):
        del protocol
        raise self._copy_error()

    def __copy__(self):
        raise self._copy_error()

    def __deepcopy__(self, memo):
        del memo
        raise self._copy_error()


def _require_recovery_wall_clock_guard() -> None:
    required = ("SIGALRM", "ITIMER_REAL", "getitimer", "setitimer")
    if any(not callable(getattr(signal, name, None)) for name in required[2:]) or any(
        not hasattr(signal, name) for name in required[:2]
    ):
        raise BoundCloseReservationExchangeConfigurationError(
            "strict POSIX wall-clock timer is unavailable"
        )
    if threading.current_thread() is not threading.main_thread():
        raise BoundCloseReservationExchangeConfigurationError(
            "strict wall-clock timer requires the main thread"
        )
    try:
        handler = signal.getsignal(signal.SIGALRM)
        active_timer = signal.getitimer(signal.ITIMER_REAL)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise BoundCloseReservationExchangeConfigurationError(
            "strict wall-clock timer state is unavailable"
        ) from exc
    if handler is not signal.SIG_DFL or any(value > 0 for value in active_timer):
        raise BoundCloseReservationExchangeConfigurationError(
            "strict wall-clock timer conflict"
        )


def _finite_recovery_monotonic(value: object) -> float:
    if isinstance(value, bool):
        raise BoundCloseReservationExchangeConfigurationError(
            "recovery deadline is invalid"
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BoundCloseReservationExchangeConfigurationError(
            "recovery deadline is invalid"
        ) from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise BoundCloseReservationExchangeConfigurationError(
            "recovery deadline is invalid"
        )
    return parsed


@contextmanager
def _recovery_wall_clock_guard(*, deadline_monotonic: float):
    _require_recovery_wall_clock_guard()
    deadline = _finite_recovery_monotonic(deadline_monotonic)
    remaining = deadline - _finite_recovery_monotonic(time.monotonic())
    if remaining <= 0:
        raise BoundCloseReservationExchangeDeadlineExceeded(
            "recovery wall-clock deadline expired"
        )

    previous_handler = signal.getsignal(signal.SIGALRM)
    handler_installed = False

    def deadline_handler(signum, frame):
        del signum, frame
        raise BoundCloseReservationExchangeDeadlineExceeded(
            "recovery wall-clock deadline expired"
        )

    try:
        signal.signal(signal.SIGALRM, deadline_handler)
        handler_installed = True
        signal.setitimer(signal.ITIMER_REAL, remaining)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if handler_installed:
            signal.signal(signal.SIGALRM, previous_handler)
        raise BoundCloseReservationExchangeConfigurationError(
            "strict wall-clock timer cannot be armed"
        ) from exc
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)


class BoundCloseReservationExchangeReader:
    """One-shot capability exposing only exact Deepcoin recovery GET reads."""

    __slots__ = ("__state", "__transport")

    def __init__(self, transport: DeepcoinRestClient) -> None:
        if not _claim_bound_close_reservation_recovery_transport(transport):
            raise BoundCloseReservationExchangeConfigurationError(
                "transport must come from the dedicated recovery factory"
            )
        self.__transport = transport
        self.__state = "fresh"

    @contextmanager
    def request_scope(
        self,
        *,
        deadline_monotonic: float,
        correlation_id: str | None = None,
        attempt_recorder: Callable[[Any], None] | None = None,
    ):
        """Apply one absolute deadline and always retire the owned transport."""

        if self.__state == "active":
            raise BoundCloseReservationExchangeConfigurationError(
                "recovery scope is already active"
            )
        if self.__state == "consumed":
            raise BoundCloseReservationExchangeConfigurationError(
                "recovery scope is already consumed"
            )

        scope = DeepcoinRequestScope(
            phase=DEEPCOIN_BOUND_CLOSE_RESERVATION_RECOVERY_PHASE,
            priority=RequestPriority.BACKGROUND,
            deadline_monotonic=deadline_monotonic,
            correlation_id=correlation_id,
            attempt_recorder=attempt_recorder,
            max_response_bytes=RECOVERY_RESPONSE_MAX_BYTES,
        )
        with _recovery_wall_clock_guard(
            deadline_monotonic=deadline_monotonic
        ):
            try:
                self.__state = "active"
                try:
                    with self.__transport.request_scope(scope):
                        yield self
                except DeepcoinReadUnavailable as exc:
                    if (
                        exc.fact.safe_code == "request_deadline_exceeded"
                        and time.monotonic()
                        >= _finite_recovery_monotonic(deadline_monotonic)
                    ):
                        raise BoundCloseReservationExchangeDeadlineExceeded(
                            "recovery wall-clock deadline expired"
                        ) from exc
                    raise
            finally:
                self.__state = "consumed"
                self.__transport.close()

    def _active_transport(self) -> DeepcoinRestClient:
        if self.__state != "active":
            raise BoundCloseReservationExchangeConfigurationError(
                "exchange reads require the active recovery scope"
            )
        return self.__transport

    def read_positions(self) -> dict[str, Any]:
        return self._active_transport().read_positions()

    def read_open_orders(self) -> dict[str, Any]:
        return self._active_transport().read_open_orders()

    def read_order_history(
        self,
        *,
        inst_id: str,
        order_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._active_transport().read_order_history(
            inst_id=inst_id,
            order_id=order_id,
            limit=limit,
        )

    def read_trade_fills(
        self,
        *,
        inst_id: str,
        order_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._active_transport().read_trade_fills(
            inst_id=inst_id,
            order_id=order_id,
            limit=limit,
        )

    def read_position_history(
        self,
        *,
        inst_id: str,
        pos_id: str,
    ) -> dict[str, Any]:
        return self._active_transport().read_position_history(
            inst_id=inst_id,
            pos_id=pos_id,
        )


def build_bound_close_reservation_exchange_reader_from_env(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | Path] | None = None,
) -> BoundCloseReservationExchangeReader:
    """Build only the closed recovery capability, never the raw transport."""

    transport = _build_deepcoin_bound_close_reservation_recovery_client_from_env(
        environ=environ,
        env_file_paths=env_file_paths,
    )
    return BoundCloseReservationExchangeReader(transport)


@dataclass(frozen=True, slots=True)
class LocalCloseMutationEvidence:
    mutation_ref: str
    status: str
    order_ref: str | None
    reserved_at: datetime
    submitted_at: datetime | None
    confirmed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    record_fingerprint: str


@dataclass(frozen=True, slots=True)
class LocalOwnedEntryLegEvidence:
    leg_ref: str
    status: str
    attribution_status: str
    order_ref: str | None
    last_verified_at: datetime | None
    created_at: datetime
    updated_at: datetime
    record_fingerprint: str


@dataclass(frozen=True, slots=True)
class LocalReservationEvidence:
    reservation_ref: str
    source_status: str
    local_reason_code: str | None
    reservation_created_at: datetime | None
    reservation_updated_at: datetime | None
    reservation_record_fingerprint: str
    binding_ref: str | None
    binding_status: str | None
    instrument_ref: str | None
    side: str | None
    binding_record_fingerprint: str | None
    position_ref: str | None
    close_event_ref: str | None
    close_order_ref: str | None
    event_created_at: datetime | None
    close_event_record_fingerprint: str | None
    entry_leg: LocalOwnedEntryLegEvidence | None
    close_mutations: tuple[LocalCloseMutationEvidence, ...]


class ExchangeCloseOrderState(StrEnum):
    """Closed normalized exchange order states accepted by recovery."""

    FILLED = "filled"
    OPEN = "open"
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ExchangePositionHistoryState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class ExchangePositionEvidence:
    instrument_ref: str
    side: str
    position_ref: str
    quantity: Decimal

    def __post_init__(self) -> None:
        _validate_exchange_identity(
            instrument_ref=self.instrument_ref,
            side=self.side,
            position_ref=self.position_ref,
        )
        _require_decimal(self.quantity, "quantity", positive=True)


@dataclass(frozen=True, slots=True)
class ExchangeOrderEvidence:
    instrument_ref: str
    side: str
    position_ref: str
    order_ref: str
    state: ExchangeCloseOrderState
    requested_quantity: Decimal
    filled_quantity: Decimal
    terminal_at: datetime | None

    def __post_init__(self) -> None:
        _validate_exchange_identity(
            instrument_ref=self.instrument_ref,
            side=self.side,
            position_ref=self.position_ref,
            order_ref=self.order_ref,
        )
        if type(self.state) is not ExchangeCloseOrderState:
            raise TypeError("state must be ExchangeCloseOrderState")
        _require_decimal(
            self.requested_quantity,
            "requested_quantity",
            positive=False,
        )
        _require_decimal(
            self.filled_quantity,
            "filled_quantity",
            positive=False,
        )
        _require_optional_aware_utc(self.terminal_at, "terminal_at")


@dataclass(frozen=True, slots=True)
class ExchangeFillEvidence:
    instrument_ref: str
    side: str
    position_ref: str
    order_ref: str
    quantity: Decimal
    filled_at: datetime

    def __post_init__(self) -> None:
        _validate_exchange_identity(
            instrument_ref=self.instrument_ref,
            side=self.side,
            position_ref=self.position_ref,
            order_ref=self.order_ref,
        )
        _require_decimal(self.quantity, "quantity", positive=False)
        _require_aware_utc(self.filled_at, "filled_at")


@dataclass(frozen=True, slots=True)
class ExchangePositionHistoryEvidence:
    instrument_ref: str
    side: str
    position_ref: str
    state: ExchangePositionHistoryState
    closed_quantity: Decimal
    closed_at: datetime | None

    def __post_init__(self) -> None:
        _validate_exchange_identity(
            instrument_ref=self.instrument_ref,
            side=self.side,
            position_ref=self.position_ref,
        )
        if type(self.state) is not ExchangePositionHistoryState:
            raise TypeError("state must be ExchangePositionHistoryState")
        _require_decimal(
            self.closed_quantity,
            "closed_quantity",
            positive=False,
        )
        _require_optional_aware_utc(self.closed_at, "closed_at")


_EXCHANGE_CAPTURE_FAILURE_REASONS = frozenset(
    {
        "exchange_evidence_unavailable",
        "exchange_schema_invalid",
        "exchange_capture_timeout",
        "exchange_response_size_exceeded",
    }
)
_MAX_EXCHANGE_EVIDENCE_ITEMS = 100
_MAX_EXCHANGE_DECIMAL_DIGITS = 64
_MAX_EXCHANGE_DECIMAL_ABS_EXPONENT = 32
_MAX_EXCHANGE_DECIMAL_TEXT_BYTES = 128


@dataclass(frozen=True, slots=True)
class ExchangeReservationEvidence:
    """Strict normalized evidence for one exact reservation identity chain."""

    capture_reason_code: str | None
    schema_valid: bool
    current_positions_complete: bool
    pending_orders_complete: bool
    order_history_complete: bool
    fills_complete: bool
    position_history_complete: bool
    order_history_at_limit: bool
    fills_at_limit: bool
    position_history_at_limit: bool
    current_positions: tuple[ExchangePositionEvidence, ...]
    pending_orders: tuple[ExchangeOrderEvidence, ...]
    order_history: tuple[ExchangeOrderEvidence, ...]
    fills: tuple[ExchangeFillEvidence, ...]
    position_history: tuple[ExchangePositionHistoryEvidence, ...]

    def __post_init__(self) -> None:
        if self.capture_reason_code is not None:
            if type(self.capture_reason_code) is not str:
                raise TypeError("capture_reason_code must be a string or None")
            if self.capture_reason_code not in _EXCHANGE_CAPTURE_FAILURE_REASONS:
                raise ValueError("capture_reason_code is not a closed recovery reason")
        for field_name in (
            "schema_valid",
            "current_positions_complete",
            "pending_orders_complete",
            "order_history_complete",
            "fills_complete",
            "position_history_complete",
            "order_history_at_limit",
            "fills_at_limit",
            "position_history_at_limit",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be a boolean")
        _require_exchange_collection(
            self.current_positions,
            "current_positions",
            ExchangePositionEvidence,
        )
        _require_exchange_collection(
            self.pending_orders,
            "pending_orders",
            ExchangeOrderEvidence,
        )
        _require_exchange_collection(
            self.order_history,
            "order_history",
            ExchangeOrderEvidence,
        )
        _require_exchange_collection(
            self.fills,
            "fills",
            ExchangeFillEvidence,
        )
        _require_exchange_collection(
            self.position_history,
            "position_history",
            ExchangePositionHistoryEvidence,
        )


class _RawReservationCapability(_OpaqueUncopyableCapability):
    """Raw identities retained only inside this process for later exact reads/CAS."""

    __slots__ = (
        "binding_id",
        "entry_leg_id",
        "event_id",
        "instrument_id",
        "mutation_ids",
        "order_id",
        "position_id",
        "reservation_id",
        "source_status",
    )

    def __init__(
        self,
        *,
        reservation_id: int,
        source_status: str,
        binding_id: int,
        event_id: int,
        position_id: str,
        order_id: str,
        instrument_id: str,
        entry_leg_id: int | None,
        mutation_ids: tuple[int, ...],
    ) -> None:
        self.reservation_id = reservation_id
        self.source_status = source_status
        self.binding_id = binding_id
        self.event_id = event_id
        self.position_id = position_id
        self.order_id = order_id
        self.instrument_id = instrument_id
        self.entry_leg_id = entry_leg_id
        self.mutation_ids = mutation_ids


class _BoundCloseReservationSourceCapability(_OpaqueUncopyableCapability):
    __slots__ = ("__raw_by_reservation_ref",)

    def __init__(self, raw_by_reservation_ref: Mapping[str, _RawReservationCapability]):
        self.__raw_by_reservation_ref = dict(raw_by_reservation_ref)

    def _get(self, reservation_ref: str) -> _RawReservationCapability:
        return self.__raw_by_reservation_ref[reservation_ref]


@dataclass(frozen=True, slots=True)
class BoundCloseReservationSource(_OpaqueUncopyableCapability):
    reservations: tuple[LocalReservationEvidence, ...]
    source_fingerprint: str
    _capability: object

    def __post_init__(self) -> None:
        if type(self.reservations) is not tuple:
            raise TypeError("reservations must be a tuple")
        if len(self.reservations) > MAX_RESERVATION_OBSERVATIONS:
            raise ValueError("reservations cannot contain more than 64 items")
        if any(type(item) is not LocalReservationEvidence for item in self.reservations):
            raise TypeError("reservations must contain LocalReservationEvidence")
        _require_lower_hex_64(self.source_fingerprint, "source_fingerprint")
        if type(self._capability) is not _BoundCloseReservationSourceCapability:
            raise TypeError("invalid private source capability")


class ReservationClassification(StrEnum):
    PROVEN_TERMINAL = "PROVEN_TERMINAL"
    ACTIVE = "ACTIVE"
    UNKNOWN = "UNKNOWN"


_REASONS_BY_CLASSIFICATION = {
    ReservationClassification.PROVEN_TERMINAL: PROVEN_TERMINAL_REASONS,
    ReservationClassification.ACTIVE: ACTIVE_REASONS,
    ReservationClassification.UNKNOWN: UNKNOWN_REASONS,
}


@dataclass(frozen=True, slots=True)
class BoundCloseReservationObservation:
    reservation_ref: str
    classification: ReservationClassification
    reason_code: str
    source_fingerprint: str
    exchange_fingerprint: str

    def __post_init__(self) -> None:
        _require_lower_hex_64(self.reservation_ref, "reservation_ref")
        if type(self.classification) is not ReservationClassification:
            raise TypeError("classification must be ReservationClassification")
        if type(self.reason_code) is not str:
            raise TypeError("reason_code must be a string")
        if self.reason_code not in _REASONS_BY_CLASSIFICATION[self.classification]:
            raise ValueError("reason_code is not allowed for classification")
        _require_lower_hex_64(self.source_fingerprint, "source_fingerprint")
        _require_lower_hex_64(
            self.exchange_fingerprint,
            "exchange_fingerprint",
        )


@dataclass(frozen=True, slots=True)
class BoundCloseReservationRecoveryPlan:
    schema_version: int
    status: str
    observations: tuple[BoundCloseReservationObservation, ...]
    source_fingerprint: str
    exchange_snapshot_fingerprint: str
    evidence_fingerprint: str
    confirmation_token: str
    action_count: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be an integer")
        if self.schema_version != RECOVERY_SCHEMA_VERSION:
            raise ValueError("schema_version is unsupported")
        if type(self.status) is not str:
            raise TypeError("status must be a string")
        if type(self.observations) is not tuple:
            raise TypeError("observations must be a tuple")
        if len(self.observations) > MAX_RESERVATION_OBSERVATIONS:
            raise ValueError("observations cannot contain more than 64 items")
        if any(
            type(observation) is not BoundCloseReservationObservation
            for observation in self.observations
        ):
            raise TypeError(
                "observations must contain BoundCloseReservationObservation"
            )
        _require_lower_hex_64(self.source_fingerprint, "source_fingerprint")
        _require_lower_hex_64(
            self.exchange_snapshot_fingerprint,
            "exchange_snapshot_fingerprint",
        )
        _require_lower_hex_64(self.evidence_fingerprint, "evidence_fingerprint")
        if type(self.confirmation_token) is not str:
            raise TypeError("confirmation_token must be a string")
        if not self.confirmation_token:
            raise ValueError("confirmation_token cannot be empty")
        if type(self.action_count) is not int:
            raise TypeError("action_count must be an integer")
        if self.action_count < 0:
            raise ValueError("action_count cannot be negative")

        ready_population = bool(self.observations) and all(
            observation.classification
            is ReservationClassification.PROVEN_TERMINAL
            for observation in self.observations
        )
        expected_status = "ready" if ready_population else "refused"
        if self.status != expected_status:
            raise ValueError(
                f"status must be {expected_status!r} for this population"
            )
        expected_action_count = len(self.observations) if ready_population else 0
        if self.action_count != expected_action_count:
            raise ValueError(
                "action_count must equal the terminal population for ready "
                "plans and zero for refused plans"
            )


_RECOVERY_PLAN_KEYS = frozenset(
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
_RECOVERY_COUNT_KEYS = frozenset(
    {"active", "proven_terminal", "total", "unknown"}
)
_RECOVERY_OBSERVATION_KEYS = frozenset(
    {
        "classification",
        "exchange_fingerprint",
        "reason_code",
        "reservation_ref",
        "source_fingerprint",
    }
)
_RECOVERY_CONFIRMATION_PREFIX = "BOUND-CLOSE-"
_SEALED_APPLY_CLAIM_ISSUER = object()


def _observation_payload(
    observation: BoundCloseReservationObservation,
) -> dict[str, object]:
    return {
        "classification": observation.classification.value,
        "exchange_fingerprint": observation.exchange_fingerprint,
        "reason_code": observation.reason_code,
        "reservation_ref": observation.reservation_ref,
        "source_fingerprint": observation.source_fingerprint,
    }


def _recovery_counts(
    observations: tuple[BoundCloseReservationObservation, ...],
) -> dict[str, int]:
    return {
        "active": sum(
            item.classification is ReservationClassification.ACTIVE
            for item in observations
        ),
        "proven_terminal": sum(
            item.classification is ReservationClassification.PROVEN_TERMINAL
            for item in observations
        ),
        "total": len(observations),
        "unknown": sum(
            item.classification is ReservationClassification.UNKNOWN
            for item in observations
        ),
    }


def _exchange_snapshot_fingerprint(
    observations: tuple[BoundCloseReservationObservation, ...],
) -> str:
    return _sha256_json(
        {
            "exchange_observations": [
                {
                    "exchange_fingerprint": item.exchange_fingerprint,
                    "reservation_ref": item.reservation_ref,
                }
                for item in observations
            ]
        }
    )


def _recovery_semantic_payload(
    *,
    status: str,
    observations: tuple[BoundCloseReservationObservation, ...],
    source_fingerprint: str,
    exchange_snapshot_fingerprint: str,
    action_count: int,
) -> dict[str, object]:
    return {
        "action_count": action_count,
        "counts": _recovery_counts(observations),
        "database_writes": 0,
        "exchange_snapshot_fingerprint": exchange_snapshot_fingerprint,
        "exchange_writes": 0,
        "history_replays": 0,
        "mode": "dry_run",
        "observations": [_observation_payload(item) for item in observations],
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "source_fingerprint": source_fingerprint,
        "status": status,
    }


def _derive_recovery_confirmation_token(evidence_fingerprint: str) -> str:
    _require_lower_hex_64(evidence_fingerprint, "evidence_fingerprint")
    digest = hashlib.sha256(
        (
            "bound-close-reservation-recovery-confirmation-v1:"
            f"{evidence_fingerprint}"
        ).encode("ascii")
    ).hexdigest()
    return f"{_RECOVERY_CONFIRMATION_PREFIX}{digest[:16]}"


def build_bound_close_reservation_recovery_plan(
    *,
    source_fingerprint: str,
    observations: tuple[BoundCloseReservationObservation, ...],
) -> BoundCloseReservationRecoveryPlan:
    """Build the only canonical semantic plan accepted by serialization."""

    _require_lower_hex_64(source_fingerprint, "source_fingerprint")
    if type(observations) is not tuple:
        raise TypeError("observations must be a tuple")
    if len(observations) > MAX_RESERVATION_OBSERVATIONS:
        raise ValueError("observations cannot contain more than 64 items")
    if any(type(item) is not BoundCloseReservationObservation for item in observations):
        raise TypeError("observations must contain BoundCloseReservationObservation")
    ordered = tuple(sorted(observations, key=lambda item: item.reservation_ref))
    refs = tuple(item.reservation_ref for item in ordered)
    if len(set(refs)) != len(refs):
        raise ValueError("observation reservation references must be unique")
    ready = bool(ordered) and all(
        item.classification is ReservationClassification.PROVEN_TERMINAL
        for item in ordered
    )
    status = "ready" if ready else "refused"
    action_count = len(ordered) if ready else 0
    exchange_fingerprint = _exchange_snapshot_fingerprint(ordered)
    evidence_fingerprint = _sha256_json(
        _recovery_semantic_payload(
            status=status,
            observations=ordered,
            source_fingerprint=source_fingerprint,
            exchange_snapshot_fingerprint=exchange_fingerprint,
            action_count=action_count,
        )
    )
    return BoundCloseReservationRecoveryPlan(
        schema_version=RECOVERY_SCHEMA_VERSION,
        status=status,
        observations=ordered,
        source_fingerprint=source_fingerprint,
        exchange_snapshot_fingerprint=exchange_fingerprint,
        evidence_fingerprint=evidence_fingerprint,
        confirmation_token=_derive_recovery_confirmation_token(
            evidence_fingerprint
        ),
        action_count=action_count,
    )


def _require_canonical_recovery_plan(
    plan: BoundCloseReservationRecoveryPlan,
) -> BoundCloseReservationRecoveryPlan:
    if type(plan) is not BoundCloseReservationRecoveryPlan:
        raise TypeError("plan must be BoundCloseReservationRecoveryPlan")
    expected = build_bound_close_reservation_recovery_plan(
        source_fingerprint=plan.source_fingerprint,
        observations=plan.observations,
    )
    for field_name in (
        "schema_version",
        "status",
        "observations",
        "exchange_snapshot_fingerprint",
        "evidence_fingerprint",
        "confirmation_token",
        "action_count",
    ):
        if getattr(plan, field_name) != getattr(expected, field_name):
            raise ValueError(f"{field_name} does not match the canonical plan")
    return plan


def _capture_identity(
    *,
    capture_started_at: datetime,
    capture_completed_at: datetime,
    evidence_fingerprint: str,
) -> str:
    return _sha256_json(
        {
            "capture_completed_at": capture_completed_at,
            "capture_started_at": capture_started_at,
            "evidence_fingerprint": evidence_fingerprint,
        }
    )


def _validated_capture_times(
    capture_started_at: datetime,
    capture_completed_at: datetime,
) -> tuple[datetime, datetime]:
    try:
        started_at = _require_aware_utc(
            capture_started_at,
            "capture_started_at",
        )
        completed_at = _require_aware_utc(
            capture_completed_at,
            "capture_completed_at",
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("capture timestamps must be aware UTC") from exc
    if completed_at < started_at:
        raise ValueError("capture_completed_at cannot precede capture_started_at")
    return started_at, completed_at


def _recovery_plan_document(
    plan: BoundCloseReservationRecoveryPlan,
    *,
    capture_started_at: datetime,
    capture_completed_at: datetime,
) -> dict[str, object]:
    _require_canonical_recovery_plan(plan)
    started_at, completed_at = _validated_capture_times(
        capture_started_at,
        capture_completed_at,
    )
    semantic = _recovery_semantic_payload(
        status=plan.status,
        observations=plan.observations,
        source_fingerprint=plan.source_fingerprint,
        exchange_snapshot_fingerprint=plan.exchange_snapshot_fingerprint,
        action_count=plan.action_count,
    )
    return {
        **semantic,
        "capture_completed_at": _canonical_json_value(completed_at),
        "capture_identity": _capture_identity(
            capture_started_at=started_at,
            capture_completed_at=completed_at,
            evidence_fingerprint=plan.evidence_fingerprint,
        ),
        "capture_started_at": _canonical_json_value(started_at),
        "confirmation_token": plan.confirmation_token,
        "evidence_fingerprint": plan.evidence_fingerprint,
    }


def serialize_bound_close_reservation_recovery_plan(
    plan: BoundCloseReservationRecoveryPlan,
    *,
    capture_started_at: datetime,
    capture_completed_at: datetime,
) -> str:
    """Serialize only the bounded, redacted dry-run document."""

    document = _canonical_json(
        _recovery_plan_document(
            plan,
            capture_started_at=capture_started_at,
            capture_completed_at=capture_completed_at,
        )
    )
    if len(document.encode("utf-8")) > MAX_RECOVERY_PLAN_BYTES:
        raise ValueError("serialized recovery plan exceeds its byte bound")
    return document


class SealedRecoveryCapture(_OpaqueUncopyableCapability):
    """Opaque in-process capability reserved for the separately gated apply."""

    __slots__ = (
        "__apply_claimed",
        "__apply_claim_lock",
        "__plan",
        "__serialized_plan",
        "__source_capability",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("SealedRecoveryCapture cannot be constructed directly")

    @property
    def plan(self) -> BoundCloseReservationRecoveryPlan:
        return self.__plan

    @property
    def serialized_plan(self) -> str:
        return self.__serialized_plan

    def __repr__(self) -> str:
        return "<SealedRecoveryCapture opaque>"


class _ClaimedSealedRecoveryCapture(_OpaqueUncopyableCapability):
    __slots__ = ("__plan", "__source_capability")

    def __init__(
        self,
        *,
        plan: BoundCloseReservationRecoveryPlan,
        source_capability: object,
        _issuer: object,
    ) -> None:
        if _issuer is not _SEALED_APPLY_CLAIM_ISSUER:
            raise TypeError("claimed recovery capture has no private issuer")
        if type(source_capability) is not _BoundCloseReservationSourceCapability:
            raise TypeError("claimed recovery capture source capability is invalid")
        self.__plan = plan
        self.__source_capability = source_capability

    @property
    def plan(self) -> BoundCloseReservationRecoveryPlan:
        return self.__plan


def _claim_sealed_recovery_capture_for_apply(
    capture: SealedRecoveryCapture,
) -> _ClaimedSealedRecoveryCapture:
    """Consume the private apply token exactly once; never trust saved bytes."""

    if type(capture) is not SealedRecoveryCapture:
        raise TypeError("capture must be a privately issued SealedRecoveryCapture")
    with capture._SealedRecoveryCapture__apply_claim_lock:
        if capture._SealedRecoveryCapture__apply_claimed:
            raise ValueError("sealed recovery capture was already claimed")
        if capture.plan.status != "ready":
            raise ValueError("only a ready sealed recovery capture can be claimed")
        capture._SealedRecoveryCapture__apply_claimed = True
        return _ClaimedSealedRecoveryCapture(
            plan=capture.plan,
            source_capability=(
                capture._SealedRecoveryCapture__source_capability
            ),
            _issuer=_SEALED_APPLY_CLAIM_ISSUER,
        )


class _RawRecoveryCapture:
    __slots__ = (
        "capture_error",
        "fills_by_order",
        "open_orders",
        "order_history_by_order",
        "position_history_by_position",
        "positions",
    )

    def __init__(self) -> None:
        self.capture_error: BaseException | None = None
        self.positions: object = None
        self.open_orders: object = None
        self.order_history_by_order: dict[tuple[str, str], object] = {}
        self.fills_by_order: dict[tuple[str, str], object] = {}
        self.position_history_by_position: dict[tuple[str, str], object] = {}


def _recovery_capture_now() -> datetime:
    """Internal clock; the public capture API never accepts caller timestamps."""

    return datetime.now(UTC)


def capture_and_seal_bound_close_reservation_recovery(
    source: BoundCloseReservationSource,
    reader: BoundCloseReservationExchangeReader,
    *,
    deadline_monotonic: float,
) -> SealedRecoveryCapture:
    """Perform one fresh bounded GET capture and issue the only apply capability."""

    if type(source) is not BoundCloseReservationSource:
        raise TypeError("source must be BoundCloseReservationSource")
    if type(reader) is not BoundCloseReservationExchangeReader:
        raise TypeError("reader must be the dedicated recovery reader")
    started_at = _require_aware_utc(
        _recovery_capture_now(),
        "capture_started_at",
    )
    raw_by_ref: dict[str, _RawReservationCapability] = {}
    for local in source.reservations:
        try:
            raw = source._capability._get(local.reservation_ref)
        except KeyError:
            continue
        if type(raw) is _RawReservationCapability:
            raw_by_ref[local.reservation_ref] = raw
    order_queries = sorted(
        {
            (raw.instrument_id, raw.order_id)
            for raw in raw_by_ref.values()
        }
    )
    position_queries = sorted(
        {
            (raw.instrument_id, raw.position_id)
            for raw in raw_by_ref.values()
        }
    )
    capture = _RawRecoveryCapture()
    try:
        with reader.request_scope(deadline_monotonic=deadline_monotonic):
            capture.positions = reader.read_positions()
            capture.open_orders = reader.read_open_orders()
            for instrument_id, order_id in order_queries:
                key = (instrument_id, order_id)
                capture.order_history_by_order[key] = reader.read_order_history(
                    inst_id=instrument_id,
                    order_id=order_id,
                    limit=100,
                )
                capture.fills_by_order[key] = reader.read_trade_fills(
                    inst_id=instrument_id,
                    order_id=order_id,
                    limit=100,
                )
            for instrument_id, position_id in position_queries:
                key = (instrument_id, position_id)
                capture.position_history_by_position[
                    key
                ] = reader.read_position_history(
                    inst_id=instrument_id,
                    pos_id=position_id,
                )
    except Exception as exc:
        capture.capture_error = exc
    completed_at = _require_aware_utc(
        _recovery_capture_now(),
        "capture_completed_at",
    )
    _validated_capture_times(started_at, completed_at)
    observations: list[BoundCloseReservationObservation] = []
    for local in source.reservations:
        exchange = _normalize_recovery_exchange_evidence(
            local=local,
            raw=raw_by_ref.get(local.reservation_ref),
            capture=capture,
        )
        observations.append(
            _classify_bound_close_reservation_pure(
                local,
                exchange,
                capture_completed_at=completed_at,
            )
        )
    plan = build_bound_close_reservation_recovery_plan(
        source_fingerprint=source.source_fingerprint,
        observations=tuple(observations),
    )
    serialized = serialize_bound_close_reservation_recovery_plan(
        plan,
        capture_started_at=started_at,
        capture_completed_at=completed_at,
    )
    sealed = object.__new__(SealedRecoveryCapture)
    sealed._SealedRecoveryCapture__apply_claimed = False
    sealed._SealedRecoveryCapture__apply_claim_lock = threading.Lock()
    sealed._SealedRecoveryCapture__plan = plan
    sealed._SealedRecoveryCapture__serialized_plan = serialized
    sealed._SealedRecoveryCapture__source_capability = source._capability
    return sealed


def _unavailable_exchange_evidence(reason_code: str) -> ExchangeReservationEvidence:
    return ExchangeReservationEvidence(
        capture_reason_code=reason_code,
        schema_valid=False,
        current_positions_complete=False,
        pending_orders_complete=False,
        order_history_complete=False,
        fills_complete=False,
        position_history_complete=False,
        order_history_at_limit=False,
        fills_at_limit=False,
        position_history_at_limit=False,
        current_positions=(),
        pending_orders=(),
        order_history=(),
        fills=(),
        position_history=(),
    )


def _normalize_recovery_exchange_evidence(
    *,
    local: LocalReservationEvidence,
    raw: _RawReservationCapability | None,
    capture: _RawRecoveryCapture,
) -> ExchangeReservationEvidence:
    if capture.capture_error is not None:
        return _unavailable_exchange_evidence(
            exchange_recovery_reason_from_error(capture.capture_error)
        )
    if raw is None:
        return _unavailable_exchange_evidence("exchange_evidence_unavailable")
    order_key = (raw.instrument_id, raw.order_id)
    position_key = (raw.instrument_id, raw.position_id)
    collections = {
        "positions": build_exchange_collection_evidence(
            endpoint="bound_close_positions",
            response=capture.positions,
            page_limit=100,
        ),
        "open_orders": build_exchange_collection_evidence(
            endpoint="bound_close_open_orders",
            response=capture.open_orders,
            page_limit=100,
        ),
        "order_history": build_exchange_collection_evidence(
            endpoint="bound_close_order_history",
            response=capture.order_history_by_order.get(order_key),
            page_limit=100,
        ),
        "fills": build_exchange_collection_evidence(
            endpoint="bound_close_fills",
            response=capture.fills_by_order.get(order_key),
            page_limit=100,
        ),
        "position_history": build_exchange_collection_evidence(
            endpoint="bound_close_position_history",
            response=capture.position_history_by_position.get(position_key),
            page_limit=100,
        ),
    }
    schema_valid = all(
        evidence.available and evidence.schema_valid
        for evidence in collections.values()
    )
    try:
        all_positions = _normalize_provider_positions(collections["positions"])
        all_open_orders = _normalize_provider_orders(collections["open_orders"])
        order_history = _normalize_provider_orders(collections["order_history"])
        fills = _normalize_provider_fills(collections["fills"])
        position_history = _normalize_provider_position_history(
            collections["position_history"]
        )
    except (TypeError, ValueError, ArithmeticError, OverflowError):
        schema_valid = False
        all_positions = ()
        all_open_orders = ()
        order_history = ()
        fills = ()
        position_history = ()
    current_positions = tuple(
        item for item in all_positions if item.position_ref == local.position_ref
    )
    pending_orders = tuple(
        item for item in all_open_orders if item.order_ref == local.close_order_ref
    )
    return ExchangeReservationEvidence(
        capture_reason_code=None,
        schema_valid=schema_valid,
        current_positions_complete=collections["positions"].complete,
        pending_orders_complete=collections["open_orders"].complete,
        order_history_complete=collections["order_history"].complete,
        fills_complete=collections["fills"].complete,
        position_history_complete=collections["position_history"].complete,
        order_history_at_limit=_collection_at_limit(collections["order_history"]),
        fills_at_limit=_collection_at_limit(collections["fills"]),
        position_history_at_limit=_collection_at_limit(
            collections["position_history"]
        ),
        current_positions=current_positions,
        pending_orders=pending_orders,
        order_history=order_history,
        fills=fills,
        position_history=position_history,
    )


def _collection_at_limit(evidence: ExchangeCollectionEvidence) -> bool:
    return evidence.row_count >= 100 and not evidence.complete


def _provider_text(row: Mapping[str, Any], field_name: str) -> str:
    value = row.get(field_name)
    if type(value) is not str:
        raise TypeError(f"provider {field_name} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized.encode("utf-8")) > 256:
        raise ValueError(f"provider {field_name} is invalid")
    return normalized


def _provider_identity(
    row: Mapping[str, Any],
    *,
    include_order: bool,
) -> tuple[str, str, str, str | None]:
    instrument_id = _provider_text(row, "instId")
    position_id = _provider_text(row, "posId")
    side = _provider_text(row, "posSide").lower()
    if side not in {"long", "short"}:
        raise ValueError("provider posSide is invalid")
    order_id = _provider_text(row, "ordId") if include_order else None
    return (
        _redacted_ref("instrument", instrument_id),
        side,
        _redacted_ref("position", position_id),
        _redacted_ref("close_order", order_id) if order_id is not None else None,
    )


def _provider_decimal(
    row: Mapping[str, Any],
    *aliases: str,
    positive: bool,
) -> Decimal:
    values = [row[name] for name in aliases if row.get(name) not in (None, "")]
    if len(values) != 1 or type(values[0]) is not str:
        raise ValueError("provider decimal alias is missing or ambiguous")
    raw = values[0].strip()
    if not raw or len(raw.encode("ascii", errors="ignore")) != len(raw):
        raise ValueError("provider decimal text is invalid")
    try:
        parsed = Decimal(raw)
    except Exception as exc:
        raise ValueError("provider decimal text is invalid") from exc
    return _require_decimal(parsed, aliases[0], positive=positive)


def _provider_timestamp(row: Mapping[str, Any], *aliases: str) -> datetime:
    values = [row[name] for name in aliases if row.get(name) not in (None, "")]
    if len(values) != 1:
        raise ValueError("provider timestamp alias is missing or ambiguous")
    raw = values[0]
    if type(raw) is not str or len(raw) != 13 or not raw.isascii() or not raw.isdigit():
        raise ValueError("provider timestamp must be epoch milliseconds")
    milliseconds = int(raw)
    try:
        parsed = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            milliseconds=milliseconds
        )
    except (OverflowError, ValueError) as exc:
        raise ValueError("provider timestamp is out of range") from exc
    if not datetime(2000, 1, 1, tzinfo=UTC) <= parsed <= datetime(
        2100, 1, 1, tzinfo=UTC
    ):
        raise ValueError("provider timestamp is out of recovery bounds")
    return parsed


_PROVIDER_ORDER_STATES = {
    "filled": ExchangeCloseOrderState.FILLED,
    "open": ExchangeCloseOrderState.OPEN,
    "live": ExchangeCloseOrderState.OPEN,
    "pending": ExchangeCloseOrderState.PENDING,
    "partially_filled": ExchangeCloseOrderState.PARTIALLY_FILLED,
    "rejected": ExchangeCloseOrderState.REJECTED,
    "cancelled": ExchangeCloseOrderState.CANCELLED,
    "canceled": ExchangeCloseOrderState.CANCELLED,
}


def _normalize_provider_positions(
    evidence: ExchangeCollectionEvidence,
) -> tuple[ExchangePositionEvidence, ...]:
    rows: list[ExchangePositionEvidence] = []
    for row in evidence.rows:
        instrument_ref, side, position_ref, _ = _provider_identity(
            row,
            include_order=False,
        )
        rows.append(
            ExchangePositionEvidence(
                instrument_ref=instrument_ref,
                side=side,
                position_ref=position_ref,
                quantity=_provider_decimal(row, "sz", positive=True),
            )
        )
    return tuple(rows)


def _normalize_provider_orders(
    evidence: ExchangeCollectionEvidence,
) -> tuple[ExchangeOrderEvidence, ...]:
    rows: list[ExchangeOrderEvidence] = []
    for row in evidence.rows:
        instrument_ref, side, position_ref, order_ref = _provider_identity(
            row,
            include_order=True,
        )
        provider_state = _provider_text(row, "state").lower()
        state = _PROVIDER_ORDER_STATES.get(provider_state)
        if state is None:
            raise ValueError("provider order state is not closed")
        terminal_at = None
        if state not in {
            ExchangeCloseOrderState.OPEN,
            ExchangeCloseOrderState.PENDING,
            ExchangeCloseOrderState.PARTIALLY_FILLED,
        }:
            terminal_at = _provider_timestamp(row, "uTime", "cTime")
        rows.append(
            ExchangeOrderEvidence(
                instrument_ref=instrument_ref,
                side=side,
                position_ref=position_ref,
                order_ref=order_ref,
                state=state,
                requested_quantity=_provider_decimal(row, "sz", positive=True),
                filled_quantity=_provider_decimal(
                    row,
                    "accFillSz",
                    "fillSz",
                    positive=False,
                ),
                terminal_at=terminal_at,
            )
        )
    return tuple(rows)


def _normalize_provider_fills(
    evidence: ExchangeCollectionEvidence,
) -> tuple[ExchangeFillEvidence, ...]:
    rows: list[ExchangeFillEvidence] = []
    for row in evidence.rows:
        instrument_ref, side, position_ref, order_ref = _provider_identity(
            row,
            include_order=True,
        )
        rows.append(
            ExchangeFillEvidence(
                instrument_ref=instrument_ref,
                side=side,
                position_ref=position_ref,
                order_ref=order_ref,
                quantity=_provider_decimal(row, "fillSz", positive=True),
                filled_at=_provider_timestamp(
                    row,
                    "fillTime",
                    "uTime",
                    "cTime",
                ),
            )
        )
    return tuple(rows)


def _normalize_provider_position_history(
    evidence: ExchangeCollectionEvidence,
) -> tuple[ExchangePositionHistoryEvidence, ...]:
    rows: list[ExchangePositionHistoryEvidence] = []
    for row in evidence.rows:
        instrument_ref, side, position_ref, _ = _provider_identity(
            row,
            include_order=False,
        )
        provider_state = _provider_text(row, "state").lower()
        try:
            state = ExchangePositionHistoryState(provider_state)
        except ValueError as exc:
            raise ValueError("provider position-history state is not closed") from exc
        closed_at = (
            _provider_timestamp(row, "uTime", "cTime")
            if state is ExchangePositionHistoryState.CLOSED
            else None
        )
        rows.append(
            ExchangePositionHistoryEvidence(
                instrument_ref=instrument_ref,
                side=side,
                position_ref=position_ref,
                state=state,
                closed_quantity=_provider_decimal(
                    row,
                    "closeSz",
                    "closedSize",
                    positive=False,
                ),
                closed_at=closed_at,
            )
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class _ParsedRecoveryDryRun:
    plan: BoundCloseReservationRecoveryPlan
    capture_started_at: datetime
    capture_completed_at: datetime
    capture_identity: str
    semantic_json: str


def _bounded_recovery_json_tree(value: object) -> None:
    item_count = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_RECOVERY_PLAN_DEPTH:
            raise ValueError("recovery JSON exceeds its depth bound")
        item_count += 1
        if item_count > _MAX_RECOVERY_PLAN_ITEMS:
            raise ValueError("recovery JSON exceeds its item bound")
        if type(current) is str:
            if len(current.encode("utf-8")) > _MAX_RECOVERY_PLAN_STRING_BYTES:
                raise ValueError("recovery JSON string exceeds its byte bound")
        elif type(current) is dict:
            stack.extend((key, depth + 1) for key in current)
            stack.extend((item, depth + 1) for item in current.values())
        elif type(current) is list:
            stack.extend((item, depth + 1) for item in current)
        elif current is not None and type(current) not in {bool, int}:
            raise TypeError("recovery JSON contains an unsupported value")


def _exact_object_keys(
    value: object,
    expected: frozenset[str],
    field_name: str,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{field_name} must be an object")
    if frozenset(value) != expected:
        raise ValueError(f"{field_name} has unknown or missing fields")
    return value


def _strict_nonnegative_int(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return value


def _parse_canonical_capture_time(value: object, field_name: str) -> datetime:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if not value.endswith("Z"):
        raise ValueError(f"{field_name} must be canonical aware UTC")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be canonical aware UTC") from exc
    _require_aware_utc(parsed, field_name)
    if _canonical_json_value(parsed) != value:
        raise ValueError(f"{field_name} must be canonical aware UTC")
    return parsed


def _parse_bound_close_reservation_dry_run_document(
    raw: bytes,
) -> _ParsedRecoveryDryRun:
    if type(raw) is not bytes:
        raise TypeError("raw recovery document must be bytes")
    if not raw or len(raw) > MAX_RECOVERY_PLAN_BYTES:
        raise ValueError("recovery document violates its byte bound")
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_closed_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise ValueError("recovery document is invalid JSON") from exc
    _bounded_recovery_json_tree(value)
    payload = _exact_object_keys(value, _RECOVERY_PLAN_KEYS, "plan")
    if type(payload["schema_version"]) is not int:
        raise TypeError("schema_version must be an integer")
    if payload["schema_version"] != RECOVERY_SCHEMA_VERSION:
        raise ValueError("schema_version is unsupported")
    if payload["mode"] != "dry_run" or type(payload["mode"]) is not str:
        raise ValueError("mode must be dry_run")
    observations_raw = payload["observations"]
    if type(observations_raw) is not list:
        raise TypeError("observations must be an array")
    if len(observations_raw) > MAX_RESERVATION_OBSERVATIONS:
        raise ValueError("observations exceed their item bound")
    observations: list[BoundCloseReservationObservation] = []
    for index, raw_item in enumerate(observations_raw):
        item = _exact_object_keys(
            raw_item,
            _RECOVERY_OBSERVATION_KEYS,
            f"observations[{index}]",
        )
        if type(item["classification"]) is not str:
            raise TypeError("classification must be a string")
        try:
            classification = ReservationClassification(item["classification"])
        except ValueError as exc:
            raise ValueError("classification is not closed") from exc
        observations.append(
            BoundCloseReservationObservation(
                reservation_ref=item["reservation_ref"],
                classification=classification,
                reason_code=item["reason_code"],
                source_fingerprint=item["source_fingerprint"],
                exchange_fingerprint=item["exchange_fingerprint"],
            )
        )
    counts = _exact_object_keys(payload["counts"], _RECOVERY_COUNT_KEYS, "counts")
    for name in _RECOVERY_COUNT_KEYS:
        _strict_nonnegative_int(counts[name], f"counts.{name}")
    for name in ("action_count", "exchange_writes", "history_replays", "database_writes"):
        _strict_nonnegative_int(payload[name], name)
    if any(payload[name] != 0 for name in ("exchange_writes", "history_replays", "database_writes")):
        raise ValueError("recovery plan write counters must be zero")
    source_fingerprint = _require_lower_hex_64(
        payload["source_fingerprint"],
        "source_fingerprint",
    )
    expected = build_bound_close_reservation_recovery_plan(
        source_fingerprint=source_fingerprint,
        observations=tuple(observations),
    )
    expected_counts = _recovery_counts(expected.observations)
    if counts != expected_counts:
        raise ValueError("counts do not conserve the observation population")
    for field_name in (
        "status",
        "action_count",
        "exchange_snapshot_fingerprint",
        "evidence_fingerprint",
        "confirmation_token",
    ):
        if payload[field_name] != getattr(expected, field_name):
            raise ValueError(f"{field_name} does not match canonical evidence")
    if tuple(observations) != expected.observations:
        raise ValueError("observations must be sorted and unique")
    started_at = _parse_canonical_capture_time(
        payload["capture_started_at"],
        "capture_started_at",
    )
    completed_at = _parse_canonical_capture_time(
        payload["capture_completed_at"],
        "capture_completed_at",
    )
    _validated_capture_times(started_at, completed_at)
    capture_identity = _require_lower_hex_64(
        payload["capture_identity"],
        "capture_identity",
    )
    if capture_identity != _capture_identity(
        capture_started_at=started_at,
        capture_completed_at=completed_at,
        evidence_fingerprint=expected.evidence_fingerprint,
    ):
        raise ValueError("capture_identity does not match capture")
    semantic = _canonical_json(
        _recovery_semantic_payload(
            status=expected.status,
            observations=expected.observations,
            source_fingerprint=expected.source_fingerprint,
            exchange_snapshot_fingerprint=expected.exchange_snapshot_fingerprint,
            action_count=expected.action_count,
        )
    )
    return _ParsedRecoveryDryRun(
        plan=expected,
        capture_started_at=started_at,
        capture_completed_at=completed_at,
        capture_identity=capture_identity,
        semantic_json=semantic,
    )


def _require_lower_hex_64(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if _LOWER_HEX_64.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be lowercase 64-hex")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json_value(value: Any) -> Any:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is Decimal:
        return _canonical_decimal_text(value)
    if isinstance(value, datetime):
        offset = value.utcoffset() if value.tzinfo is not None else None
        if offset != timedelta(0):
            raise ValueError("datetime must be aware UTC")
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return _canonical_json_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {
            key: _canonical_json_value(item)
            for key, item in value.items()
        }
    if type(value) in {tuple, list}:
        return [_canonical_json_value(item) for item in value]
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _require_aware_utc(value: object, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError(f"{field_name} must be an aware UTC datetime")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        offset = None
    if offset != timedelta(0):
        raise ValueError(f"{field_name} must be an aware UTC datetime")
    return value


def _require_optional_aware_utc(
    value: object,
    field_name: str,
) -> datetime | None:
    if value is None:
        return None
    return _require_aware_utc(value, field_name)


def _require_decimal(
    value: object,
    field_name: str,
    *,
    positive: bool,
) -> Decimal:
    if type(value) is not Decimal:
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    try:
        _canonical_decimal_text(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be bounded") from exc
    if value < 0 or (positive and value <= 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{field_name} must be {qualifier}")
    return value


def _canonical_decimal_text(value: Decimal) -> str:
    """Render one exact bounded decimal without consulting Decimal context."""

    if type(value) is not Decimal:
        raise TypeError("value must be a Decimal")
    if not value.is_finite():
        raise ValueError("Decimal must be finite")
    sign, raw_digits, raw_exponent = value.as_tuple()
    if type(raw_exponent) is not int:
        raise ValueError("Decimal exponent must be finite")
    digits = list(raw_digits)
    if len(digits) > _MAX_EXCHANGE_DECIMAL_DIGITS:
        raise ValueError("Decimal digits exceed recovery bound")
    exponent = raw_exponent
    if abs(exponent) > _MAX_EXCHANGE_DECIMAL_ABS_EXPONENT:
        raise ValueError("Decimal exponent exceeds recovery bound")
    if not any(digits):
        return "0"
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(chr(48 + digit) for digit in digits)
    if exponent >= 0:
        rendered = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            rendered = f"{coefficient[:point]}.{coefficient[point:]}"
        else:
            rendered = f"0.{('0' * -point)}{coefficient}"
    if sign:
        rendered = f"-{rendered}"
    if len(rendered.encode("ascii")) > _MAX_EXCHANGE_DECIMAL_TEXT_BYTES:
        raise ValueError("Decimal text exceeds recovery bound")
    return rendered


def _validate_exchange_identity(
    *,
    instrument_ref: object,
    side: object,
    position_ref: object,
    order_ref: object | None = None,
) -> None:
    _require_lower_hex_64(instrument_ref, "instrument_ref")
    if type(side) is not str:
        raise TypeError("side must be a string")
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    _require_lower_hex_64(position_ref, "position_ref")
    if order_ref is not None:
        _require_lower_hex_64(order_ref, "order_ref")


def _require_exchange_collection(
    value: object,
    field_name: str,
    expected_type: type,
) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be a tuple")
    if len(value) > _MAX_EXCHANGE_EVIDENCE_ITEMS:
        raise ValueError(f"{field_name} cannot contain more than 100 items")
    if any(type(item) is not expected_type for item in value):
        raise TypeError(f"{field_name} contains an invalid evidence item")


def exchange_recovery_reason_from_error(error: BaseException) -> str:
    """Map recovery read failures to the closed UNKNOWN reason vocabulary."""

    if isinstance(error, BoundCloseReservationExchangeDeadlineExceeded):
        return "exchange_capture_timeout"
    if isinstance(error, DeepcoinReadUnavailable):
        safe_code = error.fact.safe_code
        if safe_code in {
            "request_deadline_exceeded",
            "governor_deadline_exceeded",
        }:
            return "exchange_capture_timeout"
        if safe_code == "monitor_response_size_exceeded":
            return "exchange_response_size_exceeded"
        if "schema" in safe_code:
            return "exchange_schema_invalid"
    return "exchange_evidence_unavailable"


def classify_bound_close_reservation(
    local: LocalReservationEvidence,
    exchange: ExchangeReservationEvidence,
    *,
    capture_completed_at: datetime,
) -> BoundCloseReservationObservation:
    """Pure non-authoritative classifier; it never issues apply capability."""

    return _classify_bound_close_reservation_pure(
        local,
        exchange,
        capture_completed_at=capture_completed_at,
    )


def _classify_bound_close_reservation_pure(
    local: LocalReservationEvidence,
    exchange: ExchangeReservationEvidence,
    *,
    capture_completed_at: datetime,
) -> BoundCloseReservationObservation:
    """Classify one exact chain without age or callback-delay inference."""

    if type(local) is not LocalReservationEvidence:
        raise TypeError("local must be LocalReservationEvidence")
    if type(exchange) is not ExchangeReservationEvidence:
        raise TypeError("exchange must be ExchangeReservationEvidence")
    _require_aware_utc(capture_completed_at, "capture_completed_at")

    source_fingerprint = _sha256_json(local)
    exchange_fingerprint = _sha256_json(exchange)

    def observation(
        classification: ReservationClassification,
        reason_code: str,
    ) -> BoundCloseReservationObservation:
        return BoundCloseReservationObservation(
            reservation_ref=local.reservation_ref,
            classification=classification,
            reason_code=reason_code,
            source_fingerprint=source_fingerprint,
            exchange_fingerprint=exchange_fingerprint,
        )

    if local.local_reason_code in {
        "local_evidence_incomplete",
        "local_identity_conflict",
    }:
        return observation(
            ReservationClassification.UNKNOWN,
            local.local_reason_code,
        )
    if not _local_classification_shape_is_complete(
        local,
        capture_completed_at=capture_completed_at,
    ):
        return observation(
            ReservationClassification.UNKNOWN,
            "local_evidence_incomplete",
        )
    if exchange.capture_reason_code is not None:
        return observation(
            ReservationClassification.UNKNOWN,
            exchange.capture_reason_code,
        )
    if not exchange.schema_valid:
        return observation(
            ReservationClassification.UNKNOWN,
            "exchange_schema_invalid",
        )
    if not all(
        (
            exchange.current_positions_complete,
            exchange.pending_orders_complete,
            exchange.order_history_complete,
            exchange.fills_complete,
            exchange.position_history_complete,
        )
    ) or any(
        (
            exchange.order_history_at_limit,
            exchange.fills_at_limit,
            exchange.position_history_at_limit,
        )
    ):
        return observation(
            ReservationClassification.UNKNOWN,
            "exchange_history_incomplete",
        )
    if not _exchange_identities_match(local, exchange):
        return observation(
            ReservationClassification.UNKNOWN,
            "exchange_identity_conflict",
        )
    if any(
        len(collection) > 1
        for collection in (
            exchange.current_positions,
            exchange.pending_orders,
            exchange.order_history,
            exchange.fills,
            exchange.position_history,
        )
    ):
        return observation(
            ReservationClassification.UNKNOWN,
            "exchange_state_conflict",
        )
    if not all(
        _exchange_order_shape_is_valid(order)
        for order in (*exchange.pending_orders, *exchange.order_history)
    ):
        return observation(
            ReservationClassification.UNKNOWN,
            "exchange_state_conflict",
        )
    if exchange.current_positions:
        return observation(
            ReservationClassification.ACTIVE,
            "exact_position_currently_live",
        )
    if exchange.pending_orders:
        if exchange.pending_orders[0].state not in {
            ExchangeCloseOrderState.OPEN,
            ExchangeCloseOrderState.PENDING,
        }:
            return observation(
                ReservationClassification.UNKNOWN,
                "exchange_state_conflict",
            )
        return observation(
            ReservationClassification.ACTIVE,
            "exact_close_order_currently_pending",
        )

    if len(exchange.order_history) != 1:
        return observation(
            ReservationClassification.UNKNOWN,
            "exchange_history_incomplete",
        )
    order = exchange.order_history[0]
    if order.state in {
        ExchangeCloseOrderState.OPEN,
        ExchangeCloseOrderState.PENDING,
    }:
        return observation(
            ReservationClassification.ACTIVE,
            "exact_close_order_nonterminal",
        )
    if order.state is not ExchangeCloseOrderState.FILLED:
        return observation(
            ReservationClassification.UNKNOWN,
            "exchange_state_conflict",
        )
    if len(exchange.fills) != 1 or len(exchange.position_history) != 1:
        return observation(
            ReservationClassification.UNKNOWN,
            "exchange_history_incomplete",
        )

    fill = exchange.fills[0]
    position = exchange.position_history[0]
    if _local_mutation_conflicts(local):
        return observation(
            ReservationClassification.UNKNOWN,
            "exchange_state_conflict",
        )
    if (
        order.terminal_at is None
        or position.closed_at is None
        or position.state is not ExchangePositionHistoryState.CLOSED
        or order.requested_quantity <= 0
        or order.filled_quantity != order.requested_quantity
        or fill.quantity != order.requested_quantity
        or position.closed_quantity != order.requested_quantity
    ):
        return observation(
            ReservationClassification.UNKNOWN,
            "exchange_state_conflict",
        )
    if not (
        local.reservation_created_at <= order.terminal_at
        and local.event_created_at <= order.terminal_at
        and order.terminal_at <= position.closed_at
        and position.closed_at <= capture_completed_at
        and local.reservation_created_at <= fill.filled_at
        and local.event_created_at <= fill.filled_at
        and fill.filled_at <= order.terminal_at
        and fill.filled_at <= position.closed_at
        and fill.filled_at <= capture_completed_at
    ):
        return observation(
            ReservationClassification.UNKNOWN,
            "exchange_state_conflict",
        )
    return observation(
        ReservationClassification.PROVEN_TERMINAL,
        "exact_close_and_position_terminal",
    )


def _local_classification_shape_is_complete(
    local: LocalReservationEvidence,
    *,
    capture_completed_at: datetime,
) -> bool:
    required_refs = (
        local.reservation_ref,
        local.reservation_record_fingerprint,
        local.binding_ref,
        local.binding_record_fingerprint,
        local.instrument_ref,
        local.position_ref,
        local.close_event_ref,
        local.close_order_ref,
        local.close_event_record_fingerprint,
    )
    if any(not _is_lower_hex_64(value) for value in required_refs):
        return False
    if (
        local.local_reason_code is not None
        or local.source_status not in ACTIVE_CLOSE_RESERVATION_STATUSES
        or local.binding_status not in _EXECUTION_BINDING_STATUSES
        or local.side not in {"long", "short"}
        or type(local.close_mutations) is not tuple
        or any(
            type(mutation) is not LocalCloseMutationEvidence
            for mutation in local.close_mutations
        )
    ):
        return False
    try:
        reservation_created_at = _require_aware_utc(
            local.reservation_created_at,
            "reservation_created_at",
        )
        reservation_updated_at = _require_aware_utc(
            local.reservation_updated_at,
            "reservation_updated_at",
        )
        _require_aware_utc(local.event_created_at, "event_created_at")
    except (TypeError, ValueError):
        return False
    if not (
        reservation_created_at <= reservation_updated_at <= capture_completed_at
        and local.event_created_at <= capture_completed_at
    ):
        return False
    return all(
        _local_mutation_shape_is_valid(
            mutation,
            capture_completed_at=capture_completed_at,
        )
        for mutation in local.close_mutations
    )


def _exchange_order_shape_is_valid(order: ExchangeOrderEvidence) -> bool:
    if (
        order.requested_quantity <= 0
        or order.filled_quantity > order.requested_quantity
    ):
        return False
    if order.state in {
        ExchangeCloseOrderState.OPEN,
        ExchangeCloseOrderState.PENDING,
        ExchangeCloseOrderState.PARTIALLY_FILLED,
    }:
        return order.terminal_at is None
    if order.terminal_at is None:
        return False
    if order.state is ExchangeCloseOrderState.FILLED:
        return order.filled_quantity == order.requested_quantity
    return True


def _local_mutation_shape_is_valid(
    mutation: LocalCloseMutationEvidence,
    *,
    capture_completed_at: datetime | None,
) -> bool:
    if (
        not _is_lower_hex_64(mutation.mutation_ref)
        or mutation.status not in _POSITION_MUTATION_STATUSES
        or (
            mutation.order_ref is not None
            and not _is_lower_hex_64(mutation.order_ref)
        )
        or not _is_lower_hex_64(mutation.record_fingerprint)
    ):
        return False
    try:
        reserved_at = _require_aware_utc(mutation.reserved_at, "reserved_at")
        created_at = _require_aware_utc(mutation.created_at, "created_at")
        updated_at = _require_aware_utc(mutation.updated_at, "updated_at")
        submitted_at = _require_optional_aware_utc(
            mutation.submitted_at,
            "submitted_at",
        )
        confirmed_at = _require_optional_aware_utc(
            mutation.confirmed_at,
            "confirmed_at",
        )
    except (TypeError, ValueError):
        return False
    if not created_at <= reserved_at <= updated_at:
        return False
    if submitted_at is not None and not (
        reserved_at <= submitted_at <= updated_at
    ):
        return False
    if confirmed_at is not None and (
        submitted_at is None
        or not submitted_at <= confirmed_at <= updated_at
    ):
        return False
    if capture_completed_at is not None and updated_at > capture_completed_at:
        return False
    if mutation.status in {"reserved", "not_sent", "submitting"}:
        return submitted_at is None and confirmed_at is None
    if mutation.status == "submitted":
        return submitted_at is not None and confirmed_at is None
    if mutation.status == "confirmed":
        return submitted_at is not None and confirmed_at is not None
    if mutation.status == "rejected":
        return (submitted_at is None and confirmed_at is None) or (
            submitted_at is not None and confirmed_at is not None
        )
    return confirmed_at is None


def _exchange_identities_match(
    local: LocalReservationEvidence,
    exchange: ExchangeReservationEvidence,
) -> bool:
    def position_matches(item: object) -> bool:
        return (
            item.instrument_ref == local.instrument_ref
            and item.side == local.side
            and item.position_ref == local.position_ref
        )

    def order_matches(item: object) -> bool:
        return position_matches(item) and item.order_ref == local.close_order_ref

    return all(
        (
            all(position_matches(item) for item in exchange.current_positions),
            all(order_matches(item) for item in exchange.pending_orders),
            all(order_matches(item) for item in exchange.order_history),
            all(order_matches(item) for item in exchange.fills),
            all(position_matches(item) for item in exchange.position_history),
        )
    )


def _local_mutation_conflicts(local: LocalReservationEvidence) -> bool:
    for mutation in local.close_mutations:
        if mutation.order_ref is not None and mutation.order_ref != local.close_order_ref:
            return True
        if mutation.status in {"rejected", "recovery_required", "blocked"}:
            return True
        if mutation.status == "confirmed" and mutation.order_ref is None:
            return True
    return False


_REQUIRED_SOURCE_COLUMNS = {
    "bound_position_close_reservations": frozenset(
        {
            "id",
            "pos_id",
            "execution_binding_id",
            "status",
            "last_error",
            "created_at",
            "updated_at",
        }
    ),
    "execution_bindings": frozenset(
        {
            "id",
            "strategy_instance_id",
            "symbol",
            "side",
            "venue",
            "pos_id",
            "status",
            "payload_json",
            "created_at",
            "updated_at",
        }
    ),
    "execution_events": frozenset(
        {
            "id",
            "execution_binding_id",
            "strategy_instance_id",
            "venue",
            "action",
            "status",
            "symbol",
            "side",
            "order_id",
            "pos_id",
            "before_json",
            "after_json",
            "request_json",
            "response_json",
            "created_at",
        }
    ),
    "execution_order_legs": frozenset(
        {
            "id",
            "execution_binding_id",
            "strategy_instance_id",
            "leg_index",
            "purpose",
            "order_kind",
            "order_id",
            "client_order_id",
            "pos_id",
            "venue",
            "attribution_status",
            "status",
            "attribution_evidence_json",
            "terminal_reason",
            "request_json",
            "response_json",
            "last_verified_at",
            "created_at",
            "updated_at",
        }
    ),
    "position_mutation_intents": frozenset(
        {
            "id",
            "idempotency_key",
            "operation",
            "strategy_instance_id",
            "execution_binding_id",
            "execution_order_leg_id",
            "pos_id",
            "order_id",
            "authority_fingerprint",
            "request_fingerprint",
            "venue",
            "status",
            "request_json",
            "response_json",
            "error_json",
            "reserved_at",
            "submitted_at",
            "confirmed_at",
            "created_at",
            "updated_at",
        }
    ),
}


def load_bound_close_reservation_source(
    database_path: str | Path,
    *,
    between_selects_hook: Callable[[], None] | None = None,
) -> BoundCloseReservationSource:
    """Load the complete nonterminal population from one read-only snapshot.

    Source corruption and schema drift are represented by an UNKNOWN local fact
    instead of an empty population, because an empty result could otherwise be
    mistaken for a safe recovery state.
    """

    if not isinstance(database_path, (str, Path)):
        raise TypeError("database_path must be a string or Path")
    if between_selects_hook is not None and not callable(between_selects_hook):
        raise TypeError("between_selects_hook must be callable")
    resolved = Path(database_path).expanduser().resolve()
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or int(query_only[0]) != 1:
            return _source_failure("source_query_only_unavailable")
        connection.execute("BEGIN")
        if not _source_schema_is_valid(connection):
            return _source_failure("source_schema_invalid")
        reservation_rows = connection.execute(
            """
            SELECT id, pos_id, execution_binding_id, status, last_error,
                   created_at, updated_at
            FROM bound_position_close_reservations
            WHERE status IS NULL OR status != 'confirmed'
            ORDER BY id
            LIMIT 65
            """
        ).fetchall()
        if len(reservation_rows) > MAX_RESERVATION_OBSERVATIONS:
            return _source_failure("source_overflow")
        if between_selects_hook is not None:
            between_selects_hook()
        return _load_source_descendants(connection, reservation_rows)
    except (sqlite3.Error, OSError, TypeError, ValueError, OverflowError):
        return _source_failure("source_read_failed")
    finally:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                connection.close()


def _source_schema_is_valid(connection: sqlite3.Connection) -> bool:
    for table_name, required_columns in _REQUIRED_SOURCE_COLUMNS.items():
        rows = connection.execute(
            f'PRAGMA table_info("{table_name}")'
        ).fetchall()
        actual_columns = {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in rows
        }
        if not required_columns.issubset(actual_columns):
            return False
    return True


def _load_source_descendants(
    connection: sqlite3.Connection,
    reservation_rows: Sequence[sqlite3.Row],
) -> BoundCloseReservationSource:
    if not reservation_rows:
        return _make_source((), {})
    binding_ids = tuple(
        row["execution_binding_id"]
        for row in reservation_rows
        if type(row["execution_binding_id"]) is int
    )
    position_ids = tuple(
        row["pos_id"]
        for row in reservation_rows
        if type(row["pos_id"]) is str and row["pos_id"].strip()
    )
    binding_rows = _select_in(
        connection,
        """
        SELECT id, strategy_instance_id, symbol, side, venue, pos_id, status,
               payload_json, created_at, updated_at
        FROM execution_bindings
        WHERE id IN ({placeholders})
        ORDER BY id
        """,
        binding_ids,
    )
    event_rows = _select_in(
        connection,
        """
        SELECT id, execution_binding_id, strategy_instance_id, venue, action,
               status, symbol, side, order_id, pos_id, before_json, after_json,
               request_json, response_json, created_at
        FROM execution_events
        WHERE action = 'close_bound_position_market'
          AND pos_id IN ({placeholders})
        ORDER BY id
        LIMIT 257
        """,
        position_ids,
    )
    leg_rows = _select_in(
        connection,
        """
        SELECT id, execution_binding_id, strategy_instance_id, leg_index,
               purpose, order_kind, order_id, client_order_id, pos_id, venue,
               attribution_status, status, attribution_evidence_json,
               terminal_reason, request_json, response_json, last_verified_at,
               created_at, updated_at
        FROM execution_order_legs
        WHERE pos_id IN ({placeholders})
        ORDER BY id
        LIMIT 257
        """,
        position_ids,
    )
    mutation_rows = _select_in(
        connection,
        """
        SELECT id, idempotency_key, operation, strategy_instance_id,
               execution_binding_id, execution_order_leg_id, pos_id, order_id,
               authority_fingerprint, request_fingerprint, venue, status,
               request_json, response_json, error_json, reserved_at, submitted_at,
               confirmed_at, created_at, updated_at
        FROM position_mutation_intents
        WHERE operation = 'close_position'
          AND pos_id IN ({placeholders})
        ORDER BY id
        LIMIT 257
        """,
        position_ids,
    )
    if any(
        len(rows) > _MAX_LOCAL_DESCENDANTS
        for rows in (event_rows, leg_rows, mutation_rows)
    ):
        return _source_failure("source_descendant_overflow")

    bindings_by_id = _group_rows(binding_rows, "id")
    events_by_position = _group_rows(event_rows, "pos_id")
    legs_by_position = _group_rows(leg_rows, "pos_id")
    mutations_by_position = _group_rows(mutation_rows, "pos_id")
    evidences: list[LocalReservationEvidence] = []
    raw_capabilities: dict[str, _RawReservationCapability] = {}
    for index, reservation in enumerate(reservation_rows):
        evidence, raw = _build_local_reservation_evidence(
            reservation,
            row_index=index,
            binding_rows=bindings_by_id.get(reservation["execution_binding_id"], ()),
            event_rows=events_by_position.get(reservation["pos_id"], ()),
            leg_rows=legs_by_position.get(reservation["pos_id"], ()),
            mutation_rows=mutations_by_position.get(reservation["pos_id"], ()),
        )
        evidences.append(evidence)
        if raw is not None:
            raw_capabilities[evidence.reservation_ref] = raw
    return _make_source(tuple(evidences), raw_capabilities)


def _select_in(
    connection: sqlite3.Connection,
    sql: str,
    values: Sequence[object],
) -> list[sqlite3.Row]:
    if not values:
        return []
    placeholders = ",".join("?" for _ in values)
    return connection.execute(
        sql.format(placeholders=placeholders),
        tuple(values),
    ).fetchall()


def _group_rows(
    rows: Sequence[sqlite3.Row], key: str
) -> dict[object, tuple[sqlite3.Row, ...]]:
    grouped: dict[object, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row[key], []).append(row)
    return {value: tuple(group) for value, group in grouped.items()}


def _build_local_reservation_evidence(
    reservation: sqlite3.Row,
    *,
    row_index: int,
    binding_rows: Sequence[sqlite3.Row],
    event_rows: Sequence[sqlite3.Row],
    leg_rows: Sequence[sqlite3.Row],
    mutation_rows: Sequence[sqlite3.Row],
) -> tuple[LocalReservationEvidence, _RawReservationCapability | None]:
    reservation_id = reservation["id"]
    reservation_ref = _redacted_ref(
        "reservation",
        reservation_id if type(reservation_id) is int else f"malformed-{row_index}",
    )
    source_status = reservation["status"]
    if type(source_status) is not str:
        source_status = "source_row_invalid"
    position_id = reservation["pos_id"]
    binding_id = reservation["execution_binding_id"]
    position_ref = (
        _redacted_ref("position", position_id)
        if type(position_id) is str and position_id.strip()
        else None
    )
    reservation_created_at = _parse_sqlite_utc(reservation["created_at"])
    reservation_updated_at = _parse_sqlite_utc(reservation["updated_at"])
    local_reason: str | None = None
    if (
        type(reservation_id) is not int
        or reservation_id <= 0
        or type(binding_id) is not int
        or binding_id <= 0
        or position_ref is None
        or source_status not in ACTIVE_CLOSE_RESERVATION_STATUSES
        or reservation_created_at is None
        or reservation_updated_at is None
        or reservation_created_at > reservation_updated_at
        or not _optional_text(reservation["last_error"])
    ):
        local_reason = "local_evidence_incomplete"

    binding = binding_rows[0] if len(binding_rows) == 1 else None
    if binding is None:
        local_reason = "local_evidence_incomplete"
    binding_identity_valid = binding is not None and _binding_row_has_exact_identity(
        binding, binding_id=binding_id, position_id=position_id
    )
    binding_shape_valid = binding is not None and _binding_row_shape_is_complete(binding)
    binding_valid = binding_identity_valid and binding_shape_valid
    if binding is not None and not binding_identity_valid:
        local_reason = "local_identity_conflict"
    elif binding is not None and not binding_shape_valid:
        local_reason = "local_evidence_incomplete"

    event = event_rows[0] if len(event_rows) == 1 else None
    if not event_rows:
        local_reason = "local_evidence_incomplete"
    elif len(event_rows) != 1:
        local_reason = "local_identity_conflict"
    event_identity_valid = (
        event is not None
        and binding is not None
        and _event_row_has_exact_identity(
            event,
            binding=binding,
            binding_id=binding_id,
            position_id=position_id,
        )
    )
    event_shape_valid = event is not None and _event_row_shape_is_complete(event)
    event_valid = event_identity_valid and event_shape_valid
    if event is not None and binding is not None and not event_identity_valid:
        local_reason = "local_identity_conflict"
    elif event is not None and not event_shape_valid:
        local_reason = "local_evidence_incomplete"

    entry_leg = None
    entry_leg_row = leg_rows[0] if len(leg_rows) == 1 else None
    if len(leg_rows) > 1:
        local_reason = "local_identity_conflict"
    if entry_leg_row is not None:
        if binding is None:
            local_reason = "local_evidence_incomplete"
        elif not _entry_leg_row_has_exact_identity(
            entry_leg_row,
            binding=binding,
            binding_id=binding_id,
            position_id=position_id,
        ):
            local_reason = "local_identity_conflict"
        else:
            entry_leg = _entry_leg_evidence(entry_leg_row)
            if entry_leg is None:
                local_reason = "local_evidence_incomplete"

    mutation_evidences: list[LocalCloseMutationEvidence] = []
    for mutation in mutation_rows:
        if binding is None:
            local_reason = "local_evidence_incomplete"
            continue
        if entry_leg_row is None or not _mutation_row_has_exact_identity(
                mutation,
                binding=binding,
                binding_id=binding_id,
                position_id=position_id,
                entry_leg_id=entry_leg_row["id"],
            ):
            local_reason = "local_identity_conflict"
            continue
        mutation_evidence = _mutation_evidence(mutation)
        if mutation_evidence is None:
            local_reason = "local_evidence_incomplete"
            continue
        mutation_evidences.append(mutation_evidence)

    reservation_record_fingerprint = _safe_row_fingerprint(
        "reservation", reservation
    )
    binding_record_fingerprint = (
        _safe_row_fingerprint("binding", binding) if binding is not None else None
    )
    close_event_record_fingerprint = (
        _safe_row_fingerprint("close_event", event) if event is not None else None
    )
    if reservation_record_fingerprint is None:
        reservation_record_fingerprint = _redacted_ref(
            "malformed_reservation_record", row_index
        )
        local_reason = "local_evidence_incomplete"
    if binding is not None and binding_record_fingerprint is None:
        local_reason = "local_evidence_incomplete"
    if event is not None and close_event_record_fingerprint is None:
        local_reason = "local_evidence_incomplete"

    evidence = LocalReservationEvidence(
        reservation_ref=reservation_ref,
        source_status=source_status,
        local_reason_code=local_reason,
        reservation_created_at=reservation_created_at,
        reservation_updated_at=reservation_updated_at,
        reservation_record_fingerprint=reservation_record_fingerprint,
        binding_ref=(
            _redacted_ref("binding", binding_id)
            if type(binding_id) is int and binding_id > 0
            else None
        ),
        binding_status=(
            binding["status"]
            if binding is not None and type(binding["status"]) is str
            else None
        ),
        instrument_ref=(
            _redacted_ref("instrument", _binding_instrument_id(binding["symbol"]))
            if binding_valid
            else None
        ),
        side=(binding["side"] if binding_valid else None),
        binding_record_fingerprint=binding_record_fingerprint,
        position_ref=position_ref,
        close_event_ref=(
            _redacted_ref("close_event", event["id"])
            if event_valid
            else None
        ),
        close_order_ref=(
            _redacted_ref("close_order", event["order_id"])
            if event_valid
            else None
        ),
        event_created_at=(
            _parse_sqlite_utc(event["created_at"]) if event_valid else None
        ),
        close_event_record_fingerprint=close_event_record_fingerprint,
        entry_leg=entry_leg,
        close_mutations=tuple(mutation_evidences),
    )
    raw_capability = None
    if local_reason is None and binding is not None and event is not None:
        raw_capability = _RawReservationCapability(
            reservation_id=reservation_id,
            source_status=source_status,
            binding_id=binding_id,
            event_id=event["id"],
            position_id=position_id,
            order_id=event["order_id"],
            instrument_id=_binding_instrument_id(binding["symbol"]),
            entry_leg_id=(entry_leg_row["id"] if entry_leg_row is not None else None),
            mutation_ids=tuple(row["id"] for row in mutation_rows),
        )
    return evidence, raw_capability


def _binding_row_has_exact_identity(
    row: sqlite3.Row,
    *,
    binding_id: object,
    position_id: object,
) -> bool:
    # A binding may own split-position siblings, and its denormalized pos_id can
    # therefore contain only the currently live subset. Exact position ownership
    # comes from the reservation/event/entry-leg chain, not this summary column.
    del position_id
    return (
        type(row["id"]) is int
        and row["id"] == binding_id
        and row["venue"] == "deepcoin"
    )


def _binding_row_shape_is_complete(row: sqlite3.Row) -> bool:
    return (
        _required_text(row["strategy_instance_id"])
        and _required_text(row["symbol"])
        and row["side"] in {"long", "short"}
        and row["status"] in _EXECUTION_BINDING_STATUSES
        and _valid_json(row["payload_json"], required=False)
        and _valid_time_pair(row["created_at"], row["updated_at"])
    )


def _event_row_has_exact_identity(
    row: sqlite3.Row,
    *,
    binding: sqlite3.Row,
    binding_id: object,
    position_id: object,
) -> bool:
    return (
        row["execution_binding_id"] == binding_id
        and row["strategy_instance_id"] == binding["strategy_instance_id"]
        and row["venue"] == "deepcoin"
        and row["action"] == "close_bound_position_market"
        and row["symbol"] == binding["symbol"]
        and row["side"] == binding["side"]
        and row["pos_id"] == position_id
    )


def _event_row_shape_is_complete(row: sqlite3.Row) -> bool:
    return (
        type(row["id"]) is int
        and row["id"] > 0
        and row["status"] in _BOUND_CLOSE_EVENT_STATUSES
        and _required_text(row["order_id"])
        and all(
            _valid_json(row[field], required=False)
            for field in (
                "before_json",
                "after_json",
                "request_json",
                "response_json",
            )
        )
        and _parse_sqlite_utc(row["created_at"]) is not None
    )


def _entry_leg_row_has_exact_identity(
    row: sqlite3.Row,
    *,
    binding: sqlite3.Row,
    binding_id: object,
    position_id: object,
) -> bool:
    return (
        type(row["id"]) is int
        and row["id"] > 0
        and row["execution_binding_id"] == binding_id
        and row["strategy_instance_id"] == binding["strategy_instance_id"]
        and row["purpose"] == "entry"
        and row["pos_id"] == position_id
        and row["venue"] == "deepcoin"
    )


def _mutation_row_has_exact_identity(
    row: sqlite3.Row,
    *,
    binding: sqlite3.Row,
    binding_id: object,
    position_id: object,
    entry_leg_id: object,
) -> bool:
    return (
        type(row["id"]) is int
        and row["id"] > 0
        and row["operation"] == "close_position"
        and row["strategy_instance_id"] == binding["strategy_instance_id"]
        and row["execution_binding_id"] == binding_id
        and row["execution_order_leg_id"] == entry_leg_id
        and row["pos_id"] == position_id
        and row["venue"] == "deepcoin"
    )


def _entry_leg_evidence(
    row: sqlite3.Row,
) -> LocalOwnedEntryLegEvidence | None:
    if not _entry_leg_row_shape_is_complete(row):
        return None
    fingerprint = _safe_row_fingerprint("entry_leg", row)
    if fingerprint is None:
        return None
    return LocalOwnedEntryLegEvidence(
        leg_ref=_redacted_ref("entry_leg", row["id"]),
        status=row["status"],
        attribution_status=row["attribution_status"],
        order_ref=(
            _redacted_ref("entry_order", row["order_id"])
            if _required_text(row["order_id"])
            else None
        ),
        last_verified_at=_parse_sqlite_utc(row["last_verified_at"]),
        created_at=_parse_sqlite_utc(row["created_at"]),
        updated_at=_parse_sqlite_utc(row["updated_at"]),
        record_fingerprint=fingerprint,
    )


def _entry_leg_row_shape_is_complete(row: sqlite3.Row) -> bool:
    return (
        type(row["leg_index"]) is int
        and row["leg_index"] >= 0
        and _required_text(row["order_kind"])
        and row["status"] in _ENTRY_LEG_STATUSES
        and row["attribution_status"] in _ENTRY_LEG_ATTRIBUTION_STATUSES
        and _optional_text(row["order_id"])
        and _optional_text(row["client_order_id"])
        and _optional_text(row["terminal_reason"])
        and _optional_utc(row["last_verified_at"])
        and _valid_time_pair(row["created_at"], row["updated_at"])
        and all(
            _valid_json(row[field], required=False)
            for field in (
                "attribution_evidence_json",
                "request_json",
                "response_json",
            )
        )
    )


def _mutation_evidence(
    row: sqlite3.Row,
) -> LocalCloseMutationEvidence | None:
    reserved_at = _parse_sqlite_utc(row["reserved_at"])
    submitted_at = _parse_sqlite_utc(row["submitted_at"])
    confirmed_at = _parse_sqlite_utc(row["confirmed_at"])
    created_at = _parse_sqlite_utc(row["created_at"])
    updated_at = _parse_sqlite_utc(row["updated_at"])
    if (
        not _required_text(row["idempotency_key"])
        or not _is_lower_hex_64(row["authority_fingerprint"])
        or not _is_lower_hex_64(row["request_fingerprint"])
        or row["status"] not in _POSITION_MUTATION_STATUSES
        or not _optional_text(row["order_id"])
        or not _valid_json(row["request_json"], required=True)
        or not _valid_json(row["response_json"], required=False)
        or not _valid_json(row["error_json"], required=False)
        or reserved_at is None
        or created_at is None
        or updated_at is None
        or created_at > updated_at
        or (row["submitted_at"] is not None and submitted_at is None)
        or (row["confirmed_at"] is not None and confirmed_at is None)
        or (submitted_at is not None and submitted_at < reserved_at)
        or (confirmed_at is not None and confirmed_at < reserved_at)
    ):
        return None
    fingerprint = _safe_row_fingerprint("close_mutation", row)
    if fingerprint is None:
        return None
    evidence = LocalCloseMutationEvidence(
        mutation_ref=_redacted_ref("close_mutation", row["id"]),
        status=row["status"],
        order_ref=(
            _redacted_ref("close_order", row["order_id"])
            if _required_text(row["order_id"])
            else None
        ),
        reserved_at=reserved_at,
        submitted_at=submitted_at,
        confirmed_at=confirmed_at,
        created_at=created_at,
        updated_at=updated_at,
        record_fingerprint=fingerprint,
    )
    if not _local_mutation_shape_is_valid(
        evidence,
        capture_completed_at=None,
    ):
        return None
    return evidence


def _source_failure(status: str) -> BoundCloseReservationSource:
    evidence = LocalReservationEvidence(
        reservation_ref=_redacted_ref("source_failure", status),
        source_status=status,
        local_reason_code="local_evidence_incomplete",
        reservation_created_at=None,
        reservation_updated_at=None,
        reservation_record_fingerprint=_redacted_ref("source_failure_record", status),
        binding_ref=None,
        binding_status=None,
        instrument_ref=None,
        side=None,
        binding_record_fingerprint=None,
        position_ref=None,
        close_event_ref=None,
        close_order_ref=None,
        event_created_at=None,
        close_event_record_fingerprint=None,
        entry_leg=None,
        close_mutations=(),
    )
    return _make_source((evidence,), {})


def _make_source(
    reservations: tuple[LocalReservationEvidence, ...],
    raw_capabilities: Mapping[str, _RawReservationCapability],
) -> BoundCloseReservationSource:
    return BoundCloseReservationSource(
        reservations=reservations,
        source_fingerprint=_sha256_json({"reservations": reservations}),
        _capability=_BoundCloseReservationSourceCapability(raw_capabilities),
    )


def _redacted_ref(kind: str, raw_value: object) -> str:
    return _sha256_json({"kind": kind, "value": raw_value})


def _binding_instrument_id(symbol: str) -> str:
    normalized = symbol.upper().replace("_", "-")
    if normalized.endswith("-SWAP"):
        return normalized
    if normalized.endswith("-USDT"):
        return f"{normalized}-SWAP"
    if normalized.endswith("USDT"):
        return f"{normalized[:-4]}-USDT-SWAP"
    return f"{normalized}-USDT-SWAP"


def _required_text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _is_lower_hex_64(value: object) -> bool:
    return type(value) is str and _LOWER_HEX_64.fullmatch(value) is not None


def _optional_text(value: object) -> bool:
    return value is None or type(value) is str


def _optional_utc(value: object) -> bool:
    return value is None or _parse_sqlite_utc(value) is not None


def _valid_time_pair(created: object, updated: object) -> bool:
    created_at = _parse_sqlite_utc(created)
    updated_at = _parse_sqlite_utc(updated)
    return (
        created_at is not None
        and updated_at is not None
        and created_at <= updated_at
    )


def _parse_sqlite_utc(value: object) -> datetime | None:
    if type(value) is not str or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    if parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(UTC)


def _valid_json(value: object, *, required: bool) -> bool:
    if value is None:
        return not required
    if type(value) is not str or (required and not value.strip()):
        return False
    if len(value.encode("utf-8")) > _MAX_LOCAL_JSON_BYTES:
        return False
    try:
        json.loads(
            value,
            object_pairs_hook=_closed_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, RecursionError, OverflowError):
        return False
    return True


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _safe_row_fingerprint(kind: str, row: sqlite3.Row) -> str | None:
    values: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if value is not None and type(value) not in {str, int, bool}:
            return None
        values[key] = value
    try:
        return _sha256_json({"kind": kind, "row": values})
    except (TypeError, ValueError, OverflowError):
        return None
