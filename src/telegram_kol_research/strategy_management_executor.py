"""Crash-safe submission of immutable Deepcoin strategy-management batches."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_client import (
    DeepcoinDefiniteRejection,
    DeepcoinTradingClientProtocol,
)
from telegram_kol_research.deepcoin_normalization import (
    normalize_deepcoin_margin_mode,
    normalize_deepcoin_position_mode,
    normalize_deepcoin_swap_instrument,
)
from telegram_kol_research.execution_events import (
    ExecutionEventRecord,
    record_execution_event,
)
from telegram_kol_research.execution_bindings import _load_reconcile_snapshot
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    PositionProtectionLeg,
    RawMessage,
    StrategyLifecycle,
    StrategyManagementBatch,
    TriggerProtectionIntent,
    TriggerProtectionStopRescue,
)
from telegram_kol_research.position_attribution import TERMINAL_ENTRY_LEG_STATES
from telegram_kol_research.position_authority_lock import (
    serialized_position_authority_mutation,
)
from telegram_kol_research.position_mutation_gateway import (
    cancel_exact_position_sltp,
    close_exact_position,
    exact_position_write_gate,
    submit_exact_position_sltp,
)
from telegram_kol_research.remediation_snapshot import remediation_snapshot_payload
from telegram_kol_research.protection_attribution import (
    match_position_protection,
    normalize_protection_snapshot_rows,
    snapshot_protection_rows,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.position_protection_legs import (
    bind_filled_position,
    bind_verified_exchange_order,
)
from telegram_kol_research.protection_revisions import record_replacing_protection_revision
from telegram_kol_research.protection_ledger import list_verified_account_ledger_rows
from telegram_kol_research.strategy_management_batches import (
    ManagementBatchRecord,
    claim_ready_batch,
    load_management_batch,
    transition_batch,
    transition_leg,
)
from telegram_kol_research.strategy_management_market_decisions import (
    BreakEvenMarketDecisionConflict,
    BreakEvenMarketDecisionRecord,
    load_break_even_market_decision,
    reserve_break_even_market_decision,
)
from telegram_kol_research.strategy_management_market_policy import (
    BreakEvenMarketPolicyError,
    assess_break_even_market,
)
from telegram_kol_research.trigger_protection_intents import transition_trigger_protection_intent
from telegram_kol_research.trading_settings import load_trading_settings


DEEPCOIN_CLIENT_ORDER_ID_MAX_LENGTH = 20
_CLOSE_ACTIONS = frozenset(
    {"partial_close", "full_close", "full_exit", "partial_then_break_even"}
)
_PROTECTION_ACTIONS = frozenset({"adjust_stop_loss", "move_stop_to_break_even"})
_PROTECTION_PHASE_LEG_STATES = frozenset(
    {"succeeded", "restored", "recovery_required"}
)
_MANAGEABLE_ENTRY_LEG_STATES = frozenset(
    {"active", "open", "filled", "partial_closed"}
)
_DEFERRED_ENTRY_LEG_STATES = frozenset(
    {"open", "pending", "submitted"}
)


class ManagementBatchExecutionError(RuntimeError):
    """Raised when a batch cannot be submitted without guessing its state."""


class DeferredEntryCancellationError(ManagementBatchExecutionError):
    """Carry bounded per-leg diagnostics across a fail-closed preflight."""

    def __init__(self, reason: str, diagnostics: list[dict[str, Any]]):
        super().__init__(reason)
        self.diagnostics = diagnostics


class DeferredEntryIdentityDriftError(DeferredEntryCancellationError):
    """Exact deferred-entry DB identity changed after batch planning."""


@dataclass(frozen=True, slots=True)
class _DeferredExchangeMatch:
    entry: ExecutionOrderLeg
    cancel_type: str
    source: str
    order: dict[str, Any]
    order_id: str | None
    client_order_id: str | None


def build_management_client_order_id(*, batch_id: int, leg_id: int) -> str:
    """Return a stable Deepcoin-safe close ID derived only from durable IDs."""

    digest = hashlib.sha256(f"management:{batch_id}:{leg_id}".encode()).hexdigest()
    value = f"TM{digest[:18]}".upper()
    return value[:DEEPCOIN_CLIENT_ORDER_ID_MAX_LENGTH]


@serialized_position_authority_mutation
def execute_trigger_protection_stop_rescue(
    session_factory: sessionmaker,
    *,
    rescue_id: int,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Execute the explicitly planned SL-only rescue exactly once.

    A durable ``reserved`` state is written before the exchange call.  It is
    intentionally never retried automatically: a process crash at the response
    boundary must not turn into a duplicate position stop.
    """

    now = executed_at or datetime.now(UTC)
    with session_factory() as session:
        rescue = session.get(TriggerProtectionStopRescue, int(rescue_id))
        if rescue is None:
            raise ManagementBatchExecutionError("trigger_protection_rescue_not_found")
        if rescue.status != "ready":
            return _trigger_protection_rescue_result(rescue)
        intent = session.get(TriggerProtectionIntent, rescue.trigger_protection_intent_id)
        if intent is None:
            rescue.status = "blocked"
            rescue.reason_code = "rescue_intent_not_found"
            rescue.completed_at = now
            rescue.updated_at = now
            session.commit()
            return _trigger_protection_rescue_result(rescue)
        from telegram_kol_research.strategy_management_planner import (
            _prepare_trigger_protection_stop_rescue,
        )

        prepared = _prepare_trigger_protection_stop_rescue(
            session, intent=intent, deepcoin_client=deepcoin_client
        )
        if isinstance(prepared, str):
            rescue.status = "blocked"
            rescue.reason_code = prepared
            rescue.completed_at = now
            rescue.updated_at = now
            session.commit()
            return _trigger_protection_rescue_result(rescue)
        leg, payload = prepared
        if str(leg.pos_id) != rescue.pos_id:
            rescue.status = "blocked"
            rescue.reason_code = "rescue_position_identity_drift"
            rescue.completed_at = now
            rescue.updated_at = now
            session.commit()
            return _trigger_protection_rescue_result(rescue)
        rescue.status = "reserved"
        rescue.request_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        rescue.reserved_at = now
        rescue.updated_at = now
        reserved_pos_id = str(rescue.pos_id)
        session.commit()

    try:
        response = submit_exact_position_sltp(
            session_factory=session_factory,
            deepcoin_client=deepcoin_client,
            pos_id=reserved_pos_id,
            payload=payload,
            idempotency_key=f"rescue:{rescue_id}:set:stop_loss",
            live_execution_gate=lambda: exact_position_write_gate(
                session_factory, pos_id=reserved_pos_id
            ),
            now_provider=lambda: now,
            require_readback=True,
        )
    except DeepcoinDefiniteRejection as exc:
        return _complete_trigger_protection_rescue_failure(
            session_factory, rescue_id=int(rescue_id), now=now,
            reason="rescue_submission_rejected", error=exc,
        )
    except Exception as exc:
        return _complete_trigger_protection_rescue_failure(
            session_factory, rescue_id=int(rescue_id), now=now,
            reason="rescue_submission_outcome_unknown", error=exc, unknown=True,
        )
    order_id = _extract_order_id(response)
    if not order_id:
        return _complete_trigger_protection_rescue_failure(
            session_factory, rescue_id=int(rescue_id), now=now,
            reason="rescue_response_missing_order_id", error="missing ordId", unknown=True,
            response=response,
        )

    # Commit accepted exchange identity before any local ledger/event work.  A
    # later local failure must never erase the response and invite a duplicate.
    with session_factory() as session:
        rescue = session.get(TriggerProtectionStopRescue, int(rescue_id))
        if rescue is None or rescue.status != "reserved":
            raise ManagementBatchExecutionError("trigger_protection_rescue_response_persist_conflict")
        rescue.status = "submitted"
        rescue.reason_code = "rescue_stop_submitted"
        rescue.exchange_order_id = order_id
        rescue.response_json = json.dumps(response, ensure_ascii=False, sort_keys=True)
        rescue.error_json = None
        rescue.completed_at = now
        rescue.updated_at = now
        session.commit()

    # Ledger ownership and the event follow the authoritative response record.
    # If either fails, the submitted rescue remains durable and non-retryable.
    with session_factory() as session:
        rescue = session.get(TriggerProtectionStopRescue, int(rescue_id))
        intent = session.get(TriggerProtectionIntent, rescue.trigger_protection_intent_id)
        binding = session.get(ExecutionBinding, rescue.execution_binding_id)
        leg = session.get(ExecutionOrderLeg, rescue.execution_order_leg_id)
        if rescue.status != "submitted" or intent is None or binding is None or leg is None:
            raise ManagementBatchExecutionError("trigger_protection_rescue_persistence_conflict")
        upsert_protection_ledger_row(
            session,
            venue="deepcoin", execution_binding_id=binding.id,
            execution_order_leg_id=leg.id, strategy_instance_id=binding.strategy_instance_id,
            pos_id=rescue.pos_id, instrument_id=f"{binding.symbol.upper()}-USDT-SWAP",
            side=binding.side, order_id=order_id, purpose="stop_loss",
            trigger_price=str(payload["slTriggerPx"]), size_text=None, status="verified",
            evidence_source="trigger_protection_stop_rescue",
            evidence={"rescue_id": rescue.id, "intent_id": intent.id, "response": response},
            seen_at=now,
        )
        primary = (
            session.query(PositionProtectionLeg)
            .filter(
                PositionProtectionLeg.execution_order_leg_id == int(leg.id),
                PositionProtectionLeg.role == "primary_stop",
                PositionProtectionLeg.leg_index == 1,
            )
            .one_or_none()
        )
        if primary is not None:
            bind_filled_position(session, primary, pos_id=rescue.pos_id)
            bind_verified_exchange_order(
                session,
                primary,
                exchange_order_id=order_id,
                readback_evidence={
                    "source": "trigger_protection_stop_rescue",
                    "rescue_id": int(rescue.id),
                    "order_id": order_id,
                    "pos_id": rescue.pos_id,
                },
            )
        transition_trigger_protection_intent(
            session, intent, recovery_state="adopted", adopted_order_id=order_id
        )
        record_execution_event(
            session_factory,
            ExecutionEventRecord(
                execution_binding_id=binding.id, strategy_instance_id=binding.strategy_instance_id,
                kol_id=binding.kol_id, chat_id=binding.chat_id, message_id=binding.message_id,
                symbol=binding.symbol, side=binding.side, action="trigger_protection_stop_rescue",
                status="submitted", order_id=order_id, pos_id=rescue.pos_id,
                reason="deferred_trigger_protection_stop_only", request=payload,
                response=response, created_at=now,
            ),
            session=session,
        )
        rescue.status = "verified"
        rescue.reason_code = "rescue_stop_verified"
        rescue.updated_at = now
        session.commit()
        return _trigger_protection_rescue_result(rescue)


def _complete_trigger_protection_rescue_failure(
    session_factory, *, rescue_id: int, now: datetime, reason: str,
    error: object, unknown: bool = False, response: Any = None,
) -> dict[str, Any]:
    with session_factory() as session:
        rescue = session.get(TriggerProtectionStopRescue, rescue_id)
        if rescue is None or rescue.status != "reserved":
            raise ManagementBatchExecutionError("trigger_protection_rescue_state_conflict")
        rescue.status = "submit_unknown" if unknown else "failed"
        rescue.reason_code = reason
        rescue.response_json = (
            json.dumps(response, ensure_ascii=False, sort_keys=True)
            if isinstance(response, dict) else None
        )
        rescue.error_json = json.dumps(
            {"type": type(error).__name__, "message": str(error)[:512]},
            ensure_ascii=False,
            sort_keys=True,
        )
        rescue.completed_at = now
        rescue.updated_at = now
        session.commit()
        return _trigger_protection_rescue_result(rescue)


def _trigger_protection_rescue_result(rescue: TriggerProtectionStopRescue) -> dict[str, Any]:
    return {
        "rescue_id": int(rescue.id), "status": rescue.status,
        "reason": rescue.reason_code, "order_id": rescue.exchange_order_id,
    }


def reserve_break_even_market_actions(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    observed_at: datetime,
) -> BreakEvenMarketDecisionRecord:
    """Reserve one per-position decision before any exchange mutation."""

    if (
        batch.intent != "move_stop_to_break_even"
        or batch.effective_action != "break_even_by_market"
        or batch.status != "executing"
        or not batch.legs
    ):
        raise ManagementBatchExecutionError(
            "break_even_market_batch_not_executable"
        )
    existing = load_break_even_market_decision(
        session_factory, batch_id=batch.id
    )
    if existing is not None:
        binding = _load_exact_binding(session_factory, batch)
        _require_exact_entry_legs(session_factory, batch)
        inst_id = normalize_deepcoin_swap_instrument(binding.symbol)
        if existing.instrument_id.upper() != inst_id.upper():
            raise ManagementBatchExecutionError(
                "break_even_market_decision_instrument_drift"
            )
        live_positions = list(
            deepcoin_client.list_positions(inst_id=inst_id)
        )
        _preflight_exact_protection_positions(
            session_factory=session_factory,
            batch=batch,
            binding=binding,
            live_positions=live_positions,
            inst_id=inst_id,
            after_partial_close=False,
        )
        decisions_by_leg_id = {
            int(row["management_leg_id"]): row
            for row in existing.decisions
        }
        protected_legs = []
        for leg in batch.legs:
            decision_row = decisions_by_leg_id.get(int(leg.id))
            if decision_row is None:
                raise ManagementBatchExecutionError(
                    "break_even_market_decision_leg_set_not_exact"
                )
            if decision_row["action"] == "set_break_even":
                protected_legs.append(
                    replace(
                        leg,
                        old_tpsl=dict(decision_row["protection"]),
                    )
                )
        if protected_legs:
            pending = list(
                deepcoin_client.list_trigger_orders_pending(inst_id=inst_id)
            )
            _preflight_exact_protection_rows(
                session_factory=session_factory,
                batch=replace(batch, legs=tuple(protected_legs)),
                live_positions=live_positions,
                pending=pending,
            )
        return existing

    binding = _load_exact_binding(session_factory, batch)
    _require_exact_entry_legs(session_factory, batch)
    inst_id = normalize_deepcoin_swap_instrument(binding.symbol)
    live_positions = list(deepcoin_client.list_positions(inst_id=inst_id))
    _preflight_exact_protection_positions(
        session_factory=session_factory,
        batch=batch,
        binding=binding,
        live_positions=live_positions,
        inst_id=inst_id,
        after_partial_close=False,
    )
    try:
        quote = deepcoin_client.get_ticker_quote(inst_id=inst_id)
    except Exception as exc:
        raise ManagementBatchExecutionError(
            "break_even_market_quote_unavailable"
        ) from exc
    if (
        not isinstance(quote, dict)
        or str(quote.get("instrument_id") or "").upper() != inst_id.upper()
        or quote.get("price") in (None, "")
        or quote.get("price_field") not in {"last", "lastPx"}
    ):
        raise ManagementBatchExecutionError(
            "break_even_market_quote_unavailable"
        )

    decisions: list[dict[str, Any]] = []
    allowed_legs = []
    for leg in batch.legs:
        try:
            market = assess_break_even_market(
                side=binding.side,
                entry_price=leg.avg_entry_price,
                market_price=quote["price"],
            )
        except BreakEvenMarketPolicyError as exc:
            raise ManagementBatchExecutionError(
                "break_even_market_quote_unavailable"
            ) from exc
        action = "set_break_even" if market.allowed else "full_exit"
        decisions.append(
            {
                "management_leg_id": int(leg.id),
                "execution_order_leg_id": int(leg.execution_order_leg_id),
                "pos_id": str(leg.pos_id),
                "side": market.side,
                "entry_price": market.entry_price,
                "comparison": market.comparison,
                "action": action,
            }
        )
        if market.allowed:
            allowed_legs.append(leg)

    if allowed_legs:
        pending = list(
            deepcoin_client.list_trigger_orders_pending(inst_id=inst_id)
        )
        if all(
            isinstance(leg.old_tpsl, dict)
            and leg.old_tpsl.get("order_ids")
            for leg in allowed_legs
        ):
            protection_rows = _preflight_exact_protection_rows(
                session_factory=session_factory,
                batch=replace(batch, legs=tuple(allowed_legs)),
                live_positions=live_positions,
                pending=pending,
            )
        else:
            ledger_rows_by_pos_id = _ledger_rows_by_pos_id(
                session_factory,
                [leg.pos_id for leg in allowed_legs],
            )
            matches = match_position_protection(
                live_positions,
                pending,
                exact_order_position_ids=_exact_order_position_ids(
                    ledger_rows_by_pos_id
                ),
            )
            protection_rows = {}
            seen_order_ids: set[str] = set()
            for leg in allowed_legs:
                protection = matches.by_pos_id.get(str(leg.pos_id))
                current_rows = (
                    snapshot_protection_rows(protection.rows)
                    if protection is not None
                    else []
                )
                order_ids = [
                    str(row.get("order_id") or "") for row in current_rows
                ]
                if (
                    protection is None
                    or protection.status != "verified"
                    or not current_rows
                    or [str(order_id) for order_id in protection.order_ids]
                    != order_ids
                    or not all(order_ids)
                    or len(order_ids) != len(set(order_ids))
                    or bool(seen_order_ids.intersection(order_ids))
                ):
                    raise ManagementBatchExecutionError(
                        "protection_preflight_rows_ambiguous_or_drifted"
                    )
                seen_order_ids.update(order_ids)
                protection_rows[str(leg.pos_id)] = current_rows
        decisions_by_pos_id = {
            str(row["pos_id"]): row for row in decisions
        }
        for leg in allowed_legs:
            current_rows = protection_rows[str(leg.pos_id)]
            decisions_by_pos_id[str(leg.pos_id)]["protection"] = {
                "order_ids": [
                    str(row["order_id"]) for row in current_rows
                ],
                "row_snapshots": current_rows,
            }

    return reserve_break_even_market_decision(
        session_factory,
        batch_id=batch.id,
        instrument_id=inst_id,
        quote_price=quote["price"],
        quote_price_field=str(quote["price_field"]),
        observed_at=observed_at,
        decisions=decisions,
    )


