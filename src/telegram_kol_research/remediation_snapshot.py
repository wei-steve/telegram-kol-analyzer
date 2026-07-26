"""Stable exchange snapshot projection for approved remediation actions."""

from __future__ import annotations

from typing import Any


def remediation_snapshot_payload(snapshot) -> dict[str, Any]:
    """Keep write-relevant state while excluding mark-price-only noise."""

    return {
        "positions": [
            stable_position_payload(dict(row))
            for row in snapshot.positions
        ],
        "pending_trigger_orders": list(snapshot.pending_trigger_orders),
        "open_orders": list(snapshot.open_orders),
        "order_history": list(snapshot.order_history),
        "trade_fills": list(snapshot.trade_fills),
        "trigger_history": list(snapshot.trigger_history),
        "pending_tpsl_observations": list(snapshot.pending_tpsl_observations),
        "errors": dict(snapshot.errors),
    }


def stable_position_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Project exact position identity, quantity, economics, and protection."""

    return {
        "instId": _first_text(
            row,
            "instId",
            "inst_id",
            "instrument_id",
        ),
        "posId": _first_text(row, "posId", "pos_id", "id"),
        "posSide": _first_text(row, "posSide", "pos_side", "side"),
        "pos": _first_text(row, "pos", "size", "sz"),
        "avgPx": _first_text(
            row,
            "avgPx",
            "avgPrice",
            "avg_entry_price",
            "entryPrice",
        ),
        "tpTriggerPx": _first_text(
            row,
            "tpTriggerPx",
            "tp_trigger_price",
        ),
        "slTriggerPx": _first_text(
            row,
            "slTriggerPx",
            "sl_trigger_price",
        ),
        "mgnMode": _first_text(
            row,
            "mgnMode",
            "marginMode",
            "margin_mode",
        ),
        "mrgPosition": _first_text(
            row,
            "mrgPosition",
            "posMode",
            "positionMode",
            "position_mode",
        ),
        "lever": _first_text(row, "lever", "leverage"),
    }


def _first_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None
