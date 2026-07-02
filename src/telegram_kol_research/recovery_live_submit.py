"""Live Deepcoin recovery order submission after explicit confirmation."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.deepcoin_client import DeepcoinTradingClientProtocol
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecProvider
from telegram_kol_research.deepcoin_execution_actions import execute_deepcoin_management_signal
from telegram_kol_research.execution_bindings import ExecutionBindingRecord
from telegram_kol_research.execution_bindings import upsert_execution_binding
from telegram_kol_research.execution_events import ExecutionEventRecord
from telegram_kol_research.execution_events import record_execution_event
from telegram_kol_research.models import StrategyLifecycle
from telegram_kol_research.recovery_live_submit_gate import validate_recovery_live_submit_gate
from telegram_kol_research.trade_signals import TradeSignalRecord
from telegram_kol_research.trade_signals import enqueue_trade_signal
from telegram_kol_research.trade_signals import list_pending_trade_signals
from telegram_kol_research.trade_signals import load_trade_signal
from telegram_kol_research.trade_signals import mark_trade_signal_failed
from telegram_kol_research.trade_signals import mark_trade_signal_submitted
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
    max_order_legs: int | None = None,
) -> dict[str, Any]:
    """Enqueue and execute a confirmed recovery signal through the trade queue."""

    settings = load_trading_settings(session_factory)
    if not settings.auto_trade_enabled:
        raise RecoveryLiveSubmitError("auto_trade_disabled")

    trade_signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=chat_id,
        message_id=message_id,
        symbol=symbol,
        side=side,
        contract_spec_provider=contract_spec_provider,
        enqueued_at=submitted_at,
    )
    return process_trade_signal_live(
        session_factory,
        signal_id=trade_signal.id,
        deepcoin_client=deepcoin_client,
        contract_spec_provider=contract_spec_provider,
        processed_at=submitted_at,
        max_order_legs=max_order_legs,
    )


def enqueue_recovery_trade_signal(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    enqueued_at: datetime | None = None,
) -> TradeSignalRecord:
    """Send one confirmed recovery strategy into the durable trade-signal queue."""

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
            "signal_enqueue_blocked:" + ",".join(str(code) for code in gate["reason_codes"])
        )

    draft = gate["deepcoin_order_draft"]
    if not isinstance(draft, dict):
        raise RecoveryLiveSubmitError("missing_deepcoin_order_draft")
    source = draft.get("source") if isinstance(draft.get("source"), dict) else {}
    return enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id=str(source.get("kol_id") or "unknown"),
        chat_id=chat_id,
        message_id=message_id,
        symbol=str(draft.get("symbol") or symbol),
        side=side,
        action="open_position",
        payload={
            "source": {
                "chat_id": chat_id,
                "message_id": message_id,
                "symbol": str(draft.get("symbol") or symbol).upper(),
                "side": side.lower(),
            },
            "deepcoin_order_draft": draft,
        },
        strategy_instance_id=str(draft.get("strategy_instance_id") or ""),
        enqueued_at=enqueued_at,
    )


def process_trade_signal_live(
    session_factory: sessionmaker,
    *,
    signal_id: int,
    deepcoin_client: DeepcoinTradingClientProtocol,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    processed_at: datetime | None = None,
    max_order_legs: int | None = None,
) -> dict[str, Any]:
    """Receive and execute one pending trade signal."""

    settings = load_trading_settings(session_factory)
    if not settings.auto_trade_enabled:
        raise RecoveryLiveSubmitError("auto_trade_disabled")

    trade_signal = load_trade_signal(session_factory, signal_id)
    if trade_signal.status != "pending":
        raise RecoveryLiveSubmitError(f"trade_signal_not_pending:{trade_signal.status}")
    try:
        if trade_signal.action == "open_position":
            result = _submit_recovery_signal_direct(
                session_factory,
                trade_signal=trade_signal,
                deepcoin_client=deepcoin_client,
                contract_spec_provider=contract_spec_provider,
                submitted_at=processed_at,
                max_order_legs=max_order_legs,
            )
        else:
            result = execute_deepcoin_management_signal(
                session_factory,
                trade_signal=trade_signal,
                deepcoin_client=deepcoin_client,
                executed_at=processed_at,
            )
    except Exception as exc:
        mark_trade_signal_failed(
            session_factory,
            signal_id=signal_id,
            error=str(exc),
            failed_at=processed_at,
        )
        raise
    mark_trade_signal_submitted(
        session_factory,
        signal_id=signal_id,
        result=result,
        processed_at=processed_at,
    )
    return result


def process_next_trade_signal_live(
    session_factory: sessionmaker,
    *,
    deepcoin_client: DeepcoinTradingClientProtocol,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    processed_at: datetime | None = None,
    max_order_legs: int | None = None,
) -> dict[str, Any] | None:
    """Receive and execute the oldest pending Deepcoin trade signal."""

    pending = list_pending_trade_signals(session_factory, venue="deepcoin", limit=1)
    if not pending:
        return None
    return process_trade_signal_live(
        session_factory,
        signal_id=pending[0].id,
        deepcoin_client=deepcoin_client,
        contract_spec_provider=contract_spec_provider,
        processed_at=processed_at,
        max_order_legs=max_order_legs,
    )


def _submit_recovery_signal_direct(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
    submitted_at: datetime | None = None,
    max_order_legs: int | None = None,
) -> dict[str, Any]:
    gate = validate_recovery_live_submit_gate(
        session_factory,
        chat_id=trade_signal.chat_id,
        message_id=trade_signal.message_id,
        symbol=trade_signal.symbol,
        side=trade_signal.side,
        contract_spec_provider=contract_spec_provider,
    )
    if not gate["would_submit"]:
        raise RecoveryLiveSubmitError(
            "live_submit_blocked:" + ",".join(str(code) for code in gate["reason_codes"])
        )
    draft = gate["deepcoin_order_draft"]
    if not isinstance(draft, dict):
        raise RecoveryLiveSubmitError("missing_deepcoin_order_draft")
    queued_draft = (
        trade_signal.payload.get("deepcoin_order_draft")
        if isinstance(trade_signal.payload, dict)
        else None
    )
    if isinstance(queued_draft, dict):
        draft = queued_draft
    order_legs = draft.get("order_legs")
    if not isinstance(order_legs, list) or not order_legs:
        raise RecoveryLiveSubmitError("missing_order_legs")

    submitted_orders: list[dict[str, Any]] = []
    now = submitted_at or datetime.now(UTC)
    source = draft.get("source") if isinstance(draft.get("source"), dict) else {}
    kol_id = str(source.get("kol_id") or "unknown")
    symbol_key = str(draft.get("symbol") or trade_signal.symbol).upper()
    side_key = trade_signal.side.lower()
    warnings = _protection_warnings(draft)

    selected_order_legs = order_legs[:max_order_legs] if max_order_legs else order_legs
    for index, leg in enumerate(selected_order_legs, start=1):
        if not isinstance(leg, dict):
            raise RecoveryLiveSubmitError("invalid_order_leg")
        order_type = str(leg.get("order_type") or "").lower()
        if order_type == "market":
            pre_submit_position_ids = _load_matching_position_ids(
                deepcoin_client,
                draft=draft,
                side=side_key,
            )
            order_payload = build_deepcoin_market_order_payload(draft, leg)
            try:
                response = deepcoin_client.place_order(order_payload)
            except DeepcoinClientError:
                raise
            except Exception as exc:  # pragma: no cover - defensive boundary
                raise DeepcoinClientError(f"Deepcoin client failed: {exc}") from exc

            order_id = _extract_order_id(response)
            if not order_id:
                raise DeepcoinClientError("Deepcoin order response missing order id")
            client_order_id = str(leg.get("client_order_id") or order_payload.get("clOrdId") or "")
            pos_id = _extract_position_id(response) or _find_open_position_id(
                deepcoin_client,
                draft=draft,
                side=side_key,
                exclude_pos_ids=pre_submit_position_ids,
            )
            try:
                protection_payload = build_deepcoin_position_sltp_payload(
                    draft,
                    pos_id=pos_id,
                )
                protection_response = deepcoin_client.set_position_sltp(protection_payload)
            except Exception as exc:  # pragma: no cover - defensive boundary
                protection_payload = locals().get("protection_payload")
                protection_response = {"error": str(exc)}
                warnings.append("position_protection_failed_after_entry_submitted")
        elif order_type == "limit":
            order_payload = build_deepcoin_trigger_order_payload(draft, leg)
            try:
                response = deepcoin_client.trigger_order(order_payload)
            except DeepcoinClientError:
                raise
            except Exception as exc:  # pragma: no cover - defensive boundary
                raise DeepcoinClientError(f"Deepcoin client failed: {exc}") from exc

            order_id = _extract_order_id(response)
            if not order_id:
                raise DeepcoinClientError("Deepcoin trigger limit order response missing order id")
            pos_id = _extract_position_id(response)
            client_order_id = str(leg.get("client_order_id") or "")
            protection_payload = {
                key: order_payload[key]
                for key in ("tpTriggerPx", "slTriggerPx", "tpOrdPx", "slOrdPx")
                if key in order_payload
            }
            protection_response = {"code": "0", "data": {"attached_on_trigger_order": True}}
            order_type = "trigger_limit"
        else:
            order_payload = build_deepcoin_trigger_order_payload(draft, leg)
            try:
                response = deepcoin_client.trigger_order(order_payload)
            except DeepcoinClientError:
                raise
            except Exception as exc:  # pragma: no cover - defensive boundary
                raise DeepcoinClientError(f"Deepcoin client failed: {exc}") from exc

            order_id = _extract_order_id(response)
            if not order_id:
                raise DeepcoinClientError("Deepcoin trigger order response missing order id")
            pos_id = _extract_position_id(response)
            client_order_id = str(leg.get("client_order_id") or "")
            protection_payload = {
                key: order_payload[key]
                for key in ("tpTriggerPx", "slTriggerPx", "tpOrdPx", "slOrdPx")
                if key in order_payload
            }
            protection_response = {"code": "0", "data": {"attached_on_trigger_order": True}}
        submitted_orders.append(
            {
                "leg_index": index,
                "execution_type": order_type or "limit",
                "client_order_id": client_order_id,
                "order_id": order_id,
                "pos_id": pos_id,
                "request": order_payload,
                "response": response,
                "protection_request": protection_payload,
                "protection_response": protection_response,
            }
        )

    protection_failed = any(
        isinstance(order.get("protection_response"), dict)
        and order["protection_response"].get("error")
        for order in submitted_orders
    )
    binding_status = "active" if _join_ids(order["pos_id"] for order in submitted_orders) else "open"
    last_exchange_status = (
        "position_active_protection_failed"
        if protection_failed and binding_status == "active"
        else "order_open_protection_failed" if protection_failed else "submitted"
    )
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id=kol_id,
            chat_id=int(source.get("chat_id") or trade_signal.chat_id),
            message_id=int(source.get("message_id") or trade_signal.message_id),
            symbol=symbol_key,
            side=side_key,
            venue="deepcoin",
            order_id=_join_ids(order["order_id"] for order in submitted_orders),
            client_order_id=_join_ids(order["client_order_id"] for order in submitted_orders),
            pos_id=_join_ids(order["pos_id"] for order in submitted_orders),
            margin_mode=str(draft.get("margin_mode") or "cross"),
            position_mode=str(draft.get("position_mode") or "split"),
            payload={"draft": draft, "submitted_orders": submitted_orders},
            last_exchange_status=last_exchange_status,
            status=binding_status,
            strategy_instance_id=str(draft.get("strategy_instance_id") or ""),
        ),
    )
    _attach_lifecycle_binding(
        session_factory,
        chat_id=int(source.get("chat_id") or trade_signal.chat_id),
        message_id=int(source.get("message_id") or trade_signal.message_id),
        symbol=symbol_key,
        side=side_key,
        binding_id=binding_id,
        entered=bool(_join_ids(order["pos_id"] for order in submitted_orders)),
        updated_at=now,
    )
    _record_submitted_order_events(
        session_factory,
        trade_signal=trade_signal,
        binding_id=binding_id,
        draft=draft,
        submitted_orders=submitted_orders,
        kol_id=kol_id,
        symbol_key=symbol_key,
        side_key=side_key,
        source=source,
        created_at=now,
    )

    return {
        "submitted": True,
        "venue": "deepcoin",
        "signal_id": trade_signal.id,
        "signal_uid": trade_signal.signal_uid,
        "submitted_at": now.isoformat(),
        "source": {
            "chat_id": trade_signal.chat_id,
            "message_id": trade_signal.message_id,
            "symbol": symbol_key,
            "side": side_key,
        },
        "order_count": len(submitted_orders),
        "orders": submitted_orders,
        "deepcoin_order_draft": draft,
        "warnings": warnings,
    }


def _upsert_protection_failed_binding(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    draft: dict[str, Any],
    source: dict[str, Any],
    kol_id: str,
    symbol_key: str,
    side_key: str,
    order: dict[str, Any],
) -> None:
    upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id=kol_id,
            chat_id=int(source.get("chat_id") or trade_signal.chat_id),
            message_id=int(source.get("message_id") or trade_signal.message_id),
            symbol=symbol_key,
            side=side_key,
            venue="deepcoin",
            order_id=str(order.get("order_id") or ""),
            client_order_id=str(order.get("client_order_id") or ""),
            pos_id=str(order.get("pos_id") or ""),
            margin_mode=str(draft.get("margin_mode") or "cross"),
            position_mode=str(draft.get("position_mode") or "split"),
            payload={"draft": draft, "submitted_orders": [order]},
            last_exchange_status="position_active_protection_failed",
            status="active",
            strategy_instance_id=str(draft.get("strategy_instance_id") or ""),
        ),
    )


def _attach_lifecycle_binding(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    binding_id: int,
    entered: bool,
    updated_at: datetime,
) -> None:
    with session_factory() as session:
        lifecycle = (
            session.query(StrategyLifecycle)
            .filter(StrategyLifecycle.chat_id == chat_id)
            .filter(StrategyLifecycle.message_id == message_id)
            .filter(StrategyLifecycle.symbol == symbol)
            .filter(StrategyLifecycle.side == side)
            .order_by(StrategyLifecycle.id.desc())
            .first()
        )
        if lifecycle is None:
            return
        lifecycle.execution_binding_id = binding_id
        if entered and lifecycle.lifecycle_status == "pending_entry":
            lifecycle.lifecycle_status = "entered"
            lifecycle.entered_at = updated_at
        lifecycle.updated_at = updated_at
        session.commit()


def _record_submitted_order_events(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    binding_id: int,
    draft: dict[str, Any],
    submitted_orders: list[dict[str, Any]],
    kol_id: str,
    symbol_key: str,
    side_key: str,
    source: dict[str, Any],
    created_at: datetime,
) -> None:
    strategy_instance_id = str(draft.get("strategy_instance_id") or trade_signal.strategy_instance_id or "")
    chat_id = int(source.get("chat_id") or trade_signal.chat_id)
    message_id = int(source.get("message_id") or trade_signal.message_id)
    for order in submitted_orders:
        execution_type = str(order.get("execution_type") or "").lower()
        base = {
            "execution_binding_id": binding_id,
            "trade_signal_id": trade_signal.id,
            "strategy_instance_id": strategy_instance_id or None,
            "kol_id": kol_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "source_message_id": trade_signal.message_id,
            "symbol": symbol_key,
            "side": side_key,
            "order_id": str(order.get("order_id") or "") or None,
            "client_order_id": str(order.get("client_order_id") or "") or None,
            "pos_id": str(order.get("pos_id") or "") or None,
            "created_at": created_at,
        }
        if execution_type == "market":
            record_execution_event(
                session_factory,
                ExecutionEventRecord(
                    action="open_market_position",
                    reason="live_signal_auto_trade",
                    request=order.get("request"),
                    response=order.get("response"),
                    **base,
                ),
            )
            protection_request = order.get("protection_request")
            if isinstance(protection_request, dict):
                record_execution_event(
                    session_factory,
                    ExecutionEventRecord(
                        action="set_position_tpsl",
                        reason="entry_protection",
                        after=_extract_tpsl_snapshot(protection_request),
                        request=protection_request,
                        response=order.get("protection_response"),
                        related_order_id=base["order_id"],
                        **base,
                    ),
                )
        else:
            request = order.get("request") if isinstance(order.get("request"), dict) else {}
            action = "create_limit_entry" if execution_type == "limit" else "create_trigger_entry"
            record_execution_event(
                session_factory,
                ExecutionEventRecord(
                    action=action,
                    reason="live_signal_auto_trade",
                    after=_extract_tpsl_snapshot(request),
                    request=request,
                    response=order.get("response"),
                    **base,
                ),
            )


def _extract_tpsl_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for source_key, target_key in (
        ("tpTriggerPx", "take_profit"),
        ("tpTriggerPrice", "take_profit"),
        ("closeTPTriggerPrice", "take_profit"),
        ("slTriggerPx", "stop_loss"),
        ("slTriggerPrice", "stop_loss"),
        ("closeSLTriggerPrice", "stop_loss"),
    ):
        value = payload.get(source_key)
        if value in (None, "", "0", 0):
            continue
        snapshot[target_key] = value
    return snapshot


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
        "mrgPosition": _deepcoin_position_mode(str(draft.get("position_mode") or "split")),
    }


def build_deepcoin_market_order_payload(
    draft: dict[str, Any],
    leg: dict[str, Any],
) -> dict[str, Any]:
    """Convert one internal market leg to Deepcoin's place-order payload."""

    quantity = leg.get("quantity")
    if not isinstance(quantity, int | float) or quantity <= 0:
        raise RecoveryLiveSubmitError("non_positive_quantity")

    return {
        "instId": str(draft["instrument_id"]),
        "tdMode": _deepcoin_margin_mode(str(draft.get("margin_mode") or "cross")),
        "side": str(leg["side"]).lower(),
        "posSide": str(leg["position_side"]).lower(),
        "ordType": "market",
        "sz": str(quantity),
        "clOrdId": str(leg.get("client_order_id") or ""),
        "mrgPosition": _deepcoin_position_mode(str(draft.get("position_mode") or "split")),
    }


