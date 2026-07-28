"""Pure, fail-closed association of Deepcoin TPSL evidence to live positions."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(slots=True)
class PositionProtection:
    status: str
    stop_loss: float | None = None
    take_profits: list[float] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, object] = field(default_factory=dict)

    @property
    def can_mutate(self) -> bool:
        return self.status == "verified"


@dataclass(slots=True)
class ProtectionMatchResult:
    by_pos_id: dict[str, PositionProtection]


def snapshot_protection_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return stable, ordered TPSL rows suitable for mutation preflight.

    The snapshot intentionally keeps each exchange row separate.  In
    particular, multiple partial take-profit rows must never be collapsed into
    the last observed trigger price.
    """

    snapshots: list[dict[str, Any]] = []
    for row in rows:
        order_id = _first_text(
            row,
            "ordId",
            "orderId",
            "order_id",
            "algoId",
            "triggerOrderId",
            "id",
        )
        size = _first_text(row, "sz", "size") or "0"
        tp_price = _first_text(
            row, "tpTriggerPx", "tpTriggerPrice", "takeProfitPrice"
        )
        sl_price = _first_text(
            row, "slTriggerPx", "slTriggerPrice", "stopLossPrice"
        )
        if _nonzero_text(tp_price) is not None and _nonzero_text(sl_price) is not None:
            snapshots.append(
                {
                    "order_id": order_id,
                    "purpose": "combined",
                    "take_profit": {
                        "trigger_price": tp_price,
                        "trigger_type": _first_text(row, "tpTriggerPxType") or "last",
                        "order_price": _first_text(row, "tpOrdPx") or "-1",
                    },
                    "stop_loss": {
                        "trigger_price": sl_price,
                        "trigger_type": _first_text(row, "slTriggerPxType") or "last",
                        "order_price": _first_text(row, "slOrdPx") or "-1",
                    },
                    "size": size,
                    "full_position": _float_or_none(size) == 0,
                }
            )
            continue
        if _nonzero_text(tp_price) is not None:
            purpose = "take_profit"
            trigger_price = tp_price
            trigger_type = _first_text(row, "tpTriggerPxType") or "last"
            order_price = _first_text(row, "tpOrdPx") or "-1"
        elif _nonzero_text(sl_price) is not None:
            purpose = "stop_loss"
            trigger_price = sl_price
            trigger_type = _first_text(row, "slTriggerPxType") or "last"
            order_price = _first_text(row, "slOrdPx") or "-1"
        else:
            continue
        snapshots.append(
            {
                "order_id": order_id,
                "purpose": purpose,
                "trigger_price": trigger_price,
                "size": size,
                "full_position": _float_or_none(size) == 0,
                "trigger_type": trigger_type,
                "order_price": order_price,
            }
        )
    return snapshots


