"""Normalization and conservative attribution for DeepCoin native TPSL rows."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal


NativeTpslPurpose = Literal["stop_loss", "take_profit"]
NativeTpslMatchStatus = Literal["verified", "not_found", "ambiguous", "mismatch"]


@dataclass(frozen=True, slots=True)
class NativeTpslExpectation:
    """The exact native TPSL leg the system expects to find on the exchange."""

    purpose: NativeTpslPurpose
    trigger_price: Decimal | str | float
    size: Decimal | str | float
    ord_id: str | None = None

    def __post_init__(self) -> None:
        if self.purpose not in {"stop_loss", "take_profit"}:
            raise ValueError("native_tpsl_expectation_purpose_invalid")
        trigger_price = _decimal(self.trigger_price)
        size = _decimal(self.size)
        if trigger_price is None or trigger_price <= 0:
            raise ValueError("native_tpsl_expectation_trigger_price_invalid")
        if size is None or size < 0:
            raise ValueError("native_tpsl_expectation_size_invalid")
        object.__setattr__(self, "trigger_price", trigger_price)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "ord_id", _string(self.ord_id))


@dataclass(frozen=True, slots=True)
class NativeTpslOrder:
    """A normalized exchange TPSL row, preserving its original payload."""

    ord_id: str | None
    inst_id: str
    pos_side: str
    pos_id: str | None
    size: Decimal | None
    created_time: str | None
    stop_loss_trigger_price: Decimal | None
    take_profit_trigger_price: Decimal | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NativeTpslMatch:
    """The deterministic outcome of attempting to attribute one native TPSL row."""

    status: NativeTpslMatchStatus
    order: NativeTpslOrder | None


def protection_order_position_sides(payload: dict[str, Any]) -> set[str]:
    """Position-direction aliases only; an order's buy/sell is not an alias."""
    return {
        _normalize_side(str(payload[key]).strip())
        for key in ("posSide", "pos_side")
        if payload.get(key) is not None and str(payload[key]).strip()
    }


def protection_order_sides_consistent(payload: dict[str, Any]) -> bool:
    """Check protective close direction without inferring position ownership.

    Order side is optional in existing TPSL responses. When supplied it must
    be buy/sell opposite one explicit position direction, never a substitute
    for that direction. Call only for protection orders, not entry or position
    rows, whose side has a different meaning.
    """
    positions = protection_order_position_sides(payload)
    if len(positions) > 1 or not positions.issubset({"long", "short"}):
        return False
    value = payload.get("side")
    order_side = "" if value is None else str(value).strip().lower()
    if not order_side:
        return True
    return len(positions) == 1 and order_side == {
        "long": "sell", "short": "buy",
    }[next(iter(positions))]


def native_tpsl_order_id_is_unique(rows, order_id: str | None) -> bool:
    """Count exact identity claims BEFORE filtering malformed protective rows."""
    if not order_id:
        return False
    keys = ("OrderSysID", "ordId", "orderId", "order_id", "algoId", "triggerOrderId", "id")
    return sum(
        any(str(row[key]).strip() == str(order_id) for key in keys
            if row.get(key) is not None)
        for row in rows if isinstance(row, dict)
    ) == 1


def normalize_native_tpsl(payload: dict[str, Any]) -> NativeTpslOrder | None:
    """Return a normalized order only for DeepCoin's native ``TPSL`` records."""

    if str(payload.get("triggerOrderType") or "").upper() != "TPSL":
        return None
    return NativeTpslOrder(
        ord_id=_first_string(
            payload,
            "OrderSysID",
            "ordId",
            "orderId",
            "order_id",
            "algoId",
            "triggerOrderId",
            "id",
        ),
        inst_id=str(payload.get("instId") or payload.get("InstrumentID") or "").upper(),
        pos_side=_normalize_side(str(payload.get("posSide") or payload.get("PosiDirection") or "")),
        pos_id=_first_string(payload, "PositionID", "posId", "pos_id", "positionId"),
        size=_decimal(
            payload.get("sz")
            if payload.get("sz") not in (None, "")
            else payload.get("size")
            if payload.get("size") not in (None, "")
            else payload.get("Volume")
        ),
        created_time=_first_string(
            payload, "cTime", "uTime", "createdTime", "created_at", "CreateTime"
        ),
        stop_loss_trigger_price=_first_positive_decimal(
            payload,
            "SLTriggerPrice",
            "slTriggerPx",
            "slTriggerPrice",
            "closeSLTriggerPrice",
        ),
        take_profit_trigger_price=_first_positive_decimal(
            payload,
            "TPTriggerPrice",
            "tpTriggerPx",
            "tpTriggerPrice",
            "closeTPTriggerPrice",
        ),
        raw=payload,
    )


def native_tpsl_take_profit_is_market(payload: dict[str, Any]) -> bool:
    """Return whether every supplied DeepCoin TP execution-price field is market.

    DeepCoin has returned both ``tpOrdPx=-1`` and ``tpPrice=0`` for market
    TPSL exits.  If either representation is present, a non-market value must
    fail closed rather than silently turning a protective TP into a limit exit.
    """

    values = [payload.get(key) for key in ("tpOrdPx", "tpPrice") if payload.get(key) not in (None, "")]
    if not values:
        return False
    return all(
        (price := _decimal(value)) is not None and price in {Decimal("-1"), Decimal("0")}
        for value in values
    )


def match_native_tpsl_order(
    position: dict[str, Any],
    orders: list[dict[str, Any]],
    expected: NativeTpslExpectation,
    *,
    open_positions: list[dict[str, Any]] | None = None,
) -> NativeTpslMatch:
    """Validate one ledger-owned native TPSL order without inferring ownership."""

    normalized = [order for raw in orders if (order := normalize_native_tpsl(raw))]
    if not expected.ord_id:
        return NativeTpslMatch(status="not_found", order=None)
    exact = [order for order in normalized if order.ord_id == expected.ord_id]
    if len(exact) > 1:
        return NativeTpslMatch(status="ambiguous", order=None)
    if not exact:
        return NativeTpslMatch(status="not_found", order=None)
    order = exact[0]
    return NativeTpslMatch(
        status="verified" if _exact_order_matches(position, order, expected) else "mismatch",
        order=order,
    )


def _exact_order_matches(
    position: dict[str, Any],
    order: NativeTpslOrder,
    expected: NativeTpslExpectation,
) -> bool:
    position_inst_id = str(position.get("instId") or "").upper()
    position_side = _normalize_side(str(position.get("posSide") or ""))
    position_pos_id = _first_string(position, "posId", "pos_id", "id")
    if not position_inst_id or not position_side or not order.inst_id or not order.pos_side:
        return False
    if order.inst_id != position_inst_id or order.pos_side != position_side:
        return False
    if position_pos_id and order.pos_id and position_pos_id != order.pos_id:
        return False
    return _leg_matches(order, expected)


def _leg_matches(order: NativeTpslOrder, expected: NativeTpslExpectation) -> bool:
    actual_price = (
        order.stop_loss_trigger_price
        if expected.purpose == "stop_loss"
        else order.take_profit_trigger_price
    )
    return actual_price == expected.trigger_price and order.size == expected.size


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _first_positive_decimal(payload: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        value = _decimal(payload.get(key))
        if value is not None and value > 0:
            return value
    return None


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _string(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _normalize_side(value: str) -> str:
    side = value.lower()
    if side == "buy":
        return "long"
    if side == "sell":
        return "short"
    return side
