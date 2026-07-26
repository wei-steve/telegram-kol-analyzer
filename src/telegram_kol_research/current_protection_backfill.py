"""Read-only, supervised plan for current DeepCoin TPSL backfill."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class SupervisedProtectionMapping:
    """An operator-reviewed exchange order to position association."""

    order_id: str
    pos_id: str
    evidence_hash: str


@dataclass(frozen=True, slots=True)
class CurrentProtectionBackfillAction:
    order_id: str
    pos_id: str
    classification: str
    instrument_id: str
    side: str
    evidence_hash: str


@dataclass(frozen=True, slots=True)
class CurrentProtectionBackfillRefusal:
    order_id: str
    pos_id: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CurrentProtectionBackfillPlan:
    actions: tuple[CurrentProtectionBackfillAction, ...]
    refusals: tuple[CurrentProtectionBackfillRefusal, ...]
    fingerprint: str


def build_current_protection_backfill_plan(
    *,
    mappings: Iterable[SupervisedProtectionMapping],
    positions: Iterable[dict[str, Any]],
    pending_orders: Iterable[dict[str, Any]],
    verified_order_ids: set[str],
) -> CurrentProtectionBackfillPlan:
    """Validate explicit mappings against current snapshots without writes.

    No association is inferred from order attributes. An action exists only for
    the exact order-to-position pair passed by the supervising operator.
    """

    positions_by_id = {
        pos_id: row
        for row in positions
        if isinstance(row, dict)
        if (pos_id := _text(row, "posId", "pos_id", "PositionID", "id"))
        and _nonzero_position(row)
    }
    orders_by_id = {
        order_id: row
        for row in pending_orders
        if isinstance(row, dict)
        if (order_id := _text(row, "ordId", "orderId", "order_id", "OrderSysID", "id"))
    }
    actions: list[CurrentProtectionBackfillAction] = []
    refusals: list[CurrentProtectionBackfillRefusal] = []
    seen_order_ids: set[str] = set()
    for mapping in mappings:
        order_id = str(mapping.order_id or "").strip()
        pos_id = str(mapping.pos_id or "").strip()
        evidence_hash = str(mapping.evidence_hash or "").strip()
        if not order_id or not pos_id or not evidence_hash:
            refusals.append(CurrentProtectionBackfillRefusal(order_id, pos_id, "missing_explicit_mapping"))
        elif order_id in seen_order_ids:
            refusals.append(CurrentProtectionBackfillRefusal(order_id, pos_id, "duplicate_explicit_order"))
        elif order_id in verified_order_ids:
            refusals.append(CurrentProtectionBackfillRefusal(order_id, pos_id, "already_verified"))
        elif (position := positions_by_id.get(pos_id)) is None:
            refusals.append(CurrentProtectionBackfillRefusal(order_id, pos_id, "active_position_missing"))
        elif (order := orders_by_id.get(order_id)) is None:
            refusals.append(CurrentProtectionBackfillRefusal(order_id, pos_id, "pending_order_missing"))
        else:
            position_instrument, order_instrument = _instrument(position), _instrument(order)
            position_side, order_side = _side(position), _side(order)
            if (
                not position_instrument
                or position_instrument != order_instrument
                or not position_side
                or position_side != order_side
            ):
                refusals.append(
                    CurrentProtectionBackfillRefusal(
                        order_id,
                        pos_id,
                        "exchange_identity_mismatch",
                        {
                            "position_instrument": position_instrument,
                            "order_instrument": order_instrument,
                            "position_side": position_side,
                            "order_side": order_side,
                        },
                    )
                )
            else:
                actions.append(
                    CurrentProtectionBackfillAction(
                        order_id, pos_id, "review", position_instrument, position_side, evidence_hash
                    )
                )
        seen_order_ids.add(order_id)
    actions_tuple = tuple(sorted(actions, key=lambda row: (row.pos_id, row.order_id)))
    refusals_tuple = tuple(sorted(refusals, key=lambda row: (row.pos_id, row.order_id, row.reason)))
    return CurrentProtectionBackfillPlan(
        actions_tuple,
        refusals_tuple,
        _fingerprint(actions_tuple, refusals_tuple),
    )


def _text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _nonzero_position(payload: dict[str, Any]) -> bool:
    try:
        return float(_text(payload, "pos", "size", "positionSize", "Volume")) != 0
    except ValueError:
        return False


def _instrument(payload: dict[str, Any]) -> str:
    return _text(payload, "instId", "instrumentId", "InstrumentID").upper()


def _side(payload: dict[str, Any]) -> str:
    value = _text(payload, "posSide", "side", "PosiDirection").lower()
    return {"buy": "long", "sell": "short"}.get(value, value)


def _fingerprint(
    actions: tuple[CurrentProtectionBackfillAction, ...],
    refusals: tuple[CurrentProtectionBackfillRefusal, ...],
) -> str:
    payload = {"actions": [asdict(row) for row in actions], "refusals": [asdict(row) for row in refusals]}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
