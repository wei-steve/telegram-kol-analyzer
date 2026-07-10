"""Persistence helpers for exchange order/position attribution bindings."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_readonly import (
    DeepcoinOrderBinding,
    DeepcoinReadOnlyAccountState,
    DeepcoinReadOnlyClient,
)
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import ExecutionOrderLeg

PENDING_ENTRY_RECOVERY_WINDOW_HOURS = 6


@dataclass(slots=True)
class ExecutionBindingRecord:
    kol_id: str
    chat_id: int
    message_id: int
    symbol: str
    side: str
    venue: str = "deepcoin"
    order_id: str | None = None
    client_order_id: str | None = None
    pos_id: str | None = None
    margin_mode: str = "cross"
    position_mode: str = "split"
    payload: dict[str, Any] | None = None
    last_exchange_status: str | None = None
    status: str = "open"
    strategy_instance_id: str | None = None


@dataclass(slots=True)
class ExecutionOrderLegRecord:
    execution_binding_id: int
    leg_index: int
    purpose: str = "entry"
    order_kind: str = "unknown"
    strategy_instance_id: str | None = None
    order_id: str | None = None
    client_order_id: str | None = None
    pos_id: str | None = None
    status: str = "submitted"
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None


@dataclass(slots=True)
class ExecutionOrderLegSnapshot:
    id: int
    execution_binding_id: int
    strategy_instance_id: str | None
    leg_index: int
    purpose: str
    order_kind: str
    order_id: str | None
    client_order_id: str | None
    pos_id: str | None
    status: str


@dataclass(slots=True)
class ExecutionReconciliationResult:
    active: int = 0
    open: int = 0
    stale: int = 0
    updated: int = 0


@dataclass(slots=True)
class ManualCloseSyncResult:
    checked: int = 0
    manually_closed: int = 0
    skipped_without_pos_id: int = 0


def build_strategy_instance_id(
    *,
    venue: str,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
) -> str:
    """Build a stable local strategy key used across restart recovery."""

    return (
        f"{venue.lower()}:{int(chat_id)}:{int(message_id)}:"
        f"{symbol.upper()}:{side.lower()}"
    )


def build_client_order_id(
    *,
    strategy_instance_id: str,
    leg_index: int = 1,
    purpose: str = "entry",
    kol_code: str | None = None,
    message_id: int | None = None,
) -> str:
    """Build a deterministic client order id that remains stable after restarts."""

    if kol_code and message_id:
        prefix = f"TK{kol_code.upper()[:8]}{int(message_id)}"
        purpose_code = _client_order_purpose_code(purpose)
        candidate = f"{prefix}{purpose_code}{int(leg_index)}"
        if candidate.isalnum() and len(candidate) <= 20:
            return candidate
        digest_raw = f"{strategy_instance_id}:{purpose}:{int(leg_index)}"
        digest = hashlib.sha1(digest_raw.encode("utf-8")).hexdigest()[:4].upper()
        available = max(2, 20 - len(f"TK{purpose_code}{int(leg_index)}{digest}"))
        candidate = f"TK{kol_code.upper()[:available]}{purpose_code}{int(leg_index)}{digest}"
        return candidate[:20]

    raw = f"{strategy_instance_id}:{purpose}:{int(leg_index)}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14].upper()
    return f"TK{digest}{int(leg_index)}"[:20]


def _client_order_purpose_code(purpose: str) -> str:
    return {
        "entry": "E",
        "exit": "X",
        "take_profit": "T",
        "stop_loss": "S",
    }.get(str(purpose or "").lower(), "O")


def upsert_execution_binding(
    session_factory: sessionmaker,
    record: ExecutionBindingRecord,
) -> int:
    """Create or update the local exchange binding for one source strategy."""

    symbol = record.symbol.upper()
    side = record.side.lower()
    venue = record.venue.lower()
    strategy_instance_id = record.strategy_instance_id or build_strategy_instance_id(
        venue=venue,
        chat_id=record.chat_id,
        message_id=record.message_id,
        symbol=symbol,
        side=side,
    )
    payload_json = (
        json.dumps(record.payload, ensure_ascii=False, sort_keys=True)
        if record.payload is not None
        else None
    )

    with session_factory() as session:
        binding = (
            session.query(ExecutionBinding)
            .filter(
                ExecutionBinding.venue == venue,
                ExecutionBinding.chat_id == record.chat_id,
                ExecutionBinding.message_id == record.message_id,
                ExecutionBinding.symbol == symbol,
                ExecutionBinding.side == side,
            )
            .one_or_none()
        )
        if binding is None:
            binding = ExecutionBinding(
                kol_id=record.kol_id,
                chat_id=record.chat_id,
                message_id=record.message_id,
                symbol=symbol,
                side=side,
                venue=venue,
            )
            session.add(binding)
            session.flush()

        binding.strategy_instance_id = strategy_instance_id
        binding.kol_id = record.kol_id
        binding.order_id = record.order_id
        binding.client_order_id = record.client_order_id
        binding.pos_id = record.pos_id
        binding.margin_mode = _normalize_margin_mode(record.margin_mode)
        binding.position_mode = _normalize_position_mode(record.position_mode)
        binding.payload_json = payload_json
        binding.last_exchange_status = record.last_exchange_status
        binding.status = record.status
        binding.updated_at = datetime.now(UTC)
        binding_id = binding.id
        session.commit()

    return binding_id


def load_deepcoin_order_bindings(
    session_factory: sessionmaker,
) -> list[DeepcoinOrderBinding]:
    """Load active/open Deepcoin bindings for read-only account state mapping."""

    with session_factory() as session:
        rows = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.status.in_(["open", "active"]))
            .order_by(ExecutionBinding.id.asc())
            .all()
        )

        return [
            DeepcoinOrderBinding(
                kol_id=row.kol_id,
                chat_id=row.chat_id,
                source_message_id=row.message_id,
                symbol=row.symbol,
                side=row.side,
                pos_id=row.pos_id,
                order_id=row.order_id,
                client_order_id=row.client_order_id,
            )
            for row in rows
        ]


def reconcile_deepcoin_execution_bindings(
    session_factory: sessionmaker,
    *,
    client: DeepcoinReadOnlyClient,
    recovered_at: datetime | None = None,
) -> ExecutionReconciliationResult:
    """Refresh persisted Deepcoin bindings against exchange open orders/positions.

    This is intentionally read-only against Deepcoin: on process restart it
    marks locally known strategies as active/open/stale using the exchange's
    current state, so later close/TP/SL handling can continue from durable data.
    """

    now = recovered_at or datetime.now(UTC)
    positions = client.list_positions()
    orders = client.list_open_orders()
    positions_by_pos_id = {
        _first_string(position, "posId", "pos_id", "id"): position
        for position in positions
        if _first_string(position, "posId", "pos_id", "id")
    }
    active_positions = [position for position in positions if _has_nonzero_size(position)]

    result = ExecutionReconciliationResult()
    with session_factory() as session:
        rows = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(
                or_(
                    ExecutionBinding.status.in_(["open", "active", "unknown", "stale"]),
                    ExecutionBinding.status == "closed",
                )
            )
            .order_by(ExecutionBinding.id.asc())
            .all()
        )
        rows = [
            row
            for row in rows
            if row.status != "closed" or _binding_has_unresolved_entry_leg(session, row)
        ]
        trigger_orders = _load_pending_trigger_orders(client, rows=rows)
        all_orders = [*orders, *trigger_orders]
        orders_by_order_id = {
            _first_string(order, "ordId", "orderId", "order_id", "id"): order
            for order in all_orders
            if _first_string(order, "ordId", "orderId", "order_id", "id")
        }
        orders_by_client_order_id = {
            _first_string(order, "clOrdId", "clientOrderId", "client_order_id"): order
            for order in all_orders
            if _first_string(order, "clOrdId", "clientOrderId", "client_order_id")
        }
        bound_pos_ids = {
            pos_id
            for binding in rows
            for pos_id in _split_ids(binding.pos_id)
        }
        order_history_cache: dict[str, list[dict[str, Any]]] = {}
        trade_fills_cache: dict[str, list[dict[str, Any]]] = {}
        trigger_history_cache: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            row.strategy_instance_id = row.strategy_instance_id or build_strategy_instance_id(
                venue=row.venue,
                chat_id=row.chat_id,
                message_id=row.message_id,
                symbol=row.symbol,
                side=row.side,
            )
            position = _lookup_position_by_any_id(positions_by_pos_id, row.pos_id)
            order = _lookup_order_by_any_id(orders_by_order_id, row.order_id)
            if order is None:
                order = _lookup_order_by_any_id(orders_by_client_order_id, row.client_order_id)
            recovered_position_from_payload = (
                _select_position_from_submitted_order_payload(
                    row,
                    active_positions=active_positions,
                    bound_pos_ids=bound_pos_ids,
                )
                if position is None
                else None
            )

            if position is not None and _has_nonzero_size(position):
                recovered_pos_ids = _select_additional_positions_from_order_evidence(
                    row,
                    client=client,
                    active_positions=active_positions,
                    bound_pos_ids=bound_pos_ids,
                    order_history_cache=order_history_cache,
                    trade_fills_cache=trade_fills_cache,
                    trigger_history_cache=trigger_history_cache,
                )
                if recovered_pos_ids:
                    row.pos_id = _join_unique_ids([*_split_ids(row.pos_id), *recovered_pos_ids])
                    bound_pos_ids.update(recovered_pos_ids)
                    row.last_exchange_status = "position_active_recovered_additional_pos_id"
                else:
                    row.last_exchange_status = "position_active"
                row.status = "active"
                if _attach_binding_to_lifecycle(session, row, now):
                    result.active += 1
                else:
                    result.stale += 1
            elif recovered_position_from_payload is not None:
                recovered_pos_id = _first_string(
                    recovered_position_from_payload, "posId", "pos_id", "id"
                )
                row.pos_id = recovered_pos_id
                row.status = "active"
                row.last_exchange_status = "position_active_recovered_from_submitted_order_payload"
                bound_pos_ids.add(str(recovered_pos_id))
                if _attach_binding_to_lifecycle(session, row, now):
                    result.active += 1
                else:
                    result.stale += 1
            elif order is not None and _is_open_order_state(order):
                row.status = "open"
                row.last_exchange_status = (
                    "trigger_order_open" if order in trigger_orders else "order_open"
                )
                result.open += 1
            else:
                recovered_pos_ids = _select_additional_positions_from_order_evidence(
                    row,
                    client=client,
                    active_positions=active_positions,
                    bound_pos_ids=bound_pos_ids,
                    order_history_cache=order_history_cache,
                    trade_fills_cache=trade_fills_cache,
                    trigger_history_cache=trigger_history_cache,
                )
                if recovered_pos_ids:
                    existing_pos_ids = _split_ids(row.pos_id)
                    row.pos_id = _join_unique_ids([*_split_ids(row.pos_id), *recovered_pos_ids])
                    row.status = "active"
                    row.last_exchange_status = (
                        "position_active_recovered_from_filled_order"
                        if not existing_pos_ids and len(recovered_pos_ids) == 1
                        else "position_active_recovered_additional_pos_id"
                    )
                    bound_pos_ids.update(recovered_pos_ids)
                    if _attach_binding_to_lifecycle(session, row, now):
                        result.active += 1
                    else:
                        result.stale += 1
                else:
                    recovered_position = _select_position_from_order_evidence(
                        row,
                        client=client,
                        active_positions=active_positions,
                        bound_pos_ids=bound_pos_ids,
                        order_history_cache=order_history_cache,
                        trade_fills_cache=trade_fills_cache,
                        trigger_history_cache=trigger_history_cache,
                    )
                    recovered_status = "position_active_recovered_from_filled_order"
                    if recovered_position is None:
                        recovered_position = recovered_position_from_payload
                        recovered_status = "position_active_recovered_from_submitted_order_payload"
                    if recovered_position is None:
                        recovered_position = _select_recovered_position_for_unbound_binding(
                            row,
                            rows=rows,
                            active_positions=active_positions,
                            bound_pos_ids=bound_pos_ids,
                        )
                        recovered_status = "position_active_recovered_without_pos_id"
                    if recovered_position is not None:
                        recovered_pos_id = _first_string(recovered_position, "posId", "pos_id", "id")
                        row.pos_id = recovered_pos_id
                        row.status = "active"
                        row.last_exchange_status = recovered_status
                        bound_pos_ids.add(str(recovered_pos_id))
                        if _attach_binding_to_lifecycle(session, row, now):
                            result.active += 1
                        else:
                            result.stale += 1
                    else:
                        row.status = "stale"
                        row.last_exchange_status = "not_found_on_exchange"
                        result.stale += 1
            row.recovered_at = now
            row.updated_at = now
            _refresh_order_legs_from_binding_row(
                session,
                row,
                active_positions=active_positions,
                order_leg_position_ids=_recover_order_leg_position_ids(
                    row,
                    client=client,
                    active_positions=active_positions,
                    bound_pos_ids=bound_pos_ids,
                    order_history_cache=order_history_cache,
                    trade_fills_cache=trade_fills_cache,
                    trigger_history_cache=trigger_history_cache,
                ),
            )
            result.updated += 1
        session.commit()
    return result


def _load_pending_trigger_orders(
    client: DeepcoinReadOnlyClient,
    *,
    rows: list[ExecutionBinding],
) -> list[dict[str, Any]]:
    method = getattr(client, "list_trigger_orders_pending", None)
    if method is None:
        return []
    instruments = {
        f"{str(row.symbol or '').upper()}-USDT-SWAP"
        for row in rows
        if str(row.symbol or "").strip()
    }
    pending: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for instrument_id in sorted(instruments):
        try:
            rows_for_instrument = method(inst_id=instrument_id)
        except TypeError:
            rows_for_instrument = method()
        except Exception:
            rows_for_instrument = []
        if not isinstance(rows_for_instrument, list):
            continue
        for order in rows_for_instrument:
            if not isinstance(order, dict):
                continue
            order_id = _first_string(order, "ordId", "orderId", "order_id", "id") or ""
            client_order_id = (
                _first_string(order, "clOrdId", "clientOrderId", "client_order_id") or ""
            )
            identity = (order_id, client_order_id)
            if identity in seen:
                continue
            seen.add(identity)
            pending.append(order)
    return pending


def _refresh_order_legs_from_binding_row(
    session,
    row: ExecutionBinding,
    *,
    active_positions: list[dict[str, Any]] | None = None,
    order_leg_position_ids: dict[str, str] | None = None,
) -> None:
    pos_ids = _split_ids(row.pos_id)
    if not pos_ids:
        return
    order_ids = set(_split_ids(row.order_id))
    client_order_ids = set(_split_ids(row.client_order_id))
    if not order_ids and not client_order_ids:
        return
    legs = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.execution_binding_id == row.id)
        .filter(ExecutionOrderLeg.purpose == "entry")
        .all()
    )
    recovered_by_order_id = order_leg_position_ids or {}
    for leg in legs:
        recovered_pos_id = recovered_by_order_id.get(str(leg.order_id or ""))
        if recovered_pos_id is None:
            recovered_pos_id = recovered_by_order_id.get(str(leg.client_order_id or ""))
        if recovered_pos_id is None:
            continue
        leg.pos_id = recovered_pos_id
        leg.status = "active" if row.status == "active" else leg.status
        leg.updated_at = row.updated_at
    if len(pos_ids) > 1:
        _refresh_multi_position_order_legs(
            legs,
            row=row,
            pos_ids=pos_ids,
            active_positions=active_positions or [],
        )
        return
    if len(legs) != 1:
        return
    for leg in legs:
        leg_order_id = str(leg.order_id or "")
        leg_client_order_id = str(leg.client_order_id or "")
        if leg_order_id not in order_ids and leg_client_order_id not in client_order_ids:
            continue
        if leg.pos_id and leg.pos_id != pos_ids[0]:
            continue
        leg.pos_id = pos_ids[0]
        leg.status = "active" if row.status == "active" else leg.status
        leg.updated_at = row.updated_at


def _refresh_multi_position_order_legs(
    legs: list[ExecutionOrderLeg],
    *,
    row: ExecutionBinding,
    pos_ids: list[str],
    active_positions: list[dict[str, Any]],
) -> None:
    positions_by_pos_id = {
        pos_id: position
        for position in active_positions
        if (pos_id := _first_string(position, "posId", "pos_id", "id")) in pos_ids
    }
    if not positions_by_pos_id:
        return
    submitted_orders_by_leg_index = {
        int(order.get("leg_index") or index): order
        for index, order in enumerate(_submitted_orders_from_binding_payload(row), start=1)
    }
    used_pos_ids: set[str] = set()
    for leg in sorted(legs, key=lambda item: int(item.leg_index or 0)):
        submitted_order = submitted_orders_by_leg_index.get(int(leg.leg_index or 0))
        if not submitted_order:
            continue
        matches = [
            pos_id
            for pos_id, position in positions_by_pos_id.items()
            if pos_id not in used_pos_ids
            and _position_matches_submitted_order_payload(position, submitted_order)
        ]
        if len(matches) != 1:
            continue
        leg.pos_id = matches[0]
        leg.status = "active" if row.status == "active" else leg.status
        leg.updated_at = row.updated_at
        used_pos_ids.add(matches[0])


def _cancel_missing_entry_lifecycle(session, row: ExecutionBinding, cancelled_at: datetime) -> None:
    """Archive a bound entry order that disappeared before any position was known."""

    from telegram_kol_research.models import StrategyLifecycle, TradeIdea

    lifecycle = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == row.chat_id)
        .filter(StrategyLifecycle.message_id == row.message_id)
        .filter(StrategyLifecycle.symbol == row.symbol)
        .filter(StrategyLifecycle.side == row.side)
        .filter(StrategyLifecycle.lifecycle_status.in_(["pending_entry", "entered"]))
        .order_by(StrategyLifecycle.id.desc())
        .first()
    )
    if lifecycle is None:
        return

    lifecycle.lifecycle_status = "exited"
    lifecycle.exit_reason = "cancelled"
    lifecycle.exited_at = cancelled_at
    lifecycle.updated_at = cancelled_at
    if lifecycle.trade_idea_id is not None:
        trade_idea = session.get(TradeIdea, lifecycle.trade_idea_id)
        if trade_idea is not None and trade_idea.status == "open":
            trade_idea.status = "closed"
            trade_idea.closed_at = cancelled_at


def sync_manual_closed_deepcoin_positions(
    session_factory: sessionmaker,
    *,
    client: DeepcoinReadOnlyClient,
    synced_at: datetime | None = None,
) -> ManualCloseSyncResult:
    """Mark bound active positions as manual-closed when they vanish on Deepcoin."""

    from telegram_kol_research.models import StrategyLifecycle, TradeIdea

    now = synced_at or datetime.now(UTC)
    positions = client.list_positions()
    active_pos_ids = {
        _first_string(position, "posId", "pos_id", "id")
        for position in positions
        if _first_string(position, "posId", "pos_id", "id") and _has_nonzero_size(position)
    }
    result = ManualCloseSyncResult()
    with session_factory() as session:
        rows = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.status.in_(["open", "active", "stale"]))
            .order_by(ExecutionBinding.id.asc())
            .all()
        )
        for row in rows:
            pos_ids = _split_ids(row.pos_id)
            if not pos_ids:
                result.skipped_without_pos_id += 1
                continue
            result.checked += 1
            if any(pos_id in active_pos_ids for pos_id in pos_ids):
                continue
            if _binding_has_unresolved_entry_leg(session, row):
                row.status = "open"
                row.last_exchange_status = "entry_legs_pending_after_position_closed"
                row.updated_at = now
                continue

            row.status = "closed"
            row.last_exchange_status = "manual_closed_or_not_found_on_exchange"
            row.updated_at = now
            result.manually_closed += 1

            lifecycle = (
                session.query(StrategyLifecycle)
                .filter(StrategyLifecycle.chat_id == row.chat_id)
                .filter(StrategyLifecycle.message_id == row.message_id)
                .filter(StrategyLifecycle.symbol == row.symbol)
                .filter(StrategyLifecycle.side == row.side)
                .order_by(StrategyLifecycle.id.desc())
                .first()
            )
            if lifecycle is not None and lifecycle.lifecycle_status == "entered":
                lifecycle.lifecycle_status = "exited"
                lifecycle.exit_reason = "manual"
                lifecycle.exited_at = now
                lifecycle.updated_at = now
                if lifecycle.trade_idea_id is not None:
                    trade_idea = session.get(TradeIdea, lifecycle.trade_idea_id)
                    if trade_idea is not None and trade_idea.status == "open":
                        trade_idea.status = "closed"
                        trade_idea.closed_at = now
        session.commit()
    return result


def bind_deepcoin_position_to_lifecycle(
    session_factory: sessionmaker,
    *,
    lifecycle_id: int,
    pos_id: str,
    position_payload: dict[str, Any] | None = None,
    bound_at: datetime | None = None,
) -> int:
    """Attach an existing live Deepcoin position to a local KOL lifecycle."""

    from telegram_kol_research.models import StrategyLifecycle

    now = bound_at or datetime.now(UTC)
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        if lifecycle is None:
            raise LookupError("strategy lifecycle not found")
        if lifecycle.lifecycle_status not in {"entered", "pending_entry"}:
            raise ValueError("only active or pending strategies can be bound")
        if lifecycle.execution_binding_id is not None:
            binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
            if (
                binding is not None
                and binding.venue == "deepcoin"
                and binding.chat_id == lifecycle.chat_id
                and binding.message_id == lifecycle.message_id
                and binding.symbol == lifecycle.symbol
                and binding.side == lifecycle.side
                and binding.status in {"open", "active"}
            ):
                binding.pos_id = _join_unique_ids([*_split_ids(binding.pos_id), pos_id])
                binding.status = "active"
                binding.last_exchange_status = "manual_bound_live_position"
                binding.updated_at = now
                if lifecycle.lifecycle_status == "pending_entry":
                    lifecycle.lifecycle_status = "entered"
                    lifecycle.entered_at = now
                lifecycle.updated_at = now
                binding_id = int(binding.id)
                session.commit()
                return binding_id
        record = ExecutionBindingRecord(
            kol_id=f"group:{lifecycle.chat_id}",
            chat_id=lifecycle.chat_id,
            message_id=lifecycle.message_id,
            symbol=lifecycle.symbol,
            side=lifecycle.side,
            venue="deepcoin",
            order_id=None,
            client_order_id=None,
            pos_id=pos_id,
            payload={"manual_bind_position": position_payload or {}},
            last_exchange_status="manual_bound_live_position",
            status="active",
        )
    binding_id = upsert_execution_binding(session_factory, record)
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        if lifecycle is not None:
            lifecycle.execution_binding_id = binding_id
            if lifecycle.lifecycle_status == "pending_entry":
                lifecycle.lifecycle_status = "entered"
                lifecycle.entered_at = now
            lifecycle.updated_at = now
            session.commit()
    return binding_id


def upsert_execution_order_leg(
    session_factory: sessionmaker,
    record: ExecutionOrderLegRecord,
) -> int:
    """Create or update one per-leg Deepcoin id mapping."""

    request_json = _compact_json(record.request)
    response_json = _compact_json(record.response)
    purpose = str(record.purpose or "entry").lower()
    order_kind = str(record.order_kind or "unknown").lower()
    now = datetime.now(UTC)

    with session_factory() as session:
        row = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == int(record.execution_binding_id))
            .filter(ExecutionOrderLeg.purpose == purpose)
            .filter(ExecutionOrderLeg.leg_index == int(record.leg_index))
            .one_or_none()
        )
        if row is None:
            row = ExecutionOrderLeg(
                execution_binding_id=int(record.execution_binding_id),
                purpose=purpose,
                leg_index=int(record.leg_index),
            )
            session.add(row)
            session.flush()

        row.strategy_instance_id = record.strategy_instance_id
        row.order_kind = order_kind
        row.order_id = record.order_id
        row.client_order_id = record.client_order_id
        row.pos_id = record.pos_id
        row.status = str(record.status or "submitted").lower()
        if request_json is not None:
            row.request_json = request_json
        if response_json is not None:
            row.response_json = response_json
        row.updated_at = now
        leg_id = int(row.id)
        session.commit()
    return leg_id


def list_execution_order_legs(
    session_factory: sessionmaker,
    *,
    execution_binding_id: int,
) -> list[ExecutionOrderLegSnapshot]:
    with session_factory() as session:
        rows = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == int(execution_binding_id))
            .order_by(ExecutionOrderLeg.purpose.asc(), ExecutionOrderLeg.leg_index.asc())
            .all()
        )
        return [
            ExecutionOrderLegSnapshot(
                id=int(row.id),
                execution_binding_id=int(row.execution_binding_id),
                strategy_instance_id=row.strategy_instance_id,
                leg_index=int(row.leg_index),
                purpose=row.purpose,
                order_kind=row.order_kind,
                order_id=row.order_id,
                client_order_id=row.client_order_id,
                pos_id=row.pos_id,
                status=row.status,
            )
            for row in rows
        ]


def repair_execution_order_legs_from_binding_payloads(
    session_factory: sessionmaker,
) -> int:
    """Backfill per-leg rows from legacy binding submitted_orders payloads."""

    repaired = 0
    with session_factory() as session:
        rows = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.venue == "deepcoin")
            .filter(ExecutionBinding.payload_json.isnot(None))
            .order_by(ExecutionBinding.id.asc())
            .all()
        )
        snapshots: list[tuple[int, str | None, list[dict[str, Any]]]] = []
        for row in rows:
            submitted_orders = _submitted_orders_from_binding_payload(row)
            if submitted_orders:
                snapshots.append((int(row.id), row.strategy_instance_id, submitted_orders))

    for binding_id, strategy_instance_id, submitted_orders in snapshots:
        for index, order in enumerate(submitted_orders, start=1):
            leg_index = int(order.get("leg_index") or index)
            pos_id = _first_string(order, "pos_id", "posId", "position_id")
            status = "active" if pos_id else "open"
            upsert_execution_order_leg(
                session_factory,
                ExecutionOrderLegRecord(
                    execution_binding_id=binding_id,
                    strategy_instance_id=strategy_instance_id,
                    leg_index=leg_index,
                    purpose="entry",
                    order_kind=str(order.get("execution_type") or order.get("order_kind") or "unknown"),
                    order_id=_first_string(order, "order_id", "ordId", "orderId"),
                    client_order_id=_first_string(order, "client_order_id", "clOrdId", "clientOrderId"),
                    pos_id=pos_id,
                    status=status,
                    request=order.get("request") if isinstance(order.get("request"), dict) else None,
                    response=order.get("response") if isinstance(order.get("response"), dict) else None,
                ),
            )
            repaired += 1
    return repaired


def build_deepcoin_account_state(
    session_factory: sessionmaker,
    *,
    client: DeepcoinReadOnlyClient,
) -> DeepcoinReadOnlyAccountState:
    """Build a read-only Deepcoin account-state provider from persisted bindings."""

    return DeepcoinReadOnlyAccountState(
        client=client,
        bindings=load_deepcoin_order_bindings(session_factory),
    )


def list_active_positions(
    session_factory: sessionmaker,
    *,
    chat_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    """List entered-but-not-exited strategies with detailed signal info.

    Combines execution bindings and open trade ideas.  Supports optional
    chat_id filter for per-group display.  Skips strategies that have been
    closed via trade updates or close signals.
    """

    from telegram_kol_research.models import (
        TradeIdea, SignalCandidate, RawMessage, TradeUpdate,
    )
    from sqlalchemy import and_

    results: list[dict[str, object]] = []

    with session_factory() as session:
        # ------------------------------------------------------------------
        # 1. Active execution bindings (exchange-tracked positions)
        # ------------------------------------------------------------------
        bindings_q = session.query(ExecutionBinding).filter(
            ExecutionBinding.status.in_(["open", "active"])
        )
        if chat_id is not None:
            bindings_q = bindings_q.filter(ExecutionBinding.chat_id == chat_id)
        bindings = bindings_q.order_by(ExecutionBinding.created_at.desc()).limit(limit).all()

        already_covered: set[tuple[int, str, str]] = set()
        for row in bindings:
            key = (row.chat_id, row.symbol.upper(), row.side.lower())
            results.append({
                "source": "execution",
                "id": row.id,
                "kol_id": row.kol_id,
                "chat_id": row.chat_id,
                "message_id": row.message_id,
                "symbol": row.symbol,
                "side": row.side,
                "venue": row.venue,
                "order_id": row.order_id,
                "client_order_id": row.client_order_id,
                "pos_id": row.pos_id,
                "strategy_instance_id": row.strategy_instance_id,
                "margin_mode": row.margin_mode,
                "position_mode": row.position_mode,
                "status": row.status,
                "last_exchange_status": row.last_exchange_status,
                "entry_text": None,
                "stop_loss_text": None,
                "take_profit_text": None,
                "confidence": None,
                "posted_at": row.created_at,
                "opened_at": row.created_at,
                "closed": False,
            })
            already_covered.add(key)

        # ------------------------------------------------------------------
        # 2. Open trade ideas with full signal details
        # ------------------------------------------------------------------
        open_trades_q = (
            session.query(TradeIdea, SignalCandidate, RawMessage)
            .join(SignalCandidate, TradeIdea.primary_signal_candidate_id == SignalCandidate.id)
            .join(RawMessage, SignalCandidate.raw_message_id == RawMessage.id)
            .filter(TradeIdea.status == "open")
        )
        if chat_id is not None:
            open_trades_q = open_trades_q.filter(RawMessage.chat_id == chat_id)
        open_trades = (
            open_trades_q
            .order_by(TradeIdea.opened_at.desc().nullslast(), TradeIdea.id.desc())
            .limit(limit)
            .all()
        )

        # Collect all trade_idea ids for exit-check
        trade_ids = [
            trade.id for trade, _, _ in open_trades
        ]

        # ------------------------------------------------------------------
        # 3. Pre-load close signals and trade updates for exit detection
        # ------------------------------------------------------------------
        closed_trade_ids: set[int] = set()
        if trade_ids:
            # Close trade updates
            close_updates = (
                session.query(TradeUpdate.trade_idea_id)
                .filter(
                    TradeUpdate.trade_idea_id.in_(trade_ids),
                    TradeUpdate.update_type.in_([
                        "close", "close_signal", "stop_loss_hit",
                        "take_profit_hit", "manual_close", "closed",
                    ]),
                )
                .all()
            )
            closed_trade_ids.update(row[0] for row in close_updates)

            # Close signal candidates for same chat+symbol+side pairs
            for trade, candidate, raw_msg in open_trades:
                if trade.id in closed_trade_ids:
                    continue
                close_exists = session.query(SignalCandidate).filter(
                    SignalCandidate.raw_message_id.in_(
                        session.query(RawMessage.id).filter(
                            RawMessage.chat_id == raw_msg.chat_id,
                            RawMessage.posted_at > raw_msg.posted_at,
                        )
                    ),
                    SignalCandidate.symbol == candidate.symbol,
                    SignalCandidate.side == candidate.side,
                    SignalCandidate.event_type.in_(["close_signal", "stop_loss_update"]),
                ).first()
                if close_exists is not None:
                    closed_trade_ids.add(trade.id)

        # ------------------------------------------------------------------
        # 4. Build result rows for open (non-exited) trade ideas
        # ------------------------------------------------------------------
        for trade, candidate, raw_msg in open_trades:
            if trade.id in closed_trade_ids:
                continue
            key = (raw_msg.chat_id, (trade.symbol or "").upper(), (trade.side or "").lower())
            if key in already_covered:
                continue
            results.append({
                "source": "trade_idea",
                "id": trade.id,
                "kol_id": (candidate.source_id or raw_msg.sender_name or "unknown"),
                "chat_id": raw_msg.chat_id,
                "message_id": raw_msg.message_id,
                "symbol": trade.symbol or "?",
                "side": trade.side or "?",
                "venue": "",
                "order_id": None,
                "pos_id": None,
                "status": trade.status,
                "entry_text": candidate.entry_text,
                "stop_loss_text": candidate.stop_loss_text,
                "take_profit_text": candidate.take_profit_text,
                "confidence": trade.confidence,
                "posted_at": raw_msg.posted_at,
                "opened_at": trade.opened_at,
                "closed": False,
            })
            already_covered.add(key)

    return results[:limit]


def _normalize_margin_mode(value: str | None) -> str:
    text = str(value or "cross").lower()
    if text in {"cross", "crossed", "full", "全仓"}:
        return "cross"
    if text in {"isolated", "fixed", "逐仓"}:
        return "isolated"
    return "cross"


def _normalize_position_mode(value: str | None) -> str:
    text = str(value or "split").lower()
    if text in {"split", "hedge", "long_short", "分仓"}:
        return "split"
    if text in {"net", "merged", "one_way", "合仓"}:
        return "net"
    return "split"


def _compact_json(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _split_ids(value: str | None) -> list[str]:
    return [
        item.strip()
        for item in str(value or "").split(",")
        if item and item.strip()
    ]


def _join_unique_ids(values: list[str]) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return ",".join(result)


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


def _lookup_position_by_any_id(
    positions_by_pos_id: dict[str, dict[str, Any]],
    raw_ids: str | None,
) -> dict[str, Any] | None:
    for item_id in _split_ids(raw_ids):
        position = positions_by_pos_id.get(item_id)
        if position is not None:
            return position
    return None


def _lookup_order_by_any_id(
    orders_by_id: dict[str, dict[str, Any]],
    raw_ids: str | None,
) -> dict[str, Any] | None:
    for item_id in _split_ids(raw_ids):
        order = orders_by_id.get(item_id)
        if order is not None:
            return order
    return None


def _select_position_from_order_evidence(
    row: ExecutionBinding,
    *,
    client: DeepcoinReadOnlyClient,
    active_positions: list[dict[str, Any]],
    bound_pos_ids: set[str],
    order_history_cache: dict[str, list[dict[str, Any]]],
    trade_fills_cache: dict[str, list[dict[str, Any]]],
    trigger_history_cache: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    if _split_ids(row.pos_id):
        return None
    order_ids = set(_split_ids(row.order_id)) | set(_split_ids(row.client_order_id))
    if not order_ids:
        return None

    target_symbol = str(row.symbol or "").upper()
    target_side = str(row.side or "").lower()
    instrument_id = f"{target_symbol}-USDT-SWAP"
    candidates = [
        position
        for position in active_positions
        if _first_string(position, "posId", "pos_id", "id") not in bound_pos_ids
        and _symbol_from_inst_id(position.get("instId")) == target_symbol
        and _normalize_position_side(
            str(position.get("posSide") or position.get("side") or "")
        )
        == target_side
    ]
    if not candidates:
        return None

    evidence = _load_order_evidence(
        client,
        instrument_id=instrument_id,
        order_ids=order_ids,
        order_history_cache=order_history_cache,
        trade_fills_cache=trade_fills_cache,
        trigger_history_cache=trigger_history_cache,
    )
    if not evidence:
        return None

    matches: list[dict[str, Any]] = []
    for position in candidates:
        if any(_position_matches_order_evidence(position, item) for item in evidence):
            matches.append(position)
    if len(matches) == 1:
        return matches[0]
    return None


def _select_additional_positions_from_order_evidence(
    row: ExecutionBinding,
    *,
    client: DeepcoinReadOnlyClient,
    active_positions: list[dict[str, Any]],
    bound_pos_ids: set[str],
    order_history_cache: dict[str, list[dict[str, Any]]],
    trade_fills_cache: dict[str, list[dict[str, Any]]],
    trigger_history_cache: dict[str, list[dict[str, Any]]],
) -> list[str]:
    existing_pos_ids = set(_split_ids(row.pos_id))
    order_ids = set(_split_ids(row.order_id)) | set(_split_ids(row.client_order_id))
    if not order_ids:
        return []

    recovered_by_order_id = _recover_order_leg_position_ids(
        row,
        client=client,
        active_positions=active_positions,
        bound_pos_ids=bound_pos_ids,
        order_history_cache=order_history_cache,
        trade_fills_cache=trade_fills_cache,
        trigger_history_cache=trigger_history_cache,
    )
    recovered_pos_ids = [
        pos_id
        for pos_id in recovered_by_order_id.values()
        if pos_id not in existing_pos_ids
    ]
    if recovered_pos_ids:
        return recovered_pos_ids

    target_symbol = str(row.symbol or "").upper()
    target_side = str(row.side or "").lower()
    candidates = [
        position
        for position in active_positions
        if _first_string(position, "posId", "pos_id", "id") not in existing_pos_ids
        and _first_string(position, "posId", "pos_id", "id") not in bound_pos_ids
        and _symbol_from_inst_id(position.get("instId")) == target_symbol
        and _normalize_position_side(
            str(position.get("posSide") or position.get("side") or "")
        )
        == target_side
    ]
    evidence = _load_order_evidence(
        client,
        instrument_id=f"{target_symbol}-USDT-SWAP",
        order_ids=order_ids,
        order_history_cache=order_history_cache,
        trade_fills_cache=trade_fills_cache,
        trigger_history_cache=trigger_history_cache,
    )
    return [
        pos_id
        for position in candidates
        if (pos_id := _first_string(position, "posId", "pos_id", "id"))
        and any(_position_matches_order_evidence(position, item) for item in evidence)
    ]


def _recover_order_leg_position_ids(
    row: ExecutionBinding,
    *,
    client: DeepcoinReadOnlyClient,
    active_positions: list[dict[str, Any]],
    bound_pos_ids: set[str],
    order_history_cache: dict[str, list[dict[str, Any]]],
    trade_fills_cache: dict[str, list[dict[str, Any]]],
    trigger_history_cache: dict[str, list[dict[str, Any]]],
) -> dict[str, str]:
    order_ids = set(_split_ids(row.order_id)) | set(_split_ids(row.client_order_id))
    if not order_ids:
        return {}
    target_symbol = str(row.symbol or "").upper()
    target_side = str(row.side or "").lower()
    owned_pos_ids = set(_split_ids(row.pos_id))
    candidates = [
        position
        for position in active_positions
        if (
            (_first_string(position, "posId", "pos_id", "id") not in bound_pos_ids)
            or (_first_string(position, "posId", "pos_id", "id") in owned_pos_ids)
        )
        and _symbol_from_inst_id(position.get("instId")) == target_symbol
        and _normalize_position_side(
            str(position.get("posSide") or position.get("side") or "")
        )
        == target_side
    ]
    if not candidates:
        return {}
    evidence = _load_order_evidence(
        client,
        instrument_id=f"{target_symbol}-USDT-SWAP",
        order_ids=order_ids,
        order_history_cache=order_history_cache,
        trade_fills_cache=trade_fills_cache,
        trigger_history_cache=trigger_history_cache,
    )
    if not evidence:
        return {}
    recovered: dict[str, str] = {}
    used_pos_ids: set[str] = set()
    for item in evidence:
        evidence_order_id = _first_string(item, "ordId", "orderId", "order_id", "id")
        evidence_client_order_id = _first_string(
            item, "clOrdId", "clientOrderId", "client_order_id"
        )
        evidence_key = evidence_order_id or evidence_client_order_id
        if not evidence_key:
            continue
        scored_matches = [
            (_position_order_evidence_score(position, item), position)
            for position in candidates
            if (pos_id := _first_string(position, "posId", "pos_id", "id"))
            and pos_id not in used_pos_ids
        ]
        scored_matches = [item for item in scored_matches if item[0] >= 2]
        if not scored_matches:
            continue
        best_score = max(score for score, _ in scored_matches)
        matches = [position for score, position in scored_matches if score == best_score]
        if len(matches) != 1:
            continue
        pos_id = _first_string(matches[0], "posId", "pos_id", "id")
        if pos_id is None:
            continue
        recovered[evidence_key] = pos_id
        used_pos_ids.add(pos_id)
    return recovered


def _load_order_evidence(
    client: DeepcoinReadOnlyClient,
    *,
    instrument_id: str,
    order_ids: set[str],
    order_history_cache: dict[str, list[dict[str, Any]]],
    trade_fills_cache: dict[str, list[dict[str, Any]]],
    trigger_history_cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    history_rows = _cached_client_rows(
        client,
        method_name="list_order_history",
        instrument_id=instrument_id,
        cache=order_history_cache,
    )
    fill_rows = _cached_client_rows(
        client,
        method_name="list_trade_fills",
        instrument_id=instrument_id,
        cache=trade_fills_cache,
    )
    trigger_history_rows = _cached_client_rows(
        client,
        method_name="list_trigger_order_history",
        instrument_id=instrument_id,
        cache=trigger_history_cache,
    )
    for row in [*history_rows, *fill_rows, *trigger_history_rows]:
        row_order_ids = {
            value
            for value in (
                _first_string(row, "ordId", "orderId", "order_id", "id"),
                _first_string(row, "clOrdId", "clientOrderId", "client_order_id"),
            )
            if value
        }
        if not row_order_ids.intersection(order_ids):
            continue
        if _is_history_fill_evidence(row):
            evidence.append(row)
    return evidence


def _cached_client_rows(
    client: DeepcoinReadOnlyClient,
    *,
    method_name: str,
    instrument_id: str,
    cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if instrument_id in cache:
        return cache[instrument_id]
    method = getattr(client, method_name, None)
    if method is None:
        cache[instrument_id] = []
        return []
    try:
        rows = method(inst_id=instrument_id)
    except TypeError:
        rows = method()
    except Exception:
        rows = []
    if not isinstance(rows, list):
        rows = []
    cache[instrument_id] = [row for row in rows if isinstance(row, dict)]
    return cache[instrument_id]


def _is_history_fill_evidence(row: dict[str, Any]) -> bool:
    state = str(row.get("state") or row.get("status") or "").lower()
    if state and state not in {"filled", "partially_filled", "partial_filled", "done"}:
        return False
    return any(
        _to_float(row.get(key)) is not None
        for key in ("fillPx", "avgPx", "fillSz", "sz")
    )


def _position_matches_order_evidence(position: dict[str, Any], evidence: dict[str, Any]) -> bool:
    matched_fields = 0
    evidence_size = _to_float(
        evidence.get("fillSz")
        or evidence.get("accFillSz")
        or evidence.get("sz")
        or evidence.get("size")
    )
    if evidence_size is not None:
        position_size = _to_float(position.get("pos") or position.get("size"))
        if position_size is None or not _close_number(position_size, evidence_size, rel_tol=0.001):
            return False
        matched_fields += 1

    evidence_price = _to_float(
        evidence.get("fillPx")
        or evidence.get("avgPx")
        or evidence.get("avgPrice")
        or evidence.get("px")
        or evidence.get("price")
    )
    if evidence_price is not None:
        position_price = _to_float(position.get("avgPx") or position.get("avgPrice") or position.get("openAvgPx"))
        if position_price is None or not _close_number(position_price, evidence_price, rel_tol=0.001):
            return False
        matched_fields += 1

    evidence_time = _to_int(
        evidence.get("fillTime")
        or evidence.get("uTime")
        or evidence.get("ts")
        or evidence.get("cTime")
        or evidence.get("triggerTime")
    )
    if evidence_time is not None:
        position_time = _to_int(position.get("uTime") or position.get("cTime"))
        if position_time is not None and abs(position_time - evidence_time) <= 120_000:
            matched_fields += 1

    return matched_fields >= 2


def _position_order_evidence_score(position: dict[str, Any], evidence: dict[str, Any]) -> int:
    score = 0
    evidence_size = _to_float(
        evidence.get("fillSz")
        or evidence.get("accFillSz")
        or evidence.get("sz")
        or evidence.get("size")
    )
    if evidence_size is not None:
        position_size = _to_float(position.get("pos") or position.get("size"))
        if position_size is None or not _close_number(position_size, evidence_size, rel_tol=0.001):
            return -1
        score += 2

    evidence_time = _to_int(
        evidence.get("fillTime")
        or evidence.get("uTime")
        or evidence.get("ts")
        or evidence.get("cTime")
        or evidence.get("triggerTime")
    )
    if evidence_time is not None:
        position_time = _to_int(position.get("uTime") or position.get("cTime"))
        if position_time is not None and abs(position_time - evidence_time) <= 120_000:
            score += 1

    evidence_price = _to_float(
        evidence.get("fillPx")
        or evidence.get("avgPx")
        or evidence.get("avgPrice")
        or evidence.get("px")
        or evidence.get("price")
    )
    if evidence_price is not None:
        position_price = _to_float(
            position.get("avgPx") or position.get("avgPrice") or position.get("openAvgPx")
        )
        if position_price is not None and _close_number(position_price, evidence_price, rel_tol=0.001):
            score += 1
    return score


def _select_position_from_submitted_order_payload(
    row: ExecutionBinding,
    *,
    active_positions: list[dict[str, Any]],
    bound_pos_ids: set[str],
) -> dict[str, Any] | None:
    if _split_ids(row.pos_id):
        return None
    submitted_orders = _submitted_orders_from_binding_payload(row)
    if not submitted_orders:
        return None

    target_symbol = str(row.symbol or "").upper()
    target_side = str(row.side or "").lower()
    candidates = [
        position
        for position in active_positions
        if _first_string(position, "posId", "pos_id", "id") not in bound_pos_ids
        and _symbol_from_inst_id(position.get("instId")) == target_symbol
        and _normalize_position_side(
            str(position.get("posSide") or position.get("side") or "")
        )
        == target_side
    ]
    if not candidates:
        return None

    matches: list[dict[str, Any]] = []
    for position in candidates:
        if any(
            _position_matches_submitted_order_payload(position, submitted_order)
            for submitted_order in submitted_orders
        ):
            matches.append(position)
    if len(matches) == 1:
        return matches[0]
    return None


def _submitted_orders_from_binding_payload(row: ExecutionBinding) -> list[dict[str, Any]]:
    if not row.payload_json:
        return []
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError):
        return []
    submitted_orders = payload.get("submitted_orders")
    if not isinstance(submitted_orders, list):
        return []
    return [item for item in submitted_orders if isinstance(item, dict)]


def _position_matches_submitted_order_payload(
    position: dict[str, Any],
    submitted_order: dict[str, Any],
) -> bool:
    request = submitted_order.get("request")
    if not isinstance(request, dict):
        return False

    order_symbol = _symbol_from_inst_id(request.get("instId"))
    position_symbol = _symbol_from_inst_id(position.get("instId"))
    if order_symbol and position_symbol and order_symbol != position_symbol:
        return False

    order_side = _normalize_position_side(
        str(request.get("posSide") or request.get("position_side") or "")
    )
    position_side = _normalize_position_side(
        str(position.get("posSide") or position.get("side") or "")
    )
    if order_side and position_side and order_side != position_side:
        return False

    matched_fields = 0
    order_size = _to_float(request.get("sz") or request.get("size") or request.get("quantity"))
    position_size = _to_float(position.get("pos") or position.get("size"))
    if order_size is not None:
        if position_size is None or not _close_number(position_size, order_size, rel_tol=0.001):
            return False
        matched_fields += 1

    order_price = _to_float(
        request.get("triggerPrice")
        or request.get("triggerPx")
        or request.get("price")
        or request.get("px")
    )
    position_price = _to_float(
        position.get("avgPx") or position.get("avgPrice") or position.get("openAvgPx")
    )
    if order_price is not None:
        if position_price is None or not _close_number(position_price, order_price, rel_tol=0.001):
            return False
        matched_fields += 1

    return matched_fields >= 2


def _select_recovered_position_for_unbound_binding(
    row: ExecutionBinding,
    *,
    rows: list[ExecutionBinding],
    active_positions: list[dict[str, Any]],
    bound_pos_ids: set[str],
) -> dict[str, Any] | None:
    if _split_ids(row.pos_id):
        return None
    order_ids = set(_split_ids(row.order_id)) | set(_split_ids(row.client_order_id))
    if not order_ids:
        return None
    target_symbol = str(row.symbol or "").upper()
    target_side = str(row.side or "").lower()
    candidates = [
        position
        for position in active_positions
        if _first_string(position, "posId", "pos_id", "id") not in bound_pos_ids
        and _symbol_from_inst_id(position.get("instId")) == target_symbol
        and _normalize_position_side(
            str(position.get("posSide") or position.get("side") or "")
        )
        == target_side
    ]
    if not candidates:
        return None

    competing_rows = [
        other
        for other in rows
        if other.id != row.id
        and not _split_ids(other.pos_id)
        and str(other.symbol or "").upper() == target_symbol
        and str(other.side or "").lower() == target_side
        and other.status in {"open", "active", "unknown"}
    ]
    if not competing_rows and len(candidates) == 1:
        return candidates[0]
    return None


def _attach_binding_to_lifecycle(session, row: ExecutionBinding, updated_at: datetime) -> bool:
    from telegram_kol_research.models import StrategyLifecycle

    lifecycle = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == row.chat_id)
        .filter(StrategyLifecycle.message_id == row.message_id)
        .filter(StrategyLifecycle.symbol == row.symbol)
        .filter(StrategyLifecycle.side == row.side)
        .order_by(StrategyLifecycle.id.desc())
        .first()
    )
    if lifecycle is None:
        return True
    if row.status != "active" and _is_stale_unentered_lifecycle(lifecycle, updated_at):
        lifecycle.lifecycle_status = "expired"
        lifecycle.exit_reason = "expired"
        lifecycle.exited_at = _pending_entry_expired_at(lifecycle)
        lifecycle.entered_at = None
        lifecycle.entry_price_actual = None
        lifecycle.execution_binding_id = None
        lifecycle.updated_at = updated_at
        row.status = "stale"
        row.last_exchange_status = "expired_pending_entry_not_attributed"
        return False
    lifecycle.execution_binding_id = row.id
    if _is_terminal_exited_lifecycle(lifecycle) and not (
        row.status == "active" and lifecycle.exit_reason == "manual"
    ):
        lifecycle.updated_at = updated_at
        return True
    if row.status == "active" and lifecycle.lifecycle_status != "entered":
        lifecycle.lifecycle_status = "entered"
        lifecycle.exit_reason = None
        lifecycle.exited_at = None
        if lifecycle.entered_at is None:
            lifecycle.entered_at = updated_at
    elif row.status == "open" and lifecycle.lifecycle_status in {
        "exited",
        "expired",
        "invalidated",
        "cancelled",
    }:
        lifecycle.lifecycle_status = "pending_entry"
        lifecycle.exit_reason = None
        lifecycle.exited_at = None
    elif lifecycle.lifecycle_status == "pending_entry":
        lifecycle.lifecycle_status = "entered"
        lifecycle.entered_at = updated_at
    _refresh_lifecycle_prices_from_binding_payload(lifecycle, row)
    lifecycle.updated_at = updated_at
    return True


def _binding_has_unresolved_entry_leg(session, row: ExecutionBinding) -> bool:
    legs = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.execution_binding_id == row.id)
        .filter(ExecutionOrderLeg.purpose == "entry")
        .all()
    )
    if legs:
        leg_pos_ids = {str(leg.pos_id) for leg in legs if leg.pos_id}
        return not leg_pos_ids or len(leg_pos_ids) < len(legs)
    return len(_split_ids(row.order_id)) > len(_split_ids(row.pos_id))


def _is_terminal_exited_lifecycle(lifecycle: Any) -> bool:
    if str(getattr(lifecycle, "lifecycle_status", None) or "") != "exited":
        return False
    exit_reason = str(getattr(lifecycle, "exit_reason", None) or "")
    return exit_reason in {"kol_signal", "manual"}


def _is_stale_unentered_lifecycle(lifecycle: Any, updated_at: datetime) -> bool:
    if lifecycle.signal_at is None:
        return False
    status = str(lifecycle.lifecycle_status or "")
    exit_reason = str(getattr(lifecycle, "exit_reason", None) or "")
    management_action = str(getattr(lifecycle, "management_action", None) or "")
    if status == "entered":
        return False
    if status == "expired" and management_action == "expiry_expired_keep_order":
        return False
    if status == "exited" and exit_reason not in {"expired", "cancelled", "invalidated"}:
        return False
    if status not in {"pending_entry", "expired", "invalidated", "exited"}:
        return False
    return _utc_naive(updated_at) > _pending_entry_expired_at(lifecycle)


def _pending_entry_expired_at(lifecycle: Any) -> datetime:
    signal_at = _utc_naive(lifecycle.signal_at)
    return signal_at + timedelta(hours=PENDING_ENTRY_RECOVERY_WINDOW_HOURS)


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _refresh_lifecycle_prices_from_binding_payload(
    lifecycle: Any,
    row: ExecutionBinding,
) -> None:
    try:
        payload = json.loads(row.payload_json or "{}")
    except (TypeError, ValueError):
        return
    draft = payload.get("draft") if isinstance(payload, dict) else None
    if not isinstance(draft, dict):
        return
    draft_stop_loss = _to_float(draft.get("stop_loss"))
    reference_values = [
        value
        for value in (
            lifecycle.entry_price_actual,
            lifecycle.entry_range_low,
            lifecycle.entry_range_high,
            draft_stop_loss,
        )
        if value is not None and value > 0
    ]
    current_stop_loss = _to_float(lifecycle.stop_loss)
    if draft_stop_loss is not None and (
        current_stop_loss is None
        or not _price_plausible_against_reference(current_stop_loss, reference_values)
    ):
        lifecycle.stop_loss = draft_stop_loss

    draft_take_profit = _take_profit_text_from_draft(draft)
    if draft_take_profit and not lifecycle.take_profit:
        lifecycle.take_profit = draft_take_profit


def _take_profit_text_from_draft(draft: dict[str, Any]) -> str | None:
    legs = draft.get("take_profit_legs")
    if not isinstance(legs, list):
        return None
    prices: list[str] = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        price = _to_float(leg.get("price"))
        if price is None or price <= 0:
            continue
        prices.append(f"{price:g}")
    return "/".join(prices) if prices else None


def _price_plausible_against_reference(
    value: float,
    reference_values: list[float],
) -> bool:
    if value <= 0 or not reference_values:
        return value > 0
    reference = max(reference_values)
    return reference * 0.2 <= value <= reference * 5


def _symbol_from_inst_id(value: Any) -> str:
    text = str(value or "").upper()
    if text.endswith("-USDT-SWAP"):
        return text[: -len("-USDT-SWAP")]
    return text.split("-")[0] if text else ""


def _normalize_position_side(value: str) -> str:
    side = value.lower()
    if side == "buy":
        return "long"
    if side == "sell":
        return "short"
    return side


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _close_number(left: float, right: float, *, rel_tol: float) -> bool:
    scale = max(abs(left), abs(right), 1.0)
    return abs(left - right) <= scale * rel_tol