def _execute_break_even_by_market_batch(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime,
) -> dict[str, Any]:
    """Execute one durable per-position close/protection decision."""

    if batch.status == "ready":
        claimed = claim_ready_batch(
            session_factory, batch.id, claimed_at=executed_at
        )
        batch = claimed or load_management_batch(session_factory, batch.id)
    if batch.status == "reconciling":
        return _result(batch, reason="batch_already_reconciling")
    if batch.status != "executing":
        raise ManagementBatchExecutionError(
            f"batch_not_executable:{batch.status}"
        )
    restart_result = _recover_break_even_post_write_restart(
        session_factory,
        batch=batch,
        recovered_at=executed_at,
    )
    if restart_result is not None:
        return restart_result

    decision = reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=deepcoin_client,
        observed_at=executed_at,
    )
    decisions_by_leg_id = {
        int(row["management_leg_id"]): row for row in decision.decisions
    }
    if set(decisions_by_leg_id) != {int(leg.id) for leg in batch.legs}:
        raise ManagementBatchExecutionError(
            "break_even_market_decision_leg_set_not_exact"
        )
    for leg in batch.legs:
        if leg.status != "reserved":
            continue
        action = decisions_by_leg_id[int(leg.id)]["action"]
        transition_leg(
            session_factory,
            leg.id,
            expected_statuses={"reserved"},
            new_status=(
                "submit_unknown"
                if action == "full_exit"
                else "recovery_required"
            ),
            transitioned_at=executed_at,
            last_error={
                "reason": (
                    "reserved_submission_outcome_unknown"
                    if action == "full_exit"
                    else "reserved_protection_outcome_unknown"
                )
            },
        )
    batch = load_management_batch(session_factory, batch.id)
    binding = _load_exact_binding(session_factory, batch)
    close_decisions = {
        leg_id: row
        for leg_id, row in decisions_by_leg_id.items()
        if row["action"] == "full_exit"
    }
    if close_decisions:
        _cancel_deferred_entry_legs(
            session_factory,
            batch=batch,
            binding=binding,
            deepcoin_client=deepcoin_client,
            cancelled_at=executed_at,
        )

    batch = load_management_batch(session_factory, batch.id)
    for leg in batch.legs:
        row = decisions_by_leg_id[int(leg.id)]
        if row["action"] != "full_exit" or leg.status != "planned":
            continue
        client_order_id = build_management_client_order_id(
            batch_id=batch.id, leg_id=leg.id
        )
        request = _close_payload(
            binding=binding,
            pos_id=leg.pos_id,
            close_size=str(leg.preflight_size),
            client_order_id=client_order_id,
        )
        if not transition_leg(
            session_factory,
            leg.id,
            expected_statuses={"planned"},
            new_status="reserved",
            transitioned_at=executed_at,
            client_order_id=client_order_id,
            request=request,
            last_error=None,
        ):
            raise ManagementBatchExecutionError(
                f"management_leg_reservation_conflict:{leg.id}"
            )
        try:
            _require_remediation_live_gate(session_factory, batch=batch)
            response = close_exact_position(
                session_factory=session_factory,
                deepcoin_client=deepcoin_client,
                pos_id=str(leg.pos_id),
                instrument_id=str(request["instId"]),
                size=str(request["sz"]),
                client_order_id=client_order_id,
                idempotency_key=f"management:{batch.id}:{leg.id}:close:{client_order_id}",
                live_execution_gate=lambda: exact_position_write_gate(
                    session_factory, pos_id=str(leg.pos_id)
                ),
                now_provider=lambda: executed_at,
            )
        except DeepcoinDefiniteRejection as exc:
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="failed",
                transitioned_at=executed_at,
                last_error={"type": type(exc).__name__, "message": str(exc)},
            )
            _record_leg_event(
                session_factory,
                batch=batch,
                binding=binding,
                leg_id=leg.id,
                pos_id=leg.pos_id,
                client_order_id=client_order_id,
                request=request,
                response=None,
                order_id=None,
                status="failed",
                reason="submission_rejected",
                created_at=executed_at,
            )
            continue
        except Exception as exc:
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="submit_unknown",
                transitioned_at=executed_at,
                last_error={"type": type(exc).__name__, "message": str(exc)},
            )
            _record_leg_event(
                session_factory,
                batch=batch,
                binding=binding,
                leg_id=leg.id,
                pos_id=leg.pos_id,
                client_order_id=client_order_id,
                request=request,
                response=None,
                order_id=None,
                status="submit_unknown",
                reason="submission_outcome_unknown",
                created_at=executed_at,
            )
            continue
        order_id = _extract_order_id(response)
        next_status = "submitted" if order_id else "submit_unknown"
        transition_leg(
            session_factory,
            leg.id,
            expected_statuses={"reserved"},
            new_status=next_status,
            transitioned_at=executed_at,
            exchange_order_id=order_id,
            response=response,
            last_error=(
                None
                if order_id
                else {"reason": "submission_response_missing_order_id"}
            ),
        )
        _record_leg_event(
            session_factory,
            batch=batch,
            binding=binding,
            leg_id=leg.id,
            pos_id=leg.pos_id,
            client_order_id=client_order_id,
            request=request,
            response=response,
            order_id=order_id,
            status=next_status,
            reason=(
                "management_close_submitted"
                if order_id
                else "submission_response_missing_order_id"
            ),
            created_at=executed_at,
        )

    batch = load_management_batch(session_factory, batch.id)
    inst_id = normalize_deepcoin_swap_instrument(binding.symbol)
    for leg in batch.legs:
        row = decisions_by_leg_id[int(leg.id)]
        if row["action"] != "set_break_even" or leg.status != "planned":
            continue
        protection = row.get("protection")
        old_rows = list((protection or {}).get("row_snapshots") or [])
        old_order_ids = list((protection or {}).get("order_ids") or [])
        if not old_rows or [item.get("order_id") for item in old_rows] != old_order_ids:
            raise ManagementBatchExecutionError(
                "break_even_market_decision_protection_invalid"
            )
        execution_leg = replace(
            leg,
            old_tpsl={
                "order_ids": old_order_ids,
                "row_snapshots": old_rows,
            },
            planned_tpsl=(
                leg.planned_tpsl
                or {
                    "intent": "move_stop_to_break_even",
                    "stop_loss_text": None,
                }
            ),
        )
        new_rows = _adjusted_protection_rows(
            batch=replace(
                batch, effective_action="move_stop_to_break_even"
            ),
            leg=execution_leg,
            old_rows=old_rows,
        )
        if not transition_leg(
            session_factory,
            leg.id,
            expected_statuses={"planned"},
            new_status="reserved",
            transitioned_at=executed_at,
            request={
                "cancel_order_ids": old_order_ids,
                "expected_replacement_count": len(new_rows),
            },
        ):
            raise ManagementBatchExecutionError(
                f"management_protection_leg_reservation_conflict:{leg.id}"
            )
        try:
            for order_id in old_order_ids:
                _require_remediation_live_gate(session_factory, batch=batch)
                cancel_exact_position_sltp(
                    session_factory=session_factory,
                    deepcoin_client=deepcoin_client,
                    pos_id=str(leg.pos_id),
                    order_id=str(order_id),
                    instrument_id=inst_id,
                    idempotency_key=(
                        f"management:{batch.id}:{leg.id}:cancel:{order_id}"
                    ),
                    live_execution_gate=lambda: exact_position_write_gate(
                        session_factory, pos_id=str(leg.pos_id)
                    ),
                    now_provider=lambda: executed_at,
                )
                _mark_management_tpsl_ledger_cancelled(
                    session_factory,
                    batch=batch,
                    leg=leg,
                    order_id=str(order_id),
                    seen_at=executed_at,
                )
        except Exception as exc:
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="recovery_required",
                transitioned_at=executed_at,
                last_error={
                    "stage": "cancel_old_protection",
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            continue

        common = _protection_payload_common(
            binding=binding,
            position={"posId": str(leg.pos_id)},
            inst_id=inst_id,
        )
        created_order_ids: list[str] = []
        responses: list[dict[str, Any]] = []
        replacement_error: Exception | None = None
        for protection_row in new_rows:
            try:
                _require_remediation_live_gate(session_factory, batch=batch)
                payload = _protection_row_payload(
                    common=common, row=protection_row
                )
                response = submit_exact_position_sltp(
                    session_factory=session_factory,
                    deepcoin_client=deepcoin_client,
                    pos_id=str(leg.pos_id),
                    payload=payload,
                    idempotency_key=(
                        f"management:{batch.id}:{leg.id}:set:"
                        f"{protection_row['purpose']}:{len(responses)}"
                    ),
                    live_execution_gate=lambda: exact_position_write_gate(
                        session_factory, pos_id=str(leg.pos_id)
                    ),
                    now_provider=lambda: executed_at,
                    require_readback=True,
                )
                order_id = _extract_order_id(response)
                if not order_id:
                    raise ManagementBatchExecutionError(
                        "protection_replacement_missing_order_id"
                    )
                created_order_ids.append(order_id)
                responses.append(response)
                _record_management_tpsl_ledger_rows(
                    session_factory,
                    batch=batch,
                    binding=binding,
                    leg=leg,
                    inst_id=inst_id,
                    rows=[protection_row],
                    order_ids=[order_id],
                    seen_at=executed_at,
                )
            except Exception as exc:
                replacement_error = exc
                break
        if replacement_error is None:
            _record_management_tpsl_ledger_rows(
                session_factory,
                batch=batch,
                binding=binding,
                leg=leg,
                inst_id=inst_id,
                rows=new_rows,
                order_ids=created_order_ids,
                seen_at=executed_at,
            )
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="succeeded",
                transitioned_at=executed_at,
                response={"rows": responses},
                last_error=None,
            )
            continue

        if not isinstance(replacement_error, DeepcoinDefiniteRejection):
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="recovery_required",
                transitioned_at=executed_at,
                response={
                    "accepted_rows": responses,
                    "known_created_order_ids": created_order_ids,
                },
                last_error={
                    "stage": "replace_protection_outcome_unknown",
                    "type": type(replacement_error).__name__,
                    "message": str(replacement_error),
                },
            )
            continue

        restoration_error: Exception | None = None
        restore_responses: list[dict[str, Any]] = []
        try:
            for order_id in created_order_ids:
                _require_remediation_live_gate(session_factory, batch=batch)
                cancel_exact_position_sltp(
                    session_factory=session_factory,
                    deepcoin_client=deepcoin_client,
                    pos_id=str(leg.pos_id),
                    order_id=order_id,
                    instrument_id=inst_id,
                    idempotency_key=(
                        f"management:{batch.id}:{leg.id}:rollback_cancel:{order_id}"
                    ),
                    live_execution_gate=lambda: exact_position_write_gate(
                        session_factory, pos_id=str(leg.pos_id)
                    ),
                    now_provider=lambda: executed_at,
                )
                _mark_management_tpsl_ledger_cancelled(
                    session_factory,
                    batch=batch,
                    leg=leg,
                    order_id=order_id,
                    seen_at=executed_at,
                )
            for protection_row in old_rows:
                _require_remediation_live_gate(session_factory, batch=batch)
                payload = _protection_row_payload(
                    common=common, row=protection_row
                )
                response = submit_exact_position_sltp(
                    session_factory=session_factory,
                    deepcoin_client=deepcoin_client,
                    pos_id=str(leg.pos_id),
                    payload=payload,
                    idempotency_key=(
                        f"management:{batch.id}:{leg.id}:restore:"
                        f"{protection_row['purpose']}:{len(restore_responses)}"
                    ),
                    live_execution_gate=lambda: exact_position_write_gate(
                        session_factory, pos_id=str(leg.pos_id)
                    ),
                    now_provider=lambda: executed_at,
                    require_readback=True,
                )
                if not _extract_order_id(response):
                    raise ManagementBatchExecutionError(
                        "protection_restore_missing_order_id"
                    )
                restore_responses.append(response)
        except Exception as exc:
            restoration_error = exc
        if restoration_error is None:
            restored_order_ids = [
                str(_extract_order_id(response))
                for response in restore_responses
            ]
            _record_management_tpsl_ledger_rows(
                session_factory,
                batch=batch,
                binding=binding,
                leg=leg,
                inst_id=inst_id,
                rows=old_rows,
                order_ids=restored_order_ids,
                seen_at=executed_at,
                evidence_source="management_tpsl_restore",
            )
            leg_status = "restored"
        else:
            leg_status = "recovery_required"
        transition_leg(
            session_factory,
            leg.id,
            expected_statuses={"reserved"},
            new_status=leg_status,
            transitioned_at=executed_at,
            response={"restore_rows": restore_responses},
            last_error={
                "stage": "replace_protection",
                "type": type(replacement_error).__name__,
                "message": str(replacement_error),
                "restore_error": (
                    None
                    if restoration_error is None
                    else {
                        "type": type(restoration_error).__name__,
                        "message": str(restoration_error),
                    }
                ),
            },
        )

    completed = load_management_batch(session_factory, batch.id)
    statuses = {leg.status for leg in completed.legs}
    has_close = bool(close_decisions)
    if "recovery_required" in statuses and has_close:
        final_status = "reconciling"
        reason = "close_reconciliation_pending_protection_recovery"
    elif "recovery_required" in statuses:
        final_status = "recovery_required"
        reason = "break_even_market_recovery_required"
    elif "restored" in statuses or "failed" in statuses:
        final_status = "partial_failed"
        reason = "break_even_market_partial_failed"
    elif has_close:
        final_status = "reconciling"
        reason = (
            "one_or_more_close_submissions_unknown"
            if "submit_unknown" in statuses
            else "close_submissions_pending_reconciliation"
        )
    else:
        final_status = "succeeded"
        reason = "all_position_protection_replaced"
    if not transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"executing"},
        new_status=final_status,
        transitioned_at=executed_at,
        reason_code=reason,
    ):
        raise ManagementBatchExecutionError(
            "management_batch_finalization_conflict"
        )
    result_batch = load_management_batch(session_factory, batch.id)
    if final_status == "succeeded":
        _confirm_protection_lifecycle(
            session_factory,
            batch=result_batch,
            confirmed_at=executed_at,
        )
    return _result(result_batch, reason=reason)


