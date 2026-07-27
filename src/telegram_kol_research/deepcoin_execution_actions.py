"""Executable Deepcoin actions for KOL position-management signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import (
    DeepcoinDefiniteRejection,
    DeepcoinTradingClientProtocol,
)
from telegram_kol_research.deepcoin_normalization import (
    normalize_deepcoin_margin_mode as _deepcoin_margin_mode,
    normalize_deepcoin_position_mode as _deepcoin_position_mode,
    normalize_deepcoin_swap_instrument as _to_deepcoin_swap_instrument,
)
from telegram_kol_research.deepcoin_order_matching import (
    extract_pending_protection_orders,
)
from telegram_kol_research.execution_events import ExecutionEventRecord
from telegram_kol_research.execution_events import record_execution_event
from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    RawMessage,
    StrategyLifecycle,
)
from telegram_kol_research.position_authority_lock import (
    serialized_position_authority_mutation,
)
from telegram_kol_research.position_mutation_gateway import (
    cancel_exact_position_sltp,
    close_exact_position,
    exact_position_write_gate,
    submit_exact_position_sltp,
)
from telegram_kol_research.position_attribution import (
    PositionAttributionError,
    require_equivalent_live_position_economics,
    require_verified_position_ownership,
)
from telegram_kol_research.protection_attribution import (
    match_position_protection,
    snapshot_protection_rows,
)
from telegram_kol_research.protection_ledger import (
    list_verified_ledger_rows_for_positions,
    upsert_protection_ledger_row,
)
from telegram_kol_research.trade_signals import MANUAL_MANAGEMENT_SOURCE_TYPES
from telegram_kol_research.trade_signals import MANAGEMENT_TRADE_SIGNAL_ACTIONS
from telegram_kol_research.trade_signals import TradeSignalRecord
from telegram_kol_research.trade_signals import canonical_management_batch_id
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.strategy_management_batches import load_management_batch


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
    if (
        action in MANAGEMENT_TRADE_SIGNAL_ACTIONS
        and trade_signal.source_type not in MANUAL_MANAGEMENT_SOURCE_TYPES
    ):
        return close_position_market(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=deepcoin_client,
            executed_at=executed_at,
        )
    if action in {
        "close_position",
        "exit_position",
        "temporary_exit",
        "temporary_close",
        "partial_close_and_move_stop_to_entry",
    }:
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


@serialized_position_authority_mutation
def partial_close_and_move_stop_to_entry(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Reject the unsafe legacy composite path.

    A close submission is not evidence of a fill.  The batch reconciler must
    first confirm every close leg from exchange truth before a separate
    protection phase can be authorized.
    """

    raise DeepcoinExecutionActionError(
        "composite_management_requires_exchange_confirmed_batch_close"
    )


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

    if (
        trade_signal.action.lower() in {"adjust_position_tpsl", "adjust_stop_loss"}
        and trade_signal.source_type not in MANUAL_MANAGEMENT_SOURCE_TYPES
    ):
        raise DeepcoinExecutionActionError(
            "automated_position_tpsl_requires_management_batch"
        )

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
    with session_factory() as session:
        ledger_rows = list_verified_ledger_rows_for_positions(
            session, [pos_id] if pos_id else []
        )
    exact_order_position_ids = {
        str(row.order_id): str(row.pos_id)
        for row in ledger_rows
        if str(row.order_id or "").strip()
    }
    protection = match_position_protection(
        live_positions,
        pending,
        exact_order_position_ids=exact_order_position_ids,
        allow_heuristic_attribution=False,
    ).by_pos_id.get(pos_id or "")
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

    old_row_snapshots = snapshot_protection_rows(old_tpsl_rows)
    if old_row_snapshots:
        adjusted_row_snapshots = _adjust_protection_row_snapshots(
            old_row_snapshots,
            action=trade_signal.action,
            payload=trade_signal.payload,
        )
    else:
        adjusted_row_snapshots = _new_protection_row_snapshots(after)
    common_payload = _build_position_tpsl_payload(
        binding=binding, position=position, inst_id=inst_id, after={}
    )
    set_payloads = [
        _build_position_tpsl_row_payload(common_payload, row)
        for row in adjusted_row_snapshots
    ]
    if old_row_snapshots:
        pending_recheck = deepcoin_client.list_trigger_orders_pending(inst_id=inst_id)
        rechecked = match_position_protection(
            live_positions,
            pending_recheck,
            exact_order_position_ids=exact_order_position_ids,
            allow_heuristic_attribution=False,
        ).by_pos_id.get(pos_id or "")
        rechecked_pending_rows = [
            row
            for row in pending_recheck
            if _order_id_from_payload(row) in old_order_id_set
        ]
        if (
            rechecked is None
            or rechecked.status != "verified"
            or rechecked.order_ids != old_order_ids
            or snapshot_protection_rows(rechecked_pending_rows)
            != old_row_snapshots
        ):
            raise DeepcoinExecutionActionError(
                "pending_position_tpsl_changed_before_cancel"
            )

    cancel_responses: list[dict[str, Any]] = []
    for order_id in old_order_ids:
        cancel_payload = {
            "instType": "SWAP", "instId": inst_id, "ordId": str(order_id)
        }
        response = cancel_exact_position_sltp(
            session_factory=session_factory,
            deepcoin_client=deepcoin_client,
            pos_id=str(pos_id),
            order_id=str(order_id),
            instrument_id=inst_id,
            idempotency_key=(
                f"signal:{trade_signal.id}:cancel:{order_id}"
            ),
            live_execution_gate=lambda: exact_position_write_gate(
                session_factory, pos_id=str(pos_id)
            ),
            now_provider=lambda: now,
        )
        _mark_position_tpsl_ledger_cancelled(
            session_factory,
            order_id=str(order_id),
            seen_at=now,
        )
        cancel_responses.append({"order_id": str(order_id), "response": response})
    set_responses: list[dict[str, Any]] = []
    new_order_ids: list[str] = []
    try:
        for set_payload in set_payloads:
            set_response = submit_exact_position_sltp(
                session_factory=session_factory,
                deepcoin_client=deepcoin_client,
                pos_id=str(pos_id),
                payload=set_payload,
                idempotency_key=(
                    f"signal:{trade_signal.id}:set:{len(set_responses)}"
                ),
                live_execution_gate=lambda: exact_position_write_gate(
                    session_factory, pos_id=str(pos_id)
                ),
                now_provider=lambda: now,
                require_readback=True,
            )
            new_order_id = _extract_order_id(set_response)
            if not new_order_id:
                raise DeepcoinExecutionActionError(
                    "position_tpsl_replacement_missing_order_id"
                )
            set_responses.append(set_response)
            new_order_ids.append(new_order_id)
            _record_position_tpsl_ledger_rows(
                session_factory,
                binding=binding,
                position=position,
                inst_id=inst_id,
                rows=[adjusted_row_snapshots[len(new_order_ids) - 1]],
                order_ids=[new_order_id],
                evidence_source="tpsl_write_response",
                seen_at=now,
            )
    except Exception as replacement_error:
        if not isinstance(replacement_error, DeepcoinDefiniteRejection):
            raise DeepcoinExecutionActionError(
                f"position_tpsl_replacement_outcome_unknown:{replacement_error}"
            ) from replacement_error
        try:
            for new_order_id in new_order_ids:
                cancel_exact_position_sltp(
                    session_factory=session_factory,
                    deepcoin_client=deepcoin_client,
                    pos_id=str(pos_id),
                    order_id=new_order_id,
                    instrument_id=inst_id,
                    idempotency_key=(
                        f"signal:{trade_signal.id}:rollback_cancel:{new_order_id}"
                    ),
                    live_execution_gate=lambda: exact_position_write_gate(
                        session_factory, pos_id=str(pos_id)
                    ),
                    now_provider=lambda: now,
                )
                _mark_position_tpsl_ledger_cancelled(
                    session_factory,
                    order_id=new_order_id,
                    seen_at=now,
                )
            for restore_index, old_row in enumerate(old_row_snapshots):
                restore_payload = _build_position_tpsl_row_payload(
                    common_payload, old_row
                )
                restore_response = submit_exact_position_sltp(
                    session_factory=session_factory,
                    deepcoin_client=deepcoin_client,
                    pos_id=str(pos_id),
                    payload=restore_payload,
                    idempotency_key=(
                        f"signal:{trade_signal.id}:restore:{restore_index}"
                    ),
                    live_execution_gate=lambda: exact_position_write_gate(
                        session_factory, pos_id=str(pos_id)
                    ),
                    now_provider=lambda: now,
                    require_readback=True,
                )
                if not _extract_order_id(restore_response):
                    raise DeepcoinExecutionActionError(
                        "position_tpsl_restore_missing_order_id"
                    )
        except Exception as restore_error:
            raise DeepcoinExecutionActionError(
                f"position_tpsl_recovery_required:{restore_error}"
            ) from replacement_error
        raise DeepcoinExecutionActionError(
            f"position_tpsl_replacement_failed_restored:{replacement_error}"
        ) from replacement_error
    set_payload = set_payloads[-1]
    set_response = set_responses[-1]
    new_order_id = new_order_ids[-1]
    _record_position_tpsl_ledger_rows(
        session_factory,
        binding=binding,
        position=position,
        inst_id=inst_id,
        rows=adjusted_row_snapshots,
        order_ids=new_order_ids,
        evidence_source="tpsl_write_response",
        seen_at=now,
    )
    for cancelled in cancel_responses:
        cancelled_order_id = str(cancelled["order_id"])
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
                order_id=cancelled_order_id,
                pos_id=_first_string(position, "posId", "pos_id", "id"),
                reason=trade_signal.action,
                before=before,
                request={
                    "instType": "SWAP",
                    "instId": inst_id,
                    "ordId": cancelled_order_id,
                },
                response=cancelled["response"],
                created_at=now,
            ),
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
            action="adjust_position_tpsl" if require_existing else "set_position_tpsl",
            order_id=",".join(new_order_ids),
            related_order_id=",".join(old_order_ids) if old_order_ids else None,
            pos_id=_first_string(position, "posId", "pos_id", "id"),
            reason=trade_signal.action,
            before=before or None,
            after=after,
            request={"rows": set_payloads},
            response={"rows": set_responses},
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
        "requests": set_payloads,
        "responses": set_responses,
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
    """Delegate legacy close requests through a durable management batch."""

    payload = trade_signal.payload if isinstance(trade_signal.payload, dict) else {}
    normalized_batch_id = canonical_management_batch_id(payload)
    if normalized_batch_id is None:
        raise DeepcoinExecutionActionError("legacy_management_signal_requires_batch")
    settings = load_trading_settings(session_factory)
    if not settings.live_management_execution_enabled:
        raise DeepcoinExecutionActionError("management_live_execution_disabled")
    try:
        batch = load_management_batch(session_factory, normalized_batch_id)
    except LookupError as exc:
        raise DeepcoinExecutionActionError("management_signal_batch_not_found") from exc
    with session_factory() as session:
        source = session.get(RawMessage, batch.raw_message_id)
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        requested_binding_id = _canonical_positive_int(payload.get("binding_id"))
        identity_matches = (
            source is not None
            and lifecycle is not None
            and binding is not None
            and requested_binding_id is not None
            and trade_signal.source_type == "kol_management"
            and trade_signal.venue.lower() == "deepcoin"
            and trade_signal.chat_id == source.chat_id
            and trade_signal.message_id == source.message_id
            and trade_signal.strategy_instance_id == batch.strategy_instance_id
            and requested_binding_id == batch.execution_binding_id
            and lifecycle.execution_binding_id == batch.execution_binding_id
            and lifecycle.chat_id == source.chat_id
            and lifecycle.chat_id == binding.chat_id
            and lifecycle.message_id == binding.message_id
            and lifecycle.symbol.upper() == binding.symbol.upper()
            and lifecycle.symbol.upper() == trade_signal.symbol.upper()
            and lifecycle.side.lower() == binding.side.lower()
            and lifecycle.side.lower() == trade_signal.side.lower()
            and binding.chat_id == source.chat_id
            and binding.strategy_instance_id == batch.strategy_instance_id
            and trade_signal.kol_id == binding.kol_id
            and trade_signal.symbol.upper() == binding.symbol.upper()
            and trade_signal.side.lower() == binding.side.lower()
            and _management_action_matches_batch(
                signal_action=trade_signal.action,
                batch_intent=batch.intent,
                effective_action=batch.effective_action,
            )
        )
    if not identity_matches:
        raise DeepcoinExecutionActionError(
            "management_signal_batch_identity_mismatch"
        )
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    return execute_management_batch(
        session_factory,
        batch_id=normalized_batch_id,
        deepcoin_client=deepcoin_client,
        executed_at=executed_at,
    )