def build_deepcoin_trigger_order_payload(
    draft: dict[str, Any],
    leg: dict[str, Any],
) -> dict[str, Any]:
    """Convert one internal limit leg to Deepcoin's trigger-order payload."""

    quantity = leg.get("quantity")
    if not isinstance(quantity, int | float) or quantity <= 0:
        raise RecoveryLiveSubmitError("non_positive_quantity")
    price = leg.get("price")
    if not isinstance(price, int | float) or price <= 0:
        raise RecoveryLiveSubmitError("non_positive_price")

    payload: dict[str, Any] = {
        "instId": str(draft["instrument_id"]),
        "productGroup": "Swap",
        "sz": str(quantity),
        "side": str(leg["side"]).lower(),
        "posSide": str(leg["position_side"]).lower(),
        "price": str(price),
        "isCrossMargin": (
            "1"
            if _deepcoin_margin_mode(str(draft.get("margin_mode") or "cross")) == "cross"
            else "0"
        ),
        "orderType": "limit",
        "triggerPrice": str(price),
        "triggerPxType": "last",
        "mrgPosition": _deepcoin_position_mode(str(draft.get("position_mode") or "split")),
        "tdMode": _deepcoin_margin_mode(str(draft.get("margin_mode") or "cross")),
    }
    if leg.get("client_order_id"):
        payload["clOrdId"] = str(leg.get("client_order_id"))
    payload.update(_deepcoin_embedded_sltp_fields(draft))
    return payload


