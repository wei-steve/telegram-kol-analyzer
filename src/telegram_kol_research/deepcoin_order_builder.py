"""Offline Deepcoin order draft construction.

This module does not authenticate, place orders, or perform network requests.
It converts reviewed recovery previews into a conservative, auditable draft.
"""

from __future__ import annotations

import math
from typing import Any

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec


class DeepcoinOrderDraftError(ValueError):
    """Raised when a recovery payload cannot be converted into a draft."""


def build_deepcoin_order_draft(
    payload_preview: dict[str, Any],
    *,
    contract_spec: DeepcoinContractSpec | None = None,
) -> dict[str, Any]:
    """Build a dry-run-only Deepcoin order draft from an execution queue payload."""

    _require_value(payload_preview, "venue")
    if str(payload_preview["venue"]).lower() != "deepcoin":
        raise DeepcoinOrderDraftError("unsupported venue")

    order_type = str(_require_value(payload_preview, "order_type")).lower()
    if order_type != "limit":
        raise DeepcoinOrderDraftError(f"unsupported order_type: {order_type}")

    contract = str(_require_value(payload_preview, "contract")).upper()
    open_side = str(_require_value(payload_preview, "open_side")).lower()
    position_side = str(_require_value(payload_preview, "position_side")).lower()
    if open_side not in {"buy", "sell"}:
        raise DeepcoinOrderDraftError(f"unsupported open_side: {open_side}")
    if position_side not in {"long", "short"}:
        raise DeepcoinOrderDraftError(f"unsupported position_side: {position_side}")
    if (position_side == "long" and open_side != "buy") or (
        position_side == "short" and open_side != "sell"
    ):
        raise DeepcoinOrderDraftError("open_side does not match position_side")

    entry_low, entry_high = _parse_entry_range(payload_preview.get("entry_range"))
    instrument_id = _to_deepcoin_swap_instrument(contract)
    if contract_spec is not None and contract_spec.instrument_id.upper() != instrument_id:
        raise DeepcoinOrderDraftError("contract_spec instrument_id mismatch")

    edge_price = _normalize_price(entry_high if position_side == "long" else entry_low, contract_spec)
    midpoint = _normalize_price((entry_low + entry_high) / 2, contract_spec)
    source = payload_preview.get("source") or {}
    risk_budget = float(_require_value(payload_preview, "risk_budget_usdt"))
    stop_loss = _parse_optional_price(payload_preview.get("stop_loss"))

    order_legs = [
        _order_leg(
            side=open_side,
            position_side=position_side,
            order_type=order_type,
            price=edge_price,
            quantity=_estimate_leg_quantity(
                risk_budget=risk_budget,
                allocation_pct=50.0,
                entry_price=edge_price,
                stop_loss=stop_loss,
            ),
            contract_spec=contract_spec,
        ),
        _order_leg(
            side=open_side,
            position_side=position_side,
            order_type=order_type,
            price=midpoint,
            quantity=_estimate_leg_quantity(
                risk_budget=risk_budget,
                allocation_pct=50.0,
                entry_price=midpoint,
                stop_loss=stop_loss,
            ),
            contract_spec=contract_spec,
        ),
    ]
    blocking_reason_codes = _blocking_reason_codes(
        stop_loss=stop_loss,
        contract_spec=contract_spec,
        order_legs=order_legs,
    )
    quantity_notes = _quantity_notes(stop_loss=stop_loss, contract_spec=contract_spec)

    draft = {
        "venue": "deepcoin",
        "dry_run_only": True,
        "executable": False,
        "blocking_reason_codes": blocking_reason_codes,
        "symbol": _symbol_from_contract(contract),
        "instrument_id": instrument_id,
        "margin_mode": "isolated",
        "position_mode": "split",
        "order_legs": order_legs,
        "risk_budget_usdt": risk_budget,
        "source": {
            "kol_id": source.get("kol_id"),
            "chat_id": source.get("chat_id"),
            "message_id": source.get("message_id"),
        },
        "notes": ["offline_constructor_only", *quantity_notes],
    }
    if contract_spec is not None:
        draft["contract_spec"] = contract_spec.to_dict()
    return draft


