"""Closed contract for bound-position close-reservation recovery."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta
from enum import Enum, StrEnum
import hashlib
import json
import re
from typing import Any, Mapping


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
