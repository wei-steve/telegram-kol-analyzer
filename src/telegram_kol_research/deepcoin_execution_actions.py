"""Executable Deepcoin actions for KOL position-management signals."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import DeepcoinTradingClientProtocol
from telegram_kol_research.deepcoin_order_matching import (
    extract_pending_protection_orders,
)
from telegram_kol_research.execution_events import ExecutionEventRecord
from telegram_kol_research.execution_events import record_execution_event
from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    ExecutionBinding,
    ExecutionOrderLeg,
    StrategyLifecycle,
)
from telegram_kol_research.position_authority_lock import (
    serialized_position_authority_mutation,
)
from telegram_kol_research.position_attribution import (
    PositionAttributionError,
    require_equivalent_live_position_economics,
    require_verified_position_ownership,
)
from telegram_kol_research.protection_attribution import match_position_protection
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


@serialized_position_authority_mutation
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
    if action == "partial_close_and_move_stop_to_entry":
        return partial_close_and_move_stop_to_entry(
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


@serialized_position_authority_mutation
def partial_close_and_move_stop_to_entry(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Reduce each exact target, then set that position's stop to its own entry."""

    targets = trade_signal.payload.get("targets") if isinstance(trade_signal.payload, dict) else None
    if not isinstance(targets, list) or not targets:
        raise DeepcoinExecutionActionError("missing_breakeven_management_targets")

    prepared: list[tuple[_LoadedBinding, float, float]] = []
    for target in targets:
        if not isinstance(target, dict):
            raise DeepcoinExecutionActionError("invalid_breakeven_management_target")
        binding_id = target.get("binding_id")
        fraction = _positive_fraction(target.get("fraction"))
        if binding_id in (None, "") or fraction is None:
            raise DeepcoinExecutionActionError("invalid_breakeven_management_target")
        binding = _load_binding_for_signal(session_factory, replace(trade_signal, payload={"binding_id": binding_id}))
        if (
            binding.kol_id != trade_signal.kol_id
            or binding.chat_id != trade_signal.chat_id
            or binding.symbol != trade_signal.symbol.upper()
            or binding.side != trade_signal.side.lower()
            or not binding.pos_id
        ):
            raise DeepcoinExecutionActionError("breakeven_management_target_not_exactly_bound")
        _require_verified_binding_positions(session_factory, binding)
        inst_id = _to_deepcoin_swap_instrument(binding.symbol)
        position = _select_bound_position(deepcoin_client.list_positions(inst_id=inst_id), binding=binding, inst_id=inst_id)
        average_entry = _position_average_entry(position)
        if average_entry is None:
            raise DeepcoinExecutionActionError("missing_position_average_entry")
        prepared.append((binding, fraction, average_entry))

    results: list[dict[str, Any]] = []
    for binding, fraction, average_entry in prepared:
        close_signal = replace(
            trade_signal,
            action="close_position",
            strategy_instance_id=binding.strategy_instance_id,
            payload={"binding_id": binding.id, "fraction": fraction},
        )
        try:
            close_result = close_position_market(session_factory, trade_signal=close_signal, deepcoin_client=deepcoin_client, executed_at=executed_at)
        except Exception as exc:
            results.append({"binding_id": binding.id, "status": "failed", "stage": "partial_close", "error": str(exc)})
            continue
        protection_signal = replace(
            trade_signal,
            action="adjust_stop_loss",
            strategy_instance_id=binding.strategy_instance_id,
            payload={"binding_id": binding.id, "stop_loss": average_entry},
        )
        try:
            protection_result = adjust_position_tpsl(session_factory, trade_signal=protection_signal, deepcoin_client=deepcoin_client, executed_at=executed_at)
        except Exception as exc:
            results.append({"binding_id": binding.id, "status": "failed", "stage": "move_stop", "close": close_result, "error": str(exc)})
            continue
        results.append({"binding_id": binding.id, "status": "submitted", "close": close_result, "protection": protection_result})
    return {"submitted": any(item["status"] == "submitted" for item in results), "action": trade_signal.action, "targets": results}


