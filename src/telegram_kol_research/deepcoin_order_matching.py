"""Deepcoin order / position matching helpers for TP/SL management.

Deepcoin exposes entry orders, positions, and pending TP/SL trigger orders as
separate payloads.  These helpers keep the matching rules explicit so later KOL
management signals can adjust the intended protection order without guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telegram_kol_research.deepcoin_readonly import DeepcoinOrderBinding
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
    owned_order_ids: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Return only pending TPSL rows with exact position or ledger ownership."""

    target_pos_id = _first_string(position, "posId", "pos_id", "id")
    matches: list[dict[str, Any]] = []
    for order in pending_trigger_orders:
        native_order = normalize_native_tpsl(order)
        if native_order is None:
            continue
        ledger_owned = bool(
            native_order.ord_id and native_order.ord_id in owned_order_ids
        )
        exchange_scoped = bool(
            target_pos_id and native_order.pos_id == target_pos_id
        )
        if ledger_owned or exchange_scoped:
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
    target_pos_id: str | None = None,
    ledger_owned_order_ids: set[str] | frozenset[str] = frozenset(),
) -> StopLossAdjustmentTarget:
    """Find a stop-loss target from exact ``posId`` and ledger-owned ``ordId``."""

    protection_orders = [
        order
        for order in extract_pending_protection_orders(pending_trigger_orders)
        if order.purpose == "stop_loss"
    ]
    direct_id_matches = [
        order
        for order in protection_orders
        if order.trigger_order_id in ledger_owned_order_ids
    ]
    if direct_id_matches:
        return StopLossAdjustmentTarget(
            action="replace_pending_stop_loss",
            reason="matched_pending_stop_loss_by_ledger_order_id",
            order=_single_or_raise(direct_id_matches, "multiple_stop_loss_orders_for_order_id"),
        )

    exact_pos_id = str(target_pos_id or "").strip()
    position_matches = [
        order
        for order in protection_orders
        if exact_pos_id and order.pos_id == exact_pos_id
    ]
    if position_matches:
        return StopLossAdjustmentTarget(
            action="replace_pending_stop_loss",
            reason="matched_pending_stop_loss_by_pos_id",
            order=_single_or_raise(position_matches, "multiple_stop_loss_orders_for_pos_id"),
        )

    raise DeepcoinOrderMatchError("no_deepcoin_stop_loss_adjustment_target")


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


def _normalize_size(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)