def build_deepcoin_position_sltp_payload(
    draft: dict[str, Any],
    *,
    pos_id: str | None,
) -> dict[str, Any]:
    """Convert an internal draft to Deepcoin's position TP/SL payload."""

    stop_loss = draft.get("stop_loss")
    if not isinstance(stop_loss, int | float) or stop_loss <= 0:
        raise RecoveryLiveSubmitError("missing_stop_loss_for_protection")
    take_profit_price = _first_take_profit_price(draft)

    payload: dict[str, Any] = {
        "instType": "SWAP",
        "instId": str(draft["instrument_id"]),
        "posSide": _position_side_from_draft(draft),
        "mrgPosition": _deepcoin_position_mode(str(draft.get("position_mode") or "split")),
        "tdMode": _deepcoin_margin_mode(str(draft.get("margin_mode") or "cross")),
        "slTriggerPx": str(stop_loss),
        "slTriggerPxType": "last",
        "slOrdPx": "-1",
    }
    if take_profit_price is not None:
        payload.update(
            {
                "tpTriggerPx": str(take_profit_price),
                "tpTriggerPxType": "last",
                "tpOrdPx": "-1",
            }
        )
    if payload["mrgPosition"] == "split":
        if not pos_id:
            raise RecoveryLiveSubmitError("missing_pos_id_for_split_position_sltp")
        payload["posId"] = str(pos_id)
    return payload