@serialized_position_authority_mutation
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
    _require_verified_binding_positions(session_factory, binding)
    inst_id = _to_deepcoin_swap_instrument(binding.symbol)
    live_positions = deepcoin_client.list_positions(inst_id=inst_id)
    requested_pos_ids = _requested_position_ids(trade_signal.payload)
    positions = _select_bound_positions(
        live_positions,
        binding=binding,
        inst_id=inst_id,
        requested_pos_ids=requested_pos_ids,
    )
    if len(positions) != 1:
        raise DeepcoinExecutionActionError("ambiguous_bound_position")
    position = positions[0]
    _require_live_position_economics(
        session_factory,
        binding,
        [position],
        snapshot_positions=live_positions,
    )
    pending = deepcoin_client.list_trigger_orders_pending(inst_id=inst_id)
    pos_id = _first_string(position, "posId", "pos_id", "id")
    protection = match_position_protection(live_positions, pending).by_pos_id.get(pos_id or "")
    if protection is not None and protection.status == "present_but_ambiguous":
        raise DeepcoinExecutionActionError("ambiguous_pending_position_tpsl")
    old_order_ids = protection.order_ids if protection is not None else []
    old_order_id_set = set(old_order_ids)
    old_tpsl_rows = [
        row for row in pending if _order_id_from_payload(row) in old_order_id_set
    ]
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


