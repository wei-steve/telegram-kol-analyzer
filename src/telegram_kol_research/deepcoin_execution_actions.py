"""Executable Deepcoin actions for KOL position-management signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import DeepcoinTradingClientProtocol
from telegram_kol_research.deepcoin_order_matching import (
    extract_pending_protection_orders,
    pending_tpsl_order_ids_for_position,
    select_position_tpsl_orders,
)
from telegram_kol_research.execution_events import ExecutionEventRecord
from telegram_kol_research.execution_events import record_execution_event
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.trade_signals import TradeSignalRecord


class DeepcoinExecutionActionError(RuntimeError):
    """Raised when a management signal cannot be executed unambiguously."""


@dataclass(slots=True)
class _LoadedBinding:
    id: int
    strategy_instance_id: str | None
    kol_id: str
    chat_id: int
    message_id: int
    symbol: str
    side: str
    venue: str
    order_id: str | None
    client_order_id: str | None
    pos_id: str | None
    margin_mode: str
    position_mode: str
    status: str


def execute_deepcoin_management_signal(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Execute one non-entry Deepcoin trade signal from the durable queue."""

    action = trade_signal.action.lower()
    if action in {"close_position", "exit_position", "temporary_exit", "temporary_close"}:
        return close_position_market(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=deepcoin_client,
            executed_at=executed_at,
        )
    if action in {"set_position_tpsl", "adjust_position_tpsl", "adjust_stop_loss", "adjust_take_profit"}:
        return adjust_position_tpsl(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=deepcoin_client,
            executed_at=executed_at,
            require_existing=action != "set_position_tpsl",
        )
    if action in {"cancel_entry", "cancel_limit_entry", "cancel_trigger_entry"}:
        return cancel_entry_order(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=deepcoin_client,
            executed_at=executed_at,
        )
    if action in {"adjust_trigger_entry_tpsl", "recreate_trigger_entry"}:
        return recreate_trigger_entry_tpsl(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=deepcoin_client,
            executed_at=executed_at,
        )
    raise DeepcoinExecutionActionError(f"unsupported_trade_signal_action:{trade_signal.action}")


def adjust_position_tpsl(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
    require_existing: bool = True,
) -> dict[str, Any]:
    """Adjust position TP/SL by canceling matched old TPSL rows before setting new rows."""

    now = executed_at or datetime.now(UTC)
    binding = _load_binding_for_signal(session_factory, trade_signal)
    inst_id = _to_deepcoin_swap_instrument(binding.symbol)
    position = _select_bound_position(
        deepcoin_client.list_positions(inst_id=inst_id),
        binding=binding,
        inst_id=inst_id,
    )
    pending = deepcoin_client.list_trigger_orders_pending(inst_id=inst_id)
    old_tpsl_rows = select_position_tpsl_orders(position=position, pending_trigger_orders=pending)
    old_order_ids = pending_tpsl_order_ids_for_position(
        position=position,
        pending_trigger_orders=pending,
    )
    if require_existing and not old_order_ids:
        raise DeepcoinExecutionActionError("no_existing_position_tpsl_to_adjust")

    before = _tpsl_snapshot(old_tpsl_rows)
    after = _resolve_adjusted_tpsl_snapshot(
        before=before,
        payload=trade_signal.payload,
        action=trade_signal.action,
    )
    if not after:
        raise DeepcoinExecutionActionError("missing_new_tpsl_price")

    cancel_responses: list[dict[str, Any]] = []
    for order_id in old_order_ids:
        cancel_payload = {"instId": inst_id, "ordId": str(order_id)}
        response = deepcoin_client.cancel_trigger_order(cancel_payload)
        cancel_responses.append({"order_id": str(order_id), "response": response})
        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                execution_binding_id=binding.id,
                trade_signal_id=trade_signal.id,
                strategy_instance_id=binding.strategy_instance_id,
                kol_id=binding.kol_id,
                chat_id=binding.chat_id,
                message_id=binding.message_id,
                source_message_id=trade_signal.message_id,
                symbol=binding.symbol,
                side=binding.side,
                action="cancel_position_tpsl",
                order_id=str(order_id),
                pos_id=_first_string(position, "posId", "pos_id", "id"),
                reason=trade_signal.action,
                before=before,
                request=cancel_payload,
                response=response,
                created_at=now,
            ),
        )

    set_payload = _build_position_tpsl_payload(binding=binding, position=position, inst_id=inst_id, after=after)
    set_response = deepcoin_client.set_position_sltp(set_payload)
    new_order_id = _extract_order_id(set_response)
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            execution_binding_id=binding.id,
            trade_signal_id=trade_signal.id,
            strategy_instance_id=binding.strategy_instance_id,
            kol_id=binding.kol_id,
            chat_id=binding.chat_id,
            message_id=binding.message_id,
            source_message_id=trade_signal.message_id,
            symbol=binding.symbol,
            side=binding.side,
            action="adjust_position_tpsl" if require_existing else "set_position_tpsl",
            order_id=new_order_id,
            related_order_id=",".join(old_order_ids) if old_order_ids else None,
            pos_id=_first_string(position, "posId", "pos_id", "id"),
            reason=trade_signal.action,
            before=before or None,
            after=after,
            request=set_payload,
            response=set_response,
            created_at=now,
        ),
    )
    _update_binding_status(
        session_factory,
        binding.id,
        status="active",
        last_exchange_status="position_tpsl_adjusted",
        updated_at=now,
    )
    return {
        "submitted": True,
        "venue": "deepcoin",
        "signal_id": trade_signal.id,
        "action": trade_signal.action,
        "binding_id": binding.id,
        "pos_id": _first_string(position, "posId", "pos_id", "id"),
        "cancelled_tpsl_order_ids": old_order_ids,
        "before": before,
        "after": after,
        "request": set_payload,
        "response": set_response,
        "cancel_responses": cancel_responses,
        "executed_at": now.isoformat(),
    }


