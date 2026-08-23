"""Validation models for authoritative Deepcoin contract-spec snapshots."""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
from decimal import InvalidOperation
import errno
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from types import MappingProxyType
from typing import Any
from typing import Mapping

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec


DEEPCOIN_CONTRACT_SPEC_SNAPSHOT_SCHEMA_VERSION = 1
DEEPCOIN_CONTRACT_SPEC_SOURCE_PATH = "/deepcoin/market/instruments?instType=SWAP"

_INSTRUMENT_ID_PATTERN = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+-SWAP$")
_REQUIRED_FIELDS = (
    "instType",
    "instId",
    "ctVal",
    "lotSz",
    "minSz",
    "tickSz",
    "state",
)
_NUMERIC_FIELDS = ("ctVal", "lotSz", "minSz", "tickSz")
# Deepcoin contract metadata normally uses short fixed-point values. These
# limits leave substantial headroom while bounding Decimal coefficient and
# power-of-ten work before exact ratio or canonical string construction.
_MAX_NUMERIC_INPUT_LENGTH = 128
_MAX_NUMERIC_SIGNIFICANT_DIGITS = 64
_MIN_NUMERIC_ADJUSTED_EXPONENT = -100
_MAX_NUMERIC_ADJUSTED_EXPONENT = 100
_MAX_NUMERIC_INTEGER_BITS = 512
_MAX_CACHE_FILE_BYTES = 4 * 1024 * 1024
_MAX_CACHE_METADATA_TEXT_LENGTH = 512
_MAX_CACHE_INSTRUMENT_COUNT = 10_000
_MAX_CACHE_INSTRUMENT_ID_LENGTH = 128
_MAX_CACHE_STATE_LENGTH = 64
_DECIMAL_TEXT_PATTERN = re.compile(
    r"^[+]?(?:(?P<integer>[0-9]+)(?:\.(?P<fraction>[0-9]*))?"
    r"|\.(?P<fraction_only>[0-9]+))(?:[eE](?P<exponent>[+-]?[0-9]+))?$"
)