def _recover_break_even_post_write_restart(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    recovered_at: datetime,
) -> dict[str, Any] | None:
    """Stop all writes once any durable leg proves a prior write boundary."""

    if all(leg.status == "planned" for leg in batch.legs):
        return None
    decision = load_break_even_market_decision(
        session_factory, batch_id=batch.id
    )
    if decision is None:
        raise BreakEvenMarketDecisionConflict(
            "break_even_market_decision_missing"
        )
    actions = {
        int(row["management_leg_id"]): str(row["action"])
        for row in decision.decisions
    }
    if set(actions) != {int(leg.id) for leg in batch.legs}:
        raise BreakEvenMarketDecisionConflict(
            "break_even_market_decision_leg_set_not_exact"
        )
    for leg in batch.legs:
        if leg.status != "reserved":
            continue
        action = actions[int(leg.id)]
        transition_leg(
            session_factory,
            leg.id,
            expected_statuses={"reserved"},
            new_status=(
                "submit_unknown"
                if action == "full_exit"
                else "recovery_required"
            ),
            transitioned_at=recovered_at,
            last_error={
                "reason": (
                    "reserved_submission_outcome_unknown"
                    if action == "full_exit"
                    else "reserved_protection_outcome_unknown"
                )
            },
        )
    recovered = load_management_batch(session_factory, batch.id)
    has_close_evidence = any(
        actions[int(leg.id)] == "full_exit"
        and leg.status
        in {"submitted", "submit_unknown", "partial", "confirmed"}
        for leg in recovered.legs
    )
    statuses = {leg.status for leg in recovered.legs}
    if has_close_evidence:
        final_status = "reconciling"
        reason = "break_even_market_restart_requires_reconciliation"
    elif all(
        actions[int(leg.id)] == "set_break_even"
        and leg.status == "succeeded"
        for leg in recovered.legs
    ):
        final_status = "succeeded"
        reason = "all_position_protection_replaced"
    elif "recovery_required" in statuses:
        final_status = "recovery_required"
        reason = "break_even_market_post_write_recovery_required"
    elif "restored" in statuses or "failed" in statuses:
        final_status = "partial_failed"
        reason = "break_even_market_partial_failed"
    else:
        final_status = "recovery_required"
        reason = "break_even_market_post_write_recovery_required"
    if not transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"executing"},
        new_status=final_status,
        transitioned_at=recovered_at,
        reason_code=reason,
    ):
        raise ManagementBatchExecutionError(
            "management_batch_restart_handoff_conflict"
        )
    result_batch = load_management_batch(session_factory, batch.id)
    if final_status == "succeeded":
        _confirm_protection_lifecycle(
            session_factory,
            batch=result_batch,
            confirmed_at=recovered_at,
        )
    return _result(result_batch, reason=reason)


@serialized_position_authority_mutation
def execute_management_batch(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Submit close legs by durable batch ID; exchange truth closes positions later."""

    now = executed_at or datetime.now(UTC)
    batch = load_management_batch(session_factory, int(batch_id))
    if batch.management_contract_json or batch.management_contract_fingerprint:
        raise ManagementBatchExecutionError(
            "management_contract_requires_component_executor"
        )
    if batch.status in {"ready", "protection_ready", "executing"}:
        try:
            _require_remediation_live_gate(session_factory, batch=batch)
            _require_remediation_confirmation_snapshot(
                batch=batch,
                deepcoin_client=deepcoin_client,
            )
        except ManagementBatchExecutionError:
            failure_status = (
                "recovery_required"
                if batch.status == "executing"
                else "blocked"
            )
            transition_batch(
                session_factory,
                batch.id,
                expected_statuses={batch.status},
                new_status=failure_status,
                transitioned_at=now,
                reason_code="remediation_confirmation_expired",
            )
            raise
    if batch.effective_action == "break_even_by_market":
        try:
            return _execute_break_even_by_market_batch(
                session_factory,
                batch=batch,
                deepcoin_client=deepcoin_client,
                executed_at=now,
            )
        except BreakEvenMarketDecisionConflict:
            transition_batch(
                session_factory,
                batch.id,
                expected_statuses={"executing"},
                new_status="recovery_required",
                transitioned_at=now,
                reason_code="break_even_market_decision_missing_or_invalid",
            )
            raise
    if batch.effective_action in _PROTECTION_ACTIONS or (
        batch.effective_action == "partial_then_break_even"
        and (
            batch.status == "protection_ready"
            or batch.reason_code == "protection_phase_executing"
            or any(
                leg.status in _PROTECTION_PHASE_LEG_STATES for leg in batch.legs
            )
        )
    ):
        return _execute_protection_batch(
            session_factory,
            batch=batch,
            deepcoin_client=deepcoin_client,
            executed_at=now,
        )
    if batch.effective_action not in _CLOSE_ACTIONS:
        raise ManagementBatchExecutionError(
            f"batch_action_not_close:{batch.effective_action}"
        )
    if not batch.legs or any(leg.planned_close_size is None for leg in batch.legs):
        raise ManagementBatchExecutionError("batch_close_plan_incomplete")

    if batch.status == "ready":
        claimed = claim_ready_batch(session_factory, batch.id, claimed_at=now)
        if claimed is None:
            batch = load_management_batch(session_factory, batch.id)
        else:
            batch = claimed
    if batch.status == "reconciling":
        return _result(batch, reason="batch_already_reconciling")
    if batch.status != "executing":
        raise ManagementBatchExecutionError(f"batch_not_executable:{batch.status}")
    binding = _load_exact_binding(session_factory, batch)
    if batch.effective_action not in {"full_close", "full_exit"}:
        _require_exact_entry_legs(session_factory, batch)
    try:
        _require_exact_protection_recovery_full_exit_bypass(batch)
        _require_exact_risk_reduction_protection_recovery_marker(batch)
        _require_remediation_live_gate(session_factory, batch=batch)
        if batch.effective_action in {"full_close", "full_exit"}:
            _require_exact_entry_legs(session_factory, batch)
        _cancel_deferred_entry_legs(
            session_factory,
            batch=batch,
            binding=binding,
            deepcoin_client=deepcoin_client,
            cancelled_at=now,
        )
    except Exception as exc:
        if isinstance(exc, DeferredEntryIdentityDriftError):
            _persist_deferred_cancel_diagnostics(
                session_factory,
                batch=batch,
                binding=binding,
                diagnostics=exc.diagnostics,
                created_at=now,
            )
        if not transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="recovery_required",
            transitioned_at=now,
            reason_code=(
                "close_final_preflight_failed"
                if str(exc) in {
                    "close_final_preflight_protection_recovery_bypass_invalid",
                    "close_final_preflight_protection_recovery_marker_invalid",
                }
                else (
                    "deferred_entry_cancel_race_detected"
                    if isinstance(exc, DeepcoinDefiniteRejection)
                    else "deferred_entry_cancel_preflight_failed"
                )
            ),
        ):
            raise ManagementBatchExecutionError(
                "management_batch_deferred_cancel_transition_conflict"
            )
        return _result(
            load_management_batch(session_factory, batch.id),
            reason=(
                "close_final_preflight_failed"
                if str(exc) in {
                    "close_final_preflight_protection_recovery_bypass_invalid",
                    "close_final_preflight_protection_recovery_marker_invalid",
                }
                else (
                    "deferred_entry_cancel_race_detected"
                    if isinstance(exc, DeepcoinDefiniteRejection)
                    else "deferred_entry_cancel_preflight_failed"
                )
            ),
        )
    try:
        _require_fresh_close_write_boundary(
            session_factory,
            batch=batch,
            binding=binding,
            deepcoin_client=deepcoin_client,
        )
        _cancel_exact_risk_reduction_protection_before_close(
            session_factory,
            batch=batch,
            binding=binding,
            deepcoin_client=deepcoin_client,
            cancelled_at=now,
        )
    except Exception as exc:
        if not transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="recovery_required",
            transitioned_at=now,
            reason_code=(
                "close_final_preflight_failed"
                if isinstance(exc, ManagementBatchExecutionError)
                else "close_final_preflight_unavailable"
            ),
        ):
            raise ManagementBatchExecutionError(
                "management_batch_final_preflight_transition_conflict"
            ) from exc
        return _result(
            load_management_batch(session_factory, batch.id),
            reason="close_final_preflight_failed",
        )
    # A durable reservation cannot distinguish a crash before the call from a
    # crash after a successful call. Never retry it automatically.
    for leg in batch.legs:
        if leg.status == "reserved":
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="submit_unknown",
                transitioned_at=now,
                last_error={"reason": "reserved_submission_outcome_unknown"},
            )

    batch = load_management_batch(session_factory, batch.id)
    for leg in batch.legs:
        if leg.status != "planned":
            continue
        client_order_id = build_management_client_order_id(
            batch_id=batch.id, leg_id=leg.id
        )
        request = _close_payload(
            binding=binding,
            pos_id=leg.pos_id,
            close_size=str(leg.planned_close_size),
            client_order_id=client_order_id,
        )
        reserved = transition_leg(
            session_factory,
            leg.id,
            expected_statuses={"planned"},
            new_status="reserved",
            transitioned_at=now,
            client_order_id=client_order_id,
            request=request,
            last_error=None,
        )
        if not reserved:
            raise ManagementBatchExecutionError(
                f"management_leg_reservation_conflict:{leg.id}"
            )
        try:
            _require_remediation_live_gate(session_factory, batch=batch)
            response = close_exact_position(
                session_factory=session_factory,
                deepcoin_client=deepcoin_client,
                pos_id=str(leg.pos_id),
                instrument_id=str(request["instId"]),
                size=str(request["sz"]),
                client_order_id=client_order_id,
                idempotency_key=f"management:{batch.id}:{leg.id}:close:{client_order_id}",
                live_execution_gate=lambda: exact_position_write_gate(
                    session_factory, pos_id=str(leg.pos_id)
                ),
                now_provider=lambda: now,
            )
        except ManagementBatchExecutionError as exc:
            if str(exc) != "remediation_live_management_gate_closed":
                raise
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="recovery_required",
                transitioned_at=now,
                last_error={"type": type(exc).__name__, "message": str(exc)},
            )
            break
        except DeepcoinDefiniteRejection as exc:
            failed_leg_status = "failed"
            restore_error = _restore_precancelled_protection_for_rejected_close(
                session_factory,
                batch=batch,
                binding=binding,
                leg=leg,
                deepcoin_client=deepcoin_client,
            )
            if restore_error is not None:
                failed_leg_status = "recovery_required"
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status=failed_leg_status,
                transitioned_at=now,
                request={
                    **request,
                    "recovery_phase": (
                        "rejected_close_restore"
                        if restore_error is not None
                        else None
                    ),
                    "expected_replacement_count": len(
                        (leg.old_tpsl or {}).get("row_snapshots") or []
                    ),
                },
                last_error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "protection_restore_error": restore_error,
                },
            )
            _record_leg_event(
                session_factory,
                batch=batch,
                binding=binding,
                leg_id=leg.id,
                pos_id=leg.pos_id,
                client_order_id=client_order_id,
                request=request,
                response=None,
                order_id=None,
                status="failed",
                reason="submission_rejected",
                created_at=now,
            )
            continue
        except Exception as exc:
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="submit_unknown",
                transitioned_at=now,
                last_error={"type": type(exc).__name__, "message": str(exc)},
            )
            _record_leg_event(
                session_factory,
                batch=batch,
                binding=binding,
                leg_id=leg.id,
                pos_id=leg.pos_id,
                client_order_id=client_order_id,
                request=request,
                response=None,
                order_id=None,
                status="submit_unknown",
                reason="submission_outcome_unknown",
                created_at=now,
            )
            continue

        order_id = _extract_order_id(response)
        if not order_id:
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="submit_unknown",
                transitioned_at=now,
                response=response,
                last_error={"reason": "submission_response_missing_order_id"},
            )
            event_status = "submit_unknown"
            event_reason = "submission_response_missing_order_id"
        else:
            persisted = transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="submitted",
                transitioned_at=now,
                exchange_order_id=order_id,
                response=response,
                last_error=None,
            )
            if not persisted:
                raise ManagementBatchExecutionError(
                    f"management_leg_response_persist_conflict:{leg.id}"
                )
            event_status = "submitted"
            event_reason = "management_close_submitted"
        _record_leg_event(
            session_factory,
            batch=batch,
            binding=binding,
            leg_id=leg.id,
            pos_id=leg.pos_id,
            client_order_id=client_order_id,
            request=request,
            response=response,
            order_id=order_id,
            status=event_status,
            reason=event_reason,
            created_at=now,
        )

    batch = load_management_batch(session_factory, batch.id)
    statuses = {leg.status for leg in batch.legs}
    if "recovery_required" in statuses:
        final_status = "recovery_required"
        reason = "close_rejected_and_protection_restore_failed"
    elif "failed" in statuses:
        final_status = "partial_failed"
        reason = "one_or_more_close_submissions_failed"
    else:
        final_status = "reconciling"
        reason = (
            "one_or_more_close_submissions_unknown"
            if "submit_unknown" in statuses
            else "close_submissions_pending_reconciliation"
        )
    if not transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"executing"},
        new_status=final_status,
        transitioned_at=now,
        reason_code=reason,
    ):
        raise ManagementBatchExecutionError("management_batch_finalization_conflict")
    return _result(load_management_batch(session_factory, batch.id), reason=reason)


def _execute_protection_batch(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
    executed_at: datetime,
) -> dict[str, Any]:
    """Replace every exact position's complete TPSL set, compensating per leg."""

    if not batch.legs or any(
        not leg.old_tpsl or not leg.planned_tpsl for leg in batch.legs
    ):
        raise ManagementBatchExecutionError("batch_protection_plan_incomplete")
    if batch.status == "ready":
        claimed = claim_ready_batch(session_factory, batch.id, claimed_at=executed_at)
        batch = claimed or load_management_batch(session_factory, batch.id)
    elif batch.status == "protection_ready":
        if not transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"protection_ready"},
            new_status="executing",
            transitioned_at=executed_at,
            reason_code="protection_phase_executing",
        ):
            raise ManagementBatchExecutionError(
                "management_protection_phase_claim_conflict"
            )
        batch = load_management_batch(session_factory, batch.id)
    if batch.status != "executing":
        raise ManagementBatchExecutionError(f"batch_not_executable:{batch.status}")
    durable_statuses = {leg.status for leg in batch.legs}
    if "recovery_required" in durable_statuses:
        if not transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="recovery_required",
            transitioned_at=executed_at,
            reason_code="protection_recovery_required",
        ):
            raise ManagementBatchExecutionError(
                "management_protection_recovery_finalization_conflict"
            )
        return _result(
            load_management_batch(session_factory, batch.id),
            reason="protection_recovery_required",
        )
    if "restored" in durable_statuses:
        if not transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="partial_failed",
            transitioned_at=executed_at,
            reason_code="protection_replacement_failed_and_restored",
        ):
            raise ManagementBatchExecutionError(
                "management_protection_restore_finalization_conflict"
            )
        return _result(
            load_management_batch(session_factory, batch.id),
            reason="protection_replacement_failed_and_restored",
        )
    if durable_statuses == {"succeeded"}:
        if not transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="succeeded",
            transitioned_at=executed_at,
            reason_code="all_position_protection_replaced",
        ):
            raise ManagementBatchExecutionError(
                "management_protection_success_finalization_conflict"
            )
        succeeded_batch = load_management_batch(session_factory, batch.id)
        _confirm_protection_lifecycle(
            session_factory, batch=succeeded_batch, confirmed_at=executed_at
        )
        return _result(
            succeeded_batch,
            reason="all_position_protection_replaced",
        )
    if any(leg.status == "reserved" for leg in batch.legs):
        for leg in batch.legs:
            if leg.status == "reserved":
                transition_leg(
                    session_factory,
                    leg.id,
                    expected_statuses={"reserved"},
                    new_status="recovery_required",
                    transitioned_at=executed_at,
                    last_error={"reason": "reserved_protection_outcome_unknown"},
                )
        transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="recovery_required",
            transitioned_at=executed_at,
            reason_code="reserved_protection_outcome_unknown",
        )
        return _result(
            load_management_batch(session_factory, batch.id),
            reason="reserved_protection_outcome_unknown",
        )

    try:
        binding = _load_exact_binding(session_factory, batch)
        _require_exact_entry_legs(session_factory, batch)
        inst_id = normalize_deepcoin_swap_instrument(binding.symbol)
        live_positions = list(deepcoin_client.list_positions(inst_id=inst_id))
        positions_by_id = _preflight_exact_protection_positions(
            session_factory=session_factory,
            batch=batch,
            binding=binding,
            live_positions=live_positions,
            inst_id=inst_id,
        )
        # This is deliberately the final read before the first exchange cancel.
        pending = list(deepcoin_client.list_trigger_orders_pending(inst_id=inst_id))
        recovery_precancelled = _risk_reduction_recovery_enabled(batch)
        pending_order_ids = {
            str(order_id)
            for row in pending
            if (
                order_id := _first_text(row, "ordId", "orderId", "order_id")
            )
        }
        expected_order_ids = {
            str(order_id)
            for leg in batch.legs
            for order_id in (leg.old_tpsl or {}).get("order_ids") or []
        }
        old_orders_still_pending = expected_order_ids & pending_order_ids
        if recovery_precancelled and old_orders_still_pending not in (
            set(),
            expected_order_ids,
        ):
            raise ManagementBatchExecutionError(
                "protection_precancel_exchange_state_mixed"
            )
        if not recovery_precancelled or old_orders_still_pending:
            current_protection_rows_by_pos_id = _preflight_exact_protection_rows(
                session_factory=session_factory,
                batch=batch,
                live_positions=live_positions,
                pending=pending,
            )
            skip_old_cancel = False
        else:
            current_protection_rows_by_pos_id = {
                str(leg.pos_id): _precancelled_protection_rows(
                    session_factory,
                    batch=batch,
                    leg=leg,
                )
                for leg in batch.legs
            }
            skip_old_cancel = True
        if batch.effective_action == "adjust_stop_loss":
            _require_explicit_stop_write_boundary(
                batch=batch,
                binding=binding,
                deepcoin_client=deepcoin_client,
                inst_id=inst_id,
                current_rows_by_pos_id=current_protection_rows_by_pos_id,
            )
        prepared_legs = []
        for leg in batch.legs:
            old_rows = list(current_protection_rows_by_pos_id[leg.pos_id])
            replacement_rows = old_rows
            if batch.effective_action == "partial_then_break_even":
                replacement_rows = _resize_protection_rows_for_remaining_position(
                    batch=batch,
                    leg=leg,
                    rows=normalize_protection_snapshot_rows(
                        (leg.old_tpsl or {}).get("row_snapshots") or []
                    ),
                )
            prepared_legs.append(
                (
                    leg,
                    old_rows,
                    _adjusted_protection_rows(
                        batch=batch, leg=leg, old_rows=replacement_rows
                    ),
                    _protection_payload_common(
                        binding=binding,
                        position=positions_by_id[leg.pos_id],
                        inst_id=inst_id,
                    ),
                    skip_old_cancel,
                )
            )
    except ManagementBatchExecutionError:
        transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="blocked",
            transitioned_at=executed_at,
            reason_code="protection_preflight_failed",
        )
        raise
    except Exception as exc:
        transition_batch(
            session_factory,
            batch.id,
            expected_statuses={"executing"},
            new_status="blocked",
            transitioned_at=executed_at,
            reason_code="protection_preflight_unavailable",
        )
        raise ManagementBatchExecutionError(
            f"protection_preflight_unavailable:{type(exc).__name__}"
        ) from exc

    failed_status: str | None = None
    for leg, old_rows, new_rows, common, skip_old_cancel in prepared_legs:
        if leg.status == "succeeded":
            continue
        expected_leg_statuses = (
            {"confirmed"}
            if batch.effective_action == "partial_then_break_even"
            else {"planned"}
        )
        if not transition_leg(
            session_factory,
            leg.id,
            expected_statuses=expected_leg_statuses,
            new_status="reserved",
            transitioned_at=executed_at,
            request={
                "cancel_order_ids": [row["order_id"] for row in old_rows],
                "expected_replacement_count": len(new_rows),
            },
        ):
            raise ManagementBatchExecutionError(
                f"management_protection_leg_reservation_conflict:{leg.id}"
            )

        if not skip_old_cancel:
            try:
                for row in old_rows:
                    _require_remediation_live_gate(
                        session_factory, batch=batch
                    )
                    cancel_exact_position_sltp(
                        session_factory=session_factory,
                        deepcoin_client=deepcoin_client,
                        pos_id=str(leg.pos_id),
                        order_id=str(row["order_id"]),
                        instrument_id=inst_id,
                        idempotency_key=(
                            f"management:{batch.id}:{leg.id}:cancel:"
                            f"{row['order_id']}"
                        ),
                        live_execution_gate=lambda: exact_position_write_gate(
                            session_factory, pos_id=str(leg.pos_id)
                        ),
                        now_provider=lambda: executed_at,
                    )
                    _mark_management_tpsl_ledger_cancelled(
                        session_factory,
                        batch=batch,
                        leg=leg,
                        order_id=str(row["order_id"]),
                        seen_at=executed_at,
                    )
            except Exception as exc:
                transition_leg(
                    session_factory,
                    leg.id,
                    expected_statuses={"reserved"},
                    new_status="recovery_required",
                    transitioned_at=executed_at,
                    last_error={
                        "stage": "cancel_old_protection",
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                failed_status = "recovery_required"
                continue

        created_order_ids: list[str] = []
        replacement_responses: list[dict[str, Any]] = []
        replacement_error: Exception | None = None
        for row in new_rows:
            payload = _protection_row_payload(common=common, row=row)
            try:
                _require_remediation_live_gate(session_factory, batch=batch)
                response = submit_exact_position_sltp(
                    session_factory=session_factory,
                    deepcoin_client=deepcoin_client,
                    pos_id=str(leg.pos_id),
                    payload=payload,
                    idempotency_key=(
                        f"management:{batch.id}:{leg.id}:set:"
                        f"{row['purpose']}:{len(replacement_responses)}"
                    ),
                    live_execution_gate=lambda: exact_position_write_gate(
                        session_factory, pos_id=str(leg.pos_id)
                    ),
                    now_provider=lambda: executed_at,
                    require_readback=True,
                )
                order_id = _extract_order_id(response)
                if not order_id:
                    raise ManagementBatchExecutionError(
                        "protection_replacement_missing_order_id"
                    )
                created_order_ids.append(order_id)
                replacement_responses.append(response)
                _record_management_tpsl_ledger_rows(
                    session_factory,
                    batch=batch,
                    binding=binding,
                    leg=leg,
                    inst_id=inst_id,
                    rows=[row],
                    order_ids=[order_id],
                    seen_at=executed_at,
                )
            except Exception as exc:
                replacement_error = exc
                break
        if replacement_error is None:
            _record_management_tpsl_ledger_rows(
                session_factory,
                batch=batch,
                binding=binding,
                leg=leg,
                inst_id=inst_id,
                rows=new_rows,
                order_ids=created_order_ids,
                seen_at=executed_at,
            )
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="succeeded",
                transitioned_at=executed_at,
                response={"rows": replacement_responses},
                last_error=None,
            )
            continue

        if not isinstance(replacement_error, DeepcoinDefiniteRejection):
            transition_leg(
                session_factory,
                leg.id,
                expected_statuses={"reserved"},
                new_status="recovery_required",
                transitioned_at=executed_at,
                response={"rows": replacement_responses},
                last_error={
                    "stage": "replace_protection_outcome_unknown",
                    "type": type(replacement_error).__name__,
                    "message": str(replacement_error),
                    "created_order_ids": created_order_ids,
                },
            )
            failed_status = "recovery_required"
            continue

        restoration_error: Exception | None = None
        try:
            for order_id in created_order_ids:
                _require_remediation_live_gate(session_factory, batch=batch)
                cancel_exact_position_sltp(
                    session_factory=session_factory,
                    deepcoin_client=deepcoin_client,
                    pos_id=str(leg.pos_id),
                    order_id=order_id,
                    instrument_id=inst_id,
                    idempotency_key=(
                        f"management:{batch.id}:{leg.id}:rollback_cancel:{order_id}"
                    ),
                    live_execution_gate=lambda: exact_position_write_gate(
                        session_factory, pos_id=str(leg.pos_id)
                    ),
                    now_provider=lambda: executed_at,
                )
                _mark_management_tpsl_ledger_cancelled(
                    session_factory,
                    batch=batch,
                    leg=leg,
                    order_id=order_id,
                    seen_at=executed_at,
                )
            restore_responses = []
            for row in old_rows:
                _require_remediation_live_gate(session_factory, batch=batch)
                payload = _protection_row_payload(common=common, row=row)
                response = submit_exact_position_sltp(
                    session_factory=session_factory,
                    deepcoin_client=deepcoin_client,
                    pos_id=str(leg.pos_id),
                    payload=payload,
                    idempotency_key=(
                        f"management:{batch.id}:{leg.id}:restore:"
                        f"{row['purpose']}:{len(restore_responses)}"
                    ),
                    live_execution_gate=lambda: exact_position_write_gate(
                        session_factory, pos_id=str(leg.pos_id)
                    ),
                    now_provider=lambda: executed_at,
                    require_readback=True,
                )
                if not _extract_order_id(response):
                    raise ManagementBatchExecutionError(
                        "protection_restore_missing_order_id"
                    )
                restore_responses.append(response)
        except Exception as exc:
            restoration_error = exc
            restore_responses = []

        if restoration_error is None:
            restored_order_ids = [
                order_id
                for response in restore_responses
                if (order_id := _extract_order_id(response))
            ]
            _record_management_tpsl_ledger_rows(
                session_factory,
                batch=batch,
                binding=binding,
                leg=leg,
                inst_id=inst_id,
                rows=old_rows,
                order_ids=restored_order_ids,
                seen_at=executed_at,
                evidence_source="management_tpsl_restore",
            )
            leg_status = "restored"
            if failed_status is None:
                failed_status = "partial_failed"
        else:
            leg_status = "recovery_required"
            failed_status = "recovery_required"
        transition_leg(
            session_factory,
            leg.id,
            expected_statuses={"reserved"},
            new_status=leg_status,
            transitioned_at=executed_at,
            response={"restore_rows": restore_responses},
            last_error={
                "stage": "replace_protection",
                "type": type(replacement_error).__name__,
                "message": str(replacement_error),
                "restore_error": (
                    None
                    if restoration_error is None
                    else {
                        "type": type(restoration_error).__name__,
                        "message": str(restoration_error),
                    }
                ),
            },
        )
        if leg_status == "recovery_required":
            failed_status = "recovery_required"

    final_status = failed_status or "succeeded"
    reason = {
        "succeeded": "all_position_protection_replaced",
        "partial_failed": "protection_replacement_failed_and_restored",
        "recovery_required": "protection_recovery_required",
    }[final_status]
    if not transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"executing"},
        new_status=final_status,
        transitioned_at=executed_at,
        reason_code=reason,
    ):
        raise ManagementBatchExecutionError("management_batch_finalization_conflict")
    completed_batch = load_management_batch(session_factory, batch.id)
    if final_status == "succeeded":
        _confirm_protection_lifecycle(
            session_factory, batch=completed_batch, confirmed_at=executed_at
        )
    return _result(completed_batch, reason=reason)


