"""Read-only, exact-identity TPSL display joins for the position dashboard."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Mapping

from telegram_kol_research.deepcoin_contract_specs import (
    DeepcoinContractSpecProvider,
)


@dataclass(frozen=True, slots=True)
class PositionTpslDisplayRow:
    kind: str
    trigger_price_text: str
    size_text: str
    order_id: str
    ownership_state: str
    size_mode: Literal["partial", "full_position"]
    raw_size_text: str
    size_display_text: str
    current_position_size_text: str | None = None
    instrument_id: str | None = None
    side: str | None = None

    def as_dict(self) -> dict[str, str]:
        row = {
            "kind": self.kind,
            "trigger_price_text": self.trigger_price_text,
            "size_text": self.size_text,
            "raw_size_text": self.raw_size_text,
            "size_mode": self.size_mode,
            "size_display_text": self.size_display_text,
            "order_id": self.order_id,
            "ownership_state": self.ownership_state,
        }
        if self.current_position_size_text is not None:
            row["current_position_size_text"] = self.current_position_size_text
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
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
) -> PositionTpslDisplayResult:
    """Join TPSL rows only by exchange position ID or verified local order ID.

    Price, size, direction and creation time are display fields.  They never
    establish ownership, so an unscoped full-position stop remains global.
    """

    positions_by_id = {
        position_id: position
        for position in positions
        if (
            position_id := _first_text(
                position,
                "PositionID",
                "posId",
                "pos_id",
                "id",
            )
        )
    }
    by_pos_id = {position_id: [] for position_id in positions_by_id}
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

        position = positions_by_id.get(position_id or "")
        rows = _split_order(
            order,
            order_id=order_id or "-",
            position=position,
            contract_spec_provider=contract_spec_provider,
        )
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
    order: dict[str, Any],
    *,
    order_id: str,
    position: dict[str, Any] | None,
    contract_spec_provider: DeepcoinContractSpecProvider | None,
) -> list[PositionTpslDisplayRow]:
    size_mode, raw_size_text, size_display_text, current_size = _size_fields(
        order,
        position=position,
        contract_spec_provider=contract_spec_provider,
    )
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
                    size_text=raw_size_text,
                    order_id=order_id,
                    ownership_state=(
                        "已验证归属" if position is not None else "无法归属"
                    ),
                    size_mode=size_mode,
                    raw_size_text=raw_size_text,
                    size_display_text=size_display_text,
                    current_position_size_text=current_size,
                )
            )
    return result


def _size_fields(
    order: dict[str, Any],
    *,
    position: dict[str, Any] | None,
    contract_spec_provider: DeepcoinContractSpecProvider | None,
) -> tuple[Literal["partial", "full_position"], str, str, str | None]:
    raw_size = _first_text(order, "sz", "size", "Volume")
    raw_size_text = raw_size if raw_size is not None else "0"
    parsed_size = _decimal_or_none(raw_size_text)
    is_full_position = raw_size is None or parsed_size == 0
    instrument_id = str(
        order.get("instId")
        or order.get("InstrumentID")
        or (position or {}).get("instId")
        or (position or {}).get("InstrumentID")
        or ""
    ).upper()

    if not is_full_position:
        return (
            "partial",
            raw_size_text,
            _quantity_display(
                raw_size_text,
                instrument_id=instrument_id,
                contract_spec_provider=contract_spec_provider,
            ),
            None,
        )

    if position is None:
        return (
            "full_position",
            raw_size_text,
            "全部仓位（具体仓位未归属）",
            None,
        )

    current_size = _first_text(position, "pos", "size")
    current_quantity = _decimal_or_none(current_size)
    if current_size is None or current_quantity is None or current_quantity <= 0:
        return ("full_position", raw_size_text, "全部剩余仓位", None)
    snapshot = _quantity_display(
        current_size,
        instrument_id=instrument_id,
        contract_spec_provider=contract_spec_provider,
    )
    return (
        "full_position",
        raw_size_text,
        f"全部剩余仓位（当前 {snapshot}）",
        current_size,
    )


def _quantity_display(
    contracts_text: str,
    *,
    instrument_id: str,
    contract_spec_provider: DeepcoinContractSpecProvider | None,
) -> str:
    contracts = _decimal_or_none(contracts_text)
    if contracts is None:
        return f"{contracts_text} contracts"

    try:
        spec = (
            contract_spec_provider.get_contract_spec(instrument_id)
            if contract_spec_provider is not None and instrument_id
            else None
        )
    except (KeyError, TypeError, ValueError):
        spec = None
    contracts_display = _decimal_text(contracts)
    if spec is None:
        return f"{contracts_display} contracts"

    symbol = _base_symbol(spec.instrument_id)
    if not symbol:
        return f"{contracts_display} contracts"
    base_quantity = contracts * Decimal(str(spec.contract_value))
    return (
        f"{contracts_display} contracts / "
        f"{_decimal_text(base_quantity)} {symbol}"
    )


def _decimal_or_none(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _base_symbol(instrument_id: str) -> str | None:
    symbol = instrument_id.upper().split("-", 1)[0].strip()
    return symbol or None


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
