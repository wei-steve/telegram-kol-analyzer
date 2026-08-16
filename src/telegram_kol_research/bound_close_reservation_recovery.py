"""Closed contract for bound-position close-reservation recovery."""

from __future__ import annotations

from collections import OrderedDict
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
import weakref

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
BOUND_CLOSE_RESERVATION_APPLY_AUTHORIZATION = (
    "I_APPROVE_BOUND_CLOSE_RESERVATIONS_ALL_DB_UNITS_STOPPED_APPLY_CAPTURE"
)
BOUND_CLOSE_RESERVATION_TERMINAL_ONLY_AUTHORIZATION = (
    "I_AUTHORIZE_BOUND_CLOSE_RESERVATIONS_PROVEN_TERMINAL_ONLY"
)
BOUND_CLOSE_RESERVATION_CANONICAL_APPLY_AUTHORIZATION = (
    BOUND_CLOSE_RESERVATION_APPLY_AUTHORIZATION
    + "\n"
    + BOUND_CLOSE_RESERVATION_TERMINAL_ONLY_AUTHORIZATION
)
_BOUND_CLOSE_RESERVATION_AUDIT_ACTION = (
    "bound_close_reservation_history_converged"
)


class BoundCloseReservationExchangeConfigurationError(RuntimeError):
    """The recovery reader cannot establish its closed safety boundary."""


class BoundCloseReservationExchangeDeadlineExceeded(RuntimeError):
    """The one absolute recovery capture deadline expired."""


class BoundCloseReservationRecoveryConflict(RuntimeError):
    """The separately gated apply could not prove its exact CAS boundary."""


