"""Offline Deepcoin order draft construction.

This module does not authenticate, place orders, or perform network requests.
It converts reviewed recovery previews into a conservative, auditable draft.
"""

from __future__ import annotations

import math
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.execution_bindings import build_client_order_id
from telegram_kol_research.execution_bindings import build_strategy_instance_id
from telegram_kol_research.kol_codes import resolve_kol_code
from telegram_kol_research.price_normalization import extract_normalized_prices
from telegram_kol_research.take_profit_plan import TakeProfitPlanError
from telegram_kol_research.take_profit_plan import build_take_profit_plan


class DeepcoinOrderDraftError(ValueError):
    """Raised when a recovery payload cannot be converted into a draft."""


MAX_FLOAT_DECIMAL = Decimal(str(sys.float_info.max))


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
    normalized_stop_loss = (
        _normalize_price(stop_loss, contract_spec)
        if stop_loss is not None
        else None
    )
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
    current_price = _parse_optional_price(payload_preview.get("current_price"), symbol=symbol)
    market_leg_threshold = _parse_nonnegative_decimal(
        payload_preview.get("market_leg_threshold", "0"),
        field_name="market_leg_threshold",
    )
    first_limit_offset = _parse_nonnegative_decimal(
        payload_preview.get("first_limit_offset", "0"),
        field_name="first_limit_offset",
    )
    second_limit_offset = _parse_nonnegative_decimal(
        payload_preview.get("second_limit_offset", "0"),
        field_name="second_limit_offset",
    )
    parsed_take_profit_prices = _parse_take_profit_prices(
        payload_preview.get("take_profit"), symbol=symbol,
    )
    if parsed_take_profit_prices:
        try:
            take_profit_plan = build_take_profit_plan(
                prices=parsed_take_profit_prices,
                side=position_side,
                configured_allocations=payload_preview.get("take_profit_allocations"),
            )
        except TakeProfitPlanError as exc:
            raise DeepcoinOrderDraftError(str(exc)) from exc
        take_profit_legs = _take_profit_legs(
            plan=take_profit_plan, contract_spec=contract_spec,
        )
    else:
        take_profit_legs = []
    hybrid_market_price = (
        _hybrid_market_entry_price(
            position_side=position_side,
            low=entry_low,
            high=entry_high,
            current_price=current_price,
            market_leg_threshold=market_leg_threshold,
            contract_spec=contract_spec,
        )
        if order_type == "limit" and not single_entry_price
        else None
    )
    supplemental_prices = _parse_supplemental_entry_prices(
        payload_preview.get("supplemental_entry_prices"),
        symbol=symbol,
        contract_spec=contract_spec,
    )
    explicit_entry_allocations = _parse_entry_allocations(
        payload_preview.get("entry_allocations")
    )
    custom_entry_plan = bool(supplemental_prices or explicit_entry_allocations)
    if custom_entry_plan:
        if single_entry_price:
            planned_prices = [
                _normalize_final_entry_price(
                    entry_low,
                    contract_spec,
                    error_message="entry price produces non-positive price after tick normalization",
                )
            ]
        else:
            planned_prices = list(
                _range_entry_leg_prices(
                    position_side=position_side,
                    low=entry_low,
                    high=entry_high,
                    first_limit_offset=first_limit_offset,
                    second_limit_offset=second_limit_offset,
                    contract_spec=contract_spec,
                )
            )
        for price in supplemental_prices:
            if price not in planned_prices:
                planned_prices.append(price)
        if explicit_entry_allocations:
            if len(explicit_entry_allocations) != len(planned_prices):
                raise DeepcoinOrderDraftError(
                    "entry_allocations must match the final entry leg count"
                )
            allocation_fractions = explicit_entry_allocations
        else:
            equal = Decimal("1") / Decimal(len(planned_prices))
            allocation_fractions = tuple(equal for _ in planned_prices)
        order_legs = []
        for index, (price, allocation_fraction) in enumerate(
            zip(planned_prices, allocation_fractions, strict=True),
            start=1,
        ):
            allocation_pct = float(allocation_fraction * Decimal("100"))
            leg_order_type = order_type if index == 1 else "limit"
            order_legs.append(
                _order_leg(
                    side=open_side,
                    position_side=position_side,
                    order_type=leg_order_type,
                    price=price,
                    client_order_id=build_client_order_id(
                        strategy_instance_id=strategy_instance_id,
                        leg_index=index,
                        kol_code=source_kol_code,
                        message_id=source_message_id,
                    ),
                    allocation_pct=allocation_pct,
                    risk_budget_usdt=_leg_risk_budget(
                        risk_budget=risk_budget,
                        allocation_pct=allocation_pct,
                    ),
                    quantity=_estimate_leg_quantity(
                        risk_budget=risk_budget,
                        allocation_pct=allocation_pct,
                        entry_price=price,
                        stop_loss=normalized_stop_loss,
                    ),
                    stop_loss=normalized_stop_loss,
                    contract_spec=contract_spec,
                )
            )
    elif order_type == "market" or single_entry_price:
        reference_price = _normalize_final_entry_price(
            (entry_low + entry_high) / 2,
            contract_spec,
            error_message="entry price produces non-positive price after tick normalization",
        )
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
            first_limit_offset=Decimal("0"),
            second_limit_offset=second_limit_offset,
            contract_spec=contract_spec,
        )
        first_allocation_pct, second_allocation_pct = _range_entry_allocations(
            first_price=hybrid_market_price,
            second_price=limit_price,
            stop_loss=normalized_stop_loss,
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
                allocation_pct=first_allocation_pct,
                risk_budget_usdt=_leg_risk_budget(
                    risk_budget=risk_budget,
                    allocation_pct=first_allocation_pct,
                ),
                quantity=_estimate_leg_quantity(
                    risk_budget=risk_budget,
                    allocation_pct=first_allocation_pct,
                    entry_price=hybrid_market_price,
                    stop_loss=normalized_stop_loss,
                ),
                stop_loss=normalized_stop_loss,
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
                allocation_pct=second_allocation_pct,
                risk_budget_usdt=_leg_risk_budget(
                    risk_budget=risk_budget,
                    allocation_pct=second_allocation_pct,
                ),
                quantity=_estimate_leg_quantity(
                    risk_budget=risk_budget,
                    allocation_pct=second_allocation_pct,
                    entry_price=limit_price,
                    stop_loss=normalized_stop_loss,
                ),
                stop_loss=normalized_stop_loss,
                contract_spec=contract_spec,
            ),
        ]
    else:
        first_price, second_price = _range_entry_leg_prices(
            position_side=position_side,
            low=entry_low,
            high=entry_high,
            first_limit_offset=first_limit_offset,
            second_limit_offset=second_limit_offset,
            contract_spec=contract_spec,
        )
        first_allocation_pct, second_allocation_pct = _range_entry_allocations(
            first_price=first_price,
            second_price=second_price,
            stop_loss=normalized_stop_loss,
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
                allocation_pct=first_allocation_pct,
                risk_budget_usdt=_leg_risk_budget(
                    risk_budget=risk_budget,
                    allocation_pct=first_allocation_pct,
                ),
                quantity=_estimate_leg_quantity(
                    risk_budget=risk_budget,
                    allocation_pct=first_allocation_pct,
                    entry_price=first_price,
                    stop_loss=normalized_stop_loss,
                ),
                stop_loss=normalized_stop_loss,
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
                allocation_pct=second_allocation_pct,
                risk_budget_usdt=_leg_risk_budget(
                    risk_budget=risk_budget,
                    allocation_pct=second_allocation_pct,
                ),
                quantity=_estimate_leg_quantity(
                    risk_budget=risk_budget,
                    allocation_pct=second_allocation_pct,
                    entry_price=second_price,
                    stop_loss=normalized_stop_loss,
                ),
                stop_loss=normalized_stop_loss,
                contract_spec=contract_spec,
            ),
        ]
    order_legs = _coalesce_equivalent_entry_legs(order_legs)
    estimated_aggregate_risk = math.fsum(
        float(leg.get("estimated_stop_loss_usdt") or 0) for leg in order_legs
    )
    if custom_entry_plan and estimated_aggregate_risk > risk_budget + 1e-6:
        raise DeepcoinOrderDraftError("aggregate entry risk exceeds risk_budget_usdt")
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
            float(f"{normalized_stop_loss:g}")
            if normalized_stop_loss is not None
            else None
        ),
        "take_profit_legs": take_profit_legs,
        "risk_budget_usdt": risk_budget,
        "source": source_payload,
        "notes": [
            "offline_constructor_only",
            "default_cross_margin_split_position",
            "strategy_instance_id_required_for_exit_matching",
            *quantity_notes,
            *(
                ["range_entry_hybrid_market_dynamic_risk_limit"]
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


def _parse_nonnegative_decimal(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        raise DeepcoinOrderDraftError(
            f"{field_name} must be a non-negative finite decimal"
        )
    if isinstance(value, str) and not value.strip():
        raise DeepcoinOrderDraftError(
            f"{field_name} must be a non-negative finite decimal"
        )
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError, ValueError):
        raise DeepcoinOrderDraftError(
            f"{field_name} must be a non-negative finite decimal"
        ) from None
    if not parsed.is_finite() or parsed < 0 or parsed > MAX_FLOAT_DECIMAL:
        raise DeepcoinOrderDraftError(
            f"{field_name} must be a non-negative finite decimal "
            "within the supported price range"
        )
    return parsed


def _parse_take_profit_prices(value: Any, *, symbol: str | None = None) -> list[float]:
    if value in (None, ""):
        return []
    return extract_normalized_prices(value, symbol=symbol)


def _parse_supplemental_entry_prices(
    value: Any,
    *,
    symbol: str | None,
    contract_spec: DeepcoinContractSpec | None,
) -> list[float]:
    if value in (None, "", []):
        return []
    if not isinstance(value, (list, tuple)):
        raise DeepcoinOrderDraftError("supplemental_entry_prices must be a list")
    prices: list[float] = []
    for item in value:
        parsed = extract_normalized_prices(item, symbol=symbol)
        if len(parsed) != 1 or float(parsed[0]) <= 0:
            raise DeepcoinOrderDraftError(
                "supplemental_entry_prices must contain positive prices"
            )
        normalized = _normalize_final_entry_price(
            float(parsed[0]),
            contract_spec,
            error_message="supplemental entry price is invalid after normalization",
        )
        if normalized not in prices:
            prices.append(normalized)
    return prices


def _parse_entry_allocations(value: Any) -> tuple[Decimal, ...]:
    if value in (None, "", []):
        return ()
    if not isinstance(value, (list, tuple)) or not value or len(value) > 8:
        raise DeepcoinOrderDraftError("entry_allocations must be a bounded list")
    parsed: list[Decimal] = []
    for item in value:
        allocation = _parse_nonnegative_decimal(
            item, field_name="entry_allocations"
        )
        if allocation <= 0 or allocation > 1:
            raise DeepcoinOrderDraftError(
                "entry_allocations must contain fractions in (0, 1]"
            )
        parsed.append(allocation)
    if sum(parsed) != Decimal("1"):
        raise DeepcoinOrderDraftError("entry_allocations must sum to 1")
    return tuple(parsed)


def _range_entry_leg_prices(
    *,
    position_side: str,
    low: float,
    high: float,
    first_limit_offset: Decimal,
    second_limit_offset: Decimal,
    contract_spec: DeepcoinContractSpec | None,
) -> tuple[float, float]:
    low_decimal = Decimal(str(low))
    high_decimal = Decimal(str(high))
    if position_side == "long":
        first = high_decimal + first_limit_offset
        second = low_decimal + second_limit_offset
    else:
        first = low_decimal - first_limit_offset
        second = high_decimal - second_limit_offset
    if first <= 0 or second <= 0:
        raise DeepcoinOrderDraftError(
            "fixed entry offset produces non-positive price"
        )
    if first > MAX_FLOAT_DECIMAL or second > MAX_FLOAT_DECIMAL:
        raise DeepcoinOrderDraftError(
            "fixed entry offset produces price outside finite float range"
        )
    return (
        _normalize_final_entry_price(
            float(first),
            contract_spec,
            error_message=(
                "fixed entry offset produces non-positive price after tick normalization"
            ),
        ),
        _normalize_final_entry_price(
            float(second),
            contract_spec,
            error_message=(
                "fixed entry offset produces non-positive price after tick normalization"
            ),
        ),
    )


def _hybrid_market_entry_price(
    *,
    position_side: str,
    low: float,
    high: float,
    current_price: float | None,
    market_leg_threshold: Decimal,
    contract_spec: DeepcoinContractSpec | None,
) -> float | None:
    if current_price is None or market_leg_threshold <= 0:
        return None
    anchor = high if position_side == "long" else low
    distance = abs(Decimal(str(current_price)) - Decimal(str(anchor)))
    if distance > market_leg_threshold:
        return None
    return _normalize_final_entry_price(
        current_price,
        contract_spec,
        error_message=(
            "hybrid market entry produces non-positive price after tick normalization"
        ),
    )


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


def _range_entry_allocations(
    *,
    first_price: float,
    second_price: float,
    stop_loss: float | None,
) -> tuple[float, float]:
    if stop_loss is None:
        return 50.0, 50.0
    first_distance = abs(first_price - stop_loss)
    second_distance = abs(second_price - stop_loss)
    total_distance = first_distance + second_distance
    if first_distance <= 0 or second_distance <= 0 or total_distance <= 0:
        raise DeepcoinOrderDraftError("stop_loss must differ from entry price")
    first_allocation = min(
        65.0,
        max(50.0, first_distance / total_distance * 100),
    )
    return first_allocation, 100.0 - first_allocation


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
    plan,
    contract_spec: DeepcoinContractSpec | None,
) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    for index, take_profit_leg in enumerate(plan.legs):
        price = float(take_profit_leg.price)
        allocation = float(take_profit_leg.allocation_pct)
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


def _normalize_final_entry_price(
    price: float,
    contract_spec: DeepcoinContractSpec | None,
    *,
    error_message: str,
) -> float:
    if price <= 0 or not math.isfinite(price):
        raise DeepcoinOrderDraftError(error_message)
    normalized = _normalize_price(price, contract_spec)
    if normalized <= 0 or not math.isfinite(normalized):
        raise DeepcoinOrderDraftError(error_message)
    return normalized


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
