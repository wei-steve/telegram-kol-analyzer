"""Read-only Deepcoin account state adapters.

This module only normalizes data returned by an injected client. It does not
perform authentication, network requests, or trading actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from telegram_kol_research.recovery_scan import OpenOrder
from telegram_kol_research.trading_decision import ActivePosition


@dataclass(slots=True)
class DeepcoinOrderBinding:
    kol_id: str
    chat_id: int
    source_message_id: int
    symbol: str
    side: str
    pos_id: str | None = None
    order_id: str | None = None


class DeepcoinReadOnlyClient(Protocol):
    def list_positions(self) -> list[dict[str, Any]]:
        """Return raw Deepcoin position payloads."""

    def list_open_orders(self) -> list[dict[str, Any]]:
        """Return raw Deepcoin open-order payloads."""


class DeepcoinReadOnlyAccountState:
    """Adapt an injected Deepcoin read-only client into recovery account state."""

    def __init__(
        self,
        *,
        client: DeepcoinReadOnlyClient,
        bindings: list[DeepcoinOrderBinding],
    ) -> None:
        self._client = client
        self._bindings = bindings

    def load_active_positions(self) -> list[ActivePosition]:
        return map_deepcoin_positions(
            self._client.list_positions(),
            bindings=self._bindings,
        )

    def load_open_orders(self) -> list[OpenOrder]:
        return map_deepcoin_open_orders(
            self._client.list_open_orders(),
            bindings=self._bindings,
        )


def map_deepcoin_positions(
    positions: list[dict[str, Any]],
    *,
    bindings: list[DeepcoinOrderBinding],
) -> list[ActivePosition]:
    """Map raw Deepcoin positions to bound active-position summaries."""

    bindings_by_pos_id = {
        binding.pos_id: binding
        for binding in bindings
        if binding.pos_id
    }
    active_positions: list[ActivePosition] = []
    for position in positions:
        pos_id = _first_string(position, "posId", "pos_id", "id")
        if not pos_id:
            continue
        binding = bindings_by_pos_id.get(pos_id)
        if binding is None or not _has_nonzero_size(position):
            continue
        active_positions.append(
            ActivePosition(
                kol_id=binding.kol_id,
                chat_id=binding.chat_id,
                symbol=binding.symbol.upper() or _symbol_from_inst_id(position.get("instId")),
                side=binding.side.lower() or _side_from_payload(position),
                pos_id=pos_id,
            )
        )
    return active_positions


def map_deepcoin_open_orders(
    orders: list[dict[str, Any]],
    *,
    bindings: list[DeepcoinOrderBinding],
) -> list[OpenOrder]:
    """Map raw Deepcoin open orders to bound recovery open-order summaries."""

    bindings_by_order_id = {
        binding.order_id: binding
        for binding in bindings
        if binding.order_id
    }
    open_orders: list[OpenOrder] = []
    for order in orders:
        order_id = _first_string(order, "ordId", "orderId", "order_id", "id")
        if not order_id:
            continue
        binding = bindings_by_order_id.get(order_id)
        if binding is None or not _is_open_order_state(order):
            continue
        open_orders.append(
            OpenOrder(
                kol_id=binding.kol_id,
                chat_id=binding.chat_id,
                source_message_id=binding.source_message_id,
                symbol=binding.symbol.upper() or _symbol_from_inst_id(order.get("instId")),
                side=binding.side.lower() or _side_from_payload(order),
                order_id=order_id,
            )
        )
    return open_orders


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _has_nonzero_size(position: dict[str, Any]) -> bool:
    size = position.get("pos")
    if size in (None, ""):
        size = position.get("size")
    try:
        return abs(float(size or 0)) > 0
    except (TypeError, ValueError):
        return False


def _is_open_order_state(order: dict[str, Any]) -> bool:
    state = str(order.get("state") or order.get("status") or "").lower()
    if not state:
        return True
    return state in {"live", "open", "partially_filled", "partial"}


def _symbol_from_inst_id(value: Any) -> str:
    text = str(value or "").upper()
    if text.startswith("BTC"):
        return "BTC"
    if text.startswith("ETH"):
        return "ETH"
    return text.split("-")[0] if text else ""


def _side_from_payload(payload: dict[str, Any]) -> str:
    side = str(payload.get("posSide") or payload.get("side") or "").lower()
    if side in {"long", "buy"}:
        return "long"
    if side in {"short", "sell"}:
        return "short"
    return side