class DeepcoinContractSpecRefreshOrchestrator:
    """Run cache refreshes off-loop with bounded waits and cadence."""

    def __init__(
        self,
        provider: Any,
        *,
        refresh_timeout_seconds: float,
        minimum_interval_seconds: float = 30.0,
        maximum_interval_seconds: float = 12 * 60 * 60,
        now_provider: Any | None = None,
    ) -> None:
        if refresh_timeout_seconds <= 0:
            raise ValueError("refresh_timeout_seconds must be positive")
        if minimum_interval_seconds <= 0:
            raise ValueError("minimum_interval_seconds must be positive")
        if maximum_interval_seconds < minimum_interval_seconds:
            raise ValueError("maximum_interval_seconds must not be below minimum")
        ttl = getattr(provider, "ttl", None)
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("refresh provider must expose a positive ttl")
        self._provider = provider
        self._refresh_timeout_seconds = float(refresh_timeout_seconds)
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._future_lock = threading.Lock()
        self._refresh_future: concurrent.futures.Future[bool] | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="deepcoin-contract-spec-refresh",
        )
        self._closed = False
        half_ttl_seconds = ttl.total_seconds() / 2
        self.interval_seconds = max(
            float(minimum_interval_seconds),
            min(half_ttl_seconds, float(maximum_interval_seconds)),
        )

    async def refresh_once(self) -> dict[str, object]:
        """Attempt one refresh without blocking the application event loop."""

        with self._future_lock:
            if self._closed:
                return self.status(
                    refresh_succeeded=False,
                    error_override="refresh_orchestrator_closed",
                )
            if self._refresh_future is None or self._refresh_future.done():
                self._refresh_future = self._executor.submit(self._provider.refresh)
            refresh_future = self._refresh_future
        try:
            refreshed = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(refresh_future)),
                timeout=self._refresh_timeout_seconds,
            )
        except TimeoutError:
            return self.status(
                refresh_succeeded=False,
                error_override="refresh_timeout",
            )
        except Exception as exc:
            return self.status(
                refresh_succeeded=False,
                error_override=f"refresh_failed:{type(exc).__name__}",
            )
        return self.status(refresh_succeeded=bool(refreshed))

    def close(self) -> None:
        """Prevent new work; the exchange client's bounded call may finish."""

        with self._future_lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def run(self) -> None:
        """Refresh immediately at startup, then at a bounded half-TTL cadence."""

        while True:
            await self.refresh_once()
            await asyncio.sleep(self.interval_seconds)

    def status(
        self,
        *,
        refresh_succeeded: bool | None = None,
        error_override: str | None = None,
    ) -> dict[str, object]:
        metadata = getattr(self._provider, "metadata", None)
        snapshot = getattr(self._provider, "snapshot", None)
        expires_at = getattr(metadata, "expires_at", None)
        last_success_at = getattr(metadata, "last_success_at", None)
        snapshot_fetched_at = getattr(snapshot, "fetched_at", None)
        snapshot_expires_at = getattr(snapshot, "expires_at", None)
        now = self._now()
        if snapshot is None:
            state = "unavailable"
        elif not all(
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
            for value in (
                expires_at,
                last_success_at,
                snapshot_fetched_at,
                snapshot_expires_at,
            )
        ):
            state = "unavailable"
        elif (
            self._utc(last_success_at) != self._utc(snapshot_fetched_at)
            or self._utc(expires_at) != self._utc(snapshot_expires_at)
            or now < self._utc(snapshot_fetched_at)
        ):
            state = "unavailable"
        elif self._utc(expires_at) <= now:
            state = "stale"
        else:
            state = "fresh"
        return {
            "state": state,
            "refresh_succeeded": refresh_succeeded,
            "last_success_at": self._format_datetime(last_success_at),
            "expires_at": self._format_datetime(expires_at),
            "last_error": error_override
            if error_override is not None
            else getattr(metadata, "last_error", None),
        }

    def _now(self) -> datetime:
        value = self._now_provider()
        if not isinstance(value, datetime):
            raise ValueError("refresh clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _format_datetime(cls, value: object) -> str | None:
        if not isinstance(value, datetime):
            return None
        return cls._utc(value).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class DeepcoinInstrumentCapability:
    """One validated USDT perpetual, including non-live instruments."""

    instrument_id: str
    state: str
    contract_value: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    price_tick: Decimal

    @property
    def contract_spec(self) -> DeepcoinContractSpec:
        """Convert exact exchange values to the legacy execution spec type."""

        return DeepcoinContractSpec(
            instrument_id=self.instrument_id,
            contract_value=_finite_float(self.contract_value, field="ctVal"),
            quantity_step=_finite_float(self.quantity_step, field="lotSz"),
            min_quantity=_finite_float(self.min_quantity, field="minSz"),
            price_tick=_finite_float(self.price_tick, field="tickSz"),
        )


@dataclass(frozen=True)
class DeepcoinContractSpecSnapshot:
    """An immutable, complete view of Deepcoin's validated USDT swaps."""

    schema_version: int
    venue: str
    source_path: str
    fetched_at: datetime
    expires_at: datetime
    source_digest_sha256: str
    capabilities_by_instrument_id: Mapping[str, DeepcoinInstrumentCapability]

    def __post_init__(self) -> None:
        # A frozen dataclass alone does not protect a caller-owned mutable dict.
        # Copying before wrapping makes the snapshot immutable at its boundary.
        object.__setattr__(
            self,
            "capabilities_by_instrument_id",
            MappingProxyType(dict(self.capabilities_by_instrument_id)),
        )

    @property
    def specs_by_instrument_id(self) -> Mapping[str, DeepcoinContractSpec]:
        """Return only live specifications eligible for new-entry decisions."""

        return MappingProxyType(
            {
                instrument_id: capability.contract_spec
                for instrument_id, capability in self.capabilities_by_instrument_id.items()
                if capability.state == "live"
            }
        )

    @property
    def states_by_instrument_id(self) -> Mapping[str, str]:
        """Return platform states for live and non-live validated instruments."""

        return MappingProxyType(
            {
                instrument_id: capability.state
                for instrument_id, capability in self.capabilities_by_instrument_id.items()
            }
        )

    def to_instrument_rows(self) -> list[dict[str, str]]:
        """Return canonical JSON-safe rows without losing Decimal precision."""

        return [
            {
                "instType": "SWAP",
                "instId": capability.instrument_id,
                "ctVal": _canonical_decimal(capability.contract_value),
                "lotSz": _canonical_decimal(capability.quantity_step),
                "minSz": _canonical_decimal(capability.min_quantity),
                "tickSz": _canonical_decimal(capability.price_tick),
                "state": capability.state,
            }
            for _, capability in sorted(self.capabilities_by_instrument_id.items())
        ]


@dataclass(frozen=True)
class _ValidatedInstrument:
    instrument_id: str
    state: str
    contract_value: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    price_tick: Decimal


def validate_deepcoin_instrument_snapshot(
    rows: object,
    *,
    fetched_at: datetime,
    ttl: timedelta,
    source_path: str = DEEPCOIN_CONTRACT_SPEC_SOURCE_PATH,
) -> DeepcoinContractSpecSnapshot:
    """Validate a complete API candidate or reject it without partial results."""

    _validate_snapshot_timing(fetched_at=fetched_at, ttl=ttl)
    if not isinstance(source_path, str) or not source_path.strip():
        raise ValueError("source_path must be a non-empty string")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Deepcoin instrument snapshot must be a non-empty list")

    validated_by_id: dict[str, _ValidatedInstrument] = {}
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, Mapping):
            raise ValueError(f"Deepcoin instrument row {index} must be a mapping")

        inst_type = _required_string(raw_row, "instType", row_index=index)
        if inst_type != "SWAP":
            raise ValueError(f"Deepcoin instrument row {index} instType must be SWAP")

        instrument_id = _required_string(raw_row, "instId", row_index=index)
        if not _INSTRUMENT_ID_PATTERN.fullmatch(instrument_id):
            raise ValueError(f"Deepcoin instrument row {index} has invalid instId")

        # The endpoint can legitimately contain swaps settled in other quote
        # currencies. They are outside this USDT-only execution capability.
        if not instrument_id.endswith("-USDT-SWAP"):
            continue

        for field in _REQUIRED_FIELDS:
            if field not in raw_row:
                raise ValueError(
                    f"Deepcoin instrument row {index} is missing required field {field}"
                )

        if instrument_id in validated_by_id:
            raise ValueError(f"duplicate Deepcoin instrument id {instrument_id}")

        state = _required_string(raw_row, "state", row_index=index).strip().lower()
        if not state:
            raise ValueError(f"Deepcoin instrument row {index} has invalid state")
        numbers = {
            field: _positive_decimal(raw_row[field], field=field, row_index=index)
            for field in _NUMERIC_FIELDS
        }
        if not _is_integral_multiple(numbers["minSz"], numbers["lotSz"]):
            raise ValueError(
                f"Deepcoin instrument row {index} minSz must be compatible with lotSz"
            )

        validated_by_id[instrument_id] = _ValidatedInstrument(
            instrument_id=instrument_id,
            state=state,
            contract_value=numbers["ctVal"],
            quantity_step=numbers["lotSz"],
            min_quantity=numbers["minSz"],
            price_tick=numbers["tickSz"],
        )

    if not validated_by_id:
        raise ValueError("Deepcoin instrument snapshot has no USDT SWAP instruments")

    capabilities = {
        instrument_id: DeepcoinInstrumentCapability(
            instrument_id=instrument_id,
            state=validated.state,
            contract_value=validated.contract_value,
            quantity_step=validated.quantity_step,
            min_quantity=validated.min_quantity,
            price_tick=validated.price_tick,
        )
        for instrument_id, validated in sorted(validated_by_id.items())
    }
    # Exercise the legacy float conversion only after the entire response has
    # passed structural and Decimal validation. Non-live rows are checked too,
    # because they remain part of the platform capability snapshot.
    for capability in capabilities.values():
        capability.contract_spec
    digest = _snapshot_digest(validated_by_id)
    normalized_fetched_at = fetched_at.astimezone(timezone.utc)
    return DeepcoinContractSpecSnapshot(
        schema_version=DEEPCOIN_CONTRACT_SPEC_SNAPSHOT_SCHEMA_VERSION,
        venue="deepcoin",
        source_path=source_path.strip(),
        fetched_at=normalized_fetched_at,
        expires_at=normalized_fetched_at + ttl,
        source_digest_sha256=digest,
        capabilities_by_instrument_id=capabilities,
    )


