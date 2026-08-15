"""Bounded Deepcoin evidence refresh with an intentionally narrow read surface."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hmac
import json
import math
import re
import time
from typing import Any, Protocol, runtime_checkable

from telegram_kol_research.deepcoin_snapshot_authority import (
    ExchangeCollectionEvidence,
    build_exchange_collection_evidence,
)
from telegram_kol_research.production_monitor_snapshot import (
    ProductionMonitorSnapshotStore,
    SnapshotCollectionEvidence,
    SnapshotGeneration,
)


_UID_SCOPE_HASH = re.compile(r"[0-9a-f]{64}")
_MAX_PROJECTED_VALUE_BYTES = 1_024
_MAX_PROJECTED_ROW_BYTES = 16_384


@runtime_checkable
class DeepcoinMonitorReadProtocol(Protocol):
    """The complete exchange capability available to the refresher."""

    uid_scope_hash: str

    def read_positions(self, *, inst_id: str | None = None) -> dict[str, Any]: ...

    def read_open_orders(self, *, inst_id: str | None = None) -> dict[str, Any]: ...

    def read_trigger_orders_pending(self, *, inst_id: str) -> dict[str, Any]: ...


class ReadOnlyDeepcoinMonitorClient:
    """Capability wrapper exposing exactly three collection readers."""

    __slots__ = ("__transport", "uid_scope_hash")

    def __init__(self, transport: Any) -> None:
        for name in (
            "read_positions",
            "read_open_orders",
            "read_trigger_orders_pending",
        ):
            if not callable(getattr(transport, name, None)):
                raise ProductionMonitorRefreshConfigurationError(
                    "monitor read client is incomplete"
                )
        self.__transport = transport
        self.uid_scope_hash = getattr(transport, "uid_scope_hash", None)

    def read_positions(self, *, inst_id: str | None = None) -> dict[str, Any]:
        return self.__transport.read_positions(inst_id=inst_id)

    def read_open_orders(self, *, inst_id: str | None = None) -> dict[str, Any]:
        return self.__transport.read_open_orders(inst_id=inst_id)

    def read_trigger_orders_pending(self, *, inst_id: str) -> dict[str, Any]:
        return self.__transport.read_trigger_orders_pending(inst_id=inst_id)


class ProductionMonitorRefreshError(RuntimeError):
    """Base class for inability to complete and persist an attempt."""


class ProductionMonitorRefreshConfigurationError(ProductionMonitorRefreshError):
    """The refresher cannot safely initialize from its bounded inputs."""


class ProductionMonitorRefreshPersistenceError(ProductionMonitorRefreshError):
    """The completed attempt could not be made authoritative."""


@dataclass(frozen=True, slots=True)
class ProductionMonitorRefreshOutcome:
    execution_status: str
    snapshot_outcome: str
    generation: int
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class _CollectionSpec:
    name: str
    reader: Callable[[], object]


class _ClosedReadFailure(RuntimeError):
    def __init__(
        self,
        failure_code: str,
        collection: SnapshotCollectionEvidence | None = None,
    ) -> None:
        self.failure_code = failure_code
        self.collection = collection
        super().__init__(failure_code)


_FIELD_ALIASES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "positions": (
        ("posId", ("posId", "positionId", "position_id")),
        ("instId", ("instId", "instrumentId", "instrument_id")),
        ("posSide", ("posSide", "positionSide", "position_side")),
        ("mgnMode", ("mgnMode", "marginMode", "margin_mode")),
        ("pos", ("pos", "positionSize", "position_size", "size")),
        ("availPos", ("availPos", "availablePosition", "available_position")),
        ("avgPx", ("avgPx", "averagePrice", "average_price")),
        ("markPx", ("markPx", "markPrice", "mark_price")),
        ("liqPx", ("liqPx", "liquidationPrice", "liquidation_price")),
        ("lever", ("lever", "leverage")),
        ("upl", ("upl", "unrealizedPnl", "unrealized_pnl")),
        ("cTime", ("cTime", "createdAt", "created_at")),
        ("uTime", ("uTime", "updatedAt", "updated_at")),
    ),
    "open_orders": (
        ("ordId", ("ordId", "orderId", "order_id")),
        ("clOrdId", ("clOrdId", "clientOrderId", "client_order_id")),
        ("instId", ("instId", "instrumentId", "instrument_id")),
        ("posId", ("posId", "positionId", "position_id")),
        ("side", ("side",)),
        ("posSide", ("posSide", "positionSide", "position_side")),
        ("ordType", ("ordType", "orderType", "order_type")),
        ("state", ("state", "status")),
        ("sz", ("sz", "size")),
        ("fillSz", ("fillSz", "filledSize", "filled_size")),
        ("px", ("px", "price")),
        ("reduceOnly", ("reduceOnly", "reduce_only")),
        ("tpTriggerPx", ("tpTriggerPx", "takeProfitTriggerPrice")),
        ("slTriggerPx", ("slTriggerPx", "stopLossTriggerPrice")),
        ("cTime", ("cTime", "createdAt", "created_at")),
        ("uTime", ("uTime", "updatedAt", "updated_at")),
    ),
    "pending_trigger_orders": (
        ("ordId", ("ordId", "orderId", "order_id", "algoId", "triggerOrderId")),
        ("clOrdId", ("clOrdId", "clientOrderId", "client_order_id")),
        ("instId", ("instId", "instrumentId", "instrument_id")),
        ("posId", ("posId", "positionId", "position_id")),
        ("side", ("side",)),
        ("posSide", ("posSide", "positionSide", "position_side")),
        ("ordType", ("ordType", "orderType", "order_type")),
        ("state", ("state", "status")),
        ("sz", ("sz", "size")),
        ("triggerPx", ("triggerPx", "triggerPrice", "trigger_price")),
        ("tpTriggerPx", ("tpTriggerPx", "takeProfitTriggerPrice")),
        ("slTriggerPx", ("slTriggerPx", "stopLossTriggerPrice")),
        ("reduceOnly", ("reduceOnly", "reduce_only")),
        ("cTime", ("cTime", "createdAt", "created_at")),
        ("uTime", ("uTime", "updatedAt", "updated_at")),
    ),
}


def refresh_production_monitor_snapshot(
    *,
    client: DeepcoinMonitorReadProtocol,
    store: ProductionMonitorSnapshotStore,
    now: datetime | None = None,
    wall_clock_timeout_seconds: float = 45.0,
    monotonic_factory: Callable[[], float] = time.monotonic,
) -> ProductionMonitorRefreshOutcome:
    """Capture and seal one complete attempt without any database dependency."""

    timeout = _bounded_timeout(wall_clock_timeout_seconds)
    uid_scope_hash = _validated_scope_hash(getattr(client, "uid_scope_hash", None))
    timestamp_factory = (
        (lambda: now) if now is not None else (lambda: datetime.now(UTC))
    )
    try:
        manifest = store.load()
    except Exception as exc:
        raise ProductionMonitorRefreshPersistenceError(
            "monitor snapshot manifest cannot be loaded"
        ) from exc
    if manifest.uid_scope_hash is not None and not hmac.compare_digest(
        manifest.uid_scope_hash,
        uid_scope_hash,
    ):
        raise ProductionMonitorRefreshConfigurationError(
            "monitor snapshot account scope mismatch"
        )
    previous_generation = (
        -1 if manifest.latest_attempt is None else manifest.latest_attempt.generation
    )
    if previous_generation >= 2**63 - 1:
        raise ProductionMonitorRefreshConfigurationError(
            "monitor snapshot generation is exhausted"
        )
    generation = previous_generation + 1
    request_started_at = _aware_utc(timestamp_factory())
    started_monotonic = _finite_monotonic(monotonic_factory())

    specs = (
        _CollectionSpec(
            "positions",
            lambda: client.read_positions(inst_id=None),
        ),
        _CollectionSpec(
            "open_orders",
            lambda: client.read_open_orders(inst_id=None),
        ),
        _CollectionSpec(
            "pending_trigger_orders",
            lambda: client.read_trigger_orders_pending(inst_id=""),
        ),
    )
    collections: list[SnapshotCollectionEvidence] = []
    failure: _ClosedReadFailure | None = None
    for spec in specs:
        if _elapsed(monotonic_factory, started_monotonic) > timeout:
            failure = _ClosedReadFailure("wall_clock_timeout")
            break
        try:
            response = spec.reader()
        except Exception as exc:
            failure_code = _closed_exception_code(exc)
            failure = _ClosedReadFailure(
                failure_code,
                SnapshotCollectionEvidence(
                    name=spec.name,
                    available=False,
                    schema_valid=False,
                    complete=False,
                    page_count=0,
                    row_count=0,
                    rows=(),
                    reason_code=failure_code,
                ),
            )
            break
        if _elapsed(monotonic_factory, started_monotonic) > timeout:
            failure = _ClosedReadFailure("wall_clock_timeout")
            break
        try:
            collections.append(_complete_collection(spec.name, response))
        except _ClosedReadFailure as exc:
            failure = exc
            break

    request_completed_at = _aware_utc(timestamp_factory())
    if request_completed_at < request_started_at:
        failure = _ClosedReadFailure("snapshot_clock_invalid")
        request_completed_at = request_started_at

    if failure is None:
        envelope = SnapshotGeneration(
            generation=generation,
            outcome="SUCCESS",
            request_started_at=request_started_at,
            request_completed_at=request_completed_at,
            uid_scope_hash=uid_scope_hash,
            collections=tuple(collections),
        )
        try:
            store.seal_success(envelope)
        except Exception as exc:
            raise ProductionMonitorRefreshPersistenceError(
                "monitor snapshot success cannot be persisted"
            ) from exc
        return ProductionMonitorRefreshOutcome(
            execution_status="COMPLETED",
            snapshot_outcome="SUCCESS",
            generation=generation,
            failure_code=None,
        )

    failure_collections = () if failure.collection is None else (failure.collection,)
    envelope = SnapshotGeneration(
        generation=generation,
        outcome="FAILURE",
        request_started_at=request_started_at,
        request_completed_at=request_completed_at,
        uid_scope_hash=uid_scope_hash,
        collections=failure_collections,
        failure_code=failure.failure_code,
    )
    try:
        store.seal_failure(envelope)
    except Exception as exc:
        raise ProductionMonitorRefreshPersistenceError(
            "monitor snapshot failure cannot be persisted"
        ) from exc
    return ProductionMonitorRefreshOutcome(
        execution_status="COMPLETED",
        snapshot_outcome="FAILURE",
        generation=generation,
        failure_code=failure.failure_code,
    )


def _complete_collection(name: str, response: object) -> SnapshotCollectionEvidence:
    evidence = build_exchange_collection_evidence(endpoint=name, response=response)
    if not evidence.complete:
        reason = evidence.reason_code or "snapshot_schema_invalid"
        raise _ClosedReadFailure(
            reason,
            _failed_collection(
                name,
                evidence,
                reason,
                schema_valid=evidence.schema_valid,
            ),
        )
    try:
        rows = tuple(_project_row(name, row) for row in evidence.rows)
    except (TypeError, ValueError):
        reason = "snapshot_schema_invalid"
        raise _ClosedReadFailure(
            reason,
            _failed_collection(name, evidence, reason, schema_valid=False),
        ) from None
    return SnapshotCollectionEvidence(
        name=name,
        available=True,
        schema_valid=True,
        complete=True,
        page_count=evidence.page_count,
        row_count=len(rows),
        rows=rows,
    )


def _failed_collection(
    name: str,
    evidence: ExchangeCollectionEvidence,
    reason: str,
    *,
    schema_valid: bool,
) -> SnapshotCollectionEvidence:
    return SnapshotCollectionEvidence(
        name=name,
        available=evidence.available,
        schema_valid=schema_valid,
        complete=False,
        page_count=evidence.page_count,
        row_count=evidence.row_count,
        rows=(),
        reason_code=reason,
    )


def _project_row(name: str, row: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(row, Mapping):
        raise ValueError("snapshot row is invalid")
    projected: dict[str, Any] = {}
    for destination, aliases in _FIELD_ALIASES[name]:
        value = next((row[key] for key in aliases if row.get(key) not in (None, "")), None)
        if value is None:
            continue
        if not isinstance(value, (str, int, float, bool)) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            raise ValueError("snapshot row field is invalid")
        if isinstance(value, str):
            if len(value.encode("utf-8")) > _MAX_PROJECTED_VALUE_BYTES:
                raise ValueError("snapshot row field is too large")
            value = value.strip()
            if not value:
                continue
        projected[destination] = value
    identity_key = "posId" if name == "positions" else "ordId"
    identity = projected.get(identity_key)
    if not isinstance(identity, str) or not identity:
        raise ValueError("snapshot row identity is invalid")
    encoded = json.dumps(
        projected,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_PROJECTED_ROW_BYTES:
        raise ValueError("snapshot row is too large")
    return projected


def _validated_scope_hash(value: object) -> str:
    if not isinstance(value, str) or _UID_SCOPE_HASH.fullmatch(value) is None:
        raise ProductionMonitorRefreshConfigurationError(
            "monitor read client uid_scope_hash is invalid"
        )
    return value


def _bounded_timeout(value: object) -> float:
    if isinstance(value, bool):
        raise ProductionMonitorRefreshConfigurationError(
            "monitor refresh wall-clock timeout is invalid"
        )
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = math.nan
    if not math.isfinite(timeout) or not (0 < timeout <= 120):
        raise ProductionMonitorRefreshConfigurationError(
            "monitor refresh wall-clock timeout is invalid"
        )
    return timeout


def _aware_utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProductionMonitorRefreshConfigurationError(
            "monitor refresh clock is invalid"
        )
    return value.astimezone(UTC)


def _finite_monotonic(value: object) -> float:
    if isinstance(value, bool):
        raise ProductionMonitorRefreshConfigurationError(
            "monitor monotonic clock is invalid"
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = math.nan
    if not math.isfinite(parsed):
        raise ProductionMonitorRefreshConfigurationError(
            "monitor monotonic clock is invalid"
        )
    return parsed


def _elapsed(clock: Callable[[], float], started: float) -> float:
    current = _finite_monotonic(clock())
    if current < started:
        raise ProductionMonitorRefreshConfigurationError(
            "monitor monotonic clock moved backwards"
        )
    return current - started


def _closed_exception_code(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "exchange_timeout"
    safe_code = getattr(getattr(exc, "fact", None), "safe_code", None)
    if isinstance(safe_code, str):
        normalized = safe_code.lower()
        if "rate" in normalized or "throttle" in normalized:
            return "exchange_rate_limited"
        if "timeout" in normalized or "deadline" in normalized:
            return "exchange_timeout"
        if "credential" in normalized or "auth" in normalized:
            return "credential_invalid"
    return "snapshot_read_unavailable"