def _confirm_protection_lifecycle(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    confirmed_at: datetime,
) -> None:
    """Promote protection intent only after every replacement was accepted."""

    requested_stops = {
        str(leg.planned_tpsl.get("stop_loss_text")).strip()
        for leg in batch.legs
        if isinstance(leg.planned_tpsl, dict)
        and leg.planned_tpsl.get("stop_loss_text") not in {None, ""}
    }
    confirmed_stop: float | None = None
    if len(requested_stops) == 1:
        try:
            parsed = Decimal(next(iter(requested_stops)))
        except InvalidOperation:
            parsed = Decimal("NaN")
        if parsed.is_finite() and parsed > 0:
            confirmed_stop = float(parsed)

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        raw = session.get(RawMessage, batch.raw_message_id)
        if lifecycle is None or raw is None:
            raise ManagementBatchExecutionError(
                "management_protection_confirmation_identity_missing"
            )
        if confirmed_stop is not None:
            lifecycle.stop_loss = confirmed_stop
        lifecycle.management_signal_message_id = int(raw.message_id)
        lifecycle.management_action = "protection_update_confirmed"
        lifecycle.management_note = (
            "Deepcoin accepted every planned position-protection replacement."
        )
        lifecycle.updated_at = confirmed_at
        session.commit()


def _preflight_exact_protection_positions(
    *,
    session_factory: sessionmaker,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    live_positions: list[dict[str, Any]],
    inst_id: str,
    after_partial_close: bool = True,
) -> dict[str, dict[str, Any]]:
    positions_by_id = _preflight_exact_position_identity(
        batch=batch,
        binding=binding,
        live_positions=live_positions,
        inst_id=inst_id,
        error_prefix="protection_preflight",
        ignored_owned_pos_ids=_other_exact_strategy_position_ids(
            session_factory, batch=batch, binding=binding
        ),
    )
    for leg in batch.legs:
        position = positions_by_id[leg.pos_id]
        expected_size = leg.preflight_size
        if batch.effective_action == "partial_then_break_even" and after_partial_close:
            try:
                expected_size = str(
                    Decimal(str(leg.preflight_size))
                    - Decimal(str(leg.planned_close_size))
                )
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ManagementBatchExecutionError(
                    "protection_preflight_position_economics_drift"
                ) from exc
        if not _decimal_equal(
            _first_text(position, "pos", "size", "sz"), expected_size
        ) or not _decimal_equal(
            _first_text(position, "avgPx", "avgPrice", "avg_entry_price"),
            leg.avg_entry_price,
        ):
            raise ManagementBatchExecutionError(
                "protection_preflight_position_economics_drift"
            )
    return positions_by_id