def _require_value(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value in (None, ""):
        raise DeepcoinOrderDraftError(f"{key} is required")
    return value


def _parse_entry_range(value: Any) -> tuple[float, float]:
    if value in (None, ""):
        raise DeepcoinOrderDraftError("entry_range is required")
    parts = str(value).replace("~", "-").split("-")
    if len(parts) != 2:
        raise DeepcoinOrderDraftError("entry_range must be low-high")
    try:
        low = float(parts[0].strip())
        high = float(parts[1].strip())
    except ValueError as exc:
        raise DeepcoinOrderDraftError("entry_range must contain numeric prices") from exc
    if low <= 0 or high <= 0:
        raise DeepcoinOrderDraftError("entry_range prices must be positive")
    if low > high:
        low, high = high, low
    return low, high


def _parse_optional_price(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value)
    for separator in ["/", ",", " "]:
        text = text.replace(separator, "-")
    for part in text.split("-"):
        stripped = part.strip()
        if not stripped:
            continue
        try:
            price = float(stripped)
        except ValueError:
            continue
        if price > 0:
            return price
    raise DeepcoinOrderDraftError("stop_loss must contain a numeric price")


def _estimate_leg_quantity(
    *,
    risk_budget: float,
    allocation_pct: float,
    entry_price: float,
    stop_loss: float | None,
) -> float | None:
    if stop_loss is None:
        return None
    price_risk = abs(entry_price - stop_loss)
    if price_risk <= 0:
        raise DeepcoinOrderDraftError("stop_loss must differ from entry price")
    leg_risk = risk_budget * allocation_pct / 100
    return round(leg_risk / price_risk, 6)


def _to_deepcoin_swap_instrument(contract: str) -> str:
    normalized = contract.upper().replace("_", "-")
    if normalized.endswith("-SWAP"):
        return normalized
    if normalized.endswith("-USDT"):
        return f"{normalized}-SWAP"
    if normalized.endswith("USDT"):
        return f"{normalized[:-4]}-USDT-SWAP"
    return f"{normalized}-USDT-SWAP"


def _symbol_from_contract(contract: str) -> str:
    normalized = contract.upper().replace("_", "-")
    if "-" in normalized:
        return normalized.split("-")[0]
    if normalized.endswith("USDT"):
        return normalized[:-4]
    return normalized


def _order_leg(
    *,
    side: str,
    position_side: str,
    order_type: str,
    price: float,
    quantity: float | None,
    contract_spec: DeepcoinContractSpec | None,
) -> dict[str, Any]:
    leg = {
        "side": side,
        "position_side": position_side,
        "order_type": order_type,
        "price": float(f"{price:g}"),
        "allocation_pct": 50.0,
        "quantity": quantity,
    }
    if quantity is not None:
        if contract_spec is None:
            leg["quantity_unit"] = "base_asset_estimate"
        else:
            leg["base_asset_estimate"] = quantity
            leg["quantity"] = _round_down_to_step(
                quantity / contract_spec.contract_value,
                contract_spec.quantity_step,
            )
            leg["quantity_unit"] = "contracts"
    return leg


def _blocking_reason_codes(
    *,
    stop_loss: float | None,
    contract_spec: DeepcoinContractSpec | None,
    order_legs: list[dict[str, Any]],
) -> list[str]:
    if stop_loss is None:
        return ["missing_stop_loss"]
    if contract_spec is None:
        return ["contract_size_unverified"]
    if any(float(leg["quantity"] or 0) < contract_spec.min_quantity for leg in order_legs):
        return ["quantity_below_minimum"]
    return []


def _quantity_notes(
    *,
    stop_loss: float | None,
    contract_spec: DeepcoinContractSpec | None,
) -> list[str]:
    notes = ["limit_edge_selection_side_aware_default"]
    if stop_loss is None:
        return ["quantity_requires_stop_loss_or_manual_sizing", *notes]
    if contract_spec is None:
        return [
            "quantity_uses_linear_price_risk_estimate",
            *notes,
            "contract_size_must_be_verified_before_live_order",
        ]
    return [
        "quantity_uses_linear_price_risk_estimate",
        *notes,
        "contract_spec_applied",
        "quantity_rounded_down_to_step",
        "price_rounded_to_tick",
    ]


def _round_down_to_step(value: float, step: float) -> float:
    rounded = math.floor((value / step) + 1e-12) * step
    return float(f"{rounded:.12g}")


def _normalize_price(price: float, contract_spec: DeepcoinContractSpec | None) -> float:
    if contract_spec is None:
        return price
    return _round_down_to_step(price, contract_spec.price_tick)