def close_position_market(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Close a bound split-position by market order using closePosId."""

    now = executed_at or datetime.now(UTC)
    binding = _load_binding_for_signal(session_factory, trade_signal)
    inst_id = _to_deepcoin_swap_instrument(binding.symbol)
    positions = _select_bound_positions(
        deepcoin_client.list_positions(inst_id=inst_id),
        binding=binding,
        inst_id=inst_id,
    )
    submitted_orders: list[dict[str, Any]] = []
    full_close = True
    for position in positions:
        current_size = _position_size(position)
        close_size = _resolve_close_size(trade_signal.payload, current_size)
        if close_size <= 0:
            raise DeepcoinExecutionActionError("non_positive_close_size")
        payload = {
            "instId": inst_id,
            "tdMode": _deepcoin_margin_mode(binding.margin_mode),
            "side": "sell" if binding.side.lower() == "long" else "buy",
            "posSide": binding.side.lower(),
            "ordType": "market",
            "sz": f"{close_size:g}",
            "mrgPosition": _deepcoin_position_mode(binding.position_mode),
            "closePosId": str(_first_string(position, "posId", "pos_id", "id") or ""),
        }
        if not payload["closePosId"]:
            raise DeepcoinExecutionActionError("missing_close_pos_id")
        response = deepcoin_client.place_order(payload)
        order_id = _extract_order_id(response)
        position_full_close = close_size >= current_size * 0.999
        full_close = full_close and position_full_close
        submitted_orders.append(
            {
                "order_id": order_id,
                "pos_id": payload["closePosId"],
                "close_size": close_size,
                "full_close": position_full_close,
                "request": payload,
                "response": response,
            }
        )
        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                execution_binding_id=binding.id,
                trade_signal_id=trade_signal.id,
                strategy_instance_id=binding.strategy_instance_id,
                kol_id=binding.kol_id,
                chat_id=binding.chat_id,
                message_id=binding.message_id,
                source_message_id=trade_signal.message_id,
                symbol=binding.symbol,
                side=binding.side,
                action="close_position_market",
                order_id=order_id,
                pos_id=payload["closePosId"],
                reason=trade_signal.action,
                before={"position_size": current_size},
                after={"close_size": close_size, "full_close": position_full_close},
                request=payload,
                response=response,
                created_at=now,
            ),
        )
    _update_binding_status(
        session_factory,
        binding.id,
        status="closed" if full_close else "active",
        last_exchange_status="close_position_submitted" if full_close else "partial_close_submitted",
        updated_at=now,
    )
    return {
        "submitted": True,
        "venue": "deepcoin",
        "signal_id": trade_signal.id,
        "action": trade_signal.action,
        "binding_id": binding.id,
        "order_id": ",".join(item["order_id"] for item in submitted_orders if item["order_id"]),
        "pos_id": ",".join(item["pos_id"] for item in submitted_orders if item["pos_id"]),
        "close_size": sum(float(item["close_size"]) for item in submitted_orders),
        "full_close": full_close,
        "closed_positions": submitted_orders,
        "executed_at": now.isoformat(),
    }


def cancel_entry_order(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Cancel a bound pending regular or trigger entry order."""

    now = executed_at or datetime.now(UTC)
    binding = _load_binding_for_signal(session_factory, trade_signal)
    inst_id = _to_deepcoin_swap_instrument(binding.symbol)
    trigger_orders = _select_bound_orders(
        deepcoin_client.list_trigger_orders_pending(inst_id=inst_id),
        binding=binding,
    )
    regular_orders: list[dict[str, Any]] = []
    if not trigger_orders:
        regular_orders = _select_bound_orders(
            deepcoin_client.list_open_orders(inst_id=inst_id),
            binding=binding,
        )
    if not trigger_orders and not regular_orders:
        raise DeepcoinExecutionActionError("no_bound_pending_entry_order")

    cancelled_orders: list[dict[str, Any]] = []
    event_action = "cancel_trigger_entry" if trigger_orders else "cancel_regular_entry"
    cancel_type = "trigger" if trigger_orders else "regular"
    for order in trigger_orders or regular_orders:
        order_id = _first_string(order, "ordId", "orderId", "order_id", "id")
        client_order_id = _first_string(order, "clOrdId", "clientOrderId", "client_order_id")
        if not order_id and not client_order_id:
            raise DeepcoinExecutionActionError("missing_cancel_order_id")
        cancel_payload: dict[str, Any] = {"instId": inst_id}
        if order_id:
            cancel_payload["ordId"] = order_id
        if client_order_id:
            cancel_payload["clOrdId"] = client_order_id
        if trigger_orders:
            response = deepcoin_client.cancel_trigger_order(cancel_payload)
        else:
            cancel_payload["mrgPosition"] = _deepcoin_position_mode(binding.position_mode)
            response = deepcoin_client.cancel_order(cancel_payload)
        cancelled_orders.append(
            {
                "order_id": order_id,
                "client_order_id": client_order_id,
                "request": cancel_payload,
                "response": response,
            }
        )
        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                execution_binding_id=binding.id,
                trade_signal_id=trade_signal.id,
                strategy_instance_id=binding.strategy_instance_id,
                kol_id=binding.kol_id,
                chat_id=binding.chat_id,
                message_id=binding.message_id,
                source_message_id=trade_signal.message_id,
                symbol=binding.symbol,
                side=binding.side,
                action=event_action,
                order_id=order_id,
                client_order_id=client_order_id,
                reason=trade_signal.action,
                before=order,
                request=cancel_payload,
                response=response,
                created_at=now,
            ),
        )
    _update_binding_status(
        session_factory,
        binding.id,
        status="cancelled",
        last_exchange_status=event_action,
        updated_at=now,
    )
    return {
        "submitted": True,
        "venue": "deepcoin",
        "signal_id": trade_signal.id,
        "action": trade_signal.action,
        "binding_id": binding.id,
        "order_id": ",".join(item["order_id"] for item in cancelled_orders if item["order_id"]),
        "client_order_id": ",".join(
            item["client_order_id"] for item in cancelled_orders if item["client_order_id"]
        ),
        "cancel_type": cancel_type,
        "cancelled_orders": cancelled_orders,
        "executed_at": now.isoformat(),
    }


