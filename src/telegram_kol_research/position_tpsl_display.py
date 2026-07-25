"""Read-only, exact-identity TPSL display joins for the position dashboard."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class PositionTpslDisplayRow:
    kind: str
    trigger_price_text: str
    size_text: str
    order_id: str
    ownership_state: str
    instrument_id: str | None = None
    side: str | None = None

    def as_dict(self) -> dict[str, str]:
        row = {
            "kind": self.kind,
            "trigger_price_text": self.trigger_price_text,
            "size_text": self.size_text,
            "order_id": self.order_id,
            "ownership_state": self.ownership_state,
        }
        if self.instrument_id is not None:
            row["instrument_id"] = self.instrument_id
        if self.side is not None:
            row["side"] = self.side
        return row


@dataclass(frozen=True, slots=True)
class PositionTpslDisplayResult:
    by_pos_id: dict[str, list[PositionTpslDisplayRow]]
    unattributed: list[PositionTpslDisplayRow]


def build_position_tpsl_display(
    *,
    positions: list[dict[str, Any]],
    pending_orders: list[dict[str, Any]],
    exact_order_position_ids: Mapping[str, object],
) -> PositionTpslDisplayResult:
    """Join TPSL rows only by exchange position ID or verified local order ID.

    Price, size, direction and creation time are display fields.  They never
    establish ownership, so an unscoped full-position stop remains global.
    """

    position_ids = {
        position_id
        for position in positions
        if (position_id := _first_text(position, "PositionID", "posId", "pos_id", "id"))
    }
    by_pos_id = {position_id: [] for position_id in position_ids}
    unattributed: list[PositionTpslDisplayRow] = []

    for order in pending_orders:
        if str(order.get("triggerOrderType") or "").upper() != "TPSL":
            continue
        order_id = _first_text(
            order,
            "OrderSysID",
            "ordId",
            "orderId",
            "order_id",
            "algoId",
            "triggerOrderId",
            "id",
        )
        position_id = _first_text(
            order,
            "PositionID",
            "closePosId",
            "close_pos_id",
            "closePositionId",
            "posId",
            "pos_id",
            "positionId",
        )
        if position_id is None and order_id:
            candidate = exact_order_position_ids.get(order_id)
            position_id = candidate if isinstance(candidate, str) and candidate.strip() else None

        rows = _split_order(order, order_id=order_id or "-")
        if position_id in by_pos_id:
            by_pos_id[position_id].extend(rows)
            continue
        instrument_id = str(order.get("instId") or order.get("InstrumentID") or "").upper()
        side = _normalize_side(order.get("posSide") or order.get("PosiDirection"))
        unattributed.extend(
            replace(
                row,
                ownership_state="无法归属",
                instrument_id=instrument_id,
                side=side,
            )
            for row in rows
        )

    return PositionTpslDisplayResult(
        by_pos_id={key: _sorted(rows) for key, rows in by_pos_id.items()},
        unattributed=_sorted(unattributed),
    )


def _split_order(
    order: dict[str, Any], *, order_id: str
) -> list[PositionTpslDisplayRow]:
    size = _first_text(order, "sz", "size", "Volume") or "0"
    result: list[PositionTpslDisplayRow] = []
    for kind, keys in (
        ("take_profit", ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice", "TPTriggerPrice")),
        ("stop_loss", ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice", "SLTriggerPrice")),
    ):
        trigger_price = _first_nonzero_text(order, *keys)
        if trigger_price is not None:
            result.append(
                PositionTpslDisplayRow(
                    kind=kind,
                    trigger_price_text=trigger_price,
                    size_text=size,
                    order_id=order_id,
                    ownership_state="已验证归属",
                )
            )
    return result


def _sorted(rows: list[PositionTpslDisplayRow]) -> list[PositionTpslDisplayRow]:
    state_sort = {"已验证归属": 0, "无法归属": 1}
    kind_sort = {"take_profit": 0, "stop_loss": 1}
    return sorted(
        rows,
        key=lambda row: (
            state_sort.get(row.ownership_state, 99),
            kind_sort.get(row.kind, 99),
            _numeric_sort_key(row.trigger_price_text),
            row.order_id,
        ),
    )


def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _first_nonzero_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _first_text(payload, key)
        if value is None:
            continue
        try:
            if float(value) != 0:
                return value
        except ValueError:
            return value
    return None


def _numeric_sort_key(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return float("inf")


def _normalize_side(value: object) -> str:
    side = str(value or "").lower()
    return {"buy": "long", "sell": "short"}.get(side, side)