def publish_deepcoin_contract_spec_snapshot(
    cache_path: str | Path,
    snapshot: DeepcoinContractSpecSnapshot,
    *,
    now: datetime,
) -> None:
    """Durably publish a validated snapshot without exposing partial JSON."""

    if not isinstance(snapshot, DeepcoinContractSpecSnapshot):
        raise TypeError("snapshot must be a DeepcoinContractSpecSnapshot")
    normalized_now = _normalize_aware_datetime(now, field="now")
    serialized_payload = _serialize_snapshot_payload(snapshot)
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        if sys.platform.startswith("linux"):
            remove_xattr = getattr(os, "removexattr", None)
            if remove_xattr is None:
                subprocess.run(
                    [
                        "/usr/bin/setfacl",
                        "-b",
                        "--",
                        f"/proc/self/fd/{descriptor}",
                    ],
                    check=True,
                    close_fds=True,
                    pass_fds=(descriptor,),
                    env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                )
            else:
                try:
                    remove_xattr(descriptor, "system.posix_acl_access")
                except OSError as exc:
                    if exc.errno != errno.ENODATA:
                        raise
        os.fchmod(descriptor, 0o660)
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(serialized_payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        # Re-read through the same strict parser used at startup before the
        # candidate is allowed to replace the last known-good snapshot.
        _load_snapshot_file(temporary_path, now=normalized_now)
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def load_deepcoin_contract_spec_snapshot(
    cache_path: str | Path,
    *,
    now: datetime,
) -> DeepcoinContractSpecSnapshot:
    """Load a fresh, digest-verified snapshot or fail closed."""

    normalized_now = _normalize_aware_datetime(now, field="now")
    return _load_snapshot_file(Path(cache_path), now=normalized_now)


def _snapshot_payload(snapshot: DeepcoinContractSpecSnapshot) -> dict[str, object]:
    return {
        "schema_version": snapshot.schema_version,
        "venue": snapshot.venue,
        "source_path": snapshot.source_path,
        "fetched_at": _format_datetime(snapshot.fetched_at),
        "expires_at": _format_datetime(snapshot.expires_at),
        "source_digest_sha256": snapshot.source_digest_sha256,
        "instruments": snapshot.to_instrument_rows(),
    }


def _serialize_snapshot_payload(snapshot: DeepcoinContractSpecSnapshot) -> bytes:
    _validate_snapshot_publish_bounds(snapshot)
    serialized = json.dumps(
        _snapshot_payload(snapshot),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    if len(serialized) > _MAX_CACHE_FILE_BYTES:
        raise ValueError("Deepcoin contract spec cache exceeds safe size limit")
    return serialized


def _validate_snapshot_publish_bounds(snapshot: DeepcoinContractSpecSnapshot) -> None:
    capabilities = snapshot.capabilities_by_instrument_id
    if len(capabilities) > _MAX_CACHE_INSTRUMENT_COUNT:
        raise ValueError("Deepcoin contract spec cache exceeds safe instrument count")
    for index, (instrument_id, capability) in enumerate(capabilities.items()):
        if (
            not isinstance(instrument_id, str)
            or len(instrument_id) > _MAX_CACHE_INSTRUMENT_ID_LENGTH
            or not isinstance(capability, DeepcoinInstrumentCapability)
            or not isinstance(capability.instrument_id, str)
            or len(capability.instrument_id) > _MAX_CACHE_INSTRUMENT_ID_LENGTH
            or not isinstance(capability.state, str)
            or len(capability.state) > _MAX_CACHE_STATE_LENGTH
        ):
            raise ValueError("Deepcoin contract spec cache metadata exceeds safe bounds")
        for field, value in (
            ("ctVal", capability.contract_value),
            ("lotSz", capability.quantity_step),
            ("minSz", capability.min_quantity),
            ("tickSz", capability.price_tick),
        ):
            _positive_decimal(value, field=field, row_index=index)
    for field, value in (
        ("venue", snapshot.venue),
        ("source_path", snapshot.source_path),
        ("source_digest_sha256", snapshot.source_digest_sha256),
    ):
        if not isinstance(value, str) or len(value) > _MAX_CACHE_METADATA_TEXT_LENGTH:
            raise ValueError(
                f"Deepcoin contract spec cache {field} exceeds safe bounds"
            )


def _load_snapshot_file(
    path: Path,
    *,
    now: datetime | None,
) -> DeepcoinContractSpecSnapshot:
    try:
        with path.open("rb") as cache_file:
            raw_payload = cache_file.read(_MAX_CACHE_FILE_BYTES + 1)
    except OSError:
        raise
    if len(raw_payload) > _MAX_CACHE_FILE_BYTES:
        raise ValueError("Deepcoin contract spec cache exceeds safe size limit")
    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Deepcoin contract spec cache must contain valid JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("Deepcoin contract spec cache root must be an object")

    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or schema_version != DEEPCOIN_CONTRACT_SPEC_SNAPSHOT_SCHEMA_VERSION
    ):
        raise ValueError("unsupported Deepcoin contract spec cache schema_version")
    venue = _cache_text(payload, "venue")
    if venue != "deepcoin":
        raise ValueError("Deepcoin contract spec cache has invalid venue")
    source_path = _cache_text(payload, "source_path")
    fetched_at = _parse_cache_datetime(payload, "fetched_at")
    expires_at = _parse_cache_datetime(payload, "expires_at")
    if expires_at <= fetched_at:
        raise ValueError("Deepcoin contract spec cache expires_at must follow fetched_at")
    digest = _cache_text(payload, "source_digest_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Deepcoin contract spec cache has invalid source digest")
    rows = payload.get("instruments")

    validated = validate_deepcoin_instrument_snapshot(
        rows,
        fetched_at=fetched_at,
        ttl=expires_at - fetched_at,
        source_path=source_path,
    )
    if not hmac.compare_digest(validated.source_digest_sha256, digest):
        raise ValueError("Deepcoin contract spec cache digest mismatch")
    if now is not None:
        if fetched_at > now:
            raise ValueError("Deepcoin contract spec cache fetched_at is in the future")
        if now >= expires_at:
            raise ValueError("Deepcoin contract spec cache is stale")
    return validated


def _cache_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if (
        isinstance(value, bool)
        or not isinstance(value, str)
        or not value
        or len(value) > _MAX_CACHE_METADATA_TEXT_LENGTH
    ):
        raise ValueError(f"Deepcoin contract spec cache has invalid {field}")
    return value


def _parse_cache_datetime(payload: Mapping[str, Any], field: str) -> datetime:
    value = _cache_text(payload, field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"Deepcoin contract spec cache has invalid {field}"
        ) from None
    return _normalize_aware_datetime(parsed, field=field)


def _normalize_aware_datetime(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _normalize_aware_datetime(value, field="snapshot datetime").isoformat()


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_snapshot_timing(*, fetched_at: datetime, ttl: timedelta) -> None:
    if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    if fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
        raise ValueError("ttl must be positive")


def _required_string(row: Mapping[str, Any], field: str, *, row_index: int) -> str:
    if field not in row:
        raise ValueError(
            f"Deepcoin instrument row {row_index} is missing required field {field}"
        )
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, str) or not value.strip():
        raise ValueError(f"Deepcoin instrument row {row_index} has invalid {field}")
    return value


def _positive_decimal(value: Any, *, field: str, row_index: int) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"Deepcoin instrument row {row_index} has invalid {field}")

    if isinstance(value, str):
        _validate_decimal_text_bounds(value, field=field, row_index=row_index)
        decimal_input: str | int | float | Decimal = value
    elif isinstance(value, int):
        if value.bit_length() > _MAX_NUMERIC_INTEGER_BITS:
            _raise_numeric_bounds(field=field, row_index=row_index)
        decimal_input = value
    elif isinstance(value, (float, Decimal)):
        decimal_input = value
    else:
        raise ValueError(f"Deepcoin instrument row {row_index} has invalid {field}")

    try:
        parsed = (
            decimal_input
            if isinstance(decimal_input, Decimal)
            else Decimal(str(decimal_input))
        )
    except (InvalidOperation, ValueError):
        raise ValueError(
            f"Deepcoin instrument row {row_index} has invalid {field}"
        ) from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"Deepcoin instrument row {row_index} has invalid {field}")
    if (
        len(parsed.as_tuple().digits) > _MAX_NUMERIC_SIGNIFICANT_DIGITS
        or parsed.adjusted() < _MIN_NUMERIC_ADJUSTED_EXPONENT
        or parsed.adjusted() > _MAX_NUMERIC_ADJUSTED_EXPONENT
    ):
        _raise_numeric_bounds(field=field, row_index=row_index)
    return parsed


