"""Execute one ``adjust_take_profit`` management batch (design 3.4 / 3.6 / 3.7).

The planner froze *which* strategy and positions the instruction is about and
*what* the message asked (``target_snapshot['take_profit_adjustment']``). This
module decides, under the position authority lock and from fresh exchange
reads, what each position's take profits become, and then either reports it
(``shadow``) or makes it so (``live``). No new order-writing code: stops go
through ``protection_replacement.replace_stop_group`` (new first), take profits
through ``replace_take_profit_group`` (old first), and both through the
position mutation gateway.

Order of events for one live batch:

1. Reads: exact live position, complete ``trigger-orders-pending``, a last
   price. ``resolve_protection_authority`` names the position's orders. Any
   read that is not complete is "unknown": refuse, zero writes.
2. Plan every position with :func:`plan_take_profit_adjustment`. Any refusal
   refuses the whole batch before the first write.
3. A stop named in the same message goes first (``replace_stop_group``). If it
   fails the take profits are not touched.
4. In one transaction: the take-profit convergence plan becomes the new plan
   and the protection legs of the orders being replaced are ``superseded``
   (R4), so the convergence worker never pulls the position back to the entry
   plan.
5. ``replace_take_profit_group``: cancel old, prove them gone, place new, read
   back. A cancel that fails leaves the old ones armed and the step-4 rewrite is
   undone. A place that fails after the cancel leaves the new plan in place and
   hands the retry to the convergence worker.

Every refusal and failure ends the batch (``blocked``) and notifies; nothing is
left non-terminal without a deadline.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLeg,
    PositionProtectionLedger,
    PositionTakeProfitOrder,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.position_authority_lock import position_authority_lock
from telegram_kol_research.position_mutation_gateway import exact_position_write_gate
from telegram_kol_research.protection_authority import (
    ProtectionAuthority,
    adopt_protection_orders,
    evaluate_cancel_precheck,
    resolve_protection_authority,
)
from telegram_kol_research.protection_attribution import snapshot_protection_rows
from telegram_kol_research.protection_replacement import (
    GROUP_STOP,
    GROUP_TAKE_PROFIT,
    NewProtectionOrder,
    ProtectionReplacementPlan,
    replace_stop_group,
    replace_take_profit_group,
)
from telegram_kol_research.protection_snapshot import (
    read_complete_pending_tpsl_snapshot,
)
from telegram_kol_research.strategy_management_batches import (
    ManagementBatchRecord,
    load_management_batch,
)
from telegram_kol_research.take_profit_adjustment import (
    PLAN_ALREADY_SATISFIED,
    PLAN_READY,
    TAKE_PROFIT_ADJUST_INTENT,
    ExistingTakeProfit,
    TakeProfitAdjustmentPlan,
    TakeProfitInstruction,
    plan_take_profit_adjustment,
)
from telegram_kol_research.trading_settings import load_trading_settings

logger = logging.getLogger(__name__)

REASON_DEADLINE_EXPIRED = "take_profit_adjust_deadline_expired"
REASON_DISABLED = "take_profit_adjust_disabled"
REASON_SHADOW_PLANNED = "take_profit_adjust_shadow_planned"
REASON_APPLIED = "take_profit_adjust_applied"
REASON_ALREADY_SATISFIED = "take_profit_adjust_already_satisfied"
REASON_READ_INCOMPLETE = "take_profit_adjust_exchange_read_incomplete"
REASON_QUOTE_UNAVAILABLE = "take_profit_adjust_quote_unavailable"
REASON_POSITION_NOT_FOUND = "take_profit_adjust_position_not_found"
REASON_PROTECTION_UNRESOLVED = "take_profit_adjust_protection_unresolved"
REASON_STOP_MISSING = "take_profit_adjust_stop_missing"
REASON_PRICE_TICK_INVALID = "take_profit_adjust_price_tick_invalid"
REASON_STOP_NOT_TIGHTENING = "explicit_stop_adjustment_not_risk_tightening"
REASON_STOP_REPLACE_FAILED = "take_profit_adjust_stop_replace_failed"
REASON_TAKE_PROFIT_REPLACE_INCOMPLETE = "take_profit_replace_incomplete"
REASON_SNAPSHOT_INVALID = "take_profit_adjust_snapshot_invalid"
REASON_INTERRUPTED = "take_profit_adjust_interrupted"
REASON_EXECUTION_ERROR = "take_profit_adjust_execution_error"
REASON_ADOPTION_FAILED = "protection_order_adoption_failed"

_TERMINAL_BATCH_STATUSES = frozenset({"succeeded", "blocked", "resolved"})
_ACTIVE_PROTECTION_LEG_STATUSES_EXCLUDED = frozenset(
    {"superseded", "filled", "retired", "cancelled"}
)


class TakeProfitAdjustmentExecutionError(RuntimeError):
    """The batch handed to this executor is not a take-profit adjustment."""


@dataclass(slots=True)
class _Position:
    leg: Any
    live: dict[str, Any]
    authority: ProtectionAuthority
    plan: TakeProfitAdjustmentPlan
    stop_change: bool
    evidence: dict[str, Any] = field(default_factory=dict)


def execute_take_profit_adjustment_batch(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    deepcoin_client: Any,
    executed_at: datetime | None = None,
) -> dict[str, Any]:
    """Run one adjustment batch to a terminal state. Safe to call again."""

    now = executed_at or datetime.now(UTC)
    with position_authority_lock():
        batch = load_management_batch(session_factory, int(batch_id))
        if batch.intent != TAKE_PROFIT_ADJUST_INTENT:
            raise TakeProfitAdjustmentExecutionError(
                f"batch_intent_not_take_profit_adjustment:{batch.intent}"
            )
        if batch.status in _TERMINAL_BATCH_STATUSES or batch.status not in {
            "ready",
            "executing",
        }:
            return _result(batch, reason=batch.reason_code, writes=False)
        try:
            return _execute_locked(
                session_factory,
                batch=batch,
                deepcoin_client=deepcoin_client,
                now=now,
            )
        except Exception:
            # An unexpected failure must still end the batch: a non-terminal
            # batch holds the strategy's only management slot.
            logger.exception("take-profit adjustment batch %s failed", batch.id)
            writes = _any_leg_left_planned_state(session_factory, batch.id)
            _finalize(
                session_factory,
                batch_id=batch.id,
                expected={"ready", "executing"},
                status="blocked",
                reason=REASON_EXECUTION_ERROR,
                now=now,
            )
            return _result(
                load_management_batch(session_factory, batch.id),
                reason=REASON_EXECUTION_ERROR,
                writes=writes,
            )


def _execute_locked(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    deepcoin_client: Any,
    now: datetime,
) -> dict[str, Any]:
    if _deadline_passed(session_factory, batch.id, now):
        return _refuse(session_factory, batch, REASON_DEADLINE_EXPIRED, now)
    if batch.status == "ready":
        if not _claim(session_factory, batch.id, now):
            batch = load_management_batch(session_factory, batch.id)
            return _result(batch, reason=batch.reason_code, writes=False)
        batch = load_management_batch(session_factory, batch.id)
    if any(leg.status != "planned" for leg in batch.legs):
        # A previous run got as far as reserving a write and never finished.
        # What reached the exchange is not known here; a person decides.
        return _refuse(session_factory, batch, REASON_INTERRUPTED, now, writes=True)
    mode = load_trading_settings(session_factory).take_profit_adjust_mode
    if mode == "disabled":
        return _refuse(session_factory, batch, REASON_DISABLED, now)

    snapshot = batch.target_snapshot if isinstance(batch.target_snapshot, dict) else {}
    adjustment = snapshot.get("take_profit_adjustment")
    spec = snapshot.get("contract_spec")
    try:
        instruction = TakeProfitInstruction.from_dict(adjustment["instruction"])
        quantity_step = Decimal(str(spec["quantity_step"]))
        min_quantity = Decimal(str(spec["min_quantity"]))
        price_tick = Decimal(str(spec["price_tick"]))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return _refuse(session_factory, batch, REASON_SNAPSHOT_INVALID, now)
    if not batch.legs:
        return _refuse(session_factory, batch, REASON_SNAPSHOT_INVALID, now)

    with session_factory() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        if (
            binding is None
            or lifecycle is None
            or binding.strategy_instance_id != batch.strategy_instance_id
            or str(binding.status or "").lower() not in {"active", "open", "partial"}
        ):
            return _refuse(
                session_factory, batch, "batch_binding_not_active_or_exact", now
            )
        session.expunge(binding)
        strategy_take_profit = lifecycle.take_profit
        symbol = str(lifecycle.symbol or "").upper()
    side = str(binding.side or "").lower()
    instrument_id = f"{symbol}-USDT-SWAP"

    if instruction.stop_loss is not None:
        from telegram_kol_research.management_stop_price_gate import (
            record_stop_gate_rejection,
            validate_batch_stops,
        )

        gate = validate_batch_stops(
            session_factory, batch=batch, client=deepcoin_client, now=now
        )
        if gate is not None:
            refused = _refuse(session_factory, batch, gate.reason_code, now)
            record_stop_gate_rejection(
                session_factory,
                batch_id=batch.id,
                raw_message_id=batch.raw_message_id,
                result=gate,
                now=now,
            )
            return refused

    # 1. Reads. Unknown is never "nothing there".
    try:
        live_positions = [
            dict(row)
            for row in deepcoin_client.list_positions(inst_id=instrument_id)
            if isinstance(row, dict)
        ]
        pending = read_complete_pending_tpsl_snapshot(
            deepcoin_client, instrument_id=instrument_id
        )
    except Exception:
        logger.warning(
            "take-profit adjustment %s exchange read incomplete", batch.id, exc_info=True
        )
        return _refuse(session_factory, batch, REASON_READ_INCOMPLETE, now)
    last_price = _quote_price(deepcoin_client, instrument_id)
    if last_price is None:
        return _refuse(session_factory, batch, REASON_QUOTE_UNAVAILABLE, now)
    strategy_prices = _strategy_prices(strategy_take_profit, symbol)

    # 2. Plan every position before anything is written.
    positions: list[_Position] = []
    for leg in batch.legs:
        live = _exact_live_position(
            live_positions, pos_id=str(leg.pos_id), inst_id=instrument_id, side=side
        )
        if live is None:
            return _refuse(session_factory, batch, REASON_POSITION_NOT_FOUND, now)
        with session_factory() as session:
            authority = resolve_protection_authority(
                session,
                venue="deepcoin",
                pos_id=str(leg.pos_id),
                instrument_id=instrument_id,
                side=side,
                pending_rows=pending,
            )
            filled_prices = [
                row.trigger_price
                for row in session.query(PositionProtectionLedger)
                .filter(PositionProtectionLedger.venue == "deepcoin")
                .filter(PositionProtectionLedger.pos_id == str(leg.pos_id))
                .filter(PositionProtectionLedger.purpose == "take_profit")
                .filter(PositionProtectionLedger.status == "filled")
                .all()
                if row.trigger_price not in (None, "")
            ]
        if not authority.resolved or authority.execution_order_leg_id != int(
            leg.execution_order_leg_id
        ):
            from telegram_kol_research.deepcoin_execution_actions import (
                _capture_protection_authority_refusal,
            )

            _capture_protection_authority_refusal(
                session_factory, binding=binding, authority=authority, now=now
            )
            return _refuse(session_factory, batch, REASON_PROTECTION_UNRESOLVED, now)
        if not authority.stop_orders:
            # The take-profit sequence cancels before it places; that window
            # is safe only because the stop stays armed through it.
            return _refuse(session_factory, batch, REASON_STOP_MISSING, now)
        plan = plan_take_profit_adjustment(
            instruction=instruction,
            side=side,
            remaining_size=_first(live, "pos", "size"),
            last_price=last_price,
            existing_take_profits=[
                ExistingTakeProfit(
                    price=str(ref.trigger_price or ""),
                    size=str(ref.size_text or ""),
                    order_id=ref.order_id,
                )
                for ref in authority.take_profit_orders
            ],
            filled_prices=filled_prices,
            strategy_take_profit_prices=strategy_prices,
            min_quantity=min_quantity,
            quantity_step=quantity_step,
        )
        evidence = {
            "pos_id": str(leg.pos_id),
            "remaining_size": str(_first(live, "pos", "size")),
            "last_price": str(last_price),
            "existing_take_profits": [
                [ref.trigger_price, ref.size_text, ref.order_id]
                for ref in authority.take_profit_orders
            ],
            "stops": [
                [ref.trigger_price, ref.size_text, ref.order_id]
                for ref in authority.stop_orders
            ],
            "plan": plan.as_evidence(),
        }
        if plan.status not in {PLAN_READY, PLAN_ALREADY_SATISFIED}:
            _record_leg_evidence(session_factory, leg.id, evidence, now)
            return _refuse(session_factory, batch, str(plan.reason_code), now)
        if any(
            not _aligned(Decimal(price), price_tick) for price, _ in plan.place
        ):
            _record_leg_evidence(session_factory, leg.id, evidence, now)
            return _refuse(session_factory, batch, REASON_PRICE_TICK_INVALID, now)
        stop_change = False
        if instruction.stop_loss is not None:
            verdict = _stop_verdict(
                authority, stop=Decimal(instruction.stop_loss), side=side
            )
            evidence["stop"] = {"requested": instruction.stop_loss, "verdict": verdict}
            if verdict == "not_tightening":
                _record_leg_evidence(session_factory, leg.id, evidence, now)
                return _refuse(session_factory, batch, REASON_STOP_NOT_TIGHTENING, now)
            stop_change = verdict == "tightens"
        positions.append(
            _Position(
                leg=leg,
                live=live,
                authority=authority,
                plan=plan,
                stop_change=stop_change,
                evidence=evidence,
            )
        )

    for position in positions:
        _record_leg_evidence(session_factory, position.leg.id, position.evidence, now)
    nothing_to_do = all(
        position.plan.status == PLAN_ALREADY_SATISFIED and not position.stop_change
        for position in positions
    )
    if nothing_to_do:
        _set_leg_status(session_factory, batch, "succeeded", now)
        _finalize(
            session_factory,
            batch_id=batch.id,
            expected={"executing"},
            status="succeeded",
            reason=REASON_ALREADY_SATISFIED,
            now=now,
        )
        return _result(
            load_management_batch(session_factory, batch.id),
            reason=REASON_ALREADY_SATISFIED,
            writes=False,
        )
    if mode != "live":
        # Design 3.7 ``shadow``: the whole plan is on the legs and in the
        # notification; nothing reaches the exchange.
        _set_leg_status(session_factory, batch, "blocked", now)
        _finalize(
            session_factory,
            batch_id=batch.id,
            expected={"executing"},
            status="blocked",
            reason=REASON_SHADOW_PLANNED,
            now=now,
        )
        return _result(
            load_management_batch(session_factory, batch.id),
            reason=REASON_SHADOW_PLANNED,
            writes=False,
            shadow=True,
        )

    return _execute_live(
        session_factory,
        batch=batch,
        binding=binding,
        instrument_id=instrument_id,
        instruction=instruction,
        positions=positions,
        deepcoin_client=deepcoin_client,
        now=now,
    )


def _execute_live(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    instrument_id: str,
    instruction: TakeProfitInstruction,
    positions: list[_Position],
    deepcoin_client: Any,
    now: datetime,
) -> dict[str, Any]:
    from telegram_kol_research.strategy_management_executor import (
        _protection_payload_common,
        _protection_row_payload,
    )

    wrote = False
    failure: str | None = None
    for position in positions:
        leg = position.leg
        pos_id = str(leg.pos_id)
        if failure is not None:
            _transition_leg(session_factory, leg.id, "blocked", now, {"reason": "not_attempted"})
            continue
        if position.plan.status == PLAN_ALREADY_SATISFIED and not position.stop_change:
            _transition_leg(session_factory, leg.id, "succeeded", now, None)
            continue
        if position.authority.adoptions:
            try:
                with session_factory() as session:
                    adopt_protection_orders(
                        session,
                        authority=position.authority,
                        venue="deepcoin",
                        adopted_at=now,
                    )
                    session.commit()
            except Exception:
                logger.warning("take-profit adjustment adoption failed", exc_info=True)
                failure = REASON_ADOPTION_FAILED
                _transition_leg(session_factory, leg.id, "blocked", now, {"reason": failure})
                continue
        # Off ``planned`` before the first write: from here on the batch can
        # no longer be superseded, only ended by its own outcome or deadline.
        # Idempotency keys start ``management:{batch}:`` like every other
        # management write, so the instruction-execution contract sees them.
        _transition_leg(session_factory, leg.id, "reserved", now, None)
        gate = lambda target=pos_id: exact_position_write_gate(  # noqa: E731
            session_factory, pos_id=target
        )

        def pre_cancel_check(rows, order_id, authority=position.authority):
            return evaluate_cancel_precheck(authority, rows, str(order_id))

        common = _protection_payload_common(
            binding=binding, position=position.live, inst_id=instrument_id
        )

        # 3. Stop first, new-first.
        if position.stop_change:
            # One new stop per old one, same size and same ledger purpose
            # (primary or backup), at the price the message named -- the
            # ``adjust_stop_loss`` rule of moving every stop the position has.
            stop_rows = []
            for ref, row in zip(
                position.authority.stop_orders,
                snapshot_protection_rows(
                    [dict(ref.row) for ref in position.authority.stop_orders]
                ),
            ):
                replaced = dict(row)
                replaced["trigger_price"] = str(instruction.stop_loss)
                replaced["ledger_purpose"] = ref.purpose
                stop_rows.append(replaced)
            stop_plan = ProtectionReplacementPlan(
                venue="deepcoin",
                pos_id=pos_id,
                instrument_id=instrument_id,
                execution_binding_id=int(binding.id),
                execution_order_leg_id=int(leg.execution_order_leg_id),
                group=GROUP_STOP,
                new_orders=tuple(
                    NewProtectionOrder(
                        purpose=str(row["ledger_purpose"]),
                        payload=_protection_row_payload(common=common, row=row),
                    )
                    for row in stop_rows
                ),
                old_order_ids=tuple(
                    ref.order_id for ref in position.authority.stop_orders
                ),
                idempotency_prefix=f"management:{batch.id}:{leg.id}:tp-adjust:stop",
            )
            wrote = True
            stop_result = replace_stop_group(
                session_factory,
                plan=stop_plan,
                deepcoin_client=deepcoin_client,
                executed_at=now,
                live_execution_gate=gate,
                pre_cancel_check=pre_cancel_check,
            )
            position.evidence["stop_replacement"] = {
                "status": stop_result.status,
                "reason_code": stop_result.reason_code,
                "new_order_ids": list(stop_result.new_order_ids),
                "cancelled_order_ids": list(stop_result.cancelled_order_ids),
            }
            if not stop_result.succeeded:
                failure = REASON_STOP_REPLACE_FAILED
                _transition_leg(
                    session_factory, leg.id, "failed", now, position.evidence
                )
                continue
            _record_stop_replacement(
                session_factory,
                batch=batch,
                binding=binding,
                leg=leg,
                instrument_id=instrument_id,
                stop_rows=stop_rows,
                new_order_ids=list(stop_result.new_order_ids),
                take_profit_orders=position.authority.take_profit_orders,
                now=now,
            )

        # 4 + 5. Take profits: rewrite the convergence plan, then old-first.
        plan = position.plan
        if plan.status == PLAN_READY:
            rewrite = _rewrite_take_profit_plan(
                session_factory,
                leg=leg,
                plan=plan,
                now=now,
            )
            tp_plan = ProtectionReplacementPlan(
                venue="deepcoin",
                pos_id=pos_id,
                instrument_id=instrument_id,
                execution_binding_id=int(binding.id),
                execution_order_leg_id=int(leg.execution_order_leg_id),
                group=GROUP_TAKE_PROFIT,
                new_orders=tuple(
                    NewProtectionOrder(
                        purpose="take_profit",
                        payload=_protection_row_payload(
                            common=common,
                            row={
                                "purpose": "take_profit",
                                "trigger_price": price,
                                "size": size,
                                "trigger_type": "last",
                                "order_price": "-1",
                            },
                        ),
                    )
                    for price, size in plan.place
                ),
                old_order_ids=plan.cancel_order_ids,
                idempotency_prefix=f"management:{batch.id}:{leg.id}:tp-adjust:tp",
            )
            wrote = True
            tp_result = replace_take_profit_group(
                session_factory,
                plan=tp_plan,
                deepcoin_client=deepcoin_client,
                executed_at=now,
                live_execution_gate=gate,
                pre_cancel_check=pre_cancel_check,
            )
            position.evidence["take_profit_replacement"] = {
                "status": tp_result.status,
                "reason_code": tp_result.reason_code,
                "new_order_ids": list(tp_result.new_order_ids),
                "cancelled_order_ids": list(tp_result.cancelled_order_ids),
            }
            cancel_phase_done = tp_result.succeeded or str(
                tp_result.reason_code or ""
            ).startswith("take_profit_replacement_")
            if not cancel_phase_done:
                # Nothing new placed and the old take profits are armed except
                # any whose cancel the exchange already accepted: the plan goes
                # back as it was, and those accepted cancels stay retired in our
                # model. ``take_profit_replace_incomplete`` asks a person to
                # settle the rest (protection_replacement records it).
                _undo_rewrite(
                    session_factory,
                    rewrite,
                    now=now,
                    cancelled_order_ids=set(tp_result.cancelled_order_ids),
                )
            else:
                _record_replacement(
                    session_factory,
                    leg=leg,
                    instrument_id=instrument_id,
                    placed=list(zip(plan.place, tp_result.new_order_ids)),
                    cancelled=list(plan.cancel_order_ids),
                    rewrite=rewrite,
                    deepcoin_client=deepcoin_client,
                    now=now,
                )
                if not tp_result.succeeded:
                    # Cancelled, not all placed. The stop is untouched; the
                    # convergence worker retries the *new* plan (design 3.4).
                    _ready_convergence_for_retry(session_factory, rewrite, now=now)
            if not tp_result.succeeded:
                failure = REASON_TAKE_PROFIT_REPLACE_INCOMPLETE
                _transition_leg(
                    session_factory, leg.id, "failed", now, position.evidence
                )
                continue
        _transition_leg(session_factory, leg.id, "succeeded", now, position.evidence)

    final_reason = failure or REASON_APPLIED
    _finalize(
        session_factory,
        batch_id=batch.id,
        expected={"executing"},
        status="blocked" if failure else "succeeded",
        reason=final_reason,
        now=now,
    )
    return _result(
        load_management_batch(session_factory, batch.id),
        reason=final_reason,
        writes=wrote,
    )


def _record_stop_replacement(
    session_factory: sessionmaker,
    *,
    batch: ManagementBatchRecord,
    binding: ExecutionBinding,
    leg: Any,
    instrument_id: str,
    stop_rows: list[dict[str, Any]],
    new_order_ids: list[str],
    take_profit_orders,
    now: datetime,
) -> None:
    """Move the logical protection model onto the new stops.

    The ledger already holds the new stops (the gateway wrote them under their
    own purposes) and the old ones are retired (the replacement's absence
    proof). With exactly one primary and one backup, the existing atomic
    writer re-points the logical legs and the backup-stop record, carrying the
    position's current take profits along; otherwise a replacing revision is
    appended, the same fallback ``adjust_stop_loss`` uses.
    """

    from telegram_kol_research.protection_replacement_persistence import (
        VerifiedProtectionReplacement,
        persist_verified_protection_replacement,
    )
    from telegram_kol_research.protection_revisions import (
        record_replacing_protection_revision,
    )

    role_for = {"stop_loss": "primary_stop", "backup_stop": "backup_stop"}
    replacements = [
        VerifiedProtectionReplacement(
            role=role_for.get(str(row["ledger_purpose"]), "primary_stop"),
            order_id=str(order_id),
            trigger_price=str(row["trigger_price"]),
            size_text=str(row.get("size")) if row.get("size") is not None else None,
        )
        for row, order_id in zip(stop_rows, new_order_ids)
    ]
    replacements.extend(
        VerifiedProtectionReplacement(
            role="take_profit",
            order_id=str(ref.order_id),
            trigger_price=str(ref.trigger_price),
            size_text=ref.size_text,
        )
        for ref in take_profit_orders
    )
    roles = [row.role for row in replacements]
    with session_factory() as session:
        if roles.count("primary_stop") == 1 and roles.count("backup_stop") == 1:
            persist_verified_protection_replacement(
                session,
                venue="deepcoin",
                execution_binding_id=int(binding.id),
                execution_order_leg_id=int(leg.execution_order_leg_id),
                strategy_instance_id=batch.strategy_instance_id,
                pos_id=str(leg.pos_id),
                instrument_id=instrument_id,
                side=str(binding.side or "").lower(),
                source="take_profit_adjustment_stop_replacement",
                replacement_identity=f"management:{batch.id}:{leg.id}:tp-adjust:stop",
                replacements=tuple(replacements),
                seen_at=now,
            )
        else:
            record_replacing_protection_revision(
                session,
                execution_binding_id=int(binding.id),
                execution_order_leg_id=int(leg.execution_order_leg_id),
                strategy_instance_id=batch.strategy_instance_id,
                pos_id=str(leg.pos_id),
                source="take_profit_adjustment_stop_replacement",
                protection_json={
                    "order_ids": [row.order_id for row in replacements],
                    "roles": roles,
                    "management_batch_id": batch.id,
                },
            )
        session.commit()


# ---------------------------------------------------------------- R4


@dataclass(slots=True)
class _Rewrite:
    convergence_id: int | None
    previous_plan_json: str | None
    previous_status: str | None
    superseded: list[tuple[int, str]]
    created_leg_ids: list[int]
    created_by_price: dict[str, int]


def _rewrite_take_profit_plan(
    session_factory: sessionmaker,
    *,
    leg: Any,
    plan: TakeProfitAdjustmentPlan,
    now: datetime,
) -> _Rewrite:
    """Point the convergence plan and the logical legs at the new plan (R4).

    One transaction. Kept orders keep their legs; every other take-profit leg
    of this position that is still live in our model is ``superseded`` and a
    fresh leg is written per order to place, bound to the position and waiting
    for its exchange id -- the shape the convergence worker expects for a
    target it still has to place.
    """

    entry_leg_id = int(leg.execution_order_leg_id)
    pos_id = str(leg.pos_id)
    keep = set(plan.keep_order_ids)
    with session_factory() as session:
        convergence = (
            session.query(TriggerTakeProfitConvergence)
            .filter(TriggerTakeProfitConvergence.venue == "deepcoin")
            .filter(TriggerTakeProfitConvergence.execution_order_leg_id == entry_leg_id)
            .one_or_none()
        )
        rewrite = _Rewrite(
            convergence_id=int(convergence.id) if convergence is not None else None,
            previous_plan_json=(
                convergence.desired_take_profits_json if convergence is not None else None
            ),
            previous_status=convergence.status if convergence is not None else None,
            superseded=[],
            created_leg_ids=[],
            created_by_price={},
        )
        if convergence is not None:
            convergence.desired_take_profits_json = json.dumps(
                [
                    {"allocation_pct": allocation, "price": price}
                    for (price, _), allocation in zip(plan.targets, plan.allocations)
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            convergence.updated_at = now
        legs = (
            session.query(PositionProtectionLeg)
            .filter(PositionProtectionLeg.venue == "deepcoin")
            .filter(PositionProtectionLeg.execution_order_leg_id == entry_leg_id)
            .filter(PositionProtectionLeg.role == "take_profit")
            .order_by(PositionProtectionLeg.id.asc())
            .all()
        )
        for row in legs:
            if str(row.status or "") in _ACTIVE_PROTECTION_LEG_STATUSES_EXCLUDED:
                continue
            if row.pos_id not in (None, "", pos_id):
                continue
            if row.exchange_order_id and str(row.exchange_order_id) in keep:
                continue
            rewrite.superseded.append((int(row.id), str(row.status or "")))
            row.status = "superseded"
            row.updated_at = now
        next_index = max((int(row.leg_index) for row in legs), default=0)
        for price, size in plan.place:
            next_index += 1
            created = PositionProtectionLeg(
                venue="deepcoin",
                execution_binding_id=int(
                    session.get(ExecutionOrderLeg, entry_leg_id).execution_binding_id
                ),
                execution_order_leg_id=entry_leg_id,
                role="take_profit",
                leg_index=next_index,
                planned_trigger_price=price,
                planned_size=size,
                pos_id=pos_id,
                status="protection_recovery_pending",
                created_at=now,
                updated_at=now,
            )
            session.add(created)
            session.flush()
            rewrite.created_leg_ids.append(int(created.id))
            rewrite.created_by_price[price] = int(created.id)
        session.commit()
    return rewrite


def _undo_rewrite(
    session_factory: sessionmaker,
    rewrite: _Rewrite,
    *,
    now: datetime,
    cancelled_order_ids: set[str] = frozenset(),
) -> None:
    with session_factory() as session:
        if rewrite.convergence_id is not None:
            convergence = session.get(TriggerTakeProfitConvergence, rewrite.convergence_id)
            if convergence is not None and rewrite.previous_plan_json is not None:
                convergence.desired_take_profits_json = rewrite.previous_plan_json
                convergence.updated_at = now
        for leg_id, status in rewrite.superseded:
            row = session.get(PositionProtectionLeg, leg_id)
            if row is not None and str(row.exchange_order_id or "") in cancelled_order_ids:
                continue
            if row is not None and row.status == "superseded":
                row.status = status
                row.updated_at = now
        for leg_id in rewrite.created_leg_ids:
            row = session.get(PositionProtectionLeg, leg_id)
            if row is not None and not row.exchange_order_id:
                row.status = "superseded"
                row.updated_at = now
        session.commit()


def _ready_convergence_for_retry(
    session_factory: sessionmaker, rewrite: _Rewrite, *, now: datetime
) -> None:
    if rewrite.convergence_id is None:
        return
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, rewrite.convergence_id)
        if convergence is None or not convergence.pos_id:
            return
        convergence.status = "ready"
        convergence.reason_code = None
        convergence.error_json = None
        convergence.completed_at = None
        convergence.updated_at = now
        session.commit()


def _record_replacement(
    session_factory: sessionmaker,
    *,
    leg: Any,
    instrument_id: str,
    placed: list[tuple[tuple[str, str], str]],
    cancelled: list[str],
    rewrite: _Rewrite,
    deepcoin_client: Any,
    now: datetime,
) -> None:
    """Bind what was placed and retire what was cancelled in our own model.

    The ledger rows were already written by the gateway (new) and by the
    replacement's absence proof (old). This keeps the logical legs and the
    permanent take-profit order audit in step, so the convergence worker sees
    the new orders as the ones it owns.
    """

    from telegram_kol_research.position_protection_legs import (
        bind_verified_exchange_order,
    )
    from telegram_kol_research.position_take_profit_orders import (
        record_take_profit_cancel_requested,
        record_take_profit_cancelled,
        record_take_profit_order,
    )

    try:
        pending_rows = read_complete_pending_tpsl_snapshot(
            deepcoin_client, instrument_id=instrument_id
        )
    except Exception:
        pending_rows = None
    by_order_id = {
        str(row.get("ordId") or row.get("orderId") or ""): dict(row)
        for row in pending_rows or []
        if isinstance(row, dict)
    }
    with session_factory() as session:
        for (price, size), order_id in placed:
            leg_id = rewrite.created_by_price.get(price)
            protection_leg = (
                session.get(PositionProtectionLeg, leg_id) if leg_id is not None else None
            )
            native = by_order_id.get(str(order_id))
            if protection_leg is not None and not protection_leg.exchange_order_id:
                bind_verified_exchange_order(
                    session,
                    protection_leg,
                    exchange_order_id=str(order_id),
                    readback_evidence={
                        "source": "take_profit_adjustment_gateway_readback",
                        "native_tpsl": native,
                    },
                )
            if native is not None:
                try:
                    with session.begin_nested():
                        record_take_profit_order(
                            session,
                            venue="deepcoin",
                            execution_binding_id=int(
                                session.get(
                                    ExecutionOrderLeg, int(leg.execution_order_leg_id)
                                ).execution_binding_id
                            ),
                            execution_order_leg_id=int(leg.execution_order_leg_id),
                            trigger_take_profit_convergence_id=rewrite.convergence_id,
                            pos_id=str(leg.pos_id),
                            order_id=str(order_id),
                            trigger_price=str(price),
                            size_text=str(size),
                            created_at=now,
                            evidence={
                                "source": "native_tpsl_pending_readback",
                                "native_tpsl": native,
                            },
                        )
                except ValueError:
                    logger.warning(
                        "take-profit adjustment could not record order %s",
                        order_id,
                        exc_info=True,
                    )
        for order_id in cancelled:
            row = (
                session.query(PositionTakeProfitOrder)
                .filter(PositionTakeProfitOrder.venue == "deepcoin")
                .filter(PositionTakeProfitOrder.order_id == str(order_id))
                .one_or_none()
            )
            if row is None or row.status not in {"active", "cancel_requested"}:
                continue
            request = {"instId": instrument_id, "ordId": str(order_id)}
            record_take_profit_cancel_requested(
                session, row, request=request, requested_at=now
            )
            record_take_profit_cancelled(
                session,
                row,
                response={"source": "take_profit_adjustment_absence_confirmed"},
                cancelled_at=now,
            )
        session.commit()


# ---------------------------------------------------------------- helpers


def _stop_verdict(authority: ProtectionAuthority, *, stop: Decimal, side: str) -> str:
    """``unchanged`` when every stop already sits there, else tighten-or-refuse.

    The same direction rule ``adjust_stop_loss`` enforces: a stop named with a
    take-profit change may only move toward the price. Restating the stop the
    position already has is not a change and needs no write.
    """

    current = []
    for ref in authority.stop_orders:
        try:
            current.append(Decimal(str(ref.trigger_price)))
        except (InvalidOperation, TypeError, ValueError):
            return "not_tightening"
    if current and all(value == stop for value in current):
        return "unchanged"
    if not current:
        return "not_tightening"
    if side == "long":
        return "tightens" if all(stop > value for value in current) else "not_tightening"
    return "tightens" if all(stop < value for value in current) else "not_tightening"


def _deadline_passed(session_factory: sessionmaker, batch_id: int, now: datetime) -> bool:
    with session_factory() as session:
        row = session.get(StrategyManagementBatch, int(batch_id))
        deadline = row.execution_deadline_at if row is not None else None
    if deadline is None:
        return False
    return _naive_utc(now) >= _naive_utc(deadline)


def _claim(session_factory: sessionmaker, batch_id: int, now: datetime) -> bool:
    with session_factory() as session:
        result = session.execute(
            update(StrategyManagementBatch)
            .where(
                StrategyManagementBatch.id == int(batch_id),
                StrategyManagementBatch.status == "ready",
            )
            .values(status="executing", started_at=now, updated_at=now)
        )
        session.commit()
        return result.rowcount == 1


def _finalize(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    expected: set[str],
    status: str,
    reason: str,
    now: datetime,
) -> bool:
    """End the batch and write its notification in the same transaction."""

    from telegram_kol_research.system_operator_bot import (
        persist_strategy_management_notification_in_session,
    )

    values: dict[str, Any] = {
        "status": status,
        "reason_code": str(reason)[:64],
        "updated_at": now,
        "completed_at": now,
        "last_progress_at": now,
    }
    if status == "succeeded":
        values["reconciled_at"] = now
    with session_factory() as session:
        result = session.execute(
            update(StrategyManagementBatch)
            .where(
                StrategyManagementBatch.id == int(batch_id),
                StrategyManagementBatch.status.in_(tuple(expected)),
            )
            .values(**values)
        )
        if result.rowcount == 1:
            batch = session.get(StrategyManagementBatch, int(batch_id))
            persist_strategy_management_notification_in_session(
                session, batch, force=True
            )
        session.commit()
        return result.rowcount == 1


def _refuse(
    session_factory: sessionmaker,
    batch: ManagementBatchRecord,
    reason: str,
    now: datetime,
    *,
    writes: bool = False,
) -> dict[str, Any]:
    if not writes:
        _set_leg_status(session_factory, batch, "blocked", now, only_planned=True)
    _finalize(
        session_factory,
        batch_id=batch.id,
        expected={"ready", "executing"},
        status="blocked",
        reason=reason,
        now=now,
    )
    return _result(
        load_management_batch(session_factory, batch.id), reason=reason, writes=writes
    )


def _set_leg_status(
    session_factory: sessionmaker,
    batch: ManagementBatchRecord,
    status: str,
    now: datetime,
    *,
    only_planned: bool = True,
) -> None:
    with session_factory() as session:
        query = session.query(StrategyManagementLeg).filter(
            StrategyManagementLeg.management_batch_id == int(batch.id)
        )
        if only_planned:
            query = query.filter(StrategyManagementLeg.status == "planned")
        for row in query.all():
            row.status = status
            row.updated_at = now
        session.commit()


def _transition_leg(
    session_factory: sessionmaker,
    leg_id: int,
    status: str,
    now: datetime,
    evidence: dict[str, Any] | None,
) -> None:
    with session_factory() as session:
        row = session.get(StrategyManagementLeg, int(leg_id))
        if row is None:
            return
        row.status = status
        row.updated_at = now
        if evidence is not None:
            row.request_json = json.dumps(
                {"take_profit_adjustment": evidence},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        session.commit()


def _record_leg_evidence(
    session_factory: sessionmaker, leg_id: int, evidence: dict[str, Any], now: datetime
) -> None:
    with session_factory() as session:
        row = session.get(StrategyManagementLeg, int(leg_id))
        if row is None:
            return
        row.request_json = json.dumps(
            {"take_profit_adjustment": evidence},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        row.updated_at = now
        session.commit()


def _any_leg_left_planned_state(session_factory: sessionmaker, batch_id: int) -> bool:
    with session_factory() as session:
        return (
            session.query(StrategyManagementLeg.id)
            .filter(StrategyManagementLeg.management_batch_id == int(batch_id))
            .filter(StrategyManagementLeg.status.not_in(("planned", "blocked")))
            .first()
            is not None
        )


def _result(
    batch: ManagementBatchRecord,
    *,
    reason: str | None,
    writes: bool,
    shadow: bool = False,
) -> dict[str, Any]:
    """A result the instruction-outcome contract accepts without guessing.

    ``blocked`` promises no write, so a batch that ended blocked *after* a
    write is reported ``failed`` with ``submitted`` true.
    """

    if shadow:
        status = "shadow_planned"
    elif batch.status == "succeeded":
        status = "succeeded"
    elif batch.status == "blocked" and writes:
        status = "failed"
    elif batch.status in {"blocked", "resolved"}:
        status = "blocked"
    else:
        status = batch.status
    return {
        "status": status,
        "reason": reason,
        "batch_id": batch.id,
        "submitted": bool(writes),
        "management_action": TAKE_PROFIT_ADJUST_INTENT,
        "legs": [
            {
                "leg_id": leg.id,
                "pos_id": leg.pos_id,
                "status": leg.status,
            }
            for leg in batch.legs
        ],
    }


def _exact_live_position(
    rows: list[dict[str, Any]], *, pos_id: str, inst_id: str, side: str
) -> dict[str, Any] | None:
    matches = [
        row
        for row in rows
        if str(row.get("posId") or row.get("pos_id") or "") == str(pos_id)
    ]
    if len(matches) != 1:
        return None
    row = matches[0]
    if str(row.get("instId") or "").upper() != inst_id.upper():
        return None
    if str(row.get("posSide") or "").lower() != side:
        return None
    size = _decimal(_first(row, "pos", "size"))
    if size is None or size <= 0:
        return None
    return row


def _quote_price(client: Any, instrument_id: str) -> Decimal | None:
    from telegram_kol_research.management_stop_price_gate import read_stop_quote

    quote = read_stop_quote(client, instrument_id)
    if (
        not isinstance(quote, dict)
        or str(quote.get("instrument_id") or "").upper() != instrument_id.upper()
        or quote.get("price_field") not in {"last", "lastPx"}
    ):
        return None
    price = _decimal(quote.get("price"))
    return price if price is not None and price > 0 else None


def _strategy_prices(text: Any, symbol: str) -> list[str]:
    if text in (None, ""):
        return []
    try:
        from telegram_kol_research.price_normalization import extract_normalized_prices

        return [str(value) for value in extract_normalized_prices(text, symbol=symbol)]
    except Exception:
        return []


def _aligned(price: Decimal, tick: Decimal) -> bool:
    if tick <= 0:
        return False
    return (price % tick) == 0


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