def _deepcoin_embedded_sltp_fields(draft: dict[str, Any]) -> dict[str, Any]:
    protection = build_deepcoin_position_sltp_payload(draft, pos_id="placeholder")
    fields = {
        "slTriggerPx": float(protection["slTriggerPx"]),
        "slTriggerPxType": protection["slTriggerPxType"],
        "slOrdPx": -1,
    }
    if "tpTriggerPx" in protection:
        fields.update(
            {
                "tpTriggerPx": float(protection["tpTriggerPx"]),
                "tpTriggerPxType": protection["tpTriggerPxType"],
                "tpOrdPx": -1,
            }
        )
    return fields


def _first_take_profit_price(draft: dict[str, Any]) -> float | None:
    take_profit_legs = draft.get("take_profit_legs")
    if not isinstance(take_profit_legs, list) or not take_profit_legs:
        return None
    first_take_profit = take_profit_legs[0]
    if not isinstance(first_take_profit, dict):
        raise RecoveryLiveSubmitError("invalid_take_profit_for_protection")
    take_profit_price = first_take_profit.get("price")
    if take_profit_price in (None, ""):
        return None
    if not isinstance(take_profit_price, int | float) or take_profit_price <= 0:
        raise RecoveryLiveSubmitError("invalid_take_profit_for_protection")
    return float(take_profit_price)