def _canonical_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit() and not value.startswith("0"):
        normalized = int(value)
        return normalized if normalized > 0 else None
    return None


def _management_action_matches_batch(
    *, signal_action: str, batch_intent: str, effective_action: str
) -> bool:
    action = signal_action.lower()
    batch_pair = (batch_intent.lower(), effective_action.lower())
    if action in {
        "close_position",
        "exit_position",
        "temporary_exit",
        "temporary_close",
    }:
        return batch_pair == ("full_exit", "full_exit")
    if action in {"adjust_stop_loss", "adjust_position_tpsl"}:
        return batch_pair in {
            ("adjust_stop_loss", "adjust_stop_loss"),
            ("move_stop_to_break_even", "move_stop_to_break_even"),
            ("move_stop_to_break_even", "break_even_by_market"),
        }
    if action == "partial_close_and_move_stop_to_entry":
        return batch_pair == ("partial_then_break_even", "partial_then_break_even")
    return False


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
        response = close_exact_position(
            session_factory=session_factory,
            deepcoin_client=deepcoin_client,
            pos_id=normalized_pos_id,
            instrument_id=inst_id,
            size=f"{close_size:g}",
            client_order_id=None,
            idempotency_key=(
                f"bound-close:{binding.id}:{normalized_pos_id}"
            ),
            live_execution_gate=lambda: exact_position_write_gate(
                session_factory, pos_id=normalized_pos_id
            ),
            now_provider=lambda: now,
        )
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
def cancel_revision_entry_leg(
    session_factory: sessionmaker,
    *,
    strategy_thread_id: int,
    execution_binding_id: int,
    execution_order_leg_id: int,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Cancel one exact pending revision leg and confirm absence by readback."""

    now = executed_at or datetime.now(UTC)
    with session_factory() as session:
        lifecycle = (
            session.query(StrategyLifecycle)
            .filter(
                StrategyLifecycle.strategy_thread_id == int(strategy_thread_id),
                StrategyLifecycle.execution_binding_id == int(execution_binding_id),
            )
            .one_or_none()
        )
        binding = session.get(ExecutionBinding, int(execution_binding_id))
        leg = session.get(ExecutionOrderLeg, int(execution_order_leg_id))
        if (
            lifecycle is None
            or binding is None
            or leg is None
            or int(leg.execution_binding_id) != int(binding.id)
            or leg.purpose != "entry"
            or leg.pos_id not in (None, "")
        ):
            raise DeepcoinExecutionActionError("revision_entry_leg_identity_mismatch")
        order_ids = {str(leg.order_id)} if leg.order_id else set()
        client_order_ids = (
            {str(leg.client_order_id)} if leg.client_order_id else set()
        )
        if not order_ids and not client_order_ids:
            raise DeepcoinExecutionActionError("missing_cancel_order_id")
        inst_id = _to_deepcoin_swap_instrument(binding.symbol)
        margin_position = _deepcoin_position_mode(binding.position_mode)
        event_identity = {
            "binding_id": int(binding.id),
            "strategy_instance_id": binding.strategy_instance_id,
            "kol_id": binding.kol_id,
            "chat_id": int(binding.chat_id),
            "message_id": int(binding.message_id),
            "symbol": binding.symbol,
            "side": binding.side,
        }

    trigger_rows = _select_orders_by_known_ids(
        deepcoin_client.list_trigger_orders_pending(inst_id=inst_id),
        order_ids=order_ids,
        client_order_ids=client_order_ids,
    )
    regular_rows = _select_orders_by_known_ids(
        deepcoin_client.list_open_orders(inst_id=inst_id),
        order_ids=order_ids,
        client_order_ids=client_order_ids,
    )
    if len(trigger_rows) + len(regular_rows) != 1:
        raise DeepcoinExecutionActionError(
            "revision_pending_entry_leg_not_uniquely_visible"
        )
    is_trigger = bool(trigger_rows)
    visible = (trigger_rows or regular_rows)[0]
    order_id = _order_id_from_payload(visible) or next(iter(order_ids), "")
    client_order_id = _first_string(
        visible,
        "clOrdId",
        "clientOrderId",
        "client_order_id",
    ) or next(iter(client_order_ids), "")
    payload: dict[str, Any] = {"instId": inst_id}
    if order_id:
        payload["ordId"] = order_id
    if client_order_id:
        payload["clOrdId"] = client_order_id
    if is_trigger:
        response = deepcoin_client.cancel_trigger_order(payload)
    else:
        payload["mrgPosition"] = margin_position
        response = deepcoin_client.cancel_order(payload)

    remaining = _select_orders_by_known_ids(
        deepcoin_client.list_trigger_orders_pending(inst_id=inst_id),
        order_ids=order_ids,
        client_order_ids=client_order_ids,
    ) + _select_orders_by_known_ids(
        deepcoin_client.list_open_orders(inst_id=inst_id),
        order_ids=order_ids,
        client_order_ids=client_order_ids,
    )
    history_rows = _select_orders_by_known_ids(
        deepcoin_client.list_order_history(inst_id=inst_id),
        order_ids=order_ids,
        client_order_ids=client_order_ids,
    ) + _select_orders_by_known_ids(
        deepcoin_client.list_trigger_order_history(inst_id=inst_id),
        order_ids=order_ids,
        client_order_ids=client_order_ids,
    )
    fill_rows = _select_orders_by_known_ids(
        deepcoin_client.list_trade_fills(inst_id=inst_id),
        order_ids=order_ids,
        client_order_ids=client_order_ids,
    )
    terminal_states = {
        str(_first_string(row, "state", "status", "orderStatus") or "").lower()
        for row in history_rows
    }
    filled = bool(fill_rows) or bool(
        terminal_states
        & {"filled", "partially_filled", "partially-filled", "partial_filled"}
    )
    cancelled = bool(
        terminal_states
        & {"cancelled", "canceled", "cancel", "expired", "rejected"}
    )
    status = (
        "confirmed_cancelled"
        if not remaining and cancelled and not filled
        else "submit_unknown"
    )
    reason = (
        "revision_order_filled_during_cancel"
        if filled
        else "revision_cancel_not_terminally_confirmed"
        if status != "confirmed_cancelled"
        else None
    )
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            execution_binding_id=event_identity["binding_id"],
            strategy_instance_id=event_identity["strategy_instance_id"],
            kol_id=event_identity["kol_id"],
            chat_id=event_identity["chat_id"],
            message_id=event_identity["message_id"],
            source_message_id=event_identity["message_id"],
            symbol=event_identity["symbol"],
            side=event_identity["side"],
            action="cancel_revision_entry_leg",
            status=status,
            order_id=order_id or None,
            client_order_id=client_order_id or None,
            reason=reason or "strategy_revision",
            before=visible,
            request=payload,
            response=response,
            created_at=now,
        ),
    )
    return {
        "status": status,
        "order_id": order_id or None,
        "client_order_id": client_order_id or None,
        "cancel_type": "trigger" if is_trigger else "regular",
        "response": response,
        "reason": reason,
    }


@serialized_position_authority_mutation
def cancel_pending_entry_legs(
    session_factory: sessionmaker,
    *,
    trade_signal: TradeSignalRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Cancel only unfilled entry legs while preserving an already-entered binding."""

    now = executed_at or datetime.now(UTC)
    binding = _load_binding_for_signal(session_factory, trade_signal)
    pending_leg_keys = _pending_entry_leg_order_keys(session_factory, binding.id)
    if not pending_leg_keys["order_ids"] and not pending_leg_keys["client_order_ids"]:
        raise DeepcoinExecutionActionError("no_pending_entry_leg_to_cancel")

    inst_id = _to_deepcoin_swap_instrument(binding.symbol)
    trigger_orders = _select_orders_by_known_ids(
        deepcoin_client.list_trigger_orders_pending(inst_id=inst_id),
        order_ids=pending_leg_keys["order_ids"],
        client_order_ids=pending_leg_keys["client_order_ids"],
    )
    regular_orders = _select_orders_by_known_ids(
        deepcoin_client.list_open_orders(inst_id=inst_id),
        order_ids=pending_leg_keys["order_ids"],
        client_order_ids=pending_leg_keys["client_order_ids"],
    )
    if not trigger_orders and not regular_orders:
        raise DeepcoinExecutionActionError("no_bound_pending_entry_order")

    cancelled_orders: list[dict[str, Any]] = []
    for cancel_type, orders in (("trigger", trigger_orders), ("regular", regular_orders)):
        for order in orders:
            order_id = _order_id_from_payload(order)
            client_order_id = _first_string(
                order, "clOrdId", "clientOrderId", "client_order_id"
            )
            if not order_id and not client_order_id:
                raise DeepcoinExecutionActionError("missing_cancel_order_id")
            cancel_payload: dict[str, Any] = {"instId": inst_id}
            if order_id:
                cancel_payload["ordId"] = order_id
            if client_order_id:
                cancel_payload["clOrdId"] = client_order_id
            if cancel_type == "trigger":
                response = deepcoin_client.cancel_trigger_order(cancel_payload)
                event_action = "cancel_trigger_entry"
            else:
                cancel_payload["mrgPosition"] = _deepcoin_position_mode(binding.position_mode)
                response = deepcoin_client.cancel_order(cancel_payload)
                event_action = "cancel_regular_entry"
            cancelled_orders.append(
                {
                    "order_id": order_id,
                    "client_order_id": client_order_id,
                    "cancel_type": cancel_type,
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

    for cancelled_order in cancelled_orders:
        _update_entry_leg_status(
            session_factory,
            binding.id,
            status="cancelled",
            terminal_reason="operator_cancelled_unfilled_entry_leg",
            updated_at=now,
            order_id=cancelled_order.get("order_id"),
            client_order_id=cancelled_order.get("client_order_id"),
        )
    _remove_cancelled_order_ids_from_active_binding(
        session_factory,
        binding=binding,
        cancelled_order_ids={
            str(item["order_id"]) for item in cancelled_orders if item.get("order_id")
        },
        cancelled_client_order_ids={
            str(item["client_order_id"])
            for item in cancelled_orders
            if item.get("client_order_id")
        },
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
    old_order_id = _exact_exchange_order_id(old_order)
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
    new_order_id = next(
        (
            order_id
            for response_payload in _response_payloads(create_response)
            if (order_id := _exact_exchange_order_id(response_payload)) is not None
        ),
        None,
    )
    if new_order_id is None:
        raise DeepcoinExecutionActionError(
            "recreated_trigger_entry_missing_exchange_order_id"
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


def _pending_entry_leg_order_keys(
    session_factory: sessionmaker, binding_id: int
) -> dict[str, set[str]]:
    with session_factory() as session:
        legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == int(binding_id))
            .filter(ExecutionOrderLeg.purpose == "entry")
            .filter(ExecutionOrderLeg.pos_id.is_(None))
            .filter(ExecutionOrderLeg.status.in_(["pending", "open", "submitted"]))
            .all()
        )
    return {
        "order_ids": {
            str(leg.order_id).strip()
            for leg in legs
            if str(leg.order_id or "").strip()
        },
        "client_order_ids": {
            str(leg.client_order_id).strip()
            for leg in legs
            if str(leg.client_order_id or "").strip()
        },
    }


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


def _select_orders_by_known_ids(
    orders: list[dict[str, Any]],
    *,
    order_ids: set[str],
    client_order_ids: set[str],
) -> list[dict[str, Any]]:
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


def _exact_exchange_order_id(payload: dict[str, Any]) -> str | None:
    """Return an exchange-issued order id suitable for pending-entry replacement.

    A client id only correlates our local intent and a generic ``id`` is not a
    sufficiently specific DeepCoin order identity.  Pending-entry updates must
    therefore fail closed unless the exchange returned its concrete order id.
    """

    return _first_string(
        payload,
        "ordId",
        "orderId",
        "order_id",
        "algoId",
        "triggerOrderId",
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


def _adjust_protection_row_snapshots(
    rows: list[dict[str, Any]], *, action: str, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    stop_loss = _first_payload_value(
        payload, "stop_loss", "stop_loss_price", "sl", "slTriggerPx"
    )
    take_profit = _first_payload_value(
        payload, "take_profit", "take_profit_price", "tp", "tpTriggerPx"
    )
    adjusted: list[dict[str, Any]] = []
    for original in rows:
        row = dict(original)
        purpose = row.get("purpose")
        if purpose == "stop_loss" and stop_loss is not None:
            row["trigger_price"] = str(stop_loss)
        elif purpose == "take_profit" and take_profit is not None:
            row["trigger_price"] = str(take_profit)
        elif purpose == "combined":
            if stop_loss is not None:
                row["stop_loss"] = {
                    **dict(row["stop_loss"]),
                    "trigger_price": str(stop_loss),
                }
            if take_profit is not None:
                row["take_profit"] = {
                    **dict(row["take_profit"]),
                    "trigger_price": str(take_profit),
                }
        adjusted.append(row)
    if action.lower() == "adjust_stop_loss" and not any(
        row.get("purpose") in {"stop_loss", "combined"} for row in adjusted
    ):
        raise DeepcoinExecutionActionError("no_existing_stop_loss_to_adjust")
    return adjusted


def _new_protection_row_snapshots(after: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, purpose in (("take_profit", "take_profit"), ("stop_loss", "stop_loss")):
        if after.get(key) is None:
            continue
        rows.append(
            {
                "order_id": None,
                "purpose": purpose,
                "trigger_price": str(after[key]),
                "size": "0",
                "full_position": True,
                "trigger_type": "last",
                "order_price": "-1",
            }
        )
    return rows


def _record_position_tpsl_ledger_rows(
    session_factory: sessionmaker,
    *,
    binding: _LoadedBinding,
    position: dict[str, Any],
    inst_id: str,
    rows: list[dict[str, Any]],
    order_ids: list[str],
    evidence_source: str,
    seen_at: datetime,
) -> None:
    pos_id = _first_string(position, "posId", "pos_id", "id")
    if not pos_id:
        return
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == int(binding.id))
            .filter(ExecutionOrderLeg.purpose == "entry")
            .filter(ExecutionOrderLeg.pos_id == str(pos_id))
            .one_or_none()
        )
        if leg is None:
            return
        for row, order_id in zip(rows, order_ids, strict=False):
            upsert_protection_ledger_row(
                session,
                venue=binding.venue,
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id=str(pos_id),
                instrument_id=inst_id,
                side=binding.side,
                order_id=str(order_id),
                purpose=str(row.get("purpose") or ""),
                trigger_price=_ledger_trigger_price(row),
                size_text=str(row.get("size")) if row.get("size") is not None else None,
                status="verified",
                evidence_source=evidence_source,
                evidence={"match": "exchange_returned_order_id"},
                seen_at=seen_at,
            )
        session.commit()


def _mark_position_tpsl_ledger_cancelled(
    session_factory: sessionmaker,
    *,
    order_id: str,
    seen_at: datetime,
) -> None:
    with session_factory() as session:
        row = (
            session.query(PositionProtectionLedger)
            .filter(
                PositionProtectionLedger.venue == "deepcoin",
                PositionProtectionLedger.order_id == str(order_id),
            )
            .one_or_none()
        )
        if row is None:
            raise DeepcoinExecutionActionError(
                "position_tpsl_ledger_owner_missing"
            )
        row.status = "cancelled"
        row.last_seen_at = seen_at
        row.updated_at = seen_at
        session.commit()


def _ledger_trigger_price(row: dict[str, Any]) -> str | None:
    purpose = row.get("purpose")
    if purpose in {"take_profit", "stop_loss"}:
        value = row.get("trigger_price")
        return None if value is None else str(value)
    if purpose == "combined":
        stop = row.get("stop_loss")
        if isinstance(stop, dict) and stop.get("trigger_price") is not None:
            return str(stop["trigger_price"])
    return None


def _build_position_tpsl_row_payload(
    common: dict[str, Any], row: dict[str, Any]
) -> dict[str, Any]:
    result = dict(common)
    if not row.get("full_position"):
        try:
            size = Decimal(str(row.get("size")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise DeepcoinExecutionActionError("invalid_partial_position_tpsl_size") from exc
        if not size.is_finite() or size <= 0:
            raise DeepcoinExecutionActionError("invalid_partial_position_tpsl_size")
        result["sz"] = str(row["size"])
    purpose = row.get("purpose")
    if purpose in {"take_profit", "stop_loss"}:
        prefix = "tp" if purpose == "take_profit" else "sl"
        result[f"{prefix}TriggerPx"] = str(row["trigger_price"])
        result[f"{prefix}TriggerPxType"] = str(row.get("trigger_type") or "last")
        result[f"{prefix}OrdPx"] = str(row.get("order_price") or "-1")
    elif purpose == "combined":
        for key, prefix in (("take_profit", "tp"), ("stop_loss", "sl")):
            item = row[key]
            result[f"{prefix}TriggerPx"] = str(item["trigger_price"])
            result[f"{prefix}TriggerPxType"] = str(
                item.get("trigger_type") or "last"
            )
            result[f"{prefix}OrdPx"] = str(item.get("order_price") or "-1")
    else:
        raise DeepcoinExecutionActionError("unsupported_position_tpsl_row")
    return result


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
    # A pending entry has no exact position identity yet.  Keep the primary
    # market stop embedded in the entry and defer TP creation until a verified
    # filled position is available.
    if after.get("stop_loss") is not None:
        create.update(
            {
                "slTriggerPx": str(after["stop_loss"]),
                "slTriggerPxType": "last",
                "slOrdPx": "-1",
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


def _remove_cancelled_order_ids_from_active_binding(
    session_factory: sessionmaker,
    *,
    binding: _LoadedBinding,
    cancelled_order_ids: set[str],
    cancelled_client_order_ids: set[str],
    updated_at: datetime,
) -> None:
    remaining_order_ids = [
        order_id
        for order_id in _split_ids(binding.order_id)
        if order_id not in cancelled_order_ids
    ]
    remaining_client_order_ids = [
        client_order_id
        for client_order_id in _split_ids(binding.client_order_id)
        if client_order_id not in cancelled_client_order_ids
    ]
    _update_binding_order(
        session_factory,
        binding.id,
        order_id=",".join(remaining_order_ids) or None,
        client_order_id=",".join(remaining_client_order_ids) or None,
        last_exchange_status="pending_entry_leg_cancelled",
        updated_at=updated_at,
    )


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
    terminal_reason: str | None = None,
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
            if terminal_reason is not None:
                leg.terminal_reason = terminal_reason
            if new_order_id is not None:
                leg.order_id = new_order_id
            if new_client_order_id is not None:
                leg.client_order_id = new_client_order_id
            leg.updated_at = updated_at
        session.commit()