def recreate_trigger_entry_tpsl(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Adjust an unfilled trigger-limit entry by canceling and recreating it."""

    now = executed_at or datetime.now(UTC)
    binding = _load_binding_for_signal(session_factory, trade_signal)
    inst_id = _to_deepcoin_swap_instrument(binding.symbol)
    old_order = _select_bound_order(
        deepcoin_client.list_trigger_orders_pending(inst_id=inst_id),
        binding=binding,
    )
    if old_order is None:
        raise DeepcoinExecutionActionError("no_bound_pending_trigger_entry")
    old_order_id = _first_string(old_order, "ordId", "orderId", "order_id", "id")
    if not old_order_id:
        raise DeepcoinExecutionActionError("missing_trigger_order_id")

    before = _tpsl_snapshot([old_order])
    after = _resolve_adjusted_tpsl_snapshot(
        before=before,
        payload=trade_signal.payload,
        action=trade_signal.action,
    )
    if not after:
        raise DeepcoinExecutionActionError("missing_new_tpsl_price")

    cancel_payload = {"instId": inst_id, "ordId": old_order_id}
    cancel_response = deepcoin_client.cancel_trigger_order(cancel_payload)
    create_payload = _build_trigger_entry_payload_from_existing(
        binding=binding,
        old_order=old_order,
        inst_id=inst_id,
        after=after,
        payload=trade_signal.payload,
    )
    create_response = deepcoin_client.trigger_order(create_payload)
    new_order_id = _extract_order_id(create_response)
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            execution_binding_id=binding.id,
            trade_signal_id=trade_signal.id,
            strategy_instance_id=binding.strategy_instance_id,
            kol_id=binding.kol_id,
            chat_id=binding.chat_id,
            message_id=binding.message_id,
            source_message_id=trade_signal.message_id,
            symbol=binding.symbol,
            side=binding.side,
            action="recreate_trigger_entry",
            order_id=new_order_id,
            related_order_id=old_order_id,
            reason=trade_signal.action,
            before=before,
            after=after,
            request=create_payload,
            response=create_response,
            created_at=now,
        ),
    )
    _update_binding_order(
        session_factory,
        binding.id,
        order_id=new_order_id,
        client_order_id=str(create_payload.get("clOrdId") or "") or binding.client_order_id,
        last_exchange_status="trigger_entry_recreated",
        updated_at=now,
    )
    return {
        "submitted": True,
        "venue": "deepcoin",
        "signal_id": trade_signal.id,
        "action": trade_signal.action,
        "binding_id": binding.id,
        "old_order_id": old_order_id,
        "new_order_id": new_order_id,
        "before": before,
        "after": after,
        "cancel_request": cancel_payload,
        "cancel_response": cancel_response,
        "request": create_payload,
        "response": create_response,
        "executed_at": now.isoformat(),
    }


def _load_binding_for_signal(
    session_factory: sessionmaker,
    trade_signal: TradeSignalRecord,
) -> _LoadedBinding:
    payload = trade_signal.payload if isinstance(trade_signal.payload, dict) else {}
    binding_id = payload.get("binding_id") or payload.get("execution_binding_id")
    with session_factory() as session:
        query = session.query(ExecutionBinding).filter(ExecutionBinding.venue == "deepcoin")
        if binding_id not in (None, ""):
            row = query.filter(ExecutionBinding.id == int(binding_id)).one_or_none()
        elif trade_signal.strategy_instance_id:
            row = (
                query.filter(ExecutionBinding.strategy_instance_id == trade_signal.strategy_instance_id)
                .filter(ExecutionBinding.status.in_(["open", "active"]))
                .order_by(ExecutionBinding.id.desc())
                .one_or_none()
            )
        else:
            matches = (
                query.filter(ExecutionBinding.chat_id == trade_signal.chat_id)
                .filter(ExecutionBinding.symbol == trade_signal.symbol.upper())
                .filter(ExecutionBinding.side == trade_signal.side.lower())
                .filter(ExecutionBinding.status.in_(["open", "active"]))
                .order_by(ExecutionBinding.id.desc())
                .all()
            )
            if len(matches) > 1:
                raise DeepcoinExecutionActionError("ambiguous_execution_binding")
            row = matches[0] if matches else None
        if row is None:
            raise DeepcoinExecutionActionError("execution_binding_not_found")
        loaded = _LoadedBinding(
            id=int(row.id),
            strategy_instance_id=row.strategy_instance_id,
            kol_id=row.kol_id,
            chat_id=int(row.chat_id),
            message_id=int(row.message_id),
            symbol=row.symbol,
            side=row.side,
            venue=row.venue,
            order_id=row.order_id,
            client_order_id=row.client_order_id,
            pos_id=row.pos_id,
            margin_mode=row.margin_mode,
            position_mode=row.position_mode,
            status=row.status,
        )
    return loaded


def _select_bound_position(
    positions: list[dict[str, Any]],
    *,
    binding: _LoadedBinding,
    inst_id: str,
) -> dict[str, Any]:
    matches = _select_bound_positions(positions, binding=binding, inst_id=inst_id)
    if len(matches) == 1:
        return matches[0]
    raise DeepcoinExecutionActionError("ambiguous_bound_position")


def _select_bound_positions(
    positions: list[dict[str, Any]],
    *,
    binding: _LoadedBinding,
    inst_id: str,
) -> list[dict[str, Any]]:
    pos_ids = set(_split_ids(binding.pos_id))
    matches: list[dict[str, Any]] = []
    for position in positions:
        if str(position.get("instId") or "").upper() != inst_id.upper():
            continue
        if _normalize_side(str(position.get("posSide") or position.get("side") or "")) != binding.side.lower():
            continue
        if pos_ids and _first_string(position, "posId", "pos_id", "id") not in pos_ids:
            continue
        if _position_size(position) <= 0:
            continue
        matches.append(position)
    if not matches:
        raise DeepcoinExecutionActionError("bound_position_not_found")
    return matches


def _select_bound_order(
    orders: list[dict[str, Any]],
    *,
    binding: _LoadedBinding,
) -> dict[str, Any] | None:
    matches = _select_bound_orders(orders, binding=binding)
    if len(matches) > 1:
        raise DeepcoinExecutionActionError("ambiguous_bound_order")
    return matches[0] if matches else None


def _select_bound_orders(
    orders: list[dict[str, Any]],
    *,
    binding: _LoadedBinding,
) -> list[dict[str, Any]]:
    order_ids = set(_split_ids(binding.order_id))
    client_order_ids = set(_split_ids(binding.client_order_id))
    matches = []
    for order in orders:
        order_id = _first_string(order, "ordId", "orderId", "order_id", "id")
        client_order_id = _first_string(order, "clOrdId", "clientOrderId", "client_order_id")
        if order_id and order_id in order_ids:
            matches.append(order)
            continue
        if client_order_id and client_order_id in client_order_ids:
            matches.append(order)
    return matches


def _resolve_adjusted_tpsl_snapshot(
    *,
    before: dict[str, Any],
    payload: dict[str, Any],
    action: str,
) -> dict[str, Any]:
    after = dict(before)
    take_profit = _first_payload_value(payload, "take_profit", "take_profit_price", "tp", "tpTriggerPx")
    stop_loss = _first_payload_value(payload, "stop_loss", "stop_loss_price", "sl", "slTriggerPx")
    action_name = action.lower()
    if take_profit is not None:
        after["take_profit"] = take_profit
    elif action_name == "adjust_take_profit":
        after.pop("take_profit", None)
    if stop_loss is not None:
        after["stop_loss"] = stop_loss
    elif action_name == "adjust_stop_loss":
        after.pop("stop_loss", None)
    return {key: value for key, value in after.items() if value not in (None, "", 0, "0")}


def _build_position_tpsl_payload(
    *,
    binding: _LoadedBinding,
    position: dict[str, Any],
    inst_id: str,
    after: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "instType": "SWAP",
        "instId": inst_id,
        "posSide": binding.side.lower(),
        "mrgPosition": _deepcoin_position_mode(binding.position_mode),
        "tdMode": _deepcoin_margin_mode(binding.margin_mode),
    }
    pos_id = _first_string(position, "posId", "pos_id", "id")
    if payload["mrgPosition"] == "split":
        if not pos_id:
            raise DeepcoinExecutionActionError("missing_pos_id_for_position_tpsl")
        payload["posId"] = pos_id
    if after.get("take_profit") is not None:
        payload.update(
            {
                "tpTriggerPx": str(after["take_profit"]),
                "tpTriggerPxType": "last",
                "tpOrdPx": "-1",
            }
        )
    if after.get("stop_loss") is not None:
        payload.update(
            {
                "slTriggerPx": str(after["stop_loss"]),
                "slTriggerPxType": "last",
                "slOrdPx": "-1",
            }
        )
    return payload


def _build_trigger_entry_payload_from_existing(
    *,
    binding: _LoadedBinding,
    old_order: dict[str, Any],
    inst_id: str,
    after: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    price = _first_payload_value(payload, "entry_price", "price", "trigger_price")
    if price is None:
        price = _first_payload_value(old_order, "price", "px", "triggerPrice", "triggerPx")
    size = _first_payload_value(payload, "quantity", "size", "sz")
    if size is None:
        size = _first_payload_value(old_order, "sz", "size")
    if price is None or size is None:
        raise DeepcoinExecutionActionError("missing_trigger_entry_price_or_size")
    side = str(old_order.get("side") or ("buy" if binding.side.lower() == "long" else "sell")).lower()
    pos_side = str(old_order.get("posSide") or binding.side).lower()
    create: dict[str, Any] = {
        "instId": inst_id,
        "productGroup": str(old_order.get("productGroup") or "Swap"),
        "sz": str(size),
        "side": side,
        "posSide": pos_side,
        "price": str(price),
        "isCrossMargin": "1" if _deepcoin_margin_mode(binding.margin_mode) == "cross" else "0",
        "orderType": str(old_order.get("orderType") or "limit"),
        "triggerPrice": str(_first_payload_value(old_order, "triggerPrice", "triggerPx") or price),
        "triggerPxType": str(old_order.get("triggerPxType") or "last"),
        "mrgPosition": _deepcoin_position_mode(binding.position_mode),
        "tdMode": _deepcoin_margin_mode(binding.margin_mode),
    }
    client_order_id = payload.get("client_order_id") or payload.get("clOrdId") or old_order.get("clOrdId")
    if client_order_id:
        create["clOrdId"] = str(client_order_id)
    if after.get("take_profit") is not None:
        create.update(
            {
                "tpTriggerPx": float(after["take_profit"]),
                "tpTriggerPxType": "last",
                "tpOrdPx": -1,
            }
        )
    if after.get("stop_loss") is not None:
        create.update(
            {
                "slTriggerPx": float(after["stop_loss"]),
                "slTriggerPxType": "last",
                "slOrdPx": -1,
            }
        )
    return create


def _tpsl_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for order in extract_pending_protection_orders(rows):
        if order.purpose == "take_profit":
            snapshot["take_profit"] = order.trigger_price
        elif order.purpose == "stop_loss":
            snapshot["stop_loss"] = order.trigger_price
    return snapshot


def _resolve_close_size(payload: dict[str, Any], current_size: float) -> float:
    raw_size = _first_payload_value(payload, "quantity", "size", "close_size", "sz")
    if raw_size is not None:
        return float(raw_size)
    fraction = _first_payload_value(payload, "fraction", "close_fraction", "ratio")
    if fraction is None:
        return current_size
    fraction_float = float(fraction)
    if fraction_float > 1:
        fraction_float = fraction_float / 100
    return current_size * fraction_float


def _position_size(position: dict[str, Any]) -> float:
    try:
        return abs(float(position.get("pos") or position.get("size") or 0))
    except (TypeError, ValueError):
        return 0.0


def _first_payload_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _split_ids(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _normalize_side(value: str) -> str:
    side = value.lower()
    if side == "buy":
        return "long"
    if side == "sell":
        return "short"
    return side


def _to_deepcoin_swap_instrument(symbol: str) -> str:
    normalized = symbol.upper().replace("_", "-")
    if normalized.endswith("-SWAP"):
        return normalized
    if normalized.endswith("-USDT"):
        return f"{normalized}-SWAP"
    if normalized.endswith("USDT"):
        return f"{normalized[:-4]}-USDT-SWAP"
    return f"{normalized}-USDT-SWAP"


def _deepcoin_margin_mode(value: str) -> str:
    return "cross" if value.lower() in {"cross", "crossed", "full"} else "isolated"


def _deepcoin_position_mode(value: str) -> str:
    return "split" if value.lower() in {"split", "hedge", "long_short"} else "merge"


def _extract_order_id(response: dict[str, Any]) -> str | None:
    for payload in _response_payloads(response):
        for key in ("ordId", "orderId", "order_id", "id", "orderSysID", "OrderSysID"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _response_payloads(response: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = [response]
    data = response.get("data")
    if isinstance(data, dict):
        payloads.append(data)
    elif isinstance(data, list):
        payloads.extend(item for item in data if isinstance(item, dict))
    return payloads


def _update_binding_status(
    session_factory: sessionmaker,
    binding_id: int,
    *,
    status: str,
    last_exchange_status: str,
    updated_at: datetime,
) -> None:
    with session_factory() as session:
        row = session.get(ExecutionBinding, binding_id)
        if row is not None:
            row.status = status
            row.last_exchange_status = last_exchange_status
            row.updated_at = updated_at
            session.commit()


def _update_binding_order(
    session_factory: sessionmaker,
    binding_id: int,
    *,
    order_id: str | None,
    client_order_id: str | None,
    last_exchange_status: str,
    updated_at: datetime,
) -> None:
    with session_factory() as session:
        row = session.get(ExecutionBinding, binding_id)
        if row is not None:
            row.order_id = order_id
            row.client_order_id = client_order_id
            row.last_exchange_status = last_exchange_status
            row.updated_at = updated_at
            session.commit()
