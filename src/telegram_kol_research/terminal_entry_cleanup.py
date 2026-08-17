"""Resolve exact unfilled entry legs before a strategy terminates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Literal

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import DeepcoinTradingClientProtocol
from telegram_kol_research.deepcoin_execution_actions import (
    DeepcoinExecutionActionError,
    cancel_pending_entry_legs,
)
from telegram_kol_research.deepcoin_normalization import (
    normalize_deepcoin_swap_instrument,
)
from telegram_kol_research.execution_events import (
    enqueue_terminal_entry_cleanup_notification,
    list_execution_events,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    StrategyLifecycle,
)
from telegram_kol_research.position_attribution import TERMINAL_ENTRY_LEG_STATES
from telegram_kol_research.position_authority_lock import (
    serialized_position_authority_mutation,
)
from telegram_kol_research.trade_signals import (
    TradeSignalClaimError,
    claim_pending_trade_signal,
    enqueue_trade_signal,
    mark_trade_signal_failed,
    mark_trade_signal_submitted,
)

_CANCELLABLE_ENTRY_STATES = frozenset(
    {"pending", "open", "submitted", "partially_filled", "partial"}
)


@dataclass(frozen=True, slots=True)
class TerminalEntryCleanupResult:
    status: Literal["resolved", "already_absent", "blocked", "unknown"]
    binding_id: int
    lifecycle_id: int
    leg_ids: tuple[int, ...]
    order_ids: tuple[str, ...]
    event_ids: tuple[int, ...]


class _CancelWriteTracker:
    """Remember whether this cleanup invocation crossed an exchange write boundary."""

    def __init__(self, delegate: DeepcoinTradingClientProtocol) -> None:
        self._delegate = delegate
        self.cancel_attempted = False

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    def cancel_trigger_order(self, payload):
        self.cancel_attempted = True
        return self._delegate.cancel_trigger_order(payload)

    def cancel_order(self, payload):
        self.cancel_attempted = True
        return self._delegate.cancel_order(payload)


@serialized_position_authority_mutation
def cleanup_terminal_entry_legs(
    session_factory: sessionmaker,
    *,
    lifecycle_id: int,
    deepcoin_client: DeepcoinTradingClientProtocol,
    reason: str,
    cleaned_at: datetime | None = None,
    expected_binding_id: int | None = None,
    allow_position_bound_remainder: bool = False,
    live_execution_gate: Callable[[], bool] | None = None,
) -> TerminalEntryCleanupResult:
    now = cleaned_at or datetime.now(UTC)
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, int(lifecycle_id))
        if lifecycle is None or lifecycle.execution_binding_id is None:
            raise LookupError("terminal_entry_cleanup_lifecycle_not_bound")
        if (
            expected_binding_id is not None
            and int(lifecycle.execution_binding_id) != int(expected_binding_id)
        ):
            raise LookupError("terminal_entry_cleanup_binding_identity_changed")
        binding = session.get(ExecutionBinding, int(lifecycle.execution_binding_id))
        if binding is None:
            raise LookupError("terminal_entry_cleanup_binding_not_found")
        pending_legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == binding.id)
            .filter(ExecutionOrderLeg.purpose == "entry")
            .all()
        )
        pending_legs = [
            leg
            for leg in pending_legs
            if str(leg.status or "").lower() in _CANCELLABLE_ENTRY_STATES
            and (allow_position_bound_remainder or not leg.pos_id)
        ]
        if not pending_legs:
            return TerminalEntryCleanupResult(
                status="already_absent",
                binding_id=int(binding.id),
                lifecycle_id=int(lifecycle.id),
                leg_ids=(),
                order_ids=(),
                event_ids=(),
            )
        if any(not (leg.order_id or leg.client_order_id) for leg in pending_legs):
            return _with_notification(
                session_factory,
                TerminalEntryCleanupResult(
                    status="blocked",
                    binding_id=int(binding.id),
                    lifecycle_id=int(lifecycle.id),
                    leg_ids=tuple(sorted(int(leg.id) for leg in pending_legs)),
                    order_ids=tuple(
                        sorted(str(leg.order_id) for leg in pending_legs if leg.order_id)
                    ),
                    event_ids=(),
                ),
                reason=reason,
                created_at=now,
            )
        binding_id = int(binding.id)
        lifecycle_id_value = int(lifecycle.id)
        leg_ids = tuple(sorted(int(leg.id) for leg in pending_legs))
        order_ids = tuple(
            sorted(str(leg.order_id) for leg in pending_legs if leg.order_id)
        )
        signal_values = {
            "kol_id": binding.kol_id,
            "chat_id": int(binding.chat_id),
            "message_id": int(binding.message_id),
            "symbol": binding.symbol,
            "side": binding.side,
            "strategy_instance_id": binding.strategy_instance_id,
        }
        instrument_id = normalize_deepcoin_swap_instrument(binding.symbol)
        client_order_ids = tuple(
            sorted(
                str(leg.client_order_id)
                for leg in pending_legs
                if leg.client_order_id
            )
        )

    trade_signal = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="terminal_entry_cleanup",
        action="cancel_entry",
        payload={
            "binding_id": binding_id,
            "lifecycle_id": lifecycle_id_value,
            "reason": str(reason or "strategy_terminal"),
        },
        enqueued_at=now,
        **signal_values,
    )
    try:
        trade_signal = claim_pending_trade_signal(
            session_factory,
            signal_id=trade_signal.id,
            claimed_at=now,
        )
    except TradeSignalClaimError:
        return _with_notification(
            session_factory,
            TerminalEntryCleanupResult(
                status="unknown",
                binding_id=binding_id,
                lifecycle_id=lifecycle_id_value,
                leg_ids=leg_ids,
                order_ids=order_ids,
                event_ids=_event_ids_for_signal(session_factory, trade_signal.id),
            ),
            reason=reason,
            created_at=now,
        )
    tracked_client = _CancelWriteTracker(deepcoin_client)
    try:
        result = cancel_pending_entry_legs(
            session_factory,
            trade_signal=trade_signal,
            deepcoin_client=tracked_client,
            executed_at=now,
            allow_position_bound_remainder=allow_position_bound_remainder,
            live_execution_gate=live_execution_gate,
        )
    except DeepcoinExecutionActionError as exc:
        mark_trade_signal_failed(
            session_factory,
            signal_id=trade_signal.id,
            error=str(exc),
            failed_at=now,
            expected_status="processing",
            terminal_status=(
                "unknown_exchange_outcome"
                if tracked_client.cancel_attempted
                else "failed"
            ),
        )
        blocked_reasons = {
            "ambiguous_pending_entry_identity",
            "pending_entry_cancel_not_confirmed",
            "pending_entry_cancel_not_terminally_confirmed",
            "pending_entry_filled_during_cleanup",
            "pending_entry_leg_partially_filled",
            "pending_entry_terminal_evidence_unavailable",
            "live_execution_disabled",
        }
        return _with_notification(
            session_factory,
            TerminalEntryCleanupResult(
                status=(
                    "blocked"
                    if str(exc) in blocked_reasons
                    else "unknown"
                ),
                binding_id=binding_id,
                lifecycle_id=lifecycle_id_value,
                leg_ids=leg_ids,
                order_ids=order_ids,
                event_ids=_event_ids_for_signal(session_factory, trade_signal.id),
            ),
            reason=reason,
            created_at=now,
        )
    except Exception:
        still_visible = _entry_order_is_still_visible(
            tracked_client,
            instrument_id=instrument_id,
            order_ids=set(order_ids),
            client_order_ids=set(client_order_ids),
        )
        if still_visible is False:
            try:
                result = cancel_pending_entry_legs(
                    session_factory,
                    trade_signal=trade_signal,
                    deepcoin_client=tracked_client,
                    executed_at=now,
                    allow_position_bound_remainder=allow_position_bound_remainder,
                    live_execution_gate=live_execution_gate,
                )
            except Exception:
                mark_trade_signal_failed(
                    session_factory,
                    signal_id=trade_signal.id,
                    error="terminal_entry_cleanup_exchange_outcome_unknown",
                    failed_at=now,
                    expected_status="processing",
                    terminal_status=(
                        "unknown_exchange_outcome"
                        if tracked_client.cancel_attempted
                        else "failed"
                    ),
                )
                return _with_notification(
                    session_factory,
                    TerminalEntryCleanupResult(
                        status="unknown",
                        binding_id=binding_id,
                        lifecycle_id=lifecycle_id_value,
                        leg_ids=leg_ids,
                        order_ids=order_ids,
                        event_ids=_event_ids_for_signal(
                            session_factory, trade_signal.id
                        ),
                    ),
                    reason=reason,
                    created_at=now,
                )
        else:
            mark_trade_signal_failed(
                session_factory,
                signal_id=trade_signal.id,
                error="terminal_entry_cleanup_exchange_outcome_unknown",
                failed_at=now,
                expected_status="processing",
                terminal_status=(
                    "unknown_exchange_outcome"
                    if tracked_client.cancel_attempted
                    else "failed"
                ),
            )
            return _with_notification(
                session_factory,
                TerminalEntryCleanupResult(
                    status="unknown",
                    binding_id=binding_id,
                    lifecycle_id=lifecycle_id_value,
                    leg_ids=leg_ids,
                    order_ids=order_ids,
                    event_ids=_event_ids_for_signal(
                        session_factory, trade_signal.id
                    ),
                ),
                reason=reason,
                created_at=now,
            )

    status = (
        "already_absent"
        if result.get("status") == "already_absent"
        else "resolved"
    )
    terminal_reason = (
        "terminal_entry_cleanup_absent"
        if status == "already_absent"
        else "terminal_entry_cleanup_confirmed"
    )
    with session_factory() as session:
        legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.id.in_(leg_ids))
            .all()
        )
        for leg in legs:
            if allow_position_bound_remainder and leg.pos_id:
                leg.status = "active"
                leg.terminal_reason = None
            else:
                leg.status = "cancelled"
                leg.terminal_reason = terminal_reason
            leg.updated_at = now
        session.commit()
    mark_trade_signal_submitted(
        session_factory,
        signal_id=trade_signal.id,
        result=result,
        processed_at=now,
        expected_status="processing",
    )
    return _with_notification(
        session_factory,
        TerminalEntryCleanupResult(
            status=status,
            binding_id=binding_id,
            lifecycle_id=lifecycle_id_value,
            leg_ids=leg_ids,
            order_ids=order_ids,
            event_ids=_event_ids_for_signal(session_factory, trade_signal.id),
        ),
        reason=reason,
        created_at=now,
    )


def _with_notification(
    session_factory: sessionmaker,
    result: TerminalEntryCleanupResult,
    *,
    reason: str,
    created_at: datetime,
) -> TerminalEntryCleanupResult:
    notification_event_id = enqueue_terminal_entry_cleanup_notification(
        session_factory,
        lifecycle_id=result.lifecycle_id,
        binding_id=result.binding_id,
        status=result.status,
        leg_ids=result.leg_ids,
        order_ids=result.order_ids,
        reason=reason,
        created_at=created_at,
    )
    return TerminalEntryCleanupResult(
        status=result.status,
        binding_id=result.binding_id,
        lifecycle_id=result.lifecycle_id,
        leg_ids=result.leg_ids,
        order_ids=result.order_ids,
        event_ids=tuple(sorted({*result.event_ids, notification_event_id})),
    )


def _event_ids_for_signal(session_factory: sessionmaker, signal_id: int) -> tuple[int, ...]:
    return tuple(
        int(event.id)
        for event in list_execution_events(session_factory)
        if event.trade_signal_id == int(signal_id)
    )


def _entry_order_is_still_visible(
    deepcoin_client: DeepcoinTradingClientProtocol,
    *,
    instrument_id: str,
    order_ids: set[str],
    client_order_ids: set[str],
) -> bool | None:
    try:
        orders = [
            *(deepcoin_client.list_trigger_orders_pending(inst_id=instrument_id) or []),
            *(deepcoin_client.list_open_orders(inst_id=instrument_id) or []),
        ]
    except Exception:
        return None
    for order in orders:
        order_id = str(
            order.get("ordId")
            or order.get("orderId")
            or order.get("order_id")
            or ""
        )
        client_order_id = str(
            order.get("clOrdId")
            or order.get("clientOrderId")
            or order.get("client_order_id")
            or ""
        )
        if order_id in order_ids or client_order_id in client_order_ids:
            return True
    return False
