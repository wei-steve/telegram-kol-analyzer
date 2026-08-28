"""Bounded, freshness-aware Deepcoin evidence for maintenance actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any, Callable, Iterable, Literal


_MAX_EVIDENCE_AGE = timedelta(seconds=30)
_PAGE_LIMIT = 100


class DeepcoinMaintenanceEvidenceRefused(RuntimeError):
    """Required read-only exchange evidence was incomplete or stale."""


@dataclass(frozen=True, slots=True)
class DeepcoinMaintenanceEvidence:
    observed_at: datetime
    target_order_id: str
    status: Literal["complete", "unknown"]
    reason_code: str | None
    positions: tuple[dict[str, Any], ...]
    regular_orders: tuple[dict[str, Any], ...]
    pending_triggers: tuple[dict[str, Any], ...]
    trigger_history: tuple[dict[str, Any], ...]
    fills: tuple[dict[str, Any], ...]
    retry_count: int
    fingerprint: str

    @property
    def position_count(self) -> int:
        return len(self.positions)

    @property
    def regular_order_count(self) -> int:
        return len(self.regular_orders)

    @property
    def target_pending_count(self) -> int:
        return sum(
            1
            for row in self.pending_triggers
            if _order_id(row) == self.target_order_id
        )

    @property
    def pending_order_ids(self) -> tuple[str, ...]:
        return tuple(_order_id(row) for row in self.pending_triggers)


def build_deepcoin_maintenance_evidence(
    client,
    *,
    instruments: Iterable[str],
    target_order_id: str,
    observed_at: datetime,
    expected_target_pending_count: int | None = 1,
) -> DeepcoinMaintenanceEvidence:
    """Read one bounded account snapshot with at most one retry per query."""

    clean_instruments = tuple(
        sorted({str(value or "").strip().upper() for value in instruments})
    )
    if not clean_instruments or any(not value for value in clean_instruments):
        raise ValueError("maintenance evidence instruments are invalid")
    clean_target = str(target_order_id or "").strip()
    if not clean_target:
        raise ValueError("maintenance evidence target is required")
    if expected_target_pending_count not in {None, 0, 1}:
        raise ValueError("expected target pending count is invalid")
    timestamp = _timestamp(observed_at)
    positions: list[dict[str, Any]] = []
    regular: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    retries = 0

    readers: tuple[
        tuple[str, list[dict[str, Any]], Callable[[str], object]], ...
    ] = (
        (
            "positions",
            positions,
            lambda instrument: client.list_positions(inst_id=instrument),
        ),
        (
            "regular",
            regular,
            lambda instrument: client.list_open_orders(inst_id=instrument),
        ),
        (
            "pending",
            pending,
            lambda instrument: _read_pending(client, instrument),
        ),
        (
            "history",
            history,
            lambda instrument: client.list_trigger_order_history(
                inst_id=instrument
            ),
        ),
        (
            "fills",
            fills,
            lambda instrument: client.list_trade_fills(inst_id=instrument),
        ),
    )
    for query_kind, destination, reader in readers:
        for instrument in clean_instruments:
            rows, retried, complete = _read_complete_rows(reader, instrument)
            retries += int(retried)
            destination.extend(rows)
            if not complete:
                return _evidence(
                    observed_at=timestamp,
                    target_order_id=clean_target,
                    status="unknown",
                    reason_code=f"{query_kind}_query_incomplete",
                    positions=positions,
                    regular_orders=regular,
                    pending_triggers=pending,
                    trigger_history=history,
                    fills=fills,
                    retry_count=retries,
                )

    target_count = sum(1 for row in pending if _order_id(row) == clean_target)
    if (
        expected_target_pending_count is not None
        and target_count != expected_target_pending_count
    ):
        return _evidence(
            observed_at=timestamp,
            target_order_id=clean_target,
            status="unknown",
            reason_code="target_pending_readback_not_exact",
            positions=positions,
            regular_orders=regular,
            pending_triggers=pending,
            trigger_history=history,
            fills=fills,
            retry_count=retries,
        )
    return _evidence(
        observed_at=timestamp,
        target_order_id=clean_target,
        status="complete",
        reason_code=None,
        positions=positions,
        regular_orders=regular,
        pending_triggers=pending,
        trigger_history=history,
        fills=fills,
        retry_count=retries,
    )


def require_fresh_deepcoin_maintenance_evidence(
    evidence: DeepcoinMaintenanceEvidence,
    *,
    now: datetime,
) -> None:
    observed_now = _timestamp(now)
    if evidence.status != "complete":
        raise DeepcoinMaintenanceEvidenceRefused(
            evidence.reason_code or "evidence_unknown"
        )
    age = observed_now - evidence.observed_at
    if age < timedelta(0) or age > _MAX_EVIDENCE_AGE:
        raise DeepcoinMaintenanceEvidenceRefused("evidence_stale")


def require_canonical_remaining_pending_set(
    evidence: DeepcoinMaintenanceEvidence,
    *,
    canonical_order_ids: Iterable[str],
    completed_order_ids: Iterable[str],
) -> None:
    canonical = {str(value) for value in canonical_order_ids}
    completed = {str(value) for value in completed_order_ids}
    observed = evidence.pending_order_ids
    if (
        evidence.status != "complete"
        or "" in observed
        or len(observed) != len(set(observed))
        or completed - canonical
        or set(observed) != canonical - completed
    ):
        raise DeepcoinMaintenanceEvidenceRefused(
            "remaining_pending_set_mismatch"
        )


def _read_complete_rows(
    reader: Callable[[str], object],
    instrument: str,
) -> tuple[list[dict[str, Any]], bool, bool]:
    last_rows: list[dict[str, Any]] = []
    for attempt in range(2):
        try:
            raw = reader(instrument)
            rows, complete = _rows_and_completeness(raw)
        except Exception:
            rows, complete = [], False
        last_rows = rows
        if complete:
            return rows, attempt == 1, True
    return last_rows, True, False


def _read_pending(client, instrument: str) -> object:
    raw_reader = getattr(client, "read_trigger_orders_pending", None)
    if callable(raw_reader):
        return raw_reader(inst_id=instrument)
    return client.list_trigger_orders_pending(inst_id=instrument)


def _rows_and_completeness(value: object) -> tuple[list[dict[str, Any]], bool]:
    if isinstance(value, dict):
        if str(value.get("code", "0")) != "0":
            return [], False
        raw_rows = value.get("data")
        page_incomplete = any(
            value.get(key) not in (None, "", False, 0, "0")
            for key in ("nextCursor", "nextPageCursor", "hasMore")
        )
    else:
        raw_rows = value
        page_incomplete = False
    if not isinstance(raw_rows, list) or not all(
        isinstance(row, dict) for row in raw_rows
    ):
        return [], False
    rows = [dict(row) for row in raw_rows]
    return rows, not page_incomplete and len(rows) < _PAGE_LIMIT


def _evidence(
    *,
    observed_at: datetime,
    target_order_id: str,
    status: Literal["complete", "unknown"],
    reason_code: str | None,
    positions: Iterable[dict[str, Any]],
    regular_orders: Iterable[dict[str, Any]],
    pending_triggers: Iterable[dict[str, Any]],
    trigger_history: Iterable[dict[str, Any]],
    fills: Iterable[dict[str, Any]],
    retry_count: int,
) -> DeepcoinMaintenanceEvidence:
    payload: dict[str, Any] = {
        "fills": list(fills),
        "observed_at": observed_at.isoformat(),
        "pending_triggers": list(pending_triggers),
        "positions": list(positions),
        "reason_code": reason_code,
        "regular_orders": list(regular_orders),
        "retry_count": retry_count,
        "status": status,
        "target_order_id": target_order_id,
        "trigger_history": list(trigger_history),
    }
    fingerprint_material = dict(payload)
    fingerprint_material.pop("observed_at")
    return DeepcoinMaintenanceEvidence(
        observed_at=observed_at,
        target_order_id=target_order_id,
        status=status,
        reason_code=reason_code,
        positions=tuple(dict(row) for row in payload["positions"]),
        regular_orders=tuple(dict(row) for row in payload["regular_orders"]),
        pending_triggers=tuple(dict(row) for row in payload["pending_triggers"]),
        trigger_history=tuple(dict(row) for row in payload["trigger_history"]),
        fills=tuple(dict(row) for row in payload["fills"]),
        retry_count=retry_count,
        fingerprint=hashlib.sha256(
            _canonical_json(fingerprint_material)
        ).hexdigest(),
    )


def _order_id(row: dict[str, Any]) -> str:
    return str(
        row.get("ordId") or row.get("orderId") or row.get("order_id") or ""
    ).strip()


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("maintenance evidence timestamp is invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