def _protection_warnings(draft: dict[str, Any]) -> list[str]:
    take_profit_legs = draft.get("take_profit_legs")
    if isinstance(take_profit_legs, list) and len(take_profit_legs) > 1:
        return ["only_first_take_profit_submitted_for_order_sltp"]
    return []


def _position_side_from_draft(draft: dict[str, Any]) -> str:
    order_legs = draft.get("order_legs")
    if isinstance(order_legs, list):
        for leg in order_legs:
            if isinstance(leg, dict) and leg.get("position_side"):
                return str(leg["position_side"]).lower()
    source_side = str((draft.get("source") or {}).get("side") or "").lower()
    return source_side if source_side in {"long", "short"} else "long"


def _deepcoin_margin_mode(value: str) -> str:
    return "cross" if value.lower() in {"cross", "crossed", "full", "全仓"} else "isolated"


def _deepcoin_position_mode(value: str) -> str:
    return "split" if value.lower() in {"split", "hedge", "long_short", "分仓"} else "merge"


def _cancel_unprotected_order(
    deepcoin_client: DeepcoinTradingClientProtocol,
    *,
    draft: dict[str, Any],
    order_id: str | None,
    client_order_id: str | None,
) -> None:
    payload: dict[str, Any] = {
        "instId": str(draft["instrument_id"]),
        "mrgPosition": _deepcoin_position_mode(str(draft.get("position_mode") or "split")),
    }
    if order_id:
        payload["ordId"] = str(order_id)
    if client_order_id:
        payload["clOrdId"] = str(client_order_id)
    try:
        deepcoin_client.cancel_order(payload)
    except Exception as exc:  # pragma: no cover - best-effort exchange cleanup
        raise DeepcoinClientError(
            f"Deepcoin protection failed and cancel also failed: {exc}"
        ) from exc


