"""Deepcoin order / position matching helpers for TP/SL management.

Deepcoin exposes entry orders, positions, and pending TP/SL trigger orders as
separate payloads.  These helpers keep the matching rules explicit so later KOL
management signals can adjust the intended protection order without guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from telegram_kol_research.deepcoin_readonly import DeepcoinOrderBinding
from telegram_kol_research.native_tpsl import native_tpsl_belongs_to_position
from telegram_kol_research.native_tpsl import normalize_native_tpsl


@dataclass(slots=True)
class DeepcoinProtectionOrder:
    purpose: str
    trigger_order_id: str | None
    client_order_id: str | None
    inst_id: str
    pos_side: str
    trigger_price: float
    size: str | None
    pos_id: str | None
    created_time: str | None
    raw: dict[str, Any]


@dataclass(slots=True)
class StopLossAdjustmentTarget:
    action: str
    reason: str
    order: DeepcoinProtectionOrder | None = None
    pos_id: str | None = None
    order_id: str | None = None


class DeepcoinOrderMatchError(ValueError):
    """Raised when a Deepcoin order match is ambiguous or impossible."""


def extract_pending_protection_orders(
    pending_trigger_orders: list[dict[str, Any]],
) -> list[DeepcoinProtectionOrder]:
    """Normalize Deepcoin pending TPSL trigger orders into one row per TP/SL leg."""

    orders: list[DeepcoinProtectionOrder] = []
    for raw in pending_trigger_orders:
        native_order = normalize_native_tpsl(raw)
        if native_order:
            base = {
                "trigger_order_id": native_order.ord_id,
                "client_order_id": _first_string(
                    raw,
                    "clOrdId",
                    "clientOrderId",
                    "client_order_id",
                ),
                "inst_id": native_order.inst_id,
                "pos_side": native_order.pos_side,
                "size": _normalize_size(native_order.size),
                "pos_id": native_order.pos_id,
                "created_time": native_order.created_time,
            }
            if native_order.stop_loss_trigger_price is not None:
                orders.append(
                    DeepcoinProtectionOrder(
                        purpose="stop_loss",
                        trigger_price=float(native_order.stop_loss_trigger_price),
                        raw=raw,
                        **base,
                    )
                )
            if native_order.take_profit_trigger_price is not None:
                orders.append(
                    DeepcoinProtectionOrder(
                        purpose="take_profit",
                        trigger_price=float(native_order.take_profit_trigger_price),
                        raw=raw,
                        **base,
                    )
                )
            continue
        if str(raw.get("triggerOrderType") or raw.get("ordType") or "").upper() not in {
            "TPSL",
            "TP",
            "SL",
            "STOP",
            "",
        }:
            continue
        base = {
            "trigger_order_id": _first_string(
                raw,
                "ordId",
                "orderId",
                "order_id",
                "algoId",
                "triggerOrderId",
                "orderSysID",
                "OrderSysID",
                "id",
            ),
            "client_order_id": _first_string(raw, "clOrdId", "clientOrderId", "client_order_id"),
            "inst_id": str(raw.get("instId") or "").upper(),
            "pos_side": str(raw.get("posSide") or raw.get("side") or "").lower(),
            "size": _normalize_size(raw.get("sz") or raw.get("size")),
            "pos_id": _first_string(raw, "posId", "pos_id", "positionId"),
            "created_time": _first_string(raw, "cTime", "uTime", "createdTime", "created_at"),
        }
        stop_price = _first_price(raw, "slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
        if stop_price is not None:
            orders.append(
                DeepcoinProtectionOrder(
                    purpose="stop_loss",
                    trigger_price=stop_price,
                    raw=raw,
                    **base,
                )
            )
        take_profit_price = _first_price(
            raw,
            "tpTriggerPx",
            "tpTriggerPrice",
            "closeTPTriggerPrice",
        )
        if take_profit_price is not None:
            orders.append(
                DeepcoinProtectionOrder(
                    purpose="take_profit",
                    trigger_price=take_profit_price,
                    raw=raw,
                    **base,
                )
            )
    return orders


def select_position_tpsl_orders(
    *,
    position: dict[str, Any],
    pending_trigger_orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return raw pending TPSL orders that belong to one split position.

    Deepcoin's pending TPSL rows may not include ``posId``.  In that case the
    stable observed link is instrument + position side + position creation time,
    with size either matching the position or reported as zero.
    """

    matches: list[dict[str, Any]] = []
    for order in pending_trigger_orders:
        native_order = normalize_native_tpsl(order)
        if (
            native_order
            and not (native_order.pos_id is None and native_order.size == Decimal("0"))
            and native_tpsl_belongs_to_position(position, native_order)
        ):
            matches.append(order)
    return matches


def pending_tpsl_order_ids_for_position(
    *,
    position: dict[str, Any],
    pending_trigger_orders: list[dict[str, Any]],
) -> list[str]:
    """Return unique pending TPSL order ids for one position."""

    ids: list[str] = []
    for order in select_position_tpsl_orders(
        position=position,
        pending_trigger_orders=pending_trigger_orders,
    ):
        order_id = _first_string(
            order,
            "ordId",
            "orderId",
            "order_id",
            "algoId",
            "triggerOrderId",
            "id",
        )
        if order_id and order_id not in ids:
            ids.append(order_id)
    return ids


