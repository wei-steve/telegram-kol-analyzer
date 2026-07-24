"""Offline Deepcoin order draft construction.

This module does not authenticate, place orders, or perform network requests.
It converts reviewed recovery previews into a conservative, auditable draft.
"""

from __future__ import annotations

import math
from typing import Any

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.execution_bindings import build_client_order_id
from telegram_kol_research.execution_bindings import build_strategy_instance_id
from telegram_kol_research.kol_codes import resolve_kol_code
from telegram_kol_research.price_normalization import extract_normalized_prices


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
    if order_type not in {"limit", "market"}:
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

    instrument_id = _to_deepcoin_swap_instrument(contract)
    if contract_spec is not None and contract_spec.instrument_id.upper() != instrument_id:
        raise DeepcoinOrderDraftError("contract_spec instrument_id mismatch")
    symbol = _symbol_from_contract(contract)
    entry_low, entry_high = _parse_entry_range(
        payload_preview.get("entry_range"),
        symbol=symbol,
    )
    entry_low = _normalize_price(entry_low, contract_spec)
    entry_high = _normalize_price(entry_high, contract_spec)
    single_entry_price = math.isclose(entry_low, entry_high)

    source = payload_preview.get("source") or {}
    source_kol_id = source.get("kol_id")
    source_chat_id = int(source.get("chat_id") or 0)
    source_message_id = int(source.get("message_id") or 0)
    source_kol_code = resolve_kol_code(
        chat_id=source_chat_id,
        explicit_code=source.get("kol_code"),
    )
    risk_budget = float(_require_value(payload_preview, "risk_budget_usdt"))
    stop_loss = _parse_optional_price(payload_preview.get("stop_loss"), symbol=symbol)
    margin_mode = _normalize_margin_mode(payload_preview.get("margin_mode"))
    position_mode = _normalize_position_mode(payload_preview.get("position_mode"))
    strategy_instance_id = str(
        payload_preview.get("strategy_instance_id")
        or build_strategy_instance_id(
            venue="deepcoin",
            chat_id=source_chat_id,
            message_id=source_message_id,
            symbol=symbol,
            side=position_side,
        )
    )
    entry_range_order_style = str(
        payload_preview.get("entry_range_order_style") or "conservative"
    ).lower()
    current_price = _parse_optional_price(payload_preview.get("current_price"), symbol=symbol)
    market_entry_deviation_pct = _parse_optional_float(
        payload_preview.get("max_market_entry_deviation_pct")
    )
    take_profit_prices = _order_take_profit_prices(
        position_side=position_side,
        prices=_parse_take_profit_prices(
            payload_preview.get("take_profit"),
            symbol=symbol,
        ),
    )
    take_profit_allocations = _normalize_take_profit_allocations(
        payload_preview.get("take_profit_allocations"),
        len(take_profit_prices),
    )
    hybrid_market_price = (
        _hybrid_market_entry_price(
            position_side=position_side,
            low=entry_low,
            high=entry_high,
            current_price=current_price,
            max_deviation_pct=market_entry_deviation_pct,
            contract_spec=contract_spec,
        )
        if order_type == "limit" and not single_entry_price
        else None
    )
    if order_type == "market" or single_entry_price:
        reference_price = _normalize_price((entry_low + entry_high) / 2, contract_spec)
        order_legs = [
            _single_entry_leg(
                open_side=open_side,
                position_side=position_side,
                order_type=order_type,
                price=reference_price,
                strategy_instance_id=strategy_instance_id,
                source_kol_code=source_kol_code,
                source_message_id=source_message_id,
                risk_budget=risk_budget,
                stop_loss=stop_loss,
                contract_spec=contract_spec,
            ),
        ]
    elif hybrid_market_price is not None:
        _, limit_price = _range_entry_leg_prices(
            position_side=position_side,
            low=entry_low,
            high=entry_high,
            deviation_pct=market_entry_deviation_pct,
            contract_spec=contract_spec,
        )
        order_legs = [
            _order_leg(
                side=open_side,
                position_side=position_side,
                order_type="market",
                price=hybrid_market_price,
                client_order_id=build_client_order_id(
                    strategy_instance_id=strategy_instance_id,
                    leg_index=1,
                    kol_code=source_kol_code,
                    message_id=source_message_id,
                ),
                allocation_pct=50.0,
                risk_budget_usdt=_leg_risk_budget(
                    risk_budget=risk_budget,
                    allocation_pct=50.0,
                ),
                quantity=_estimate_leg_quantity(
                    risk_budget=risk_budget,
                    allocation_pct=50.0,
                    entry_price=hybrid_market_price,
                    stop_loss=stop_loss,
                ),
                stop_loss=stop_loss,
                contract_spec=contract_spec,
            ),
            _order_leg(
                side=open_side,
                position_side=position_side,
                order_type="limit",
                price=limit_price,
                client_order_id=build_client_order_id(
                    strategy_instance_id=strategy_instance_id,
                    leg_index=2,
                    kol_code=source_kol_code,
                    message_id=source_message_id,
                ),
                allocation_pct=50.0,
                risk_budget_usdt=_leg_risk_budget(
                    risk_budget=risk_budget,
                    allocation_pct=50.0,
                ),
                quantity=_estimate_leg_quantity(
                    risk_budget=risk_budget,
                    allocation_pct=50.0,
                    entry_price=limit_price,
                    stop_loss=stop_loss,
                ),
                stop_loss=stop_loss,
                contract_spec=contract_spec,
            ),
        ]
    else:
        first_price, second_price = _range_entry_leg_prices(
            position_side=position_side,
            low=entry_low,
            high=entry_high,
            deviation_pct=market_entry_deviation_pct,
            contract_spec=contract_spec,
        )
        order_legs = [
            _order_leg(
                side=open_side,
                position_side=position_side,
                order_type=order_type,
                price=first_price,
                client_order_id=build_client_order_id(
                    strategy_instance_id=strategy_instance_id,
                    leg_index=1,
                    kol_code=source_kol_code,
                    message_id=source_message_id,
                ),
                allocation_pct=50.0,
                risk_budget_usdt=_leg_risk_budget(
                    risk_budget=risk_budget,
                    allocation_pct=50.0,
                ),
                quantity=_estimate_leg_quantity(
                    risk_budget=risk_budget,
                    allocation_pct=50.0,
                    entry_price=first_price,
                    stop_loss=stop_loss,
                ),
                stop_loss=stop_loss,
                contract_spec=contract_spec,
            ),
            _order_leg(
                side=open_side,
                position_side=position_side,
                order_type=order_type,
                price=second_price,
                client_order_id=build_client_order_id(
                    strategy_instance_id=strategy_instance_id,
                    leg_index=2,
                    kol_code=source_kol_code,
                    message_id=source_message_id,
                ),
                allocation_pct=50.0,
                risk_budget_usdt=_leg_risk_budget(
                    risk_budget=risk_budget,
                    allocation_pct=50.0,
                ),
                quantity=_estimate_leg_quantity(
                    risk_budget=risk_budget,
                    allocation_pct=50.0,
                    entry_price=second_price,
                    stop_loss=stop_loss,
                ),
                stop_loss=stop_loss,
                contract_spec=contract_spec,
            ),
        ]
    order_legs = _coalesce_equivalent_entry_legs(order_legs)
    blocking_reason_codes = _blocking_reason_codes(
        stop_loss=stop_loss,
        contract_spec=contract_spec,
        order_legs=order_legs,
    )
    quantity_notes = _quantity_notes(stop_loss=stop_loss, contract_spec=contract_spec)

    source_payload = {
        "kol_id": source_kol_id,
        "chat_id": source_chat_id,
        "message_id": source_message_id,
    }
    if source_kol_code:
        source_payload["kol_code"] = source_kol_code

    draft = {
        "venue": "deepcoin",
        "dry_run_only": True,
        "executable": False,
        "blocking_reason_codes": blocking_reason_codes,
        "strategy_instance_id": strategy_instance_id,
        "symbol": symbol,
        "instrument_id": instrument_id,
        "margin_mode": margin_mode,
        "position_mode": position_mode,
        "order_legs": order_legs,
        "stop_loss": (
            float(f"{_normalize_price(stop_loss, contract_spec):g}")
            if stop_loss is not None
            else None
        ),
        "take_profit_legs": _take_profit_legs(
            prices=take_profit_prices,
            allocations=take_profit_allocations,
            contract_spec=contract_spec,
        ),
        "risk_budget_usdt": risk_budget,
        "source": source_payload,
        "notes": [
            "offline_constructor_only",
            "default_cross_margin_split_position",
            "strategy_instance_id_required_for_exit_matching",
            *quantity_notes,
            *(
                ["range_entry_hybrid_market_half_limit_half"]
                if hybrid_market_price is not None
                else []
            ),
        ],
    }
    if contract_spec is not None:
        draft["contract_spec"] = contract_spec.to_dict()
    return draft