class _SealedRecoveryCaptureExpired(ValueError):
    """A capture missed its issuer-bound absolute monotonic deadline."""


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
        "__sealed",
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
        self.__sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_RawReservationCapability__sealed", False):
            raise AttributeError("raw reservation authority is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("raw reservation authority is immutable")


class _BoundCloseReservationSourceCapability(_OpaqueUncopyableCapability):
    __slots__ = ("__raw_by_reservation_ref",)

    def __init__(self, raw_by_reservation_ref: Mapping[str, _RawReservationCapability]):
        self.__raw_by_reservation_ref = dict(raw_by_reservation_ref)

    def _get(self, reservation_ref: str) -> _RawReservationCapability:
        return self.__raw_by_reservation_ref[reservation_ref]


@dataclass(frozen=True, slots=True, weakref_slot=True)
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


@dataclass(frozen=True, slots=True)
class _DatabaseFileIdentity:
    resolved_path: str
    device: int
    inode: int


class _BoundCloseReservationSourceIssuance:
    __slots__ = (
        "capability",
        "database_identity",
        "raw_source_snapshot_fingerprint",
        "reservations",
        "reservations_snapshot_fingerprint",
        "source_fingerprint",
        "source_ref",
    )

    def __init__(
        self,
        *,
        source_ref: weakref.ReferenceType[BoundCloseReservationSource],
        source: BoundCloseReservationSource,
        database_identity: _DatabaseFileIdentity,
    ) -> None:
        self.source_ref = source_ref
        self.reservations = source.reservations
        self.reservations_snapshot_fingerprint = _sha256_json(
            source.reservations
        )
        self.source_fingerprint = source.source_fingerprint
        self.capability = source._capability
        self.raw_source_snapshot_fingerprint = (
            _raw_source_snapshot_fingerprint(source._capability)
        )
        self.database_identity = database_identity


_SOURCE_ISSUANCE_LOCK = threading.RLock()
_MAX_SOURCE_ISSUANCE = MAX_RESERVATION_OBSERVATIONS * 8
_SOURCE_ISSUANCE_REGISTRY: OrderedDict[
    int,
    _BoundCloseReservationSourceIssuance,
] = OrderedDict()


def _database_file_identity(path: Path) -> _DatabaseFileIdentity:
    try:
        resolved = path.expanduser().resolve(strict=True)
        stat_result = resolved.stat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _apply_refusal("database_authority_invalid") from exc
    if not resolved.is_file():
        raise _apply_refusal("database_authority_invalid")
    return _DatabaseFileIdentity(
        resolved_path=str(resolved),
        device=int(stat_result.st_dev),
        inode=int(stat_result.st_ino),
    )


def _register_bound_close_reservation_source(
    source: BoundCloseReservationSource,
    *,
    database_identity: _DatabaseFileIdentity,
) -> BoundCloseReservationSource:
    source_identity = id(source)

    def discard(
        reference: weakref.ReferenceType[BoundCloseReservationSource],
        *,
        expected_identity: int = source_identity,
    ) -> None:
        with _SOURCE_ISSUANCE_LOCK:
            current = _SOURCE_ISSUANCE_REGISTRY.get(expected_identity)
            if current is not None and current.source_ref is reference:
                _SOURCE_ISSUANCE_REGISTRY.pop(expected_identity, None)

    reference = weakref.ref(source, discard)
    issuance = _BoundCloseReservationSourceIssuance(
        source_ref=reference,
        source=source,
        database_identity=database_identity,
    )
    with _SOURCE_ISSUANCE_LOCK:
        _SOURCE_ISSUANCE_REGISTRY[source_identity] = issuance
        _SOURCE_ISSUANCE_REGISTRY.move_to_end(source_identity)
        while len(_SOURCE_ISSUANCE_REGISTRY) > _MAX_SOURCE_ISSUANCE:
            _SOURCE_ISSUANCE_REGISTRY.popitem(last=False)
    return source


def _require_issued_bound_close_reservation_source(
    source: BoundCloseReservationSource,
) -> _DatabaseFileIdentity:
    if type(source) is not BoundCloseReservationSource:
        raise _apply_refusal("source_authority_invalid")
    with _SOURCE_ISSUANCE_LOCK:
        issued = _SOURCE_ISSUANCE_REGISTRY.get(id(source))
        if issued is None or issued.source_ref() is not source:
            raise _apply_refusal("source_authority_invalid")
        try:
            if (
                source.reservations is not issued.reservations
                or source._capability is not issued.capability
                or source.source_fingerprint != issued.source_fingerprint
                or _sha256_json(source.reservations)
                != issued.reservations_snapshot_fingerprint
                or _raw_source_snapshot_fingerprint(source._capability)
                != issued.raw_source_snapshot_fingerprint
            ):
                raise _apply_refusal("source_authority_invalid")
        except (AttributeError, RecursionError, TypeError, ValueError) as exc:
            raise _apply_refusal("source_authority_invalid") from exc
        return issued.database_identity


def _raw_source_snapshot_fingerprint(
    source_capability: _BoundCloseReservationSourceCapability,
) -> str:
    """Hash every exact raw CAS identity without returning raw authority bytes."""

    try:
        if type(source_capability) is not _BoundCloseReservationSourceCapability:
            raise TypeError("source capability type is invalid")
        raw_by_ref = (
            source_capability
            ._BoundCloseReservationSourceCapability__raw_by_reservation_ref
        )
        if type(raw_by_ref) is not dict or len(raw_by_ref) > (
            MAX_RESERVATION_OBSERVATIONS
        ):
            raise ValueError("raw source population is invalid")
        if any(type(reference) is not str for reference in raw_by_ref):
            raise TypeError("raw source reference is invalid")
        rows: list[dict[str, object]] = []
        for reservation_ref in sorted(raw_by_ref):
            _require_lower_hex_64(reservation_ref, "reservation_ref")
            raw = raw_by_ref[reservation_ref]
            if type(raw) is not _RawReservationCapability:
                raise TypeError("raw reservation authority type is invalid")
            if getattr(raw, "_RawReservationCapability__sealed", None) is not True:
                raise ValueError("raw reservation authority seal is invalid")
            positive_ids = (
                raw.reservation_id,
                raw.binding_id,
                raw.event_id,
            )
            if any(type(value) is not int or value <= 0 for value in positive_ids):
                raise ValueError("raw numeric authority is invalid")
            if raw.entry_leg_id is not None and (
                type(raw.entry_leg_id) is not int or raw.entry_leg_id <= 0
            ):
                raise ValueError("raw entry-leg authority is invalid")
            if (
                type(raw.mutation_ids) is not tuple
                or len(raw.mutation_ids) > _MAX_RECOVERY_PLAN_ITEMS
                or any(
                    type(value) is not int or value <= 0
                    for value in raw.mutation_ids
                )
                or len(set(raw.mutation_ids)) != len(raw.mutation_ids)
            ):
                raise ValueError("raw mutation authority is invalid")
            raw_texts = (
                raw.source_status,
                raw.position_id,
                raw.order_id,
                raw.instrument_id,
            )
            if any(
                type(value) is not str
                or not value.strip()
                or len(value.encode("utf-8")) > _MAX_RECOVERY_PLAN_STRING_BYTES
                for value in raw_texts
            ):
                raise ValueError("raw text authority is invalid")
            rows.append(
                {
                    "binding_id": raw.binding_id,
                    "entry_leg_id": raw.entry_leg_id,
                    "event_id": raw.event_id,
                    "instrument_id": raw.instrument_id,
                    "mutation_ids": list(raw.mutation_ids),
                    "order_id": raw.order_id,
                    "position_id": raw.position_id,
                    "reservation_id": raw.reservation_id,
                    "reservation_ref": reservation_ref,
                    "source_status": raw.source_status,
                }
            )
        return _sha256_json({"raw_reservations": rows})
    except (AttributeError, RecursionError, TypeError, ValueError) as exc:
        raise ValueError("raw source authority is invalid") from exc


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


@dataclass(frozen=True, slots=True)
class BoundCloseReservationRecoveryResult:
    status: str
    evidence_fingerprint: str
    action_count: int
    audit_event_id: int

    def __post_init__(self) -> None:
        if self.status not in {
            "applied",
            "applied_after_deadline_verified",
            "already_applied",
        }:
            raise ValueError("recovery result status is invalid")
        _require_lower_hex_64(
            self.evidence_fingerprint,
            "evidence_fingerprint",
        )
        if type(self.action_count) is not int or self.action_count <= 0:
            raise ValueError("action_count must be a positive integer")
        if type(self.audit_event_id) is not int or self.audit_event_id <= 0:
            raise ValueError("audit_event_id must be a positive integer")


def serialize_bound_close_reservation_recovery_result(
    result: BoundCloseReservationRecoveryResult,
) -> str:
    """Serialize the closed, redacted result returned by the dormant CLI."""

    if type(result) is not BoundCloseReservationRecoveryResult:
        raise TypeError("result must be BoundCloseReservationRecoveryResult")
    document = _canonical_json(
        {
            "action_count": result.action_count,
            "audit_event_id": result.audit_event_id,
            "evidence_fingerprint": result.evidence_fingerprint,
            "mode": "apply",
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "status": result.status,
        }
    )
    if len(document.encode("utf-8")) > MAX_RECOVERY_PLAN_BYTES:
        raise ValueError("serialized recovery result exceeds its byte bound")
    return document


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
        "__database_identity",
        "__deadline_monotonic",
        "__plan",
        "__serialized_plan",
        "__source_capability",
        "__weakref__",
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


_MAX_SEALED_CAPTURE_ISSUANCE = MAX_RESERVATION_OBSERVATIONS * 4
_SEALED_CAPTURE_ISSUANCE_LOCK = threading.RLock()


class _SealedCaptureIssuance:
    __slots__ = (
        "capture_ref",
        "database_identity",
        "deadline_monotonic",
        "plan",
        "plan_snapshot_fingerprint",
        "raw_source_snapshot_fingerprint",
        "serialized_plan",
        "source_capability",
    )

    def __init__(
        self,
        *,
        capture_ref: weakref.ReferenceType[SealedRecoveryCapture],
        database_identity: _DatabaseFileIdentity,
        deadline_monotonic: float,
        plan: BoundCloseReservationRecoveryPlan,
        plan_snapshot_fingerprint: str,
        raw_source_snapshot_fingerprint: str,
        serialized_plan: str,
        source_capability: _BoundCloseReservationSourceCapability,
    ) -> None:
        self.capture_ref = capture_ref
        self.database_identity = database_identity
        self.deadline_monotonic = deadline_monotonic
        self.plan = plan
        self.plan_snapshot_fingerprint = plan_snapshot_fingerprint
        self.raw_source_snapshot_fingerprint = raw_source_snapshot_fingerprint
        self.serialized_plan = serialized_plan
        self.source_capability = source_capability


_SEALED_CAPTURE_ISSUANCE_REGISTRY: OrderedDict[
    int,
    _SealedCaptureIssuance,
] = OrderedDict()


def _register_sealed_recovery_capture(capture: SealedRecoveryCapture) -> None:
    capture_identity = id(capture)

    def discard(
        reference: weakref.ReferenceType[SealedRecoveryCapture],
        *,
        expected_identity: int = capture_identity,
    ) -> None:
        with _SEALED_CAPTURE_ISSUANCE_LOCK:
            current = _SEALED_CAPTURE_ISSUANCE_REGISTRY.get(expected_identity)
            if current is not None and current.capture_ref is reference:
                _SEALED_CAPTURE_ISSUANCE_REGISTRY.pop(expected_identity, None)

    reference = weakref.ref(capture, discard)
    issuance = _SealedCaptureIssuance(
        capture_ref=reference,
        database_identity=_DatabaseFileIdentity(
            resolved_path=(
                capture._SealedRecoveryCapture__database_identity.resolved_path
            ),
            device=capture._SealedRecoveryCapture__database_identity.device,
            inode=capture._SealedRecoveryCapture__database_identity.inode,
        ),
        deadline_monotonic=_finite_recovery_monotonic(
            capture._SealedRecoveryCapture__deadline_monotonic
        ),
        plan=capture.plan,
        plan_snapshot_fingerprint=_sha256_json(capture.plan),
        raw_source_snapshot_fingerprint=_raw_source_snapshot_fingerprint(
            capture._SealedRecoveryCapture__source_capability
        ),
        serialized_plan=capture.serialized_plan,
        source_capability=capture._SealedRecoveryCapture__source_capability,
    )
    with _SEALED_CAPTURE_ISSUANCE_LOCK:
        _SEALED_CAPTURE_ISSUANCE_REGISTRY[capture_identity] = issuance
        _SEALED_CAPTURE_ISSUANCE_REGISTRY.move_to_end(capture_identity)
        while len(_SEALED_CAPTURE_ISSUANCE_REGISTRY) > (
            _MAX_SEALED_CAPTURE_ISSUANCE
        ):
            _SEALED_CAPTURE_ISSUANCE_REGISTRY.popitem(last=False)


class _ClaimedSealedRecoveryCapture(_OpaqueUncopyableCapability):
    __slots__ = (
        "__capture_completed_at",
        "__database_identity",
        "__deadline_monotonic",
        "__plan",
        "__source_capability",
    )

    def __init__(
        self,
        *,
        plan: BoundCloseReservationRecoveryPlan,
        source_capability: object,
        capture_completed_at: datetime,
        database_identity: _DatabaseFileIdentity,
        deadline_monotonic: float,
        _issuer: object,
    ) -> None:
        if _issuer is not _SEALED_APPLY_CLAIM_ISSUER:
            raise TypeError("claimed recovery capture has no private issuer")
        if type(source_capability) is not _BoundCloseReservationSourceCapability:
            raise TypeError("claimed recovery capture source capability is invalid")
        self.__plan = plan
        self.__source_capability = source_capability
        self.__capture_completed_at = _require_aware_utc(
            capture_completed_at,
            "capture_completed_at",
        )
        if type(database_identity) is not _DatabaseFileIdentity:
            raise TypeError("claimed recovery database authority is invalid")
        self.__database_identity = database_identity
        self.__deadline_monotonic = _finite_recovery_monotonic(
            deadline_monotonic
        )

    @property
    def plan(self) -> BoundCloseReservationRecoveryPlan:
        return self.__plan

    @property
    def capture_completed_at(self) -> datetime:
        return self.__capture_completed_at

    @property
    def deadline_monotonic(self) -> float:
        return self.__deadline_monotonic

    @property
    def database_identity(self) -> _DatabaseFileIdentity:
        return self.__database_identity


def _claim_sealed_recovery_capture_for_apply(
    capture: SealedRecoveryCapture,
) -> _ClaimedSealedRecoveryCapture:
    """Consume the private apply token exactly once; never trust saved bytes."""

    if type(capture) is not SealedRecoveryCapture:
        raise TypeError("capture must be a privately issued SealedRecoveryCapture")
    with _SEALED_CAPTURE_ISSUANCE_LOCK:
        if getattr(capture, "_SealedRecoveryCapture__apply_claimed", False):
            raise ValueError("sealed recovery capture was already claimed")
        issued = _SEALED_CAPTURE_ISSUANCE_REGISTRY.get(id(capture))
        if issued is None or issued.capture_ref() is not capture:
            raise ValueError("capture must be a privately issued SealedRecoveryCapture")
        if (
            getattr(capture, "_SealedRecoveryCapture__deadline_monotonic", None)
            != issued.deadline_monotonic
            or getattr(capture, "_SealedRecoveryCapture__database_identity", None)
            != issued.database_identity
        ):
            raise ValueError("sealed recovery capture issued contents changed")
        if time.monotonic() >= issued.deadline_monotonic:
            raise _SealedRecoveryCaptureExpired(
                "sealed recovery capture deadline expired"
            )
        if (
            getattr(capture, "_SealedRecoveryCapture__plan", None)
            is not issued.plan
            or getattr(capture, "_SealedRecoveryCapture__serialized_plan", None)
            is not issued.serialized_plan
            or getattr(capture, "_SealedRecoveryCapture__source_capability", None)
            is not issued.source_capability
        ):
            raise ValueError("sealed recovery capture issued contents changed")
        try:
            _require_canonical_recovery_plan(issued.plan)
            current_plan_fingerprint = _sha256_json(issued.plan)
        except (RecursionError, TypeError, ValueError) as exc:
            raise ValueError(
                "sealed recovery capture issued contents changed"
            ) from exc
        if current_plan_fingerprint != issued.plan_snapshot_fingerprint:
            raise ValueError("sealed recovery capture issued contents changed")
        try:
            current_raw_source_fingerprint = _raw_source_snapshot_fingerprint(
                issued.source_capability
            )
        except ValueError as exc:
            raise ValueError(
                "sealed recovery capture raw source authority changed"
            ) from exc
        if (
            current_raw_source_fingerprint
            != issued.raw_source_snapshot_fingerprint
        ):
            raise ValueError(
                "sealed recovery capture raw source authority changed"
            )
        if issued.plan.status != "ready":
            raise ValueError("only a ready sealed recovery capture can be claimed")
        try:
            parsed_capture = _parse_bound_close_reservation_dry_run_document(
                issued.serialized_plan.encode("utf-8")
            )
        except (RecursionError, TypeError, UnicodeError, ValueError) as exc:
            raise ValueError(
                "sealed recovery capture issued contents changed"
            ) from exc
        if parsed_capture.plan != issued.plan:
            raise ValueError("sealed recovery capture issued contents changed")
        claimed = _ClaimedSealedRecoveryCapture(
            plan=issued.plan,
            source_capability=issued.source_capability,
            capture_completed_at=parsed_capture.capture_completed_at,
            database_identity=issued.database_identity,
            deadline_monotonic=issued.deadline_monotonic,
            _issuer=_SEALED_APPLY_CLAIM_ISSUER,
        )
        capture._SealedRecoveryCapture__apply_claimed = True
        _SEALED_CAPTURE_ISSUANCE_REGISTRY.pop(id(capture), None)
        return claimed


def _apply_refusal(reason_code: str) -> BoundCloseReservationRecoveryConflict:
    return BoundCloseReservationRecoveryConflict(reason_code)


def _require_bound_close_reservation_apply_arguments(
    *,
    plan: BoundCloseReservationRecoveryPlan,
    expected_fingerprint: str,
    expected_action_count: int,
    confirmation_token: str,
    authorization: str,
    applied_at: datetime,
) -> datetime:
    if type(authorization) is not str or authorization != (
        BOUND_CLOSE_RESERVATION_CANONICAL_APPLY_AUTHORIZATION
    ):
        raise _apply_refusal("authorization_invalid")
    try:
        _require_canonical_recovery_plan(plan)
    except (RecursionError, TypeError, ValueError, OverflowError) as exc:
        raise _apply_refusal("plan_not_actionable") from exc
    if plan.status != "ready" or plan.action_count <= 0:
        raise _apply_refusal("plan_not_actionable")
    if (
        type(expected_fingerprint) is not str
        or expected_fingerprint != plan.evidence_fingerprint
    ):
        raise _apply_refusal("evidence_fingerprint_mismatch")
    if (
        type(expected_action_count) is not int
        or expected_action_count != plan.action_count
    ):
        raise _apply_refusal("action_count_mismatch")
    if (
        type(confirmation_token) is not str
        or confirmation_token != plan.confirmation_token
    ):
        raise _apply_refusal("confirmation_token_mismatch")
    try:
        applied = _require_aware_utc(applied_at, "applied_at")
        internal_now = _require_aware_utc(datetime.now(UTC), "internal_now")
    except (TypeError, ValueError) as exc:
        raise _apply_refusal("applied_at_invalid") from exc
    if applied > internal_now:
        raise _apply_refusal("applied_at_in_future")
    return applied


def _claim_exact_bound_close_reservation_apply_capture(
    *,
    capture: SealedRecoveryCapture,
    plan: BoundCloseReservationRecoveryPlan,
    applied_at: datetime,
) -> _ClaimedSealedRecoveryCapture:
    try:
        unclaimed_plan = capture.plan
        parsed_capture = _parse_bound_close_reservation_dry_run_document(
            capture.serialized_plan.encode("utf-8")
        )
    except (
        AttributeError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        raise _apply_refusal("fresh_capture_capability_invalid") from exc
    if (
        parsed_capture.plan != unclaimed_plan
        or applied_at < parsed_capture.capture_completed_at
    ):
        raise _apply_refusal("applied_at_precedes_fresh_capture")
    if unclaimed_plan.source_fingerprint != plan.source_fingerprint:
        raise _apply_refusal("fresh_capture_source_drift")
    if (
        unclaimed_plan.exchange_snapshot_fingerprint
        != plan.exchange_snapshot_fingerprint
    ):
        raise _apply_refusal("fresh_capture_exchange_drift")
    if unclaimed_plan.evidence_fingerprint != plan.evidence_fingerprint:
        raise _apply_refusal("fresh_capture_evidence_drift")
    if unclaimed_plan != plan:
        raise _apply_refusal("fresh_capture_plan_drift")
    try:
        claimed = _claim_sealed_recovery_capture_for_apply(capture)
    except _SealedRecoveryCaptureExpired as exc:
        raise _apply_refusal("fresh_capture_expired") from exc
    except (AttributeError, RecursionError, TypeError, ValueError) as exc:
        raise _apply_refusal("fresh_capture_capability_invalid") from exc
    captured_plan = claimed.plan
    if captured_plan.source_fingerprint != plan.source_fingerprint:
        raise _apply_refusal("fresh_capture_source_drift")
    if (
        captured_plan.exchange_snapshot_fingerprint
        != plan.exchange_snapshot_fingerprint
    ):
        raise _apply_refusal("fresh_capture_exchange_drift")
    if captured_plan.evidence_fingerprint != plan.evidence_fingerprint:
        raise _apply_refusal("fresh_capture_evidence_drift")
    if captured_plan != plan:
        raise _apply_refusal("fresh_capture_plan_drift")
    return claimed


def _open_bound_close_reservation_writable_connection(
    database_path: str | Path,
) -> sqlite3.Connection:
    resolved = Path(database_path).expanduser().resolve()
    uri = f"file:{quote(str(resolved), safe='/')}?mode=rw"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except Exception:
        connection.close()
        raise


def _connection_matches_database_identity(
    connection: sqlite3.Connection,
    *,
    expected: _DatabaseFileIdentity,
) -> bool:
    """Recheck the named main database around open and lock acquisition."""

    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
        main = [row for row in rows if row[1] == "main"]
        if len(main) != 1 or not main[0][2]:
            return False
        return _database_file_identity(Path(main[0][2])) == expected
    except (
        BoundCloseReservationRecoveryConflict,
        OSError,
        RuntimeError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        return False


def _require_no_user_recovery_triggers(
    connection: sqlite3.Connection,
) -> None:
    """Fail closed instead of attempting to reason about trigger side effects."""

    for schema_table in ("sqlite_master", "sqlite_temp_master"):
        rows = connection.execute(
            f"SELECT name FROM {schema_table} "
            "WHERE type = 'trigger' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchall()
        if rows:
            raise _apply_refusal("unexpected_user_trigger")


def _bound_close_reservation_write_authorizer(
    action: int,
    table_name: str | None,
    column_name: str | None,
    database_name: str | None,
    trigger_name: str | None,
) -> int:
    if action == sqlite3.SQLITE_UPDATE:
        if (
            database_name == "main"
            and trigger_name is None
            and table_name == "bound_position_close_reservations"
            and column_name in {"status", "last_error", "updated_at"}
        ):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_INSERT:
        return (
            sqlite3.SQLITE_OK
            if database_name == "main"
            and trigger_name is None
            and table_name == "execution_events"
            else sqlite3.SQLITE_DENY
        )
    if action in {
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_SELECT,
    }:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _execute_bound_close_reservation_apply_statement(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[object] = (),
) -> sqlite3.Cursor:
    """Single mutation seam used only for statement-boundary rollback tests."""

    return connection.execute(sql, tuple(parameters))


def _locked_mimo_contract_mode_is_v1(
    connection: sqlite3.Connection,
    *,
    schema_already_validated: bool = False,
) -> bool:
    try:
        if not schema_already_validated:
            columns = {
                row["name"]
                for row in connection.execute(
                    'PRAGMA table_info("trading_settings")'
                ).fetchall()
            }
            if not {"key", "value_json"}.issubset(columns):
                return False
        rows = connection.execute(
            "SELECT value_json FROM trading_settings WHERE key = 'global' LIMIT 2"
        ).fetchall()
        if len(rows) > 1:
            return False
        if not rows:
            payload: object = {}
        else:
            raw = rows[0]["value_json"]
            if type(raw) is not str or len(raw.encode("utf-8")) > (
                _MAX_LOCAL_JSON_BYTES
            ):
                return False
            payload = json.loads(
                raw,
                object_pairs_hook=_closed_json_object,
                parse_constant=_reject_json_constant,
            )
        if type(payload) is not dict:
            return False
        mode = payload.get("mimo_contract_mode", "v1")
        return type(mode) is str and mode == "v1"
    except (
        KeyError,
        RecursionError,
        sqlite3.Error,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        return False


def _locked_active_bound_close_reservation_source(
    connection: sqlite3.Connection,
    *,
    schema_already_validated: bool = False,
) -> BoundCloseReservationSource:
    if not schema_already_validated and not _source_schema_is_valid(connection):
        raise _apply_refusal("source_schema_invalid")
    rows = connection.execute(
        """
        SELECT id, pos_id, execution_binding_id, status, last_error,
               created_at, updated_at
        FROM bound_position_close_reservations
        WHERE status IS NULL OR status != 'confirmed'
        ORDER BY id
        LIMIT 65
        """
    ).fetchall()
    if len(rows) > MAX_RESERVATION_OBSERVATIONS:
        raise _apply_refusal("source_population_overflow")
    return _load_source_descendants(connection, rows)


def _locked_plan_observations_match_source(
    *,
    plan: BoundCloseReservationRecoveryPlan,
    source: BoundCloseReservationSource,
) -> bool:
    """Bind every approved observation to the exact locked local fact."""

    if (
        type(plan) is not BoundCloseReservationRecoveryPlan
        or type(source) is not BoundCloseReservationSource
        or source.source_fingerprint != plan.source_fingerprint
        or source.source_fingerprint
        != _sha256_json({"reservations": source.reservations})
        or len(source.reservations) != len(plan.observations)
    ):
        return False
    local_by_ref = {item.reservation_ref: item for item in source.reservations}
    if len(local_by_ref) != len(source.reservations):
        return False
    for observation in plan.observations:
        local = local_by_ref.get(observation.reservation_ref)
        if (
            local is None
            or observation.classification
            is not ReservationClassification.PROVEN_TERMINAL
            or observation.reason_code != "exact_close_and_position_terminal"
            or observation.source_fingerprint != _sha256_json(local)
        ):
            return False
    return True


def _claimed_source_capability(
    claimed: _ClaimedSealedRecoveryCapture,
) -> _BoundCloseReservationSourceCapability:
    capability = claimed._ClaimedSealedRecoveryCapture__source_capability
    if type(capability) is not _BoundCloseReservationSourceCapability:
        raise _apply_refusal("fresh_capture_capability_invalid")
    return capability


def _planned_raw_reservations(
    *,
    claimed: _ClaimedSealedRecoveryCapture,
    plan: BoundCloseReservationRecoveryPlan,
) -> tuple[tuple[str, _RawReservationCapability], ...]:
    capability = _claimed_source_capability(claimed)
    raw_rows: list[tuple[str, _RawReservationCapability]] = []
    try:
        for observation in plan.observations:
            raw = capability._get(observation.reservation_ref)
            if type(raw) is not _RawReservationCapability:
                raise TypeError("raw recovery authority is invalid")
            raw_rows.append((observation.reservation_ref, raw))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise _apply_refusal("fresh_capture_capability_invalid") from exc
    if len(raw_rows) != plan.action_count:
        raise _apply_refusal("fresh_capture_capability_invalid")
    return tuple(raw_rows)


def _durable_row_payload(
    row: sqlite3.Row,
    *,
    excluded_columns: frozenset[str] = frozenset(),
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key in sorted(row.keys()):
        if key in excluded_columns:
            continue
        value = row[key]
        if value is not None and type(value) not in {str, int, bool}:
            raise _apply_refusal("durable_invariant_shape_invalid")
        payload[key] = value
    return payload


def _durable_invariant_fingerprints(
    connection: sqlite3.Connection,
    *,
    raw_rows: tuple[tuple[str, _RawReservationCapability], ...],
) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for reservation_ref, raw in raw_rows:
        reservation_rows = connection.execute(
            "SELECT * FROM bound_position_close_reservations WHERE id = ?",
            (raw.reservation_id,),
        ).fetchall()
        binding_rows = connection.execute(
            "SELECT * FROM execution_bindings WHERE id = ?",
            (raw.binding_id,),
        ).fetchall()
        event_rows = connection.execute(
            "SELECT * FROM execution_events WHERE id = ?",
            (raw.event_id,),
        ).fetchall()
        leg_rows = (
            []
            if raw.entry_leg_id is None
            else connection.execute(
                "SELECT * FROM execution_order_legs WHERE id = ?",
                (raw.entry_leg_id,),
            ).fetchall()
        )
        mutation_rows = _select_in(
            connection,
            "SELECT * FROM position_mutation_intents "
            "WHERE id IN ({placeholders}) ORDER BY id",
            raw.mutation_ids,
        )
        if (
            len(reservation_rows) != 1
            or len(binding_rows) != 1
            or len(event_rows) != 1
            or len(leg_rows) != (0 if raw.entry_leg_id is None else 1)
            or len(mutation_rows) != len(raw.mutation_ids)
        ):
            raise _apply_refusal("durable_invariant_shape_invalid")
        reservation = reservation_rows[0]
        if (
            reservation["pos_id"] != raw.position_id
            or reservation["execution_binding_id"] != raw.binding_id
        ):
            raise _apply_refusal("durable_invariant_shape_invalid")
        payload = {
            "binding": _durable_row_payload(binding_rows[0]),
            "close_event": _durable_row_payload(event_rows[0]),
            "entry_leg": (
                None
                if not leg_rows
                else _durable_row_payload(leg_rows[0])
            ),
            "mutations": [
                _durable_row_payload(row) for row in mutation_rows
            ],
            "reservation_immutable": _durable_row_payload(
                reservation,
                excluded_columns=frozenset(
                    {"status", "last_error", "updated_at"}
                ),
            ),
        }
        try:
            fingerprints[reservation_ref] = _sha256_json(
                {"durable_invariant": payload}
            )
        except (RecursionError, TypeError, ValueError, OverflowError) as exc:
            raise _apply_refusal("durable_invariant_shape_invalid") from exc
    return fingerprints


def _recovery_audit_payloads(
    *,
    plan: BoundCloseReservationRecoveryPlan,
    raw_rows: tuple[tuple[str, _RawReservationCapability], ...],
    durable_invariant_fingerprints: Mapping[str, str],
) -> tuple[str, str]:
    expected_refs = {reservation_ref for reservation_ref, _raw in raw_rows}
    if (
        type(durable_invariant_fingerprints) is not dict
        or set(durable_invariant_fingerprints) != expected_refs
    ):
        raise _apply_refusal("durable_invariant_binding_invalid")
    for fingerprint in durable_invariant_fingerprints.values():
        try:
            _require_lower_hex_64(
                fingerprint,
                "durable_invariant_fingerprint",
            )
        except (TypeError, ValueError) as exc:
            raise _apply_refusal("durable_invariant_binding_invalid") from exc
    before = {
        "action_count": plan.action_count,
        "evidence_fingerprint": plan.evidence_fingerprint,
        "reservations": [
            {
                "durable_invariant_fingerprint": (
                    durable_invariant_fingerprints[reservation_ref]
                ),
                "reservation_ref": reservation_ref,
                "status": raw.source_status,
            }
            for reservation_ref, raw in raw_rows
        ],
    }
    after = {
        "action_count": plan.action_count,
        "evidence_fingerprint": plan.evidence_fingerprint,
        "reservations": [
            {
                "durable_invariant_fingerprint": (
                    durable_invariant_fingerprints[reservation_ref]
                ),
                "reservation_ref": reservation_ref,
                "status": "confirmed",
            }
            for reservation_ref, _raw in raw_rows
        ],
        "status": "confirmed",
    }
    before_json = _canonical_json(before)
    after_json = _canonical_json(after)
    if any(
        len(value.encode("utf-8")) > MAX_RECOVERY_PLAN_BYTES
        for value in (before_json, after_json)
    ):
        raise _apply_refusal("audit_payload_exceeds_bound")
    return before_json, after_json


def _load_bound_close_reservation_audits(
    connection: sqlite3.Connection,
    *,
    schema_already_validated: bool = False,
) -> tuple[sqlite3.Row, ...]:
    if not schema_already_validated:
        columns = {
            row["name"]
            for row in connection.execute(
                'PRAGMA table_info("execution_events")'
            ).fetchall()
        }
        required = {
            "id",
            "execution_binding_id",
            "venue",
            "action",
            "status",
            "order_id",
            "client_order_id",
            "pos_id",
            "related_order_id",
            "before_json",
            "after_json",
            "request_json",
            "response_json",
            "notification_fingerprint",
            "notification_attempts",
            "created_at",
        }
        if not required.issubset(columns):
            raise _apply_refusal("audit_schema_invalid")
    rows = connection.execute(
        """
        SELECT id, execution_binding_id, venue, action, status, order_id,
               client_order_id, pos_id, related_order_id,
               before_json, after_json, request_json, response_json,
               notification_fingerprint, notification_attempts, created_at
        FROM execution_events
        WHERE action = ?
        ORDER BY id
        LIMIT 66
        """,
        (_BOUND_CLOSE_RESERVATION_AUDIT_ACTION,),
    ).fetchall()
    if len(rows) > MAX_RESERVATION_OBSERVATIONS:
        raise _apply_refusal("unexpected_audit_event")
    return tuple(rows)


def _exact_recovery_audit_matches(
    row: sqlite3.Row,
    *,
    plan: BoundCloseReservationRecoveryPlan,
    before_json: str,
    after_json: str,
) -> bool:
    return (
        type(row["id"]) is int
        and row["id"] > 0
        and row["execution_binding_id"] is None
        and row["venue"] == "deepcoin"
        and row["action"] == _BOUND_CLOSE_RESERVATION_AUDIT_ACTION
        and row["status"] == "succeeded"
        and row["order_id"] is None
        and row["client_order_id"] is None
        and row["pos_id"] is None
        and row["related_order_id"] is None
        and row["before_json"] == before_json
        and row["after_json"] == after_json
        and row["request_json"] is None
        and row["response_json"] is None
        and row["notification_fingerprint"] == plan.evidence_fingerprint
        and row["notification_attempts"] == 0
        and _parse_sqlite_utc(row["created_at"]) is not None
    )


def _planned_rows_are_exactly_confirmed(
    connection: sqlite3.Connection,
    *,
    raw_rows: tuple[tuple[str, _RawReservationCapability], ...],
    audit_created_at: object,
) -> bool:
    planned_ids = tuple(raw.reservation_id for _ref, raw in raw_rows)
    rows = _select_in(
        connection,
        """
        SELECT id, pos_id, execution_binding_id, status, last_error, updated_at
        FROM bound_position_close_reservations
        WHERE id IN ({placeholders})
        ORDER BY id
        """,
        planned_ids,
    )
    if len(rows) != len(planned_ids):
        return False
    by_id = {row["id"]: row for row in rows}
    audit_time = _parse_sqlite_utc(audit_created_at)
    if audit_time is None:
        return False
    for _reservation_ref, raw in raw_rows:
        row = by_id.get(raw.reservation_id)
        if (
            row is None
            or row["pos_id"] != raw.position_id
            or row["execution_binding_id"] != raw.binding_id
            or row["status"] != "confirmed"
            or row["last_error"] is not None
            or _parse_sqlite_utc(row["updated_at"]) != audit_time
        ):
            return False
    return True


def _sqlite_apply_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(
        sep=" ",
        timespec="microseconds",
    )


def _require_claimed_apply_deadline(
    claimed: _ClaimedSealedRecoveryCapture,
) -> None:
    if time.monotonic() >= claimed.deadline_monotonic:
        raise _apply_refusal("fresh_capture_expired_during_apply")


def _commit_bound_close_reservation_apply(
    connection: sqlite3.Connection,
) -> None:
    connection.commit()


def _close_recovery_connection_safely(connection: object) -> None:
    try:
        connection.close()
    except Exception:
        pass


def _verify_committed_bound_close_reservation_apply_read_only(
    database_path: Path,
    *,
    expected_database_identity: _DatabaseFileIdentity,
    plan: BoundCloseReservationRecoveryPlan,
    raw_rows: tuple[tuple[str, _RawReservationCapability], ...],
    before_json: str,
    after_json: str,
) -> int | None:
    uri = f"file:{quote(str(database_path), safe='/')}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        if _database_file_identity(database_path) != expected_database_identity:
            return None
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            return None
        connection.execute("BEGIN")
        if not _connection_matches_database_identity(
            connection,
            expected=expected_database_identity,
        ):
            return None
        if not _locked_mimo_contract_mode_is_v1(connection):
            return None
        audits = _load_bound_close_reservation_audits(connection)
        matching = tuple(
            row
            for row in audits
            if row["notification_fingerprint"] == plan.evidence_fingerprint
        )
        if len(matching) != 1:
            return None
        audited_statuses, audited_invariants = (
            _parse_applied_recovery_audit_statuses(
                plan=plan,
                before_json=matching[0]["before_json"],
                after_json=matching[0]["after_json"],
            )
        )
        expected_statuses = {
            reservation_ref: raw.source_status
            for reservation_ref, raw in raw_rows
        }
        if (
            audited_statuses != expected_statuses
            or _durable_invariant_fingerprints(
                connection,
                raw_rows=raw_rows,
            )
            != audited_invariants
        ):
            return None
        if (
            len(audits) != 1
            or not _exact_recovery_audit_matches(
                matching[0],
                plan=plan,
                before_json=before_json,
                after_json=after_json,
            )
            or not _planned_rows_are_exactly_confirmed(
                connection,
                raw_rows=raw_rows,
                audit_created_at=matching[0]["created_at"],
            )
            or _locked_active_bound_close_reservation_source(
                connection
            ).reservations
        ):
            return None
        return int(matching[0]["id"])
    except (
        BoundCloseReservationRecoveryConflict,
        OSError,
        RecursionError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        return None
    finally:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                _close_recovery_connection_safely(connection)


def apply_bound_close_reservation_recovery(
    database_path: str | Path,
    *,
    plan: BoundCloseReservationRecoveryPlan,
    capture: SealedRecoveryCapture,
    expected_fingerprint: str,
    expected_action_count: int,
    confirmation_token: str,
    authorization: str,
    applied_at: datetime,
) -> BoundCloseReservationRecoveryResult:
    """Atomically converge only a fresh capture's exact terminal population."""

    if not isinstance(database_path, (str, Path)):
        raise _apply_refusal("database_path_invalid")
    applied = _require_bound_close_reservation_apply_arguments(
        plan=plan,
        expected_fingerprint=expected_fingerprint,
        expected_action_count=expected_action_count,
        confirmation_token=confirmation_token,
        authorization=authorization,
        applied_at=applied_at,
    )
    expanded_path = Path(database_path).expanduser()
    if expanded_path.is_symlink() or not expanded_path.is_file():
        raise _apply_refusal("database_path_invalid")
    resolved_database_path = expanded_path.resolve()
    claimed = _claim_exact_bound_close_reservation_apply_capture(
        capture=capture,
        plan=plan,
        applied_at=applied,
    )
    _require_claimed_apply_deadline(claimed)
    if _database_file_identity(resolved_database_path) != (
        claimed.database_identity
    ):
        raise _apply_refusal("database_authority_mismatch")
    raw_rows = _planned_raw_reservations(claimed=claimed, plan=plan)
    _require_claimed_apply_deadline(claimed)

    connection: sqlite3.Connection | None = None
    authorizer_installed = False
    commit_exception: Exception | None = None
    commit_attempted = False
    commit_returned_after_deadline = False
    audit_event_id: int | None = None
    before_json = ""
    after_json = ""
    try:
        with _recovery_wall_clock_guard(
            deadline_monotonic=claimed.deadline_monotonic
        ):
            _require_claimed_apply_deadline(claimed)
            if _database_file_identity(resolved_database_path) != (
                claimed.database_identity
            ):
                raise _apply_refusal("database_authority_mismatch")
            connection = _open_bound_close_reservation_writable_connection(
                resolved_database_path
            )
            if _database_file_identity(resolved_database_path) != (
                claimed.database_identity
            ):
                raise _apply_refusal("database_authority_mismatch")
            _require_claimed_apply_deadline(claimed)
            connection.execute("BEGIN IMMEDIATE")
            _require_claimed_apply_deadline(claimed)
            if not _connection_matches_database_identity(
                connection,
                expected=claimed.database_identity,
            ):
                raise _apply_refusal("database_authority_mismatch")
            if not _locked_mimo_contract_mode_is_v1(connection):
                raise _apply_refusal("mimo_contract_mode_not_v1")
            _require_claimed_apply_deadline(claimed)
            audits = _load_bound_close_reservation_audits(connection)
            _require_claimed_apply_deadline(claimed)
            matching = tuple(
                row
                for row in audits
                if row["notification_fingerprint"] == plan.evidence_fingerprint
            )
            if matching:
                (
                    audited_statuses,
                    audited_invariant_fingerprints,
                ) = _parse_applied_recovery_audit_statuses(
                    plan=plan,
                    before_json=matching[0]["before_json"],
                    after_json=matching[0]["after_json"],
                )
                expected_statuses = {
                    reservation_ref: raw.source_status
                    for reservation_ref, raw in raw_rows
                }
                if audited_statuses != expected_statuses:
                    raise _apply_refusal("unexpected_audit_event")
                current_invariant_fingerprints = _durable_invariant_fingerprints(
                    connection,
                    raw_rows=raw_rows,
                )
                if current_invariant_fingerprints != (
                    audited_invariant_fingerprints
                ):
                    raise _apply_refusal("durable_invariant_conflict")
                before_json, after_json = _recovery_audit_payloads(
                    plan=plan,
                    raw_rows=raw_rows,
                    durable_invariant_fingerprints=(
                        audited_invariant_fingerprints
                    ),
                )
                _require_claimed_apply_deadline(claimed)
                audit_created_at = _parse_sqlite_utc(matching[0]["created_at"])
                if (
                    audit_created_at is None
                    or claimed.capture_completed_at <= audit_created_at
                ):
                    raise _apply_refusal(
                        "fresh_capture_does_not_postdate_audit"
                    )
                if (
                    len(audits) != 1
                    or len(matching) != 1
                    or not _exact_recovery_audit_matches(
                        matching[0],
                        plan=plan,
                        before_json=before_json,
                        after_json=after_json,
                    )
                    or not _planned_rows_are_exactly_confirmed(
                        connection,
                        raw_rows=raw_rows,
                        audit_created_at=matching[0]["created_at"],
                    )
                ):
                    raise _apply_refusal("unexpected_audit_event")
                active = _locked_active_bound_close_reservation_source(connection)
                _require_claimed_apply_deadline(claimed)
                if active.reservations:
                    raise _apply_refusal("source_state_conflict")
                connection.rollback()
                _close_recovery_connection_safely(connection)
                connection = None
                verified_event_id = (
                    _verify_committed_bound_close_reservation_apply_read_only(
                        resolved_database_path,
                        expected_database_identity=claimed.database_identity,
                        plan=plan,
                        raw_rows=raw_rows,
                        before_json=before_json,
                        after_json=after_json,
                    )
                )
                if verified_event_id != int(matching[0]["id"]):
                    raise _apply_refusal("database_authority_mismatch")
                return BoundCloseReservationRecoveryResult(
                    status="already_applied",
                    evidence_fingerprint=plan.evidence_fingerprint,
                    action_count=plan.action_count,
                    audit_event_id=int(matching[0]["id"]),
                )
            if audits:
                raise _apply_refusal("unexpected_audit_event")

            locked_source = _locked_active_bound_close_reservation_source(
                connection
            )
            _require_claimed_apply_deadline(claimed)
            if not _locked_plan_observations_match_source(
                plan=plan,
                source=locked_source,
            ):
                raise _apply_refusal("source_state_conflict")
            if _raw_source_snapshot_fingerprint(locked_source._capability) != (
                _raw_source_snapshot_fingerprint(
                    _claimed_source_capability(claimed)
                )
            ):
                raise _apply_refusal("source_state_conflict")
            _require_no_user_recovery_triggers(connection)
            _require_claimed_apply_deadline(claimed)
            durable_invariant_fingerprints = _durable_invariant_fingerprints(
                connection,
                raw_rows=raw_rows,
            )
            _require_claimed_apply_deadline(claimed)
            before_json, after_json = _recovery_audit_payloads(
                plan=plan,
                raw_rows=raw_rows,
                durable_invariant_fingerprints=durable_invariant_fingerprints,
            )

            updated_at = _sqlite_apply_time(applied)
            baseline_changes = connection.total_changes
            connection.set_authorizer(_bound_close_reservation_write_authorizer)
            authorizer_installed = True
            for _reservation_ref, raw in raw_rows:
                _require_claimed_apply_deadline(claimed)
                cursor = _execute_bound_close_reservation_apply_statement(
                    connection,
                    """
                    UPDATE bound_position_close_reservations
                    SET status = 'confirmed', last_error = NULL, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (updated_at, raw.reservation_id, raw.source_status),
                )
                if cursor.rowcount != 1:
                    raise _apply_refusal("reservation_cas_conflict")
                _require_claimed_apply_deadline(claimed)
            cursor = _execute_bound_close_reservation_apply_statement(
                connection,
                """
                INSERT INTO execution_events (
                    execution_binding_id, venue, action, status, order_id,
                    client_order_id, pos_id, related_order_id,
                    before_json, after_json, request_json, response_json,
                    notification_fingerprint, notification_attempts, created_at
                ) VALUES (NULL, 'deepcoin', ?, 'succeeded', NULL, NULL, NULL, NULL,
                          ?, ?, NULL, NULL, ?, 0, ?)
                """,
                (
                    _BOUND_CLOSE_RESERVATION_AUDIT_ACTION,
                    before_json,
                    after_json,
                    plan.evidence_fingerprint,
                    updated_at,
                ),
            )
            _require_claimed_apply_deadline(claimed)
            audit_event_id = cursor.lastrowid
            if type(audit_event_id) is not int or audit_event_id <= 0:
                raise _apply_refusal("audit_insert_failed")
            if connection.total_changes - baseline_changes != plan.action_count + 1:
                raise _apply_refusal("unexpected_transaction_changes")
            if _durable_invariant_fingerprints(
                connection,
                raw_rows=raw_rows,
            ) != durable_invariant_fingerprints:
                raise _apply_refusal("durable_invariant_conflict")
            postwrite_audits = _load_bound_close_reservation_audits(
                connection,
                schema_already_validated=True,
            )
            if (
                len(postwrite_audits) != 1
                or int(postwrite_audits[0]["id"]) != audit_event_id
                or not _exact_recovery_audit_matches(
                    postwrite_audits[0],
                    plan=plan,
                    before_json=before_json,
                    after_json=after_json,
                )
                or not _planned_rows_are_exactly_confirmed(
                    connection,
                    raw_rows=raw_rows,
                    audit_created_at=updated_at,
                )
                or _locked_active_bound_close_reservation_source(
                    connection,
                    schema_already_validated=True,
                ).reservations
                or not _locked_mimo_contract_mode_is_v1(
                    connection,
                    schema_already_validated=True,
                )
            ):
                raise _apply_refusal("postwrite_state_conflict")
            _require_claimed_apply_deadline(claimed)
            if _database_file_identity(resolved_database_path) != (
                claimed.database_identity
            ):
                raise _apply_refusal("database_authority_mismatch")
            if connection.total_changes - baseline_changes != plan.action_count + 1:
                raise _apply_refusal("unexpected_transaction_changes")
            if not connection.in_transaction:
                raise _apply_refusal("recovery_transaction_not_active")
            connection.set_authorizer(None)
            authorizer_installed = False
            commit_attempted = True
            try:
                _commit_bound_close_reservation_apply(connection)
                if _database_file_identity(resolved_database_path) != (
                    claimed.database_identity
                ):
                    raise _apply_refusal("database_authority_mismatch")
                commit_returned_after_deadline = (
                    time.monotonic() >= claimed.deadline_monotonic
                )
            except Exception as exc:
                commit_exception = exc

        if commit_exception is not None or commit_returned_after_deadline:
            if connection is not None and connection.in_transaction:
                try:
                    connection.rollback()
                except Exception:
                    pass
            if connection is not None:
                _close_recovery_connection_safely(connection)
                connection = None
            verified_event_id = (
                _verify_committed_bound_close_reservation_apply_read_only(
                    resolved_database_path,
                    expected_database_identity=claimed.database_identity,
                    plan=plan,
                    raw_rows=raw_rows,
                    before_json=before_json,
                    after_json=after_json,
                )
            )
            if verified_event_id == audit_event_id:
                late = commit_returned_after_deadline or (
                    isinstance(
                        commit_exception,
                        BoundCloseReservationExchangeDeadlineExceeded,
                    )
                    or time.monotonic() >= claimed.deadline_monotonic
                )
                return BoundCloseReservationRecoveryResult(
                    status=(
                        "applied_after_deadline_verified" if late else "applied"
                    ),
                    evidence_fingerprint=plan.evidence_fingerprint,
                    action_count=plan.action_count,
                    audit_event_id=verified_event_id,
                )
            raise _apply_refusal("apply_commit_outcome_unresolved") from (
                commit_exception
            )
        if audit_event_id is None:
            raise _apply_refusal("audit_insert_failed")
        if connection is not None:
            _close_recovery_connection_safely(connection)
            connection = None
        verified_event_id = (
            _verify_committed_bound_close_reservation_apply_read_only(
                resolved_database_path,
                expected_database_identity=claimed.database_identity,
                plan=plan,
                raw_rows=raw_rows,
                before_json=before_json,
                after_json=after_json,
            )
        )
        if verified_event_id != audit_event_id:
            raise _apply_refusal("apply_commit_outcome_unresolved")
        return BoundCloseReservationRecoveryResult(
            status="applied",
            evidence_fingerprint=plan.evidence_fingerprint,
            action_count=plan.action_count,
            audit_event_id=verified_event_id,
        )
    except BoundCloseReservationRecoveryConflict as exc:
        if connection is not None and authorizer_installed:
            try:
                connection.set_authorizer(None)
            except Exception:
                pass
        if commit_attempted:
            if connection is not None and connection.in_transaction:
                try:
                    connection.rollback()
                except Exception:
                    pass
            if connection is not None:
                _close_recovery_connection_safely(connection)
                connection = None
            verified_event_id = (
                _verify_committed_bound_close_reservation_apply_read_only(
                    resolved_database_path,
                    expected_database_identity=claimed.database_identity,
                    plan=plan,
                    raw_rows=raw_rows,
                    before_json=before_json,
                    after_json=after_json,
                )
            )
            if verified_event_id == audit_event_id:
                return BoundCloseReservationRecoveryResult(
                    status=(
                        "applied_after_deadline_verified"
                        if time.monotonic() >= claimed.deadline_monotonic
                        else "applied"
                    ),
                    evidence_fingerprint=plan.evidence_fingerprint,
                    action_count=plan.action_count,
                    audit_event_id=verified_event_id,
                )
            raise _apply_refusal("apply_commit_outcome_unresolved") from exc
        if connection is not None and connection.in_transaction:
            connection.rollback()
        raise
    except BoundCloseReservationExchangeDeadlineExceeded as exc:
        if connection is not None and authorizer_installed:
            try:
                connection.set_authorizer(None)
            except Exception:
                pass
        if commit_attempted:
            if connection is not None and connection.in_transaction:
                try:
                    connection.rollback()
                except Exception:
                    pass
            if connection is not None:
                _close_recovery_connection_safely(connection)
                connection = None
            verified_event_id = (
                _verify_committed_bound_close_reservation_apply_read_only(
                    resolved_database_path,
                    expected_database_identity=claimed.database_identity,
                    plan=plan,
                    raw_rows=raw_rows,
                    before_json=before_json,
                    after_json=after_json,
                )
            )
            if verified_event_id == audit_event_id:
                return BoundCloseReservationRecoveryResult(
                    status="applied_after_deadline_verified",
                    evidence_fingerprint=plan.evidence_fingerprint,
                    action_count=plan.action_count,
                    audit_event_id=verified_event_id,
                )
            raise _apply_refusal("apply_commit_outcome_unresolved") from exc
        if connection is not None and connection.in_transaction:
            connection.rollback()
        raise _apply_refusal("fresh_capture_expired_during_apply") from exc
    except Exception as exc:
        if connection is not None and authorizer_installed:
            try:
                connection.set_authorizer(None)
            except Exception:
                pass
        if commit_attempted:
            if connection is not None and connection.in_transaction:
                try:
                    connection.rollback()
                except Exception:
                    pass
            if connection is not None:
                _close_recovery_connection_safely(connection)
                connection = None
            verified_event_id = (
                _verify_committed_bound_close_reservation_apply_read_only(
                    resolved_database_path,
                    expected_database_identity=claimed.database_identity,
                    plan=plan,
                    raw_rows=raw_rows,
                    before_json=before_json,
                    after_json=after_json,
                )
            )
            if verified_event_id == audit_event_id:
                return BoundCloseReservationRecoveryResult(
                    status=(
                        "applied_after_deadline_verified"
                        if time.monotonic() >= claimed.deadline_monotonic
                        else "applied"
                    ),
                    evidence_fingerprint=plan.evidence_fingerprint,
                    action_count=plan.action_count,
                    audit_event_id=verified_event_id,
                )
            raise _apply_refusal("apply_commit_outcome_unresolved") from exc
        if connection is not None and connection.in_transaction:
            connection.rollback()
        raise _apply_refusal("apply_transaction_failed") from exc
    except BaseException as exc:
        if connection is not None and authorizer_installed:
            try:
                connection.set_authorizer(None)
            except Exception:
                pass
        if connection is not None and connection.in_transaction:
            try:
                connection.rollback()
            except Exception:
                pass
        if commit_attempted:
            if connection is not None:
                _close_recovery_connection_safely(connection)
                connection = None
            verified_event_id = (
                _verify_committed_bound_close_reservation_apply_read_only(
                    resolved_database_path,
                    expected_database_identity=claimed.database_identity,
                    plan=plan,
                    raw_rows=raw_rows,
                    before_json=before_json,
                    after_json=after_json,
                )
            )
            if verified_event_id == audit_event_id:
                exc.add_note(
                    "bound-close recovery commit was verified on the sealed "
                    "database path before this system interrupt was re-raised"
                )
            else:
                exc.add_note(
                    "bound-close recovery commit outcome could not be verified "
                    "on the sealed database path"
                )
        raise
    finally:
        if connection is not None:
            _close_recovery_connection_safely(connection)


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


def _capture_recovery_exchange_collections(
    *,
    raw_by_ref: Mapping[str, _RawReservationCapability],
    reader: BoundCloseReservationExchangeReader,
    deadline_monotonic: float,
) -> tuple[_RawRecoveryCapture, datetime, datetime]:
    started_at = _require_aware_utc(
        _recovery_capture_now(),
        "capture_started_at",
    )
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
    return capture, started_at, completed_at


def _issue_sealed_recovery_capture(
    *,
    plan: BoundCloseReservationRecoveryPlan,
    source_capability: _BoundCloseReservationSourceCapability,
    database_identity: _DatabaseFileIdentity,
    capture_started_at: datetime,
    capture_completed_at: datetime,
    deadline_monotonic: float,
) -> SealedRecoveryCapture:
    serialized = serialize_bound_close_reservation_recovery_plan(
        plan,
        capture_started_at=capture_started_at,
        capture_completed_at=capture_completed_at,
    )
    sealed = object.__new__(SealedRecoveryCapture)
    sealed._SealedRecoveryCapture__apply_claimed = False
    sealed._SealedRecoveryCapture__apply_claim_lock = threading.Lock()
    sealed._SealedRecoveryCapture__database_identity = database_identity
    sealed._SealedRecoveryCapture__deadline_monotonic = (
        _finite_recovery_monotonic(deadline_monotonic)
    )
    sealed._SealedRecoveryCapture__plan = plan
    sealed._SealedRecoveryCapture__serialized_plan = serialized
    sealed._SealedRecoveryCapture__source_capability = source_capability
    _register_sealed_recovery_capture(sealed)
    return sealed


def capture_and_seal_bound_close_reservation_recovery(
    source: BoundCloseReservationSource,
    reader: BoundCloseReservationExchangeReader,
    *,
    deadline_monotonic: float,
) -> SealedRecoveryCapture:
    """Perform one fresh bounded GET capture and issue the only apply capability."""

    database_identity = _require_issued_bound_close_reservation_source(source)
    if type(reader) is not BoundCloseReservationExchangeReader:
        raise TypeError("reader must be the dedicated recovery reader")
    raw_by_ref: dict[str, _RawReservationCapability] = {}
    for local in source.reservations:
        try:
            raw = source._capability._get(local.reservation_ref)
        except KeyError:
            continue
        if type(raw) is _RawReservationCapability:
            raw_by_ref[local.reservation_ref] = raw
    capture, started_at, completed_at = _capture_recovery_exchange_collections(
        raw_by_ref=raw_by_ref,
        reader=reader,
        deadline_monotonic=deadline_monotonic,
    )
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
    return _issue_sealed_recovery_capture(
        plan=plan,
        source_capability=source._capability,
        database_identity=database_identity,
        capture_started_at=started_at,
        capture_completed_at=completed_at,
        deadline_monotonic=deadline_monotonic,
    )


def _parse_applied_recovery_audit_statuses(
    *,
    plan: BoundCloseReservationRecoveryPlan,
    before_json: object,
    after_json: object,
) -> tuple[dict[str, str], dict[str, str]]:
    expected_top_before = frozenset(
        {"action_count", "evidence_fingerprint", "reservations"}
    )
    expected_top_after = frozenset(
        {"action_count", "evidence_fingerprint", "reservations", "status"}
    )
    expected_item = frozenset(
        {"durable_invariant_fingerprint", "reservation_ref", "status"}
    )
    try:
        if type(before_json) is not str or type(after_json) is not str:
            raise TypeError("audit JSON must be text")
        if any(
            len(value.encode("utf-8")) > MAX_RECOVERY_PLAN_BYTES
            for value in (before_json, after_json)
        ):
            raise ValueError("audit JSON exceeds its bound")
        before = json.loads(
            before_json,
            object_pairs_hook=_closed_json_object,
            parse_constant=_reject_json_constant,
        )
        after = json.loads(
            after_json,
            object_pairs_hook=_closed_json_object,
            parse_constant=_reject_json_constant,
        )
        before = _exact_object_keys(before, expected_top_before, "audit.before")
        after = _exact_object_keys(after, expected_top_after, "audit.after")
        if (
            before["action_count"] != plan.action_count
            or after["action_count"] != plan.action_count
            or before["evidence_fingerprint"] != plan.evidence_fingerprint
            or after["evidence_fingerprint"] != plan.evidence_fingerprint
            or after["status"] != "confirmed"
            or type(before["reservations"]) is not list
            or type(after["reservations"]) is not list
            or len(before["reservations"]) != plan.action_count
            or len(after["reservations"]) != plan.action_count
        ):
            raise ValueError("audit payload does not match the approved plan")
        approved_refs = tuple(item.reservation_ref for item in plan.observations)
        statuses: dict[str, str] = {}
        invariant_fingerprints: dict[str, str] = {}
        after_refs: list[str] = []
        for raw_item in before["reservations"]:
            item = _exact_object_keys(raw_item, expected_item, "audit.before.item")
            reference = _require_lower_hex_64(
                item["reservation_ref"],
                "reservation_ref",
            )
            status = item["status"]
            if status not in ACTIVE_CLOSE_RESERVATION_STATUSES:
                raise ValueError("audit source status is invalid")
            if reference in statuses:
                raise ValueError("audit reservation reference is duplicated")
            statuses[reference] = status
            invariant_fingerprints[reference] = _require_lower_hex_64(
                item["durable_invariant_fingerprint"],
                "durable_invariant_fingerprint",
            )
        for raw_item in after["reservations"]:
            item = _exact_object_keys(raw_item, expected_item, "audit.after.item")
            reference = _require_lower_hex_64(
                item["reservation_ref"],
                "reservation_ref",
            )
            if item["status"] != "confirmed":
                raise ValueError("audit terminal status is invalid")
            if item["durable_invariant_fingerprint"] != (
                invariant_fingerprints.get(reference)
            ):
                raise ValueError("audit durable invariant is inconsistent")
            after_refs.append(reference)
        if tuple(statuses) != approved_refs or tuple(after_refs) != approved_refs:
            raise ValueError("audit population does not match the approved plan")
        return statuses, invariant_fingerprints
    except (
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        raise _apply_refusal("unexpected_audit_event") from exc


def _load_applied_recovery_source_read_only(
    database_path: str | Path,
    *,
    approved_plan: BoundCloseReservationRecoveryPlan,
) -> BoundCloseReservationSource:
    expanded = Path(database_path).expanduser()
    if expanded.is_symlink() or not expanded.is_file():
        raise _apply_refusal("database_path_invalid")
    resolved = expanded.resolve()
    database_identity = _database_file_identity(resolved)
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise _apply_refusal("source_query_only_unavailable")
        connection.execute("BEGIN")
        if _database_file_identity(resolved) != database_identity:
            raise _apply_refusal("database_authority_mismatch")
        if not _locked_mimo_contract_mode_is_v1(connection):
            raise _apply_refusal("mimo_contract_mode_not_v1")
        audits = _load_bound_close_reservation_audits(connection)
        matching = tuple(
            row
            for row in audits
            if row["notification_fingerprint"]
            == approved_plan.evidence_fingerprint
        )
        if len(audits) != 1 or len(matching) != 1:
            raise _apply_refusal("unexpected_audit_event")
        audit = matching[0]
        (
            statuses_by_ref,
            audited_invariant_fingerprints,
        ) = _parse_applied_recovery_audit_statuses(
            plan=approved_plan,
            before_json=audit["before_json"],
            after_json=audit["after_json"],
        )
        candidates = connection.execute(
            """
            SELECT id, pos_id, execution_binding_id, status, last_error,
                   created_at, updated_at
            FROM bound_position_close_reservations
            WHERE status = 'confirmed' AND last_error IS NULL AND updated_at = ?
            ORDER BY id
            LIMIT 65
            """,
            (audit["created_at"],),
        ).fetchall()
        if len(candidates) > MAX_RESERVATION_OBSERVATIONS:
            raise _apply_refusal("source_population_overflow")
        candidates_by_ref = {
            _redacted_ref("reservation", row["id"]): row
            for row in candidates
            if type(row["id"]) is int and row["id"] > 0
        }
        if set(candidates_by_ref) != set(statuses_by_ref):
            raise _apply_refusal("source_state_conflict")
        ordered_rows = [
            candidates_by_ref[item.reservation_ref]
            for item in approved_plan.observations
        ]
        status_overrides = {
            int(row["id"]): statuses_by_ref[
                _redacted_ref("reservation", row["id"])
            ]
            for row in ordered_rows
        }
        source = _load_source_descendants(
            connection,
            ordered_rows,
            source_status_overrides=status_overrides,
        )
        raw_rows = _planned_raw_reservations_from_source(
            source=source,
            plan=approved_plan,
        )
        current_invariant_fingerprints = _durable_invariant_fingerprints(
            connection,
            raw_rows=raw_rows,
        )
        if current_invariant_fingerprints != audited_invariant_fingerprints:
            raise _apply_refusal("durable_invariant_conflict")
        before_expected, after_expected = _recovery_audit_payloads(
            plan=approved_plan,
            raw_rows=raw_rows,
            durable_invariant_fingerprints=(
                audited_invariant_fingerprints
            ),
        )
        if (
            not _exact_recovery_audit_matches(
                audit,
                plan=approved_plan,
                before_json=before_expected,
                after_json=after_expected,
            )
            or not _planned_rows_are_exactly_confirmed(
                connection,
                raw_rows=raw_rows,
                audit_created_at=audit["created_at"],
            )
            or _locked_active_bound_close_reservation_source(
                connection
            ).reservations
        ):
            raise _apply_refusal("source_state_conflict")
        if _database_file_identity(resolved) != database_identity:
            raise _apply_refusal("database_authority_mismatch")
        return _register_bound_close_reservation_source(
            source,
            database_identity=database_identity,
        )
    except BoundCloseReservationRecoveryConflict:
        raise
    except (OSError, RecursionError, sqlite3.Error, TypeError, ValueError) as exc:
        raise _apply_refusal("idempotent_source_read_failed") from exc
    finally:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                try:
                    connection.close()
                except Exception:
                    pass


def _planned_raw_reservations_from_source(
    *,
    source: BoundCloseReservationSource,
    plan: BoundCloseReservationRecoveryPlan,
) -> tuple[tuple[str, _RawReservationCapability], ...]:
    raw_rows: list[tuple[str, _RawReservationCapability]] = []
    try:
        for observation in plan.observations:
            raw = source._capability._get(observation.reservation_ref)
            if type(raw) is not _RawReservationCapability:
                raise TypeError("raw recovery authority is invalid")
            raw_rows.append((observation.reservation_ref, raw))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise _apply_refusal("idempotent_source_authority_invalid") from exc
    if len(raw_rows) != plan.action_count:
        raise _apply_refusal("idempotent_source_authority_invalid")
    return tuple(raw_rows)


def recapture_and_seal_applied_bound_close_reservation_recovery(
    database_path: str | Path,
    *,
    approved_plan: BoundCloseReservationRecoveryPlan,
    reader: BoundCloseReservationExchangeReader,
    deadline_monotonic: float,
) -> SealedRecoveryCapture:
    """Re-read one already-applied exact population and all five GET facts."""

    try:
        _require_canonical_recovery_plan(approved_plan)
    except (RecursionError, TypeError, ValueError) as exc:
        raise _apply_refusal("plan_not_actionable") from exc
    if approved_plan.status != "ready" or approved_plan.action_count <= 0:
        raise _apply_refusal("plan_not_actionable")
    if type(reader) is not BoundCloseReservationExchangeReader:
        raise TypeError("reader must be the dedicated recovery reader")
    deadline = _finite_recovery_monotonic(deadline_monotonic)
    if time.monotonic() >= deadline:
        raise _apply_refusal("fresh_capture_expired")
    source = _load_applied_recovery_source_read_only(
        database_path,
        approved_plan=approved_plan,
    )
    database_identity = _require_issued_bound_close_reservation_source(source)
    local_by_ref = {item.reservation_ref: item for item in source.reservations}
    raw_rows = _planned_raw_reservations_from_source(
        source=source,
        plan=approved_plan,
    )
    raw_by_ref = dict(raw_rows)
    capture, started_at, completed_at = _capture_recovery_exchange_collections(
        raw_by_ref=raw_by_ref,
        reader=reader,
        deadline_monotonic=deadline,
    )
    observations: list[BoundCloseReservationObservation] = []
    for approved in approved_plan.observations:
        local = local_by_ref.get(approved.reservation_ref)
        raw = raw_by_ref.get(approved.reservation_ref)
        if local is None:
            raise _apply_refusal("idempotent_source_authority_invalid")
        exchange = _normalize_recovery_exchange_evidence(
            local=local,
            raw=raw,
            capture=capture,
        )
        classified = _classify_bound_close_reservation_pure(
            local,
            exchange,
            capture_completed_at=completed_at,
        )
        observations.append(
            BoundCloseReservationObservation(
                reservation_ref=approved.reservation_ref,
                classification=classified.classification,
                reason_code=classified.reason_code,
                source_fingerprint=approved.source_fingerprint,
                exchange_fingerprint=classified.exchange_fingerprint,
            )
        )
    plan = build_bound_close_reservation_recovery_plan(
        source_fingerprint=approved_plan.source_fingerprint,
        observations=tuple(observations),
    )
    return _issue_sealed_recovery_capture(
        plan=plan,
        source_capability=source._capability,
        database_identity=database_identity,
        capture_started_at=started_at,
        capture_completed_at=completed_at,
        deadline_monotonic=deadline,
    )


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


def _provider_consistent_text(
    row: Mapping[str, Any],
    *field_names: str,
    required: bool = True,
    casefold: bool = False,
) -> str | None:
    values = [
        _provider_text(row, field_name)
        for field_name in field_names
        if row.get(field_name) not in (None, "")
    ]
    if not values:
        if required:
            raise ValueError("provider semantic field is missing")
        return None
    comparable = [value.casefold() if casefold else value for value in values]
    if len(set(comparable)) != 1:
        raise ValueError("provider semantic field is conflicting")
    return comparable[0] if casefold else values[0]


def _provider_identity(
    row: Mapping[str, Any],
    *,
    include_order: bool,
    require_position_field: str | None = None,
) -> tuple[str, str, str, str | None]:
    instrument_id = _provider_text(row, "instId")
    position_id = _provider_consistent_text(row, "posId", "closePosId")
    if position_id is None:
        raise ValueError("provider position identity is missing")
    if require_position_field is not None:
        required_position_id = _provider_text(row, require_position_field)
        if required_position_id != position_id:
            raise ValueError("provider position identity is conflicting")
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


def _provider_timestamp_field(
    row: Mapping[str, Any],
    field_name: str,
    *,
    required: bool,
) -> datetime | None:
    raw = row.get(field_name)
    if raw in (None, ""):
        if required:
            raise ValueError(f"provider {field_name} is missing")
        return None
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


def _provider_authoritative_timestamp(
    row: Mapping[str, Any],
    authority_field: str,
    *,
    not_after_authority: tuple[str, ...] = (),
    not_before_authority: tuple[str, ...] = (),
) -> datetime:
    authority = _provider_timestamp_field(
        row,
        authority_field,
        required=True,
    )
    if authority is None:
        raise ValueError("provider authoritative timestamp is missing")
    for field_name in not_after_authority:
        candidate = _provider_timestamp_field(row, field_name, required=False)
        if candidate is not None and candidate > authority:
            raise ValueError("provider timestamp order is invalid")
    for field_name in not_before_authority:
        candidate = _provider_timestamp_field(row, field_name, required=False)
        if candidate is not None and candidate < authority:
            raise ValueError("provider timestamp order is invalid")
    return authority


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
            require_position_field="posId",
        )
        rows.append(
            ExchangePositionEvidence(
                instrument_ref=instrument_ref,
                side=side,
                position_ref=position_ref,
                quantity=_provider_decimal(row, "pos", positive=True),
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
        provider_state_text = _provider_consistent_text(
            row,
            "state",
            "status",
            casefold=True,
        )
        if provider_state_text is None:
            raise ValueError("provider order state is missing")
        provider_state = provider_state_text.lower()
        state = _PROVIDER_ORDER_STATES.get(provider_state)
        if state is None:
            raise ValueError("provider order state is not closed")
        terminal_at = None
        if state not in {
            ExchangeCloseOrderState.OPEN,
            ExchangeCloseOrderState.PENDING,
            ExchangeCloseOrderState.PARTIALLY_FILLED,
        }:
            terminal_at = _provider_authoritative_timestamp(
                row,
                "uTime",
                not_after_authority=("cTime",),
            )
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
                filled_at=_provider_authoritative_timestamp(
                    row,
                    "fillTime",
                    not_after_authority=("cTime",),
                    not_before_authority=("uTime",),
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
            require_position_field="posId",
        )
        provider_state = _provider_consistent_text(
            row,
            "state",
            "status",
            required=False,
            casefold=True,
        )
        if provider_state is not None and provider_state.lower() != "closed":
            raise ValueError("provider position-history state is not closed")
        original_quantity = _provider_decimal(row, "pos", positive=True)
        closed_quantity = _provider_decimal(row, "closePos", positive=True)
        if closed_quantity != original_quantity:
            raise ValueError("provider closed position quantity is invalid")
        closed_at = _provider_authoritative_timestamp(
            row,
            "uTime",
            not_after_authority=("cTime",),
        )
        rows.append(
            ExchangePositionHistoryEvidence(
                instrument_ref=instrument_ref,
                side=side,
                position_ref=position_ref,
                state=ExchangePositionHistoryState.CLOSED,
                closed_quantity=closed_quantity,
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
    if len(local.close_mutations) > 1:
        return True
    for mutation in local.close_mutations:
        if mutation.order_ref is not None and mutation.order_ref != local.close_order_ref:
            return True
        if mutation.status != "confirmed":
            return True
        if mutation.order_ref is None:
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
    database_identity: _DatabaseFileIdentity | None = None

    def issued(
        source: BoundCloseReservationSource,
    ) -> BoundCloseReservationSource:
        if database_identity is None:
            return source
        if _database_file_identity(resolved) != database_identity:
            return _source_failure("source_file_identity_changed")
        return _register_bound_close_reservation_source(
            source,
            database_identity=database_identity,
        )

    try:
        database_identity = _database_file_identity(resolved)
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or int(query_only[0]) != 1:
            return issued(_source_failure("source_query_only_unavailable"))
        connection.execute("BEGIN")
        if _database_file_identity(resolved) != database_identity:
            return _source_failure("source_file_identity_changed")
        if not _source_schema_is_valid(connection):
            return issued(_source_failure("source_schema_invalid"))
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
            return issued(_source_failure("source_overflow"))
        if between_selects_hook is not None:
            between_selects_hook()
        return issued(_load_source_descendants(connection, reservation_rows))
    except (
        BoundCloseReservationRecoveryConflict,
        sqlite3.Error,
        OSError,
        TypeError,
        ValueError,
        OverflowError,
    ):
        return issued(_source_failure("source_read_failed"))
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
    *,
    source_status_overrides: Mapping[int, str] | None = None,
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
        reservation_id = reservation["id"]
        evidence, raw = _build_local_reservation_evidence(
            reservation,
            row_index=index,
            source_status_override=(
                source_status_overrides.get(reservation_id)
                if source_status_overrides is not None
                and type(reservation_id) is int
                else None
            ),
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
    source_status_override: str | None = None,
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
    source_status = (
        source_status_override
        if source_status_override is not None
        else reservation["status"]
    )
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

    mutation_row_ids = [row["id"] for row in mutation_rows]
    mutation_identity_keys = [
        row["idempotency_key"]
        for row in mutation_rows
        if _required_text(row["idempotency_key"])
    ]
    if (
        len(mutation_row_ids) != len(set(mutation_row_ids))
        or len(mutation_identity_keys) != len(set(mutation_identity_keys))
    ):
        local_reason = "local_identity_conflict"

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