def _find_open_position_id(
    deepcoin_client: DeepcoinTradingClientProtocol,
    *,
    draft: dict[str, Any],
    side: str,
    preferred_pos_id: str | None = None,
    exclude_pos_ids: set[str] | None = None,
    attempts: int = 5,
    delay_seconds: float = 0.5,
) -> str | None:
    for attempt in range(attempts):
        try:
            positions = deepcoin_client.list_positions(inst_id=str(draft["instrument_id"]))
        except Exception:
            positions = []
        position = _select_matching_position(
            positions,
            draft=draft,
            side=side,
            preferred_pos_id=preferred_pos_id,
            exclude_pos_ids=exclude_pos_ids,
        )
        pos_id = _first_payload_string(position, "posId", "pos_id", "id") if position else None
        if pos_id:
            return pos_id
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    return None


def _load_matching_position_ids(
    deepcoin_client: DeepcoinTradingClientProtocol,
    *,
    draft: dict[str, Any],
    side: str,
) -> set[str] | None:
    try:
        positions = deepcoin_client.list_positions(inst_id=str(draft["instrument_id"]))
    except Exception:
        return None
    result: set[str] = set()
    for position in _matching_positions(positions, draft=draft, side=side):
        pos_id = _first_payload_string(position, "posId", "pos_id", "id")
        if pos_id:
            result.add(pos_id)
    return result


