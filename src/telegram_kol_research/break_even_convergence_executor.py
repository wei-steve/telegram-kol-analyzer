"""Execute durable strategy-wide break-even convergence in audited phases."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import (
    DeepcoinDefiniteRejection,
    DeepcoinTradingClientProtocol,
)
from telegram_kol_research.deepcoin_normalization import (
    normalize_deepcoin_swap_instrument,
)
from telegram_kol_research.execution_events import (
    ExecutionEventRecord,
    record_execution_event,
)
from telegram_kol_research.deepcoin_trigger_rows import (
    position_id_or_none,
    stop_trigger_price,
    take_profit_trigger_price,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionMutationIntent,
    PositionProtectionLedger,
    StrategyBreakEvenConvergence,
    StrategyBreakEvenConvergenceLeg,
)
from telegram_kol_research.protection_authority import (
    evaluate_cancel_precheck,
    resolve_protection_authority,
)
from telegram_kol_research.protection_replacement import (
    GROUP_STOP as PROTECTION_GROUP_STOP,
    NewProtectionOrder,
    ProtectionReplacementPlan,
    replace_stop_group,
)
from telegram_kol_research.position_mutation_gateway import (
    close_exact_position,
    exact_position_write_gate,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.position_mutation_intents import (
    transition_position_mutation_intent,
)
from telegram_kol_research.strategy_management_market_policy import (
    assess_break_even_with_existing_stop,
)
from telegram_kol_research.terminal_entry_cleanup import (
    cleanup_terminal_entry_legs,
)
from telegram_kol_research.trading_settings import load_trading_settings


logger = logging.getLogger(__name__)

_MAX_QUOTE_AGE = timedelta(seconds=30)

#: Phase 6h. Positions whose break-even convergence may actually replace a stop
#: on the exchange. **Empty on purpose.**
#:
#: Correcting the preflight below (it read TPSL rows with a position row's
#: vocabulary and so refused every candidate it ever saw) reconnects a path
#: that has never once run to completion against the venue. Reconnecting it and
#: releasing it are two different decisions, and only the first one belongs in
#: the same change as a fixed read. While this set is empty the executor
#: computes the whole replacement, records it, and returns without writing.
#:
#: A constant rather than a setting, for the same reason as
#: ``ADOPTED_PRIMARY_BACKUP_RELEASED_POS_IDS``: releasing a position then costs
#: a code change, a full suite and a deploy, which is the deliberate pause. A
#: runtime flag would let the first one go out by editing a row.
BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS: frozenset[str] = frozenset()

#: Recorded on the convergence leg when a replacement was computed but held.
BREAK_EVEN_WOULD_REPLACE_KEY = "break_even_would_replace"


@dataclass(frozen=True, slots=True)
class BreakEvenConvergenceExecutionResult:
    id: int
    status: str
    reason_code: str | None


def execute_break_even_convergence(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
    stop_after_deferred_cleanup: bool = False,
) -> BreakEvenConvergenceExecutionResult:
    """Run one convergence without repeating exchange writes after uncertainty."""

    now = executed_at or datetime.now(UTC)
    with session_factory() as session:
        convergence = session.get(StrategyBreakEvenConvergence, int(convergence_id))
        if convergence is None:
            raise LookupError("break_even_convergence_not_found")
        if convergence.status in {
            "completed",
            "blocked",
            "failed_terminal",
            "recovery_required",
            "shadow_planned",
        }:
            return _result(convergence)
        if convergence.execution_mode == "disabled":
            convergence.status = "blocked"
            convergence.reason_code = "automatic_break_even_disabled"
            convergence.updated_at = now
            session.commit()
            return _result(convergence)
        if not _runtime_mode_enabled(
            session_factory,
            execution_mode=str(convergence.execution_mode),
        ):
            convergence.status = "blocked"
            convergence.reason_code = "automatic_break_even_runtime_disabled"
            convergence.updated_at = now
            session.commit()
            return _result(convergence)
        if convergence.execution_mode == "shadow":
            convergence.status = "shadow_deciding"
            convergence.reason_code = None
            convergence.updated_at = now
            session.commit()
            return _execute_shadow_market_decisions(
                session_factory,
                convergence_id=int(convergence_id),
                deepcoin_client=deepcoin_client,
                executed_at=now,
            )
        if convergence.execution_mode != "live":
            raise RuntimeError("break_even_convergence_execution_mode_invalid")
        snapshot = _load_target_snapshot(convergence.target_snapshot_json)
        deferred_ids = tuple(
            sorted(int(item) for item in snapshot.get("deferred_entry_leg_ids") or [])
        )
        current_deferred = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.id.in_(deferred_ids))
            .all()
            if deferred_ids
            else []
        )
        if {int(leg.id) for leg in current_deferred} != set(deferred_ids):
            convergence.status = "blocked"
            convergence.reason_code = "deferred_entry_target_drift"
            convergence.updated_at = now
            session.commit()
            return _result(convergence)
        for leg in current_deferred:
            if leg.pos_id or str(leg.status or "").lower() not in {
                "pending",
                "open",
                "submitted",
                "partially_filled",
                "partial",
                "cancelled",
                "exchange_cancelled",
            }:
                convergence.status = "blocked"
                convergence.reason_code = "deferred_entry_target_drift"
                convergence.updated_at = now
                session.commit()
                return _result(convergence)
        convergence.status = "preflight_verified"
        convergence.started_at = convergence.started_at or now
        convergence.updated_at = now
        session.commit()
        lifecycle_id = int(convergence.target_lifecycle_id)
        binding_id = int(convergence.execution_binding_id)

    cleanup = cleanup_terminal_entry_legs(
        session_factory,
        lifecycle_id=lifecycle_id,
        deepcoin_client=deepcoin_client,
        reason="automatic_break_even",
        cleaned_at=now,
        expected_binding_id=binding_id,
        live_execution_gate=lambda: _runtime_mode_enabled(
            session_factory,
            execution_mode="live",
        ),
    )
    with session_factory() as session:
        convergence = session.get(StrategyBreakEvenConvergence, int(convergence_id))
        if convergence is None:
            raise LookupError("break_even_convergence_not_found")
        if cleanup.status in {"resolved", "already_absent"}:
            convergence.status = "deciding_by_market"
            convergence.reason_code = None
        elif cleanup.status == "unknown":
            convergence.status = "recovery_required"
            convergence.reason_code = "deferred_entry_cancel_outcome_unknown"
        else:
            filled_during_cleanup = (
                session.query(ExecutionOrderLeg)
                .filter(ExecutionOrderLeg.id.in_(cleanup.leg_ids))
                .filter(ExecutionOrderLeg.pos_id.is_not(None))
                .first()
            )
            convergence.status = (
                "recovery_required" if filled_during_cleanup else "blocked"
            )
            convergence.reason_code = (
                "deferred_entry_filled_during_cleanup"
                if filled_during_cleanup
                else "deferred_entry_cancel_not_confirmed"
            )
        convergence.updated_at = now
        session.commit()
        result = _result(convergence)
    if stop_after_deferred_cleanup or result.status != "deciding_by_market":
        return result
    return _execute_market_decisions(
        session_factory,
        convergence_id=int(convergence_id),
        deepcoin_client=deepcoin_client,
        executed_at=now,
    )


def _execute_market_decisions(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime,
) -> BreakEvenConvergenceExecutionResult:
    """Freeze one quote/decision set, then execute exact idempotent mutations."""

    try:
        positions, pending, quote = _load_complete_market_preflight(
            session_factory,
            convergence_id=convergence_id,
            deepcoin_client=deepcoin_client,
            observed_at=executed_at,
        )
        _reserve_market_decisions(
            session_factory,
            convergence_id=convergence_id,
            positions=positions,
            pending=pending,
            quote=quote,
            decided_at=executed_at,
        )
    except Exception:
        return _finish_convergence(
            session_factory,
            convergence_id=convergence_id,
            status="blocked",
            reason_code="break_even_market_preflight_unavailable",
            finished_at=executed_at,
        )

    with session_factory() as session:
        convergence = session.get(StrategyBreakEvenConvergence, convergence_id)
        if convergence is None:
            raise LookupError("break_even_convergence_not_found")
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        legs = (
            session.query(StrategyBreakEvenConvergenceLeg)
            .filter_by(convergence_id=convergence_id)
            .order_by(StrategyBreakEvenConvergenceLeg.id.asc())
            .all()
        )
        if binding is None or not legs:
            raise RuntimeError("break_even_execution_identity_missing")
        instrument_id = normalize_deepcoin_swap_instrument(binding.symbol)
        work = [
            {
                "id": int(leg.id),
                "execution_binding_id": int(binding.id),
                "execution_order_leg_id": int(leg.execution_order_leg_id),
                "pos_id": str(leg.pos_id),
                "size": str(leg.preflight_size),
                "entry": str(leg.avg_entry_price),
                "status": str(leg.status),
                "decision": _load_decision(leg.decision_json),
            }
            for leg in legs
        ]

    held_unreleased = False
    for item in work:
        if item["status"] == "succeeded":
            continue
        if not _runtime_mode_enabled(session_factory, execution_mode="live"):
            return _finish_convergence(
                session_factory,
                convergence_id=convergence_id,
                status="blocked",
                reason_code="automatic_break_even_runtime_disabled",
                finished_at=executed_at,
            )
        action = item["decision"].get("action")
        try:
            if action == "keep_tighter_stop":
                _finish_leg(
                    session_factory,
                    leg_id=item["id"],
                    status="succeeded",
                    reason_code="existing_stop_at_or_better_than_cost",
                    finished_at=executed_at,
                )
                continue
            if action == "set_break_even":
                # Phase 6a. The same replacement sequence the management path
                # uses, so there is one implementation of "place the new stop,
                # then prove the old ones are gone" rather than two that drift.
                # What this adds here is the proof: before, the cancel was
                # issued and its outcome assumed.
                plan = ProtectionReplacementPlan(
                    venue="deepcoin",
                    pos_id=item["pos_id"],
                    instrument_id=instrument_id,
                    execution_binding_id=int(item["execution_binding_id"]),
                    execution_order_leg_id=int(item["execution_order_leg_id"]),
                    group=PROTECTION_GROUP_STOP,
                    new_orders=(
                        NewProtectionOrder(
                            purpose="stop_loss",
                            payload={
                                "instType": "SWAP",
                                "instId": instrument_id,
                                "posId": item["pos_id"],
                                "slTriggerPx": item["entry"],
                                "slTriggerPxType": "last",
                                "slOrdPx": "-1",
                                "sz": item["size"],
                            },
                        ),
                    ),
                    old_order_ids=tuple(
                        str(old_order_id)
                        for old_order_id in item["decision"].get(
                            "replace_stop_order_ids", []
                        )
                    ),
                    idempotency_prefix=f"break-even:{convergence_id}:{item['id']}",
                )
                # Phase 6h. The plan is complete and every check above has
                # passed; this is the last step before the exchange is written
                # to. Until a position is named in the released set, that write
                # does not happen: the computed replacement is recorded and the
                # item is skipped. Computed first and held second, deliberately
                # -- holding earlier would leave a record saying only "held",
                # with no target price and no order id for a person to approve
                # (the same mistake phase 6f made and fixed).
                if str(item["pos_id"]) not in BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS:
                    held_unreleased = True
                    _record_break_even_would_replace(
                        session_factory,
                        convergence_id=convergence_id,
                        item=item,
                        plan=plan,
                        recorded_at=executed_at,
                    )
                    continue
                # Phase 6e. The four-field read-back is not optional: the
                # order about to be cancelled is looked at again, by its exact
                # id, and instrument / posSide / trigger / size all have to
                # still agree with what was resolved. Until this was passed,
                # break-even cancelled on the strength of a read taken before
                # the new stop was even placed.
                with session_factory() as session:
                    authority = resolve_protection_authority(
                        session,
                        venue="deepcoin",
                        pos_id=item["pos_id"],
                        instrument_id=instrument_id,
                        side=_position_side_for_leg(session, item),
                        pending_rows=_read_pending_rows(
                            deepcoin_client, instrument_id
                        ),
                    )
                replacement = replace_stop_group(
                    session_factory,
                    plan=plan,
                    deepcoin_client=deepcoin_client,
                    executed_at=executed_at,
                    live_execution_gate=lambda pos_id=item["pos_id"]: (
                        _runtime_mode_enabled(session_factory, execution_mode="live")
                        and exact_position_write_gate(session_factory, pos_id=pos_id)
                    ),
                    pre_cancel_check=(
                        lambda rows, order_id, _authority=authority: (
                            evaluate_cancel_precheck(_authority, rows, str(order_id))
                        )
                    ),
                )
                order_id = (
                    replacement.new_order_ids[0]
                    if replacement.new_order_ids
                    else ""
                )
                if order_id:
                    _record_break_even_stop(
                        session_factory,
                        convergence_id=convergence_id,
                        leg_id=item["id"],
                        order_id=order_id,
                        instrument_id=instrument_id,
                        trigger_price=item["entry"],
                        size=item["size"],
                        seen_at=executed_at,
                    )
                if not replacement.succeeded:
                    # The new stop may well be live; the old one could not be
                    # proven gone. That is over-protection plus an incident the
                    # shared sequence already filed. Raising keeps this on the
                    # existing recovery path -- the convergence goes
                    # ``recovery_required`` and is never retried on its own --
                    # rather than reporting a convergence it did not achieve.
                    raise RuntimeError(
                        "break_even_stop_replacement_"
                        f"{replacement.reason_code or 'incomplete'}"
                    )
                _finish_leg(
                    session_factory,
                    leg_id=item["id"],
                    status="succeeded",
                    reason_code="break_even_stop_confirmed",
                    exchange_order_id=order_id,
                    finished_at=executed_at,
                )
                continue
            if action == "full_exit":
                response = close_exact_position(
                    session_factory=session_factory,
                    deepcoin_client=deepcoin_client,
                    pos_id=item["pos_id"],
                    instrument_id=instrument_id,
                    size=item["size"],
                    client_order_id=f"bec{convergence_id}l{item['id']}",
                    idempotency_key=(
                        f"break-even:{convergence_id}:{item['id']}:full-exit"
                    ),
                    live_execution_gate=lambda pos_id=item["pos_id"]: (
                        _runtime_mode_enabled(session_factory, execution_mode="live")
                        and exact_position_write_gate(session_factory, pos_id=pos_id)
                    ),
                    now_provider=lambda: executed_at,
                )
                remaining = [
                    row
                    for row in deepcoin_client.list_positions(inst_id=instrument_id)
                    if str(row.get("posId") or "") == item["pos_id"]
                    and _positive(row.get("pos"))
                ]
                if remaining:
                    raise RuntimeError("break_even_close_readback_pending")
                intent_id = _confirm_close_intent(
                    session_factory,
                    convergence_id=convergence_id,
                    leg_id=item["id"],
                    confirmed_at=executed_at,
                    response=response,
                )
                _finish_leg(
                    session_factory,
                    leg_id=item["id"],
                    status="succeeded",
                    reason_code="exact_position_closed_after_cost_cross",
                    exchange_order_id=_response_order_id(response),
                    mutation_intent_id=intent_id,
                    finished_at=executed_at,
                )
                continue
            raise RuntimeError("break_even_market_decision_invalid")
        except DeepcoinDefiniteRejection:
            _finish_leg(
                session_factory,
                leg_id=item["id"],
                status="failed_terminal",
                reason_code="break_even_mutation_rejected",
                finished_at=executed_at,
            )
            return _finish_convergence(
                session_factory,
                convergence_id=convergence_id,
                status="failed_terminal",
                reason_code="break_even_mutation_rejected",
                finished_at=executed_at,
            )
        except Exception:
            _finish_leg(
                session_factory,
                leg_id=item["id"],
                status="recovery_required",
                reason_code="break_even_mutation_outcome_unknown",
                finished_at=executed_at,
            )
            return _finish_convergence(
                session_factory,
                convergence_id=convergence_id,
                status="recovery_required",
                reason_code="break_even_mutation_outcome_unknown",
                finished_at=executed_at,
            )

    if held_unreleased:
        # Nothing was replaced, so this convergence did not converge. Reporting
        # "completed" here would be a false statement of exchange state -- the
        # weaker stop is still the position's stop -- and it is the reading a
        # person or a later round would act on.
        return _finish_convergence(
            session_factory,
            convergence_id=convergence_id,
            status="blocked",
            reason_code="break_even_replacement_not_released",
            finished_at=executed_at,
        )
    return _finish_convergence(
        session_factory,
        convergence_id=convergence_id,
        status="completed",
        reason_code=None,
        finished_at=executed_at,
    )


def _execute_shadow_market_decisions(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime,
) -> BreakEvenConvergenceExecutionResult:
    """Persist the exact would-do decisions while making zero exchange writes."""

    try:
        positions, pending, quote = _load_complete_market_preflight(
            session_factory,
            convergence_id=convergence_id,
            deepcoin_client=deepcoin_client,
            observed_at=executed_at,
        )
        _reserve_market_decisions(
            session_factory,
            convergence_id=convergence_id,
            positions=positions,
            pending=pending,
            quote=quote,
            decided_at=executed_at,
        )
    except Exception:
        return _finish_convergence(
            session_factory,
            convergence_id=convergence_id,
            status="shadow_planned",
            reason_code="shadow_market_preflight_unavailable",
            finished_at=executed_at,
        )
    with session_factory() as session:
        legs = (
            session.query(StrategyBreakEvenConvergenceLeg)
            .filter_by(convergence_id=convergence_id)
            .all()
        )
        for leg in legs:
            leg.status = "shadow_planned"
            leg.reason_code = "shadow_only_no_exchange_write"
            leg.updated_at = executed_at
        convergence = session.get(StrategyBreakEvenConvergence, convergence_id)
        if convergence is None:
            raise LookupError("break_even_convergence_not_found")
        convergence.status = "shadow_planned"
        convergence.reason_code = None
        convergence.completed_at = executed_at
        convergence.updated_at = executed_at
        session.commit()
        return _result(convergence)


def _load_complete_market_preflight(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    deepcoin_client: DeepcoinTradingClientProtocol,
    observed_at: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    with session_factory() as session:
        convergence = session.get(StrategyBreakEvenConvergence, convergence_id)
        if convergence is None:
            raise LookupError("break_even_convergence_not_found")
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        if binding is None:
            raise RuntimeError("break_even_binding_missing")
        instrument_id = normalize_deepcoin_swap_instrument(binding.symbol)
    positions = list(deepcoin_client.list_positions(inst_id=instrument_id))
    pending = list(deepcoin_client.list_trigger_orders_pending(inst_id=instrument_id))
    quote = deepcoin_client.get_ticker_quote(inst_id=instrument_id)
    if (
        not isinstance(quote, dict)
        or str(quote.get("instrument_id") or "").upper() != instrument_id.upper()
        or quote.get("price_field") not in {"last", "lastPx"}
        or not _positive(quote.get("price"))
        or not _quote_is_fresh(quote.get("observed_at"), now=observed_at)
    ):
        raise RuntimeError("break_even_market_quote_unavailable")
    return positions, pending, quote


def _runtime_mode_enabled(
    session_factory: sessionmaker,
    *,
    execution_mode: str,
) -> bool:
    settings = load_trading_settings(session_factory)
    if not settings.move_stop_to_breakeven_after_tp1:
        return False
    if execution_mode == "live":
        return settings.live_management_execution_enabled
    if execution_mode == "shadow":
        return settings.management_execution_mode == "shadow"
    return False


def _quote_is_fresh(value: Any, *, now: datetime) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        observed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed.tzinfo is None:
        return False
    current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    age = current.astimezone(UTC) - observed.astimezone(UTC)
    return timedelta(seconds=-5) <= age <= _MAX_QUOTE_AGE


def _reserve_market_decisions(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    positions: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    quote: dict[str, Any],
    decided_at: datetime,
) -> None:
    with session_factory() as session:
        convergence = session.get(StrategyBreakEvenConvergence, convergence_id)
        if convergence is None:
            raise LookupError("break_even_convergence_not_found")
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        legs = (
            session.query(StrategyBreakEvenConvergenceLeg)
            .filter_by(convergence_id=convergence_id)
            .order_by(StrategyBreakEvenConvergenceLeg.id.asc())
            .all()
        )
        if binding is None or not legs:
            raise RuntimeError("break_even_execution_identity_missing")
        if all(leg.decision_json != "{}" for leg in legs):
            return
        if any(leg.decision_json != "{}" for leg in legs):
            raise RuntimeError("break_even_market_decision_partial")
        instrument_id = normalize_deepcoin_swap_instrument(binding.symbol)
        pending_by_id = {
            str(row.get("ordId") or row.get("orderId") or ""): row
            for row in pending
        }
        for leg in legs:
            matches = [
                row for row in positions
                if str(row.get("posId") or "") == str(leg.pos_id)
            ]
            if len(matches) != 1:
                raise RuntimeError("break_even_live_position_not_unique")
            position = matches[0]
            if (
                str(position.get("instId") or "").upper() != instrument_id
                or str(position.get("posSide") or "").lower()
                != str(binding.side or "").lower()
                or not _decimal_equal(position.get("pos"), leg.preflight_size)
                or not _decimal_equal(position.get("avgPx"), leg.avg_entry_price)
            ):
                raise RuntimeError("break_even_live_position_drift")
            stop_rows = (
                session.query(PositionProtectionLedger)
                .filter_by(
                    venue="deepcoin",
                    execution_binding_id=convergence.execution_binding_id,
                    execution_order_leg_id=leg.execution_order_leg_id,
                    pos_id=leg.pos_id,
                    purpose="stop_loss",
                )
                .filter(PositionProtectionLedger.status.in_(["verified", "protected"]))
                .all()
            )
            stop_prices = []
            stop_order_ids = []
            for stop in stop_rows:
                exchange_row = pending_by_id.get(str(stop.order_id))
                if exchange_row is None:
                    raise RuntimeError("break_even_existing_stop_drift")
                # Phase 6h. Attribution is the order id -- the key this row was
                # looked up by. A TPSL row from ``trigger-orders-pending``
                # carries no ``posId`` at all, so requiring one to equal the
                # position id could only ever fail, and did: every candidate
                # this executor ever saw was refused here. A position id is
                # allowed to contradict, never to be required.
                row_pos_id = position_id_or_none(exchange_row)
                if row_pos_id is not None and row_pos_id != str(leg.pos_id):
                    raise RuntimeError("break_even_existing_stop_drift")
                # And the price is spelled ``slTriggerPrice`` on a TPSL row,
                # not ``slTriggerPx``. Read it by name, compare it as a number.
                if not _decimal_equal(
                    stop_trigger_price(exchange_row), stop.trigger_price
                ):
                    raise RuntimeError("break_even_existing_stop_drift")
                stop_prices.append(str(stop.trigger_price))
                stop_order_ids.append(str(stop.order_id))
            _validate_remaining_take_profits(
                session,
                convergence=convergence,
                leg=leg,
                pending_by_id=pending_by_id,
            )
            decision = assess_break_even_with_existing_stop(
                side=binding.side,
                entry_price=leg.avg_entry_price,
                market_price=quote["price"],
                existing_stop_prices=stop_prices,
            )
            leg.decision_json = json.dumps(
                {
                    "version": 1,
                    "action": decision.action,
                    "side": decision.market.side,
                    "entry_price": decision.market.entry_price,
                    "market_price": decision.market.market_price,
                    "quote_price_field": quote["price_field"],
                    "comparison": decision.market.comparison,
                    "existing_stop_prices": stop_prices,
                    "effective_stop_price": decision.effective_stop_price,
                    "replace_stop_order_ids": (
                        stop_order_ids
                        if decision.action == "set_break_even"
                        else []
                    ),
                    "decided_at": decided_at.isoformat(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            leg.status = "decision_reserved"
            leg.updated_at = decided_at
        convergence.status = "executing_market_decisions"
        convergence.updated_at = decided_at
        session.commit()


def _record_break_even_stop(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    leg_id: int,
    order_id: str,
    instrument_id: str,
    trigger_price: str,
    size: str,
    seen_at: datetime,
) -> None:
    with session_factory() as session:
        convergence = session.get(StrategyBreakEvenConvergence, convergence_id)
        leg = session.get(StrategyBreakEvenConvergenceLeg, leg_id)
        if convergence is None or leg is None:
            raise RuntimeError("break_even_execution_identity_missing")
        binding = session.get(ExecutionBinding, convergence.execution_binding_id)
        if binding is None:
            raise RuntimeError("break_even_binding_missing")
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=int(convergence.execution_binding_id),
            execution_order_leg_id=int(leg.execution_order_leg_id),
            strategy_instance_id=str(convergence.strategy_instance_id),
            pos_id=str(leg.pos_id),
            instrument_id=instrument_id,
            side=str(binding.side),
            order_id=order_id,
            purpose="stop_loss",
            trigger_price=trigger_price,
            size_text=size,
            status="verified",
            evidence_source="automatic_break_even",
            evidence={"convergence_id": convergence_id, "leg_id": leg_id},
            seen_at=seen_at,
        )
        intent = (
            session.query(PositionMutationIntent)
            .filter_by(idempotency_key=f"break-even:{convergence_id}:{leg_id}:set-stop")
            .one_or_none()
        )
        leg.mutation_intent_id = int(intent.id) if intent is not None else None
        leg.exchange_order_id = order_id
        leg.status = "stop_confirmed"
        leg.reason_code = "break_even_stop_confirmed_pending_old_stop_cleanup"
        leg.updated_at = seen_at
        session.commit()


def _record_break_even_would_replace(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    item: dict[str, Any],
    plan: ProtectionReplacementPlan,
    recorded_at: datetime,
) -> None:
    """Write down the replacement that was computed and not sent.

    Everything a person needs to approve it, in one row: the position, the
    stop it would move to, and the exact order ids it would cancel. Phase 6f
    learned this the hard way -- its first hold recorded only "held", which is
    unreviewable, so the plan is computed first and held second here too.

    The leg is finished ``blocked``. Leaving it pending would make the
    convergence spin, re-deciding and re-recording the same held replacement
    every round; ``blocked`` is also simply true -- the replacement did not
    happen. A release re-runs the convergence rather than resuming this one.
    """

    payload = {
        "pos_id": str(item["pos_id"]),
        "target_stop_price": str(item.get("entry")),
        "size": str(item.get("size")),
        "would_cancel_order_ids": list(plan.old_order_ids),
        "instrument_id": plan.instrument_id,
        "convergence_id": int(convergence_id),
        "released": False,
    }
    try:
        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                action=BREAK_EVEN_WOULD_REPLACE_KEY,
                venue="deepcoin",
                status="held",
                execution_binding_id=int(item["execution_binding_id"]),
                pos_id=str(item["pos_id"]),
                symbol=plan.instrument_id,
                order_id=",".join(plan.old_order_ids),
                reason="break_even_replacement_not_released",
                after=payload,
                created_at=recorded_at,
            ),
        )
    except Exception:  # pragma: no cover - a record must not break the hold
        logger.warning(
            "break-even would-replace record failed pos_id=%s",
            item.get("pos_id"),
            exc_info=True,
        )
    _finish_leg(
        session_factory,
        leg_id=item["id"],
        status="blocked",
        reason_code="break_even_replacement_not_released",
        finished_at=recorded_at,
    )


def _finish_leg(
    session_factory: sessionmaker,
    *,
    leg_id: int,
    status: str,
    reason_code: str,
    finished_at: datetime,
    exchange_order_id: str | None = None,
    mutation_intent_id: int | None = None,
) -> None:
    with session_factory() as session:
        leg = session.get(StrategyBreakEvenConvergenceLeg, leg_id)
        if leg is None:
            raise RuntimeError("break_even_leg_missing")
        leg.status = status
        leg.reason_code = reason_code
        leg.exchange_order_id = exchange_order_id or leg.exchange_order_id
        leg.mutation_intent_id = mutation_intent_id or leg.mutation_intent_id
        leg.updated_at = finished_at
        session.commit()


def _validate_remaining_take_profits(
    session,
    *,
    convergence: StrategyBreakEvenConvergence,
    leg: StrategyBreakEvenConvergenceLeg,
    pending_by_id: dict[str, dict[str, Any]],
) -> None:
    rows = (
        session.query(PositionProtectionLedger)
        .filter_by(
            venue="deepcoin",
            execution_binding_id=convergence.execution_binding_id,
            execution_order_leg_id=leg.execution_order_leg_id,
            pos_id=leg.pos_id,
            purpose="take_profit",
        )
        .filter(PositionProtectionLedger.status.in_(["verified", "protected"]))
        .all()
    )
    total = Decimal("0")
    for row in rows:
        pending = pending_by_id.get(str(row.order_id))
        if pending is None:
            raise RuntimeError("break_even_remaining_take_profit_drift")
        # Phase 6h, the same correction as the stop side above and in the same
        # change, because the two halves fail identically and fixing one alone
        # would leave a convergence refusing for the other's reason.
        pending_pos_id = position_id_or_none(pending)
        if pending_pos_id is not None and pending_pos_id != str(leg.pos_id):
            raise RuntimeError("break_even_remaining_take_profit_drift")
        if (
            not _decimal_equal(
                take_profit_trigger_price(pending), row.trigger_price
            )
            or row.size_text in (None, "")
            or not _decimal_equal(pending.get("sz"), row.size_text)
        ):
            raise RuntimeError("break_even_remaining_take_profit_drift")
        total += Decimal(str(row.size_text))
    if total > Decimal(str(leg.preflight_size)):
        raise RuntimeError("break_even_remaining_take_profit_oversized")


def _read_pending_rows(deepcoin_client, instrument_id: str):
    """One pending read for the pre-cancel check. ``None`` means "could not look"."""

    lister = getattr(deepcoin_client, "list_trigger_orders_pending", None)
    if not callable(lister):
        return None
    try:
        rows = lister(inst_id=instrument_id)
    except Exception:
        return None
    return rows if isinstance(rows, list) else None


def _position_side_for_leg(session, item) -> str:
    leg = session.get(ExecutionOrderLeg, int(item["execution_order_leg_id"]))
    if leg is None:
        return ""
    binding = session.get(ExecutionBinding, int(item["execution_binding_id"]))
    return str(getattr(binding, "side", "") or "").lower()


def _mark_old_stop_cancelled(
    session_factory: sessionmaker,
    *,
    order_id: str,
    cancelled_at: datetime,
) -> None:
    with session_factory() as session:
        row = (
            session.query(PositionProtectionLedger)
            .filter_by(venue="deepcoin", order_id=order_id)
            .one_or_none()
        )
        if row is not None:
            row.status = "cancelled"
            row.last_seen_at = cancelled_at
            row.updated_at = cancelled_at
            session.commit()


def _confirm_close_intent(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    leg_id: int,
    confirmed_at: datetime,
    response: Any,
) -> int | None:
    key = f"break-even:{convergence_id}:{leg_id}:full-exit"
    with session_factory() as session:
        intent = (
            session.query(PositionMutationIntent)
            .filter_by(idempotency_key=key)
            .one_or_none()
        )
        intent_id = int(intent.id) if intent is not None else None
    if intent_id is not None:
        transition_position_mutation_intent(
            session_factory,
            intent_id,
            expected_statuses={"submitted"},
            new_status="confirmed",
            transitioned_at=confirmed_at,
            response=response if isinstance(response, dict) else None,
        )
    return intent_id


def _finish_convergence(
    session_factory: sessionmaker,
    *,
    convergence_id: int,
    status: str,
    reason_code: str | None,
    finished_at: datetime,
) -> BreakEvenConvergenceExecutionResult:
    with session_factory() as session:
        convergence = session.get(StrategyBreakEvenConvergence, convergence_id)
        if convergence is None:
            raise LookupError("break_even_convergence_not_found")
        convergence.status = status
        convergence.reason_code = reason_code
        convergence.completed_at = finished_at if status == "completed" else None
        convergence.updated_at = finished_at
        session.commit()
        return _result(convergence)


def _load_decision(value: str) -> dict[str, Any]:
    loaded = json.loads(value)
    if not isinstance(loaded, dict) or not loaded.get("action"):
        raise RuntimeError("break_even_market_decision_invalid")
    return loaded


def _response_order_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    candidates = data if isinstance(data, list) else [data, response]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("ordId", "orderId", "order_id", "orderSysID"):
            if candidate.get(key) not in (None, ""):
                return str(candidate[key])
    return None


def _decimal_equal(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _positive(value: Any) -> bool:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return parsed.is_finite() and parsed > 0


def _load_target_snapshot(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise RuntimeError("break_even_target_snapshot_corrupt") from None
    if not isinstance(parsed, dict):
        raise RuntimeError("break_even_target_snapshot_corrupt")
    return parsed


def _result(
    convergence: StrategyBreakEvenConvergence,
) -> BreakEvenConvergenceExecutionResult:
    return BreakEvenConvergenceExecutionResult(
        id=int(convergence.id),
        status=str(convergence.status),
        reason_code=convergence.reason_code,
    )
