"""Closed contract for bound-position close-reservation recovery."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, StrEnum
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote


RECOVERY_SCHEMA_VERSION = 1
MAX_RESERVATION_OBSERVATIONS = 64

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

_MAX_LOCAL_DESCENDANTS = 256
_MAX_LOCAL_JSON_BYTES = 1_048_576


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


class _RawReservationCapability:
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


class _BoundCloseReservationSourceCapability:
    __slots__ = ("__raw_by_reservation_ref",)

    def __init__(self, raw_by_reservation_ref: Mapping[str, _RawReservationCapability]):
        self.__raw_by_reservation_ref = dict(raw_by_reservation_ref)

    def _get(self, reservation_ref: str) -> _RawReservationCapability:
        return self.__raw_by_reservation_ref[reservation_ref]


@dataclass(frozen=True, slots=True)
class BoundCloseReservationSource:
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
            "purpose",
            "order_id",
            "pos_id",
            "venue",
            "attribution_status",
            "status",
            "attribution_evidence_json",
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
            "operation",
            "strategy_instance_id",
            "execution_binding_id",
            "execution_order_leg_id",
            "pos_id",
            "order_id",
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
        SELECT id, execution_binding_id, strategy_instance_id, purpose,
               order_id, pos_id, venue, attribution_status, status,
               attribution_evidence_json, request_json, response_json,
               last_verified_at, created_at, updated_at
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
        SELECT id, operation, strategy_instance_id, execution_binding_id,
               execution_order_leg_id, pos_id, order_id, venue, status,
               request_json, response_json, error_json, reserved_at,
               submitted_at, confirmed_at, created_at, updated_at
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
        and _required_text(row["status"])
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
        and _required_text(row["status"])
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
        _required_text(row["status"])
        and _required_text(row["attribution_status"])
        and _optional_text(row["order_id"])
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
        not _required_text(row["status"])
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
    return LocalCloseMutationEvidence(
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