def _require_value(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value in (None, ""):
        raise DeepcoinOrderDraftError(f"{key} is required")
    return value


def _parse_entry_range(value: Any, *, symbol: str | None = None) -> tuple[float, float]:
    if value in (None, ""):
        raise DeepcoinOrderDraftError("entry_range is required")
    prices = extract_normalized_prices(value, symbol=symbol)
    if len(prices) != 2:
        raise DeepcoinOrderDraftError("entry_range must be low-high")
    low = float(prices[0])
    high = float(prices[1])
    if low <= 0 or high <= 0:
        raise DeepcoinOrderDraftError("entry_range prices must be positive")
    if low > high:
        low, high = high, low
    return low, high


def _parse_optional_price(value: Any, *, symbol: str | None = None) -> float | None:
    if value in (None, ""):
        return None
    prices = extract_normalized_prices(value, symbol=symbol)
    if prices:
        return prices[0]
    raise DeepcoinOrderDraftError("stop_loss must contain a numeric price")


def _parse_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_take_profit_prices(value: Any, *, symbol: str | None = None) -> list[float]:
    if value in (None, ""):
        return []
    return extract_normalized_prices(value, symbol=symbol)


def _order_take_profit_prices(*, position_side: str, prices: list[float]) -> list[float]:
    reverse = position_side.lower() == "short"
    return sorted((float(price) for price in prices if float(price) > 0), reverse=reverse)[:3]


def _normalize_take_profit_allocations(value: Any, count: int) -> list[float]:
    if count <= 0:
        return []
    if count == 1:
        return [100.0]
    raw_items: list[Any]
    if isinstance(value, str):
        raw_items = value.replace("/", ",").replace("-", ",").split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = [40, 30, 30] if count == 3 else []
    allocations: list[float] = []
    for item in raw_items[:count]:
        try:
            parsed = float(item)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            allocations.append(parsed)
    if count == 2 and allocations == [40.0, 30.0]:
        allocations = []
    if len(allocations) < count:
        allocations = [100 / count for _ in range(count)]
    total = sum(allocations)
    return [round(item * 100 / total, 8) for item in allocations[:count]]


def _range_entry_leg_prices(
    *,
    position_side: str,
    low: float,
    high: float,
    deviation_pct: float | None,
    contract_spec: DeepcoinContractSpec | None,
) -> tuple[float, float]:
    deviation = max(0.0, float(deviation_pct or 0.0)) / 100
    if position_side == "long":
        first = _normalize_price(high * (1 + deviation), contract_spec)
        second = _normalize_price(low * (1 + deviation), contract_spec)
    else:
        first = _normalize_price(low * (1 - deviation), contract_spec)
        second = _normalize_price(high * (1 - deviation), contract_spec)
    if math.isclose(first, second):
        alternate_endpoint = high if math.isclose(first, low) else low
        second = _normalize_price(alternate_endpoint, contract_spec)
    return first, second


def _hybrid_market_entry_price(
    *,
    position_side: str,
    low: float,
    high: float,
    current_price: float | None,
    max_deviation_pct: float | None,
    contract_spec: DeepcoinContractSpec | None,
) -> float | None:
    if current_price is None or max_deviation_pct is None:
        return None
    anchor = high if position_side == "long" else low
    if anchor <= 0:
        return None
    deviation_pct = abs(current_price - anchor) / anchor * 100
    if deviation_pct > max_deviation_pct:
        return None
    return _normalize_price(current_price, contract_spec)


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


def _leg_risk_budget(
    *,
    risk_budget: float,
    allocation_pct: float,
) -> float:
    return float(f"{risk_budget * allocation_pct / 100:g}")


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
    client_order_id: str,
    allocation_pct: float,
    risk_budget_usdt: float,
    quantity: float | None,
    stop_loss: float | None,
    contract_spec: DeepcoinContractSpec | None,
) -> dict[str, Any]:
    leg = {
        "side": side,
        "position_side": position_side,
        "order_type": order_type,
        "price": float(f"{price:g}"),
        "allocation_pct": allocation_pct,
        "risk_budget_usdt": risk_budget_usdt,
        "client_order_id": client_order_id,
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
    estimated_loss = _estimated_stop_loss_usdt(
        entry_price=price,
        stop_loss=stop_loss,
        quantity=leg.get("quantity"),
        quantity_unit=leg.get("quantity_unit"),
        contract_spec=contract_spec,
    )
    if estimated_loss is not None:
        leg["estimated_stop_loss_usdt"] = estimated_loss
    return leg


def _single_entry_leg(
    *,
    open_side: str,
    position_side: str,
    order_type: str,
    price: float,
    strategy_instance_id: str,
    source_kol_code: str | None,
    source_message_id: int,
    risk_budget: float,
    stop_loss: float | None,
    contract_spec: DeepcoinContractSpec | None,
) -> dict[str, Any]:
    return _order_leg(
        side=open_side,
        position_side=position_side,
        order_type=order_type,
        price=price,
        client_order_id=build_client_order_id(
            strategy_instance_id=strategy_instance_id,
            leg_index=1,
            kol_code=source_kol_code,
            message_id=source_message_id,
        ),
        allocation_pct=100.0,
        risk_budget_usdt=_leg_risk_budget(
            risk_budget=risk_budget,
            allocation_pct=100.0,
        ),
        quantity=_estimate_leg_quantity(
            risk_budget=risk_budget,
            allocation_pct=100.0,
            entry_price=price,
            stop_loss=stop_loss,
        ),
        stop_loss=stop_loss,
        contract_spec=contract_spec,
    )


def _coalesce_equivalent_entry_legs(
    order_legs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    coalesced: list[dict[str, Any]] = []
    key_positions: dict[tuple[Any, ...], int] = {}
    source_indices: dict[int, list[int]] = {}
    summed_fields = (
        "allocation_pct",
        "risk_budget_usdt",
        "quantity",
        "base_asset_estimate",
        "estimated_stop_loss_usdt",
    )

    for leg_index, leg in enumerate(order_legs, start=1):
        key = (
            leg.get("order_type"),
            leg.get("price"),
            leg.get("side"),
            leg.get("position_side"),
            leg.get("quantity_unit"),
        )
        position = key_positions.get(key)
        if position is None:
            position = len(coalesced)
            key_positions[key] = position
            coalesced.append(dict(leg))
            source_indices[position] = [leg_index]
            continue

        merged_leg = coalesced[position]
        for field in summed_fields:
            current = merged_leg.get(field)
            incoming = leg.get(field)
            if current is not None and incoming is not None:
                merged_leg[field] = math.fsum((float(current), float(incoming)))
        source_indices[position].append(leg_index)
        merged_leg["merged_from_leg_indices"] = list(source_indices[position])

    return coalesced


def _estimated_stop_loss_usdt(
    *,
    entry_price: float,
    stop_loss: float | None,
    quantity: Any,
    quantity_unit: Any,
    contract_spec: DeepcoinContractSpec | None,
) -> float | None:
    if stop_loss is None or quantity in (None, ""):
        return None
    price_risk = abs(float(entry_price) - float(stop_loss))
    if price_risk <= 0:
        return None
    quantity_float = float(quantity)
    if quantity_unit == "contracts":
        if contract_spec is None:
            return None
        loss = price_risk * quantity_float * contract_spec.contract_value
    else:
        loss = price_risk * quantity_float
    return float(f"{loss:.6g}")


def _take_profit_legs(
    *,
    prices: list[float],
    allocations: list[float],
    contract_spec: DeepcoinContractSpec | None,
) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    for index, price in enumerate(prices):
        allocation = allocations[index] if index < len(allocations) else 100 / len(prices)
        legs.append(
            {
                "index": index + 1,
                "price": float(f"{_normalize_price(price, contract_spec):g}"),
                "allocation_pct": allocation,
                "order_type": "market_on_trigger",
            }
        )
    return legs


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


def _normalize_margin_mode(value: Any) -> str:
    text = str(value or "cross").lower()
    if text in {"isolated", "fixed", "逐仓"}:
        return "isolated"
    return "cross"


def _normalize_position_mode(value: Any) -> str:
    text = str(value or "split").lower()
    if text in {"net", "merged", "one_way", "合仓"}:
        return "net"
    return "split"