def _select_matching_position(
    positions: list[dict[str, Any]],
    *,
    draft: dict[str, Any],
    side: str,
    preferred_pos_id: str | None = None,
    exclude_pos_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    matches = _matching_positions(positions, draft=draft, side=side)
    if exclude_pos_ids is not None:
        matches = [
            match
            for match in matches
            if _first_payload_string(match, "posId", "pos_id", "id") not in exclude_pos_ids
        ]
    if not matches:
        return None
    if preferred_pos_id:
        for match in matches:
            if _first_payload_string(match, "posId", "pos_id", "id") == str(preferred_pos_id):
                return match
    if len(matches) != 1:
        return None
    return matches[0]


def _matching_positions(
    positions: list[dict[str, Any]],
    *,
    draft: dict[str, Any],
    side: str,
) -> list[dict[str, Any]]:
    instrument_id = str(draft["instrument_id"]).upper()
    margin_mode = _deepcoin_margin_mode(str(draft.get("margin_mode") or "cross"))
    position_mode = _deepcoin_position_mode(str(draft.get("position_mode") or "split"))
    matches = []
    for position in positions:
        if str(position.get("instId") or "").upper() != instrument_id:
            continue
        if str(position.get("posSide") or "").lower() != side.lower():
            continue
        if str(position.get("mrgPosition") or position.get("posMode") or "").lower() not in {
            "",
            position_mode,
        }:
            continue
        if str(position.get("mgnMode") or position.get("tdMode") or "").lower() not in {
            "",
            margin_mode,
        }:
            continue
        try:
            size = abs(float(position.get("pos") or position.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            continue
        matches.append(position)
    return sorted(
        matches,
        key=lambda item: int(float(item.get("uTime") or item.get("cTime") or 0)),
        reverse=True,
    )


def _first_payload_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _extract_order_id(response: dict[str, Any]) -> str | None:
    for payload in _response_payloads(response):
        for key in ("ordId", "orderId", "order_id", "id", "orderSysID", "OrderSysID"):
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