def normalize_protection_snapshot_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Canonicalize persisted protection snapshots from before zero-side filtering.

    Older snapshots represented a TPSL row with a disabled zero-valued side as
    ``combined``.  Current exchange snapshots omit that disabled side, so use
    the same canonical representation when comparing durable preflight state.
    """

    normalized: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        take_profit = row.get("take_profit")
        stop_loss = row.get("stop_loss")
        if (
            row.get("purpose") != "combined"
            or not isinstance(take_profit, dict)
            or not isinstance(stop_loss, dict)
        ):
            normalized.append(row)
            continue
        take_profit = dict(take_profit)
        stop_loss = dict(stop_loss)
        active_take_profit = _nonzero_text(take_profit.get("trigger_price"))
        active_stop_loss = _nonzero_text(stop_loss.get("trigger_price"))
        if active_take_profit is not None and active_stop_loss is not None:
            row["take_profit"] = take_profit
            row["stop_loss"] = stop_loss
            normalized.append(row)
            continue
        if active_take_profit is not None:
            normalized.append(
                _single_side_snapshot(row, purpose="take_profit", side=take_profit)
            )
            continue
        if active_stop_loss is not None:
            normalized.append(
                _single_side_snapshot(row, purpose="stop_loss", side=stop_loss)
            )
            continue
        normalized.append(row)
    return normalized


def _single_side_snapshot(
    row: dict[str, Any], *, purpose: str, side: dict[str, Any]
) -> dict[str, Any]:
    return {
        "order_id": row.get("order_id"),
        "purpose": purpose,
        "trigger_price": side.get("trigger_price"),
        "size": row.get("size"),
        "full_position": row.get("full_position"),
        "trigger_type": side.get("trigger_type") or "last",
        "order_price": side.get("order_price") or "-1",
    }


def _nonzero_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if Decimal(text) == 0:
            return None
    except InvalidOperation:
        pass
    return text


@dataclass(frozen=True, slots=True)
class _Position:
    pos_id: str


def match_position_protection(
    positions: list[dict[str, Any]],
    tpsl_orders: list[dict[str, Any]],
    *,
    evidence_available: bool = True,
    exact_order_position_ids: dict[str, str] | None = None,
) -> ProtectionMatchResult:
    """Match TPSL rows only through the canonical ``ordId → posId`` ledger.

    Exchange position IDs are validation evidence, not an ownership source.
    Price, size, instrument, side and timestamps never establish ownership.
    """

    parsed_positions = [_parse_position(row) for row in positions]
    parsed_positions = [row for row in parsed_positions if row is not None]
    by_pos_id = {
        row.pos_id: PositionProtection(
            status="absent" if evidence_available else "evidence_unavailable"
        )
        for row in parsed_positions
    }
    positions_by_id = {row.pos_id: row for row in parsed_positions}

    exact_rows: dict[str, list[dict[str, Any]]] = {
        row.pos_id: [] for row in parsed_positions
    }
    exact_order_position_ids = exact_order_position_ids or {}
    conflicting_pos_ids: set[str] = set()
    unowned_order_present = False
    for order in tpsl_orders:
        if str(order.get("triggerOrderType") or "TPSL").upper() != "TPSL":
            continue
        if not _row_has_protection(order):
            continue
        order_id = _first_text(
            order,
            "ordId",
            "orderId",
            "order_id",
            "algoId",
            "triggerOrderId",
            "id",
        )
        ledger_pos_id = exact_order_position_ids.get(order_id or "")
        if ledger_pos_id is None:
            unowned_order_present = True
            continue
        if ledger_pos_id not in positions_by_id:
            continue
        exchange_pos_id = _first_text(
            order,
            "closePosId",
            "close_pos_id",
            "closePositionId",
            "posId",
            "pos_id",
            "positionId",
        )
        if exchange_pos_id is not None and exchange_pos_id != ledger_pos_id:
            conflicting_pos_ids.add(ledger_pos_id)
            continue
        exact_rows[ledger_pos_id].append(order)

    for pos_id, rows in exact_rows.items():
        if pos_id in conflicting_pos_ids:
            by_pos_id[pos_id] = PositionProtection(
                status="present_but_ambiguous",
                evidence={"match": "ledger_exchange_position_conflict"},
            )
            continue
        if rows:
            if unowned_order_present:
                by_pos_id[pos_id] = PositionProtection(
                    status="present_but_ambiguous",
                    evidence={"match": "global_unowned_order_present"},
                )
                continue
            by_pos_id[pos_id] = _verified_protection(
                rows,
                evidence={
                    "match": "ledger_confirmed_current_order",
                    "pos_id": pos_id,
                },
            )

    return ProtectionMatchResult(by_pos_id=by_pos_id)


def _parse_position(row: dict[str, Any]) -> _Position | None:
    pos_id = _first_text(row, "posId", "pos_id", "id")
    if not pos_id:
        return None
    return _Position(pos_id=pos_id)


def _verified_protection(
    rows: list[dict[str, Any]], *, evidence: dict[str, object]
) -> PositionProtection:
    stop_losses = [_protection_price(row, "sl") for row in rows]
    take_profits = [_protection_price(row, "tp") for row in rows]
    stop_losses = [value for value in stop_losses if value is not None]
    take_profits = [value for value in take_profits if value is not None]
    return PositionProtection(
        status="verified",
        stop_loss=stop_losses[-1] if stop_losses else None,
        take_profits=_unique_floats(take_profits),
        order_ids=_order_ids(rows),
        rows=[dict(row) for row in rows],
        evidence=evidence,
    )


def _row_has_protection(row: dict[str, Any]) -> bool:
    return _protection_price(row, "sl") is not None or _protection_price(row, "tp") is not None


def _protection_price(row: dict[str, Any], kind: str) -> float | None:
    keys = (
        ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
        if kind == "sl"
        else ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")
    )
    for key in keys:
        value = _float_or_none(row.get(key))
        if value is not None and value != 0:
            return value
    return None


def _order_ids(rows: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for row in rows:
        order_id = _first_text(row, "ordId", "orderId", "order_id")
        if order_id and order_id not in result:
            result.append(order_id)
    return result


def _unique_floats(values: list[float]) -> list[float]:
    result: list[float] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return None