def _validate_decimal_text_bounds(
    value: str,
    *,
    field: str,
    row_index: int,
) -> None:
    if len(value) > _MAX_NUMERIC_INPUT_LENGTH:
        _raise_numeric_bounds(field=field, row_index=row_index)

    match = _DECIMAL_TEXT_PATTERN.fullmatch(value.strip())
    if match is None:
        return

    integer = match.group("integer") or ""
    fraction = match.group("fraction")
    if fraction is None:
        fraction = match.group("fraction_only") or ""
    coefficient = integer + fraction
    significant = coefficient.lstrip("0")
    if len(significant) > _MAX_NUMERIC_SIGNIFICANT_DIGITS:
        _raise_numeric_bounds(field=field, row_index=row_index)

    exponent_text = match.group("exponent")
    explicit_exponent = int(exponent_text) if exponent_text else 0
    first_nonzero = next(
        (index for index, digit in enumerate(coefficient) if digit != "0"),
        None,
    )
    if first_nonzero is None:
        adjusted_exponent = explicit_exponent
    else:
        adjusted_exponent = explicit_exponent + len(integer) - first_nonzero - 1
    if not (
        _MIN_NUMERIC_ADJUSTED_EXPONENT
        <= adjusted_exponent
        <= _MAX_NUMERIC_ADJUSTED_EXPONENT
    ):
        _raise_numeric_bounds(field=field, row_index=row_index)