@serialized_position_authority_mutation
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
    requested_pos_ids = _requested_position_ids(trade_signal.payload)
    _require_verified_binding_positions(
        session_factory,
        binding,
        requested_pos_ids=requested_pos_ids,
    )
    inst_id = _to_deepcoin_swap_instrument(binding.symbol)
    live_positions = deepcoin_client.list_positions(inst_id=inst_id)
    positions = _select_bound_positions(
        live_positions,
        binding=binding,
        inst_id=inst_id,
        requested_pos_ids=requested_pos_ids,
    )
    _require_live_position_economics(
        session_factory,
        binding,
        positions,
        snapshot_positions=live_positions,
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
    for submitted_order in submitted_orders:
        _update_entry_leg_status(
            session_factory,
            binding.id,
            status="closed" if submitted_order["full_close"] else "partial_closed",
            updated_at=now,
            pos_id=submitted_order["pos_id"],
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


@serialized_position_authority_mutation
def close_bound_position_market(
    session_factory: sessionmaker,
    *,
    pos_id: str,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Submit one market close for one exact, actively bound live position.

    This intentionally does not mark the binding or strategy lifecycle closed: a
    submitted market order is not proof that the exchange position has closed.
    Reconciliation owns that state transition.
    """

    normalized_pos_id = str(pos_id or "").strip()
    if not normalized_pos_id:
        raise DeepcoinExecutionActionError("missing_pos_id")
    now = executed_at or datetime.now(UTC)
    binding = _load_exact_active_binding_for_position(session_factory, normalized_pos_id)
    _require_verified_binding_positions(
        session_factory,
        binding,
        requested_pos_ids={normalized_pos_id},
    )
    inst_id = _to_deepcoin_swap_instrument(binding.symbol)
    live_positions = deepcoin_client.list_positions(inst_id=inst_id)
    positions = _select_bound_positions(
        live_positions,
        binding=binding,
        inst_id=inst_id,
        requested_pos_ids={normalized_pos_id},
    )
    if len(positions) != 1:
        raise DeepcoinExecutionActionError("ambiguous_exact_bound_live_position")
    position = positions[0]
    live_pos_id = _first_string(position, "posId", "pos_id", "id")
    if live_pos_id != normalized_pos_id:
        raise DeepcoinExecutionActionError("exact_bound_live_position_not_found")
    _require_live_position_economics(
        session_factory,
        binding,
        [position],
        snapshot_positions=live_positions,
    )
    close_size = _position_size(position)
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
        "closePosId": normalized_pos_id,
    }
    _reserve_bound_position_close(
        session_factory, binding=binding, pos_id=normalized_pos_id, now=now
    )
    _record_bound_position_close_reservation_event(
        session_factory,
        binding=binding,
        pos_id=normalized_pos_id,
        status="reserved",
        now=now,
    )
    try:
        response = deepcoin_client.place_order(payload)
    except Exception as exc:
        _mark_bound_position_close_reservation(
            session_factory,
            pos_id=normalized_pos_id,
            status="unknown_exchange_outcome",
            error=str(exc),
            now=now,
        )
        _record_bound_position_close_reservation_event(
            session_factory,
            binding=binding,
            pos_id=normalized_pos_id,
            status="unknown_exchange_outcome",
            error=str(exc),
            now=now,
        )
        raise

    _mark_bound_position_close_reservation(
        session_factory,
        pos_id=normalized_pos_id,
        status="submitted",
        now=now,
    )
    _record_bound_position_close_reservation_event(
        session_factory,
        binding=binding,
        pos_id=normalized_pos_id,
        status="submitted",
        now=now,
    )
    order_id = _extract_order_id(response)
    event_id = record_execution_event(
        session_factory,
        ExecutionEventRecord(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            kol_id=binding.kol_id,
            chat_id=binding.chat_id,
            message_id=binding.message_id,
            source_message_id=binding.message_id,
            symbol=binding.symbol,
            side=binding.side,
            action="close_bound_position_market",
            order_id=order_id,
            pos_id=normalized_pos_id,
            reason="manual_bound_position_close",
            before={"position_size": close_size},
            after={"close_size": close_size, "full_close_requested": True},
            request=payload,
            response=response,
            created_at=now,
        ),
    )
    return {
        "submitted": True,
        "venue": "deepcoin",
        "action": "close_bound_position_market",
        "binding_id": binding.id,
        "pos_id": normalized_pos_id,
        "order_id": order_id,
        "close_size": close_size,
        "event_id": event_id,
        "executed_at": now.isoformat(),
    }


def _reserve_bound_position_close(
    session_factory: sessionmaker,
    *,
    binding: _LoadedBinding,
    pos_id: str,
    now: datetime,
) -> None:
    """Durably claim an exact position before any exchange request is made."""

    with session_factory() as session:
        try:
            session.execute(text("BEGIN IMMEDIATE"))
            require_verified_position_ownership(
                session,
                venue=binding.venue,
                pos_id=pos_id,
            )
            session.add(
                BoundPositionCloseReservation(
                    pos_id=pos_id,
                    execution_binding_id=binding.id,
                    status="reserved",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
        except PositionAttributionError as exc:
            session.rollback()
            raise DeepcoinExecutionActionError(str(exc)) from exc
        except IntegrityError as exc:
            session.rollback()
            raise DeepcoinExecutionActionError("bound_position_close_already_reserved") from exc


def _mark_bound_position_close_reservation(
    session_factory: sessionmaker,
    *,
    pos_id: str,
    status: str,
    now: datetime,
    error: str | None = None,
) -> None:
    with session_factory() as session:
        reservation = (
            session.query(BoundPositionCloseReservation)
            .filter(BoundPositionCloseReservation.pos_id == pos_id)
            .one()
        )
        reservation.status = status
        reservation.last_error = error
        reservation.updated_at = now
        session.commit()


def _record_bound_position_close_reservation_event(
    session_factory: sessionmaker,
    *,
    binding: _LoadedBinding,
    pos_id: str,
    status: str,
    now: datetime,
    error: str | None = None,
) -> None:
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            kol_id=binding.kol_id,
            chat_id=binding.chat_id,
            message_id=binding.message_id,
            source_message_id=binding.message_id,
            symbol=binding.symbol,
            side=binding.side,
            action="close_bound_position_reservation",
            status=status,
            pos_id=pos_id,
            reason="manual_bound_position_close",
            after={"error": error} if error else None,
            created_at=now,
        ),
    )


@serialized_position_authority_mutation
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
        order_id = _order_id_from_payload(order)
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
    for cancelled_order in cancelled_orders:
        _update_entry_leg_status(
            session_factory,
            binding.id,
            status="cancelled",
            updated_at=now,
            order_id=cancelled_order.get("order_id"),
            client_order_id=cancelled_order.get("client_order_id"),
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


@serialized_position_authority_mutation
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
    _update_entry_leg_status(
        session_factory,
        binding.id,
        status="open",
        updated_at=now,
        order_id=old_order_id,
        new_order_id=new_order_id,
        new_client_order_id=str(create_payload.get("clOrdId") or "") or None,
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


def _load_exact_active_binding_for_position(
    session_factory: sessionmaker,
    pos_id: str,
) -> _LoadedBinding:
    """Return the sole active Deepcoin binding containing this exact position ID."""

    with session_factory() as session:
        rows = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.status.in_(["open", "active"]))
            .all()
        )
        matches = [row for row in rows if pos_id in _split_ids(row.pos_id)]
        if len(matches) != 1:
            raise DeepcoinExecutionActionError("position_not_bound_to_exactly_one_active_binding")
        row = matches[0]
        return _LoadedBinding(
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


def _require_verified_binding_positions(
    session_factory: sessionmaker,
    binding: _LoadedBinding,
    *,
    requested_pos_ids: set[str] | None = None,
) -> None:
    bound_pos_ids = set(_split_ids(binding.pos_id))
    target_pos_ids = requested_pos_ids if requested_pos_ids is not None else bound_pos_ids
    if not target_pos_ids or not target_pos_ids.issubset(bound_pos_ids):
        raise DeepcoinExecutionActionError("position_ownership_not_unique")
    try:
        with session_factory() as session:
            for pos_id in sorted(target_pos_ids):
                require_verified_position_ownership(
                    session,
                    venue=binding.venue,
                    pos_id=pos_id,
                )
    except PositionAttributionError as exc:
        raise DeepcoinExecutionActionError(str(exc)) from exc


def _require_live_position_economics(
    session_factory: sessionmaker,
    binding: _LoadedBinding,
    positions: list[dict[str, Any]],
    *,
    snapshot_positions: list[dict[str, Any]],
) -> None:
    """Revalidate selected owners and any full reviewed component in one snapshot."""

    try:
        with session_factory() as session:
            for position in positions:
                pos_id = _first_string(position, "posId", "pos_id", "id")
                if not pos_id:
                    raise PositionAttributionError("live_position_economics_changed")
                leg = require_verified_position_ownership(
                    session,
                    venue=binding.venue,
                    pos_id=pos_id,
                )
                require_equivalent_live_position_economics(
                    leg,
                    live_positions=snapshot_positions,
                    session=session,
                )
    except PositionAttributionError as exc:
        raise DeepcoinExecutionActionError(str(exc)) from exc


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
    requested_pos_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    pos_ids = set(_split_ids(binding.pos_id))
    if requested_pos_ids:
        if not pos_ids.issuperset(requested_pos_ids):
            raise DeepcoinExecutionActionError("requested_pos_id_not_bound")
        pos_ids = requested_pos_ids
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
        order_id = _order_id_from_payload(order)
        client_order_id = _first_string(order, "clOrdId", "clientOrderId", "client_order_id")
        if order_id and order_id in order_ids:
            matches.append(order)
            continue
        if client_order_id and client_order_id in client_order_ids:
            matches.append(order)
    return matches


def _order_id_from_payload(payload: dict[str, Any]) -> str | None:
    return _first_string(
        payload,
        "ordId",
        "orderId",
        "order_id",
        "algoId",
        "triggerOrderId",
        "id",
    )


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


def _position_average_entry(position: dict[str, Any]) -> float | None:
    for key in ("avgPx", "avgPrice", "openAvgPx", "entryPrice"):
        try:
            value = float(position.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _positive_fraction(value: Any) -> float | None:
    try:
        fraction = float(value)
    except (TypeError, ValueError):
        return None
    return fraction if 0 < fraction < 1 else None


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


def _requested_position_ids(payload: dict[str, Any]) -> set[str] | None:
    raw = _first_payload_value(payload, "pos_id", "posId", "position_id", "close_pos_id", "closePosId")
    ids = set(_split_ids(str(raw) if raw is not None else None))
    return ids or None


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


def _update_entry_leg_status(
    session_factory: sessionmaker,
    binding_id: int,
    *,
    status: str,
    updated_at: datetime,
    order_id: str | None = None,
    client_order_id: str | None = None,
    pos_id: str | None = None,
    new_order_id: str | None = None,
    new_client_order_id: str | None = None,
) -> None:
    with session_factory() as session:
        query = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == int(binding_id))
            .filter(ExecutionOrderLeg.purpose == "entry")
        )
        if pos_id:
            query = query.filter(ExecutionOrderLeg.pos_id == str(pos_id))
        elif order_id or client_order_id:
            matches = []
            if order_id:
                matches.append(ExecutionOrderLeg.order_id == str(order_id))
            if client_order_id:
                matches.append(ExecutionOrderLeg.client_order_id == str(client_order_id))
            query = query.filter(matches[0] if len(matches) == 1 else matches[0] | matches[1])
        else:
            return
        for leg in query.all():
            leg.status = status
            if new_order_id is not None:
                leg.order_id = new_order_id
            if new_client_order_id is not None:
                leg.client_order_id = new_client_order_id
            leg.updated_at = updated_at
        session.commit()