def resolve_stop_loss_adjustment_target(
    *,
    binding: DeepcoinOrderBinding,
    pending_trigger_orders: list[dict[str, Any]],
    live_positions: list[dict[str, Any]] | None = None,
) -> StopLossAdjustmentTarget:
    """Find the exact Deepcoin target for a later stop-loss update signal.

    Matching priority:
    1. Existing pending SL trigger order tied to this binding by order/client id.
    2. Existing pending SL trigger order tied to this binding by position id.
    3. Existing pending SL trigger order uniquely matching instrument + side.
    4. Active split-position fallback: use set-position-sltp with posId.
    5. Open entry-order fallback: use replace-order-sltp with orderSysID.
    """

    protection_orders = [
        order
        for order in extract_pending_protection_orders(pending_trigger_orders)
        if order.purpose == "stop_loss"
    ]
    direct_id_matches = _match_by_known_order_ids(binding, protection_orders)
    if direct_id_matches:
        return StopLossAdjustmentTarget(
            action="replace_pending_stop_loss",
            reason="matched_pending_stop_loss_by_order_id",
            order=_single_or_raise(direct_id_matches, "multiple_stop_loss_orders_for_order_id"),
        )

    position_matches = _match_by_position_ids(binding, protection_orders)
    if position_matches:
        return StopLossAdjustmentTarget(
            action="replace_pending_stop_loss",
            reason="matched_pending_stop_loss_by_pos_id",
            order=_single_or_raise(position_matches, "multiple_stop_loss_orders_for_pos_id"),
        )

    symbol_side_matches = _match_by_symbol_side(binding, protection_orders)
    if symbol_side_matches:
        return StopLossAdjustmentTarget(
            action="replace_pending_stop_loss",
            reason="matched_pending_stop_loss_by_unique_symbol_side",
            order=_single_or_raise(
                symbol_side_matches,
                "ambiguous_stop_loss_orders_for_symbol_side",
            ),
        )

    live_pos_id = _first_live_position_id(binding, live_positions or [])
    if live_pos_id:
        return StopLossAdjustmentTarget(
            action="set_position_sltp",
            reason="no_pending_stop_loss_but_active_position_found",
            pos_id=live_pos_id,
        )

    raise DeepcoinOrderMatchError("no_deepcoin_stop_loss_adjustment_target")


def _match_by_known_order_ids(
    binding: DeepcoinOrderBinding,
    orders: list[DeepcoinProtectionOrder],
) -> list[DeepcoinProtectionOrder]:
    order_ids = set(_split_ids(binding.order_id))
    client_order_ids = set(_split_ids(binding.client_order_id))
    return [
        order
        for order in orders
        if (order.trigger_order_id and order.trigger_order_id in order_ids)
        or (order.client_order_id and order.client_order_id in client_order_ids)
    ]


def _match_by_position_ids(
    binding: DeepcoinOrderBinding,
    orders: list[DeepcoinProtectionOrder],
) -> list[DeepcoinProtectionOrder]:
    pos_ids = set(_split_ids(binding.pos_id))
    if not pos_ids:
        return []
    return [order for order in orders if order.pos_id and order.pos_id in pos_ids]


def _match_by_symbol_side(
    binding: DeepcoinOrderBinding,
    orders: list[DeepcoinProtectionOrder],
) -> list[DeepcoinProtectionOrder]:
    expected_symbol = binding.symbol.upper()
    expected_side = binding.side.lower()
    return [
        order
        for order in orders
        if _symbol_from_inst_id(order.inst_id) == expected_symbol
        and _normalize_side(order.pos_side) == expected_side
    ]


def _first_live_position_id(
    binding: DeepcoinOrderBinding,
    positions: list[dict[str, Any]],
) -> str | None:
    pos_ids = set(_split_ids(binding.pos_id))
    for position in positions:
        pos_id = _first_string(position, "posId", "pos_id", "id")
        if pos_ids and pos_id not in pos_ids:
            continue
        if _symbol_from_inst_id(position.get("instId")) != binding.symbol.upper():
            continue
        if _normalize_side(str(position.get("posSide") or position.get("side") or "")) != binding.side.lower():
            continue
        if _has_nonzero_size(position):
            return pos_id
    return None


def _single_or_raise(
    orders: list[DeepcoinProtectionOrder],
    reason: str,
) -> DeepcoinProtectionOrder:
    if len(orders) == 1:
        return orders[0]
    raise DeepcoinOrderMatchError(reason)


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _first_price(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


def _split_ids(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _normalize_size(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _symbol_from_inst_id(value: Any) -> str:
    text = str(value or "").upper()
    return text.split("-")[0] if text else ""


def _normalize_side(value: str) -> str:
    side = value.lower()
    if side == "buy":
        return "long"
    if side == "sell":
        return "short"
    return side


def _has_nonzero_size(position: dict[str, Any]) -> bool:
    try:
        return abs(float(position.get("pos") or position.get("size") or 0)) > 0
    except (TypeError, ValueError):
        return False
