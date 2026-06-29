"""Live Deepcoin recovery order submission after explicit confirmation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.deepcoin_client import DeepcoinTradingClientProtocol
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecProvider
from telegram_kol_research.execution_bindings import ExecutionBindingRecord
from telegram_kol_research.execution_bindings import upsert_execution_binding
from telegram_kol_research.recovery_live_submit_gate import validate_recovery_live_submit_gate
from telegram_kol_research.trading_settings import load_trading_settings


class RecoveryLiveSubmitError(RuntimeError):
    """Raised when a live recovery order cannot be submitted safely."""


def submit_recovery_order_live(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    deepcoin_client: DeepcoinTradingClientProtocol,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    submitted_at: datetime | None = None,
) -> dict[str, Any]:
    """Submit live Deepcoin limit entry orders for a confirmed recovery item."""

    settings = load_trading_settings(session_factory)
    if not settings.auto_trade_enabled:
        raise RecoveryLiveSubmitError("auto_trade_disabled")

    gate = validate_recovery_live_submit_gate(
        session_factory,
        chat_id=chat_id,
        message_id=message_id,
        symbol=symbol,
        side=side,
        contract_spec_provider=contract_spec_provider,
    )
    if not gate["would_submit"]:
        raise RecoveryLiveSubmitError(
            "live_submit_blocked:" + ",".join(str(code) for code in gate["reason_codes"])
        )

    draft = gate["deepcoin_order_draft"]
    if not isinstance(draft, dict):
        raise RecoveryLiveSubmitError("missing_deepcoin_order_draft")
    order_legs = draft.get("order_legs")
    if not isinstance(order_legs, list) or not order_legs:
        raise RecoveryLiveSubmitError("missing_order_legs")

    submitted_orders: list[dict[str, Any]] = []
    now = submitted_at or datetime.now(UTC)
    source = draft.get("source") if isinstance(draft.get("source"), dict) else {}
    kol_id = str(source.get("kol_id") or "unknown")
    symbol_key = str(draft.get("symbol") or symbol).upper()
    side_key = side.lower()

    for index, leg in enumerate(order_legs, start=1):
        if not isinstance(leg, dict):
            raise RecoveryLiveSubmitError("invalid_order_leg")
        order_payload = build_deepcoin_place_order_payload(draft, leg)
        try:
            response = deepcoin_client.place_order(order_payload)
        except DeepcoinClientError:
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
            raise DeepcoinClientError(f"Deepcoin client failed: {exc}") from exc

        order_id = _extract_order_id(response)
        pos_id = _extract_position_id(response)
        client_order_id = str(leg.get("client_order_id") or order_payload.get("clOrdId") or "")
        submitted_orders.append(
            {
                "leg_index": index,
                "client_order_id": client_order_id,
                "order_id": order_id,
                "pos_id": pos_id,
                "request": order_payload,
                "response": response,
            }
        )

    upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id=kol_id,
            chat_id=int(source.get("chat_id") or chat_id),
            message_id=int(source.get("message_id") or message_id),
            symbol=symbol_key,
            side=side_key,
            venue="deepcoin",
            order_id=_join_ids(order["order_id"] for order in submitted_orders),
            client_order_id=_join_ids(order["client_order_id"] for order in submitted_orders),
            pos_id=_join_ids(order["pos_id"] for order in submitted_orders),
            margin_mode=str(draft.get("margin_mode") or "cross"),
            position_mode=str(draft.get("position_mode") or "split"),
            payload={"draft": draft, "submitted_orders": submitted_orders},
            last_exchange_status="submitted",
            status="open",
            strategy_instance_id=str(draft.get("strategy_instance_id") or ""),
        ),
    )

    return {
        "submitted": True,
        "venue": "deepcoin",
        "submitted_at": now.isoformat(),
        "source": {
            "chat_id": chat_id,
            "message_id": message_id,
            "symbol": symbol_key,
            "side": side_key,
        },
        "order_count": len(submitted_orders),
        "orders": submitted_orders,
        "deepcoin_order_draft": draft,
        "warnings": ["protection_not_submitted_verify_stop_loss_take_profit_manually"],
    }


def build_deepcoin_place_order_payload(
    draft: dict[str, Any],
    leg: dict[str, Any],
) -> dict[str, Any]:
    """Convert one internal order leg to Deepcoin's place-order payload."""

    quantity = leg.get("quantity")
    if not isinstance(quantity, int | float) or quantity <= 0:
        raise RecoveryLiveSubmitError("non_positive_quantity")
    price = leg.get("price")
    if not isinstance(price, int | float) or price <= 0:
        raise RecoveryLiveSubmitError("non_positive_price")

    return {
        "instId": str(draft["instrument_id"]),
        "tdMode": _deepcoin_margin_mode(str(draft.get("margin_mode") or "cross")),
        "side": str(leg["side"]).lower(),
        "posSide": str(leg["position_side"]).lower(),
        "ordType": "limit",
        "px": str(price),
        "sz": str(quantity),
        "clOrdId": str(leg.get("client_order_id") or ""),
    }


def _deepcoin_margin_mode(value: str) -> str:
    return "cross" if value.lower() in {"cross", "crossed", "full", "全仓"} else "isolated"


def _extract_order_id(response: dict[str, Any]) -> str | None:
    for payload in _response_payloads(response):
        for key in ("ordId", "orderId", "order_id", "id"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _extract_position_id(response: dict[str, Any]) -> str | None:
    for payload in _response_payloads(response):
        for key in ("posId", "pos_id", "positionId"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _join_ids(values) -> str | None:
    items = [str(value) for value in values if value not in (None, "")]
    return ",".join(items) if items else None


def _response_payloads(response: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = [response]
    data = response.get("data")
    if isinstance(data, dict):
        payloads.append(data)
    elif isinstance(data, list):
        payloads.extend(item for item in data if isinstance(item, dict))
    return payloads