def _require_fresh_close_write_boundary(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    deepcoin_client: DeepcoinTradingClientProtocol,
) -> None:
    """Re-read exact exchange positions at the shared close write boundary."""

    inst_id = normalize_deepcoin_swap_instrument(binding.symbol)
    live_positions = list(deepcoin_client.list_positions(inst_id=inst_id))
    positions = _preflight_exact_position_identity(
        batch=batch,
        binding=binding,
        live_positions=live_positions,
        inst_id=inst_id,
        error_prefix="close_final_preflight",
        ignored_owned_pos_ids=_other_exact_strategy_position_ids(
            session_factory, batch=batch, binding=binding
        ),
    )
    contract_spec = (
        batch.target_snapshot.get("contract_spec")
        if isinstance(batch.target_snapshot, dict)
        else None
    )
    if not isinstance(contract_spec, dict):
        raise ManagementBatchExecutionError(
            "close_final_preflight_contract_spec_missing"
        )
    try:
        contract_step = Decimal(str(contract_spec["quantity_step"]))
        min_quantity = Decimal(str(contract_spec["min_quantity"]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ManagementBatchExecutionError(
            "close_final_preflight_contract_spec_invalid"
        ) from exc
    if (
        not contract_step.is_finite()
        or contract_step <= 0
        or not min_quantity.is_finite()
        or min_quantity <= 0
    ):
        raise ManagementBatchExecutionError(
            "close_final_preflight_contract_spec_invalid"
        )
    for leg in batch.legs:
        try:
            planned_size = Decimal(str(leg.planned_close_size))
            persisted_step = Decimal(str(leg.quantity_step))
            preflight_size = Decimal(str(leg.preflight_size))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ManagementBatchExecutionError(
                "close_final_preflight_quantity_invalid"
            ) from exc
        if (
            not planned_size.is_finite()
            or not preflight_size.is_finite()
            or preflight_size <= 0
            or planned_size < min_quantity
            or planned_size > preflight_size
            or persisted_step != contract_step
            or planned_size % contract_step != 0
        ):
            raise ManagementBatchExecutionError(
                "close_final_preflight_quantity_invalid"
            )
        if not _decimal_equal(
            _first_text(positions[leg.pos_id], "pos", "size", "sz"),
            leg.preflight_size,
        ):
            raise ManagementBatchExecutionError(
                "close_final_preflight_position_size_drift"
            )


def _require_exact_protection_recovery_full_exit_bypass(
    batch: ManagementBatchRecord,
) -> None:
    if batch.reason_code != "protection_recovery_bypassed_for_full_exit":
        return
    snapshot = batch.target_snapshot
    marker = (
        snapshot.get("protection_recovery_bypass")
        if isinstance(snapshot, dict)
        else None
    )
    expected_pos_ids = sorted(str(leg.pos_id) for leg in batch.legs)
    if not isinstance(marker, dict) or (
        marker.get("version") != 1
        or marker.get("reason") != "protection_recovery_required"
        or marker.get("allowed_action") != "full_exit"
        or batch.effective_action != "full_exit"
        or marker.get("target_lifecycle_id") != batch.target_lifecycle_id
        or marker.get("execution_binding_id") != batch.execution_binding_id
        or marker.get("target_pos_ids") != expected_pos_ids
    ):
        raise ManagementBatchExecutionError(
            "close_final_preflight_protection_recovery_bypass_invalid"
        )


def _require_exact_risk_reduction_protection_recovery_marker(
    batch: ManagementBatchRecord,
) -> None:
    snapshot = batch.target_snapshot
    marker = (
        snapshot.get("protection_recovery")
        if isinstance(snapshot, dict)
        else None
    )
    if marker is None:
        return
    positions = marker.get("positions") if isinstance(marker, dict) else None
    expected = [
        {
            "pos_id": str(leg.pos_id),
            "execution_order_leg_id": int(leg.execution_order_leg_id),
            "owned_order_ids": list((leg.old_tpsl or {}).get("order_ids") or []),
        }
        for leg in batch.legs
    ]
    all_order_ids = [
        str(order_id)
        for position in expected
        for order_id in position["owned_order_ids"]
    ]
    if (
        batch.effective_action != "partial_then_break_even"
        or not isinstance(marker, dict)
        or marker.get("version") != 1
        or marker.get("mode") != "replace_after_reduction"
        or positions != expected
        or any(not position["owned_order_ids"] for position in expected)
        or len(all_order_ids) != len(set(all_order_ids))
    ):
        raise ManagementBatchExecutionError(
            "close_final_preflight_protection_recovery_marker_invalid"
        )


def _risk_reduction_recovery_enabled(batch: ManagementBatchRecord) -> bool:
    snapshot = batch.target_snapshot
    return isinstance(snapshot, dict) and isinstance(
        snapshot.get("protection_recovery"), dict
    )


def _protection_cancel_reservation_key(
    *, batch_id: int, leg_id: int, order_id: str
) -> str:
    digest = hashlib.sha256(str(order_id).encode("utf-8")).hexdigest()[:12]
    return f"pmpr:{batch_id}:{leg_id}:{digest}"


def _cancel_exact_risk_reduction_protection_before_close(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    deepcoin_client: DeepcoinTradingClientProtocol,
    cancelled_at: datetime,
) -> None:
    """Durably reserve and cancel the exact old TPSL set before reducing risk."""

    if not _risk_reduction_recovery_enabled(batch):
        return
    _require_exact_risk_reduction_protection_recovery_marker(batch)
    inst_id = normalize_deepcoin_swap_instrument(binding.symbol)
    live_positions = list(deepcoin_client.list_positions(inst_id=inst_id))
    _preflight_exact_protection_positions(
        session_factory=session_factory,
        batch=batch,
        binding=binding,
        live_positions=live_positions,
        inst_id=inst_id,
        after_partial_close=False,
    )
    pending = list(deepcoin_client.list_trigger_orders_pending(inst_id=inst_id))
    rows_by_pos_id = _preflight_exact_protection_rows(
        session_factory=session_factory,
        batch=batch,
        live_positions=live_positions,
        pending=pending,
    )
    for leg in batch.legs:
        for row in rows_by_pos_id[str(leg.pos_id)]:
            order_id = str(row["order_id"])
            reservation_key = _protection_cancel_reservation_key(
                batch_id=batch.id, leg_id=leg.id, order_id=order_id
            )
            with session_factory() as session:
                existing = (
                    session.query(ExecutionEvent)
                    .filter(
                        ExecutionEvent.action
                        == "strategy_management_protection_precancel",
                        ExecutionEvent.client_order_id == reservation_key,
                    )
                    .order_by(ExecutionEvent.id.desc())
                    .first()
                )
                if existing is not None:
                    raise ManagementBatchExecutionError(
                        "protection_precancel_reservation_already_exists"
                    )
                record_execution_event(
                    session_factory,
                    ExecutionEventRecord(
                        execution_binding_id=binding.id,
                        strategy_instance_id=batch.strategy_instance_id,
                        action="strategy_management_protection_precancel",
                        status="reserved",
                        symbol=binding.symbol,
                        side=binding.side,
                        order_id=order_id,
                        client_order_id=reservation_key,
                        pos_id=str(leg.pos_id),
                        reason="reserved_before_exact_protection_cancel",
                        before={
                            "management_batch_id": batch.id,
                            "management_leg_id": leg.id,
                            "protection_row": row,
                        },
                        created_at=cancelled_at,
                    ),
                    session=session,
                )
                session.commit()
            _require_remediation_live_gate(session_factory, batch=batch)
            response = cancel_exact_position_sltp(
                session_factory=session_factory,
                deepcoin_client=deepcoin_client,
                pos_id=str(leg.pos_id),
                order_id=order_id,
                instrument_id=inst_id,
                idempotency_key=(
                    f"management:{batch.id}:{leg.id}:precancel:{order_id}"
                ),
                live_execution_gate=lambda: exact_position_write_gate(
                    session_factory, pos_id=str(leg.pos_id)
                ),
                now_provider=lambda: cancelled_at,
            )
            record_execution_event(
                session_factory,
                ExecutionEventRecord(
                    execution_binding_id=binding.id,
                    strategy_instance_id=batch.strategy_instance_id,
                    action="strategy_management_protection_precancel",
                    status="succeeded",
                    symbol=binding.symbol,
                    side=binding.side,
                    order_id=order_id,
                    client_order_id=reservation_key,
                    pos_id=str(leg.pos_id),
                    reason="exact_protection_cancelled_before_close",
                    before={
                        "management_batch_id": batch.id,
                        "management_leg_id": leg.id,
                        "protection_row": row,
                    },
                    response=response,
                    created_at=cancelled_at,
                ),
            )
            with session_factory() as session:
                upsert_protection_ledger_row(
                    session,
                    venue=binding.venue,
                    execution_binding_id=batch.execution_binding_id,
                    execution_order_leg_id=int(leg.execution_order_leg_id),
                    strategy_instance_id=batch.strategy_instance_id,
                    pos_id=str(leg.pos_id),
                    instrument_id=inst_id,
                    side=binding.side,
                    order_id=order_id,
                    purpose=str(row.get("purpose") or ""),
                    trigger_price=_ledger_trigger_price(row),
                    size_text=(
                        str(row.get("size"))
                        if row.get("size") is not None
                        else None
                    ),
                    status="cancelled",
                    evidence_source="management_protection_precancel",
                    evidence={
                        "management_batch_id": batch.id,
                        "management_leg_id": leg.id,
                        "reason": "cancelled_before_risk_reduction",
                    },
                    seen_at=cancelled_at,
                )
                session.commit()


def _precancelled_protection_rows(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    leg: Any,
) -> list[dict[str, Any]]:
    rows = normalize_protection_snapshot_rows(
        (leg.old_tpsl or {}).get("row_snapshots") or []
    )
    expected_ids = [str(value) for value in (leg.old_tpsl or {}).get("order_ids") or []]
    if [str(row.get("order_id")) for row in rows] != expected_ids:
        raise ManagementBatchExecutionError(
            "protection_precancel_snapshot_rows_invalid"
        )
    with session_factory() as session:
        succeeded_keys = {
            str(value)
            for (value,) in (
                session.query(ExecutionEvent.client_order_id)
                .filter(
                    ExecutionEvent.action
                    == "strategy_management_protection_precancel",
                    ExecutionEvent.status == "succeeded",
                    ExecutionEvent.client_order_id.in_(
                        [
                            _protection_cancel_reservation_key(
                                batch_id=batch.id,
                                leg_id=leg.id,
                                order_id=order_id,
                            )
                            for order_id in expected_ids
                        ]
                    ),
                )
                .all()
            )
        }
    expected_keys = {
        _protection_cancel_reservation_key(
            batch_id=batch.id, leg_id=leg.id, order_id=order_id
        )
        for order_id in expected_ids
    }
    if succeeded_keys != expected_keys:
        raise ManagementBatchExecutionError(
            "protection_precancel_success_evidence_incomplete"
        )
    return rows


def _resize_protection_rows_for_remaining_position(
    *,
    batch: ManagementBatchRecord,
    leg: Any,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    remaining = _remaining_size_after_partial_close(leg)
    preflight = _decimal_or_none(leg.preflight_size)
    if remaining is None or preflight is None:
        raise ManagementBatchExecutionError("protection_remaining_size_invalid")
    contract_spec = (
        batch.target_snapshot.get("contract_spec")
        if isinstance(batch.target_snapshot, dict)
        else None
    )
    if not isinstance(contract_spec, dict):
        raise ManagementBatchExecutionError(
            "protection_remaining_contract_spec_missing"
        )
    quantity_step = _decimal_or_none(contract_spec.get("quantity_step"))
    min_quantity = _decimal_or_none(contract_spec.get("min_quantity"))
    persisted_step = _decimal_or_none(leg.quantity_step)
    if (
        quantity_step is None
        or min_quantity is None
        or quantity_step <= 0
        or min_quantity <= 0
        or persisted_step != quantity_step
        or remaining % quantity_step != 0
        or min_quantity % quantity_step != 0
    ):
        raise ManagementBatchExecutionError(
            "protection_remaining_contract_spec_invalid"
        )

    resized: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        size = _decimal_or_none(row.get("size"))
        has_take_profit = row.get("purpose") == "take_profit" or (
            row.get("purpose") == "combined"
            and isinstance(row.get("take_profit"), dict)
            and _has_positive_trigger(row["take_profit"])
        )
        if size == preflight and not has_take_profit:
            row["size"] = _plain_decimal(remaining)
        resized.append(row)

    take_profit_stages: list[tuple[int, Decimal]] = []
    for index, row in enumerate(resized):
        purpose = row.get("purpose")
        if purpose == "combined" and isinstance(row.get("take_profit"), dict):
            if _has_positive_trigger(row["take_profit"]):
                raise ManagementBatchExecutionError(
                    "protection_take_profit_combined_consumption_unsupported"
                )
            continue
        if purpose != "take_profit":
            continue
        size = (
            preflight
            if row.get("full_position")
            else _decimal_or_none(row.get("size"))
        )
        if (
            size is None
            or size <= 0
            or size < min_quantity
            or size % quantity_step != 0
        ):
            raise ManagementBatchExecutionError(
                "protection_take_profit_size_invalid"
            )
        take_profit_stages.append((index, size))

    total_before = sum((size for _index, size in take_profit_stages), Decimal("0"))
    if total_before > preflight:
        raise ManagementBatchExecutionError(
            "protection_take_profit_total_exceeds_preflight_size"
        )

    close_to_consume = _decimal_or_none(leg.planned_close_size)
    if close_to_consume is None or close_to_consume <= 0:
        raise ManagementBatchExecutionError("protection_close_size_invalid")
    removed_indexes: set[int] = set()
    for index, stage_size in take_profit_stages:
        if close_to_consume <= 0:
            break
        consumed = min(stage_size, close_to_consume)
        stage_remaining = stage_size - consumed
        close_to_consume -= consumed
        if stage_remaining == 0:
            removed_indexes.add(index)
            continue
        if (
            stage_remaining < min_quantity
            or stage_remaining % quantity_step != 0
        ):
            raise ManagementBatchExecutionError(
                "protection_take_profit_size_invalid"
            )
        if resized[index].get("full_position"):
            resized[index]["full_position"] = False
        resized[index]["size"] = _plain_decimal(stage_remaining)

    result = [
        row for index, row in enumerate(resized) if index not in removed_indexes
    ]
    total_after = sum(
        (
            _decimal_or_none(row.get("size")) or Decimal("0")
            for row in result
            if row.get("purpose") == "take_profit"
        ),
        Decimal("0"),
    )
    if total_after > remaining:
        raise ManagementBatchExecutionError(
            "protection_take_profit_total_exceeds_remaining_size"
        )
    return result


def _plain_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _restore_precancelled_protection_for_rejected_close(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    leg: Any,
    deepcoin_client: DeepcoinTradingClientProtocol,
) -> dict[str, str] | None:
    if not _risk_reduction_recovery_enabled(batch):
        return None
    try:
        rows = normalize_protection_snapshot_rows(
            (leg.old_tpsl or {}).get("row_snapshots") or []
        )
        if not rows:
            raise ManagementBatchExecutionError(
                "protection_restore_snapshot_missing"
            )
        inst_id = normalize_deepcoin_swap_instrument(binding.symbol)
        positions = list(deepcoin_client.list_positions(inst_id=inst_id))
        positions_by_id = _preflight_exact_protection_positions(
            session_factory=session_factory,
            batch=batch,
            binding=binding,
            live_positions=positions,
            inst_id=inst_id,
            after_partial_close=False,
        )
        common = _protection_payload_common(
            binding=binding,
            position=positions_by_id[str(leg.pos_id)],
            inst_id=inst_id,
        )
        restored_order_ids: list[str] = []
        for index, row in enumerate(rows):
            reservation_digest = hashlib.sha256(
                f"{index}:{row.get('order_id')}".encode("utf-8")
            ).hexdigest()[:12]
            reservation_key = (
                f"pmrr:{batch.id}:{leg.id}:{reservation_digest}"
            )
            with session_factory() as session:
                record_execution_event(
                    session_factory,
                    ExecutionEventRecord(
                        execution_binding_id=binding.id,
                        strategy_instance_id=batch.strategy_instance_id,
                        action="strategy_management_protection_restore",
                        status="reserved",
                        symbol=binding.symbol,
                        side=binding.side,
                        related_order_id=str(row.get("order_id") or "") or None,
                        client_order_id=reservation_key,
                        pos_id=str(leg.pos_id),
                        reason="reserved_before_rejected_close_protection_restore",
                        before={
                            "management_batch_id": batch.id,
                            "management_leg_id": leg.id,
                            "protection_row": row,
                        },
                        created_at=datetime.now(UTC),
                    ),
                    session=session,
                )
                session.commit()
            _require_remediation_live_gate(session_factory, batch=batch)
            payload = _protection_row_payload(common=common, row=row)
            response = submit_exact_position_sltp(
                session_factory=session_factory,
                deepcoin_client=deepcoin_client,
                pos_id=str(leg.pos_id),
                payload=payload,
                idempotency_key=(
                    f"management:{batch.id}:{leg.id}:rejected_close_restore:"
                    f"{row.get('purpose')}:{len(restored_order_ids)}"
                ),
                live_execution_gate=lambda: exact_position_write_gate(
                    session_factory, pos_id=str(leg.pos_id)
                ),
                now_provider=lambda: datetime.now(UTC),
                require_readback=True,
            )
            order_id = _extract_order_id(response)
            if not order_id:
                raise ManagementBatchExecutionError(
                    "protection_restore_missing_order_id"
                )
            restored_order_ids.append(order_id)
            record_execution_event(
                session_factory,
                ExecutionEventRecord(
                    execution_binding_id=binding.id,
                    strategy_instance_id=batch.strategy_instance_id,
                    action="strategy_management_protection_restore",
                    status="succeeded",
                    symbol=binding.symbol,
                    side=binding.side,
                    order_id=order_id,
                    related_order_id=str(row.get("order_id") or "") or None,
                    client_order_id=reservation_key,
                    pos_id=str(leg.pos_id),
                    reason="rejected_close_protection_restored",
                    before={
                        "management_batch_id": batch.id,
                        "management_leg_id": leg.id,
                        "protection_row": row,
                    },
                    request=payload,
                    response=response,
                    created_at=datetime.now(UTC),
                ),
            )
        _record_management_tpsl_ledger_rows(
            session_factory,
            batch=batch,
            binding=binding,
            leg=leg,
            inst_id=inst_id,
            rows=rows,
            order_ids=restored_order_ids,
            seen_at=datetime.now(UTC),
        )
    except Exception as exc:
        return {"type": type(exc).__name__, "message": str(exc)}
    return None


def _require_remediation_live_gate(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
) -> None:
    marker = (
        batch.target_snapshot.get("remediation_confirmation")
        if isinstance(batch.target_snapshot, dict)
        else None
    )
    if marker is None:
        return
    if (
        batch.execution_mode != "live"
        or not load_trading_settings(
            session_factory
        ).live_management_execution_enabled
    ):
        raise ManagementBatchExecutionError(
            "remediation_live_management_gate_closed"
        )


def _require_remediation_confirmation_snapshot(
    *,
    batch: ManagementBatchRecord,
    deepcoin_client: DeepcoinTradingClientProtocol,
) -> None:
    marker = (
        batch.target_snapshot.get("remediation_confirmation")
        if isinstance(batch.target_snapshot, dict)
        else None
    )
    if marker is None:
        return
    if batch.status not in {"ready", "executing"}:
        return
    if batch.status == "executing" and any(
        leg.status != "planned" for leg in batch.legs
    ):
        return
    instruments = {
        str(value).upper()
        for value in marker.get("instrument_scope", [])
        if str(value or "").strip()
    }
    expected_fingerprint = str(
        marker.get("exchange_snapshot_fingerprint") or ""
    )
    if not instruments or len(expected_fingerprint) != 64:
        raise ManagementBatchExecutionError(
            "remediation_confirmation_marker_invalid"
        )
    snapshot = _load_reconcile_snapshot(
        deepcoin_client,
        instruments=instruments,
    )
    if snapshot.errors or any(
        not bool(observation.get("complete"))
        for observation in snapshot.pending_tpsl_observations
    ):
        raise ManagementBatchExecutionError(
            "remediation_confirmation_snapshot_incomplete"
        )
    payload = remediation_snapshot_payload(snapshot)
    current_fingerprint = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    if current_fingerprint != expected_fingerprint:
        raise ManagementBatchExecutionError(
            "remediation_confirmation_snapshot_changed"
        )


def _preflight_exact_position_identity(
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    live_positions: list[dict[str, Any]],
    inst_id: str,
    error_prefix: str,
    ignored_owned_pos_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    expected_pos_ids = {leg.pos_id for leg in batch.legs}
    ignored = set(ignored_owned_pos_ids or ()) - expected_pos_ids
    if any(
        _first_text(row, "instId", "inst_id") == inst_id
        and (_first_text(row, "posSide", "pos_side", "side") or "").lower()
        == binding.side.lower()
        and _first_text(row, "posId", "pos_id", "id") is None
        for row in live_positions
    ):
        raise ManagementBatchExecutionError(
            f"{error_prefix}_position_ambiguous"
        )
    bound_pos_ids = {
        value.strip()
        for value in str(binding.pos_id or "").split(",")
        if value.strip()
    }
    if bound_pos_ids != expected_pos_ids:
        raise ManagementBatchExecutionError(
            f"{error_prefix}_binding_position_set_drift"
        )
    relevant_live_ids = {
        str(pos_id)
        for row in live_positions
        if _first_text(row, "instId", "inst_id") == inst_id
        and (_first_text(row, "posSide", "pos_side", "side") or "").lower()
        == binding.side.lower()
        and (pos_id := _first_text(row, "posId", "pos_id", "id")) is not None
        and str(pos_id) not in ignored
    }
    if relevant_live_ids != expected_pos_ids:
        raise ManagementBatchExecutionError(
            f"{error_prefix}_live_position_set_drift"
        )
    positions_by_id: dict[str, dict[str, Any]] = {}
    for row in live_positions:
        pos_id = _first_text(row, "posId", "pos_id", "id")
        if pos_id in ignored:
            continue
        if pos_id not in expected_pos_ids:
            continue
        if (
            _first_text(row, "instId", "inst_id") != inst_id
            or (_first_text(row, "posSide", "pos_side", "side") or "").lower()
            != binding.side.lower()
            or pos_id in positions_by_id
        ):
            raise ManagementBatchExecutionError(
                f"{error_prefix}_position_ambiguous"
            )
        positions_by_id[str(pos_id)] = row
    if set(positions_by_id) != expected_pos_ids:
        raise ManagementBatchExecutionError(f"{error_prefix}_position_set_drift")
    return positions_by_id


def validate_management_restart_snapshot(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    snapshot: Any,
) -> None:
    """Require exact frozen exchange identity before resuming an all-planned batch."""

    if getattr(snapshot, "errors", {}):
        raise ManagementBatchExecutionError("restart_snapshot_exchange_read_failed")
    batch = load_management_batch(session_factory, int(batch_id))
    binding = _load_exact_binding(session_factory, batch)
    try:
        _require_exact_entry_legs(session_factory, batch)
    except DeferredEntryIdentityDriftError as exc:
        _persist_deferred_cancel_diagnostics(
            session_factory,
            batch=batch,
            binding=binding,
            diagnostics=exc.diagnostics,
            created_at=datetime.now(UTC),
        )
        raise
    inst_id = normalize_deepcoin_swap_instrument(binding.symbol)
    positions = _preflight_exact_position_identity(
        batch=batch,
        binding=binding,
        live_positions=list(getattr(snapshot, "positions", [])),
        inst_id=inst_id,
        error_prefix="restart_snapshot",
        ignored_owned_pos_ids=_other_exact_strategy_position_ids(
            session_factory, batch=batch, binding=binding
        ),
    )
    for leg in batch.legs:
        if not _decimal_equal(
            _first_text(positions[leg.pos_id], "pos", "size", "sz"),
            leg.preflight_size,
        ):
            raise ManagementBatchExecutionError(
                "restart_snapshot_position_size_drift"
            )


def _other_exact_strategy_position_ids(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
) -> set[str]:
    """Return only uniquely verified live positions owned by another binding."""

    with session_factory() as session:
        rows = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.purpose == "entry")
            .filter(ExecutionOrderLeg.pos_id.is_not(None))
            .filter(ExecutionOrderLeg.execution_binding_id != batch.execution_binding_id)
            .all()
        )
        by_pos: dict[str, list[ExecutionOrderLeg]] = {}
        for row in rows:
            if (
                str(row.status or "").lower() in TERMINAL_ENTRY_LEG_STATES
                or row.terminal_reason is not None
            ):
                continue
            by_pos.setdefault(str(row.pos_id), []).append(row)

        owned: set[str] = set()
        for pos_id, candidates in by_pos.items():
            if len(candidates) != 1:
                continue
            row = candidates[0]
            other = session.get(ExecutionBinding, row.execution_binding_id)
            bound_ids = {
                value.strip()
                for value in str(other.pos_id if other is not None else "").split(",")
                if value.strip()
            }
            if (
                other is not None
                and other.status == "active"
                and other.venue == binding.venue
                and row.venue == binding.venue
                and row.attribution_status == "verified"
                and str(row.status or "").lower()
                in {"active", "open", "filled", "partial_closed"}
                and bool(other.strategy_instance_id)
                and other.strategy_instance_id != batch.strategy_instance_id
                and row.strategy_instance_id == other.strategy_instance_id
                and other.symbol.upper() == binding.symbol.upper()
                and other.side.lower() == binding.side.lower()
                and pos_id in bound_ids
            ):
                owned.add(pos_id)
        return owned


def _preflight_exact_protection_rows(
    *,
    session_factory: sessionmaker,
    batch: ManagementBatchRecord,
    live_positions: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    ledger_rows_by_pos_id = _ledger_rows_by_pos_id(
        session_factory, [leg.pos_id for leg in batch.legs]
    )
    matches = match_position_protection(
        live_positions,
        pending,
        exact_order_position_ids=_exact_order_position_ids(
            ledger_rows_by_pos_id
        ),
    )
    seen_ids: set[str] = set()
    current_rows_by_pos_id: dict[str, list[dict[str, Any]]] = {}
    for leg in batch.legs:
        protection = matches.by_pos_id.get(leg.pos_id)
        expected = leg.old_tpsl or {}
        current_rows = (
            snapshot_protection_rows(protection.rows) if protection is not None else []
        )
        position_only_without_order_ids = bool(
            protection is not None
            and protection.status == "verified"
            and protection.rows
            and not protection.order_ids
            and all(
                row.get("_evidence_source") == "position"
                for row in protection.rows
            )
        )
        if (
            (
                protection is None
                or protection.status in {"absent", "evidence_unavailable"}
                or position_only_without_order_ids
            )
            and expected.get("order_ids")
        ):
            current_rows = _ledger_confirmed_current_snapshots(
                leg=leg,
                expected=expected,
                pending=pending,
                ledger_rows=ledger_rows_by_pos_id.get(str(leg.pos_id), []),
            )
        expected_rows = normalize_protection_snapshot_rows(
            expected.get("row_snapshots") or []
        )
        current_ids = [row.get("order_id") for row in current_rows]
        if (
            not _protection_rows_match_expected_snapshot(
                batch=batch,
                leg=leg,
                current_rows=current_rows,
                expected_rows=expected_rows,
            )
            or current_ids != expected.get("order_ids")
            or any(not order_id or order_id in seen_ids for order_id in current_ids)
        ):
            raise ManagementBatchExecutionError(
                "protection_preflight_rows_ambiguous_or_drifted"
            )
        seen_ids.update(str(order_id) for order_id in current_ids)
        current_rows_by_pos_id[str(leg.pos_id)] = current_rows
    return current_rows_by_pos_id


def _protection_rows_match_expected_snapshot(
    *,
    batch: ManagementBatchRecord,
    leg: Any,
    current_rows: list[dict[str, Any]],
    expected_rows: list[dict[str, Any]],
) -> bool:
    if current_rows == expected_rows:
        return True
    if batch.effective_action != "partial_then_break_even":
        return False
    if len(current_rows) != len(expected_rows):
        return False
    remaining_size = _remaining_size_after_partial_close(leg)
    if remaining_size is None:
        return False
    for current, expected in zip(current_rows, expected_rows, strict=False):
        if _row_without_size(current) != _row_without_size(expected):
            return False
        expected_size = _decimal_or_none(expected.get("size"))
        current_size = _decimal_or_none(current.get("size"))
        if expected_size is None or current_size is None:
            return False
        if expected_size == current_size:
            continue
        if expected_size != _decimal_or_none(leg.preflight_size):
            return False
        if current_size != remaining_size:
            return False
    return True


def _row_without_size(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    normalized.pop("size", None)
    return normalized


def _remaining_size_after_partial_close(leg: Any) -> Decimal | None:
    preflight = _decimal_or_none(leg.preflight_size)
    close_size = _decimal_or_none(leg.planned_close_size)
    if preflight is None or close_size is None:
        return None
    remaining = preflight - close_size
    return remaining if remaining > 0 else None


def _ledger_rows_by_pos_id(
    session_factory: sessionmaker, _pos_ids: list[str]
) -> dict[str, list[Any]]:
    with session_factory() as session:
        rows = list_verified_account_ledger_rows(session)
        result: dict[str, list[Any]] = {}
        for row in rows:
            result.setdefault(str(row.pos_id), []).append(row)
        return result


def _exact_order_position_ids(
    ledger_rows_by_pos_id: dict[str, list[Any]],
) -> dict[str, str]:
    return {
        str(row.order_id): str(row.pos_id)
        for rows in ledger_rows_by_pos_id.values()
        for row in rows
        if str(row.order_id or "").strip()
    }


def _ledger_confirmed_current_snapshots(
    *,
    leg: Any,
    expected: dict[str, Any],
    pending: list[dict[str, Any]],
    ledger_rows: list[Any],
) -> list[dict[str, Any]]:
    if not ledger_rows:
        return []
    expected_order_ids = [str(order_id) for order_id in expected.get("order_ids") or []]
    pending_by_order_id = {
        order_id: row
        for row in pending
        if (order_id := _first_text(row, "ordId", "orderId", "order_id")) is not None
    }
    ledger_by_order_id = {str(row.order_id): row for row in ledger_rows}
    current_rows: list[dict[str, Any]] = []
    for order_id in expected_order_ids:
        ledger = ledger_by_order_id.get(order_id)
        pending_row = pending_by_order_id.get(order_id)
        if ledger is None or pending_row is None:
            return []
        if (
            int(ledger.execution_order_leg_id) != int(leg.execution_order_leg_id)
            or str(ledger.pos_id) != str(leg.pos_id)
            or not _ledger_matches_pending_row(ledger, pending_row)
        ):
            return []
        snapshot = snapshot_protection_rows([pending_row])
        if len(snapshot) != 1:
            return []
        current_rows.extend(snapshot)
    return current_rows


def _ledger_matches_pending_row(ledger: Any, row: dict[str, Any]) -> bool:
    if str(row.get("triggerOrderType") or "TPSL").upper() != "TPSL":
        return False
    if str(row.get("instId") or "").upper() != str(ledger.instrument_id or "").upper():
        return False
    side = str(row.get("posSide") or row.get("side") or "").lower()
    side = {"buy": "long", "sell": "short"}.get(side, side)
    if side != str(ledger.side or "").lower():
        return False
    ledger_price = _decimal_or_none(ledger.trigger_price)
    if ledger_price is not None:
        current_price = _pending_row_trigger_price(row, str(ledger.purpose or ""))
        if current_price is None or current_price != ledger_price:
            return False
    ledger_size = _decimal_or_none(ledger.size_text)
    row_size = _decimal_or_none(row.get("sz") or row.get("size"))
    if ledger_size is not None and row_size is not None and row_size != ledger_size:
        return False
    return True


def _pending_row_trigger_price(row: dict[str, Any], purpose: str) -> Decimal | None:
    keys = (
        ("slTriggerPx", "slTriggerPrice", "closeSLTriggerPrice")
        if purpose in {"stop_loss", "sl", "loss"}
        else ("tpTriggerPx", "tpTriggerPrice", "closeTPTriggerPrice")
    )
    for key in keys:
        value = _decimal_or_none(row.get(key))
        if value is not None and value != 0:
            return value
    return None


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _adjusted_protection_rows(
    *, batch: ManagementBatchRecord, leg: Any, old_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    stop_price = _planned_stop_price(batch=batch, leg=leg)
    adjusted: list[dict[str, Any]] = []
    found_stop = False
    for old in old_rows:
        row = dict(old)
        if row.get("purpose") == "stop_loss":
            row["trigger_price"] = str(stop_price)
            found_stop = True
            adjusted.append(row)
        elif row.get("purpose") == "combined":
            adjusted_row, includes_stop = _adjusted_combined_protection_row(
                row=row, stop_price=str(stop_price)
            )
            found_stop = found_stop or includes_stop
            adjusted.append(adjusted_row)
        else:
            adjusted.append(row)
    if not found_stop:
        raise ManagementBatchExecutionError("existing_stop_loss_missing")
    return adjusted


def _planned_stop_price(*, batch: ManagementBatchRecord, leg: Any) -> str:
    planned_tpsl = leg.planned_tpsl or {}
    explicit = planned_tpsl.get("stop_loss_text")
    explicit_source = str(
        planned_tpsl.get("stop_price_source") or ""
    ).strip().lower()
    break_even_action = batch.effective_action in {
        "move_stop_to_break_even",
        "partial_then_break_even",
    }
    if explicit not in (None, "") and (
        not break_even_action
        or explicit_source in {"", "current_message_text"}
    ):
        parsed = _decimal_or_none(explicit)
        if parsed is None or parsed <= 0:
            raise ManagementBatchExecutionError("planned_stop_loss_invalid")
        return str(explicit).strip()

    if break_even_action:
        parsed = _decimal_or_none(leg.avg_entry_price)
        if parsed is None or parsed <= 0:
            raise ManagementBatchExecutionError("planned_stop_loss_missing")
        return str(leg.avg_entry_price).strip()

    raise ManagementBatchExecutionError("planned_stop_loss_missing")


def _require_explicit_stop_write_boundary(
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    deepcoin_client: DeepcoinTradingClientProtocol,
    inst_id: str,
    current_rows_by_pos_id: dict[str, list[dict[str, Any]]],
) -> None:
    try:
        quote = deepcoin_client.get_ticker_quote(inst_id=inst_id)
    except Exception as exc:
        raise ManagementBatchExecutionError(
            "explicit_stop_market_quote_unavailable"
        ) from exc
    if (
        not isinstance(quote, dict)
        or str(quote.get("instrument_id") or "").upper() != inst_id.upper()
        or quote.get("price_field") not in {"last", "lastPx"}
    ):
        raise ManagementBatchExecutionError(
            "explicit_stop_market_quote_unavailable"
        )
    market_price = _decimal_or_none(quote.get("price"))
    if market_price is None or market_price <= 0:
        raise ManagementBatchExecutionError(
            "explicit_stop_market_quote_unavailable"
        )
    side = str(binding.side or "").strip().lower()
    if side not in {"long", "short"}:
        raise ManagementBatchExecutionError("explicit_stop_market_side_invalid")
    for leg in batch.legs:
        planned_stop = _decimal_or_none(
            _planned_stop_price(batch=batch, leg=leg)
        )
        current_stops = {
            price
            for row in current_rows_by_pos_id.get(str(leg.pos_id), [])
            if (price := _snapshot_stop_price(row))
            is not None
            and price > 0
        }
        if planned_stop is None:
            raise ManagementBatchExecutionError("planned_stop_loss_invalid")
        if side == "long":
            market_safe = planned_stop < market_price
            tightens = bool(current_stops) and all(
                planned_stop > stop for stop in current_stops
            )
        else:
            market_safe = planned_stop > market_price
            tightens = bool(current_stops) and all(
                planned_stop < stop for stop in current_stops
            )
        if not market_safe:
            raise ManagementBatchExecutionError(
                "explicit_stop_market_side_invalid"
            )
        if not tightens:
            raise ManagementBatchExecutionError(
                "explicit_stop_not_risk_tightening"
            )


def _snapshot_stop_price(row: dict[str, Any]) -> Decimal | None:
    purpose = str(row.get("purpose") or "").strip().lower()
    if purpose == "stop_loss":
        return _decimal_or_none(row.get("trigger_price"))
    if purpose == "combined":
        stop_loss = row.get("stop_loss")
        if isinstance(stop_loss, dict):
            return _decimal_or_none(stop_loss.get("trigger_price"))
    return None


def _adjusted_combined_protection_row(
    *, row: dict[str, Any], stop_price: str
) -> tuple[dict[str, Any], bool]:
    take_profit = dict(row.get("take_profit") or {})
    stop_loss = dict(row.get("stop_loss") or {})
    has_take_profit = _has_positive_trigger(take_profit)
    has_stop_loss = _has_positive_trigger(stop_loss)

    if has_take_profit and has_stop_loss:
        row["take_profit"] = take_profit
        row["stop_loss"] = {**stop_loss, "trigger_price": stop_price}
        return row, True

    common = {
        "order_id": row.get("order_id"),
        "size": row.get("size"),
        "full_position": row.get("full_position"),
    }
    if has_take_profit:
        return (
            {
                **common,
                "purpose": "take_profit",
                "trigger_price": take_profit.get("trigger_price"),
                "trigger_type": take_profit.get("trigger_type"),
                "order_price": take_profit.get("order_price"),
            },
            False,
        )
    if has_stop_loss:
        return (
            {
                **common,
                "purpose": "stop_loss",
                "trigger_price": stop_price,
                "trigger_type": stop_loss.get("trigger_type"),
                "order_price": stop_loss.get("order_price"),
            },
            True,
        )
    raise ManagementBatchExecutionError("existing_protection_row_missing_trigger")


def _has_positive_trigger(row: dict[str, Any]) -> bool:
    parsed = _decimal_or_none(row.get("trigger_price"))
    return parsed is not None and parsed > 0


def _protection_payload_common(
    *, binding: ExecutionBinding, position: dict[str, Any], inst_id: str
) -> dict[str, Any]:
    payload = {
        "instType": "SWAP",
        "instId": inst_id,
        "posSide": binding.side.lower(),
        "mrgPosition": normalize_deepcoin_position_mode(binding.position_mode),
        "tdMode": normalize_deepcoin_margin_mode(binding.margin_mode),
    }
    if payload["mrgPosition"] == "split":
        pos_id = _first_text(position, "posId", "pos_id", "id")
        if not pos_id:
            raise ManagementBatchExecutionError("protection_preflight_pos_id_missing")
        payload["posId"] = pos_id
    return payload


def _record_management_tpsl_ledger_rows(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    leg: Any,
    inst_id: str,
    rows: list[dict[str, Any]],
    order_ids: list[str],
    seen_at: datetime,
    evidence_source: str = "management_tpsl_replacement",
) -> None:
    with session_factory() as session:
        for row, order_id in zip(rows, order_ids, strict=False):
            upsert_protection_ledger_row(
                session,
                venue=binding.venue,
                execution_binding_id=batch.execution_binding_id,
                execution_order_leg_id=int(leg.execution_order_leg_id),
                strategy_instance_id=batch.strategy_instance_id,
                pos_id=str(leg.pos_id),
                instrument_id=inst_id,
                side=binding.side,
                order_id=str(order_id),
                purpose=str(row.get("purpose") or ""),
                trigger_price=_ledger_trigger_price(row),
                size_text=str(row.get("size")) if row.get("size") is not None else None,
                status="verified",
                evidence_source=evidence_source,
                evidence={
                    "match": "exchange_returned_order_id",
                    "management_batch_id": batch.id,
                    "management_leg_id": leg.id,
                },
                seen_at=seen_at,
            )
        record_replacing_protection_revision(
            session,
            venue=binding.venue,
            execution_binding_id=batch.execution_binding_id,
            execution_order_leg_id=int(leg.execution_order_leg_id),
            strategy_instance_id=batch.strategy_instance_id,
            pos_id=str(leg.pos_id),
            source=evidence_source,
            protection_json={"order_ids": [str(value) for value in order_ids], "rows": rows, "management_batch_id": batch.id},
        )
        session.commit()


def _mark_management_tpsl_ledger_cancelled(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    leg: Any,
    order_id: str,
    seen_at: datetime,
) -> None:
    with session_factory() as session:
        row = (
            session.query(PositionProtectionLedger)
            .filter(
                PositionProtectionLedger.venue == "deepcoin",
                PositionProtectionLedger.order_id == str(order_id),
                PositionProtectionLedger.execution_binding_id
                == batch.execution_binding_id,
                PositionProtectionLedger.execution_order_leg_id
                == int(leg.execution_order_leg_id),
                PositionProtectionLedger.pos_id == str(leg.pos_id),
            )
            .one_or_none()
        )
        if row is not None:
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


def _protection_row_payload(
    *, common: dict[str, Any], row: dict[str, Any]
) -> dict[str, Any]:
    payload = dict(common)
    size = row.get("size")
    if not row.get("full_position"):
        try:
            parsed_size = Decimal(str(size))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ManagementBatchExecutionError("invalid_partial_protection_size") from exc
        if not parsed_size.is_finite() or parsed_size <= 0:
            raise ManagementBatchExecutionError("invalid_partial_protection_size")
        payload["sz"] = str(size)
    purpose = row.get("purpose")
    if purpose in {"take_profit", "stop_loss"}:
        prefix = "tp" if purpose == "take_profit" else "sl"
        payload[f"{prefix}TriggerPx"] = str(row["trigger_price"])
        payload[f"{prefix}TriggerPxType"] = str(row.get("trigger_type") or "last")
        payload[f"{prefix}OrdPx"] = str(row.get("order_price") or "-1")
    elif purpose == "combined":
        for key, prefix in (("take_profit", "tp"), ("stop_loss", "sl")):
            item = row[key]
            payload[f"{prefix}TriggerPx"] = str(item["trigger_price"])
            payload[f"{prefix}TriggerPxType"] = str(
                item.get("trigger_type") or "last"
            )
            payload[f"{prefix}OrdPx"] = str(item.get("order_price") or "-1")
    else:
        raise ManagementBatchExecutionError("unsupported_protection_row_purpose")
    return payload


def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _decimal_equal(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _load_exact_binding(
    session_factory: sessionmaker, batch: ManagementBatchRecord
) -> ExecutionBinding:
    with session_factory() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        if (
            binding is None
            or binding.strategy_instance_id != batch.strategy_instance_id
            or binding.status not in {"active", "open", "partial"}
        ):
            raise ManagementBatchExecutionError("batch_binding_not_active_or_exact")
        session.expunge(binding)
        return binding


def _require_exact_entry_legs(
    session_factory: sessionmaker, batch: ManagementBatchRecord
) -> None:
    with session_factory() as session:
        stored_batch = session.get(StrategyManagementBatch, batch.id)
        deferred_snapshot_id_list = _parse_exact_deferred_entry_leg_ids(
            getattr(stored_batch, "target_snapshot_json", None),
            error_code="batch_entry_set_not_exact",
        )
        deferred_snapshot_ids = set(deferred_snapshot_id_list)
        all_entries = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == batch.execution_binding_id)
            .filter(ExecutionOrderLeg.purpose == "entry")
            .all()
        )
        if batch.effective_action in {"full_close", "full_exit"}:
            snapshot_rows = (
                session.query(ExecutionOrderLeg)
                .filter(ExecutionOrderLeg.id.in_(deferred_snapshot_id_list))
                .all()
                if deferred_snapshot_id_list
                else []
            )
            snapshot_rows_by_id = {int(entry.id): entry for entry in snapshot_rows}
            current_deferred_entries = [
                entry
                for entry in all_entries
                if entry.strategy_instance_id == batch.strategy_instance_id
                and _is_deferred_pending_entry_leg(entry)
            ]
            identity_diagnostics = _deferred_entry_identity_diagnostics(
                batch=batch,
                snapshot_leg_ids=deferred_snapshot_id_list,
                snapshot_rows_by_id=snapshot_rows_by_id,
                current_deferred_entries=current_deferred_entries,
            )
            if identity_diagnostics:
                raise DeferredEntryIdentityDriftError(
                    "batch_entry_set_not_exact", identity_diagnostics
                )
        batch_identity = {
            (int(leg.execution_order_leg_id), str(leg.pos_id)) for leg in batch.legs
        }
        current_identity: set[tuple[int, str]] = set()
        current_deferred_ids: set[int] = set()
        accepted_cancelled_deferred_ids: set[int] = set()
        if not all_entries:
            raise ManagementBatchExecutionError("batch_entry_set_not_exact")
        for entry in all_entries:
            status = str(entry.status or "").lower()
            if entry.strategy_instance_id != batch.strategy_instance_id:
                raise ManagementBatchExecutionError("batch_entry_set_not_exact")
            identity = (int(entry.id), str(entry.pos_id)) if entry.pos_id else None
            if identity in batch_identity:
                if (
                    entry.attribution_status != "verified"
                    or status in TERMINAL_ENTRY_LEG_STATES
                    or status not in _MANAGEABLE_ENTRY_LEG_STATES
                    or entry.terminal_reason is not None
                ):
                    raise ManagementBatchExecutionError("batch_entry_set_not_exact")
                current_identity.add(identity)
                continue
            if (
                int(entry.id) in deferred_snapshot_ids
                and _is_management_cancelled_deferred_entry_leg(entry)
            ):
                accepted_cancelled_deferred_ids.add(int(entry.id))
                continue
            if status in TERMINAL_ENTRY_LEG_STATES:
                continue
            if _is_deferred_pending_entry_leg(entry):
                current_deferred_ids.add(int(entry.id))
                continue
            raise ManagementBatchExecutionError("batch_entry_set_not_exact")
        if (
            current_identity != batch_identity
            or (
                current_deferred_ids | accepted_cancelled_deferred_ids
            )
            != deferred_snapshot_ids
        ):
            raise ManagementBatchExecutionError("batch_entry_set_not_exact")
        for leg in batch.legs:
            entry = session.get(ExecutionOrderLeg, leg.execution_order_leg_id)
            status = str(entry.status or "").lower() if entry is not None else ""
            if (
                entry is None
                or entry.execution_binding_id != batch.execution_binding_id
                or entry.strategy_instance_id != batch.strategy_instance_id
                or entry.pos_id != leg.pos_id
                or status not in _MANAGEABLE_ENTRY_LEG_STATES
                or entry.attribution_status != "verified"
                or entry.terminal_reason is not None
            ):
                raise ManagementBatchExecutionError(
                    f"batch_entry_leg_not_exact_or_active:{leg.id}"
                )


def _is_management_cancelled_deferred_entry_leg(
    entry: ExecutionOrderLeg,
) -> bool:
    return bool(
        str(entry.status or "").lower() == "cancelled"
        and entry.terminal_reason
        == "management_full_close_cancelled_unfilled_entry_leg"
        and not entry.pos_id
    )


def _deferred_entry_identity_diagnostics(
    *,
    batch: ManagementBatchRecord,
    snapshot_leg_ids: list[int],
    snapshot_rows_by_id: dict[int, ExecutionOrderLeg],
    current_deferred_entries: list[ExecutionOrderLeg],
) -> list[dict[str, Any]]:
    """Describe bounded exact-set drift, prioritising live pending entries."""

    snapshot_diagnostics: list[dict[str, Any]] = []
    current_deferred_ids = {int(entry.id) for entry in current_deferred_entries}
    for leg_id in snapshot_leg_ids:
        entry = snapshot_rows_by_id.get(leg_id)
        if entry is None:
            snapshot_diagnostics.append(
                _deferred_identity_diagnostic(
                    leg_id=leg_id,
                    identity_state="snapshot_leg_missing",
                    reason="snapshot_deferred_entry_leg_missing",
                )
            )
            continue
        if (
            entry.execution_binding_id != batch.execution_binding_id
            or entry.strategy_instance_id != batch.strategy_instance_id
            or entry.purpose != "entry"
        ):
            snapshot_diagnostics.append(
                _deferred_identity_diagnostic(
                    leg_id=leg_id,
                    identity_state="snapshot_leg_reassigned",
                    reason="snapshot_deferred_entry_leg_reassigned",
                )
            )
            continue
        if leg_id not in current_deferred_ids:
            snapshot_diagnostics.append(
                _deferred_identity_diagnostic(
                    leg_id=leg_id,
                    identity_state="snapshot_leg_state_drift",
                    reason="snapshot_deferred_entry_leg_state_drift",
                    order_id=entry.order_id,
                    client_order_id=entry.client_order_id,
                )
            )
    snapshot_ids = set(snapshot_leg_ids)
    pending_diagnostics = [
        _deferred_identity_diagnostic(
            leg_id=int(entry.id),
            identity_state="unsnapshotted_pending",
            reason="unsnapshotted_pending_entry_leg",
            order_id=entry.order_id,
            client_order_id=entry.client_order_id,
        )
        for entry in sorted(current_deferred_entries, key=lambda item: int(item.id))
        if int(entry.id) not in snapshot_ids
    ]
    diagnostics = [*pending_diagnostics, *snapshot_diagnostics]
    capped_diagnostics = diagnostics[:20]
    if len(diagnostics) > len(capped_diagnostics):
        capped_diagnostics[-1] = {
            **capped_diagnostics[-1],
            "omitted_identity_drift_count": len(diagnostics) - len(capped_diagnostics),
        }
    return capped_diagnostics


def _deferred_identity_diagnostic(
    *,
    leg_id: int,
    identity_state: str,
    reason: str,
    order_id: Any = None,
    client_order_id: Any = None,
) -> dict[str, Any]:
    diagnostic = {
        "execution_order_leg_id": int(leg_id),
        "identity_state": identity_state,
        "live_match_source": "not_checked",
        "match_type": "identity",
        "status": "unresolved",
        "reason": reason,
    }
    bounded_order_id = _bounded_diagnostic_identifier(order_id)
    bounded_client_order_id = _bounded_diagnostic_identifier(client_order_id)
    if bounded_order_id:
        diagnostic["order_id"] = bounded_order_id
    if bounded_client_order_id:
        diagnostic["client_order_id"] = bounded_client_order_id
    return diagnostic


def _bounded_diagnostic_identifier(value: Any) -> str | None:
    text = _stored_text(value)
    if text is None:
        return None
    if any(
        marker in text.lower()
        for marker in ("api-key", "api_key", "authorization", "passphrase", "secret")
    ):
        return "[redacted]"
    return text[:120]


def _is_deferred_pending_entry_leg(entry: ExecutionOrderLeg) -> bool:
    status = str(entry.status or "").lower()
    state = str(entry.attribution_status or "unassigned")
    return bool(
        status in _DEFERRED_ENTRY_LEG_STATES
        and status not in TERMINAL_ENTRY_LEG_STATES
        and entry.terminal_reason is None
        and not entry.pos_id
        and state not in {"attribution_conflict", "evidence_unavailable"}
    )


def _load_exact_deferred_entry_legs(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
) -> list[ExecutionOrderLeg]:
    """Load the exact pending entry legs named by the persisted batch snapshot."""

    with session_factory() as session:
        stored_batch = session.get(StrategyManagementBatch, batch.id)
        deferred_leg_ids = _parse_exact_deferred_entry_leg_ids(
            getattr(stored_batch, "target_snapshot_json", None),
            error_code="deferred_entry_cancel_identity_drift",
        )
        if not deferred_leg_ids:
            return []

        rows = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.id.in_(deferred_leg_ids))
            .all()
        )
        rows_by_id = {int(row.id): row for row in rows}
        if len(rows_by_id) != len(deferred_leg_ids):
            raise ManagementBatchExecutionError(
                "deferred_entry_cancel_identity_drift"
            )
        ordered_rows = [rows_by_id[leg_id] for leg_id in deferred_leg_ids]
        for entry in ordered_rows:
            if (
                entry.execution_binding_id != batch.execution_binding_id
                or entry.strategy_instance_id != batch.strategy_instance_id
                or entry.purpose != "entry"
                or not (_stored_text(entry.order_id) or _stored_text(entry.client_order_id))
            ):
                raise ManagementBatchExecutionError(
                    "deferred_entry_cancel_identity_drift"
                )
            status = str(entry.status or "").lower()
            if (
                status not in _DEFERRED_ENTRY_LEG_STATES
                or status in TERMINAL_ENTRY_LEG_STATES
                or entry.terminal_reason is not None
                or entry.pos_id
            ):
                raise ManagementBatchExecutionError(
                    "deferred_entry_cancel_leg_not_pending"
                )
            session.expunge(entry)
        return ordered_rows


def _parse_exact_deferred_entry_leg_ids(
    target_snapshot_json: Any,
    *,
    error_code: str,
) -> list[int]:
    try:
        target_snapshot = json.loads(target_snapshot_json)
        identity = target_snapshot["identity"]
        deferred_leg_ids = identity["deferred_entry_leg_ids"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ManagementBatchExecutionError(error_code) from exc
    if (
        not isinstance(target_snapshot, dict)
        or not isinstance(identity, dict)
        or not isinstance(deferred_leg_ids, list)
        or any(type(leg_id) is not int or leg_id <= 0 for leg_id in deferred_leg_ids)
        or len(set(deferred_leg_ids)) != len(deferred_leg_ids)
    ):
        raise ManagementBatchExecutionError(error_code)
    return deferred_leg_ids


def _stored_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _exchange_order_ownership(
    order: dict[str, Any], entry: ExecutionOrderLeg
) -> tuple[str | None, str | None, str | None]:
    order_ids = [
        str(value)
        for key in ("ordId", "orderId", "id")
        if (value := order.get(key)) not in (None, "")
    ]
    client_order_ids = [
        str(value)
        for key in ("clOrdId", "clientOrderId")
        if (value := order.get(key)) not in (None, "")
    ]
    stored_order_id = _stored_text(entry.order_id)
    stored_client_order_id = _stored_text(entry.client_order_id)
    order_match = stored_order_id if stored_order_id in order_ids else None
    client_match = (
        stored_client_order_id if stored_client_order_id in client_order_ids else None
    )
    if not order_match and not client_match:
        return None, None, None
    if len(set(order_ids)) > 1:
        return None, None, "exchange_order_id_alias_conflict"
    if len(set(client_order_ids)) > 1:
        return None, None, "exchange_client_order_id_alias_conflict"
    # Return only IDs whose exact alias value established ownership. Merely
    # present secondary identifiers must not enter a cancellation request.
    return order_match, client_match, None


def _match_exact_deferred_exchange_orders(
    *,
    deferred_entries: list[ExecutionOrderLeg],
    binding: ExecutionBinding,
    deepcoin_client: DeepcoinTradingClientProtocol,
) -> list[_DeferredExchangeMatch]:
    """Match each snapshotted leg to exactly one live exchange order."""

    if not deferred_entries:
        return []
    inst_id = normalize_deepcoin_swap_instrument(binding.symbol)
    trigger_orders = list(deepcoin_client.list_trigger_orders_pending(inst_id=inst_id))
    regular_orders = list(deepcoin_client.list_open_orders(inst_id=inst_id))
    exchange_rows = [
        (cancel_type, index, order)
        for cancel_type, orders in (
            ("trigger", trigger_orders),
            ("regular", regular_orders),
        )
        for index, order in enumerate(orders)
        if isinstance(order, dict)
    ]
    matched_row_keys: set[tuple[str, int]] = set()
    exact_matches: list[_DeferredExchangeMatch] = []
    diagnostics: list[dict[str, Any]] = []
    for entry in deferred_entries:
        matches = []
        conflicts = []
        for cancel_type, index, order in exchange_rows:
            order_id, client_order_id, conflict = _exchange_order_ownership(
                order, entry
            )
            if conflict:
                conflicts.append((cancel_type, index, conflict))
            elif order_id or client_order_id:
                matches.append(
                    (cancel_type, index, order, order_id, client_order_id)
                )
        if conflicts:
            cancel_type, _, reason = conflicts[0]
            diagnostics.append(
                _deferred_cancel_diagnostic(
                    entry,
                    source=_deferred_cancel_source(cancel_type),
                    match_type=cancel_type,
                    status="unresolved",
                    reason=reason,
                )
            )
            continue
        if len(matches) != 1:
            diagnostics.append(
                _deferred_cancel_diagnostic(
                    entry,
                    source=(
                        _deferred_cancel_source(matches[0][0]) if matches else "none"
                    ),
                    match_type=matches[0][0] if matches else "unknown",
                    status="unresolved",
                    reason=(
                        "exchange_order_match_ambiguous"
                        if matches else "exchange_order_not_found"
                    ),
                )
            )
            continue
        cancel_type, index, order, order_id, client_order_id = matches[0]
        row_key = (cancel_type, index)
        if row_key in matched_row_keys:
            diagnostics.append(
                _deferred_cancel_diagnostic(
                    entry,
                    source=_deferred_cancel_source(cancel_type),
                    match_type=cancel_type,
                    status="unresolved",
                    reason="exchange_order_match_reused",
                )
            )
            continue
        matched_row_keys.add(row_key)
        exact_matches.append(
            _DeferredExchangeMatch(
                entry=entry,
                cancel_type=cancel_type,
                source=_deferred_cancel_source(cancel_type),
                order=order,
                order_id=order_id,
                client_order_id=client_order_id,
            )
        )
    if diagnostics:
        diagnosed_entry_ids = {
            int(diagnostic["execution_order_leg_id"])
            for diagnostic in diagnostics
        }
        exact_matches_by_entry_id = {
            int(match.entry.id): match for match in exact_matches
        }
        diagnostics.extend(
            _deferred_cancel_diagnostic(
                entry,
                source=(
                    exact_matches_by_entry_id[int(entry.id)].source
                    if int(entry.id) in exact_matches_by_entry_id else "none"
                ),
                match_type=(
                    exact_matches_by_entry_id[int(entry.id)].cancel_type
                    if int(entry.id) in exact_matches_by_entry_id else "unknown"
                ),
                status="not_attempted",
                reason="preflight_failed_for_batch",
            )
            for entry in deferred_entries
            if int(entry.id) not in diagnosed_entry_ids
        )
        raise DeferredEntryCancellationError(
            "deferred_entry_cancel_exchange_state_drift", diagnostics
        )
    return exact_matches


def _deferred_cancel_source(cancel_type: str) -> str:
    return "pending_trigger_orders" if cancel_type == "trigger" else "open_orders"


def _deferred_cancel_diagnostic(
    entry: ExecutionOrderLeg,
    *,
    source: str,
    match_type: str,
    status: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "execution_order_leg_id": int(entry.id),
        "live_match_source": source,
        "match_type": match_type,
        "status": status,
        "reason": reason,
    }


def _persist_deferred_cancel_diagnostics(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    diagnostics: list[dict[str, Any]],
    created_at: datetime,
) -> None:
    with session_factory() as session:
        for diagnostic in diagnostics[:20]:
            record_execution_event(
                session_factory,
                ExecutionEventRecord(
                    execution_binding_id=binding.id,
                    strategy_instance_id=batch.strategy_instance_id,
                    venue="deepcoin",
                    action="strategy_management_deferred_entry_cancel_diagnostic",
                    status="failed",
                    kol_id=binding.kol_id,
                    chat_id=binding.chat_id,
                    message_id=binding.message_id,
                    source_message_id=batch.raw_message_id,
                    symbol=binding.symbol,
                    side=binding.side,
                    reason=str(diagnostic.get("reason") or "preflight_failed")[:255],
                    after=diagnostic,
                    created_at=created_at,
                ),
                session=session,
            )
        session.commit()


def _cancel_deferred_entry_legs(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    deepcoin_client: DeepcoinTradingClientProtocol,
    cancelled_at: datetime,
) -> None:
    """Cancel only batch-snapshotted deferred entry orders before a full exit."""

    deferred_entries = _load_exact_deferred_entry_legs(
        session_factory, batch=batch
    )
    try:
        exact_matches = _match_exact_deferred_exchange_orders(
            deferred_entries=deferred_entries,
            binding=binding,
            deepcoin_client=deepcoin_client,
        )
    except DeferredEntryCancellationError as exc:
        _persist_deferred_cancel_diagnostics(
            session_factory,
            batch=batch,
            binding=binding,
            diagnostics=exc.diagnostics,
            created_at=cancelled_at,
        )
        raise
    with session_factory() as session:
        if any(
            not _deferred_entry_still_matches_snapshot(
                session.get(ExecutionOrderLeg, detached_entry.id),
                detached_entry=detached_entry,
                batch=batch,
            )
            for detached_entry in deferred_entries
        ):
            raise ManagementBatchExecutionError(
                "deferred_entry_cancel_leg_not_pending"
            )
    inst_id = normalize_deepcoin_swap_instrument(binding.symbol)
    for match_index, match in enumerate(exact_matches):
        detached_entry = match.entry
        cancel_type = match.cancel_type
        exchange_order = match.order
        order_id = match.order_id
        client_order_id = match.client_order_id
        cancel_payload: dict[str, Any] = {"instId": inst_id}
        if order_id:
            cancel_payload["ordId"] = order_id
        if client_order_id:
            cancel_payload["clOrdId"] = client_order_id
        try:
            _require_remediation_live_gate(session_factory, batch=batch)
            if cancel_type == "trigger":
                response = deepcoin_client.cancel_trigger_order(cancel_payload)
                action = "strategy_management_cancel_deferred_trigger_entry"
            else:
                cancel_payload["mrgPosition"] = normalize_deepcoin_position_mode(
                    binding.position_mode
                )
                response = deepcoin_client.cancel_order(cancel_payload)
                action = "strategy_management_cancel_deferred_regular_entry"
        except Exception as exc:
            diagnostics = [
                _deferred_cancel_diagnostic(
                    detached_entry,
                    source=match.source,
                    match_type=cancel_type,
                    status="unresolved",
                    reason=f"cancel_{type(exc).__name__}"[:80],
                )
            ]
            diagnostics.extend(
                _deferred_cancel_diagnostic(
                    remaining.entry,
                    source=remaining.source,
                    match_type=remaining.cancel_type,
                    status="not_attempted",
                    reason="earlier_cancel_failed",
                )
                for remaining in exact_matches[match_index + 1 :]
            )
            _persist_deferred_cancel_diagnostics(
                session_factory,
                batch=batch,
                binding=binding,
                diagnostics=diagnostics,
                created_at=cancelled_at,
            )
            raise

        with session_factory() as session:
            entry = session.get(ExecutionOrderLeg, detached_entry.id)
            if not _deferred_entry_still_matches_snapshot(
                entry,
                detached_entry=detached_entry,
                batch=batch,
            ):
                raise ManagementBatchExecutionError(
                    "deferred_entry_cancel_leg_not_pending"
                )
            entry.status = "cancelled"
            entry.terminal_reason = (
                "management_full_close_cancelled_unfilled_entry_leg"
            )
            entry.last_verified_at = cancelled_at
            entry.updated_at = cancelled_at
            record_execution_event(
                session_factory,
                ExecutionEventRecord(
                    execution_binding_id=binding.id,
                    strategy_instance_id=batch.strategy_instance_id,
                    venue="deepcoin",
                    action=action,
                    kol_id=binding.kol_id,
                    chat_id=binding.chat_id,
                    message_id=binding.message_id,
                    source_message_id=batch.raw_message_id,
                    symbol=binding.symbol,
                    side=binding.side,
                    order_id=order_id,
                    client_order_id=client_order_id,
                    reason="management_full_close_cancelled_unfilled_entry_leg",
                    before=exchange_order,
                    after=_deferred_cancel_diagnostic(
                        detached_entry,
                        source=match.source,
                        match_type=cancel_type,
                        status="resolved",
                        reason="exchange_cancel_confirmed",
                    ),
                    request=cancel_payload,
                    response=response,
                    created_at=cancelled_at,
                ),
                session=session,
            )
            session.commit()


def _deferred_entry_still_matches_snapshot(
    entry: ExecutionOrderLeg | None,
    *,
    detached_entry: ExecutionOrderLeg,
    batch: ManagementBatchRecord,
) -> bool:
    status = str(getattr(entry, "status", "") or "").lower()
    return bool(
        entry is not None
        and entry.execution_binding_id == batch.execution_binding_id
        and entry.strategy_instance_id == batch.strategy_instance_id
        and entry.purpose == "entry"
        and status in _DEFERRED_ENTRY_LEG_STATES
        and status not in TERMINAL_ENTRY_LEG_STATES
        and entry.terminal_reason is None
        and not entry.pos_id
        and entry.order_id == detached_entry.order_id
        and entry.client_order_id == detached_entry.client_order_id
    )


def _close_payload(
    *, binding: ExecutionBinding, pos_id: str, close_size: str, client_order_id: str
) -> dict[str, str]:
    return {
        "instId": normalize_deepcoin_swap_instrument(binding.symbol),
        "tdMode": normalize_deepcoin_margin_mode(binding.margin_mode),
        "side": "sell" if binding.side.lower() == "long" else "buy",
        "posSide": binding.side.lower(),
        "ordType": "market",
        "sz": close_size,
        "mrgPosition": normalize_deepcoin_position_mode(binding.position_mode),
        "closePosId": str(pos_id),
        "clOrdId": client_order_id,
    }


def _extract_order_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    rows = [response]
    data = response.get("data")
    if isinstance(data, dict):
        rows.append(data)
    elif isinstance(data, list):
        rows.extend(row for row in data if isinstance(row, dict))
    for row in rows:
        for key in ("ordId", "orderId", "order_id", "id", "orderSysID"):
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _record_leg_event(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    leg_id: int,
    pos_id: str,
    client_order_id: str,
    request: dict[str, Any],
    response: dict[str, Any] | None,
    order_id: str | None,
    status: str,
    reason: str,
    created_at: datetime,
) -> None:
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            execution_binding_id=binding.id,
            strategy_instance_id=batch.strategy_instance_id,
            venue="deepcoin",
            action="strategy_management_close_submit",
            status=status,
            kol_id=binding.kol_id,
            chat_id=binding.chat_id,
            message_id=binding.message_id,
            source_message_id=batch.raw_message_id,
            symbol=binding.symbol,
            side=binding.side,
            order_id=order_id,
            client_order_id=client_order_id,
            pos_id=pos_id,
            reason=reason,
            before={"management_batch_id": batch.id, "management_leg_id": leg_id},
            after={"management_batch_id": batch.id, "management_leg_id": leg_id},
            request={**request, "managementBatchId": batch.id, "managementLegId": leg_id},
            response=response,
            created_at=created_at,
        ),
    )


def _result(batch: ManagementBatchRecord, *, reason: str) -> dict[str, Any]:
    return {
        "submitted": any(
            leg.status in {"submitted", "succeeded"} for leg in batch.legs
        ),
        "batch_id": batch.id,
        "status": batch.status,
        "reason": reason,
        "legs": [
            {
                "leg_id": leg.id,
                "pos_id": leg.pos_id,
                "status": leg.status,
                "client_order_id": leg.client_order_id,
                "order_id": leg.exchange_order_id,
            }
            for leg in batch.legs
        ],
    }