def _raise_numeric_bounds(*, field: str, row_index: int) -> None:
    raise ValueError(
        f"Deepcoin instrument row {row_index} {field} exceeds safe numeric bounds"
    )


def _is_integral_multiple(value: Decimal, step: Decimal) -> bool:
    value_numerator, value_denominator = value.as_integer_ratio()
    step_numerator, step_denominator = step.as_integer_ratio()
    ratio_numerator = value_numerator * step_denominator
    ratio_denominator = value_denominator * step_numerator
    return ratio_numerator % ratio_denominator == 0


def _snapshot_digest(instruments: Mapping[str, _ValidatedInstrument]) -> str:
    normalized_rows = [
        {
            "instId": instrument.instrument_id,
            "instType": "SWAP",
            "ctVal": _canonical_decimal(instrument.contract_value),
            "lotSz": _canonical_decimal(instrument.quantity_step),
            "minSz": _canonical_decimal(instrument.min_quantity),
            "state": instrument.state,
            "tickSz": _canonical_decimal(instrument.price_tick),
        }
        for _, instrument in sorted(instruments.items())
    ]
    normalized_json = json.dumps(
        normalized_rows,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(normalized_json.encode("utf-8")).hexdigest()


def _canonical_decimal(value: Decimal) -> str:
    # Decimal.normalize() applies the active arithmetic context and can round
    # high-precision exchange metadata. Fixed-point formatting is exact.
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _finite_float(value: Decimal, *, field: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError(
            f"Deepcoin instrument {field} cannot be represented as a positive finite value"
        )
    return converted
